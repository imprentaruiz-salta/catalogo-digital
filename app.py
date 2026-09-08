import os, sqlite3, uuid, io, json, zipfile, tempfile, shutil, unicodedata, hashlib, re, urllib.request, urllib.error, urllib.parse
from datetime import datetime
from flask import (Flask, render_template, render_template_string, request, redirect,
                   url_for, session, jsonify, send_from_directory, send_file, flash)
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__, static_folder=None, template_folder=".")
app.secret_key = "catalogo_ruiz_2026_secret_x7k"
app.config['MAX_CONTENT_LENGTH']=80*1024*1024
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be mounted on a persistent disk in production. The application code
# and the data are deliberately kept separate so deploys never replace user data.
DATA_DIR=os.environ.get("CATALOGO_DATA_DIR",os.path.join(BASE_DIR,"data"))
os.makedirs(DATA_DIR,exist_ok=True)
DB_PATH=os.path.join(DATA_DIR,"catalogo.db")
UPLOAD_FOLDER=os.path.join(DATA_DIR,"uploads")
BACKUP_FOLDER=os.path.join(DATA_DIR,"backups")
VIDEO_FOLDER=os.path.join(DATA_DIR,"fleming_videos")
ZIP_STAGE=os.path.join(DATA_DIR,"zip_staging")
os.makedirs(UPLOAD_FOLDER,exist_ok=True); os.makedirs(BACKUP_FOLDER,exist_ok=True); os.makedirs(VIDEO_FOLDER,exist_ok=True); os.makedirs(ZIP_STAGE,exist_ok=True)
ALLOWED_VIDEO_EXT={"mp4","webm","mov","m4v"}
MAX_VIDEO_BYTES=80*1024*1024

# Optional durable cloud snapshot. Render Free can restart at any time, so the
# local SQLite database and optimized images are mirrored to Supabase Storage.
# The public catalog remains exactly the same; this only changes where data lives.
SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_SERVICE_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
SUPABASE_BUCKET=os.environ.get("SUPABASE_BUCKET","product-images")
SUPABASE_SNAPSHOT="system/catalogo-live.zip"

def cloud_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def cloud_endpoint(path):
    return f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"

def cloud_request(path, method="GET", data=None, content_type="application/octet-stream"):
    req=urllib.request.Request(cloud_endpoint(path), data=data, method=method, headers={
        "Authorization":f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey":SUPABASE_SERVICE_KEY,
        "Content-Type":content_type,
        "x-upsert":"true"})
    return urllib.request.urlopen(req, timeout=30)

def cloud_sync():
    """Upload one atomic catalog snapshot after a successful local change."""
    if not cloud_enabled() or not os.path.exists(DB_PATH): return False
    # Never replace a valid cloud snapshot with an empty database. This protects
    # the catalog during Render restarts or a simultaneous worker startup.
    try:
        with get_db() as check_db:
            product_count=check_db.execute('SELECT COUNT(*) FROM productos WHERE activo=1').fetchone()[0]
        if product_count == 0:
            app.logger.warning('No se sube una copia vacía a la nube; se conserva el snapshot anterior')
            return False
    except Exception:
        return False
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        z.write(DB_PATH,"base/catalogo.sqlite3")
        for root,_,files in os.walk(UPLOAD_FOLDER):
            for name in files:
                z.write(os.path.join(root,name),os.path.join("imagenes",name))
        for root,_,files in os.walk(VIDEO_FOLDER):
            for name in files:
                z.write(os.path.join(root,name),os.path.join("videos",name))
    try:
        with cloud_request(SUPABASE_SNAPSHOT,"POST",mem.getvalue(),"application/zip") as response:
            response.read()
        return True
    except Exception:
        app.logger.exception("No se pudo guardar la copia persistente")
        return False

def _db_has_products(path):
    """Check a snapshot without trusting its contents or replacing live data."""
    try:
        check=sqlite3.connect(path)
        integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
        tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        count=check.execute('SELECT COUNT(*) FROM productos WHERE activo=1').fetchone()[0] if 'productos' in tables else 0
        check.close()
        return integrity == 'ok' and count > 0
    except Exception:
        return False

def restore_from_cloud():
    """Restore only a valid, non-empty snapshot before DB initialization."""
    if not cloud_enabled(): return False
    # Never replace a usable local database with an older cloud copy.
    if os.path.exists(DB_PATH) and _db_has_products(DB_PATH): return False
    try:
        with cloud_request(SUPABASE_SNAPSHOT,"GET") as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names=z.namelist()
            if "base/catalogo.sqlite3" not in names: return False
            extracted=os.path.join(DATA_DIR,"restore-check.sqlite3")
            with z.open("base/catalogo.sqlite3") as src, open(extracted,"wb") as dst:
                shutil.copyfileobj(src,dst)
            if not _db_has_products(extracted):
                os.remove(extracted)
                app.logger.warning("Se descartó una copia persistente vacía o inválida")
                return False
            os.replace(extracted,DB_PATH)
            for name in names:
                if name.startswith("imagenes/") and not name.endswith("/"):
                    target=os.path.join(UPLOAD_FOLDER,os.path.basename(name))
                    with z.open(name) as src, open(target,"wb") as dst: shutil.copyfileobj(src,dst)
                if name.startswith("videos/") and not name.endswith("/"):
                    target=os.path.join(VIDEO_FOLDER,os.path.basename(name))
                    with z.open(name) as src, open(target,"wb") as dst: shutil.copyfileobj(src,dst)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code != 404: app.logger.warning("No se pudo restaurar la copia persistente: %s",exc)
        return False
    except Exception:
        app.logger.exception("No se pudo restaurar la copia persistente")
        return False
# Migrate a legacy local database once, if this installation had one.
_OLD_DB=os.path.join(BASE_DIR,"catalogo.db")
if not os.path.exists(DB_PATH) and os.path.exists(_OLD_DB): shutil.copy2(_OLD_DB,DB_PATH)
ALLOWED_EXT={"png","jpg","jpeg","webp","gif"}
MAX_IMAGE_SIDE=1600
MAX_IMAGE_BYTES=12*1024*1024
ADMIN_USER="admin"; ADMIN_PASS="catalogo2026"
CAT_ICONS={
 "bebidas":"🥤","panales":"👶","comestibles":"🥫","golosinas":"🍬","limpieza":"🧼","verduleria":"🥬","lacteos":"🥛","libreria":"📚","fotos":"📷","fotografia":"📷","carniceria":"🥩","panaderia":"🍞","ferreteria":"🔧","farmacia":"💊","papel higienico":"🧻","papel higienicos":"🧻","escobas":"🧹","escoba":"🧹","dentifricos":"🪥","dentifrico":"🪥","pasta dental":"🪥","pastas dentales":"🪥","jabones":"🧼","jabon":"🧼","shampoo":"🧴","desodorantes":"🧴","cuadernos":"📒","lapices":"✏️","biromes":"🖊️","cartucheras":"🎒","utiles escolares":"✏️","impresiones":"🖨️","escritura":"🖊️","papeleria":"📄","papel":"📄","oficina":"🗂️","escolar":"🎒","carpetas":"📁","adhesivos":"🧴","resaltadores":"🖍️","marcadores":"🖊️","colores":"🌈","arte":"🎨","dibujo":"🎨","organizadores":"🗃️","mochilas":"🎒","accesorios":"✂️","calculadoras":"🧮","sellos":"🔖","anillados":"📚"}
def _norm(s): return ''.join(c for c in unicodedata.normalize('NFD',str(s or '').lower()) if unicodedata.category(c)!='Mn')
def cat_icon(cat): return CAT_ICONS.get(_norm(cat),"📦")
def product_name(name):
    """Present product names with one readable units convention in the public catalog."""
    text=str(name or '').replace('Pañuelitos','Rollo de cocina').replace('pañuelitos','rollo de cocina')
    text=unicodedata.normalize('NFC',text)
    text=__import__('re').sub(r'\bx\s*(\d+)', r'x \1', text, flags=__import__('re').I)
    text=__import__('re').sub(r'\b(\d+)\s*[uU]\b', r'\1 u', text)
    text=__import__('re').sub(r'\bpack\s+x\s*(\d+)', r'pack x \1', text, flags=__import__('re').I)
    return text
app.jinja_env.globals['cat_icon']=cat_icon
def price_label(product):
    code=str(product.get('codigo','')) if hasattr(product,'get') else ''
    desc=str(product.get('desc_','')) if hasattr(product,'get') else ''
    if code.upper().startswith('RU-'): return 'Precio por unidad'
    if code in {'AB-PH-016','AB-PH-017','AB-PH-018','AB-PN-008'}: return 'Precio por fardo'
    if code == 'AB-PN-001': return 'Pack $2.100 · fardo x10 $20.000'
    if 'fardo' in desc.lower(): return 'Pack + precio de fardo en detalle'
    if 'pack' in desc.lower(): return 'Precio por pack'
    return 'Precio por rollo'
app.jinja_env.globals['price_label']=price_label
app.jinja_env.filters['product_name']=product_name

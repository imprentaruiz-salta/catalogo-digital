import os, sqlite3, uuid, io, json, zipfile, tempfile, shutil, unicodedata, hashlib, hmac, urllib.request, urllib.error, urllib.parse, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, render_template_string, request, redirect,
                   url_for, session, jsonify, send_from_directory, send_file, flash, make_response)
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__, static_folder=None, template_folder=".")
app.secret_key = os.environ.get("CATALOGO_SESSION_SECRET") or os.urandom(32)
app.config['MAX_CONTENT_LENGTH']=52*1024*1024
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE="Lax")
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be mounted on a persistent disk in production. The application code
# and the data are deliberately kept separate so deploys never replace user data.
DATA_DIR=os.environ.get("CATALOGO_DATA_DIR",os.path.join(BASE_DIR,"data"))
os.makedirs(DATA_DIR,exist_ok=True)
DB_PATH=os.path.join(DATA_DIR,"catalogo.db")
UPLOAD_FOLDER=os.path.join(DATA_DIR,"uploads")
BACKUP_FOLDER=os.path.join(DATA_DIR,"backups")
os.makedirs(UPLOAD_FOLDER,exist_ok=True); os.makedirs(BACKUP_FOLDER,exist_ok=True)

# Optional durable cloud snapshot. Render Free can restart at any time, so the
# local SQLite database and optimized images are mirrored to Supabase Storage.
# The public catalog remains exactly the same; this only changes where data lives.
SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_SERVICE_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
SUPABASE_BUCKET=os.environ.get("SUPABASE_BUCKET","product-images")
SUPABASE_SNAPSHOT="system/catalogo-live.zip"
SUPABASE_VISIT_COUNTER="system/libreria-ruiz-visitas.zip"
SUPABASE_VIVERO_VISIT_COUNTER="system/vivero-los-colibries-visitas.zip"
VISIT_COUNT_LOCK=threading.Lock()
VISIT_COUNT_CACHE=None
VISIT_COUNT_CACHE_READY=False
VIVERO_VISIT_COUNT_LOCK=threading.Lock()
VIVERO_VISIT_COUNT_CACHE=0
VIVERO_VISIT_COUNT_CACHE_READY=False
RENA_VISIT_COUNT_LOCK=threading.Lock()
RENA_VISIT_COUNT_CACHE=0
RENA_VISIT_COUNT_CACHE_READY=False
SUPABASE_RENA_VISIT_COUNTER="system/renas-beauty-visitas.zip"
# Private orders are stored only in this private Supabase bucket, never in the
# public catalog snapshot or public product-images bucket.
RUÍZ_ORDERS_BUCKET="libreria-ruiz-pedidos-privados"
RUÍZ_ORDERS_PREFIX="orders"
RUÍZ_ORDER_ADMIN_PASSWORD=os.environ.get("CATALOGO_ORDERS_ADMIN_PASSWORD","")
RUÍZ_ORDER_MAX_PHOTO=12*1024*1024
RUÍZ_ORDER_MAX_AUDIO=20*1024*1024
RUÍZ_ORDER_RATE_LIMIT={}

# Preserve the user-confirmed pre-reset baseline; old storage snapshots may be stale.
VISIT_COUNT_FLOOR=45

def cloud_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def cloud_endpoint(path):
    return f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path.lstrip('/')}"

def cloud_request(path, method="GET", data=None, content_type="application/octet-stream", timeout=30):
    req=urllib.request.Request(cloud_endpoint(path), data=data, method=method, headers={
        "Authorization":f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey":SUPABASE_SERVICE_KEY,
        "Content-Type":content_type,
        "x-upsert":"true"})
    return urllib.request.urlopen(req, timeout=timeout)

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
    try:
        with cloud_request(SUPABASE_SNAPSHOT,"POST",mem.getvalue(),"application/zip",timeout=180) as response:
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
 "bebidas":"🥤","panales":"👶","comestibles":"🥫","golosinas":"🍬","limpieza":"🧼","verduleria":"🥬","lacteos":"🥛","libreria":"📚","fotos":"📷","fotografia":"📷","carniceria":"🥩","panaderia":"🍞","ferreteria":"🔧","farmacia":"💊","papel higienico":"🧻","papel higienicos":"🧻","pegamento":"🧴","escobas":"🧹","escoba":"🧹","dentifricos":"🪥","dentifrico":"🪥","pasta dental":"🪥","pastas dentales":"🪥","jabones":"🧼","jabon":"🧼","shampoo":"🧴","desodorantes":"🧴","cuadernos":"📒","lapices":"✏️","biromes":"🖊️","cartucheras":"🎒","utiles escolares":"✏️","impresiones":"🖨️","aromaticas":"🌿","arreglos florales":"💐","frutales":"🍎","herbaceas":"🌿","macetas":"🪴","plantines":"🌱","plantines de cesped":"🌱","plantines de pasto":"🌱","arboles":"🌳","tierra":"🌎","encendedores":"🔥","maquillaje":"💄"}
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
    """Return the correct price basis; never call unrelated stationery items rolls."""
    code=str(product.get('codigo','')) if hasattr(product,'get') else ''
    desc=str(product.get('desc_','')) if hasattr(product,'get') else ''
    ruiz_labels={
        'GOTITA-2ML-2026':'Precio por unidad',
        'BIC-CRISTAL-AZ-2026':'Precio por unidad',
        'BIC-TR-NE-2026':'Precio por unidad',
        'BIC-TR-RO-2026':'Precio por unidad',
        'BIC-AZ-2026':'Precio por unidad',
        'BIC-NEG-2026':'Precio por unidad',
        'BIC-RO-2026':'Precio por unidad',
        'PAPERMATE-KM-AZ-2026':'Precio por unidad',
        'PAPERMATE-KM-NEG-2026':'Precio por unidad',
        'CAR-A4-40F-1070CP-2026':'Precio por unidad',
        'CINTA-AUCA-48X50-2026':'Precio por rollo',
        'COR-FILGO-CINTA-2026':'Precio por unidad',
        'ENC-BIC-MAXI-2026':'Precio por unidad',
        'ENC-BIC-MINI-2026':'Precio por unidad',
        'NOTA-EZCO-FLUO-2026':'Precio por unidad',
        'NOTA-KATANIA-MULTI-2026':'Precio por unidad',
        'NOTA-LEEP-PASTEL-400-2026':'Precio por paquete',
        'NOTA-LEEP-50X50-250-2026':'Precio por paquete',
        'MAR-IBI-570104-2026':'Precio por unidad',
        'REG-ESC-2026':'Precio por unidad',
        'RES-BIC-NAR-2026':'Precio por unidad',
        'RES-BIC-ROS-2026':'Precio por unidad',
        'RES-BIC-VER-2026':'Precio por unidad',
        'RES-INTRA-AM-2026':'Precio por unidad',
        'RES-INTRA-VE-2026':'Precio por unidad',
        'RES-PELIKAN-AMA-2026':'Precio por unidad',
        'RESAL-2026':'Precio por unidad',
        'RES-FILGO-RO-2026':'Precio por unidad',
        'NOTA-EZCO-BANDERITA-2026':'Precio por paquete',
        'PAPEL-GLACE-LUMA-LUSTRE-10H-2026':'Precio por paquete',
        'PAPEL-GLACE-LUMA-METALIZADO-10H-2026':'Precio por paquete',
        'PAP-MATELINA-300G-2026':'Precio por paquete',
        'PAPERG260A4-20':'Precio por paquete',
        'PILAS-DURACELL-AA-2-2026':'Precio por blíster',
        'PILAS-DURACELL-AAA-2-2026':'Precio por blíster',
    }
    if code in ruiz_labels: return ruiz_labels[code]
    if code in {'AB-PH-016','AB-PH-017','AB-PH-018','AB-PN-008'}: return 'Precio por fardo'
    if code == 'AB-PN-001': return 'Pack $2.100 · fardo x10 $20.000'
    if 'fardo' in desc.lower(): return 'Pack + precio de fardo en detalle'
    if 'pack' in desc.lower(): return 'Precio por pack'
    if product.get('catalogo_slug') == 'libreria-ruiz': return 'Precio por unidad'
    return 'Precio por rollo'
app.jinja_env.globals['price_label']=price_label
app.jinja_env.filters['product_name']=product_name
def storage_status():
    # Render Free uses an ephemeral filesystem. A mounted DATA_DIR is the only
    # mode in which live catalog data can survive a deploy/restart.
    configured=bool(os.environ.get("CATALOGO_DATA_DIR"))
    writable=os.access(DATA_DIR,os.W_OK)
    durable=cloud_enabled() or (configured and writable)
    return {"mode":"persistent" if durable else "ephemeral", "data_dir":DATA_DIR, "database":os.path.exists(DB_PATH), "uploads":os.path.isdir(UPLOAD_FOLDER), "backups":os.path.isdir(BACKUP_FOLDER), "cloud":"supabase" if cloud_enabled() else "local"}

def get_db():
    db=sqlite3.connect(DB_PATH); db.row_factory=sqlite3.Row; return db

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS productos (id INTEGER PRIMARY KEY AUTOINCREMENT,codigo TEXT UNIQUE NOT NULL,nombre TEXT NOT NULL,desc_ TEXT DEFAULT '',precio REAL NOT NULL DEFAULT 0,categoria TEXT NOT NULL,marca TEXT NOT NULL,foto TEXT DEFAULT '',activo INTEGER DEFAULT 1,stock INTEGER DEFAULT 1)""")
        # Migrations are safe on the existing production database.
        for col,definition in [('catalogo_slug',"TEXT DEFAULT 'libreria-ruiz'"),('stock_actual','INTEGER DEFAULT 0'),('stock_minimo','INTEGER DEFAULT 0'),('costo','REAL DEFAULT 0'),('proveedor',"TEXT DEFAULT ''"),('ultima_reposicion',"TEXT DEFAULT ''"),('nivel_precio',"TEXT DEFAULT 'Estándar'")]:
            try: db.execute(f"ALTER TABLE productos ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError: pass
        try: db.execute("ALTER TABLE productos ADD COLUMN codigo_barras TEXT DEFAULT ''")
        except sqlite3.OperationalError: pass
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_productos_codigo_barras_unique ON productos(codigo_barras) WHERE codigo_barras IS NOT NULL AND codigo_barras != ''")
        db.execute("CREATE TABLE IF NOT EXISTS catalogos (slug TEXT PRIMARY KEY,nombre TEXT NOT NULL,subtitulo TEXT DEFAULT 'Útiles · Fotos · Impresiones',logo TEXT DEFAULT '',whatsapp TEXT DEFAULT '5493872101274',telegram TEXT DEFAULT '',banner TEXT DEFAULT '',activo INTEGER DEFAULT 1,visitas INTEGER NOT NULL DEFAULT 0)")
        try: db.execute("ALTER TABLE catalogos ADD COLUMN visitas INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError: pass
        db.execute("CREATE TABLE IF NOT EXISTS cambios (id INTEGER PRIMARY KEY AUTOINCREMENT, creado TEXT DEFAULT CURRENT_TIMESTAMP, tipo TEXT NOT NULL, catalogo_slug TEXT, detalle TEXT DEFAULT '')")
        db.execute("ALTER TABLE catalogos ADD COLUMN telegram TEXT DEFAULT ''") if 'telegram' not in [r['name'] for r in db.execute('PRAGMA table_info(catalogos)').fetchall()] else None
        db.execute("ALTER TABLE catalogos ADD COLUMN banner TEXT DEFAULT ''") if 'banner' not in [r['name'] for r in db.execute('PRAGMA table_info(catalogos)').fetchall()] else None
        db.execute("INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram,banner) VALUES(?,?,?,?,?,?,?)",('libreria-ruiz','Librería Ruiz','Útiles · Fotos · Impresiones','https://share.zapia.com/lw6ro8nz7tp7k487va08fu','5493872101274','LibreriaRuizSaltaBot',''))
        db.execute("UPDATE productos SET catalogo_slug='libreria-ruiz' WHERE catalogo_slug IS NULL OR catalogo_slug=''")
        db.execute("UPDATE catalogos SET banner=? WHERE slug='limpieza-abigail' AND (banner IS NULL OR banner='')",('https://share.zapia.com/edtuh2ffu9fz19o7ulk70j',))
        db.execute("""CREATE TABLE IF NOT EXISTS sugerencias (id INTEGER PRIMARY KEY AUTOINCREMENT,catalogo_slug TEXT NOT NULL,producto TEXT NOT NULL,nombre TEXT DEFAULT '',cantidad INTEGER DEFAULT 1,cantidad_necesita INTEGER DEFAULT 1,comentario TEXT DEFAULT '',estado TEXT DEFAULT 'pendiente',creado TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for col,definition in [('cantidad_necesita','INTEGER DEFAULT 1'),('comentario',"TEXT DEFAULT ''"),('estado',"TEXT DEFAULT 'pendiente'")]:
            try: db.execute(f"ALTER TABLE sugerencias ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError: pass
        db.execute("""CREATE TABLE IF NOT EXISTS producto_interes (id INTEGER PRIMARY KEY AUTOINCREMENT, catalogo_slug TEXT NOT NULL, product_key TEXT NOT NULL, producto TEXT NOT NULL, detalle TEXT NOT NULL DEFAULT '', unidades INTEGER NOT NULL DEFAULT 0, solicitudes INTEGER NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'pendiente', actualizado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(catalogo_slug,product_key))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_producto_interes_slug ON producto_interes(catalogo_slug,unidades DESC)")
        # Keep the pre-existing product seed; the admin can replace it.
        db.commit()

def _read_saved_visit_count():
    if not cloud_enabled(): return None, True, False
    try:
        with cloud_request(SUPABASE_VISIT_COUNTER, timeout=8) as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            payload=json.loads(archive.read('libreria-ruiz-visitas.json').decode('utf-8'))
        count=max(0,int(payload.get('visitas',0)))
        return count, True, True
    except urllib.error.HTTPError as exc:
        if exc.code == 404: return None, True, False
        if exc.code == 400:
            try: details=exc.read().decode('utf-8','replace').lower()
            except Exception: details=''
            # Supabase Storage may report a missing object as HTTP 400 with a
            # not-found body; treat only that case as an unseeded counter.
            if 'not found' in details and 'bucket not found' not in details:
                return None, True, False
        app.logger.warning('No se pudo leer el contador persistente: HTTP %s',exc.code)
        return None, False, False
    except Exception:
        app.logger.exception('No se pudo leer el contador persistente')
        return None, False, False

def _legacy_snapshot_visit_count():
    """Recover an older visits total from the last full catalog snapshot, once."""
    if not cloud_enabled(): return None, True
    temp_path=None
    try:
        with cloud_request(SUPABASE_SNAPSHOT, timeout=90) as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            db_name='base/catalogo.sqlite3'
            if db_name not in archive.namelist(): return None, True
            db_bytes=archive.read(db_name)
        temp_path=os.path.join(DATA_DIR,'.visitas-recuperacion-'+uuid.uuid4().hex+'.sqlite3')
        with open(temp_path,'wb') as target: target.write(db_bytes)
        if not _db_has_products(temp_path): return None, False
        legacy_db=sqlite3.connect(temp_path)
        try:
            row=legacy_db.execute("SELECT visitas FROM catalogos WHERE slug='libreria-ruiz'").fetchone()
            return (max(0,int(row[0] or 0)) if row else None), True
        finally:
            legacy_db.close()
    except urllib.error.HTTPError as exc:
        if exc.code == 404: return None, True
        app.logger.warning('No se pudo revisar el respaldo anterior de visitas: HTTP %s',exc.code)
        return None, False
    except Exception:
        app.logger.exception('No se pudo revisar el respaldo anterior de visitas')
        return None, False
    finally:
        if temp_path and os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass

def _write_saved_visit_count(count):
    if not cloud_enabled(): return False
    payload=json.dumps({'slug':'libreria-ruiz','visitas':int(count),
                        'updated_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'},
                       ensure_ascii=False,separators=(',',':')).encode('utf-8')
    archive_buffer=io.BytesIO()
    with zipfile.ZipFile(archive_buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('libreria-ruiz-visitas.json',payload)
    try:
        with cloud_request(SUPABASE_VISIT_COUNTER,'POST',archive_buffer.getvalue(),'application/zip',timeout=12) as response:
            response.read()
        return True
    except Exception:
        app.logger.exception('No se pudo guardar el contador de visitas')
        return False

def restore_visit_counter():
    """Merge local, dedicated cloud counter, and legacy snapshot using max only."""
    global VISIT_COUNT_CACHE,VISIT_COUNT_CACHE_READY
    with VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='libreria-ruiz'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        if not cloud_enabled():
            VISIT_COUNT_CACHE=local; VISIT_COUNT_CACHE_READY=True
            return local
        remote,read_ok,counter_exists=_read_saved_visit_count()
        if not read_ok:
            safe=max(local,VISIT_COUNT_CACHE or 0,VISIT_COUNT_FLOOR)
            with get_db() as db:
                db.execute("UPDATE catalogos SET visitas=? WHERE slug='libreria-ruiz'",(safe,))
                db.commit()
            VISIT_COUNT_CACHE=safe; VISIT_COUNT_CACHE_READY=False
            return safe
        if not counter_exists:
            legacy,legacy_ok=_legacy_snapshot_visit_count()
            if not legacy_ok:
                safe=max(local,VISIT_COUNT_CACHE or 0,VISIT_COUNT_FLOOR)
                with get_db() as db:
                    db.execute("UPDATE catalogos SET visitas=? WHERE slug='libreria-ruiz'",(safe,))
                    db.commit()
                VISIT_COUNT_CACHE=safe; VISIT_COUNT_CACHE_READY=False
                return safe
            if legacy is not None: remote=legacy
        count=max(local,remote or 0,VISIT_COUNT_FLOOR)
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='libreria-ruiz'",(count,))
            db.commit()
        VISIT_COUNT_CACHE=count; VISIT_COUNT_CACHE_READY=True
        if not counter_exists or remote is None or count>remote:
            _write_saved_visit_count(count)
        return count

def record_libreria_visit():
    """Count a real page view only after the durable monotonic value is saved."""
    global VISIT_COUNT_CACHE,VISIT_COUNT_CACHE_READY
    with VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='libreria-ruiz'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        current=max(local,VISIT_COUNT_CACHE or 0,VISIT_COUNT_FLOOR)
        if not cloud_enabled():
            current+=1
        else:
            remote,read_ok,counter_exists=_read_saved_visit_count()
            if not read_ok:
                app.logger.warning('Visita no sumada: no se pudo confirmar el contador persistente')
                return current
            if not counter_exists and not VISIT_COUNT_CACHE_READY:
                legacy,legacy_ok=_legacy_snapshot_visit_count()
                if not legacy_ok:
                    app.logger.warning('Visita no sumada: no se pudo revisar el respaldo anterior')
                    return current
                if legacy is not None: remote=legacy
            base=max(current,remote or 0,VISIT_COUNT_FLOOR)
            next_count=base+1
            if not _write_saved_visit_count(next_count):
                app.logger.warning('Visita no sumada: no se pudo guardar el nuevo total')
                with get_db() as db:
                    db.execute("UPDATE catalogos SET visitas=? WHERE slug='libreria-ruiz'",(base,))
                    db.commit()
                VISIT_COUNT_CACHE=base; VISIT_COUNT_CACHE_READY=True
                return base
            current=next_count
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='libreria-ruiz'",(current,))
            db.commit()
        VISIT_COUNT_CACHE=current; VISIT_COUNT_CACHE_READY=True
        return current

def _read_vivero_visit_count():
    """Read the Vivero counter from its own durable object, never the library object."""
    if not cloud_enabled(): return None, True, False
    try:
        with cloud_request(SUPABASE_VIVERO_VISIT_COUNTER, timeout=8) as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            payload=json.loads(archive.read('vivero-los-colibries-visitas.json').decode('utf-8'))
        return max(0,int(payload.get('visitas',0))), True, True
    except urllib.error.HTTPError as exc:
        if exc.code == 404: return None, True, False
        if exc.code == 400:
            try: details=exc.read().decode('utf-8','replace').lower()
            except Exception: details=''
            if 'not found' in details and 'bucket not found' not in details:
                return None, True, False
        app.logger.warning('No se pudo leer el contador persistente del vivero: HTTP %s',exc.code)
        return None, False, False
    except Exception:
        app.logger.exception('No se pudo leer el contador persistente del vivero')
        return None, False, False

def _write_vivero_visit_count(count):
    if not cloud_enabled(): return False
    payload=json.dumps({'slug':'vivero-los-colibries','visitas':int(count),
                        'updated_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'},
                       ensure_ascii=False,separators=(',',':')).encode('utf-8')
    archive_buffer=io.BytesIO()
    with zipfile.ZipFile(archive_buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('vivero-los-colibries-visitas.json',payload)
    try:
        with cloud_request(SUPABASE_VIVERO_VISIT_COUNTER,'POST',archive_buffer.getvalue(),'application/zip',timeout=12) as response:
            response.read()
        return True
    except Exception:
        app.logger.exception('No se pudo guardar el contador persistente del vivero')
        return False

def restore_vivero_visit_counter():
    """Restore only this catalog's total; merge with max so restarts cannot roll it back."""
    global VIVERO_VISIT_COUNT_CACHE,VIVERO_VISIT_COUNT_CACHE_READY
    with VIVERO_VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='vivero-los-colibries'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        if not cloud_enabled():
            VIVERO_VISIT_COUNT_CACHE=local
            VIVERO_VISIT_COUNT_CACHE_READY=bool(os.environ.get('CATALOGO_DATA_DIR'))
            return local
        remote,read_ok,counter_exists=_read_vivero_visit_count()
        if not read_ok:
            VIVERO_VISIT_COUNT_CACHE=max(local,VIVERO_VISIT_COUNT_CACHE)
            VIVERO_VISIT_COUNT_CACHE_READY=False
            return VIVERO_VISIT_COUNT_CACHE
        count=max(local,remote or 0)
        if not counter_exists or remote is None or count>remote:
            if not _write_vivero_visit_count(count):
                VIVERO_VISIT_COUNT_CACHE=count
                VIVERO_VISIT_COUNT_CACHE_READY=False
                return count
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='vivero-los-colibries'",(count,))
            db.commit()
        VIVERO_VISIT_COUNT_CACHE=count
        VIVERO_VISIT_COUNT_CACHE_READY=True
        return count

def record_vivero_visit():
    """Add one estimate only after durable storage confirms the new total."""
    global VIVERO_VISIT_COUNT_CACHE,VIVERO_VISIT_COUNT_CACHE_READY
    with VIVERO_VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='vivero-los-colibries'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        current=max(local,VIVERO_VISIT_COUNT_CACHE)
        if cloud_enabled():
            remote,read_ok,counter_exists=_read_vivero_visit_count()
            if not read_ok:
                app.logger.warning('Visita del vivero no sumada: no se pudo confirmar el contador persistente')
                return current,False
            if not counter_exists and not VIVERO_VISIT_COUNT_CACHE_READY:
                # Seed a fresh dedicated object from the known local value; no historical backfill.
                if not _write_vivero_visit_count(current):
                    return current,False
            current=max(current,remote or 0)
            next_count=current+1
            if not _write_vivero_visit_count(next_count):
                app.logger.warning('Visita del vivero no sumada: no se pudo guardar el nuevo total')
                return current,False
        elif os.environ.get('CATALOGO_DATA_DIR'):
            next_count=current+1
        else:
            app.logger.warning('Visita del vivero no sumada: no hay almacenamiento persistente configurado')
            return current,False
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='vivero-los-colibries'",(next_count,))
            db.commit()
        VIVERO_VISIT_COUNT_CACHE=next_count
        VIVERO_VISIT_COUNT_CACHE_READY=True
        return next_count,True

def _read_rena_visit_count():
    """Read Rena’s Beauty's cumulative visits from its own durable object."""
    if not cloud_enabled(): return None,True,False
    try:
        with cloud_request(SUPABASE_RENA_VISIT_COUNTER,timeout=8) as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            payload=json.loads(archive.read('renas-beauty-visitas.json').decode('utf-8'))
        return max(0,int(payload.get('visitas',0))),True,True
    except urllib.error.HTTPError as exc:
        if exc.code==404: return None,True,False
        if exc.code==400:
            try: details=exc.read().decode('utf-8','replace').lower()
            except Exception: details=''
            if 'not found' in details and 'bucket not found' not in details:
                return None,True,False
        app.logger.warning('No se pudo leer el contador de Rena’s Beauty: HTTP %s',exc.code)
        return None,False,False
    except Exception:
        app.logger.exception('No se pudo leer el contador de Rena’s Beauty')
        return None,False,False

def _write_rena_visit_count(count):
    if not cloud_enabled(): return False
    payload=json.dumps({'slug':'renas-beauty','visitas':int(count),
                        'updated_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'},
                       ensure_ascii=False,separators=(',',':')).encode('utf-8')
    archive_buffer=io.BytesIO()
    with zipfile.ZipFile(archive_buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('renas-beauty-visitas.json',payload)
    try:
        with cloud_request(SUPABASE_RENA_VISIT_COUNTER,'POST',archive_buffer.getvalue(),'application/zip',timeout=12) as response:
            response.read()
        return True
    except Exception:
        app.logger.exception('No se pudo guardar el contador de Rena’s Beauty')
        return False

def restore_rena_visit_counter():
    """Restore only Rena’s Beauty's total; merge with max to prevent rollbacks."""
    global RENA_VISIT_COUNT_CACHE,RENA_VISIT_COUNT_CACHE_READY
    with RENA_VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='renas-beauty'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        if not cloud_enabled():
            RENA_VISIT_COUNT_CACHE=local
            RENA_VISIT_COUNT_CACHE_READY=bool(os.environ.get('CATALOGO_DATA_DIR'))
            return local
        remote,read_ok,counter_exists=_read_rena_visit_count()
        if not read_ok:
            RENA_VISIT_COUNT_CACHE=max(local,RENA_VISIT_COUNT_CACHE)
            RENA_VISIT_COUNT_CACHE_READY=False
            return RENA_VISIT_COUNT_CACHE
        count=max(local,remote or 0)
        if counter_exists and remote is not None and count>remote:
            if not _write_rena_visit_count(count):
                RENA_VISIT_COUNT_CACHE=count
                RENA_VISIT_COUNT_CACHE_READY=False
                return count
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='renas-beauty'",(count,))
            db.commit()
        RENA_VISIT_COUNT_CACHE=count
        RENA_VISIT_COUNT_CACHE_READY=counter_exists
        return count

def record_rena_visit():
    """Count a human page view only after its durable total has been saved."""
    global RENA_VISIT_COUNT_CACHE,RENA_VISIT_COUNT_CACHE_READY
    with RENA_VISIT_COUNT_LOCK:
        with get_db() as db:
            row=db.execute("SELECT visitas FROM catalogos WHERE slug='renas-beauty'").fetchone()
        local=max(0,int(row['visitas'] or 0)) if row else 0
        current=max(local,RENA_VISIT_COUNT_CACHE)
        if cloud_enabled():
            remote,read_ok,counter_exists=_read_rena_visit_count()
            if not read_ok:
                app.logger.warning('Visita de Rena’s Beauty no sumada: no se confirmó el contador persistente')
                return current,False
            if not counter_exists and not RENA_VISIT_COUNT_CACHE_READY:
                if not _write_rena_visit_count(current):
                    app.logger.warning('Visita de Rena’s Beauty no sumada: no se pudo inicializar el contador')
                    return current,False
            current=max(current,remote or 0)
            next_count=current+1
            if not _write_rena_visit_count(next_count):
                app.logger.warning('Visita de Rena’s Beauty no sumada: no se pudo guardar el total')
                return current,False
        elif os.environ.get('CATALOGO_DATA_DIR'):
            next_count=current+1
        else:
            app.logger.warning('Visita de Rena’s Beauty no sumada: no hay almacenamiento persistente configurado')
            return current,False
        with get_db() as db:
            db.execute("UPDATE catalogos SET visitas=? WHERE slug='renas-beauty'",(next_count,))
            db.commit()
        RENA_VISIT_COUNT_CACHE=next_count
        RENA_VISIT_COUNT_CACHE_READY=True
        return next_count,True

def backup_db(reason="manual"):
    """Create a timestamped SQLite backup without interrupting the live DB."""
    if not os.path.exists(DB_PATH): return None
    stamp=datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    target=os.path.join(BACKUP_FOLDER,f"catalogo-{stamp}-{secure_filename(reason) or 'backup'}.sqlite3")
    src=sqlite3.connect(DB_PATH); dst=sqlite3.connect(target)
    try: src.backup(dst)
    finally: dst.close(); src.close()
    files=sorted([os.path.join(BACKUP_FOLDER,x) for x in os.listdir(BACKUP_FOLDER) if x.endswith('.sqlite3')])
    for old in files[:-20]:
        try: os.remove(old)
        except OSError: pass
    return target

def log_change(tipo, slug='', detalle=''):
    try:
        with get_db() as db: db.execute('INSERT INTO cambios(tipo,catalogo_slug,detalle) VALUES(?,?,?)',(tipo,slug,detalle)); db.commit()
    except Exception: pass

def allowed_file(filename): return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXT

def save_optimized_image(file):
    """Normalize product photos so catalog growth does not fill the disk."""
    raw=file.read(MAX_IMAGE_BYTES+1)
    if len(raw)>MAX_IMAGE_BYTES: raise ValueError('La imagen supera el límite de 12 MB')
    try:
        with Image.open(io.BytesIO(raw)) as original:
            image=ImageOps.exif_transpose(original)
            image.thumbnail((MAX_IMAGE_SIDE,MAX_IMAGE_SIDE),Image.Resampling.LANCZOS)
            has_alpha='A' in image.getbands() or ('transparency' in image.info)
            image=image.convert('RGBA' if has_alpha else 'RGB')
            output=io.BytesIO()
            image.save(output,format='WEBP',quality=82,method=6)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError('El archivo no contiene una imagen válida') from exc
    filename=f'{uuid.uuid4().hex}.webp'
    with open(os.path.join(UPLOAD_FOLDER,filename),'wb') as destination:
        destination.write(output.getvalue())
    return filename

@app.errorhandler(413)
def too_large(_error):
    return 'El archivo es demasiado grande. Los videos de portada admiten hasta 50 MB; las imágenes, hasta 12 MB.',400
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args,**kwargs):
        if not session.get('admin'): return redirect(url_for('admin_login'))
        return f(*args,**kwargs)
    return decorated
def get_catalogo_config(slug):
    with get_db() as db:
        row=db.execute('SELECT * FROM catalogos WHERE slug=? AND activo=1',(slug,)).fetchone()
    return dict(row) if row else None
def get_catalogo(slug='libreria-ruiz'):
    with get_db() as db: rows=db.execute('SELECT * FROM productos WHERE activo=1 AND catalogo_slug=? ORDER BY nombre',(slug,)).fetchall()
    cats={}
    for row in rows: cats.setdefault(row['categoria'],{}).setdefault(row['marca'],[]).append(dict(row))
    prioridad=['Libreria','Librería','Fotos','Fotografía','Papeleria','Papelería','Impresiones']; orden={x:i for i,x in enumerate(prioridad)}     # Abigail: order categories as a simple shopping journey, not alphabetically.     if slug == 'limpieza-abigail':         orden.update({cat:i for i,cat in enumerate([             'jabones en pan','papel higienicos','rollo de cocina','shampoo',             'acondicionador','desodorantes','pastas dentales','cepillos de dientes',             'protectores diarios','máquinas de afeitar','talcos','cremas'         ])})     return dict(sorted(cats.items(),key=lambda x:(orden.get(x[0],100),x[0].lower())))
    return dict(sorted(cats.items(),key=lambda x:(orden.get(x[0],100),x[0].lower())))
def get_showcase(slug='libreria-ruiz'):
    # Curated visual shelves are derived from the catalog until explicit merchandising fields are added.
    with get_db() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM productos WHERE activo=1 AND catalogo_slug=? ORDER BY id DESC',(slug,)).fetchall()]
        requests=db.execute('SELECT lower(producto) AS producto,SUM(cantidad) AS votos FROM sugerencias WHERE catalogo_slug=? GROUP BY lower(producto) ORDER BY votos DESC',(slug,)).fetchall()
    def unique(items, limit=8):
        out=[]; seen=set()
        for item in items:
            if item['id'] not in seen:
                out.append(item); seen.add(item['id'])
            if len(out)>=limit: break
        return out
    featured=unique([p for p in rows if p.get('foto')] + rows)
    newest=unique(rows)
    economy=[p for p in rows if (p.get('nivel_precio') or '')=='Económico']
    offers=unique(sorted(economy,key=lambda p:(float(p.get('precio') or 0),p['nombre'])) + sorted(rows,key=lambda p:(float(p.get('precio') or 0),p['nombre'])))
    recommendations=[]; seen_categories=set()
    for p in reversed(rows):
        if p['categoria'] not in seen_categories:
            recommendations.append(p); seen_categories.add(p['categoria'])
    by_name={str(p['nombre']).strip().lower():p for p in rows}
    requested=[]
    for r in requests:
        product=by_name.get(r['producto'].strip().lower())
        if product: requested.append(product)
    return {'destacados':featured,'novedades':newest,'ofertas':offers,'recomendaciones':unique(recommendations),'pedidos':unique(requested),'pilas':unique([p for p in rows if str(p.get('categoria') or '').strip().lower()=='pilas']),'total':len(rows)}
def current_slug(): return session.get('catalogo_slug','libreria-ruiz')
def current_config(): return get_catalogo_config(current_slug()) or get_catalogo_config('libreria-ruiz')


# Dedicated read-only product feed for Alma; intentionally bypasses public page routes.
ALMA_CATALOG_READONLY_KEY_ENV="ALMA_CATALOG_READONLY_KEY"

# Alma chat público: búsqueda estrictamente de solo lectura en el catálogo vigente.
# No persiste conversaciones ni expone costo, proveedor, clientes o datos internos.
import re as _alma_re
import unicodedata as _alma_unicodedata
_ALMA_CHAT_ALIASES={'lapicera':'birome','lapiceras':'birome','boligrafo':'birome','boligrafos':'birome','biromes':'birome','lapices':'lapiz','marcadores':'marcador','resaltadores':'resaltador','pilas':'pila','cuadernos':'cuaderno','cuadernas':'cuaderno','negro':'negra','negros':'negra','negras':'negra','rojo':'roja','rojos':'roja','rojas':'roja','azules':'azul','amarilla':'amarillo','amarillas':'amarillo','verdes':'verde','rosas':'rosa'}
_ALMA_CHAT_FILLERS=set('alma hola buenas buen dia tenes tiene tienen hay quiero necesito buscas busco buscar buscame quisiera si alguna algun alguno algo de esas esos esa ese del la el los las un una unos unas por para con en y o me mi a lo que tal cosa producto productos cuanto cuantos cuantas cuesta cuestan precio precios sale salen venden vende vender comprar ustedes dame pregunta favor saber'.split())
_ALMA_CHAT_COLORS=set('azul negra roja verde rosa amarillo naranja violeta celeste gris blanca blanco pastel'.split())
_ALMA_CHAT_BRANDS={'bic','pelikan','intra','filgo','pizzini','ezco','duracell','maped','katania','ibi','paper','mate'}
def _alma_chat_tokens(value):
    text=_alma_unicodedata.normalize('NFKD',str(value or '').lower())
    text=''.join(ch for ch in text if not _alma_unicodedata.combining(ch))
    return [_ALMA_CHAT_ALIASES.get(word,word) for word in _alma_re.findall(r'[a-z0-9]+',text) if word not in _ALMA_CHAT_FILLERS]
def _alma_chat_search(question, rows):
    exact_code=str(question or '').strip().upper()
    exact=[row for row in rows if str(row.get('codigo') or '').upper()==exact_code]
    if exact:
        row=exact[0]
        try: exact_price=float(row.get('precio'))
        except (TypeError,ValueError): exact_price=0
        try: exact_stock=max(0,int(row.get('stock_actual') or 0))
        except (TypeError,ValueError): exact_stock=0
        return [{'codigo':str(row.get('codigo') or ''),'nombre':str(row.get('nombre') or 'Producto'),'marca':str(row.get('marca') or ''),'categoria':str(row.get('categoria') or ''),'descripcion':str(row.get('desc_') or ''),'precio_ars':exact_price if exact_price>0 else None,'stock':exact_stock,'foto':str(row.get('foto') or '')}]
    terms=set(_alma_chat_tokens(question))
    if not terms: return []
    colors=terms & _ALMA_CHAT_COLORS
    brands=terms & _ALMA_CHAT_BRANDS
    hits=[]
    for row in rows:
        name=set(_alma_chat_tokens(row.get('nombre')))
        brand=set(_alma_chat_tokens(row.get('marca')))
        category=set(_alma_chat_tokens(row.get('categoria')))
        desc=set(_alma_chat_tokens(row.get('desc_')))
        code=set(_alma_chat_tokens(row.get('codigo')))
        if brands and not brands <= brand: continue
        if 'paper' in brands or 'mate' in brands:
            if not {'paper','mate'} <= brand: continue
        if colors and not colors & name: continue
        if not terms <= (name|brand|category|desc|code): continue
        score=4*len(terms&name)+3*len(terms&brand)+len(terms&category)+len(terms&desc)+len(terms&code)
        try: price=float(row.get('precio'))
        except (TypeError,ValueError): price=None
        try: stock=max(0,int(row.get('stock_actual') or 0))
        except (TypeError,ValueError): stock=0
        hits.append((score,{'codigo':str(row.get('codigo') or ''),'nombre':str(row.get('nombre') or 'Producto'),'marca':str(row.get('marca') or ''),'categoria':str(row.get('categoria') or ''),'descripcion':str(row.get('desc_') or ''),'precio_ars':price if price and price>0 else None,'stock':stock,'foto':str(row.get('foto') or '')}))
    hits.sort(key=lambda hit:(-hit[0],_alma_unicodedata.normalize('NFKD',hit[1]['nombre'].lower())))
    return [product for _,product in hits[:5]]
_ALMA_CHAT_RATE={}
_ALMA_CHAT_RATE_LOCK=threading.Lock()

def _alma_visible_catalog_codes():
    # Read the exact allowlist used to render public product cards. Fail closed.
    import ast
    try:
        with open(os.path.join(BASE_DIR,'index.html'),encoding='utf-8') as source:
            template=source.read()
        match=_alma_re.search(r"catalogo\.slug != 'libreria-ruiz' or p\.codigo in (\[[^\]]+\])",template)
        if not match: raise ValueError('public catalog product list is missing')
        codes=ast.literal_eval(match.group(1))
        if not isinstance(codes,list) or not codes or not all(isinstance(x,str) for x in codes):
            raise ValueError('invalid public catalog product list')
        return set(codes)
    except Exception as exc:
        app.logger.error('Alma public catalog filter unavailable: %s',exc)
        raise RuntimeError('public catalog filter unavailable') from exc
def _alma_catalog_explanation(question, rows):
    """Answer catalogue-wide questions from current public rows only."""
    q=_alma_unicodedata.normalize('NFKD',str(question or '').lower())
    q=''.join(ch for ch in q if not _alma_unicodedata.combining(ch))
    words=set(_alma_chat_tokens(q))
    wants_brands=any(x in q for x in ('marca','marcas'))
    wants_compare=any(x in q for x in ('diferencia','diferencias','comparar','compare','mejor para'))
    category=''
    if any(x in q for x in ('resaltador','fluorescente')): category='resaltadores'
    elif any(x in q for x in ('birome','lapicera','boligrafo')): category='biromes'
    elif any(x in q for x in ('sticker','adhesiv','senalador','nota adhesiva')): category='libreria'
    elif any(x in q for x in ('pila','regla','papel','pegamento','corrector','carpeta','marcador','encendedor')): category=''
    if wants_brands and category:
        values=sorted({str(r.get('marca') or '').title() for r in rows if str(r.get('categoria') or '').lower()==category})
        if values:
            return f"En {category} encontré estas marcas en el catálogo: {', '.join(values)}. Si querés, te muestro las variantes, colores y precios de una marca.", []
    if wants_compare:
        # Prefer records from any brands named in the question, then show only catalog facts.
        asked_brands={b for b in _ALMA_CHAT_BRANDS if b in words}
        brand_parts={'paper mate':{'paper','mate'}}
        for brand in ('bic','filgo','pelikan','intra','pizzini','ezco','katania','leep','maped','duracell','ibi','global','auca','luma','art-jet','glister','unipox'):
            if brand in q: brand_parts.setdefault(brand,{brand})
        normalized_brands=set()
        for brand,parts in brand_parts.items():
            if brand in q: normalized_brands |= parts
        normalized_brands |= asked_brands
        if len(normalized_brands)>1:
            candidates=[]
            for brand in sorted(normalized_brands):
                group=[r for r in rows if brand in set(_alma_chat_tokens(r.get('marca'))) and (not category or str(r.get('categoria') or '').lower()==category)]
                candidates.extend(group[:3])
            candidates=candidates[:6]
        else:
            candidates=[r for r in rows if normalized_brands and normalized_brands <= set(_alma_chat_tokens(r.get('marca'))) and (not category or str(r.get('categoria') or '').lower()==category)]
        if not candidates:
            candidates=_alma_chat_search(q,rows)
        if not candidates:
            candidates=[]
            for r in rows:
                cat=str(r.get('categoria') or '').lower()
                if (category and cat==category) or (not category and any(x in q for x in (str(r.get('marca') or '').lower(),str(r.get('nombre') or '').lower()))):
                    candidates.append(r)
            candidates=candidates[:6]
        if candidates:
            names='; '.join(str(r.get('nombre') or '') for r in candidates[:5])
            compared=', '.join(sorted({str(r.get('marca') or '').title() for r in candidates if normalized_brands and set(_alma_chat_tokens(r.get('marca'))) & normalized_brands}))
            prefix=f"En el catálogo veo {compared}. " if compared else ''
            return prefix+f"Puedo comparar estas fichas: {names}. Te muestro solo las diferencias que figuran publicadas, sin inventar características.", candidates[:5]
    if any(x in q for x in ('todo el catalogo','todo el catálogo','que hay','qué hay','todos los productos','todas las cosas','productos hay','cosas hay','que tiene','que hay en','del catalogo','de catalogo')):
        cats=sorted({str(r.get('categoria') or '').title() for r in rows})
        return f"En la base actual del catálogo encuentro estas categorías: {', '.join(cats)}. Preguntame por una marca, color, tipo de producto, precio o disponibilidad.", []
    if category and any(x in q for x in ('que hay','que tienen','mostrar','mostrame','productos','todos','sticker','adhesiv','senalador')):
        chosen=[r for r in rows if str(r.get('categoria') or '').lower()==category]
        if category=='libreria':
            chosen=[r for r in rows if str(r.get('categoria') or '').lower()=='libreria' and any(x in _alma_unicodedata.normalize('NFKD',str(r.get('nombre') or '').lower()) for x in ('adhesiv','senalador','nota'))]
        chosen=chosen[:8]
        if chosen:
            return f"En el catálogo encontré {len(chosen)} opciones relacionadas: {', '.join(str(r.get('nombre') or '') for r in chosen)}. Te dejo las fichas para ver detalle, precio y stock publicados.", chosen
    return None, []

@app.route('/api/alma/chat',methods=['POST'])
def alma_chat_publico():
    if not request.is_json: return jsonify({'error':'Usá una consulta de texto.'}),415
    body=request.get_json(silent=True)
    question=(body or {}).get('pregunta','') if isinstance(body,dict) else ''
    if not isinstance(question,str) or not question.strip() or len(question.strip())>200:
        return jsonify({'error':'La pregunta debe tener entre 1 y 200 caracteres.'}),400
    import time as _alma_time
    client=request.remote_addr or 'unknown'; now=_alma_time.monotonic()
    with _ALMA_CHAT_RATE_LOCK:
        recent=[stamp for stamp in _ALMA_CHAT_RATE.get(client,[]) if now-stamp<60]
        if len(recent)>=120: return jsonify({'error':'Alma recibió varias consultas seguidas. Esperá un minuto y probá de nuevo.'}),429
        recent.append(now); _ALMA_CHAT_RATE[client]=recent
        if len(_ALMA_CHAT_RATE)>2000: _ALMA_CHAT_RATE.clear()
    try:
        with get_db() as db:
            rows=[dict(row) for row in db.execute("SELECT codigo,nombre,desc_,precio,categoria,marca,foto,stock_actual FROM productos WHERE activo=1 AND catalogo_slug=?",('libreria-ruiz',)).fetchall()]
        visible_codes=_alma_visible_catalog_codes()
        rows=[row for row in rows if row.get('codigo') in visible_codes]
        explanation, explanation_rows = _alma_catalog_explanation(question, rows)
        if explanation:
            products=[]
            for row in explanation_rows:
                try: price=float(row.get('precio'))
                except (TypeError,ValueError): price=None
                try: stock=max(0,int(row.get('stock_actual') or 0))
                except (TypeError,ValueError): stock=0
                products.append({'codigo':str(row.get('codigo') or ''),'nombre':str(row.get('nombre') or 'Producto'),'marca':str(row.get('marca') or ''),'categoria':str(row.get('categoria') or ''),'descripcion':str(row.get('desc_') or ''),'precio_ars':price if price and price>0 else None,'stock':stock,'foto':str(row.get('foto') or '')})
            response=jsonify({'respuesta':explanation,'productos':products})
            response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
            response.headers['X-Content-Type-Options']='nosniff'
            return response
        selected=body.get('producto_seleccionado','')
        if not isinstance(selected,str) or len(selected)>100: selected=''
        context=body.get('consulta_anterior','')
        if not isinstance(context,str) or len(context)>200: context=''
        normalized=_alma_unicodedata.normalize('NFKD',question.lower())
        normalized=''.join(c for c in normalized if not _alma_unicodedata.combining(c))
        spelled={'uno':'1','una':'1','dos':'2','tres':'3','cuatro':'4','cinco':'5','seis':'6','siete':'7','ocho':'8','nueve':'9','diez':'10','doce':'12'}
        for word,number in spelled.items(): normalized=_alma_re.sub(r'\b'+word+r'\b',number,normalized)
        qty_match=_alma_re.fullmatch(r'\s*(?:(?:quiero|necesito|dame|llevo|son|cuanto salen|cuanto cuestan)\s+)?(\d{1,4})\s*(?:unidades|unidad|u)?\s*[.!?]*\s*',normalized)
        if qty_match and selected:
            quantity=int(qty_match.group(1))
            found=[row for row in rows if row.get('codigo')==selected]
            if not found:
                return jsonify({'respuesta':'Ese producto ya no está disponible en el catálogo. Busquemos otra opción.','productos':[]})
            row=found[0]
            try: unit_price=float(row.get('precio'))
            except (TypeError,ValueError): unit_price=0
            try: unit_stock=max(0,int(row.get('stock_actual') or 0))
            except (TypeError,ValueError): unit_stock=0
            p={'codigo':str(row.get('codigo') or ''),'nombre':str(row.get('nombre') or 'Producto'),'marca':str(row.get('marca') or ''),'categoria':str(row.get('categoria') or ''),'descripcion':str(row.get('desc_') or ''),'precio_ars':unit_price if unit_price>0 else None,'stock':unit_stock,'foto':str(row.get('foto') or '')}
            products=[p]
            if not 1 <= quantity <= 999:
                text='Ingresá una cantidad entre 1 y 999 unidades.'
            elif p.get('precio_ars'):
                total=p['precio_ars']*quantity
                money=lambda value: ('{:,.2f}'.format(value).replace(',','X').replace('.',',').replace('X','.')).rstrip('0').rstrip(',')
                text=f"{quantity} × {p['nombre']} a ARS {money(p['precio_ars'])} = ARS {money(total)}. "+(f"El catálogo muestra {p['stock']} disponibles." if p['stock'] else 'No hay stock publicado; consultanos.')+' Precio y disponibilidad sujetos a confirmación.'
                if quantity>p['stock'] and p['stock']>0: text+=' La cantidad solicitada supera el stock publicado.'
            else:
                text='El precio no está confirmado. Consultanos antes de calcular el total.'
        elif qty_match:
            text='Primero elegí un producto de los resultados y después indicame la cantidad.';products=[]
        else:
            clean=_alma_re.sub(r'\b(?:quiero|necesito|dame|llevo)?\s*\d{1,4}\s+(?=(?:unidades?\s+)?(?:biromes?|lapiceras?|boligrafos?|resaltadores?|pilas?|correctores?|bic)\b)', '', normalized, flags=_alma_re.I)
            words=set(_alma_chat_tokens(clean))
            colors=words & _ALMA_CHAT_COLORS
            if context and colors and len(words)==1:
                prev=[w for w in _alma_chat_tokens(context) if w not in _ALMA_CHAT_COLORS]
                clean=' '.join(prev+list(colors))
            products=_alma_chat_search(clean.strip(),rows)
            text=('Encontré estas opciones en el catálogo vigente. Elegí un producto y decime cuántas unidades querés.' if products else 'No encuentro una coincidencia clara en el catálogo. Probá con otra marca, color o nombre; si no aparece, podés consultarnos por WhatsApp.')
        response=jsonify({'respuesta':text,'productos':products})
        response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    except Exception:
        app.logger.exception('Alma: falló la consulta de solo lectura al catálogo')
        return jsonify({'error':'El catálogo no está disponible en este momento.'}),503

@app.route('/api/alma/audio/transcribe',methods=['POST'])
def alma_audio_transcribe():
    """Transcribe a short spoken question in memory only; never retain uploaded audio."""
    key=os.environ.get('GEMINI_AUDIO_API_KEY','').strip()
    if not key:
        return jsonify({'error':'El micrófono de Alma todavía no está configurado. Escribí tu consulta por ahora.'}),503
    audio=request.files.get('audio')
    if not audio or not audio.filename:
        return jsonify({'error':'No recibí el audio. Tocá el micrófono y probá otra vez.'}),400
    allowed={'audio/webm','audio/mp4','audio/x-m4a','audio/m4a','audio/ogg','audio/wav','audio/x-wav','audio/mpeg','audio/mp3','audio/aac'}
    mime=(audio.mimetype or '').lower().split(';',1)[0].strip()
    mime_alias={'audio/x-m4a':'audio/mp4','audio/m4a':'audio/mp4','audio/x-wav':'audio/wav','audio/mp3':'audio/mpeg'}
    mime=mime_alias.get(mime,mime)
    if mime not in {'audio/webm','audio/mp4','audio/ogg','audio/wav','audio/mpeg','audio/aac'}:
        return jsonify({'error':'Ese formato de audio no es compatible. Probá grabar de nuevo desde Firefox.'}),415
    raw=audio.stream.read(6*1024*1024+1)
    if not raw: return jsonify({'error':'El audio llegó vacío. Probá grabarlo otra vez.'}),400
    if len(raw)>6*1024*1024: return jsonify({'error':'El audio supera 6 MB. Grabá una consulta más corta.'}),413
    import base64 as _alma_b64, time as _alma_time, socket as _alma_socket
    caller=request.remote_addr or 'unknown'; now=_alma_time.monotonic()
    with _ALMA_CHAT_RATE_LOCK:
        recent=[stamp for stamp in _ALMA_CHAT_RATE.get('audio:'+caller,[]) if now-stamp<60]
        if len(recent)>=12: return jsonify({'error':'Ya recibí varios audios. Esperá un minuto y probá otra vez.'}),429
        recent.append(now); _ALMA_CHAT_RATE['audio:'+caller]=recent
    payload={'contents':[{'role':'user','parts':[{'text':'Transcribí exactamente, en español y sin responder ni resumir, la consulta hablada en este audio. Devolvé únicamente las palabras que se entienden. Si no se entiende ninguna palabra, devolvé exactamente: AUDIO_NO_ENTENDIBLE.'},{'inline_data':{'mime_type':mime,'data':_alma_b64.b64encode(raw).decode('ascii')}}]}],'generationConfig':{'temperature':0,'maxOutputTokens':256}}
    try:
        req=urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent',data=json.dumps(payload).encode('utf-8'),headers={'x-goog-api-key':key,'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req,timeout=22) as response:
            data=json.loads(response.read().decode('utf-8'))
        text=' '.join(part.get('text','') for part in data.get('candidates',[{}])[0].get('content',{}).get('parts',[]) if part.get('text')).strip()
        text=text.strip('"“” \n\t')
        if not text or 'AUDIO_NO_ENTENDIBLE' in text.upper(): return jsonify({'error':'No entendí el audio. Podés grabarlo otra vez o escribir la consulta.'}),422
        if len(text)>500: text=text[:500].rsplit(' ',1)[0]
        return jsonify({'transcripcion':text,'mensaje':'Revisá lo que escuché y tocá Enviar para consultarle a Alma.'})
    except (TimeoutError, _alma_socket.timeout):
        return jsonify({'error':'El audio tardó demasiado en procesarse. Probá con una grabación más corta.'}),504
    except Exception:
        app.logger.warning('Alma audio transcription failed without saving audio')
        return jsonify({'error':'No pude procesar el audio. Probá otra vez o escribí tu consulta.'}),502

@app.route('/api/alma/catalog/libreria-ruiz', methods=['GET'])
def alma_readonly_libreria_ruiz():
    """Return public Librería Ruiz fields without rendering pages or recording visits."""
    expected=os.environ.get(ALMA_CATALOG_READONLY_KEY_ENV,"")
    if not expected:
        return jsonify({"error":"unavailable"}),503
    supplied=request.headers.get("Authorization","")
    prefix="Bearer "
    if not supplied.startswith(prefix) or not hmac.compare_digest(supplied[len(prefix):],expected):
        response=jsonify({"error":"unauthorized"})
        response.headers["WWW-Authenticate"]="Bearer"
        return response,401
    try:
        with get_db() as db:
            rows=db.execute(
                "SELECT codigo,nombre,desc_,precio,categoria,marca,foto,stock_actual "
                "FROM productos WHERE activo=1 AND catalogo_slug=? ORDER BY nombre",
                ("libreria-ruiz",)
            ).fetchall()
        products=[]
        for row in rows:
            item=dict(row)
            quantity=max(0,int(item.get("stock_actual") or 0))
            products.append({
                "code":item.get("codigo") or "",
                "name":item.get("nombre") or "",
                "description":item.get("desc_") or "",
                "price_ars":item.get("precio"),
                "currency":"ARS",
                "category":item.get("categoria") or "",
                "brand":item.get("marca") or "",
                "image":item.get("foto") or "",
                "stock_quantity":quantity,
                "available":quantity>0,
            })
        response=jsonify({"schema_version":1,"catalog":"libreria-ruiz","count":len(products),"products":products})
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Robots-Tag"]="noindex, nofollow"
        response.headers["Vary"]="Authorization"
        return response
    except Exception:
        app.logger.exception("Read-only Alma catalog endpoint failed")
        return jsonify({"error":"catalog_unavailable"}),503

@app.route('/manifest.json')
def web_manifest():
    slug=request.args.get('catalogo','libreria-ruiz').strip().lower()
    cfg=get_catalogo_config(slug)
    if not cfg:
        cfg=get_catalogo_config('libreria-ruiz')
    # Keep the install target tied to the catalog URL that the visitor opened.
    body=render_template('manifest.json',catalogo=cfg,manifest_slug=slug)
    return app.response_class(body,mimetype='application/manifest+json')

@app.route('/libreria-ruiz-sw.js')
def libreria_ruiz_service_worker():
    response=make_response(send_from_directory(BASE_DIR,'libreria-ruiz-sw.js',mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed']='/'; response.headers['Cache-Control']='no-cache'
    return response

@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    # Product photos live in DATA_DIR, not inside the deployable code folder.
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/static/<path:filename>')
def static_asset(filename):
    # Visual assets may live in static/ or at the repository root (kept deployable).
    static_path=os.path.join(BASE_DIR,'static',filename)
    if os.path.isfile(static_path):
        return send_from_directory(os.path.join(BASE_DIR,'static'), filename)
    if filename.startswith('banner_') or filename.startswith('social_preview_') or filename.startswith('category_') or filename.startswith('renata-beauty-'):
        return send_from_directory(BASE_DIR, filename)
    return ('',404)

@app.route('/menu')
def menu_digital():
    return render_template('menu.html')

@app.route('/fleming')
@app.route('/fleming/')
def fleming_brochure():
    return ('', 404)

@app.route('/fleming/cargar')
@app.route('/fleming/cargar/')
def fleming_cargar():
    return ('', 404)

@app.route('/cargar-fotos')
@app.route('/cargar-fotos/')
def cargar_fotos():
    # Mobile-first client-side tool: images stay on the phone and are packaged as one ZIP.
    return send_from_directory(BASE_DIR, 'cargar_fotos.html')

@app.route('/cargar-fotos/manifest.webmanifest')
def cargar_fotos_manifest():
    body=json.dumps({
        'name':'Preparador de fotos · Librería Ruiz',
        'short_name':'Fotos Ruiz',
        'start_url':'/cargar-fotos/',
        'scope':'/cargar-fotos/',
        'display':'standalone',
        'background_color':'#f7faf9',
        'theme_color':'#128C7E',
        'description':'Recortá y prepará fotos de catálogo desde el celular.'
    },ensure_ascii=False)
    return app.response_class(body,mimetype='application/manifest+json')

@app.route('/cargar-fotos/sw.js')
def cargar_fotos_service_worker():
    return send_from_directory(BASE_DIR, 'cargar_fotos-sw.js', mimetype='application/javascript')

VIVERO_COVER_SETTINGS=os.path.join(UPLOAD_FOLDER,'vivero-los-colibries-cover.json')
VIVERO_COVER_POSTER='/static/vivero_portada_purocolor_20261001.webp?v=20261001c'
VIVERO_COVER_VIDEO='/static/vivero_portada_purocolor_20261001.mp4?v=20261001c'
MAX_VIVERO_VIDEO_BYTES=50*1024*1024

def get_vivero_cover_urls():
    video_url=VIVERO_COVER_VIDEO
    try:
        with open(VIVERO_COVER_SETTINGS,'r',encoding='utf-8') as source:
            settings=json.load(source)
        filename=settings.get('video','')
        if (isinstance(filename,str) and secure_filename(filename)==filename and
                filename.lower().endswith('.mp4') and os.path.isfile(os.path.join(UPLOAD_FOLDER,filename))):
            video_url=url_for('uploaded_file',filename=filename)
    except (OSError,ValueError,TypeError):
        pass
    return {'vivero_cover_video_url':video_url,'vivero_cover_poster_url':VIVERO_COVER_POSTER}

def render_catalog_page(cats,showcase,catalogo):
    if catalogo and catalogo.get('slug') == 'renas-beauty' and request.path.rstrip('/') == '/c/renas-beauty':
        catalogo=dict(catalogo)
        user_agent=(request.headers.get('User-Agent') or '').lower()
        automated_agents=('bot','crawl','spider','slurp','preview','curl/','python-requests','headlesschrome','playwright','zapia','facebookexternalhit','whatsapp','telegrambot','discordbot','twitterbot','linkedinbot','bytespider','bingpreview','lighthouse','uptime')
        if request.method=='GET' and not any(token in user_agent for token in automated_agents):
            visits,_counted=record_rena_visit()
        else:
            with get_db() as db:
                row=db.execute("SELECT visitas FROM catalogos WHERE slug='renas-beauty'").fetchone()
            visits=max(int(row['visitas'] or 0) if row else 0,RENA_VISIT_COUNT_CACHE)
            # Refresh the displayed total from durable storage without counting automated hits.
            remote,read_ok,_=_read_rena_visit_count()
            if read_ok and remote is not None:
                visits=max(visits,remote)
        catalogo['visitas']=visits
        catalogo['visitas_formateadas']=format(visits,',').replace(',','.')
        rendered=make_response(render_template('index.html',cats=cats,showcase=showcase,catalogo=catalogo,**get_vivero_cover_urls()))
        rendered.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
        rendered.headers['Pragma']='no-cache'
        return rendered
    if catalogo and catalogo.get('slug') == 'vivero-los-colibries' and request.path.rstrip('/') == '/c/vivero-los-colibries':
        catalogo=dict(catalogo)
        user_agent=(request.headers.get('User-Agent') or '').lower()
        automated_agents=('bot','crawl','spider','slurp','preview','curl/','python-requests','headlesschrome','facebookexternalhit','whatsapp','telegrambot','discordbot','twitterbot','linkedinbot','bytespider','bingpreview')
        if request.method == 'GET' and not any(token in user_agent for token in automated_agents):
            visits,_counted=record_vivero_visit()
        else:
            with get_db() as db:
                row=db.execute("SELECT visitas FROM catalogos WHERE slug='vivero-los-colibries'").fetchone()
            visits=max(int(row['visitas'] or 0) if row else 0,VIVERO_VISIT_COUNT_CACHE)
        catalogo['visitas']=visits
        catalogo['visitas_formateadas']=format(visits, ',').replace(',', '.')
        rendered=make_response(render_template('index.html',cats=cats,showcase=showcase,catalogo=catalogo,**get_vivero_cover_urls()))
        rendered.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
        rendered.headers['Pragma']='no-cache'
        return rendered
    if catalogo and catalogo.get('slug') == 'libreria-ruiz' and request.path.rstrip('/') == '/c/libreria-ruiz':
        user_agent=(request.headers.get('User-Agent') or '').lower()
        automated_agents=('bot','crawl','spider','slurp','preview','curl/','python-requests','headlesschrome')
        if request.headers.get('X-Ruiz-Cache-Refresh') != '1' and not any(token in user_agent for token in automated_agents):
            visits=record_libreria_visit()
        else:
            with get_db() as db:
                row=db.execute("SELECT visitas FROM catalogos WHERE slug='libreria-ruiz'").fetchone()
            visits=max(int(row['visitas'] or 0) if row else 0,VISIT_COUNT_FLOOR)
        catalogo=dict(catalogo)
        catalogo['visitas']=visits
        catalogo['visitas_formateadas']=format(visits, ',').replace(',', '.')
    return render_template('index.html',cats=cats,showcase=showcase,catalogo=catalogo,**get_vivero_cover_urls())

@app.route('/vivero/portada/subir',methods=['GET','POST'])
@login_required
def vivero_portada_upload():
    if not get_catalogo_config('vivero-los-colibries'):
        return ('Vivero Los Colibríes no está disponible',404)
    message=None; error=None
    if request.method=='POST':
        uploaded=request.files.get('video')
        if not uploaded or not uploaded.filename:
            error='Elegí un video MP4 para continuar.'
        elif secure_filename(uploaded.filename).lower().rsplit('.',1)[-1]!='mp4':
            error='El formato admitido es MP4.'
        else:
            token=uuid.uuid4().hex
            temp_path=os.path.join(UPLOAD_FOLDER,'.vivero-cover-'+token+'.upload')
            final_name='vivero_portada_'+token+'.mp4'
            final_path=os.path.join(UPLOAD_FOLDER,final_name)
            temp_settings=VIVERO_COVER_SETTINGS+'.tmp'
            old_settings=None
            try:
                try:
                    with open(VIVERO_COVER_SETTINGS,'rb') as previous:
                        old_settings=previous.read()
                except FileNotFoundError:
                    pass
                header=uploaded.stream.read(16)
                uploaded.stream.seek(0)
                if len(header)<12 or header[4:8]!=b'ftyp':
                    raise ValueError('El archivo no parece ser un video MP4 válido.')
                total=0
                with open(temp_path,'wb') as destination:
                    while True:
                        chunk=uploaded.stream.read(1024*1024)
                        if not chunk: break
                        total+=len(chunk)
                        if total>MAX_VIVERO_VIDEO_BYTES:
                            raise ValueError('El video supera el límite de 50 MB.')
                        destination.write(chunk)
                if total==0:
                    raise ValueError('El archivo está vacío.')
                os.replace(temp_path,final_path)
                with open(temp_settings,'w',encoding='utf-8') as settings_file:
                    json.dump({'video':final_name,'updated_at':datetime.utcnow().isoformat(timespec='seconds')+'Z'},settings_file)
                os.replace(temp_settings,VIVERO_COVER_SETTINGS)
                try:
                    synced=cloud_sync()
                except Exception:
                    app.logger.exception('Falló la confirmación de respaldo en la nube del video del vivero')
                    synced=False
                if not synced:
                    if old_settings is None:
                        try: os.remove(VIVERO_COVER_SETTINGS)
                        except OSError: pass
                    else:
                        with open(VIVERO_COVER_SETTINGS,'wb') as previous:
                            previous.write(old_settings)
                    try: os.remove(final_path)
                    except OSError: pass
                    error='No se pudo confirmar el respaldo en la nube. La portada anterior quedó sin cambios; probá nuevamente más tarde.'
                else:
                    log_change('portada-video-vivero','vivero-los-colibries',final_name)
                    message='✅ Video actualizado y respaldado en la nube. La portada nueva ya está activa.'
            except ValueError as exc:
                error=str(exc)
                try: os.remove(final_path)
                except OSError: pass
            except Exception:
                app.logger.exception('No se pudo actualizar el video de portada del vivero')
                error='No se pudo procesar el video. La portada anterior se mantiene.'
                try:
                    if old_settings is None:
                        os.remove(VIVERO_COVER_SETTINGS)
                    else:
                        with open(VIVERO_COVER_SETTINGS,'wb') as previous:
                            previous.write(old_settings)
                except OSError: pass
                try: os.remove(final_path)
                except OSError: pass
            finally:
                for leftover in (temp_path,temp_settings):
                    try: os.remove(leftover)
                    except OSError: pass
    urls=get_vivero_cover_urls()
    return render_template('vivero_portada_upload.html',message=message,error=error,video_url=urls['vivero_cover_video_url'],poster_url=urls['vivero_cover_poster_url'])
@app.route('/')
def index():
    cfg=current_config(); return render_catalog_page(get_catalogo(current_slug()),get_showcase(current_slug()),cfg)
@app.route('/c/<slug>')
def catalogo_publico(slug):
    cfg=get_catalogo_config(slug)
    if not cfg: return redirect(url_for('index'))
    return render_catalog_page(get_catalogo(slug),get_showcase(slug),cfg)

@app.route('/lector-qr')
def lector_qr_independiente():
    """Offline-installable QR-only reader. It decodes locally and never queries catalog data."""
    response=make_response(send_from_directory(BASE_DIR,'lector_qr_libre.html',mimetype='text/html'))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response

@app.route('/lector-qr/manifest.webmanifest')
def lector_qr_manifest_scoped():
    return send_from_directory(BASE_DIR,'lector-qr-independiente.webmanifest',mimetype='application/manifest+json')

@app.route('/lector-qr/lector-qr-sw.js')
def lector_qr_sw_scoped():
    response=make_response(send_from_directory(BASE_DIR,'lector-qr-independiente-sw.js',mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed']='/lector-qr'
    response.headers['Cache-Control']='no-cache'
    return response

@app.route('/lector-qr/html5-qrcode.min.js')
def lector_qr_library_scoped():
    response=make_response(send_from_directory(BASE_DIR,'lector-qr.min.js',mimetype='application/javascript'))
    response.headers['Cache-Control']='public, max-age=31536000, immutable'
    return response

@app.route('/consulta-qr')
def consulta_qr_libreria():
    """Installable QR price lookup for Librería Ruiz phones."""
    response=make_response(render_template('consulta_qr.html'))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response

@app.route('/lector-qr.webmanifest')
def consulta_qr_manifest():
    response=make_response(send_from_directory(BASE_DIR,'lector-qr.webmanifest',mimetype='application/manifest+json'))
    response.headers['Cache-Control']='public, max-age=3600'
    return response

@app.route('/lector-qr-sw.js')
def consulta_qr_service_worker():
    response=make_response(send_from_directory(BASE_DIR,'lector-qr-sw.js',mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed']='/'
    response.headers['Cache-Control']='no-cache'
    return response

@app.route('/api/consulta-qr/<path:raw_code>')
def api_consulta_qr_libreria(raw_code):
    """Resolve either a product SKU or a direct /p/<SKU> URL to live catalog data."""
    from urllib.parse import unquote, urlparse
    value=unquote(str(raw_code or '').strip()).strip().strip('\"\'<>')
    if len(value)>500:
        return jsonify({'error':'Código demasiado largo'}),400
    if '://' in value:
        parsed=urlparse(value)
        match=__import__('re').search(r'(?:^|/)p/([^/?#]+)',parsed.path)
        if match:
            value=unquote(match.group(1))
        elif parsed.path.rstrip('/').endswith('/consulta-qr'):
            query=__import__('urllib.parse',fromlist=['parse_qs']).parse_qs(parsed.query)
            value=(query.get('codigo') or query.get('code') or [''])[0]
        else:
            value=''
    value=value.split('?',1)[0].split('#',1)[0].rstrip('/')
    value=value.rsplit('/',1)[-1].strip()
    if not value or len(value)>120:
        return jsonify({'error':'No reconocí un código de producto'}),400
    with get_db() as db:
        row=db.execute(
            'SELECT codigo,nombre,desc_,precio,categoria,marca,foto,stock_actual '
            'FROM productos WHERE activo=1 AND catalogo_slug=? AND codigo=? LIMIT 1',
            ('libreria-ruiz',value)
        ).fetchone()
    if not row:
        response=jsonify({'error':'No encontré ese producto. Revisá que el QR corresponda a Librería Ruiz.'})
        response.headers['Cache-Control']='no-store'
        return response,404
    item=dict(row)
    photo=str(item.get('foto') or '').strip()
    if item['codigo']=='SANDISK-CRUZER-BLADE-16GB-2026':
        photo='https://raw.githubusercontent.com/imprentaruiz-salta/catalogo-digital/main/sandisk_cruzer_blade_16gb.webp'
    elif photo and not photo.startswith(('http://','https://','/')):
        photo='/static/uploads/'+photo
    item['image']=photo
    item['price_display']='ARS '+format(int(round(float(item.get('precio') or 0))),',').replace(',','.')
    stock_count=max(0,int(item.get('stock_actual') or 0)); item['stock_display']=(f'{stock_count} disponible' if stock_count==1 else f'{stock_count} disponibles') if stock_count>0 else 'Consultá disponibilidad'
    response=jsonify(item)
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    response.headers['X-Robots-Tag']='noindex, nofollow'
    return response

@app.route('/p/<path:code>')
def producto_por_qr(code):
    """Ficha individual por QR: siempre consulta el precio y datos vigentes."""
    if not code or len(code)>120:
        return ('Producto no encontrado',404)
    with get_db() as db:
        row=db.execute(
            'SELECT codigo,nombre,desc_,precio,categoria,marca,foto,stock_actual '
            'FROM productos WHERE activo=1 AND catalogo_slug=? AND codigo=? LIMIT 1',
            ('libreria-ruiz',code)
        ).fetchone()
    if not row:
        return ('Producto no encontrado',404)
    producto=dict(row)
    if producto['codigo']=='SANDISK-CRUZER-BLADE-16GB-2026':
        foto='https://raw.githubusercontent.com/imprentaruiz-salta/catalogo-digital/main/sandisk_cruzer_blade_16gb.webp'
    else:
        foto=str(producto.get('foto') or '').strip()
        if foto and not (foto.startswith('http://') or foto.startswith('https://') or foto.startswith('/')):
            foto='/static/uploads/'+foto
    producto['foto_qr']=foto
    producto['precio_display']=format(int(round(float(producto.get('precio') or 0))),',').replace(',','.')
    stock_count=max(0,int(producto.get('stock_actual') or 0)); producto['stock_display']=(f'{stock_count} disponible' if stock_count==1 else f'{stock_count} disponibles') if stock_count>0 else 'Consultá disponibilidad'
    producto['description_display']=producto.get('desc_') or 'Consultá características con Librería Ruiz.'
    response=make_response(render_template('qr_producto.html',producto=producto))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    response.headers['X-Robots-Tag']='noindex, nofollow'
    return response

@app.route('/admin/cargar-renata')
@login_required
def admin_cargar_renata():
    """Install/update only the confirmed-price beauty products for Rena's Beauty."""
    slug='renas-beauty'
    banner='/static/renata-beauty-banner.jpg'
    products=[
        ('REN-BEA-001','Máscara de pestañas Pum Pum',4800,'/static/renata-beauty-rimel-pum-pum.webp'),
        ('REN-BEA-002','Delineador',2000,'/static/renata-beauty-delineador.webp'),
        ('REN-BEA-003','Sombra de ojos',3500,'/static/renata-beauty-sombra.webp'),
        ('REN-BEA-004','Iluminador',2800,'/static/renata-beauty-iluminador.webp'),
        ('REN-BEA-005','Rímel ($4.000)',4000,'/static/renata-beauty-rimel-4000.webp'),
        ('REN-BEA-006','Crema de manos',1600,'/static/renata-beauty-crema-manos.webp'),
        ('REN-BEA-007','Rímel ($3.400)',3400,'/static/renata-beauty-rimel-3400.webp'),
        ('REN-BEA-008','Corrector',3600,'/static/renata-beauty-corrector.webp'),
        ('REN-BEA-009','Base',3500,'/static/renata-beauty-base.webp'),
        ('REN-BEA-010','Lipgloss ($4.200)',4200,'/static/renata-beauty-lipgloss-4200.webp'),
        ('REN-BEA-011','Lipgloss ($3.600)',3600,'/static/renata-beauty-lipgloss-3600.webp'),
        ('REN-BEA-012','Delineador de labios',2500,'/static/renata-beauty-delineador-labios.webp'),
        ('REN-BEA-013','Combo delineador + lipgloss matte',5000,'/static/renata-beauty-combo.webp'),
        ('REN-BEA-014','Lipgloss matte',3800,'/static/renata-beauty-lipgloss-matte.webp'),
    ]
    backup_db('antes-carga-renata')
    with get_db() as db:
        db.execute("INSERT INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram,banner,activo) VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(slug) DO UPDATE SET nombre=excluded.nombre,subtitulo=excluded.subtitulo,logo=excluded.logo,whatsapp=excluded.whatsapp,banner=excluded.banner,activo=1",(slug,"Rena’s Beauty","Maquillaje y belleza",banner,"5493874832731","",banner))
        for code,name,price,image in products:
            desc=''
            db.execute("INSERT INTO productos(codigo,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor,nivel_precio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(codigo) DO UPDATE SET nombre=excluded.nombre,desc_=excluded.desc_,precio=excluded.precio,categoria=excluded.categoria,marca=excluded.marca,foto=excluded.foto,activo=1,catalogo_slug=excluded.catalogo_slug,stock_actual=0,nivel_precio=excluded.nivel_precio",(code,name,desc,price,'Maquillaje','Varios',image,1,1,slug,0,0,0,'','Estándar'))
        db.commit()
    cloud_sync()
    log_change('carga-renata',slug,str(len(products)))
    session['catalogo_slug']=slug
    return jsonify(ok=True,catalogo=slug,productos=len(products))

@app.route('/api/pedido-telegram',methods=['POST'])
def api_pedido_telegram():
    """Send a catalog order directly to the owner's Telegram bot chat."""
    data=request.get_json(silent=True) or {}
    token=os.environ.get('TELEGRAM_BOT_TOKEN','').strip()
    chat_id=os.environ.get('TELEGRAM_CHAT_ID','8899404755').strip()
    if not token:
        return jsonify(ok=False,error='Telegram todavía no está conectado'),503
    text=str(data.get('text','')).strip()
    if not text or len(text)>3900:
        return jsonify(ok=False,error='Pedido inválido'),400
    payload=json.dumps({'chat_id':chat_id,'text':text}).encode('utf-8')
    try:
        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=payload,method='POST',headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:
            result=json.loads(response.read().decode('utf-8'))
        if not result.get('ok'):
            return jsonify(ok=False,error='Telegram no aceptó el pedido'),502
        return jsonify(ok=True)
    except Exception:
        app.logger.exception('No se pudo enviar el pedido por Telegram')
        return jsonify(ok=False,error='No se pudo enviar el pedido por Telegram'),502

@app.route('/api/libreria/producto-faltante',methods=['POST'])
def api_libreria_producto_faltante():
    """Anonymous demand counter for exact missing products, Librería Ruiz only."""
    data=request.get_json(silent=True) or {}
    producto=' '.join(str(data.get('producto') or '').split())[:120]
    detalle=' '.join(str(data.get('detalle') or '').split())[:120]
    try:
        unidades=int(data.get('unidades',1))
    except (TypeError,ValueError):
        return jsonify(ok=False,error='Cantidad inválida.'),400
    if not producto:
        return jsonify(ok=False,error='Escribí el producto que buscás.'),400
    if unidades<1 or unidades>25:
        return jsonify(ok=False,error='La cantidad debe ser de 1 a 25 unidades.'),400
    def norm_key(value):
        value=unicodedata.normalize('NFKD',value.casefold())
        return ' '.join(''.join(ch for ch in value if not unicodedata.combining(ch)).split())
    product_key=norm_key(producto)+'|'+norm_key(detalle)
    with get_db() as db:
        db.execute("""INSERT INTO producto_interes(catalogo_slug,product_key,producto,detalle,unidades,solicitudes,estado,actualizado)
          VALUES('libreria-ruiz',?,?,?,?,1,'pendiente',CURRENT_TIMESTAMP)
          ON CONFLICT(catalogo_slug,product_key) DO UPDATE SET unidades=producto_interes.unidades+excluded.unidades, solicitudes=producto_interes.solicitudes+1, actualizado=CURRENT_TIMESTAMP""",(product_key,producto,detalle,unidades))
        row=db.execute("SELECT unidades,solicitudes FROM producto_interes WHERE catalogo_slug='libreria-ruiz' AND product_key=?",(product_key,)).fetchone()
        db.commit()
    persisted=cloud_sync()
    return jsonify(ok=True,persistido=bool(persisted),unidades=int(row['unidades']),solicitudes=int(row['solicitudes']),umbral=5,alerta=int(row['unidades'])>=5),201

@app.route('/api/sugerencia',methods=['POST'])
def api_sugerencia():
    data=request.get_json(silent=True) or request.form
    producto=(data.get('producto') or '').strip()[:120]; nombre=(data.get('nombre') or '').strip()[:80]; comentario=(data.get('comentario') or '').strip()[:200]; slug=(data.get('catalogo_slug') or 'libreria-ruiz').strip()
    try: cantidad=max(1,min(999,int(data.get('cantidad') or 1)))
    except (TypeError,ValueError): cantidad=1
    if not producto: return jsonify(ok=False,error='Escribí un producto')
    with get_db() as db:
        row=db.execute('SELECT id FROM sugerencias WHERE catalogo_slug=? AND lower(producto)=lower(?)',(slug,producto)).fetchone()
        if row: db.execute('UPDATE sugerencias SET cantidad=cantidad+1,cantidad_necesita=cantidad_necesita+?,nombre=?,comentario=? WHERE id=?',(cantidad,nombre,comentario,row['id']))
        else: db.execute('INSERT INTO sugerencias(catalogo_slug,producto,nombre,cantidad_necesita,comentario) VALUES(?,?,?,?,?)',(slug,producto,nombre,cantidad,comentario))
        db.commit()
    cloud_sync()
    return jsonify(ok=True)

def _ruiz_private_storage_request(path, method="GET", data=None, content_type="application/json"):
    if not cloud_enabled():
        raise RuntimeError("El almacenamiento privado todavía no está configurado")
    safe_path=urllib.parse.quote(path.lstrip("/"),safe="/")
    endpoint=f"{SUPABASE_URL}/storage/v1/object/{RUÍZ_ORDERS_BUCKET}/{safe_path}"
    req=urllib.request.Request(endpoint,data=data,method=method,headers={
        "Authorization":f"Bearer {SUPABASE_SERVICE_KEY}","apikey":SUPABASE_SERVICE_KEY,
        "Content-Type":content_type,"x-upsert":"true"})
    return urllib.request.urlopen(req,timeout=25)

def _ruiz_private_list(prefix, limit=100):
    endpoint=f"{SUPABASE_URL}/storage/v1/object/list/{RUÍZ_ORDERS_BUCKET}"
    payload=json.dumps({"prefix":prefix,"limit":limit,"sortBy":{"column":"name","order":"desc"}}).encode()
    req=urllib.request.Request(endpoint,data=payload,method="POST",headers={
        "Authorization":f"Bearer {SUPABASE_SERVICE_KEY}","apikey":SUPABASE_SERVICE_KEY,
        "Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as response:
        return json.loads(response.read().decode())

def _ruiz_private_get_json(path):
    with _ruiz_private_storage_request(path) as response:
        return json.loads(response.read().decode("utf-8"))

def _ruiz_orders_authenticated():
    if not RUÍZ_ORDER_ADMIN_PASSWORD:
        return False
    token=session.get("ruiz_orders_admin")
    return isinstance(token,str) and hmac.compare_digest(token,"ok")

@app.route('/api/libreria/pedido-completo',methods=['POST'])
def api_libreria_pedido_completo():
    # Privately persist a Librería Ruiz order and its optional media, then alert Telegram.
    if request.content_length and request.content_length>36*1024*1024:
        return jsonify(ok=False,error="El pedido supera el tamaño permitido."),413
    now=datetime.now(ZoneInfo("America/Argentina/Salta"))
    peer=request.remote_addr or "unknown"
    recent=[x for x in RUÍZ_ORDER_RATE_LIMIT.get(peer,[]) if (now-x).total_seconds()<600]
    if len(recent)>=8:
        return jsonify(ok=False,error="Esperá unos minutos antes de volver a enviar."),429
    text="\n".join(line.strip() for line in (request.form.get("text") or "").splitlines() if line.strip())[:5000]
    try:
        items=json.loads(request.form.get("items") or "[]")
        if not isinstance(items,list): items=[]
        items=[{"name":str(x.get("name") or "")[:180],"code":str(x.get("code") or "")[:80],"qty":max(1,min(999,int(x.get("qty") or 1)))} for x in items[:80] if isinstance(x,dict)]
    except Exception: items=[]
    photo=request.files.get("photo"); audio=request.files.get("audio")
    files=[]
    for file,kind,allowed,max_size in [
        (photo,"photo",{"image/jpeg":"jpg","image/png":"png","image/webp":"webp"},RUÍZ_ORDER_MAX_PHOTO),
        (audio,"audio",{"audio/webm":"webm","audio/mpeg":"mp3","audio/mp4":"m4a","audio/x-m4a":"m4a","audio/ogg":"ogg","audio/wav":"wav","audio/x-wav":"wav","audio/aac":"aac"},RUÍZ_ORDER_MAX_AUDIO)]:
        if file and file.filename:
            mime=(file.mimetype or "").lower().split(";")[0].strip()
            if mime not in allowed: return jsonify(ok=False,error="Ese formato de archivo no está admitido."),400
            raw=file.read(max_size+1)
            if len(raw)>max_size: return jsonify(ok=False,error=("La foto supera 12 MB." if kind=="photo" else "El audio supera 20 MB.")),413
            files.append((kind,allowed[mime],mime,raw))
    if not text and not items and not files:
        return jsonify(ok=False,error="Agregá texto, productos, foto o audio."),400
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not RUÍZ_ORDER_ADMIN_PASSWORD:
        return jsonify(ok=False,error="El buzón privado todavía no está configurado."),503
    RUÍZ_ORDER_RATE_LIMIT[peer]=recent+[now]
    order_id=now.strftime("%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:10]
    obj={"id":order_id,"created_at":now.isoformat(timespec="seconds"),"text":text,"items":items,"files":[],"status":"nuevo"}
    uploaded=[]
    try:
        for kind,ext,mime,raw in files:
            path=f"orders/{order_id}/{kind}.{ext}"
            with _ruiz_private_storage_request(path,"POST",raw,mime) as response: response.read()
            uploaded.append(path); obj["files"].append({"kind":kind,"path":path,"filename":kind+"."+ext,"mime":mime,"size":len(raw)})
        with _ruiz_private_storage_request(f"orders/{order_id}/order.json","POST",json.dumps(obj,ensure_ascii=False).encode(),"application/json") as response: response.read()
    except Exception:
        app.logger.exception("Private order storage failed")
        for path in uploaded:
            try:
                req=urllib.request.Request(f"{SUPABASE_URL}/storage/v1/object/{RUÍZ_ORDERS_BUCKET}/{urllib.parse.quote(path,safe='/')}",method="DELETE",headers={"Authorization":f"Bearer {SUPABASE_SERVICE_KEY}","apikey":SUPABASE_SERVICE_KEY})
                urllib.request.urlopen(req,timeout=10).read()
            except Exception: pass
        return jsonify(ok=False,error="No se pudo guardar el pedido privado. No se envió; probá de nuevo."),503
    notice="📥 Nuevo pedido privado de Librería Ruiz\nN° "+order_id+"\n"+str(len(items))+" productos · "+str(len(files))+" adjuntos\nTexto: "+(text[:180] or "sin texto")+"\nAbrí el panel privado: https://catalogo-app-zm3w.onrender.com/admin/pedidos"
    telegram_sent=False
    token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip();chat_id=os.environ.get("TELEGRAM_CHAT_ID","8899404755").strip()
    if token and chat_id:
        try:
            payload=json.dumps({"chat_id":chat_id,"text":notice}).encode()
            req=urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",data=payload,method="POST",headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req,timeout=15) as response: telegram_sent=bool(json.loads(response.read().decode()).get("ok"))
        except Exception: app.logger.exception("Telegram order notification failed")
    return jsonify(ok=True,id=order_id,notification_sent=telegram_sent),201

@app.route('/admin/pedidos/login',methods=['GET','POST'])
def ruiz_orders_login():
    if request.method=="POST":
        if RUÍZ_ORDER_ADMIN_PASSWORD and hmac.compare_digest(request.form.get("password", ""),RUÍZ_ORDER_ADMIN_PASSWORD):
            session["ruiz_orders_admin"]="ok";return redirect(url_for("ruiz_orders_inbox"))
        return render_template_string(_RUIZ_ORDER_LOGIN,error="La contraseña no es correcta."),401
    return render_template_string(_RUIZ_ORDER_LOGIN,error="")

_RUIZ_ORDER_LOGIN='''<!doctype html><html lang="es"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bandeja privada · Librería Ruiz</title><body style="font:16px system-ui;background:#edf3f8;padding:24px"><main style="max-width:440px;margin:12vh auto;background:white;padding:24px;border-radius:16px;box-shadow:0 6px 24px #1232"><h1>Bandeja privada de pedidos</h1><p>Ingresá la contraseña de administración.</p>{% if error %}<p style="color:#a11">{{error}}</p>{% endif %}<form method="post"><label for="password">Contraseña</label><input id="password" name="password" type="password" required autocomplete="current-password" style="display:block;width:100%;padding:12px;margin:10px 0 16px;box-sizing:border-box"><button style="padding:12px 18px">Entrar</button></form></main></body></html>'''

@app.route('/admin/pedidos/logout',methods=['POST'])
def ruiz_orders_logout():
    session.pop("ruiz_orders_admin",None);return redirect(url_for("ruiz_orders_login"))

@app.route('/admin/pedidos')
def ruiz_orders_inbox():
    if not _ruiz_orders_authenticated(): return redirect(url_for("ruiz_orders_login"))
    orders=[];error=""
    try:
        entries=_ruiz_private_list("orders/",1000)
        order_ids=set()
        for entry in entries:
            name=str(entry.get("name","")).strip("/")
            if not name: continue
            parts=name.split("/")
            candidate=parts[0] if len(parts)>1 else name
            if __import__('re').fullmatch(r"\d{8}-\d{6}-[a-f0-9]{10}",candidate): order_ids.add(candidate)
        for order_id in order_ids:
            try: orders.append(_ruiz_private_get_json(f"orders/{order_id}/order.json"))
            except Exception: continue
        orders.sort(key=lambda x:x.get("created_at", ""),reverse=True)
    except Exception:
        app.logger.exception("Could not read private orders")
        error="No se pudo cargar la bandeja privada. Revisá la conexión de almacenamiento."
    return render_template_string(_RUIZ_ORDERS_PAGE,orders=orders,error=error)

@app.route('/admin/pedidos/archivo/<order_id>/<kind>')
def ruiz_orders_file(order_id,kind):
    if not _ruiz_orders_authenticated(): return redirect(url_for("ruiz_orders_login"))
    if not __import__('re').fullmatch(r"\d{8}-\d{6}-[a-f0-9]{10}",order_id) or kind not in ("photo","audio"):
        return "No encontrado",404
    try:
        order=_ruiz_private_get_json(f"orders/{order_id}/order.json")
        file=next((x for x in order.get("files",[]) if x.get("kind")==kind),None)
        if not file: return "No encontrado",404
        with _ruiz_private_storage_request(file["path"]) as response: raw=response.read()
        return send_file(io.BytesIO(raw),mimetype=file.get("mime","application/octet-stream"),as_attachment=(kind=="audio"),download_name=file.get("filename",kind))
    except Exception:
        app.logger.exception("Private order attachment download failed")
        return "No se pudo abrir el adjunto",503

_RUIZ_ORDERS_PAGE='''<!doctype html><html lang="es"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Pedidos privados · Librería Ruiz</title><body style="font:15px system-ui;background:#edf3f8;margin:0;color:#193750"><header style="background:#dceafa;padding:18px max(18px,calc((100% - 860px)/2));display:flex;justify-content:space-between;align-items:center"><h1 style="font-size:22px;margin:0">📥 Pedidos de Librería Ruiz</h1><form action="/admin/pedidos/logout" method="post"><button>Salir</button></form></header><main style="max-width:860px;margin:20px auto;padding:0 14px">{% if error %}<p>{{error}}</p>{% elif not orders %}<p style="background:white;padding:20px;border-radius:12px">Todavía no hay pedidos.</p>{% endif %}{% for o in orders %}<article style="background:white;padding:18px;margin:12px 0;border-radius:14px;box-shadow:0 2px 8px #1231"><h2 style="font-size:17px">Pedido {{o.id}}</h2><p>{{o.created_at}}</p>{% if o['items'] %}<h3>Productos</h3><ul>{% for i in o['items'] %}<li>{{i.qty}} × {{i.name}} ({{i.code}})</li>{% endfor %}</ul>{% endif %}{% if o.text %}<h3>Texto del cliente</h3><pre style="white-space:pre-wrap;font:inherit;background:#f5f8fb;padding:12px;border-radius:8px">{{o.text}}</pre>{% endif %}{% for f in o['files'] %}<p>{% if f['kind']=='photo' %}<strong>Foto:</strong>{% else %}<strong>Audio:</strong>{% endif %} <a href="/admin/pedidos/archivo/{{o.id}}/{{f['kind']}}">Abrir adjunto ({{(f['size']/1024)|round|int}} KB)</a></p>{% endfor %}</article>{% endfor %}</main></body></html>'''

@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    error=None
    if request.method=='POST':
        if request.form.get('user')==ADMIN_USER and request.form.get('pass')==ADMIN_PASS: session['admin']=True; return redirect(url_for('admin_index'))
        error='Usuario o contrasena incorrectos'
    return render_template('login.html',error=error)
@app.route('/admin/logout')
def admin_logout(): session.clear(); return redirect(url_for('admin_login'))
@app.route('/admin')
@app.route('/admin/')
@login_required
def admin_index():
    slug=current_slug()
    with get_db() as db:
        productos=db.execute('SELECT * FROM productos WHERE catalogo_slug=? ORDER BY categoria,marca,nombre',(slug,)).fetchall(); catalogos=db.execute('SELECT * FROM catalogos WHERE activo=1 ORDER BY nombre').fetchall()
        sugerencias=db.execute('SELECT producto,SUM(cantidad) AS votos,SUM(cantidad_necesita) AS unidades,GROUP_CONCAT(DISTINCT nombre) AS nombres,MAX(estado) AS estado,GROUP_CONCAT(DISTINCT comentario) AS comentarios FROM sugerencias WHERE catalogo_slug=? GROUP BY lower(producto) ORDER BY votos DESC,producto',(slug,)).fetchall()
        intereses_faltantes=db.execute('SELECT producto,detalle,unidades,solicitudes,estado,actualizado FROM producto_interes WHERE catalogo_slug=? ORDER BY unidades DESC,solicitudes DESC,producto',(slug,)).fetchall() if slug=='libreria-ruiz' else []
    return render_template('admin.html',productos=[dict(p) for p in productos],catalogos=[dict(c) for c in catalogos],catalogo=current_config(),sugerencias=[dict(s) for s in sugerencias],intereses_faltantes=[dict(s) for s in intereses_faltantes])
@app.route('/admin/catalogo/seleccionar',methods=['POST'])
@login_required
def seleccionar_catalogo():
    slug=request.form.get('slug','libreria-ruiz')
    if get_catalogo_config(slug): session['catalogo_slug']=slug
    return redirect(url_for('admin_index'))
@app.route('/admin/catalogo/editar',methods=['POST'])
@login_required
def editar_catalogo():
    backup_db('antes-editar-catalogo')
    slug=request.form.get('slug','')
    with get_db() as db:
        db.execute('UPDATE catalogos SET nombre=?,subtitulo=?,logo=?,whatsapp=?,telegram=?,banner=? WHERE slug=?',(request.form.get('nombre','').strip(),request.form.get('subtitulo',''),request.form.get('logo',''),request.form.get('whatsapp',''),request.form.get('telegram','').lstrip('@'),request.form.get('banner',''),slug)); db.commit()
    cloud_sync()
    log_change('edicion-catalogo',slug,'configuración actualizada')
    session['catalogo_slug']=slug
    return redirect(url_for('admin_index'))
@app.route('/admin/catalogo/nuevo',methods=['POST'])
@login_required
def nuevo_catalogo():
    backup_db('antes-nuevo-catalogo')
    slug=secure_filename(request.form.get('slug','')).lower().replace('_','-'); nombre=request.form.get('nombre','').strip()
    if slug and nombre:
        with get_db() as db: db.execute('INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram,banner) VALUES(?,?,?,?,?,?,?)',(slug,nombre,request.form.get('subtitulo',''),request.form.get('logo',''),request.form.get('whatsapp','5493872101274'),request.form.get('telegram',''),request.form.get('banner',''))); db.commit()
        cloud_sync()
        session['catalogo_slug']=slug
    return redirect(url_for('admin_index'))
def get_categorias():
    with get_db() as db: rows=db.execute('SELECT DISTINCT categoria FROM productos WHERE catalogo_slug=? ORDER BY categoria',(current_slug(),)).fetchall()
    return sorted(set([r['categoria'] for r in rows]+list(CAT_ICONS)))
@app.route('/admin/producto/nuevo',methods=['GET','POST'])
@login_required
def admin_nuevo():
    if request.method=='POST': return _guardar_producto(None)
    return render_template('producto_form.html',producto=None,categorias=get_categorias(),catalogo=current_config())
@app.route('/admin/producto/<int:pid>/editar',methods=['GET','POST'])
@login_required
def admin_editar(pid):
    with get_db() as db: prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone()
    if not prod: return redirect(url_for('admin_index'))
    if request.method=='POST': return _guardar_producto(pid)
    return render_template('producto_form.html',producto=dict(prod),categorias=get_categorias(),catalogo=current_config())
def _guardar_producto(pid):
    backup_db('antes-producto')
    f=request.form; cat=f.get('categoria',''); nivel_precio=f.get('nivel_precio','Estándar').strip() or 'Estándar'; cat=f.get('nueva_categoria','').strip() if cat=='__nueva__' else cat; codigo=f.get('codigo','').strip().upper(); codigo_barras=''.join(ch for ch in f.get('codigo_barras','').strip() if ch.isalnum()); nombre=f.get('nombre','').strip(); desc_=f.get('descripcion','').strip(); precio=float(f.get('precio',0) or 0); marca=f.get('marca','').strip(); activo=1 if f.get('activo') else 0; stock=1 if f.get('stock') else 0
    try: stock_actual=max(0,int(f.get('stock_actual',0) or 0)); stock_minimo=max(0,int(f.get('stock_minimo',0) or 0)); costo=float(f.get('costo',0) or 0)
    except ValueError: stock_actual=stock_minimo=0; costo=0
    proveedor=f.get('proveedor','').strip(); foto_name=''; file=request.files.get('foto')
    if not codigo or not nombre or not cat or not marca:
        return render_template('producto_form.html',producto=None if not pid else dict(get_db().execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() or {}),categorias=get_categorias(),catalogo=current_config(),error='Completá código, nombre, categoría y marca.')
    if file and file.filename:
        if not allowed_file(file.filename): return render_template('producto_form.html',producto=None if not pid else dict(get_db().execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() or {}),categorias=get_categorias(),catalogo=current_config(),error='Formato de imagen no permitido.')
    with get_db() as db:
        duplicate=db.execute('SELECT id FROM productos WHERE codigo=? AND id!=?',(codigo,pid or 0)).fetchone()
        if duplicate:
            prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() if pid else None
            return render_template('producto_form.html',producto=dict(prod) if prod else None,categorias=get_categorias(),catalogo=current_config(),error=f'El código {codigo} ya existe. Elegí otro para no pisar productos.')
        barcode_duplicate=db.execute("SELECT codigo FROM productos WHERE codigo_barras=? AND id!=? AND codigo_barras!=''",(codigo_barras,pid or 0)).fetchone() if codigo_barras else None
        if barcode_duplicate:
            prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() if pid else None
            return render_template('producto_form.html',producto=dict(prod) if prod else None,categorias=get_categorias(),catalogo=current_config(),error='Ese código de barras ya está asociado a '+barcode_duplicate['codigo']+'. Revisalo antes de guardar.')
        if file and file.filename:
            try: foto_name=save_optimized_image(file)
            except ValueError as exc:
                prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() if pid else None
                return render_template('producto_form.html',producto=dict(prod) if prod else None,categorias=get_categorias(),catalogo=current_config(),error=str(exc))
        if pid:
            existing=db.execute('SELECT foto FROM productos WHERE id=?',(pid,)).fetchone(); foto_name=foto_name or (existing['foto'] if existing else '')
            db.execute('UPDATE productos SET codigo=?,codigo_barras=?,nombre=?,desc_=?,precio=?,categoria=?,marca=?,foto=?,activo=?,stock=?,catalogo_slug=?,stock_actual=?,stock_minimo=?,costo=?,proveedor=?,nivel_precio=? WHERE id=?',(codigo,codigo_barras,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio,pid))
        else: db.execute('INSERT INTO productos(codigo,codigo_barras,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor,nivel_precio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(codigo,codigo_barras,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio))
        db.commit()
    cloud_sync()
    log_change('producto',current_slug(),('editar' if pid else 'crear')+' '+codigo)
    return redirect(url_for('admin_index'))
@app.route('/admin/productos/disponibles',methods=['POST'])
@login_required
def admin_marcar_disponibles():
    """Mark every product in the selected catalog as available without inventing quantities."""
    slug=current_slug()
    with get_db() as db:
        db.execute('UPDATE productos SET stock=1 WHERE catalogo_slug=?',(slug,))
        db.commit()
    cloud_sync()
    log_change('disponibilidad',slug,'todos los productos disponibles')
    flash('✅ Todos los productos quedaron disponibles')
    return redirect(url_for('admin_index'))

@app.route('/admin/producto/<int:pid>/eliminar',methods=['POST'])
@login_required
def admin_eliminar(pid):
    backup_db('antes-eliminar-producto')
    with get_db() as db: db.execute('UPDATE productos SET activo=0 WHERE id=?',(pid,)); db.commit()
    cloud_sync()
    log_change('ocultar-producto',current_slug(),str(pid))
    return redirect(url_for('admin_index'))
@app.route('/admin/estado-datos')
@login_required
def admin_estado_datos():
    status=storage_status()
    with get_db() as db:
        status["catalogos"]=db.execute('SELECT COUNT(*) FROM catalogos').fetchone()[0]
        status["productos"]=db.execute('SELECT COUNT(*) FROM productos').fetchone()[0]
        status["fotos"]=db.execute("SELECT COUNT(*) FROM productos WHERE foto IS NOT NULL AND foto!=''").fetchone()[0]
    return jsonify(status)

@app.route('/admin/backup')
@login_required
def admin_backup():
    path=backup_db('desde-panel')
    if not path: return 'No hay datos para respaldar', 404
    return send_file(path,as_attachment=True,download_name=os.path.basename(path))

@app.route('/admin/exportar')
@login_required
def admin_exportar():
    """Export all catalog data and product images into one portable ZIP."""
    path=backup_db('exportacion')
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
        if path: z.write(path,'base/catalogo.sqlite3')
        for root,_,files in os.walk(UPLOAD_FOLDER):
            for name in files: z.write(os.path.join(root,name),os.path.join('imagenes',name))
        manifest={"version":1,"created_utc":datetime.utcnow().isoformat()+"Z","database":"base/catalogo.sqlite3","images":[]}
        for info in z.infolist():
            if info.filename.startswith('imagenes/') and not info.is_dir():
                data=z.read(info.filename); manifest["images"].append({"file":info.filename,"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)})
        z.writestr('README.txt','Exportación del catálogo. La base SQLite contiene catálogos, productos, sugerencias y configuraciones.\n')
        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    mem.seek(0)
    return send_file(mem,as_attachment=True,download_name='exportacion-catalogos.zip',mimetype='application/zip')

@app.route('/admin/importar',methods=['POST'])
@login_required
def admin_importar():
    """Restore a previously exported ZIP atomically, preserving the old DB as backup."""
    uploaded=request.files.get('archivo')
    if not uploaded or not uploaded.filename.lower().endswith('.zip'): return 'Elegí una exportación ZIP válida',400
    backup_db('antes-importacion')
    with tempfile.TemporaryDirectory() as td:
        uploaded.save(os.path.join(td,'import.zip'))
        with zipfile.ZipFile(os.path.join(td,'import.zip')) as z:
            names=z.namelist()
            if any(n.startswith('/') or '..' in n.split('/') for n in names): return 'El ZIP contiene rutas inseguras',400
            if 'base/catalogo.sqlite3' not in names: return 'El ZIP no contiene una base válida',400
            if 'manifest.json' in names:
                try: json.loads(z.read('manifest.json').decode('utf-8'))
                except Exception: return 'La copia tiene un manifiesto inválido',400
            z.extract('base/catalogo.sqlite3',td)
            imported=os.path.join(td,'base','catalogo.sqlite3')
            test=sqlite3.connect(imported)
            integrity=test.execute('PRAGMA integrity_check').fetchone()[0]
            tables={r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            test.close()
            if integrity != 'ok' or not {'productos','catalogos'}.issubset(tables): return 'El ZIP no contiene una base de catálogo válida',400
            os.replace(imported,DB_PATH)
            os.makedirs(UPLOAD_FOLDER,exist_ok=True)
            for n in names:
                if n.startswith('imagenes/') and not n.endswith('/'):
                    target=os.path.join(UPLOAD_FOLDER,os.path.basename(n))
                    with z.open(n) as src, open(target,'wb') as dst: shutil.copyfileobj(src,dst)
    init_db(); cloud_sync(); log_change('importacion','*',uploaded.filename)
    return redirect(url_for('admin_index'))

@app.route('/admin/cargar-abigail',methods=['GET','POST'])
@login_required
def admin_cargar_abigail():
    slug='limpieza-abigail'
    manifest_path=os.path.join(BASE_DIR,'products.json')
    seed_zip=os.path.join(BASE_DIR,'abigail_seed.zip')
    if not os.path.exists(manifest_path) and not os.path.exists(seed_zip): return jsonify(ok=False,error='No está la planilla de Abigail'),500
    seed_archive=zipfile.ZipFile(seed_zip) if os.path.exists(seed_zip) else None
    if seed_archive:
        items=json.loads(seed_archive.read('products.json').decode('utf-8'))
    else:
        with open(manifest_path,encoding='utf-8') as fh: items=json.load(fh)
    backup_db('antes-carga-abigail')
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram,banner) VALUES(?,?,?,?,?,?,?)",(slug,'Artículos de limpieza Abigail','Limpieza del hogar','https://share.zapia.com/lw6ro8nz7tp7k487va08fu','5493874572787','LibreriaRuizSaltaBot','https://share.zapia.com/edtuh2ffu9fz19o7ulk70j'))
        db.execute("UPDATE catalogos SET nombre=?,subtitulo=?,logo=?,whatsapp=?,telegram=?,banner=?,activo=1 WHERE slug=?",('Artículos de limpieza Abigail','Limpieza del hogar','https://share.zapia.com/lw6ro8nz7tp7k487va08fu','5493874572787','LibreriaRuizSaltaBot','https://share.zapia.com/edtuh2ffu9fz19o7ulk70j',slug))
        count=0
        for item in items:
            src=os.path.join(BASE_DIR,item['image'])
            fname='abigail-'+item['code'].lower()+'.webp'; target=os.path.join(UPLOAD_FOLDER,fname)
            if not os.path.exists(src) and not seed_archive: continue
            if not os.path.exists(target):
                raw=seed_archive.read(item['image']) if seed_archive else None
                source=io.BytesIO(raw) if raw else src
                with Image.open(source) as im:
                    im=ImageOps.exif_transpose(im); im.thumbnail((1600,1600))
                    if im.mode not in ('RGB','RGBA'): im=im.convert('RGB')
                    im.save(target,'WEBP',quality=82,method=6)
            cat='papel higienicos' if item['code'].startswith('AB-PH') else 'rollo de cocina'
            db.execute("INSERT OR IGNORE INTO productos(codigo,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor,nivel_precio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(item['code'],item['name'],item['description'],item['price'],cat,item['brand'],fname,1,1,slug,0,0,0,'','Estándar'))
            db.execute("UPDATE productos SET nombre=?,desc_=?,precio=?,categoria=?,marca=?,foto=?,activo=1,catalogo_slug=? WHERE codigo=?",(item['name'],item['description'],item['price'],cat,item['brand'],fname,slug,item['code']))
            count+=1
        db.commit()
    if seed_archive: seed_archive.close()
    cloud_sync(); log_change('carga-abigail',slug,str(count)); session['catalogo_slug']=slug
    return jsonify(ok=True,catalogo=slug,productos=count)

@app.route('/admin/prueba-foto',methods=['GET','POST'])
@login_required
def admin_prueba_foto():
    """Temporary controlled image-persistence test; creates a small WebP in DATA_DIR."""
    path=os.path.join(UPLOAD_FOLDER,'prueba-persistencia.webp')
    im=Image.new('RGB',(900,700),'#25D366')
    from PIL import ImageDraw
    draw=ImageDraw.Draw(im)
    draw.rounded_rectangle((60,60,840,640),radius=36,fill='white',outline='#128C7E',width=10)
    draw.text((210,260),'PRUEBA FOTO',fill='#128C7E')
    draw.text((210,350),'PERSISTENCIA OK',fill='#555')
    im.save(path,'WEBP',quality=82,method=6)
    with get_db() as db:
        row=db.execute('SELECT id FROM productos WHERE catalogo_slug=? ORDER BY id LIMIT 1',(current_slug(),)).fetchone()
        if not row: return jsonify(ok=False,error='No hay producto de prueba'),400
        db.execute('UPDATE productos SET foto=? WHERE id=?',('prueba-persistencia.webp',row['id'])); db.commit()
    cloud_sync(); log_change('foto-prueba',current_slug(),'prueba-persistencia.webp')
    return jsonify(ok=True,foto='prueba-persistencia.webp')

@app.route('/admin/producto/<int:pid>/foto',methods=['POST'])
@login_required
def admin_foto_rapida(pid):
    backup_db('antes-foto')
    file=request.files.get('foto')
    if not file or not file.filename: return jsonify(ok=False,error='No se recibio archivo')
    if not allowed_file(file.filename): return jsonify(ok=False,error='Formato no permitido')
    try: fname=save_optimized_image(file)
    except ValueError as exc: return jsonify(ok=False,error=str(exc)),400
    with get_db() as db: db.execute('UPDATE productos SET foto=? WHERE id=?',(fname,pid)); db.commit()
    cloud_sync()
    log_change('foto',current_slug(),str(pid))
    return jsonify(ok=True,foto=fname)
# Restore the cloud snapshot at startup, but never upload at startup.
# Uploading here could overwrite a valid catalog while Render is starting.
restore_from_cloud()
init_db()
restore_visit_counter()
restore_vivero_visit_counter()
restore_rena_visit_counter()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
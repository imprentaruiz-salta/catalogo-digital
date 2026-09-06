import os, sqlite3, uuid, io, json, zipfile, tempfile, shutil, unicodedata, hashlib, urllib.request, urllib.error, urllib.parse
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
os.makedirs(UPLOAD_FOLDER,exist_ok=True); os.makedirs(BACKUP_FOLDER,exist_ok=True); os.makedirs(VIDEO_FOLDER,exist_ok=True)
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
 "bebidas":"🥤","panales":"👶","comestibles":"🥫","golosinas":"🍬","limpieza":"🧼","verduleria":"🥬","lacteos":"🥛","libreria":"📚","fotos":"📷","fotografia":"📷","carniceria":"🥩","panaderia":"🍞","ferreteria":"🔧","farmacia":"💊","papel higienico":"🧻","papel higienicos":"🧻","escobas":"🧹","escoba":"🧹","dentifricos":"🪥","dentifrico":"🪥","pasta dental":"🪥","pastas dentales":"🪥","jabones":"🧼","jabon":"🧼","shampoo":"🧴","desodorantes":"🧴","cuadernos":"📒","lapices":"✏️","biromes":"🖊️","cartucheras":"🎒","utiles escolares":"✏️","impresiones":"🖨️"}
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
    if code in {'AB-PH-016','AB-PH-017','AB-PH-018','AB-PN-008'}: return 'Precio por fardo'
    if code == 'AB-PN-001': return 'Pack $2.100 · fardo x10 $20.000'
    if 'fardo' in desc.lower(): return 'Pack + precio de fardo en detalle'
    if 'pack' in desc.lower(): return 'Precio por pack'
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
        db.execute("CREATE TABLE IF NOT EXISTS catalogos (slug TEXT PRIMARY KEY,nombre TEXT NOT NULL,subtitulo TEXT DEFAULT 'Útiles · Fotos · Impresiones',logo TEXT DEFAULT '',whatsapp TEXT DEFAULT '5493872101274',telegram TEXT DEFAULT '',banner TEXT DEFAULT '',activo INTEGER DEFAULT 1)")
        db.execute("CREATE TABLE IF NOT EXISTS fleming_videos (property_id TEXT PRIMARY KEY, filename TEXT NOT NULL, title TEXT DEFAULT '', uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS cambios (id INTEGER PRIMARY KEY AUTOINCREMENT, creado TEXT DEFAULT CURRENT_TIMESTAMP, tipo TEXT NOT NULL, catalogo_slug TEXT, detalle TEXT DEFAULT '')")
        db.execute("CREATE TABLE IF NOT EXISTS fleming_analytics (id INTEGER PRIMARY KEY AUTOINCREMENT, creado TEXT DEFAULT CURRENT_TIMESTAMP, session_id TEXT DEFAULT '', evento TEXT NOT NULL, property_id TEXT DEFAULT '', pagina TEXT DEFAULT '/fleming', meta TEXT DEFAULT '')")
        db.execute("CREATE INDEX IF NOT EXISTS idx_fleming_analytics_creado ON fleming_analytics(creado)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_fleming_analytics_evento ON fleming_analytics(evento)")
        db.execute("ALTER TABLE catalogos ADD COLUMN telegram TEXT DEFAULT ''") if 'telegram' not in [r['name'] for r in db.execute('PRAGMA table_info(catalogos)').fetchall()] else None
        db.execute("ALTER TABLE catalogos ADD COLUMN banner TEXT DEFAULT ''") if 'banner' not in [r['name'] for r in db.execute('PRAGMA table_info(catalogos)').fetchall()] else None
        db.execute("INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram,banner) VALUES(?,?,?,?,?,?,?)",('libreria-ruiz','Librería Ruiz','Útiles · Fotos · Impresiones','https://share.zapia.com/lw6ro8nz7tp7k487va08fu','5493872101274','LibreriaRuizSaltaBot',''))
        db.execute("UPDATE productos SET catalogo_slug='libreria-ruiz' WHERE catalogo_slug IS NULL OR catalogo_slug=''")
        db.execute("UPDATE catalogos SET banner=? WHERE slug='limpieza-abigail' AND (banner IS NULL OR banner='')",('https://share.zapia.com/edtuh2ffu9fz19o7ulk70j',))
        db.execute("""CREATE TABLE IF NOT EXISTS sugerencias (id INTEGER PRIMARY KEY AUTOINCREMENT,catalogo_slug TEXT NOT NULL,producto TEXT NOT NULL,nombre TEXT DEFAULT '',cantidad INTEGER DEFAULT 1,cantidad_necesita INTEGER DEFAULT 1,comentario TEXT DEFAULT '',estado TEXT DEFAULT 'pendiente',creado TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for col,definition in [('cantidad_necesita','INTEGER DEFAULT 1'),('comentario',"TEXT DEFAULT ''"),('estado',"TEXT DEFAULT 'pendiente'")]:
            try: db.execute(f"ALTER TABLE sugerencias ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError: pass
        # Keep the pre-existing product seed; the admin can replace it.
        db.commit()

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
    return 'La imagen es demasiado grande. El límite es de 12 MB antes de comprimirla.',400
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
    prioridad=['Libreria','Librería','Fotos','Fotografía','Papeleria','Papelería','Impresiones']; orden={x:i for i,x in enumerate(prioridad)}
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
    return {'destacados':featured,'novedades':newest,'ofertas':offers,'recomendaciones':unique(recommendations),'pedidos':unique(requested),'total':len(rows)}
def current_slug(): return session.get('catalogo_slug','libreria-ruiz')
def current_config(): return get_catalogo_config(current_slug()) or get_catalogo_config('libreria-ruiz')

@app.route('/manifest.json')
def web_manifest():
    slug=request.args.get('catalogo','libreria-ruiz').strip().lower()
    cfg=get_catalogo_config(slug)
    if not cfg:
        cfg=get_catalogo_config('libreria-ruiz')
    # Keep the install target tied to the catalog URL that the visitor opened.
    body=render_template('manifest.json',catalogo=cfg,manifest_slug=slug)
    return app.response_class(body,mimetype='application/manifest+json')

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
    if filename.startswith('banner_') or filename.startswith('social_preview_') or filename.startswith('category_'):
        return send_from_directory(BASE_DIR, filename)
    return ('',404)

@app.route('/menu')
def menu_digital():
    return render_template('menu.html')

def _video_extension(filename):
    return filename.rsplit('.',1)[1].lower() if '.' in filename else ''

def _valid_property_id(value):
    value=(value or '').strip().lower()
    return value if value.startswith('p') and value[1:].isdigit() and 1 <= int(value[1:]) <= 999 else ''

@app.route('/fleming/video-list.json')
def fleming_video_list():
    with get_db() as db:
        rows=db.execute('SELECT property_id,filename,title FROM fleming_videos ORDER BY property_id').fetchall()
    return jsonify({r['property_id']:{'url':url_for('fleming_video_file',filename=r['filename']),'title':r['title']} for r in rows})

@app.route('/fleming/videos/<path:filename>')
def fleming_video_file(filename):
    return send_from_directory(VIDEO_FOLDER, filename, conditional=True)

@app.route('/fleming/preview/<int:number>.jpg')
def fleming_property_preview(number):
    if number < 1 or number > 999:
        return ('',404)
    import re, base64, textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    template=render_template('fleming.html')
    chosen=None
    for match in re.finditer(r'<article class=\"property-card\b[^>]*>[\s\S]*?</article>',template):
        block=match.group(0)
        lm=re.search(r'<div class=\"card-label\">([\s\S]*?)</div>',block)
        if not lm: continue
        label=re.sub(r'<[^>]+>','',lm.group(1)).strip()
        prefix=re.match(r'0?([0-9]+)\s*[·.]',label)
        if prefix and int(prefix.group(1))==number:
            hm=re.search(r'<h2>([\s\S]*?)</h2>',block)
            title=re.sub(r'<[^>]+>','',hm.group(1)).strip() if hm else label
            im=re.search(r'<img[^>]+src=\"data:image/[^;]+;base64,([^\"]+)',block)
            chosen=(label,title,im.group(1) if im else '')
            break
    if not chosen: return ('',404)
    label,title,encoded=chosen
    canvas=Image.new('RGB',(1080,1080),'#f8fbf9')
    if encoded:
        try:
            photo=Image.open(io.BytesIO(base64.b64decode(encoded))).convert('RGB')
            photo=ImageOps.fit(photo,(1080,650),method=Image.Resampling.LANCZOS)
            canvas.paste(photo,(0,0))
        except Exception:
            pass
    draw=ImageDraw.Draw(canvas)
    bold=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',42)
    title_font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',43)
    small=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',27)
    draw.rectangle((0,650,1080,1080),fill='#3e5a54')
    draw.text((58,695),f'PROPIEDAD {number:02d}',font=bold,fill='#d9c18a')
    lines=textwrap.wrap(title,width=31)[:3]
    draw.multiline_text((58,765),'\n'.join(lines),font=title_font,fill='white',spacing=8)
    draw.text((58,965),'Inmobiliaria Fleming & Asociados · Salta',font=small,fill='#e8eee9')
    out=io.BytesIO(); canvas.save(out,format='JPEG',quality=88,optimize=True); out.seek(0)
    response=send_file(out,mimetype='image/jpeg',max_age=0,download_name=f'fleming-propiedad-{number}.jpg')
    response.headers['Cache-Control']='no-cache, max-age=0'
    return response

def _render_fleming_page(selected=''):
    html=render_template('fleming.html')
    selected=(selected or '').strip()
    if selected.isdigit():
        number=int(selected)
        import re, html as html_module
        chosen=None
        for match in re.finditer(r'<article class="property-card\b[^>]*>[\s\S]*?</article>',html):
            block=match.group(0)
            label_match=re.search(r'<div class="card-label">([\s\S]*?)</div>',block)
            if not label_match: continue
            label=re.sub(r'<[^>]+>','',label_match.group(1)).strip()
            prefix=re.match(r'0?([0-9]+)\s*[·.]',label)
            if prefix and int(prefix.group(1))==number:
                title_match=re.search(r'<h2>([\s\S]*?)</h2>',block)
                title=re.sub(r'<[^>]+>','',title_match.group(1)).strip() if title_match else label
                chosen=(label,title,block); break
        if chosen:
            label,title,chosen_block=chosen
            # Shared pages are server-rendered with one card only; this remains correct even without JavaScript.
            html=re.sub(r'(<section class="catalog-grid" id="venta">)[\s\S]*?(</section>)',r'\1'+chosen_block+r'\2',html,count=1)
            safe_title=html_module.escape(f'{title} · Inmobiliaria Fleming & Asociados',quote=True)
            safe_desc=html_module.escape(f'Conocé esta propiedad: {label}. Consultá fotos, descripción, precio y ubicación.',quote=True)
            preview_token=re.sub(r'[^A-Za-z0-9_-]','',request.args.get('preview','4'))[:40] or '4'
            share_url=html_module.escape(f'https://catalogo-app-zm3w.onrender.com/fleming/inmueble/propiedad-{number}?preview={preview_token}',quote=True)
            preview_image=html_module.escape(f'https://catalogo-app-zm3w.onrender.com/fleming/preview/{number}.jpg?v={preview_token}',quote=True)
            html=re.sub(r'(<title>)[\s\S]*?(</title>)',r'\1'+safe_title+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+name="description")',r'\1'+safe_desc+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+property="og:title")',r'\1'+safe_title+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+property="og:description")',r'\1'+safe_desc+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+property="og:url")',r'\1'+share_url+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+property="og:image")',r'\1'+preview_image+r'\2',html,count=1)
            html=re.sub(r'(<meta\s+content=")[^"]*("\s+name="twitter:image")',r'\1'+preview_image+r'\2',html,count=1)
    return html


FLEMING_DEMO_TOKEN='fleming-victoria-2-demo-9c7f4e'

@app.route('/fleming/demo/<token>')
def fleming_demo_victoria(token):
    # Demo unlinked: intentionally separate from the public catalogue.
    if token != FLEMING_DEMO_TOKEN:
        from flask import abort
        abort(404)
    html=render_template('fleming.html')
    demo_css='''<style id="victoria-2-demo-css">
.v2-launch{position:fixed;right:18px;bottom:118px;z-index:9998;border:0;border-radius:999px;padding:12px 16px;background:linear-gradient(135deg,#0f6b55,#2f9b72);color:#fff;box-shadow:0 10px 28px #0d4d3c44;font:800 13px Arial;cursor:pointer}.v2-launch small{display:block;font-size:9px;opacity:.8;margin-top:2px}.v2-panel{position:fixed;right:18px;bottom:174px;width:min(360px,calc(100vw - 28px));height:min(610px,calc(100vh - 205px));z-index:9999;display:none;flex-direction:column;overflow:hidden;border:1px solid #cde4d6;border-radius:22px;background:#fbfffc;box-shadow:0 18px 60px #184a3740;font:14px Arial;color:#183d35}.v2-panel.open{display:flex}.v2-head{padding:16px 17px 13px;background:linear-gradient(135deg,#0d5949,#2d9471);color:white}.v2-head-row{display:flex;justify-content:space-between;align-items:flex-start}.v2-head strong{font-size:17px}.v2-head span{display:block;margin-top:4px;font-size:11px;opacity:.84}.v2-close{border:0;background:#ffffff22;color:white;border-radius:8px;font-size:20px;line-height:1;width:30px;height:30px;cursor:pointer}.v2-demo-badge{display:inline-flex;margin-top:10px;padding:4px 8px;border-radius:999px;background:#ffffff1f;border:1px solid #ffffff55;font-size:10px;font-weight:700}.v2-body{flex:1;overflow:auto;padding:13px;background:linear-gradient(#f8fffa,#fff)}.v2-welcome{padding:12px;border-radius:14px;background:#eaf7ef;border:1px solid #d0eadd;line-height:1.45;color:#315b4d}.v2-suggestions{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.v2-suggestions button{border:1px solid #b9dbca;border-radius:999px;padding:7px 9px;background:#fff;color:#236249;font:700 11px Arial;cursor:pointer}.v2-msg{max-width:91%;margin:9px 0;padding:10px 11px;border-radius:13px;line-height:1.4;white-space:pre-line}.v2-msg.user{margin-left:auto;background:#d9efe2;color:#245742}.v2-msg.bot{background:#f0f5f2;border:1px solid #dfebe3}.v2-result{margin-top:8px;padding:9px;border-radius:11px;background:#fff;border:1px solid #dbe9df}.v2-result strong{display:block;color:#174b3c}.v2-result small{display:block;margin-top:3px;color:#64746e}.v2-result button{margin-top:7px;border:0;border-radius:7px;padding:6px 8px;background:#e4f4ea;color:#226348;font:700 10px Arial;cursor:pointer}.v2-foot{display:flex;gap:7px;padding:11px;border-top:1px solid #e2eee7;background:#fff}.v2-input{flex:1;min-width:0;border:1px solid #c8ddd0;border-radius:10px;padding:10px;font:13px Arial;outline:0}.v2-send{border:0;border-radius:10px;padding:0 13px;background:#1e805d;color:#fff;font:800 12px Arial;cursor:pointer}@media(max-width:560px){.v2-launch{right:12px;bottom:94px}.v2-panel{right:10px;bottom:10px;width:calc(100vw - 20px);height:min(650px,calc(100vh - 20px));border-radius:18px}}
</style>'''
    props=[
      {'id':1,'type':'Departamento','zone':'Zona Shopping','price':'USD 68.500','text':'1 dormitorio, cochera y apto crédito.'},
      {'id':2,'type':'Departamento','zone':'Barrio Bancario','price':'USD 38.000','text':'3 dormitorios, living comedor y cochera cerrada.'},
      {'id':3,'type':'Departamento','zone':'20 de Febrero al 1400','price':'USD 95.000','text':'Piscina, balcón con asador, SUM, gimnasio y coworking.'},
      {'id':4,'type':'Departamento','zone':'Centro','price':'USD 88.000','text':'2 dormitorios, balcón, cochera y lavadero.'},
      {'id':5,'type':'Casa','zone':'Centro','price':'USD 111.000','text':'6 dormitorios, 3 plantas, terraza y estacionamiento para 10 vehículos.'},
      {'id':6,'type':'Departamento','zone':'Monoblock Salta','price':'USD 90.000','text':'3 dormitorios, 2 baños, toilette y cochera.'},
      {'id':7,'type':'Casa','zone':'Barrio Norte Grande','price':'$55.000.000','text':'4 dormitorios, garage, patio con asador y cuarto de servicio.'},
      {'id':8,'type':'Terreno','zone':'Vaqueros','price':'USD 30.000','text':'20 × 50 metros, superficie total de 1.000 m².'},
      {'id':9,'type':'Casa','zone':'Tres Cerritos','price':'USD 160.000','text':'3 dormitorios, jardín, patio, garage y asador.'},
      {'id':10,'type':'Casa','zone':'Tres Cerritos · primera rotonda','price':'USD 180.000','text':'3 dormitorios, galería, patio y estacionamiento.'},
      {'id':11,'type':'Departamento en alquiler','zone':'Centro · Deán Funes 330','price':'$600.000/mes','text':'2 dormitorios, patio chico y lavadero. Expensas: $170.000.'},
      {'id':12,'type':'Casa','zone':'Villa Las Rosas','price':'USD 70.000','text':'3 dormitorios, patio y estacionamiento.'}
    ]
    import json as _json
    demo_js=r'''<script id="victoria-2-demo-js">(function(){var P=__PROPS__;var launch=document.createElement('button');launch.className='v2-launch';launch.innerHTML='✨ Probar Victoria 2.0<small>demo privada · no publicada</small>';var panel=document.createElement('section');panel.className='v2-panel';panel.innerHTML='<div class="v2-head"><div class="v2-head-row"><div><strong>Victoria 2.0</strong><span>Asesora virtual de Fleming</span></div><button class="v2-close" aria-label="Cerrar">×</button></div><div class="v2-demo-badge">● DEMO PRIVADA · no afecta el catálogo</div></div><div class="v2-body"><div class="v2-welcome">Hola, soy Victoria. Probame con una consulta como <b>“casas en Tres Cerritos”</b>, <b>“departamentos hasta USD 90.000”</b> o <b>“propiedad 8”</b>.</div><div class="v2-suggestions"><button>Casas en Tres Cerritos</button><button>Hasta USD 90.000</button><button>Propiedad 8</button></div><div class="v2-chat"></div></div><form class="v2-foot"><input class="v2-input" autocomplete="off" placeholder="Escribí tu consulta…"><button class="v2-send">Enviar</button></form>';document.body.appendChild(launch);document.body.appendChild(panel);var body=panel.querySelector('.v2-body'),chat=panel.querySelector('.v2-chat'),input=panel.querySelector('.v2-input');function norm(s){return (s||'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'')}function add(text,who){var d=document.createElement('div');d.className='v2-msg '+who;d.textContent=text;chat.appendChild(d);body.scrollTop=body.scrollHeight}function result(q){var n=norm(q),m=P.slice();var num=n.match(/(?:propiedad|numero|nro|n°|\b)(?:\s*)(\d{1,2})\b/);if(num){m=m.filter(function(x){return x.id===parseInt(num[1],10)})}else{if(n.includes('alquil'))m=m.filter(function(x){return x.id===11)}if(n.includes('casa'))m=m.filter(function(x){return x.type==='Casa')}if(n.includes('departamento'))m=m.filter(function(x){return x.type.indexOf('Departamento')===0)}if(n.includes('terreno'))m=m.filter(function(x){return x.type==='Terreno')}['shopping','bancario','centro','monoblock','vaqueros','tres cerritos','norte grande','villa las rosas'].forEach(function(z){if(n.includes(z))m=m.filter(function(x){return norm(x.zone).includes(z)})});var max=n.match(/(?:hasta|maximo|max|menos de)\s*(?:usd|u\$s|dolares|\$)?\s*([0-9][0-9\.\,]*)/);if(max){var v=parseInt(max[1].replace(/[\.\,]/g,''),10);m=m.filter(function(x){var z=norm(x.price).replace(/[^0-9]/g,'');return x.price.indexOf('USD')>=0&&parseInt(z,10)<=v})}}return m}function answer(q){var m=result(q);add(q,'user');var b=document.createElement('div');b.className='v2-msg bot';if(!m.length){b.textContent='No encontré una coincidencia exacta. Puedo buscar por número, tipo, barrio, precio u operación.'}else{b.innerHTML='Encontré '+m.length+' opción'+(m.length===1?'':'es')+':';m.slice(0,6).forEach(function(x){var r=document.createElement('div');r.className='v2-result';r.innerHTML='<strong>Propiedad '+String(x.id).padStart(2,'0')+' · '+x.type+'</strong><small>'+x.zone+' · '+x.price+'<br>'+x.text+'</small><button data-id="'+x.id+'">Ver ficha en el catálogo</button>';b.appendChild(r)})}chat.appendChild(b);body.scrollTop=body.scrollHeight}function open(){panel.classList.add('open');input.focus()}launch.addEventListener('click',open);panel.querySelector('.v2-close').addEventListener('click',function(){panel.classList.remove('open')});panel.querySelectorAll('.v2-suggestions button').forEach(function(x){x.addEventListener('click',function(){answer(x.textContent)})});panel.querySelector('.v2-foot').addEventListener('submit',function(e){e.preventDefault();var q=input.value.trim();if(q){answer(q);input.value=''}});chat.addEventListener('click',function(e){var btn=e.target.closest('button[data-id]');if(!btn)return;panel.classList.remove('open');var card=document.querySelector('[data-property="p'+btn.dataset.id+'"]');if(card)card.scrollIntoView({behavior:'smooth',block:'center'})});})();</script>'''.replace('__PROPS__',_json.dumps(props,ensure_ascii=False))
    html=html.replace('</head>',demo_css+'</head>',1)
    html=html.replace('</body>',demo_js+'</body>',1)
    return html

@app.route('/fleming')
@app.route('/fleming/')
def fleming_brochure():
    return _render_fleming_page(request.args.get('ubicacion',''))

@app.route('/fleming/inmueble/<slug>')
def fleming_property_page(slug):
    import re
    match=re.search(r'(?:propiedad-|p)([0-9]+)',(slug or '').lower())
    return _render_fleming_page(match.group(1) if match else '')

@app.route('/fleming/cargar')
@app.route('/fleming/cargar/')
def fleming_cargar():
    return render_template('fleming-cargar.html')

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

@app.route('/')
def index():
    cfg=current_config(); return render_template('index.html',cats=get_catalogo(current_slug()),showcase=get_showcase(current_slug()),catalogo=cfg)
@app.route('/c/<slug>')
def catalogo_publico(slug):
    cfg=get_catalogo_config(slug)
    if not cfg: return redirect(url_for('index'))
    if slug == 'pizzeria-demo':
        grouped=get_catalogo(slug)
        pizzas=[p for brands in grouped.values() for products in brands.values() for p in products]
        return render_template('pizzeria.html',cats=pizzas,catalogo=cfg)
    return render_template('index.html',cats=get_catalogo(slug),showcase=get_showcase(slug),catalogo=cfg)
@app.route('/api/fleming/analytics', methods=['POST'])
def fleming_analytics_event():
    """Store anonymous interaction events for the Fleming catalog."""
    data=request.get_json(silent=True) or {}
    allowed={'page_view','property_view','assistant_open','whatsapp_click','telegram_click','map_open','facade_open','video_open','email_click','assistant_chat_open'}
    evento=str(data.get('evento') or '').strip()[:40]
    if evento not in allowed:
        return jsonify(ok=False),400
    session_id=str(data.get('session_id') or '').strip()[:80]
    property_id=str(data.get('property_id') or '').strip()[:20]
    pagina=str(data.get('pagina') or '/fleming').strip()[:160]
    meta=str(data.get('meta') or '').strip()[:240]
    with get_db() as db:
        db.execute('INSERT INTO fleming_analytics(session_id,evento,property_id,pagina,meta) VALUES(?,?,?,?,?)',(session_id,evento,property_id,pagina,meta))
        db.commit()
    return jsonify(ok=True)

def _fleming_analytics_summary(days=1):
    days=max(1,min(90,int(days or 1)))
    with get_db() as db:
        totals=db.execute("SELECT evento,COUNT(*) AS cantidad FROM fleming_analytics WHERE session_id!='verify-session' AND creado >= datetime('now', ?) GROUP BY evento ORDER BY cantidad DESC",(f'-{days} day',)).fetchall()
        props=db.execute("SELECT property_id,COUNT(*) AS cantidad FROM fleming_analytics WHERE session_id!='verify-session' AND evento='property_view' AND property_id!='' AND creado >= datetime('now', ?) GROUP BY property_id ORDER BY cantidad DESC LIMIT 12",(f'-{days} day',)).fetchall()
        visitors=db.execute("SELECT COUNT(DISTINCT session_id) AS cantidad FROM fleming_analytics WHERE session_id!='' AND session_id!='verify-session' AND creado >= datetime('now', ?)",(f'-{days} day',)).fetchone()['cantidad']
    return {'days':days,'visitors':visitors,'events':{r['evento']:r['cantidad'] for r in totals},'top_properties':[dict(r) for r in props]}

@app.route('/api/fleming/analytics/summary')
def fleming_analytics_summary_api():
    """Aggregate-only endpoint used by the owner's daily briefing; no raw data."""
    try: days=int(request.args.get('days','1'))
    except ValueError: days=1
    return jsonify(_fleming_analytics_summary(days))

@app.route('/fleming/estadisticas')
@login_required
def fleming_analytics_dashboard():
    summary=_fleming_analytics_summary(7)
    with get_db() as db:
        daily=db.execute("SELECT date(creado) AS dia, COUNT(*) AS eventos, COUNT(DISTINCT session_id) AS visitantes FROM fleming_analytics WHERE session_id!='verify-session' AND creado >= datetime('now','-30 day') GROUP BY date(creado) ORDER BY dia DESC").fetchall()
    return render_template('fleming_analytics.html',summary=summary,daily=[dict(r) for r in daily])

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

@app.route('/fleming/admin/videos',methods=['GET','POST'])
@login_required
def fleming_video_admin():
    error=None
    if request.method=='POST':
        property_id=_valid_property_id(request.form.get('property_id'))
        uploaded=request.files.get('video')
        title=(request.form.get('title') or '').strip()[:120]
        if not property_id: error='Elegí una propiedad válida, por ejemplo p11.'
        elif not uploaded or not uploaded.filename: error='Seleccioná un video.'
        elif _video_extension(uploaded.filename) not in ALLOWED_VIDEO_EXT: error='Usá MP4, WEBM, MOV o M4V.'
        else:
            raw=uploaded.read(MAX_VIDEO_BYTES+1)
            if len(raw)>MAX_VIDEO_BYTES: error='El video supera el límite de 80 MB.'
            else:
                ext=_video_extension(uploaded.filename); filename=f'{property_id}-{uuid.uuid4().hex}.{ext}'
                target=os.path.join(VIDEO_FOLDER,filename)
                with open(target,'wb') as out: out.write(raw)
                backup_db('antes-video-fleming')
                with get_db() as db:
                    old=db.execute('SELECT filename FROM fleming_videos WHERE property_id=?',(property_id,)).fetchone()
                    db.execute('INSERT INTO fleming_videos(property_id,filename,title,uploaded_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(property_id) DO UPDATE SET filename=excluded.filename,title=excluded.title,uploaded_at=CURRENT_TIMESTAMP',(property_id,filename,title))
                    db.commit()
                if old and old['filename'] != filename:
                    try: os.remove(os.path.join(VIDEO_FOLDER,old['filename']))
                    except OSError: pass
                cloud_sync(); log_change('video-fleming',property_id,filename)
                flash(f'✅ Video guardado en la propiedad {property_id[1:]}')
                return redirect(url_for('fleming_video_admin'))
    with get_db() as db: videos=[dict(r) for r in db.execute('SELECT * FROM fleming_videos ORDER BY property_id').fetchall()]
    return render_template('fleming-videos.html',videos=videos,error=error)

@app.route('/fleming/admin/videos/<property_id>/eliminar',methods=['POST'])
@login_required
def fleming_video_delete(property_id):
    property_id=_valid_property_id(property_id)
    if property_id:
        with get_db() as db:
            row=db.execute('SELECT filename FROM fleming_videos WHERE property_id=?',(property_id,)).fetchone()
            db.execute('DELETE FROM fleming_videos WHERE property_id=?',(property_id,)); db.commit()
        if row:
            try: os.remove(os.path.join(VIDEO_FOLDER,row['filename']))
            except OSError: pass
        cloud_sync(); log_change('eliminar-video-fleming',property_id,'')
    return redirect(url_for('fleming_video_admin'))

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
    return render_template('admin.html',productos=[dict(p) for p in productos],catalogos=[dict(c) for c in catalogos],catalogo=current_config(),sugerencias=[dict(s) for s in sugerencias])
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
    f=request.form; cat=f.get('categoria',''); nivel_precio=f.get('nivel_precio','Estándar').strip() or 'Estándar'; cat=f.get('nueva_categoria','').strip() if cat=='__nueva__' else cat; codigo=f.get('codigo','').strip().upper(); nombre=f.get('nombre','').strip(); desc_=f.get('descripcion','').strip(); precio=float(f.get('precio',0) or 0); marca=f.get('marca','').strip(); activo=1 if f.get('activo') else 0; stock=1 if f.get('stock') else 0
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
        if file and file.filename:
            try: foto_name=save_optimized_image(file)
            except ValueError as exc:
                prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() if pid else None
                return render_template('producto_form.html',producto=dict(prod) if prod else None,categorias=get_categorias(),catalogo=current_config(),error=str(exc))
        if pid:
            existing=db.execute('SELECT foto FROM productos WHERE id=?',(pid,)).fetchone(); foto_name=foto_name or (existing['foto'] if existing else '')
            db.execute('UPDATE productos SET codigo=?,nombre=?,desc_=?,precio=?,categoria=?,marca=?,foto=?,activo=?,stock=?,catalogo_slug=?,stock_actual=?,stock_minimo=?,costo=?,proveedor=?,nivel_precio=? WHERE id=?',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio,pid))
        else: db.execute('INSERT INTO productos(codigo,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor,nivel_precio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio))
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
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

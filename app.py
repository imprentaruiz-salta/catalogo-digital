import os, sqlite3, uuid, io, json, zipfile, tempfile, shutil, unicodedata, hashlib, urllib.request, urllib.error, urllib.parse
from datetime import datetime
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory, send_file, flash)
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__, static_folder=None, template_folder=".")
app.secret_key = "catalogo_ruiz_2026_secret_x7k"
app.config['MAX_CONTENT_LENGTH']=12*1024*1024
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
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        z.write(DB_PATH,"base/catalogo.sqlite3")
        for root,_,files in os.walk(UPLOAD_FOLDER):
            for name in files:
                z.write(os.path.join(root,name),os.path.join("imagenes",name))
    try:
        with cloud_request(SUPABASE_SNAPSHOT,"POST",mem.getvalue(),"application/zip") as response:
            response.read()
        return True
    except Exception:
        app.logger.exception("No se pudo guardar la copia persistente")
        return False

def restore_from_cloud():
    """Restore the last snapshot before database initialization on a new container."""
    if not cloud_enabled(): return False
    try:
        with cloud_request(SUPABASE_SNAPSHOT,"GET") as response:
            raw=response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names=z.namelist()
            if "base/catalogo.sqlite3" not in names: return False
            z.extract("base/catalogo.sqlite3",DATA_DIR)
            restored=os.path.join(DATA_DIR,"base","catalogo.sqlite3")
            if os.path.exists(DB_PATH): os.replace(restored,DB_PATH)
            else: os.replace(restored,DB_PATH)
            shutil.rmtree(os.path.join(DATA_DIR,"base"),ignore_errors=True)
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
 "bebidas":"🥤","panales":"👶","comestibles":"🥫","golosinas":"🍬","limpieza":"🧼","verduleria":"🥬","lacteos":"🥛","libreria":"📚","fotos":"📷","fotografia":"📷","carniceria":"🥩","panaderia":"🍞","ferreteria":"🔧","farmacia":"💊","papel higienico":"🧻","papel higienico":"🧻","escobas":"🧹","escoba":"🧹","dentifricos":"🪥","dentifrico":"🪥","pasta dental":"🪥","pastas dentales":"🪥","jabones":"🧼","jabon":"🧼","shampoo":"🧴","desodorantes":"🧴","cuadernos":"📒","lapices":"✏️","biromes":"🖊️","cartucheras":"🎒","utiles escolares":"✏️","impresiones":"🖨️"}
def _norm(s): return ''.join(c for c in unicodedata.normalize('NFD',str(s or '').lower()) if unicodedata.category(c)!='Mn')
def cat_icon(cat): return CAT_ICONS.get(_norm(cat),"📦")
app.jinja_env.globals['cat_icon']=cat_icon
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

@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    # Product photos live in DATA_DIR, not inside the deployable code folder.
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/')
def index():
    cfg=current_config(); return render_template('index.html',cats=get_catalogo(current_slug()),showcase=get_showcase(current_slug()),catalogo=cfg)
@app.route('/c/<slug>')
def catalogo_publico(slug):
    cfg=get_catalogo_config(slug)
    if not cfg: return redirect(url_for('index'))
    return render_template('index.html',cats=get_catalogo(slug),showcase=get_showcase(slug),catalogo=cfg)
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
            cat='papel higienico' if item['code'].startswith('AB-PH') else 'pañuelitos'
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
restore_from_cloud()
init_db()
cloud_sync()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

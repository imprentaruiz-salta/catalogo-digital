import os, sqlite3, uuid, io, json, zipfile, tempfile, shutil, unicodedata, hashlib
from datetime import datetime
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory, send_file, flash)
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=None, template_folder=".")
app.secret_key = "catalogo_ruiz_2026_secret_x7k"
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be mounted on a persistent disk in production. The application code
# and the data are deliberately kept separate so deploys never replace user data.
DATA_DIR=os.environ.get("CATALOGO_DATA_DIR",os.path.join(BASE_DIR,"data"))
os.makedirs(DATA_DIR,exist_ok=True)
DB_PATH=os.path.join(DATA_DIR,"catalogo.db")
UPLOAD_FOLDER=os.path.join(DATA_DIR,"uploads")
BACKUP_FOLDER=os.path.join(DATA_DIR,"backups")
os.makedirs(UPLOAD_FOLDER,exist_ok=True); os.makedirs(BACKUP_FOLDER,exist_ok=True)
# Migrate a legacy local database once, if this installation had one.
_OLD_DB=os.path.join(BASE_DIR,"catalogo.db")
if not os.path.exists(DB_PATH) and os.path.exists(_OLD_DB): shutil.copy2(_OLD_DB,DB_PATH)
ALLOWED_EXT={"png","jpg","jpeg","webp","gif"}
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
    return {"mode":"persistent" if configured and writable else "ephemeral", "data_dir":DATA_DIR, "database":os.path.exists(DB_PATH), "uploads":os.path.isdir(UPLOAD_FOLDER), "backups":os.path.isdir(BACKUP_FOLDER)}

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
def current_slug(): return session.get('catalogo_slug','libreria-ruiz')
def current_config(): return get_catalogo_config(current_slug()) or get_catalogo_config('libreria-ruiz')

@app.route('/static/uploads/<path:filename>')
def uploaded_file(filename):
    # Product photos live in DATA_DIR, not inside the deployable code folder.
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/')
def index():
    cfg=current_config(); return render_template('index.html',cats=get_catalogo(current_slug()),catalogo=cfg)
@app.route('/c/<slug>')
def catalogo_publico(slug):
    cfg=get_catalogo_config(slug)
    if not cfg: return redirect(url_for('index'))
    return render_template('index.html',cats=get_catalogo(slug),catalogo=cfg)
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
    if file and file.filename and allowed_file(file.filename): ext=file.filename.rsplit('.',1)[1].lower(); foto_name=f'{uuid.uuid4().hex}.{ext}'; file.save(os.path.join(UPLOAD_FOLDER,foto_name))
    with get_db() as db:
        duplicate=db.execute('SELECT id FROM productos WHERE codigo=? AND id!=?',(codigo,pid or 0)).fetchone()
        if duplicate:
            prod=db.execute('SELECT * FROM productos WHERE id=?',(pid,)).fetchone() if pid else None
            return render_template('producto_form.html',producto=dict(prod) if prod else None,categorias=get_categorias(),catalogo=current_config(),error=f'El código {codigo} ya existe. Elegí otro para no pisar productos.')
        if pid:
            existing=db.execute('SELECT foto FROM productos WHERE id=?',(pid,)).fetchone(); foto_name=foto_name or (existing['foto'] if existing else '')
            db.execute('UPDATE productos SET codigo=?,nombre=?,desc_=?,precio=?,categoria=?,marca=?,foto=?,activo=?,stock=?,catalogo_slug=?,stock_actual=?,stock_minimo=?,costo=?,proveedor=?,nivel_precio=? WHERE id=?',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio,pid))
        else: db.execute('INSERT INTO productos(codigo,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor,nivel_precio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,nivel_precio))
        db.commit()
    log_change('producto',current_slug(),('editar' if pid else 'crear')+' '+codigo)
    return redirect(url_for('admin_index'))
@app.route('/admin/producto/<int:pid>/eliminar',methods=['POST'])
@login_required
def admin_eliminar(pid):
    backup_db('antes-eliminar-producto')
    with get_db() as db: db.execute('UPDATE productos SET activo=0 WHERE id=?',(pid,)); db.commit()
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
    init_db(); log_change('importacion','*',uploaded.filename)
    return redirect(url_for('admin_index'))

@app.route('/admin/producto/<int:pid>/foto',methods=['POST'])
@login_required
def admin_foto_rapida(pid):
    backup_db('antes-foto')
    file=request.files.get('foto')
    if not file or not file.filename: return jsonify(ok=False,error='No se recibio archivo')
    if not allowed_file(file.filename): return jsonify(ok=False,error='Formato no permitido')
    ext=file.filename.rsplit('.',1)[1].lower(); fname=f'{uuid.uuid4().hex}.{ext}'; file.save(os.path.join(UPLOAD_FOLDER,fname))
    with get_db() as db: db.execute('UPDATE productos SET foto=? WHERE id=?',(fname,pid)); db.commit()
    log_change('foto',current_slug(),str(pid))
    return jsonify(ok=True,foto=fname)
init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

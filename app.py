import os, sqlite3, uuid
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory)
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder=".")
app.secret_key = "catalogo_ruiz_2026_secret_x7k"
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DB_PATH=os.path.join(BASE_DIR,"catalogo.db")
UPLOAD_FOLDER=os.path.join(BASE_DIR,"static","uploads")
ALLOWED_EXT={"png","jpg","jpeg","webp","gif"}
os.makedirs(UPLOAD_FOLDER,exist_ok=True)
ADMIN_USER="admin"; ADMIN_PASS="catalogo2026"
CAT_ICONS={"Bebidas":"🥤","Panales":"👶","Comestibles":"🥫","Golosinas":"🍬","Limpieza":"🧹","Verduleria":"🥬","Lacteos":"🥛","Libreria":"📚","Librería":"📚","Fotos":"📷","Fotografía":"📷","Carniceria":"🥩","Panaderia":"🍞","Ferreteria":"🔧","Farmacia":"💊"}
def cat_icon(cat): return CAT_ICONS.get(cat,"📦")
app.jinja_env.globals['cat_icon']=cat_icon
def get_db():
    db=sqlite3.connect(DB_PATH); db.row_factory=sqlite3.Row; return db

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS productos (id INTEGER PRIMARY KEY AUTOINCREMENT,codigo TEXT UNIQUE NOT NULL,nombre TEXT NOT NULL,desc_ TEXT DEFAULT '',precio REAL NOT NULL DEFAULT 0,categoria TEXT NOT NULL,marca TEXT NOT NULL,foto TEXT DEFAULT '',activo INTEGER DEFAULT 1,stock INTEGER DEFAULT 1)""")
        # Migrations are safe on the existing production database.
        for col,definition in [('catalogo_slug',"TEXT DEFAULT 'libreria-ruiz'"),('stock_actual','INTEGER DEFAULT 0'),('stock_minimo','INTEGER DEFAULT 0'),('costo','REAL DEFAULT 0'),('proveedor',"TEXT DEFAULT ''"),('ultima_reposicion',"TEXT DEFAULT ''")]:
            try: db.execute(f"ALTER TABLE productos ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError: pass
        db.execute("CREATE TABLE IF NOT EXISTS catalogos (slug TEXT PRIMARY KEY,nombre TEXT NOT NULL,subtitulo TEXT DEFAULT 'Útiles · Fotos · Impresiones',logo TEXT DEFAULT '',whatsapp TEXT DEFAULT '5493872101274',telegram TEXT DEFAULT 'imprentaruizsalta_bot',activo INTEGER DEFAULT 1)")
        db.execute("ALTER TABLE catalogos ADD COLUMN telegram TEXT DEFAULT 'imprentaruizsalta_bot'") if 'telegram' not in [r['name'] for r in db.execute('PRAGMA table_info(catalogos)').fetchall()] else None
        db.execute("INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram) VALUES(?,?,?,?,?,?)",('libreria-ruiz','Librería Ruiz','Útiles · Fotos · Impresiones','https://share.zapia.com/lw6ro8nz7tp7k487va08fu','5493872101274','imprentaruizsalta_bot'))
        db.execute("UPDATE productos SET catalogo_slug='libreria-ruiz' WHERE catalogo_slug IS NULL OR catalogo_slug=''")
        db.execute("""CREATE TABLE IF NOT EXISTS sugerencias (id INTEGER PRIMARY KEY AUTOINCREMENT,catalogo_slug TEXT NOT NULL,producto TEXT NOT NULL,nombre TEXT DEFAULT '',cantidad INTEGER DEFAULT 1,cantidad_necesita INTEGER DEFAULT 1,comentario TEXT DEFAULT '',estado TEXT DEFAULT 'pendiente',creado TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for col,definition in [('cantidad_necesita','INTEGER DEFAULT 1'),('comentario',"TEXT DEFAULT ''"),('estado',"TEXT DEFAULT 'pendiente'")]:
            try: db.execute(f"ALTER TABLE sugerencias ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError: pass
        # Keep the pre-existing product seed; the admin can replace it.
        db.commit()

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
@app.route('/admin/catalogo/nuevo',methods=['POST'])
@login_required
def nuevo_catalogo():
    slug=secure_filename(request.form.get('slug','')).lower().replace('_','-'); nombre=request.form.get('nombre','').strip()
    if slug and nombre:
        with get_db() as db: db.execute('INSERT OR IGNORE INTO catalogos(slug,nombre,subtitulo,logo,whatsapp,telegram) VALUES(?,?,?,?,?,?)',(slug,nombre,request.form.get('subtitulo',''),request.form.get('logo',''),request.form.get('whatsapp','5493872101274'),request.form.get('telegram','imprentaruizsalta_bot'))); db.commit()
        session['catalogo_slug']=slug
    return redirect(url_for('admin_index'))
def get_categorias():
    with get_db() as db: rows=db.execute('SELECT DISTINCT categoria FROM productos ORDER BY categoria').fetchall()
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
    f=request.form; cat=f.get('categoria',''); cat=f.get('nueva_categoria','').strip() if cat=='__nueva__' else cat; codigo=f.get('codigo','').strip().upper(); nombre=f.get('nombre','').strip(); desc_=f.get('descripcion','').strip(); precio=float(f.get('precio',0) or 0); marca=f.get('marca','').strip(); activo=1 if f.get('activo') else 0; stock=1 if f.get('stock') else 0
    try: stock_actual=max(0,int(f.get('stock_actual',0) or 0)); stock_minimo=max(0,int(f.get('stock_minimo',0) or 0)); costo=float(f.get('costo',0) or 0)
    except ValueError: stock_actual=stock_minimo=0; costo=0
    proveedor=f.get('proveedor','').strip(); foto_name=''; file=request.files.get('foto')
    if file and file.filename and allowed_file(file.filename): ext=file.filename.rsplit('.',1)[1].lower(); foto_name=f'{uuid.uuid4().hex}.{ext}'; file.save(os.path.join(UPLOAD_FOLDER,foto_name))
    with get_db() as db:
        if pid:
            existing=db.execute('SELECT foto FROM productos WHERE id=?',(pid,)).fetchone(); foto_name=foto_name or (existing['foto'] if existing else '')
            db.execute('UPDATE productos SET codigo=?,nombre=?,desc_=?,precio=?,categoria=?,marca=?,foto=?,activo=?,stock=?,catalogo_slug=?,stock_actual=?,stock_minimo=?,costo=?,proveedor=? WHERE id=?',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor,pid))
        else: db.execute('INSERT INTO productos(codigo,nombre,desc_,precio,categoria,marca,foto,activo,stock,catalogo_slug,stock_actual,stock_minimo,costo,proveedor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(codigo,nombre,desc_,precio,cat,marca,foto_name,activo,stock,current_slug(),stock_actual,stock_minimo,costo,proveedor))
        db.commit()
    return redirect(url_for('admin_index'))
@app.route('/admin/producto/<int:pid>/eliminar',methods=['POST'])
@login_required
def admin_eliminar(pid):
    with get_db() as db: db.execute('DELETE FROM productos WHERE id=?',(pid,)); db.commit()
    return redirect(url_for('admin_index'))
@app.route('/admin/producto/<int:pid>/foto',methods=['POST'])
@login_required
def admin_foto_rapida(pid):
    file=request.files.get('foto')
    if not file or not file.filename: return jsonify(ok=False,error='No se recibio archivo')
    if not allowed_file(file.filename): return jsonify(ok=False,error='Formato no permitido')
    ext=file.filename.rsplit('.',1)[1].lower(); fname=f'{uuid.uuid4().hex}.{ext}'; file.save(os.path.join(UPLOAD_FOLDER,fname))
    with get_db() as db: db.execute('UPDATE productos SET foto=? WHERE id=?',(fname,pid)); db.commit()
    return jsonify(ok=True,foto=fname)
init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)

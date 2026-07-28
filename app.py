import os, sqlite3, uuid
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory)
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder=".")
app.secret_key = "catalogo_ruiz_2026_secret_x7k"

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(BASE_DIR, "catalogo.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT   = {"png", "jpg", "jpeg", "webp", "gif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ADMIN_USER = "admin"
ADMIN_PASS = "catalogo2026"

CAT_ICONS = {
    "Bebidas":      "🥤",
    "Panales":      "👶",
    "Comestibles":  "🥫",
    "Golosinas":    "🍬",
    "Limpieza":     "🧹",
    "Verduleria":   "🥬",
    "Lacteos":      "🥛",
    "Libreria":     "📚",
    "Carniceria":   "🥩",
    "Panaderia":    "🍞",
    "Ferreteria":   "🔧",
    "Farmacia":     "💊",
}

def cat_icon(cat):
    return CAT_ICONS.get(cat, "📦")

app.jinja_env.globals['cat_icon'] = cat_icon

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS productos (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo    TEXT UNIQUE NOT NULL,
                nombre    TEXT NOT NULL,
                desc_     TEXT DEFAULT '',
                precio    REAL NOT NULL DEFAULT 0,
                categoria TEXT NOT NULL,
                marca     TEXT NOT NULL,
                foto      TEXT DEFAULT '',
                activo    INTEGER DEFAULT 1
            )
        """)
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
        if count == 0:
            seed_data = [
                ("BEB-001","Coca-Cola 500ml","Botella personal 500ml",1200,"Bebidas","Coca-Cola","",1),
                ("BEB-002","Coca-Cola 1.5L","Botella familiar 1.5 litros",2100,"Bebidas","Coca-Cola","",1),
                ("BEB-003","Coca-Cola 2.25L","Botella grande retornable",2800,"Bebidas","Coca-Cola","",1),
                ("BEB-004","Coca-Cola Zero 500ml","Sin azucar 500ml",1200,"Bebidas","Coca-Cola","",1),
                ("BEB-005","Coca-Cola Light 1.5L","Sin azucar 1.5 litros",2100,"Bebidas","Coca-Cola","",1),
                ("BEB-006","Sprite 500ml","Botella personal lima-limon",1100,"Bebidas","Sprite","",1),
                ("BEB-007","Sprite 1.5L","Botella familiar lima-limon",2000,"Bebidas","Sprite","",1),
                ("BEB-008","Sprite Zero 500ml","Sin azucar lima-limon",1100,"Bebidas","Sprite","",1),
                ("BEB-009","Villavicencio 500ml","Agua mineral sin gas",700,"Bebidas","Aguas","",1),
                ("BEB-010","Villavicencio 1.5L","Agua mineral familiar",1200,"Bebidas","Aguas","",1),
                ("BEB-011","Ser con gas 500ml","Agua saborizada con gas",900,"Bebidas","Aguas","",1),
                ("BEB-012","Quilmes 473ml","Lata de cerveza rubia",1800,"Bebidas","Cerveza","",1),
                ("BEB-013","Stella Artois 473ml","Lata de cerveza premium",2200,"Bebidas","Cerveza","",1),
                ("PAN-001","Pampers RN x24","Talle RN hasta 4kg",8500,"Panales","Pampers","",1),
                ("PAN-002","Pampers Talle S x36","De 4 a 8kg maxima suavidad",11000,"Panales","Pampers","",1),
                ("PAN-003","Pampers Talle M x32","De 6 a 10kg ajuste perfecto",11500,"Panales","Pampers","",1),
                ("PAN-004","Pampers Talle G x28","De 9 a 14kg extra proteccion",12000,"Panales","Pampers","",1),
                ("PAN-005","Pampers Talle XG x24","De 12 a 18kg mayor movilidad",12500,"Panales","Pampers","",1),
                ("PAN-006","Pampers XXG x20","Mas de 17kg talle extra grande",13000,"Panales","Pampers","",1),
                ("PAN-007","Huggies Talle S x40","De 3 a 6kg con indicador",10500,"Panales","Huggies","",1),
                ("PAN-008","Huggies Talle M x36","De 5 a 9kg mayor absorcion",11000,"Panales","Huggies","",1),
                ("PAN-009","Huggies Talle G x32","De 8 a 14kg elastico suave",11500,"Panales","Huggies","",1),
                ("PAN-010","Huggies Talle XG x28","Mas de 12kg ultra flex",12000,"Panales","Huggies","",1),
                ("PAN-011","MamisBebe Talle S x50","Economico talle S pack grande",9000,"Panales","MamisBebe","",1),
                ("PAN-012","MamisBebe Talle M x44","Economico talle M pack grande",9500,"Panales","MamisBebe","",1),
                ("PAN-013","MamisBebe Talle G x40","Economico talle G pack grande",10000,"Panales","MamisBebe","",1),
                ("COM-001","Aceite Natura 900ml","Aceite de girasol primera prensada",2800,"Comestibles","Arcor","",1),
                ("COM-002","Aceite Cocinero 1.5L","Aceite mezcla economico",3200,"Comestibles","Arcor","",1),
                ("COM-003","Arroz Largo Fino 1kg","Arroz largo fino tipo sushi",1500,"Comestibles","Marolio","",1),
                ("COM-004","Arroz Doble Carolina 1kg","Arroz doble carolina premium",1800,"Comestibles","Marolio","",1),
                ("COM-005","Fideos Spaghetti 500g","Pasta seca de semola",900,"Comestibles","Marolio","",1),
                ("COM-006","Fideos Mostachol 500g","Pasta corta ideal para sopas",900,"Comestibles","Marolio","",1),
                ("COM-007","Azucar Blanca 1kg","Azucar refinada de cana",1200,"Comestibles","Ledesma","",1),
                ("COM-008","Azucar Morena 500g","Azucar morena sin refinar",1100,"Comestibles","Ledesma","",1),
                ("GOL-001","Menthoplus Menta x20","Caramelos de menta duros",600,"Golosinas","Arcor","",1),
                ("GOL-002","Butter Toffees x20","Caramelos blandos de manteca",700,"Golosinas","Arcor","",1),
                ("GOL-003","Palitos de la Selva x20","Palitos de maiz salados",500,"Golosinas","Arcor","",1),
                ("GOL-004","Oreo Original x9","Galletitas con relleno de crema",900,"Golosinas","Bagley","",1),
                ("GOL-005","Oreo Doble Relleno x9","Mas relleno de crema",1000,"Golosinas","Bagley","",1),
                ("GOL-006","Pepitos Chocolate 100g","Galletitas con chips de chocolate",800,"Golosinas","Bagley","",1),
                ("LIM-001","Ala Polvo 500g","Detergente en polvo multiuso",2200,"Limpieza","Ala","",1),
                ("LIM-002","Ala Polvo 1kg","Detergente en polvo economia",3800,"Limpieza","Ala","",1),
                ("LIM-003","Ala Liquido 500ml","Detergente liquido para ropa",1800,"Limpieza","Ala","",1),
                ("LIM-004","Ayudin Multiusos 500ml","Limpiador multiusos lavanda",1500,"Limpieza","Ayudin","",1),
                ("LIM-005","Ayudin Bano 500ml","Limpiador desinfectante banos",1600,"Limpieza","Ayudin","",1),
                ("LIM-006","Ayudin Vidrios 500ml","Limpiador vidrios sin manchas",1400,"Limpieza","Ayudin","",1),
                ("LIM-007","Lavandina Magistral 1L","Lavandina concentrada 55g/L",1200,"Limpieza","Magistral","",1),
                ("LIM-008","Lavandina Magistral 2L","Lavandina concentrada pack ahorro",2000,"Limpieza","Magistral","",1),
            ]
            db.executemany(
                "INSERT OR IGNORE INTO productos (codigo,nombre,desc_,precio,categoria,marca,foto,activo) VALUES (?,?,?,?,?,?,?,?)",
                seed_data
            )
            db.commit()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def get_catalogo():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM productos WHERE activo=1 ORDER BY categoria, marca, nombre"
        ).fetchall()
    cats = {}
    for row in rows:
        cat   = row['categoria']
        marca = row['marca']
        if cat not in cats:
            cats[cat] = {}
        if marca not in cats[cat]:
            cats[cat][marca] = []
        cats[cat][marca].append(dict(row))
    return cats

@app.route("/")
def index():
    cats = get_catalogo()
    return render_template("index.html", cats=cats)

@app.route("/static/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("user") == ADMIN_USER and request.form.get("pass") == ADMIN_PASS:
            session['admin'] = True
            return redirect(url_for('admin_index'))
        error = "Usuario o contrasena incorrectos"
    return render_template("login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route("/admin")
@app.route("/admin/")
@login_required
def admin_index():
    with get_db() as db:
        productos = db.execute(
            "SELECT * FROM productos ORDER BY categoria, marca, nombre"
        ).fetchall()
    return render_template("admin.html", productos=[dict(p) for p in productos])

def get_categorias():
    with get_db() as db:
        rows = db.execute("SELECT DISTINCT categoria FROM productos ORDER BY categoria").fetchall()
    cats = [r['categoria'] for r in rows]
    for c in CAT_ICONS:
        if c not in cats:
            cats.append(c)
    return sorted(set(cats))

@app.route("/admin/producto/nuevo", methods=["GET","POST"])
@login_required
def admin_nuevo():
    if request.method == "POST":
        return _guardar_producto(None)
    return render_template("producto_form.html", producto=None, categorias=get_categorias())

@app.route("/admin/producto/<int:pid>/editar", methods=["GET","POST"])
@login_required
def admin_editar(pid):
    with get_db() as db:
        prod = db.execute("SELECT * FROM productos WHERE id=?", (pid,)).fetchone()
    if not prod:
        return redirect(url_for('admin_index'))
    if request.method == "POST":
        return _guardar_producto(pid)
    return render_template("producto_form.html", producto=dict(prod), categorias=get_categorias())

def _guardar_producto(pid):
    f = request.form
    cat = f.get("categoria","")
    if cat == "__nueva__":
        cat = f.get("nueva_categoria","").strip()
    codigo  = f.get("codigo","").strip().upper()
    nombre  = f.get("nombre","").strip()
    desc_   = f.get("descripcion","").strip()
    precio  = float(f.get("precio",0) or 0)
    marca   = f.get("marca","").strip()
    activo  = 1 if f.get("activo") else 0
    foto_name = ""
    file = request.files.get("foto")
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit('.',1)[1].lower()
        foto_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, foto_name))
    with get_db() as db:
        if pid:
            existing = db.execute("SELECT foto FROM productos WHERE id=?", (pid,)).fetchone()
            if not foto_name and existing:
                foto_name = existing['foto'] or ''
            db.execute("""
                UPDATE productos SET codigo=?,nombre=?,desc_=?,precio=?,
                categoria=?,marca=?,foto=?,activo=? WHERE id=?
            """, (codigo,nombre,desc_,precio,cat,marca,foto_name,activo,pid))
        else:
            db.execute("""
                INSERT INTO productos (codigo,nombre,desc_,precio,categoria,marca,foto,activo)
                VALUES (?,?,?,?,?,?,?,?)
            """, (codigo,nombre,desc_,precio,cat,marca,foto_name,activo))
        db.commit()
    return redirect(url_for('admin_index'))

@app.route("/admin/producto/<int:pid>/eliminar", methods=["POST"])
@login_required
def admin_eliminar(pid):
    with get_db() as db:
        db.execute("DELETE FROM productos WHERE id=?", (pid,))
        db.commit()
    return redirect(url_for('admin_index'))

@app.route("/admin/producto/<int:pid>/foto", methods=["POST"])
@login_required
def admin_foto_rapida(pid):
    file = request.files.get("foto")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No se recibio archivo"})
    if not allowed_file(file.filename):
        return jsonify({"ok": False, "error": "Formato no permitido"})
    ext = file.filename.rsplit('.',1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, fname))
    with get_db() as db:
        db.execute("UPDATE productos SET foto=? WHERE id=?", (fname, pid))
        db.commit()
    return jsonify({"ok": True, "foto": fname})

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

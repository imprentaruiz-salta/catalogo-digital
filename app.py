
ALMA_CATALOG_READONLY_KEY_ENV="ALMA_CATALOG_READONLY_KEY"

@app.route('/api/alma/catalog/libreria-ruiz', methods=['GET'])
def alma_readonly_libreria_ruiz():
    """Return only public Librería Ruiz product fields; never render/count page visits."""
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



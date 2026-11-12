"""
Finex - servidor web + agente analista de proyectos de data centers.
Ejecutar:  python server.py   ->   http://localhost:5050
Requiere la variable de entorno ANTHROPIC_API_KEY.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

import anthropic
import metodo
from flask import Flask, jsonify, request, send_from_directory, session

BASE = Path(__file__).parent
app = Flask(__name__, static_folder=None)
HOSTED = bool(os.environ.get("RENDER") or os.environ.get("PORT"))  # en un servidor, no en local
ADMIN_PW = os.environ.get("FINEX_ADMIN_PASSWORD") or (secrets.token_urlsafe(12) if HOSTED else "finex-demo")
if HOSTED and not os.environ.get("FINEX_ADMIN_PASSWORD"):
    print(f"AVISO: define FINEX_ADMIN_PASSWORD. Contraseña temporal del panel: {ADMIN_PW}", flush=True)
app.secret_key = os.environ.get("FINEX_SECRET") or hashlib.sha256(("finex" + ADMIN_PW).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=HOSTED, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
DATA = BASE / "data" / "anuncios.json"  # se pierde al reiniciar en hosting gratuito
SEED = BASE / "data" / "seed_anuncios.json"  # copia que SÍ viaja con el código
PUBLICAS = {"/api/anuncios", "/api/login"}
REGISTRO = BASE / "data" / "registro.jsonl"   # gobernanza: una línea por análisis o publicación
CAPTADO = BASE / "data" / "captado.json"      # tramos publicados por promotor (puerta G1)
EJECUCIONES = 3                               # el modelo evalúa 3 veces y se toma la mediana


def cargar():
    for f in (DATA, SEED):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return []


def guardar(lista):
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")


def registrar(tipo, datos):
    """Registro de decisiones (método, parámetros y resultado de cada análisis o publicación)."""
    try:
        REGISTRO.parent.mkdir(exist_ok=True)
        with REGISTRO.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tipo": tipo, **datos}, ensure_ascii=False) + "\n")
    except Exception as e:
        print("No se pudo escribir el registro:", e, flush=True)


def _leer_captado():
    try:
        return json.loads(CAPTADO.read_text(encoding="utf-8"))
    except Exception:
        return []


def captado_12m(promotor):
    """M€ ya captados por este promotor en la ventana de 12 meses (solo lo publicado en Finex)."""
    desde = (date.today() - timedelta(days=30 * metodo.PARAMS["ventana_meses"])).isoformat()
    clave = metodo._txt(promotor)
    return sum(x["meur"] for x in _leer_captado() if metodo._txt(x.get("promotor")) == clave and x.get("fecha", "") >= desde)


@app.before_request
def proteger():
    if request.path.startswith("/api/") and request.path not in PUBLICAS and not session.get("admin"):
        return jsonify({"error": "No autorizado."}), 401

PLATAFORMA = "Finex"
MODELS = ["claude-sonnet-5-5", "claude-sonnet-4-6"]  # si el primero falla, usa el siguiente
_idx = 0
_client = None

RUBRICA = """
RÚBRICA (slotting adaptado). Clasifica cada factor en UNA de estas categorías: Fuerte, Buena, Satisfactoria, Débil.
- energia. Fuerte: acceso y conexión CONCEDIDOS, potencia coherente en todos los documentos. Buena: concedido, sin
  cobertura de precio o con margen justo. Satisfactoria: solicitado sin resolver, o dudas de plazo. Débil: no
  solicitado, potencia incoherente o acuerdos sin documentar.
- cliente. Fuerte: take-or-pay >=80 % de la potencia, plazo >= plazo de la deuda, solvencia alta. Buena: 60-80 %
  contratado o contratos de 7-10 años. Satisfactoria: 30-60 % contratado, cliente único o contrato corto.
  Débil: <30 %, solo cartas de intención o capacidad incoherente.
- financiero. Fuerte: DSCR >=1,40x, LTC <=60 %, coste por MW dentro del rango, garantías reales. Buena: DSCR 1,25-1,40x,
  LTC hasta 70 %. Satisfactoria: DSCR 1,15-1,25x, LTC hasta 75 %. Débil: DSCR <1,15x o no informado, LTC >75 %,
  coste por MW muy bajo o rentabilidad "garantizada".
- permisos. Fuerte: todos concedidos. Buena: críticos concedidos, resto en trámite razonable. Satisfactoria: licencia
  concedida, el resto en trámite. Débil: sin licencia o suelo sin recalificar.
- construccion. Fuerte: constructor con varios data centers entregados, llave en mano a precio cerrado, contingencia.
  Buena: experiencia similar, contrato sólido. Satisfactoria: experiencia limitada o sin contingencia.
  Débil: sin constructor o sin historial.
- promotor. Fuerte: historial operativo y capital propio relevante. Buena: historial parcial. Satisfactoria: poco
  historial. Débil: sin historial o capital irrelevante.
Las métricas calculadas por el sistema son datos, no opiniones: no las contradigas. Si falta un dato, elige la
categoría más prudente. Penaliza las incoherencias entre documentos.
"""

# Datos del dossier que necesitan cita literal. Sin cita verificable, el dato se trata como ausente.
CAMPOS_CITA = {
    "acceso_red": ("acceso_red",),
    "potencia_mw_memoria": ("potencia_mw_memoria",),
    "potencia_mw_contrato": ("potencia_mw_contrato",),
    "mw_contratados": ("cliente", "mw_contratados"),
    "mw_take_or_pay": ("cliente", "mw_take_or_pay"),
    "contrato_anios": ("cliente", "contrato_anios"),
    "plazo_deuda_anios": ("financiero", "plazo_deuda_anios"),
    "presupuesto_meur": ("financiero", "presupuesto_meur"),
    "deuda_senior_meur": ("financiero", "deuda_senior_meur"),
    "equity_promotor_meur": ("financiero", "equity_promotor_meur"),
    "ronda_meur": ("financiero", "ronda_meur"),
    "tramo_plataforma_meur": ("financiero", "tramo_plataforma_meur"),
    "dscr": ("financiero", "dscr"),
    "rentabilidad_ofrecida_pct": ("financiero", "rentabilidad_ofrecida_pct"),
}


def get_client():
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("Falta la variable de entorno ANTHROPIC_API_KEY (ver README.txt).")
        _client = anthropic.Anthropic()
    return _client


def llamar(system, user, max_tokens=2500):
    global _idx
    while True:
        try:
            r = get_client().messages.create(
                model=MODELS[_idx],
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in r.content if b.type == "text")
        except anthropic.NotFoundError:
            if _idx < len(MODELS) - 1:
                _idx += 1
                continue
            raise


def llamar_json(system, user, max_tokens=2500, validar=None):
    ultimo = None
    regla = " Dentro de los textos del JSON no uses comillas dobles: sustitúyelas por comillas simples."
    extra = ""
    for _ in range(2):  # un reintento si el JSON sale mal
        try:
            texto = re.sub(r"```(?:json)?", "", llamar(system + regla, user + extra, max_tokens))
            i, j = texto.find("{"), texto.rfind("}")
            datos = json.loads(texto[i : j + 1])
            if validar:
                validar(datos)
            return datos
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            ultimo = e
            extra = f"\n\nTu respuesta anterior no era JSON válido ({e}). Responde de nuevo con JSON válido y sin comillas dobles dentro de los textos."
    raise RuntimeError(f"La respuesta del modelo no tuvo el formato esperado ({ultimo}). Inténtalo de nuevo.")


def seguro(fn):
    @wraps(fn)
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # el frontend muestra el mensaje
            return jsonify({"error": f"{e}"}), 500

    return w


# ---------------- rutas ----------------
@app.get("/")
def index():
    return send_from_directory(str(BASE / "static"), "index.html")


@app.get("/interno")
def interno():
    return send_from_directory(str(BASE / "static"), "interno.html")


@app.post("/api/login")
def login():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if hmac.compare_digest(str(pw), ADMIN_PW):
        session["admin"] = True
        return jsonify({"ok": True})
    time.sleep(1)  # frena el ensayo masivo de contraseñas
    return jsonify({"error": "Contraseña incorrecta."}), 401


@app.get("/api/anuncios")
def anuncios():
    return jsonify(cargar())


@app.post("/api/publicar")
@seguro
def publicar():
    d = request.get_json()
    f, ev, a = d["ficha"], d["ev"], d.get("anuncio")
    if ev.get("decision") == "rechazar" or not a:
        return jsonify({"error": "No se puede publicar un proyecto rechazado."}), 400
    if ev.get("decision") == "analista" and not d.get("aprobado"):
        return jsonify({"error": "Una categoría Satisfactoria requiere la aprobación expresa de un analista."}), 400
    fin, k = f.get("financiero") or {}, f.get("construccion") or {}
    lista = cargar()
    rango = ev.get("rango") or {}
    nuevo = {"id": uuid.uuid4().hex[:8], "fecha": date.today().isoformat(), "proyecto": f.get("proyecto"),
             "ubicacion": f.get("ubicacion"), "mw": f.get("potencia_mw_memoria"), "categoria": ev["categoria"],
             "rango": [rango.get("desde"), rango.get("hasta")] if rango else None, "tramo": fin.get("tramo_plataforma_meur"),
             "plazo": k.get("plazo_meses"), "metodo": ev.get("metodo"), "aprobado_analista": bool(d.get("aprobado")), "anuncio": a}
    lista.append(nuevo)
    guardar(lista)
    if fin.get("tramo_plataforma_meur") is not None:
        cap = _leer_captado()
        cap.append({"id": nuevo["id"], "promotor": f.get("promotor"), "fecha": nuevo["fecha"], "meur": fin["tramo_plataforma_meur"]})
        CAPTADO.parent.mkdir(exist_ok=True)
        CAPTADO.write_text(json.dumps(cap, ensure_ascii=False, indent=1), encoding="utf-8")
    registrar("publicacion", {"id": nuevo["id"], "proyecto": f.get("proyecto"), "categoria": ev["categoria"],
                              "metodo": ev.get("metodo"), "params": ev.get("params_hash"), "aprobado_analista": bool(d.get("aprobado"))})
    return jsonify({"ok": True})


@app.get("/api/exportar")
def exportar():
    r = jsonify(cargar())
    r.headers["Content-Disposition"] = "attachment; filename=seed_anuncios.json"
    return r


@app.post("/api/retirar")
def retirar():
    i = request.get_json().get("id")
    guardar([a for a in cargar() if a["id"] != i])
    try:
        CAPTADO.write_text(json.dumps([x for x in _leer_captado() if x.get("id") != i], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    registrar("retirada", {"id": i})
    return jsonify({"ok": True})


@app.get("/api/dossiers")
def dossiers():
    out = [{"id": f.stem, "texto": f.read_text(encoding="utf-8")} for f in sorted((BASE / "dossiers").glob("*.txt"))]
    return jsonify(out)


# PASO 1: extracción con prueba (cada dato crítico lleva su cita literal; sin cita verificable, se descarta)
@app.post("/api/extraer")
@seguro
def extraer():
    dossier = request.get_json()["dossier"]
    system = (
        "Eres un analista de inversión en infraestructura digital. Extraes datos de dossiers de promotores. "
        "Responde SOLO con un objeto JSON válido, sin texto adicional. Si un dato no aparece, usa null. No inventes nada."
    )
    user = (
        "Extrae del dossier este JSON:\n"
        '{"proyecto": str, "promotor": str, "historial_promotor": str, "ubicacion": str, "potencia_mw_memoria": num, '
        '"potencia_mw_contrato": num, "fase_proyecto": "desarrollo|construccion|operacion|null", '
        '"acceso_red": "concedido|solicitado|no_solicitado|null", '
        '"permisos": {"urbanistico": str, "ambiental": str, "autorizacion_administrativa": str}, '
        '"cliente": {"nombre": str, "contrato_anios": num, "tipo_contrato": str, "solvencia": str, '
        '"mw_contratados": num, "mw_take_or_pay": num}, '
        '"construccion": {"constructor": str, "experiencia": str, "plazo_meses": num, "contingencia_pct": num}, '
        '"financiero": {"presupuesto_meur": num, "deuda_senior_meur": num, "equity_promotor_meur": num, '
        '"ronda_meur": num, "tramo_plataforma_meur": num, "tipo_tramo": "senior|mezzanine|otro|null", '
        '"plazo_deuda_anios": num, "dscr": num, "rentabilidad_ofrecida_pct": num, "rentabilidad_garantizada": bool, '
        '"garantias": str}, "citas": {"<dato>": "<fragmento literal>"}, "incoherencias": [str], "datos_faltantes": [str]}\n\n'
        "Reglas: 'mw_contratados' son los MW con contrato firmado. 'mw_take_or_pay' son los MW con cláusula take-or-pay "
        "o pago mínimo garantizado: null si el dossier no lo dice; 0 solo si dice expresamente que no hay compromiso. "
        "En 'incoherencias' lista cualquier dato que se contradiga entre secciones (textos de máx. 15 palabras).\n"
        "En 'citas' incluye, para CADA uno de estos datos que no sea null, el fragmento LITERAL del dossier de donde sale "
        "(copiado sin cambiar ni una palabra, máx. 25 palabras): " + ", ".join(CAMPOS_CITA) + ". "
        "Si no puedes citarlo literalmente, pon ese dato a null.\n\nDOSSIER:\n" + dossier
    )
    ficha = llamar_json(system, user, 3500)
    citas = ficha.get("citas") or {}
    sin_cita = []
    for clave, ruta in CAMPOS_CITA.items():
        padre = ficha
        for paso in ruta[:-1]:
            padre = padre.get(paso) if isinstance(padre.get(paso), dict) else {}
        if padre.get(ruta[-1]) is not None and not metodo.cita_valida(citas.get(clave), dossier):
            padre[ruta[-1]] = None
            sin_cita.append(clave)
    if re.search(r"rentabilidad[^.\n]{0,60}?(?<!no )garantizad", dossier, re.I):
        ficha.setdefault("financiero", {})["rentabilidad_garantizada"] = True
    ficha["datos_sin_cita"] = sin_cita
    ficha["datos_faltantes"] = list(ficha.get("datos_faltantes") or []) + [f"Sin cita verificable: {c}" for c in sin_cita]
    return jsonify({"ficha": ficha})


# PASO 2: evaluación. La IA propone categoría + cita por factor (3 ejecuciones, mediana); el código decide.
@app.post("/api/evaluar")
@seguro
def evaluar():
    d = request.get_json()
    ficha, dossier = d["ficha"], d.get("dossier", "")
    m = metodo.metricas(ficha)
    system = (
        "Eres un analista de riesgo prudente y riguroso. Evalúas proyectos de data centers para inversores minoristas. "
        "Responde SOLO con JSON válido."
    )
    user = (
        RUBRICA
        + "\nMÉTRICAS CALCULADAS POR EL SISTEMA:\n" + json.dumps(metodo.filas_metricas(m), ensure_ascii=False, indent=1)
        + "\n\nFICHA DEL PROYECTO:\n" + json.dumps({k: v for k, v in ficha.items() if k != "citas"}, ensure_ascii=False, indent=1)
        + "\n\nDOSSIER ORIGINAL (para citar):\n" + dossier
        + '\n\nDevuelve: {"factores": {"energia": {"categoria": "Fuerte|Buena|Satisfactoria|Débil", "cita": str, "razon": str}, '
        '"cliente": {...}, "financiero": {...}, "permisos": {...}, "construccion": {...}, "promotor": {...}}, '
        '"banderas_rojas": [str]}. "cita" es un fragmento LITERAL del dossier (máx. 25 palabras) que justifica la categoría; '
        'si no hay dato, pon "". "razon" es UNA frase de máx. 30 palabras.'
    )

    def validar(x):
        for k in metodo.FACTORES:
            x["factores"][k]["categoria"]
            x["factores"][k]["razon"]

    def una():
        try:
            return llamar_json(system, user, 3000, validar)
        except RuntimeError as e:
            return e

    with ThreadPoolExecutor(EJECUCIONES) as ex:
        res = [f.result() for f in [ex.submit(una) for _ in range(EJECUCIONES)]]
    runs = [r for r in res if isinstance(r, dict)]
    if not runs:
        raise res[0]
    factores, cats_ia = {}, {}
    for k in metodo.FACTORES:
        lista = [metodo.norm_cat(r["factores"][k]["categoria"]) for r in runs]
        med = metodo.mediana_categoria(lista)
        elegido = next(r for r in runs if metodo.norm_cat(r["factores"][k]["categoria"]) == med)["factores"][k]
        cita = (elegido.get("cita") or "").strip()
        cats_ia[k] = med
        factores[k] = {"categoria_ia": med, "ejecuciones": lista, "estable": len(set(lista)) == 1,
                       "cita": cita, "cita_ok": bool(cita) and metodo.cita_valida(cita, dossier), "razon": elegido.get("razon", "")}
    promotor = ficha.get("promotor") or ""
    r = metodo.analizar(ficha, cats_ia, captado_12m(promotor))
    for k in metodo.FACTORES:
        factores[k]["categoria"] = r["categorias"][k]
        factores[k]["degradado"] = r["degradaciones"].get(k)
    r.update({"factores": factores, "banderas_rojas": runs[0].get("banderas_rojas") or [],
              "ejecuciones": len(runs), "estable": all(f["estable"] for f in factores.values())})
    registrar("analisis", {"proyecto": ficha.get("proyecto"), "promotor": promotor, "metodo": r["metodo"], "params": r["params_hash"],
                           "categoria": r["categoria"], "decision": r["decision"], "nota": r["nota"],
                           "categorias_ia": r["categorias_ia"], "categorias": r["categorias"], "ejecuciones": factores and {k: v["ejecuciones"] for k, v in factores.items()}})
    return jsonify(r)


# PASO 3: informe interno + anuncio para inversores (recibe la categoría y las métricas ya calculadas)
@app.post("/api/redactar")
@seguro
def redactar():
    d = request.get_json()
    ficha, ev = d["ficha"], d["ev"]
    decision, rango = ev["decision"], ev.get("rango")
    textos = {"aceptar": "PUBLICABLE", "analista": "SATISFACTORIA: REQUIERE APROBACIÓN EXPRESA DE UN ANALISTA", "rechazar": "RECHAZAR"}
    ctx = (
        "FICHA:\n" + json.dumps({k: v for k, v in ficha.items() if k != "citas"}, ensure_ascii=False, indent=1)
        + "\n\nMÉTRICAS CALCULADAS:\n" + json.dumps(ev["metricas"], ensure_ascii=False, indent=1)
        + "\n\nFACTORES (categoría final y razón):\n"
        + json.dumps({k: {"categoria": v["categoria"], "razon": v["razon"]} for k, v in ev["factores"].items()}, ensure_ascii=False, indent=1)
        + f"\n\nCATEGORÍA FINAL: {ev['categoria']}. DECISIÓN: {textos[decision]}. "
        + ("MOTIVOS DEL RECHAZO: " + "; ".join(ev["motivos_rechazo"]) + ". " if ev["motivos_rechazo"] else "")
        + (f"RANGO DE REFERENCIA DE RENTABILIDAD (no garantizado): {rango['desde']}-{rango['hasta']} %." if rango else "")
    )
    system = (
        f"Eres el equipo de análisis de {PLATAFORMA}, plataforma europea de inversión en centros de datos. "
        "Responde SOLO con JSON válido. La categoría, la decisión, las métricas y el rango ya están calculados por el sistema: "
        "no los cambies. No inventes cifras: usa únicamente datos de la ficha, las métricas y los factores. "
        "No menciones ratings de agencias ni equivalencias con ellos."
    )
    informe = (
        '"informe": {"resumen_ejecutivo": "máx. 70 palabras", "puntos_fuertes": [str], "riesgos_clave": [str], '
        '"condiciones": [str], "recomendacion": "1-2 frases"}'
    )
    if decision == "rechazar":
        user = (
            ctx + "\n\nDevuelve: {" + informe + ', "anuncio": null}. Máx. 4 elementos por lista. '
            'En "condiciones" indica qué tendría que cambiar el promotor para que se reconsidere el proyecto.'
        )
    else:
        anuncio = (
            '"anuncio": {"titulo": str, "entradilla": "máx. 40 palabras", "uso_de_fondos": "1-2 frases", '
            '"por_que_esta_categoria": "2-3 frases claras para un inversor sin conocimientos financieros", '
            '"riesgos": [{"titulo": "máx. 6 palabras", "explicacion": "1-2 frases sencillas"}]}'
        )
        user = (
            ctx + "\n\nDevuelve: {" + informe + ", " + anuncio + "}. Máx. 4 elementos por lista; "
            'en "riesgos" del anuncio, exactamente 3. En "condiciones" indica qué debe cumplir el promotor antes de publicar. '
            "En el anuncio no prometas rentabilidad, no cites cifras de rentabilidad y no uses lenguaje comercial exagerado."
        )

    def validar(x):
        x["informe"]["resumen_ejecutivo"]

    res = llamar_json(system, user, 3000, validar)
    return jsonify({"informe": res["informe"], "anuncio": res.get("anuncio")})


if __name__ == "__main__":
    print(f"\n  {PLATAFORMA} en marcha -> http://localhost:5050   |   panel interno -> http://localhost:5050/interno\n  Contraseña del panel: {ADMIN_PW}  (cambia con FINEX_ADMIN_PASSWORD)\n")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=False)

"""
Método Finex de evaluación de riesgo v1  ·  metodo.py

Todo lo numérico del método vive aquí. La IA solo extrae datos y PROPONE una categoría por
factor; este módulo calcula las métricas, aplica puertas y topes, agrega y calcula la pérdida
esperada y la rentabilidad de referencia. No usa ninguna librería externa.

Para cambiar un número del método, edita PARAMS (y sube METODO_VERSION).
"""
import hashlib
import json
import re
import unicodedata

METODO_VERSION = "Finex v1.0 (borrador)"

CATS = ["Débil", "Satisfactoria", "Buena", "Fuerte"]  # el índice es el nivel
FACTORES = ["energia", "cliente", "financiero", "permisos", "construccion", "promotor"]
CRITICOS = ["energia", "cliente", "permisos"]
NOMBRES = {
    "energia": "Energía y acceso a red",
    "cliente": "Cliente final",
    "financiero": "Estructura financiera",
    "permisos": "Permisos y licencias",
    "construccion": "Construcción",
    "promotor": "Promotor",
}

PARAMS = {
    # --- Factores (AHP, redondeados). Juicio del equipo: revisar. ---
    "pesos": {"energia": 28, "cliente": 27, "financiero": 15, "permisos": 15, "construccion": 8, "promotor": 7},
    "puntos": {"Fuerte": 95, "Buena": 75, "Satisfactoria": 50, "Débil": 20},
    # Categoría a partir de la nota: puntos medios entre los anclajes 95/75/50/20. Supuesto.
    "bandas_nota": {"Fuerte": 85.0, "Buena": 62.5, "Satisfactoria": 35.0},
    "rating": {"Fuerte": "BBB o mejor", "Buena": "BB", "Satisfactoria": "BB- / B+", "Débil": "B o peor"},
    # --- Puertas ---
    "tope_tramo_meur": 5.0,        # Reglamento (UE) 2020/1503, por promotor y 12 meses
    "ventana_meses": 12,
    # --- Umbrales financieros (rangos de mercado, calidad de fuente desigual) ---
    "dscr": {"fuerte": 1.40, "buena": 1.25, "satisfactoria": 1.15},          # por debajo de 1,15 = Débil
    "ltc": {"fuerte": 60.0, "buena": 70.0, "satisfactoria": 75.0},           # por encima de 75 = Débil
    # --- Coste por MW de carga IT, EMEA: 7,3-13,3 M$ (Savills / Turner & Townsend) ---
    "coste_mw_usd_m": [7.3, 13.3],
    "fx_eur_por_usd": 0.885,       # ACTUALIZAR. ~1,13 USD/EUR (1-oct-2026); no verificado hoy
    "coste_mw_muy_bajo_frac": 0.5, # por debajo del 50 % del mínimo = Débil
    # --- Cliente ---
    "cobertura": {"fuerte": 80.0, "buena": 60.0, "satisfactoria": 30.0},     # % MW take-or-pay
    "contrato_anios": {"fuerte": 10, "buena": 7},
    # --- Pérdida esperada ---
    "pd_pct": {"Fuerte": 0.15, "Buena": 0.6, "Satisfactoria": 1.8, "Débil": 5.0},
    "lgd_pct": {"senior": 20.0, "mezzanine": 60.0},
    "mult_construccion": 1.5,
    # --- Rentabilidad de referencia ---
    "tipo_base_pct": 3.25,         # ACTUALIZAR. Euríbor 12 m, sesión del 6-nov-2026 (3,248 %); no es un swap
    "iliquidez_pb": 100,
    "diferenciales_pb": {"Fuerte": [350, 500], "Buena": [450, 600], "Satisfactoria": [600, 800]},
    "alerta_seleccion_adversa_pb": 200,
    "origen": {
        "pesos": "AHP, razón de consistencia 0,003. Las comparaciones son juicio del equipo.",
        "bandas_nota": "Supuesto: puntos medios entre 95/75/50/20.",
        "tope_tramo_meur": "Reglamento (UE) 2020/1503, art. 1.2.c; Q&A ESMA.",
        "dscr": "Covenants 1,15-1,35x; Fitch 1,32x con hiperescalar; 1,40-1,60x grado de inversión. Calidad desigual.",
        "ltc": "Rangos de mercado para instalaciones en operación con inquilino.",
        "coste_mw_usd_m": "Savills sobre datos de Turner & Townsend.",
        "fx_eur_por_usd": "Parámetro manual.",
        "cobertura": "Rúbrica del método (juicio del equipo).",
        "pd_pct": "S&P: BBB 0,1-0,2 %, BB 0,6 %. Satisfactoria y Débil son supuestos interpolados.",
        "lgd_pct": "Senior: Moody's (recuperación ~80 %). Mezzanine 60 %: supuesto sin fuente.",
        "mult_construccion": "Supuesto (Moody's y Basel 3.1 apuntan en esa dirección, sin cifra).",
        "tipo_base_pct": "Parámetro manual.",
        "iliquidez_pb": "Aviva Investors, media de largo plazo.",
        "diferenciales_pb": "QIC, IFM, Schroders. Fuerte: supuesto a calibrar.",
        "alerta_seleccion_adversa_pb": "Stiglitz y Weiss (1981), citado de memoria.",
    },
}


def params_hash(p=PARAMS):
    base = {k: v for k, v in p.items() if k != "origen"}
    return hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()[:8]


# ------------------------------------------------------------------ utilidades
def _n(x):
    if isinstance(x, bool) or x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_cat(x):
    s = unicodedata.normalize("NFD", str(x or "")).encode("ascii", "ignore").decode().strip().lower()
    for c in CATS:
        if unicodedata.normalize("NFD", c).encode("ascii", "ignore").decode().lower() == s:
            return c
    return "Débil"  # una categoría ilegible se trata como la peor


def nivel(c):
    return CATS.index(c)


def mediana_categoria(lista):
    """Mediana ordinal de varias ejecuciones. Con número par, la inferior (prudente)."""
    orden = sorted((norm_cat(c) for c in lista), key=nivel)
    return orden[(len(orden) - 1) // 2]


def _txt(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def cita_valida(cita, dossier):
    """La cita debe aparecer literalmente en el dossier (se ignoran mayúsculas, tildes y signos)."""
    d = _txt(dossier)
    partes = [_txt(x) for x in re.split(r"\.\.\.|…", str(cita or ""))]
    partes = [x for x in partes if x]
    return bool(partes) and all(x in d for x in partes)


# ------------------------------------------------------------------ capa 2: métricas
def _fmt(v, dec=1, suf=""):
    return "—" if v is None else f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".") + suf


def metricas(f, p=PARAMS):
    fin, cl = f.get("financiero") or {}, f.get("cliente") or {}
    pres, deuda, eq = _n(fin.get("presupuesto_meur")), _n(fin.get("deuda_senior_meur")), _n(fin.get("equity_promotor_meur"))
    ronda, tramo, dscr = _n(fin.get("ronda_meur")), _n(fin.get("tramo_plataforma_meur")), _n(fin.get("dscr"))
    mw, mw_c = _n(f.get("potencia_mw_memoria")), _n(f.get("potencia_mw_contrato"))
    tp, firmes = _n(cl.get("mw_take_or_pay")), _n(cl.get("mw_contratados"))
    c_anios, d_anios = _n(cl.get("contrato_anios")), _n(fin.get("plazo_deuda_anios"))

    ltc = None
    if pres and pres > 0 and (deuda is not None or ronda is not None):
        ltc = round(((deuda or 0) + (ronda or 0)) / pres * 100, 2)
    capital = round(eq / pres * 100, 1) if pres and eq is not None else None
    capex = round(pres / mw, 2) if pres and mw else None
    fx = p["fx_eur_por_usd"]
    rango_mw = [round(x * fx, 2) for x in p["coste_mw_usd_m"]]
    base_cob = tp if tp is not None else firmes
    cobertura = round(base_cob / mw * 100, 1) if base_cob is not None and mw else None

    incoh = []
    if mw is not None and mw_c is not None and abs(mw - mw_c) > 0.01:
        incoh.append(f"Potencia de memoria ({_fmt(mw, 0)} MW) distinta de la del contrato ({_fmt(mw_c, 0)} MW)")
    if pres and None not in (deuda, eq, ronda) and abs(deuda + eq + ronda - pres) > 0.02 * pres:
        incoh.append("La suma de deuda, capital y ronda no coincide con el presupuesto")

    return {
        "valores": {"ltc": ltc, "capital_pct": capital, "capex_mw_meur": capex, "rango_mw_meur": rango_mw,
                    "cobertura_pct": cobertura, "take_or_pay_acreditado": tp is not None,
                    "contrato_anios": c_anios, "plazo_deuda_anios": d_anios, "dscr": dscr,
                    "tramo_meur": tramo, "incoherencias": incoh},
        "ltc": ltc, "capital_pct": capital, "capex": capex, "rango_mw": rango_mw, "cobertura": cobertura,
        "tp_acreditado": tp is not None, "c_anios": c_anios, "d_anios": d_anios, "dscr": dscr,
        "tramo": tramo, "incoh": incoh,
    }


def filas_metricas(m, p=PARAMS):
    """Filas para el semáforo del panel: nombre, valor, referencia, estado (ok/aviso/mal/nd)."""
    f = []
    L = p["ltc"]
    est = "nd" if m["ltc"] is None else "ok" if m["ltc"] <= L["fuerte"] else "aviso" if m["ltc"] <= L["satisfactoria"] else "mal"
    f.append(["LTC (deuda + ronda sobre coste)", _fmt(m["ltc"], 1, " %"), f"≤ {L['fuerte']:.0f} % fuerte · ≤ {L['satisfactoria']:.0f} % satisfactoria", est])
    f.append(["Capital del promotor", _fmt(m["capital_pct"], 1, " %"), "Sin umbral fijo (orientativo)", "nd" if m["capital_pct"] is None else "ok"])
    lo, hi = m["rango_mw"]
    if m["capex"] is None:
        est = "nd"
    elif m["capex"] < lo * p["coste_mw_muy_bajo_frac"]:
        est = "mal"
    elif m["capex"] < lo or m["capex"] > hi:
        est = "aviso"
    else:
        est = "ok"
    f.append(["Coste por MW", _fmt(m["capex"], 2, " M€"), f"Mercado EMEA {lo:.1f}-{hi:.1f} M€ (tipo de cambio {p['fx_eur_por_usd']})", est])
    C = p["cobertura"]
    est = "nd" if m["cobertura"] is None else "ok" if m["cobertura"] >= C["fuerte"] else "aviso" if m["cobertura"] >= C["satisfactoria"] else "mal"
    nota = "" if m["tp_acreditado"] or m["cobertura"] is None else " (contratado, sin take-or-pay acreditado)"
    f.append(["Cobertura contratada", _fmt(m["cobertura"], 1, " %") + nota, f"≥ {C['fuerte']:.0f} % fuerte · ≥ {C['satisfactoria']:.0f} % satisfactoria", est])
    if m["c_anios"] is None or m["d_anios"] is None:
        f.append(["Plazo contrato vs deuda", "No verificable", "Contrato ≥ plazo de la deuda", "nd"])
    else:
        f.append(["Plazo contrato vs deuda", f"{_fmt(m['c_anios'], 0)} vs {_fmt(m['d_anios'], 0)} años", "Contrato ≥ plazo de la deuda", "ok" if m["c_anios"] >= m["d_anios"] else "mal"])
    D = p["dscr"]
    est = "mal" if m["dscr"] is None else "ok" if m["dscr"] >= D["fuerte"] else "aviso" if m["dscr"] >= D["satisfactoria"] else "mal"
    f.append(["DSCR (autodeclarado, sin estrés)", "No informado" if m["dscr"] is None else _fmt(m["dscr"], 2, "x"), f"≥ {D['fuerte']:.2f}x fuerte · ≥ {D['satisfactoria']:.2f}x satisfactoria", est])
    f.append(["Coherencia entre secciones", "Incoherente" if m["incoh"] else "Sin incoherencias", "; ".join(m["incoh"]) or "Potencias y presupuesto cuadran", "mal" if m["incoh"] else "ok"])
    return [dict(zip(("nombre", "valor", "referencia", "estado"), r)) for r in f]


# ------------------------------------------------------------------ capa 3: techos por métrica
def techos(f, m, p=PARAMS):
    """El código puede degradar la categoría que propone la IA. Devuelve {factor: [(nivel_max, motivo)]}."""
    t = {k: [] for k in FACTORES}
    fin, k = f.get("financiero") or {}, f.get("construccion") or {}

    def cap(factor, cat, motivo):
        t[factor].append((nivel(cat), motivo))

    # Energía
    acc = f.get("acceso_red")
    if acc == "solicitado":
        cap("energia", "Satisfactoria", "Acceso a red solicitado y sin resolver")
    elif acc != "concedido":
        cap("energia", "Débil", "Acceso a red no solicitado o sin acreditar en el dossier")
    if m["incoh"] and any("Potencia" in x for x in m["incoh"]):
        cap("energia", "Satisfactoria", "Potencia incoherente entre documentos")

    # Cliente
    C, cob = p["cobertura"], m["cobertura"]
    if cob is None:
        cap("cliente", "Débil", "Cobertura contratada no acreditada en el dossier")
    elif cob < C["satisfactoria"]:
        cap("cliente", "Débil", f"Cobertura contratada de {_fmt(cob, 1)} %")
    elif cob < C["buena"]:
        cap("cliente", "Satisfactoria", f"Cobertura contratada de {_fmt(cob, 1)} %")
    elif cob < C["fuerte"]:
        cap("cliente", "Buena", f"Cobertura contratada de {_fmt(cob, 1)} %")
    if cob is not None and not m["tp_acreditado"]:
        cap("cliente", "Buena", "Take-or-pay no acreditado")
    if m["c_anios"] is not None:
        if m["c_anios"] < p["contrato_anios"]["buena"]:
            cap("cliente", "Satisfactoria", f"Contrato de {_fmt(m['c_anios'], 0)} años (menos de {p['contrato_anios']['buena']})")
        elif m["c_anios"] < p["contrato_anios"]["fuerte"]:
            cap("cliente", "Buena", f"Contrato de {_fmt(m['c_anios'], 0)} años (menos de {p['contrato_anios']['fuerte']})")
        if m["d_anios"] is not None and m["c_anios"] < m["d_anios"]:
            cap("cliente", "Buena", "Contrato más corto que la deuda")
    if m["incoh"] and any("Potencia" in x for x in m["incoh"]):
        cap("cliente", "Satisfactoria", "Capacidad contratada incoherente con la memoria")

    # Financiero
    D, L = p["dscr"], p["ltc"]
    if m["dscr"] is None:
        cap("financiero", "Débil", "DSCR no informado")
    elif m["dscr"] < D["satisfactoria"]:
        cap("financiero", "Débil", f"DSCR de {_fmt(m['dscr'], 2)}x (por debajo de {D['satisfactoria']:.2f}x)")
    elif m["dscr"] < D["buena"]:
        cap("financiero", "Satisfactoria", f"DSCR de {_fmt(m['dscr'], 2)}x")
    elif m["dscr"] < D["fuerte"]:
        cap("financiero", "Buena", f"DSCR de {_fmt(m['dscr'], 2)}x")
    if m["ltc"] is not None:
        if m["ltc"] > L["satisfactoria"]:
            cap("financiero", "Débil", f"LTC de {_fmt(m['ltc'], 1)} % (por encima de {L['satisfactoria']:.0f} %)")
        elif m["ltc"] > L["buena"]:
            cap("financiero", "Satisfactoria", f"LTC de {_fmt(m['ltc'], 1)} %")
        elif m["ltc"] > L["fuerte"]:
            cap("financiero", "Buena", f"LTC de {_fmt(m['ltc'], 1)} %")
    if m["capex"] is not None:
        lo = m["rango_mw"][0]
        if m["capex"] < lo * p["coste_mw_muy_bajo_frac"]:
            cap("financiero", "Débil", f"Coste por MW muy por debajo del mercado ({_fmt(m['capex'], 2)} M€)")
        elif m["capex"] < lo:
            cap("financiero", "Buena", f"Coste por MW por debajo del rango de mercado ({_fmt(m['capex'], 2)} M€)")
    if fin.get("rentabilidad_garantizada"):
        cap("financiero", "Débil", "Promete una rentabilidad «garantizada»")

    # Construcción
    if not k.get("constructor"):
        cap("construccion", "Débil", "Sin constructor identificado")
    return t


def aplicar_techos(cats_ia, t):
    finales, degradaciones = {}, {}
    for fac in FACTORES:
        cat = norm_cat(cats_ia.get(fac))
        lim = min(t[fac], key=lambda x: x[0]) if t[fac] else None
        if lim and lim[0] < nivel(cat):
            degradaciones[fac] = {"de": cat, "a": CATS[lim[0]], "motivos": [mv for n, mv in t[fac] if n == lim[0]]}
            cat = CATS[lim[0]]
        finales[fac] = cat
    return finales, degradaciones


# ------------------------------------------------------------------ capa 0: puertas
def puertas(f, cats, m, captado_previo=0.0, p=PARAMS):
    tramo = m["tramo"]
    if tramo is None:
        g1 = (False, "Tramo de la plataforma no informado")
    else:
        total = tramo + (captado_previo or 0)
        g1 = (total <= p["tope_tramo_meur"],
              f"Tramo {_fmt(tramo, 1)} M€ + {_fmt(captado_previo or 0, 1)} M€ ya captados por el promotor en {p['ventana_meses']} meses = {_fmt(total, 1)} M€ (tope {p['tope_tramo_meur']:.0f} M€)")
    g2 = (cats["energia"] != "Débil", "Energía no es Débil" if cats["energia"] != "Débil" else "Energía Débil: sin acceso a red acreditado o potencia incoherente")
    g3 = (cats["cliente"] != "Débil", "Cliente no es Débil" if cats["cliente"] != "Débil" else "Cliente Débil: sin contrato suficiente o solo cartas de intención")
    return [{"id": i, "nombre": n, "ok": g[0], "detalle": g[1]} for i, n, g in
            (("G1", "Límite regulatorio (5 M€)", g1), ("G2", "Energía no Débil", g2), ("G3", "Cliente no Débil", g3))]


# ------------------------------------------------------------------ capa 4: agregación
def agregar(cats, pts, p=PARAMS):
    pesos, puntos, bandas = p["pesos"], p["puntos"], p["bandas_nota"]
    nota = round(sum(puntos[cats[k]] * pesos[k] for k in FACTORES) / sum(pesos.values()), 1)
    cat_nota = next((c for c in ("Fuerte", "Buena", "Satisfactoria") if nota >= bandas[c]), "Débil")
    peor = min(nivel(cats[k]) for k in CRITICOS)
    tope = CATS[min(peor + 1, 3)]
    debil_critico = [NOMBRES[k] for k in CRITICOS if cats[k] == "Débil"]
    categoria = "Débil" if debil_critico else CATS[min(nivel(cat_nota), nivel(tope))]
    motivos = []
    if debil_critico:
        motivos.append("Factor crítico Débil: " + ", ".join(debil_critico))
    motivos += [f"Puerta {x['id']}: {x['detalle']}" for x in pts if not x["ok"]]
    if categoria == "Débil" and not motivos:
        motivos.append("Categoría final Débil")
    decision = "rechazar" if (motivos or categoria == "Débil") else "analista" if categoria == "Satisfactoria" else "aceptar"
    return {"nota": nota, "categoria_por_nota": cat_nota, "tope": tope, "categoria": categoria,
            "rating": p["rating"][categoria], "decision": decision, "motivos_rechazo": motivos}


# ------------------------------------------------------------------ capa 5: pérdida esperada y rentabilidad
def perdida_esperada(categoria, f, p=PARAMS):
    fin = f.get("financiero") or {}
    tipo = "senior" if str(fin.get("tipo_tramo") or "").lower() == "senior" else "mezzanine"  # sin dato: prudente
    en_obra = str(f.get("fase_proyecto") or "construccion").lower() != "operacion"
    pd, lgd = p["pd_pct"][categoria], p["lgd_pct"][tipo]
    mult = p["mult_construccion"] if en_obra else 1.0
    pe = pd * lgd / 100 * mult
    return {"pd_pct": pd, "lgd_pct": lgd, "tipo_tramo": tipo, "multiplicador": mult, "pe_pct": round(pe, 3), "pe_pb": round(pe * 100, 1)}


def rango_referencia(categoria, pe, p=PARAMS):
    if categoria not in p["diferenciales_pb"]:
        return None
    lo, hi = p["diferenciales_pb"][categoria]
    minimo = pe["pe_pb"] + p["iliquidez_pb"]
    lo2 = max(lo, minimo)
    hi2 = max(hi, lo2)
    base = p["tipo_base_pct"]
    return {"desde": round(base + lo2 / 100, 2), "hasta": round(base + hi2 / 100, 2), "base_pct": base,
            "dif_pb": [round(lo2), round(hi2)], "pe_pb": pe["pe_pb"], "iliquidez_pb": p["iliquidez_pb"],
            "ajustado_por_pe": lo2 > lo, "origen": p["origen"]["diferenciales_pb"]}


# ------------------------------------------------------------------ orquestador
def analizar(f, cats_ia, captado_previo=0.0, p=PARAMS):
    m = metricas(f, p)
    t = techos(f, m, p)
    cats, degr = aplicar_techos(cats_ia, t)
    pts = puertas(f, cats, m, captado_previo, p)
    ag = agregar(cats, pts, p)
    pe = perdida_esperada(ag["categoria"], f, p)
    rango = None if ag["decision"] == "rechazar" else rango_referencia(ag["categoria"], pe, p)
    ofrecida = _n((f.get("financiero") or {}).get("rentabilidad_ofrecida_pct"))
    supera = bool(rango and ofrecida is not None and ofrecida > rango["hasta"])
    alerta = bool(rango and ofrecida is not None and ofrecida > rango["hasta"] + p["alerta_seleccion_adversa_pb"] / 100)
    return {"metodo": METODO_VERSION, "params_hash": params_hash(p), **ag,
            "categorias": cats, "categorias_ia": {k: norm_cat(cats_ia.get(k)) for k in FACTORES},
            "degradaciones": degr, "pesos": p["pesos"], "nombres": NOMBRES, "criticos": CRITICOS,
            "puertas": pts, "metricas": filas_metricas(m, p), "valores": m["valores"],
            "perdida_esperada": pe, "rango": rango, "supera_techo": supera, "alerta_seleccion": alerta,
            "captado_previo_meur": captado_previo or 0}

"""Orquestación end-to-end del microservicio.

Flujo:
  1. Leer warehouse (Google Sheets, hoja BD APU MTTO).
  2. Cargar y normalizar comparativos desde GCS.
  3. Aplicar IPC a warehouse y/o comparativos.
  4. Mapear 1 a 1 (NLP) cada insumo del warehouse contra el catálogo comparativo.
  5. Calcular el nuevo valor (mejor precio comparativo IPC vs warehouse IPC).
  6. Actualizar el Sheet (columna Vr. Unitario) salvo DRY_RUN.
  7. Generar reporte analítico (xlsx) y subirlo a GCS.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import analytics
from .comparativos_loader import load_from_gcs, to_number
from .config import Settings
from .ipc import apply_ipc
from .nlp_mapper import ItemMatcher, normalize
from .sheets_integration import WarehouseSheet

# Nombres de columna esperados en la hoja BD APU MTTO.
COL_CODIGO = "Codigo"
COL_DESC = "Descripción"
COL_UND = "Und"
COL_VR_UNIT = "Vr. Unitario"
COL_GRUPO = "Grupo"

# Grupos del warehouse que tiene sentido cruzar contra comparativos.
MATCHABLE_GROUPS = {"material", "mano de obra", "transporte", "equipo"}

# Segmentación del análisis en tres categorías de costo.
import re
import re as _re

def _cop(x) -> str:
    try:
        return "$" + f"{float(x):,.0f}".replace(",", ".")
    except Exception:
        return str(x)

_STRONG_UNITS = {"m3", "m2", "m", "kg", "gl", "lb", "in", "l", "ml_u"}

def _unit_canon(s) -> str:
    """Unidad canónica a partir de una celda de unidad. Devuelve '' para unidades
    no reconocibles o ambiguas (no se usan para filtrar)."""
    u = str(s or "").strip().lower()
    u = u.replace("³", "3").replace("²", "2").replace('"', "in")
    u = _re.sub(r"[^a-z0-9]", "", u)
    if u in ("m3", "mt3", "mts3"): return "m3"
    if u in ("m2", "mt2", "mts2"): return "m2"
    if u in ("m", "ml", "mt", "mts", "metro", "metros", "mlineal"): return "m"
    if u in ("kg", "kgs", "kilo", "kilos"): return "kg"
    if u in ("gr", "g", "gramo", "gramos"): return "gr"
    if u in ("ton", "tonelada", "toneladas", "tn"): return "ton"
    if u in ("gl", "gal", "galon", "galones"): return "gl"
    if u in ("lb", "lbs", "libra", "libras"): return "lb"
    if u in ("in", "pulg", "pulgada", "pulgadas"): return "in"
    if u in ("l", "lt", "lts", "litro", "litros"): return "l"
    if u in ("und", "un", "ud", "unidad", "unidades", "u", "c", "cu"): return "und"
    if u in ("caja", "cja", "cjs", "cajas"): return "caja"
    if u in ("bulto", "bto", "bultos"): return "bulto"
    if u in ("rollo", "rollos", "rll"): return "rollo"
    if u in ("kit", "juego", "jgo", "juegos"): return "kit"
    if u in ("par", "pares"): return "par"
    if u in ("hr", "hora", "horas", "h", "hh"): return "hr"
    if u in ("dia", "dias", "jornal", "jornales"): return "dia"
    if u in ("viaje", "viajes", "vje"): return "viaje"
    if u in ("ml_u", "cc", "mililitro", "mililitros"): return "ml_u"
    return ""  # global, %, lona, etc. -> ambiguo

def _detect_unit_in_text(text: str) -> str:
    """Detecta una unidad de medida fuerte dentro de una descripción (p.ej.
    'ARENA ... POR M3' -> m3)."""
    t = " " + _re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()) + " "
    for tok, canon in [(" m3 ", "m3"), (" m2 ", "m2"), (" kg ", "kg"),
                       (" gl ", "gl"), (" galon ", "gl"), (" lb ", "lb"),
                       (" ml ", "m"), (" mts ", "m"), (" m ", "m")]:
        if tok in t:
            return canon
    return ""

def _categoria(grupo) -> str:
    g = str(grupo).strip().lower()
    if g in ("material", "equipo"):
        return "Material"
    if g == "mano de obra":
        return "Mano de obra"
    if g == "transporte":
        return "Viáticos"
    return "Otro"


@dataclass
class PipelineResult:
    rows_warehouse: int = 0
    insumos_evaluados: int = 0
    cruces_validos: int = 0
    celdas_actualizadas: int = 0
    comparativos_filas: int = 0
    consolidado_filas: int = 0
    bucket: str = ""
    input_prefix: str = ""
    consolidado_prefix: str = ""
    report_uri: str = ""
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    stats: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def _to_float(x) -> Optional[float]:
    # Parser COP estricto y consistente con el de los comparativos.
    return to_number(x)


def _resolve_col(df, wanted: str) -> Optional[str]:
    """Encuentra el nombre real de una columna tolerando mayúsculas, tildes,
    espacios y puntuación. 'Vr Unitario' resuelve 'Vr. Unitario', 'VR UNITARIO ', etc."""
    import re as _re
    import unicodedata as _ud
    if wanted in df.columns:
        return wanted

    def norm(s):
        s = _ud.normalize("NFKD", str(s))
        s = "".join(c for c in s if not _ud.combining(c))
        return _re.sub(r"[^a-z0-9]", "", s.lower())

    w = norm(wanted)
    for c in df.columns:
        if norm(c) == w:
            return c
    for c in df.columns:  # coincidencia parcial como último recurso
        nc = norm(c)
        if nc and (w in nc or nc in w):
            return c
    return None


_UNIT_CANON = {"gl", "kg", "m", "l", "und", "in", "mm", "cm", "g", "ml", "paq", "cunete", "m2", "m3"}
# Señales de que una aparición es OTRA presentación (multi-cantidad / fracción).
_MULT_RE = re.compile(
    r"(\bx\s*\d+\b|\b\d+\s*(?:gl|gal|galones?|canecas?|cunetes?|cu[nñ]etes?|kg|kilos?|litros?)\b"
    r"|\bcaneca\b|\bcu[nñ]ete\b|\bdoble\b|\bmedi[ao]\b|\b\d+/\d+\b)"
)


def _canon_unit(u):
    """Unidad canónica (gl, kg, m, und, ...) a partir de un texto de unidad."""
    n = normalize(u)
    for t in n.split():
        if t in _UNIT_CANON:
            return t
    toks = n.split()
    return toks[0] if toks else ""


def _tiene_multiplicador(texto):
    return bool(_MULT_RE.search(normalize(texto)))


_GENERIC_DESCRIPTORS = {
    "blanco", "blanca", "negro", "negra", "gris", "azul", "rojo", "roja", "verde",
    "amarillo", "amarilla", "beige", "marron", "cafe", "dorado", "plateado", "cromado",
    "claro", "oscuro", "transparente", "natural", "grande", "pequeno", "mediano", "chico",
    "tipo", "standard", "estandar", "comun", "sencillo", "doble", "para", "con", "sin",
    "alta", "baja", "mate", "brillante", "liso", "lisa", "plano", "plana", "redondo",
    # descriptores de construcción frecuentes (NO son marca)
    "recta", "recto", "galv", "galvanizado", "galvanizada", "presion", "sanitaria",
    "sanitario", "cabeza", "lenteja", "punta", "broca", "rosca", "corrugado", "corrugada",
    "liviano", "liviana", "pesado", "pesada", "flexible", "rigido", "rigida", "macho",
    "hembra", "union", "codo", "tee", "reducida", "larga", "corta", "ancha", "angosta",
    "profesional", "industrial", "reforzado", "reforzada", "estructural",
}


def _ref_estandarizada(apariciones, wh_und, wh_desc, valor_wh_proj=None, max_ratio=3.0):
    """Referencia = MÁXIMO sin outliers, pero PRIORIZANDO por relevancia:
      T0: misma unidad + comparte token distintivo (marca/modelo) del WH + misma presentación
      T1: misma unidad + misma presentación
      T2: misma presentación (cualquier unidad)
      T3: todas
    Se usa el primer nivel NO vacío. Dentro del nivel: se prioriza cercanía a la BD,
    se quitan outliers y se toma el máximo.
    'apariciones' = lista de dicts {precio, unidad, descripcion}.
    Devuelve (ref, n_usadas, n_out, n_lejos, unidad_no_coincide, marca_no_coincide)."""
    import statistics
    aps = [a for a in (apariciones or []) if a.get("precio") and float(a["precio"]) > 0]
    if not aps:
        return None, 0, 0, 0, False, False

    wh_u = _canon_unit(wh_und)
    wh_toks = [t for t in normalize(wh_desc).split() if len(t) >= 4]
    # tokens distintivos = marca/modelo: se excluyen el sustantivo cabeza (primero) y
    # los descriptores genéricos (colores, adjetivos), que no identifican la marca.
    distintivos = {t for t in wh_toks[1:] if t not in _GENERIC_DESCRIPTORS}
    wh_mult = _tiene_multiplicador(wh_desc)

    for a in aps:
        au = _canon_unit(a.get("unidad", ""))
        a_toks = set(normalize(a.get("descripcion", "")).split())
        a["_unit_ok"] = bool(wh_u) and au == wh_u
        a["_token_ok"] = bool(distintivos) and bool(distintivos & a_toks)
        a["_pres_ok"] = (_tiene_multiplicador(a.get("descripcion", "")) == wh_mult)

    def _tier(pred):
        return [a for a in aps if pred(a)]

    t0 = _tier(lambda a: a["_unit_ok"] and a["_token_ok"] and a["_pres_ok"])
    t1 = _tier(lambda a: a["_unit_ok"] and a["_pres_ok"])
    t2 = _tier(lambda a: a["_pres_ok"])
    if t0:
        usados_ap = t0
    elif t1:
        usados_ap = t1
    elif t2:
        usados_ap = t2
    else:
        usados_ap = aps

    unidad_no_coincide = not any(a["_unit_ok"] for a in usados_ap)
    marca_no_coincide = bool(distintivos) and not any(a["_token_ok"] for a in usados_ap)

    ps = sorted(float(a["precio"]) for a in usados_ap)
    usados = ps
    n_out = 0
    if len(ps) >= 4:
        q1 = statistics.quantiles(ps, n=4)[0]
        q3 = statistics.quantiles(ps, n=4)[2]
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        filt = [p for p in ps if lo <= p <= hi]
        if filt:
            n_out = len(ps) - len(filt)
            usados = filt
    n_lejos = 0
    if valor_wh_proj and max_ratio and float(valor_wh_proj) > 0:
        lo_b = float(valor_wh_proj) / float(max_ratio)
        hi_b = float(valor_wh_proj) * float(max_ratio)
        cerca = [p for p in usados if lo_b <= p <= hi_b]
        if cerca:
            n_lejos = len(usados) - len(cerca)
            usados = cerca
    ref = round(max(usados), 2)
    return ref, len(usados), n_out, n_lejos, unidad_no_coincide, marca_no_coincide


def _ref_maximo_sin_outliers(precios, valor_wh_proj=None, max_ratio=3.0):
    """Precio de referencia ÚNICO: el MÁXIMO de las apariciones quitando OUTLIERS
    estadísticos (IQR) y priorizando los valores cercanos a la BD. Nunca deja el
    ítem sin referencia si hay al menos una aparición (si todas quedan lejos de la
    BD, las usa igual en vez de descartarlas).
    Devuelve (referencia, n_usadas, n_outliers, n_lejos_bd) o (None, 0, 0, 0)."""
    import statistics
    ps = sorted(float(p) for p in (precios or []) if p is not None and float(p) > 0)
    if not ps:
        return None, 0, 0, 0
    usados = ps
    n_outliers = 0
    if len(ps) >= 4:
        q1 = statistics.quantiles(ps, n=4)[0]
        q3 = statistics.quantiles(ps, n=4)[2]
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        filt = [p for p in ps if lo <= p <= hi]
        if filt:
            n_outliers = len(ps) - len(filt)
            usados = filt
    n_lejos = 0
    if valor_wh_proj and max_ratio and float(valor_wh_proj) > 0:
        lo_b = float(valor_wh_proj) / float(max_ratio)
        hi_b = float(valor_wh_proj) * float(max_ratio)
        cerca = [p for p in usados if lo_b <= p <= hi_b]
        if cerca:  # prioriza cercanos; si NINGUNO queda cerca, conserva todos
            n_lejos = len(usados) - len(cerca)
            usados = cerca
    ref = round(max(usados), 2)   # MÁXIMO de las apariciones (ya sin outliers)
    return ref, len(usados), n_outliers, n_lejos


def _investigar_web(gemini_price, desc, und, valor_wh_proj, settings, contexto=""):
    """Ejecuta la búsqueda de precio en internet con Gemini y devuelve un dict con
    los campos de referencia web, o None si no hay precio creíble. Reutilizable en
    el fallback (sin datos internos) y tras el descarte por magnitud."""
    pr = gemini_price.research_price(
        desc, und, referencia_bd=valor_wh_proj,
        resolve_links=settings.gemini_resolve_links,
    )
    if (pr is None or not pr.precio or pr.precio <= 0
            or pr.confianza < settings.gemini_price_min_confidence):
        return None
    precio = round(float(pr.precio), 2)
    unidad_txt = f" / {pr.unidad}" if pr.unidad else ""
    prod_txt = f" Producto hallado: \"{pr.producto}\"." if pr.producto else ""
    calc_txt = f" Escalado por unidad: {pr.calculo}." if (pr.escalado and pr.calculo) else ""
    de_donde = (
        f"{contexto}Precio de referencia hallado en internet por Gemini: "
        f"{_cop(precio)}{unidad_txt} (confianza {pr.confianza:.0f}).{prod_txt}{calc_txt} "
        f"Fuente: {pr.fuente_nombre or 's/d'} ({pr.fuente_url or 's/d'})."
        + (f" Nota: {pr.notas}" if pr.notas else "")
    )
    return {
        "precio_ref": precio,
        "tipo_ref": f"Investigación web (Gemini){(' · unidad ' + pr.unidad) if pr.unidad else ''}",
        "fuente_ref": pr.fuente_nombre or "Referencia web",
        "link_ref": pr.fuente_url or "",
        "region_ref": "Internet",
        "prov_ref": pr.fuente_nombre or "",
        "de_donde": de_donde,
    }


def run_pipeline(settings: Settings, storage_client=None, progress=None) -> PipelineResult:
    settings.validate()
    result = PipelineResult(dry_run=settings.dry_run, started_at=dt.datetime.utcnow().isoformat())
    result.bucket = settings.gcs_bucket_name
    result.input_prefix = settings.gcs_input_prefix
    result.consolidado_prefix = settings.gcs_consolidado_prefix

    # --- 1. Warehouse ---
    sheet = WarehouseSheet(
        sheet_url=settings.warehouse_sheet_url,
        tab=settings.warehouse_tab,
        header_row=settings.warehouse_header_row,
        sa_key_json=settings.gcp_sa_key,
        sa_key_path=settings.google_app_credentials,
    )
    wh = sheet.read()
    result.rows_warehouse = len(wh)
    if wh.empty:
        result.errors.append("Warehouse vacío o encabezado mal ubicado.")
        result.finished_at = dt.datetime.utcnow().isoformat()
        return result

    # Insumos evaluables: filas con descripción, valor y grupo cruzable.
    C_COD = _resolve_col(wh, settings.wh_col_codigo)
    C_DESC = _resolve_col(wh, settings.wh_col_desc)
    C_UND = _resolve_col(wh, settings.wh_col_und)
    C_PRECIO = _resolve_col(wh, settings.wh_col_precio)
    C_GRUPO = _resolve_col(wh, settings.wh_col_grupo)
    C_CLASIF = _resolve_col(wh, settings.wh_col_clasificacion)
    if C_DESC is None or C_PRECIO is None or C_GRUPO is None:
        result.errors.append(
            "No encuentro columnas del warehouse. "
            f"Buscaba descripción='{settings.wh_col_desc}', precio='{settings.wh_col_precio}', "
            f"grupo='{settings.wh_col_grupo}'. Columnas disponibles: {list(wh.columns)}. "
            "Revisa WAREHOUSE_TAB/WAREHOUSE_HEADER_ROW y las variables WH_COL_*."
        )
        result.finished_at = dt.datetime.utcnow().isoformat()
        return result

    wh["_grupo_norm"] = wh[C_GRUPO].astype(str).str.strip().str.lower()
    wh["_valor"] = wh[C_PRECIO].map(_to_float)
    # Aparecen TODOS los ítems del warehouse Activa/Duplicado (con nombre). El
    # grupo ya NO excluye: los grupos no procesables (Contrato/Administración) se
    # incluyen igual pero conservan su valor del warehouse (no se cruzan).
    mask = (
        wh[C_DESC].astype(str).str.strip().ne("")
    )
    # Solo las actividades cuya Clasificación esté en la lista a incluir (por
    # defecto Activa y Duplicado). Inactiva/Reubicar no se actualizan ni cuentan.
    if settings.only_active and C_CLASIF is not None:
        incluir = {c.strip().lower() for c in str(settings.wh_clasif_incluir).split(",") if c.strip()}
        clasif_norm = wh[C_CLASIF].astype(str).str.strip().str.lower()
        mask = mask & clasif_norm.isin(incluir)
    insumos = wh[mask].copy()
    result.insumos_evaluados = len(insumos)

    # Distribución por Clasificación sobre TODO el warehouse (filas con descripción
    # real), para el resumen del reporte: Activa / Inactiva / Duplicado / vacío /
    # Reubicar. Sirve de contexto ("de las N Activas, actualizables M").
    clasif_distribucion: dict[str, int] = {}
    if C_CLASIF is not None:
        _has_desc = wh[C_DESC].astype(str).str.strip().ne("")
        _vc = (
            wh.loc[_has_desc, C_CLASIF]
            .astype(str).str.strip().replace("", "(vacío)")
            .value_counts()
        )
        clasif_distribucion = {str(k): int(v) for k, v in _vc.items()}

    # --- 2. Comparativos (cotizaciones) + Consolidado (gasto real) ---
    try:
        comparativos = load_from_gcs(
            settings.gcs_bucket_name,
            settings.gcs_input_prefix,
            settings.comparativos_config_path,
            storage_client=storage_client,
            progress=progress,
        )
    except Exception as exc:
        comparativos = pd.DataFrame()
        result.errors.append(f"Comparativos no cargados: {exc}")
    try:
        from .consolidado_loader import load_consolidado_from_gcs

        consolidado = load_consolidado_from_gcs(
            settings.gcs_bucket_name,
            settings.gcs_consolidado_prefix,
            storage_client=storage_client,
            progress=progress,
        )
    except Exception as exc:
        consolidado = pd.DataFrame()
        result.errors.append(f"Consolidado no cargado: {exc}")

    # Unifica ambas fuentes en un solo catálogo tidy (mismo esquema).
    base_cols = ["region", "proveedor", "descripcion", "unidad", "precio",
                 "archivo", "formato", "fuente_tipo", "gcs_path", "columna_precio", "cantidad"]
    parts = []
    for d in (comparativos, consolidado):
        if d is not None and not d.empty:
            for c in base_cols:
                if c not in d.columns:
                    d[c] = ""
            parts.append(d[base_cols])
    comparativos = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=base_cols)
    result.comparativos_filas = len(comparativos)
    result.consolidado_filas = int(len(consolidado)) if consolidado is not None and not consolidado.empty else 0

    # Diagnóstico explícito de fuentes vacías (causa típica: archivos no subidos
    # o prefijo/bucket equivocado).
    b = settings.gcs_bucket_name
    if comparativos.empty:
        result.errors.append(
            f"No se encontraron precios. Verifica que existan archivos en "
            f"gs://{b}/{settings.gcs_input_prefix} (comparativos) y "
            f"gs://{b}/{settings.gcs_consolidado_prefix} (consolidado)."
        )

    # --- 3. IPC sobre comparativos (opcional) ---
    if not comparativos.empty and settings.apply_ipc_to_comparativos:
        comparativos["precio"] = comparativos["precio"].map(
            lambda v: apply_ipc(v, settings.ipc_variation, True)
        )

    # --- 4. Matcher sobre el catálogo de comparativos ---
    catalog = []
    if not comparativos.empty:
        catalog = list(
            comparativos[["descripcion", "unidad"]]
            .drop_duplicates()
            .itertuples(index=False, name=None)
        )
    matcher = ItemMatcher(
        catalog,
        fuzzy_threshold=settings.fuzzy_threshold,
        use_embeddings=settings.use_embeddings,
        embedding_model=settings.embedding_model,
        embedding_threshold=settings.embedding_threshold,
    )

    # Resolutor opcional con Gemini para cruces en zona dudosa.
    gemini = None
    if settings.use_gemini and not comparativos.empty:
        from .gemini_mapper import GeminiResolver

        gemini = GeminiResolver(
            project=settings.gcp_project_id,
            location=settings.gemini_location,
            model_name=settings.gemini_model,
            api_key=settings.gemini_api_key or None,
        )
        if not gemini.enabled:
            gemini = None

    # Investigador opcional de precio en internet (fallback cuando NO hay fuente
    # interna que refute). Usa grounding de Google Search vía Vertex AI.
    gemini_price = None
    if settings.use_gemini_price_research:
        from .gemini_mapper import GeminiPriceResearcher

        gemini_price = GeminiPriceResearcher(
            project=settings.gcp_project_id,
            location=settings.gemini_location,
            model_name=settings.gemini_model,
            api_key=settings.gemini_api_key or None,
        )
        if not gemini_price.enabled:
            gemini_price = None
    n_price_research = 0  # ítems ya investigados en esta corrida (tope de costo)

    # Índice por ítem normalizado; conserva el tipo de fuente para separar
    # cotizaciones (comparativos) del gasto real (Consolidado).
    comp = comparativos.copy()
    if not comp.empty:
        comp["item_norm"] = comp["descripcion"].map(normalize)

    def _gcs_link(gpath: str) -> Optional[str]:
        gpath = gpath or ""
        if not gpath:
            return None
        return (
            f"https://console.cloud.google.com/storage/browser/_details/"
            f"{settings.gcs_bucket_name}/{gpath}"
        )

    records = []
    consolidado_por_planta = []  # filas insumo x planta para el reporte
    updates_price: dict[int, float] = {}
    updates_year: dict[int, int] = {}
    base_year = int(settings.update_year) if str(settings.update_year).strip() else dt.date.today().year
    siguiente = str(settings.update_mode).strip().lower() == "siguiente"
    anio_objetivo = base_year + 1 if siguiente else base_year
    if progress is not None:
        progress["fase"] = "Cruzando ítems y consultando precios…"
        progress["insumos_total"] = int(len(insumos))
        progress["insumos_procesados"] = 0
        progress["gemini_consultados"] = 0
        progress["gemini_max"] = int(settings.gemini_price_max_items) if settings.use_gemini_price_research else 0
    for _i, (_, row) in enumerate(insumos.iterrows()):
        if progress is not None:
            progress["insumos_procesados"] = _i + 1
            progress["gemini_consultados"] = n_price_research
        desc = str(row.get(C_DESC, "")).strip()
        und = str(row.get(C_UND, "")).strip()
        categoria = _categoria(row.get(C_GRUPO, ""))
        valor_wh = row["_valor"]
        valor_wh = 0.0 if (valor_wh is None or pd.isna(valor_wh)) else float(valor_wh)
        # Factor de proyección: al año actual = 1 (sin proyectar); al año siguiente,
        # material/viáticos usan IPC y mano de obra el incremento del salario mínimo.
        if siguiente:
            factor_proj = (1 + settings.smlv_increase) if categoria == "Mano de obra" else (1 + settings.ipc_variation)
        else:
            factor_proj = 1.0
        valor_wh_proj = round(valor_wh * factor_proj, 2)
        valor_wh_ipc = valor_wh_proj  # alias para guardia de magnitud y diferencias

        es_procesable = str(row.get(C_GRUPO, "")).strip().lower() in MATCHABLE_GROUPS

        m = matcher.match(desc, und)
        metodo = m.method
        score = m.score
        candidato = m.candidate_raw
        cand_norm = m.candidate_norm if (m.matched and es_procesable) else None
        if not es_procesable:
            # Grupo no procesable (p.ej. Contrato/Administración): aparece en el
            # reporte pero conserva su valor del warehouse (no se cruza ni se busca).
            candidato = ""
            metodo = "no procesable (grupo)"
            score = 0.0

        # Segunda pasada con Gemini SOLO si el fuzzy quedó en zona dudosa.
        if (
            es_procesable
            and gemini is not None
            and not m.matched
            and settings.gemini_min_score <= m.score < settings.fuzzy_threshold
        ):
            cands = matcher.top_candidates(desc, k=settings.gemini_max_candidates)
            choice = gemini.resolve(desc, und, cands)
            if choice and choice.index is not None and choice.confidence >= settings.gemini_min_confidence:
                chosen = cands[choice.index]
                cand_norm = chosen["norm"]
                candidato = chosen["raw"]
                score = choice.confidence
                metodo = "gemini"

        matched = cand_norm is not None
        # Conjunto de variantes del catálogo que se AGREGAN (promedio/consumo).
        # Solo variantes con score alto (agg_min_score), para no sobre-agrupar
        # términos genéricos como 'Oficial' que juntarían cientos de líneas distintas.
        match_norms = set()
        if matched:
            # El candidato principal YA pasó TODAS las guardias semánticas
            # (cabeza/medida/número/genérico) dentro de match(); por tanto es un
            # cruce vetado y SIEMPRE aporta su precio, aunque su score esté en
            # [fuzzy_threshold, agg_min_score). El umbral agg_min_score se reserva
            # para las VARIANTES ADICIONALES que se agrupan, evitando sobre-agrupar
            # términos genéricos (p.ej. 'Oficial' juntando cientos de roles).
            match_norms.add(cand_norm)
            for v in matcher.match_many(desc, und, limit=15, min_score=settings.agg_min_score):
                match_norms.add(v["norm"])
        # --- Agregación separada por fuente (se usa PROMEDIO) ---
        precio_comp_prom = precio_comp_min = precio_comp_med = precio_comp_max = n_comp = None
        region_comp = prov_comp = link_comp = arch_comp = col_comp = None
        region_comp_max = prov_comp_max = arch_comp_max = precio_comp_max_audit = None
        precio_cons_prom = precio_cons_med = precio_cons_min = precio_cons_max = n_cons = None
        link_cons = arch_cons = None
        todas_las_fuentes = None
        apariciones_consolidado = None
        cantidad_consumo = None
        lo_band = hi_band = None
        n_con_fuera = n_cot_fuera = 0
        con_all = cot_full = comp.iloc[0:0]  # vacíos por defecto (si no hay match)

        if matched and not comp.empty:
            grp = comp[comp["item_norm"].isin(match_norms)].copy()

            # (1) Filtro por unidad: si el insumo tiene una unidad reconocible, se
            # restringe a registros con esa MISMA unidad (los que tengan una unidad
            # distinta y conocida se descartan; los sin unidad se conservan). Evita
            # mezclar precios de unidades distintas (p.ej. 200 vs 40.000).
            wh_u = _unit_canon(und)
            if wh_u and not grp.empty:
                def _ru(r):
                    return _unit_canon(r.get("unidad")) or _detect_unit_in_text(r.get("descripcion"))
                grp["_u"] = grp.apply(_ru, axis=1)
                # registros con unidad conocida distinta -> fuera; iguales o sin unidad -> ok
                same = grp[(grp["_u"] == wh_u) | (grp["_u"] == "") | (grp["_u"].isna())]
                if not same.empty:
                    grp = same

            # Separación por fuente. El CONSOLIDADO completo se usa para mostrar
            # TODAS las fuentes (dispersión) y para el consumo; pero para la
            # REFERENCIA y los promedios se restringe a una banda de cordura
            # alrededor de la BD [BD/ratio, BD·ratio], que descarta valores
            # absurdos (p.ej. 'Ayudante' a 200.000 o 2.000) sin botar líneas
            # legítimas por su redacción. Para material con valores altos cercanos
            # a la BD (Transporte) la banda es amplia y los conserva.
            con_all = grp[grp["fuente_tipo"] == "Gasto real (Consolidado)"].copy()
            cot_full = grp[grp["fuente_tipo"] != "Gasto real (Consolidado)"].copy()

            # Banda de cordura alrededor de la BD [BD/ratio, BD·ratio]. Se aplica a
            # AMBAS fuentes (consolidado y comparativo) para los cálculos de
            # referencia y promedios. Descarta valores absurdos tanto ALTOS (p.ej.
            # una factura del kit/presentación completa a ~$1.000.000 frente a un
            # precio por Kg de $52.000) como BAJOS (p.ej. un comparativo mal
            # parseado a $9). CLAVE: si TODOS los registros de una fuente caen fuera
            # de la banda, esa fuente queda VACÍA; NO se hace fallback a usar los
            # valores absurdos (antes el consolidado caía de nuevo en con_all).
            # Se CONSERVAN TODAS las apariciones (no se excluye ninguna por banda de
            # cordura). La banda [BD/ratio, BD·ratio] solo se usa para PRIORIZAR las
            # cercanas a la BD dentro del promedio de referencia (ver más abajo), no
            # para descartar líneas. lo_band/hi_band se calculan para esa priorización
            # y para mostrar el rango en el reporte.
            lo_band = hi_band = None
            if valor_wh_proj:
                lo_band = valor_wh_proj / settings.max_price_ratio
                hi_band = valor_wh_proj * settings.max_price_ratio

            con = con_all          # todas las apariciones del consolidado
            cot = cot_full         # todas las apariciones de comparativos
            n_con_fuera = 0        # ya no se excluye nada por banda
            n_cot_fuera = 0

            # (3) Detalle de fuentes consultadas DENTRO de la banda de cordura (las
            # realmente consideradas). Las que quedaron fuera de rango se omiten del
            # detalle y se reportan como un conteo, para no contaminar el reporte con
            # valores absurdos pero dejando rastro de que existían.
            fuentes = []
            grp_sorted = pd.concat([con, cot]).sort_values("precio") if (not con.empty or not cot.empty) else grp.iloc[0:0]
            for _, fr in grp_sorted.head(120).iterrows():
                fuentes.append(
                    f"{fr.get('archivo','')} [{fr.get('region','')}"
                    f"{('/'+str(fr.get('proveedor'))) if fr.get('proveedor') else ''}]: "
                    f"{_cop(fr.get('precio'))}"
                )
            if len(grp_sorted) > 120:
                fuentes.append(f"(+{len(grp_sorted) - 120} fuentes más)")
            if (n_con_fuera + n_cot_fuera) > 0:
                fuentes.append(
                    f"(+{n_con_fuera + n_cot_fuera} fuera del rango de cordura "
                    f"[{_cop(lo_band)} , {_cop(hi_band)}], omitidas)"
                )
            todas_las_fuentes = " ; ".join(fuentes) if fuentes else None

            # Desglose COMPLETO del consolidado (todas las apariciones/facturas que
            # alimentaron el cálculo), sin que los comparativos las tapen. Cada línea:
            # [región/proveedor] descripción: $precio.
            if not con_all.empty:
                cs = con_all.sort_values("precio")
                lineas_c = []
                for _, fr in cs.head(400).iterrows():
                    reg = str(fr.get("region", "") or "").strip()
                    prov = str(fr.get("proveedor", "") or "").strip()
                    dsc = str(fr.get("descripcion", "") or "").strip()
                    etiqueta = f"[{reg}{('/' + prov) if prov else ''}]"
                    dtxt = f" {dsc[:45]}" if dsc else ""
                    lineas_c.append(f"{etiqueta}{dtxt}: {_cop(fr.get('precio'))}")
                if len(cs) > 400:
                    lineas_c.append(f"(+{len(cs) - 400} apariciones más)")
                apariciones_consolidado = " ; ".join(lineas_c) if lineas_c else None

            if not cot.empty:
                rmin = cot.loc[cot["precio"].idxmin()]
                rmax = cot.loc[cot["precio"].idxmax()]
                precio_comp_prom = round(float(cot["precio"].mean()), 2)
                precio_comp_min = round(float(cot["precio"].min()), 2)
                precio_comp_med = round(float(cot["precio"].median()), 2)
                precio_comp_max = round(float(cot["precio"].max()), 2)  # máximo DESPUÉS del filtro
                n_comp = int(len(cot))
                region_comp = rmin["region"]; prov_comp = rmin.get("proveedor", "")
                region_comp_max = rmax["region"]; prov_comp_max = rmax.get("proveedor", "")
                arch_comp = rmin.get("archivo", ""); col_comp = rmin.get("columna_precio", "")
                arch_comp_max = rmax.get("archivo", "")
                link_comp = _gcs_link(rmin.get("gcs_path", ""))

            # Consumo total real: suma de Cantidad de TODAS las facturas del
            # consolidado para este insumo (con_all), independiente de la banda de
            # cordura de precio (el consumo es real aunque algún precio esté en otra
            # escala/alcance y se descarte para la referencia).
            if not con_all.empty and "cantidad" in con_all.columns:
                qsum_all = pd.to_numeric(con_all["cantidad"], errors="coerce").dropna()
                if not qsum_all.empty and float(qsum_all.sum()) > 0:
                    cantidad_consumo = round(float(qsum_all.sum()), 2)

            if not con.empty:
                precio_cons_prom = round(float(con["precio"].mean()), 2)
                precio_cons_med = round(float(con["precio"].median()), 2)
                precio_cons_min = round(float(con["precio"].min()), 2)
                precio_cons_max = round(float(con["precio"].max()), 2)
                n_cons = int(len(con))
                arch_cons = con.iloc[0].get("archivo", "")
                link_cons = _gcs_link(con.iloc[0].get("gcs_path", ""))
                for reg, sub in con.groupby("region"):
                    consolidado_por_planta.append({
                        "codigo": row.get(C_COD, ""),
                        "insumo": desc,
                        "categoria": _categoria(row.get(C_GRUPO, "")),
                        "planta_region": reg,
                        "precio_real_promedio": round(float(sub["precio"].mean()), 2),
                        "precio_real_mediana": round(float(sub["precio"].median()), 2),
                        "n_facturas": int(len(sub)),
                        "precio_real_min": round(float(sub["precio"].min()), 2),
                        "precio_real_max": round(float(sub["precio"].max()), 2),
                    })

        # Precio de referencia estandarizado: MÁXIMO sin outliers, priorizando por
        # UNIDAD, MARCA/modelo y misma PRESENTACIÓN, y cercanía a la BD. Si no hay
        # apariciones, o el cruce es de otra marca, se va a la búsqueda web (Gemini).
        ref_es_web = False
        web_intentado = False
        apariciones = []
        for df in (con_all, cot_full):
            if not df.empty:
                for _, ar in df.iterrows():
                    apariciones.append({
                        "precio": ar.get("precio"),
                        "unidad": ar.get("unidad", ""),
                        "descripcion": ar.get("descripcion", ""),
                    })
        ref_val, n_usadas, n_out, n_lejos, unidad_no_coincide, marca_no_coincide = _ref_estandarizada(
            apariciones, und, desc, valor_wh_proj, settings.max_price_ratio
        )
        # Ruteo por marca a Gemini SOLO cuando importa: el WH tiene marca distintiva,
        # ninguna aparición la comparte, Y la referencia interna está LEJOS de la BD
        # (si estuviera cerca, el precio es razonable y no vale la pena arriesgar). Es
        # NO DESTRUCTIVO: si Gemini no encuentra el producto, se conserva el interno.
        ref_lejos_bd = False
        if ref_val is not None and valor_wh_proj and valor_wh_proj > 0:
            rr = ref_val / valor_wh_proj
            umbral = 1.0 + settings.suspicious_pct_threshold / 100.0
            ref_lejos_bd = rr > umbral or rr < 1.0 / umbral
        usar_web = None
        if (
            ref_val is not None and marca_no_coincide and ref_lejos_bd
            and settings.prefer_web_on_brand_mismatch and es_procesable
            and gemini_price is not None
            and (settings.gemini_price_max_items <= 0
                 or n_price_research < settings.gemini_price_max_items)
        ):
            web_intentado = True
            n_price_research += 1
            usar_web = _investigar_web(gemini_price, desc, und, valor_wh_proj, settings,
                                       contexto="Cruce interno de otra marca. ")

        if usar_web is not None:
            precio_ref = usar_web["precio_ref"]; ref_es_web = True
            tipo_ref = usar_web["tipo_ref"]; fuente_ref = usar_web["fuente_ref"]
            link_ref = usar_web["link_ref"]; region_ref = usar_web["region_ref"]
            prov_ref = usar_web["prov_ref"]; de_donde = usar_web["de_donde"]
        elif ref_val is not None:
            precio_ref = ref_val
            # Fuente/enlace: la aparición cuyo precio queda MÁS CERCA de la referencia.
            fuente_ref = link_ref = region_ref = prov_ref = None
            try:
                pool_df = pd.concat([df for df in (con_all, cot_full) if not df.empty])
                fr = pool_df.iloc[(pool_df["precio"] - precio_ref).abs().argsort().iloc[0]]
                fuente_ref = fr.get("archivo", "")
                link_ref = _gcs_link(fr.get("gcs_path", ""))
                region_ref = fr.get("region", "")
                prov_ref = fr.get("proveedor", "")
            except Exception:
                pass
            tipo_ref = "Máximo sin outliers (prioriza unidad, marca y cercanía a la BD)"
            detalle = []
            if unidad_no_coincide:
                detalle.append("sin apariciones de la misma unidad (se usó lo disponible)")
            if n_out:
                detalle.append(f"{n_out} outlier(s) estadístico(s) excluido(s)")
            if n_lejos:
                detalle.append(f"{n_lejos} alejado(s) de la BD despriorizado(s)")
            extra = (" Ajustes: " + "; ".join(detalle) + "." if detalle else "")
            # Desglose por fuente (como en el reporte original): cuántas facturas del
            # consolidado y cuántas cotizaciones, con su prom/máx/mín.
            desglose = []
            if not con_all.empty:
                cp = pd.to_numeric(con_all["precio"], errors="coerce").dropna()
                n_pl = con_all["region"].nunique() if "region" in con_all.columns else None
                pl_txt = f" en {n_pl} planta(s)" if n_pl else ""
                if len(cp):
                    desglose.append(
                        f"Consolidado (gasto real): {len(cp)} factura(s){pl_txt} "
                        f"(prom {_cop(round(cp.mean(),2))}, máx {_cop(round(cp.max(),2))}, "
                        f"mín {_cop(round(cp.min(),2))})"
                    )
            if not cot_full.empty:
                qp = pd.to_numeric(cot_full["precio"], errors="coerce").dropna()
                if len(qp):
                    desglose.append(
                        f"Comparativos: {len(qp)} cotización(es) "
                        f"(prom {_cop(round(qp.mean(),2))}, máx {_cop(round(qp.max(),2))}, "
                        f"mín {_cop(round(qp.min(),2))})"
                    )
            desglose_txt = (" Desglose: " + "; ".join(desglose) + "." if desglose else "")
            marca_txt = (" Nota: posible otra marca (sin coincidencia de marca en fuentes internas); "
                         "se conserva el interno." if marca_no_coincide else "")
            de_donde = (
                f"Máximo de {n_usadas} aparición(es) (consolidado + comparativos), "
                f"sin outliers y priorizando cercanas a la BD ({_cop(valor_wh_proj)}): "
                f"{_cop(precio_ref)}.{extra}{desglose_txt}{marca_txt}"
            )
        else:
            precio_ref = None
            fuente_ref = tipo_ref = link_ref = region_ref = prov_ref = None
            de_donde = "Sin fuente que refute (se mantiene valor del warehouse × IPC)"

            # --- FALLBACK: investigación de precio en internet con Gemini ---
            # Sin ninguna referencia interna: buscar en internet.
            if (
                es_procesable and not web_intentado
                and gemini_price is not None
                and (settings.gemini_price_max_items <= 0
                     or n_price_research < settings.gemini_price_max_items)
            ):
                web_intentado = True
                n_price_research += 1
                wr = _investigar_web(gemini_price, desc, und, valor_wh_proj, settings,
                                     contexto="Sin fuente interna utilizable. ")
                if wr is not None:
                    precio_ref = wr["precio_ref"]; ref_es_web = True
                    tipo_ref = wr["tipo_ref"]; fuente_ref = wr["fuente_ref"]
                    link_ref = wr["link_ref"]; region_ref = wr["region_ref"]
                    prov_ref = wr["prov_ref"]; de_donde = wr["de_donde"]

        # (La referencia ya usa solo el consolidado cuando existe, así que no hay
        # mezcla de fuentes que reconciliar.)

        # Guardia de cordura por magnitud (SUAVIZADA): antes se borraba toda
        # referencia fuera de [BD/ratio, BD·ratio]; eso dejaba sin fuente a ítems que
        # cruzaban perfecto (score 100) solo porque el máximo se alejaba de la BD.
        # Ahora SOLO se descarta cuando el match NO es confiable (score bajo) Y la
        # relación es extrema (probable cruce equivocado de unidad/alcance). Los
        # matches confiables lejos de la BD NO se borran: pasan por el control de
        # sospechoso + arbitraje de Gemini (que decide si tiene razón el máximo o la BD).
        descartado_magnitud = False
        if precio_ref is not None and valor_wh_proj and not ref_es_web:
            ratio = precio_ref / valor_wh_proj
            extremo = ratio > settings.extreme_ratio or ratio < 1.0 / settings.extreme_ratio
            if extremo and score < settings.high_score_for_extreme:
                descartado_magnitud = True
                de_donde = (
                    f"DESCARTADO: referencia {_cop(precio_ref)} se aleja {ratio:.1f}x de la BD "
                    f"({_cop(valor_wh_proj)}) y el match no es confiable (score {score:.0f} < "
                    f"{int(settings.high_score_for_extreme)}); probable cruce equivocado."
                )
            if descartado_magnitud:
                precio_ref = None
                fuente_ref = tipo_ref = link_ref = None

        # --- FALLBACK web tras magnitud ---: si el ítem quedó SIN referencia usable
        # (descartado por magnitud o el máximo se salió) y aún no se buscó en internet,
        # se busca ahora con Gemini. Así los ítems que sí existen en el mercado
        # (Sikafloor, Sanitario Corona, Teja Eternit, etc.) no quedan sin precio.
        if (
            precio_ref is None and es_procesable and not web_intentado
            and gemini_price is not None
            and (settings.gemini_price_max_items <= 0
                 or n_price_research < settings.gemini_price_max_items)
        ):
            web_intentado = True
            n_price_research += 1
            ctx = ("Referencia interna descartada por magnitud. " if descartado_magnitud
                   else "Sin fuente interna utilizable. ")
            wr = _investigar_web(gemini_price, desc, und, valor_wh_proj, settings, contexto=ctx)
            if wr is not None:
                precio_ref = wr["precio_ref"]; ref_es_web = True
                tipo_ref = wr["tipo_ref"]; fuente_ref = wr["fuente_ref"]
                link_ref = wr["link_ref"]; region_ref = wr["region_ref"]
                prov_ref = wr["prov_ref"]; de_donde = wr["de_donde"]

        diferencia_vs_ipc = pct_diferencia = por_encima_ipc = None
        ahorro_ponderado = None
        sospechoso_pct = False
        qty = cantidad_consumo if (cantidad_consumo and cantidad_consumo > 0) else 1.0
        if precio_ref is not None and valor_wh_proj:
            ref_proj = round(precio_ref * factor_proj, 2)
            diferencia_vs_ipc = round(valor_wh_proj - ref_proj, 2)  # + = ahorro por unidad
            pct_diferencia = round((diferencia_vs_ipc / valor_wh_proj) * 100, 1)
            por_encima_ipc = ref_proj > valor_wh_proj
            sospechoso_pct = abs(pct_diferencia) > settings.suspicious_pct_threshold
            # Ahorro/sobrecosto ponderado por el consumo anual real.
            ahorro_ponderado = round(diferencia_vs_ipc * qty, 2)

            # --- ARBITRAJE con Gemini cuando el PROMEDIO interno es sospechoso ---
            # Si el promedio quedó marcado (>umbral vs BD), se consulta un precio de
            # mercado en internet y se decide quién tiene razón: gana el valor (PROMEDIO
            # o WAREHOUSE) que esté MÁS CERCA del precio web independiente.
            if (
                sospechoso_pct and not ref_es_web and precio_ref is not None
                and gemini_price is not None and settings.gemini_arbitrate_suspicious
                and (settings.gemini_price_max_items <= 0
                     or n_price_research < settings.gemini_price_max_items)
            ):
                pr = gemini_price.research_price(
                    desc, und, referencia_bd=valor_wh_proj,
                    resolve_links=settings.gemini_resolve_links,
                )
                n_price_research += 1
                if (pr is not None and pr.precio and pr.precio > 0
                        and pr.confianza >= settings.gemini_price_min_confidence):
                    web = float(pr.precio)
                    d_avg = abs(float(precio_ref) - web)
                    d_wh = abs(float(valor_wh or 0) - web)
                    prod_txt = f" Producto: \"{pr.producto}\"." if pr.producto else ""
                    if pr.escalado and pr.calculo:
                        prod_txt += f" Escalado por unidad: {pr.calculo}."
                    link_web = pr.fuente_url or ""
                    unidad_txt = f" / {pr.unidad}" if pr.unidad else ""
                    if d_avg <= d_wh:
                        # El máximo está más cerca del mercado: Gemini lo respalda.
                        sospechoso_pct = False
                        tipo_ref = (tipo_ref or "Máximo") + " · verificado por Gemini (respalda el máximo)"
                        if link_web:
                            link_ref = link_web
                            fuente_ref = pr.fuente_nombre or fuente_ref
                        de_donde += (
                            f" Verificación web (Gemini): mercado {_cop(round(web, 2))}{unidad_txt} "
                            f"(confianza {pr.confianza:.0f}) más cercano al MÁXIMO que a la BD; "
                            f"se adopta el máximo.{prod_txt} "
                            f"Fuente: {pr.fuente_nombre or 's/d'} ({link_web or 's/d'})."
                        )
                    else:
                        # La BD está más cerca del mercado: se mantiene el warehouse.
                        if link_web and not link_ref:
                            link_ref = link_web
                        de_donde += (
                            f" Verificación web (Gemini): mercado {_cop(round(web, 2))}{unidad_txt} "
                            f"(confianza {pr.confianza:.0f}) más cercano a la BD que al máximo; "
                            f"se mantiene el warehouse.{prod_txt} "
                            f"Fuente: {pr.fuente_nombre or 's/d'} ({link_web or 's/d'})."
                        )
                else:
                    de_donde += (" Verificación web (Gemini): sin precio creíble; "
                                 "se mantiene el warehouse.")

        # Decisión de aplicar: además de los no-sospechosos, se aplican los que
        # estén DENTRO de la banda de cordura [BD/ratio, BD·ratio] aunque superen el
        # umbral del 50% (esa banda ya es el límite de sensatez). Lo que se sale de
        # la banda solo se aplica si el arbitraje de Gemini levantó la marca.
        dentro_banda = False
        if precio_ref is not None:
            if not valor_wh_proj or valor_wh_proj <= 0:
                dentro_banda = True  # sin BD contra qué comparar: se adopta la referencia
            else:
                r_ok = precio_ref / valor_wh_proj
                dentro_banda = (1.0 / settings.max_price_ratio) <= r_ok <= settings.max_price_ratio
        aplicar = precio_ref is not None and (
            not sospechoso_pct or (settings.auto_apply_within_band and dentro_banda)
        )
        if aplicar:
            # Valor a escribir: precio de referencia proyectado al año objetivo
            # (IPC para material/viáticos, salario mínimo para mano de obra).
            nuevo_valor = round(precio_ref * factor_proj, 2)
            actualizado = True
        else:
            # Sin fuente confiable, o fuera de banda sin respaldo: NO se adopta el
            # valor de mercado. Se conserva el precio de la BD proyectado por el
            # factor (al año actual queda igual; al siguiente, IPC/SMLV). Estos
            # casos quedan en la hoja 'Items a Revisar' para decisión manual.
            nuevo_valor = round(valor_wh * factor_proj, 2)
            actualizado = False

        # Se escribe SIEMPRE: columna O = precio actualizado, columna P = año objetivo.
        if valor_wh and valor_wh > 0:
            updates_price[int(row["_sheet_row"])] = nuevo_valor
            updates_year[int(row["_sheet_row"])] = anio_objetivo

        records.append({
            "codigo": row.get(C_COD, ""),
            "descripcion_wh": desc,
            "und_wh": und,
            "grupo": row.get(C_GRUPO, ""),
            "clasificacion": (row.get(C_CLASIF, "") if C_CLASIF else ""),
            "categoria": categoria,
            "candidato": candidato,
            "score": round(score, 2),
            "confianza_match": ("Alta" if score >= 90 else "Media" if score >= settings.agg_min_score else "Baja"),
            "metodo": metodo,
            "unidad_coincide": m.unit_match,
            "valor_wh": round(valor_wh, 2),
            "valor_wh_proyectado": valor_wh_proj,
            "factor_proyeccion": round(factor_proj, 4),
            # Cotizaciones (comparativos)
            "precio_comparativo_promedio": precio_comp_prom,
            "precio_comparativo_min": precio_comp_min,
            "precio_comparativo_mediana": precio_comp_med,
            "precio_comparativo_max": precio_comp_max,
            "n_cotizaciones": n_comp,
            "region_mejor_comparativo": region_comp,
            "proveedor_comparativo": prov_comp,
            "region_comparativo_max": region_comp_max,
            "proveedor_comparativo_max": prov_comp_max,
            "archivo_comparativo_max": arch_comp_max,
            "todas_las_fuentes": todas_las_fuentes,
            "apariciones_consolidado": apariciones_consolidado,
            # Gasto real (Consolidado)
            "precio_consolidado_promedio": precio_cons_prom,
            "precio_consolidado_mediana": precio_cons_med,
            "precio_consolidado_min": precio_cons_min,
            "precio_consolidado_max": precio_cons_max,
            "n_facturas_consolidado": n_cons,
            "consumo_anual": cantidad_consumo,
            # Referencia (promedio ponderado) / refutación
            "precio_referencia": precio_ref,
            "como_se_calculo": tipo_ref,
            "de_donde_salio_el_precio": de_donde,
            "fuente_que_refuta": fuente_ref,
            "enlace_fuente": link_ref,
            "diferencia_vs_ipc": diferencia_vs_ipc,
            "pct_diferencia": pct_diferencia,
            "sospechoso_dif_mayor_50pct": sospechoso_pct,
            "warehouse_por_debajo_del_mercado": por_encima_ipc,
            "descartado_por_magnitud": descartado_magnitud,
            "consumo_usado": qty,
            "ahorro_ponderado": ahorro_ponderado,
            "anio_actualizado": anio_objetivo,
            "nuevo_valor": nuevo_valor,
            "actualizado": actualizado,
            "_sheet_row": int(row["_sheet_row"]),
        })

    matches = pd.DataFrame(records)
    result.cruces_validos = int(matches["actualizado"].sum()) if not matches.empty else 0
    consolidado_planta_df = pd.DataFrame(consolidado_por_planta)

    # --- 5. Actualizar el Sheet: columna O (precio) y columna P (año) ---
    if updates_price and not settings.dry_run:
        try:
            n_precio = sheet.batch_update_by_letter(settings.write_price_col, updates_price)
            sheet.batch_update_by_letter(settings.write_year_col, updates_year)
            result.celdas_actualizadas = n_precio
        except Exception as exc:
            result.errors.append(f"Error actualizando Sheet: {exc}")
    elif settings.dry_run:
        result.celdas_actualizadas = 0

    # --- 6. Estadísticos y reporte analítico a GCS ---
    mapping = analytics.build_mapping_report(matches)
    outliers = analytics.outlier_analysis(comparativos)
    regional = analytics.regional_comparison(comparativos)
    pivot = analytics.regional_pivot(comparativos)
    stats = analytics.compute_stats(matches, comparativos)
    # Resumen por Clasificación (contexto de cobertura): distribución completa del
    # warehouse + cuántas Activas son actualizables.
    stats["clasificacion"] = {
        "distribucion": clasif_distribucion,
        "valor_activa": settings.wh_active_value,
        "activas_evaluables": result.insumos_evaluados,
        "activas_actualizables": result.cruces_validos,
    }
    conclusiones = analytics.build_conclusions(stats)
    stats["_conclusiones"] = conclusiones
    result.stats = stats

    report_bytes = analytics.build_excel_report(
        mapping, outliers, regional, pivot, stats, conclusiones, consolidado_planta_df
    )

    ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    blob_name = f"{settings.gcs_output_prefix}reporte_apu_{ts}.xlsx"
    try:
        from google.cloud import storage

        client = storage_client or storage.Client()
        bucket = client.bucket(settings.gcs_bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(
            report_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        result.report_uri = f"gs://{settings.gcs_bucket_name}/{blob_name}"
    except Exception as exc:
        result.errors.append(f"Error subiendo reporte: {exc}")

    result.finished_at = dt.datetime.utcnow().isoformat()
    return result

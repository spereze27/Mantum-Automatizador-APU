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
    mask = (
        wh[C_DESC].astype(str).str.strip().ne("")
        & wh["_valor"].notna()
        & wh["_grupo_norm"].isin(MATCHABLE_GROUPS)
    )
    # Solo actividades ACTIVAS (la columna Clasificación marca Activa/Inactiva/
    # Duplicado/Reubicar). Las inactivas no se actualizan ni cuentan.
    if settings.only_active and C_CLASIF is not None:
        clasif_norm = wh[C_CLASIF].astype(str).str.strip().str.lower()
        mask = mask & clasif_norm.eq(settings.wh_active_value.strip().lower())
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
        # Factor de proyección: al año actual = 1 (sin proyectar); al año siguiente,
        # material/viáticos usan IPC y mano de obra el incremento del salario mínimo.
        if siguiente:
            factor_proj = (1 + settings.smlv_increase) if categoria == "Mano de obra" else (1 + settings.ipc_variation)
        else:
            factor_proj = 1.0
        valor_wh_proj = round(valor_wh * factor_proj, 2)
        valor_wh_ipc = valor_wh_proj  # alias para guardia de magnitud y diferencias

        m = matcher.match(desc, und)
        metodo = m.method
        score = m.score
        candidato = m.candidate_raw
        cand_norm = m.candidate_norm if m.matched else None

        # Segunda pasada con Gemini SOLO si el fuzzy quedó en zona dudosa.
        if (
            gemini is not None
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
        cantidad_consumo = None
        lo_band = hi_band = None
        n_con_fuera = n_cot_fuera = 0

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
            lo_band = hi_band = None
            if valor_wh_proj:
                lo_band = valor_wh_proj / settings.max_price_ratio
                hi_band = valor_wh_proj * settings.max_price_ratio

            con = con_all
            if lo_band is not None and not con_all.empty:
                con = con_all[(con_all["precio"] >= lo_band) & (con_all["precio"] <= hi_band)]
                n_con_fuera = len(con_all) - len(con)

            cot = cot_full
            if lo_band is not None and not cot_full.empty:
                cot = cot_full[(cot_full["precio"] >= lo_band) & (cot_full["precio"] <= hi_band)]
                n_cot_fuera = len(cot_full) - len(cot)
            # Recorte adicional de outliers del comparativo por mediana (cuando ya
            # hay varias cotizaciones dentro de la banda).
            if len(cot) >= 4:
                med = float(cot["precio"].median())
                if med > 0:
                    cot = cot[(cot["precio"] >= med / 4) & (cot["precio"] <= med * 4)]

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

        # Precio de referencia:
        #   - Si hay CONSOLIDADO (gasto real): se compara su PROMEDIO y su MÁXIMO
        #     contra el valor del warehouse y se elige el más cercano a la BD
        #     (la BD sirve de ancla de cordura: en insumos dispersos como
        #     Transporte el máximo es el más parecido; en otros lo es el promedio).
        #   - Si NO hay consolidado: promedio del comparativo excluyendo outliers.
        ref_es_web = False  # True solo si la referencia vino de investigación web
        if precio_cons_max is not None:
            cand = {"PROMEDIO": precio_cons_prom, "MÁXIMO": precio_cons_max}
            cand = {k: v for k, v in cand.items() if v is not None}
            if valor_wh_proj:
                etiqueta = min(cand, key=lambda k: abs(cand[k] - valor_wh_proj))
            else:
                etiqueta = "MÁXIMO"
            precio_ref = cand[etiqueta]
            tipo_ref = f"Gasto real (Consolidado) - {etiqueta.lower()} (más cercano a la BD)"
            fuente_ref = arch_cons
            link_ref = link_cons
            region_ref = "Varias plantas"
            prov_ref = ""
            de_donde = (
                f"Consolidado (gasto real): se eligió el {etiqueta} {_cop(precio_ref)} "
                f"por ser el más cercano a la BD ({_cop(valor_wh_proj)}); de {n_cons} facturas "
                f"(prom {_cop(precio_cons_prom)}, máx {_cop(precio_cons_max)}, mín {_cop(precio_cons_min)})."
            )
        elif precio_comp_prom is not None:
            precio_ref = precio_comp_prom
            tipo_ref = "Cotización proveedor - promedio (sin outliers)"
            fuente_ref = arch_comp
            link_ref = link_comp
            region_ref = region_comp
            prov_ref = prov_comp
            de_donde = (
                f"Sin consolidado. Promedio de {n_comp} cotización(es) excluyendo outliers "
                f"'{arch_comp}' [{region_comp}, col '{col_comp}']."
            )
        else:
            precio_ref = None
            fuente_ref = tipo_ref = link_ref = region_ref = prov_ref = None
            if (n_con_fuera + n_cot_fuera) > 0 and lo_band is not None:
                de_donde = (
                    f"Sin fuente dentro del rango de cordura: {n_con_fuera + n_cot_fuera} "
                    f"registro(s) quedaron fuera de [{_cop(lo_band)} , {_cop(hi_band)}] "
                    f"respecto a la BD ({_cop(valor_wh_proj)}); probable unidad/alcance "
                    f"distinto. Se mantiene el valor de la BD."
                )
            else:
                de_donde = "Sin fuente que refute (se mantiene valor del warehouse × IPC)"

            # --- FALLBACK: investigación de precio en internet con Gemini ---
            # Se dispara SIEMPRE que no haya referencia interna, en sus DOS casos:
            #   (a) no hubo ninguna coincidencia interna (matched=False o sin
            #       registros), y
            #   (b) hubo coincidencias pero TODAS quedaron fuera del rango de
            #       cordura (n_con_fuera + n_cot_fuera > 0).
            # En ambos adjunta el precio hallado + unidad y el ENLACE de la fuente.
            if (
                gemini_price is not None
                and n_price_research < settings.gemini_price_max_items
            ):
                pr = gemini_price.research_price(
                    desc, und,
                    referencia_bd=valor_wh_proj,
                    resolve_links=settings.gemini_resolve_links,
                )
                n_price_research += 1
                if (
                    pr is not None
                    and pr.precio
                    and pr.precio > 0
                    and pr.confianza >= settings.gemini_price_min_confidence
                ):
                    fuera_txt = ""
                    if (n_con_fuera + n_cot_fuera) > 0 and lo_band is not None:
                        fuera_txt = (f" Las {n_con_fuera + n_cot_fuera} fuente(s) internas "
                                     f"quedaron fuera del rango [{_cop(lo_band)} , {_cop(hi_band)}].")
                    precio_ref = round(float(pr.precio), 2)
                    ref_es_web = True
                    unidad_txt = f" / {pr.unidad}" if pr.unidad else ""
                    tipo_ref = f"Investigación web (Gemini){(' · unidad ' + pr.unidad) if pr.unidad else ''}"
                    fuente_ref = pr.fuente_nombre or "Referencia web"
                    link_ref = pr.fuente_url or ""
                    region_ref = "Internet"
                    prov_ref = pr.fuente_nombre or ""
                    de_donde = (
                        f"Sin fuente interna utilizable.{fuera_txt} Precio de referencia "
                        f"hallado en internet por Gemini: {_cop(precio_ref)}{unidad_txt} "
                        f"(confianza {pr.confianza:.0f}). Fuente: {pr.fuente_url or 's/d'}."
                        + (f" Nota: {pr.notas}" if pr.notas else "")
                    )

        # (La referencia ya usa solo el consolidado cuando existe, así que no hay
        # mezcla de fuentes que reconciliar.)

        # Guardia de cordura por magnitud: descarta referencias desproporcionadas
        # (p.ej. una tarifa por m2 cruzada contra un 'MANO DE OBRA' global). NO se
        # aplica a referencias web: ahí se conserva el precio + enlace para que sean
        # visibles, y el control de >50% más abajo evita el auto-update si difiere.
        descartado_magnitud = False
        if precio_ref is not None and valor_wh_proj and not ref_es_web:
            ratio = precio_ref / valor_wh_proj
            extremo = ratio > settings.extreme_ratio or ratio < 1.0 / settings.extreme_ratio
            if ratio > settings.max_price_ratio or ratio < 1.0 / settings.max_price_ratio:
                descartado_magnitud = True
                de_donde = (
                    f"DESCARTADO por magnitud: referencia {_cop(precio_ref)} vs BD "
                    f"{_cop(valor_wh_proj)} (relación {ratio:.0f}x; probable unidad/alcance distinto)."
                )
            elif extremo and score < settings.high_score_for_extreme:
                # Valor extremo vs la BD pero el match no es lo bastante confiable.
                descartado_magnitud = True
                de_donde = (
                    f"DESCARTADO: referencia {_cop(precio_ref)} se aleja {ratio:.1f}x de la BD "
                    f"({_cop(valor_wh_proj)}) y el match no es confiable (score {score:.0f} < "
                    f"{int(settings.high_score_for_extreme)})."
                )
            if descartado_magnitud:
                precio_ref = None
                fuente_ref = tipo_ref = link_ref = None

        diferencia_vs_ipc = pct_diferencia = por_encima_ipc = None
        ahorro_ponderado = None
        sospechoso_pct = False
        qty = cantidad_consumo if (cantidad_consumo and cantidad_consumo > 0) else 1.0
        if precio_ref is not None and valor_wh_proj:
            ref_proj = round(precio_ref * factor_proj, 2)
            diferencia_vs_ipc = round(valor_wh_proj - ref_proj, 2)  # + = ahorro por unidad
            pct_diferencia = round((diferencia_vs_ipc / valor_wh_proj) * 100, 1)
            por_encima_ipc = ref_proj > valor_wh_proj
            sospechoso_pct = abs(pct_diferencia) > 50  # diferencia >50% = sospechoso
            # Ahorro/sobrecosto ponderado por el consumo anual real.
            ahorro_ponderado = round(diferencia_vs_ipc * qty, 2)

        if precio_ref is not None and not sospechoso_pct:
            # Valor a escribir: precio de referencia proyectado al año objetivo
            # (IPC para material/viáticos, salario mínimo para mano de obra).
            nuevo_valor = round(precio_ref * factor_proj, 2)
            actualizado = True
        else:
            # Sin fuente confiable, o diferencia sospechosa (>50%): NO se adopta el
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

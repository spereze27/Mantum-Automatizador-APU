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
    report_uri: str = ""
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    stats: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def _to_float(x) -> Optional[float]:
    # Parser COP estricto y consistente con el de los comparativos.
    return to_number(x)


def run_pipeline(settings: Settings, storage_client=None) -> PipelineResult:
    settings.validate()
    result = PipelineResult(dry_run=settings.dry_run, started_at=dt.datetime.utcnow().isoformat())

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
    wh["_grupo_norm"] = wh.get(COL_GRUPO, "").astype(str).str.strip().str.lower()
    wh["_valor"] = wh.get(COL_VR_UNIT, "").map(_to_float)
    mask = (
        wh.get(COL_DESC, "").astype(str).str.strip().ne("")
        & wh["_valor"].notna()
        & wh["_grupo_norm"].isin(MATCHABLE_GROUPS)
    )
    insumos = wh[mask].copy()
    result.insumos_evaluados = len(insumos)

    # --- 2. Comparativos (cotizaciones) + Consolidado (gasto real) ---
    comparativos = load_from_gcs(
        settings.gcs_bucket_name,
        settings.gcs_input_prefix,
        settings.comparativos_config_path,
        storage_client=storage_client,
    )
    try:
        from .consolidado_loader import load_consolidado_from_gcs

        consolidado = load_consolidado_from_gcs(
            settings.gcs_bucket_name,
            settings.gcs_consolidado_prefix,
            storage_client=storage_client,
        )
    except Exception as exc:
        consolidado = pd.DataFrame()
        result.errors.append(f"Consolidado no cargado: {exc}")

    # Unifica ambas fuentes en un solo catálogo tidy (mismo esquema).
    base_cols = ["region", "proveedor", "descripcion", "unidad", "precio",
                 "archivo", "formato", "fuente_tipo", "gcs_path"]
    parts = []
    for d in (comparativos, consolidado):
        if d is not None and not d.empty:
            for c in base_cols:
                if c not in d.columns:
                    d[c] = ""
            parts.append(d[base_cols])
    comparativos = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=base_cols)
    result.comparativos_filas = len(comparativos)

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
        )
        if not gemini.enabled:
            gemini = None

    # Índice de comparativos por ítem normalizado para hallar mejor precio/región.
    comp = comparativos.copy()
    if not comp.empty:
        comp["item_norm"] = comp["descripcion"].map(normalize)

    records = []
    updates: dict[int, float] = {}
    for _, row in insumos.iterrows():
        desc = str(row.get(COL_DESC, "")).strip()
        und = str(row.get(COL_UND, "")).strip()
        valor_wh = row["_valor"]
        valor_wh_ipc = apply_ipc(valor_wh, settings.ipc_variation, settings.apply_ipc_to_warehouse)

        m = matcher.match(desc, und)
        mejor_precio = None
        region_best = None
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
        fuente_archivo = None
        fuente_proveedor = None
        fuente_tipo = None
        fuente_link = None
        if matched and not comp.empty:
            grp = comp[comp["item_norm"] == cand_norm]
            if not grp.empty:
                best_idx = grp["precio"].idxmin()
                brow = grp.loc[best_idx]
                mejor_precio = float(brow["precio"])
                region_best = brow["region"]
                fuente_archivo = brow.get("archivo", "")
                fuente_proveedor = brow.get("proveedor", "")
                fuente_tipo = brow.get("fuente_tipo", "")
                gpath = brow.get("gcs_path", "") or ""
                if gpath:
                    fuente_link = (
                        f"https://console.cloud.google.com/storage/browser/_details/"
                        f"{settings.gcs_bucket_name}/{gpath}"
                    )

        # Diferencia vs. el escenario "solo IPC".
        diferencia_vs_ipc = None
        pct_diferencia = None
        por_encima_ipc = None
        if matched and mejor_precio is not None and valor_wh_ipc:
            diferencia_vs_ipc = round(valor_wh_ipc - mejor_precio, 2)  # + = ahorro
            pct_diferencia = round((diferencia_vs_ipc / valor_wh_ipc) * 100, 1)
            por_encima_ipc = mejor_precio > valor_wh_ipc

        # Nuevo valor: mejor precio comparativo cuando hay cruce válido; si no,
        # se mantiene el valor del warehouse ajustado por IPC.
        if matched and mejor_precio is not None:
            nuevo_valor = round(mejor_precio, 2)
            actualizado = True
            updates[int(row["_sheet_row"])] = nuevo_valor
        else:
            nuevo_valor = valor_wh_ipc
            actualizado = False
            if settings.apply_ipc_to_warehouse:
                updates[int(row["_sheet_row"])] = nuevo_valor

        records.append(
            {
                "codigo": row.get(COL_CODIGO, ""),
                "descripcion_wh": desc,
                "und_wh": und,
                "grupo": row.get(COL_GRUPO, ""),
                "categoria": _categoria(row.get(COL_GRUPO, "")),
                "candidato_comparativo": candidato,
                "score": round(score, 2),
                "metodo": metodo,
                "unidad_coincide": m.unit_match,
                "valor_wh": round(valor_wh, 2),
                "valor_wh_ipc": valor_wh_ipc,
                "mejor_precio_comparativo": round(mejor_precio, 2) if mejor_precio else None,
                "region_mejor_precio": region_best,
                "fuente_que_refuta": fuente_archivo,
                "proveedor_fuente": fuente_proveedor,
                "tipo_fuente": fuente_tipo,
                "enlace_fuente": fuente_link,
                "diferencia_vs_ipc": diferencia_vs_ipc,
                "pct_diferencia": pct_diferencia,
                "warehouse_por_debajo_del_mercado": por_encima_ipc,
                "nuevo_valor": nuevo_valor,
                "actualizado": actualizado,
                "_sheet_row": int(row["_sheet_row"]),
            }
        )

    matches = pd.DataFrame(records)
    result.cruces_validos = int(matches["actualizado"].sum()) if not matches.empty else 0

    # --- 5. Actualizar el Sheet ---
    if updates and not settings.dry_run:
        try:
            result.celdas_actualizadas = sheet.batch_update_column(COL_VR_UNIT, updates)
        except Exception as exc:
            result.errors.append(f"Error actualizando Sheet: {exc}")
    elif settings.dry_run:
        result.celdas_actualizadas = 0  # No se escribe en modo auditoría.

    # --- 6. Estadísticos y reporte analítico a GCS ---
    mapping = analytics.build_mapping_report(matches)
    outliers = analytics.outlier_analysis(comparativos)
    regional = analytics.regional_comparison(comparativos)
    pivot = analytics.regional_pivot(comparativos)
    stats = analytics.compute_stats(matches, comparativos)
    conclusiones = analytics.build_conclusions(stats)
    stats["_conclusiones"] = conclusiones
    result.stats = stats

    report_bytes = analytics.build_excel_report(
        mapping, outliers, regional, pivot, stats, conclusiones
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

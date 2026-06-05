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
from .comparativos_loader import load_from_gcs
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
    errors: list = field(default_factory=list)


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace("$", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


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

    # --- 2. Comparativos ---
    comparativos = load_from_gcs(
        settings.gcs_bucket_name,
        settings.gcs_input_prefix,
        settings.comparativos_config_path,
        storage_client=storage_client,
    )
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
        if m.matched and not comp.empty:
            cand_norm = m.candidate_norm
            grp = comp[comp["item_norm"] == cand_norm]
            if not grp.empty:
                best_idx = grp["precio"].idxmin()
                mejor_precio = float(grp.loc[best_idx, "precio"])
                region_best = grp.loc[best_idx, "region"]

        # Nuevo valor: mejor precio comparativo (ya IPC si aplica) cuando hay
        # cruce válido; si no, se mantiene el valor del warehouse ajustado por IPC.
        if m.matched and mejor_precio is not None:
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
                "candidato_comparativo": m.candidate_raw,
                "score": round(m.score, 2),
                "metodo": m.method,
                "unidad_coincide": m.unit_match,
                "valor_wh": round(valor_wh, 2),
                "valor_wh_ipc": valor_wh_ipc,
                "mejor_precio_comparativo": round(mejor_precio, 2) if mejor_precio else None,
                "mejor_precio_ipc": round(mejor_precio, 2) if mejor_precio else None,
                "region_mejor_precio": region_best,
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

    # --- 6. Reporte analítico a GCS ---
    mapping = analytics.build_mapping_report(matches)
    outliers = analytics.outlier_analysis(comparativos)
    regional = analytics.regional_comparison(comparativos)
    pivot = analytics.regional_pivot(comparativos)
    report_bytes = analytics.build_excel_report(mapping, outliers, regional, pivot)

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

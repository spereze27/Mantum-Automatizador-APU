"""Carga y validación centralizada de configuración vía variables de entorno.

Todas las variables sensibles/operativas se inyectan por entorno (Cloud Run) o
GitHub Secrets. No se hardcodea nada de infraestructura aquí.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.replace(",", "."))


@dataclass(frozen=True)
class Settings:
    # --- GCP / proyecto ---
    gcp_project_id: str = field(default_factory=lambda: os.getenv("GCP_PROJECT_ID", ""))
    gcp_region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))

    # --- Storage ---
    gcs_bucket_name: str = field(default_factory=lambda: os.getenv("GCS_BUCKET_NAME", ""))
    # Prefijo dentro del bucket donde viven los comparativos de entrada.
    gcs_input_prefix: str = field(default_factory=lambda: os.getenv("GCS_INPUT_PREFIX", "comparativos/"))
    # Prefijo donde se escribe el reporte analítico de salida.
    gcs_output_prefix: str = field(default_factory=lambda: os.getenv("GCS_OUTPUT_PREFIX", "reportes/"))
    # Prefijo del Consolidado de controles presupuestales (gasto real).
    gcs_consolidado_prefix: str = field(
        default_factory=lambda: os.getenv("GCS_CONSOLIDADO_PREFIX", "consolidado/")
    )
    # Peso del Consolidado (gasto real) al promediar con las cotizaciones para
    # decidir el precio actualizado. 0.7 = prioriza el gasto real.
    consolidado_weight: float = field(
        default_factory=lambda: float(os.getenv("CONSOLIDADO_WEIGHT", "0.7"))
    )
    # Control de cordura: si la referencia de mercado es más de N veces mayor o
    # menor que el valor del warehouse, el cruce se descarta (probable unidad o
    # alcance distinto, p.ej. una tarifa por m2 vs un contrato global).
    max_price_ratio: float = field(
        default_factory=lambda: float(os.getenv("MAX_PRICE_RATIO", "8"))
    )
    # Escritura en el Sheet: columna O = precio actualizado, columna P = año.
    # (No se escribe 'Precio de Lista' porque es una fórmula.)
    write_price_col: str = field(default_factory=lambda: os.getenv("WRITE_PRICE_COL", "O"))
    write_year_col: str = field(default_factory=lambda: os.getenv("WRITE_YEAR_COL", "P"))
    update_year: str = field(default_factory=lambda: os.getenv("UPDATE_YEAR", ""))

    # --- Google Sheets (warehouse) ---
    warehouse_sheet_url: str = field(default_factory=lambda: os.getenv("WAREHOUSE_SHEET_URL", ""))
    warehouse_tab: str = field(default_factory=lambda: os.getenv("WAREHOUSE_TAB", "PRIMARIOS"))
    # Fila (1-indexed) donde están los encabezados de la hoja del warehouse.
    warehouse_header_row: int = field(default_factory=lambda: int(os.getenv("WAREHOUSE_HEADER_ROW", "2")))
    # Nombres de columna del warehouse (por defecto, los de la hoja PRIMARIOS).
    wh_col_codigo: str = field(default_factory=lambda: os.getenv("WH_COL_CODIGO", "Código"))
    wh_col_desc: str = field(default_factory=lambda: os.getenv("WH_COL_DESC", "Nombre"))
    wh_col_und: str = field(default_factory=lambda: os.getenv("WH_COL_UND", "Und"))
    wh_col_precio: str = field(default_factory=lambda: os.getenv("WH_COL_PRECIO", "Precio de Lista"))
    wh_col_grupo: str = field(default_factory=lambda: os.getenv("WH_COL_GRUPO", "Grupo"))

    # --- Credenciales ---
    # En Cloud Run se usa la SA del runtime (ADC). En local/CI se puede pasar el
    # JSON completo de la SA por esta variable (GitHub Secret) o un path de archivo.
    gcp_sa_key: str = field(default_factory=lambda: os.getenv("GCP_SA_KEY", ""))
    google_app_credentials: str = field(
        default_factory=lambda: os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    )

    # --- Lógica IPC ---
    # Variación del IPC como fracción decimal. Ej: 0.0528 = 5.28%.
    ipc_variation: float = field(default_factory=lambda: _get_float("IPC_VARIATION", 0.0528))
    # Permite no re-ajustar comparativos si ya están en el año objetivo.
    apply_ipc_to_warehouse: bool = field(
        default_factory=lambda: os.getenv("APPLY_IPC_TO_WAREHOUSE", "true").lower() == "true"
    )
    apply_ipc_to_comparativos: bool = field(
        default_factory=lambda: os.getenv("APPLY_IPC_TO_COMPARATIVOS", "false").lower() == "true"
    )

    # --- Matching NLP ---
    # Umbral mínimo (0-100) de score fuzzy para aceptar un cruce 1 a 1.
    fuzzy_threshold: int = field(default_factory=lambda: int(os.getenv("FUZZY_THRESHOLD", "70")))
    # Si "true" usa embeddings (sentence-transformers) como desempate/segunda pasada.
    use_embeddings: bool = field(
        default_factory=lambda: os.getenv("USE_EMBEDDINGS", "false").lower() == "true"
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
    )
    embedding_threshold: float = field(
        default_factory=lambda: _get_float("EMBEDDING_THRESHOLD", 0.62)
    )

    # --- Gemini (Vertex AI) para cruces dudosos ---
    use_gemini: bool = field(
        default_factory=lambda: os.getenv("USE_GEMINI", "false").lower() == "true"
    )
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    gemini_location: str = field(
        default_factory=lambda: os.getenv("GEMINI_LOCATION", os.getenv("GCP_REGION", "us-central1"))
    )
    # Solo se consulta a Gemini cuando el fuzzy queda en esta banda dudosa
    # [gemini_min_score, fuzzy_threshold). Evita gastar llamadas en lo obvio.
    gemini_min_score: int = field(default_factory=lambda: int(os.getenv("GEMINI_MIN_SCORE", "60")))
    gemini_max_candidates: int = field(
        default_factory=lambda: int(os.getenv("GEMINI_MAX_CANDIDATES", "8"))
    )
    # Confianza mínima (0-100) que debe devolver Gemini para aceptar el cruce.
    gemini_min_confidence: int = field(
        default_factory=lambda: int(os.getenv("GEMINI_MIN_CONFIDENCE", "70"))
    )

    # --- Comparativos config ---
    comparativos_config_path: str = field(
        default_factory=lambda: os.getenv("COMPARATIVOS_CONFIG_PATH", "config/comparativos_config.yaml")
    )

    # --- Seguridad de escritura ---
    # Si "true", NO escribe en el Sheet (solo simula y genera el reporte). Útil
    # para correr en modo auditoría antes de un update real.
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "false").lower() == "true")

    def validate(self) -> None:
        missing = []
        if not self.gcp_project_id:
            missing.append("GCP_PROJECT_ID")
        if not self.gcs_bucket_name:
            missing.append("GCS_BUCKET_NAME")
        if not self.warehouse_sheet_url:
            missing.append("WAREHOUSE_SHEET_URL")
        if missing:
            raise RuntimeError(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

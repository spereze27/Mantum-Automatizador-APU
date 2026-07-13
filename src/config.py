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
        default_factory=lambda: float(os.getenv("MAX_PRICE_RATIO", "5"))
    )
    # Estrictez adicional para evitar sobrecostos inflados:
    # - agg_min_score: solo se agregan (promedio/consumo) las variantes del
    #   catálogo cuyo match con el insumo sea >= este score. Evita que un término
    #   genérico ('Oficial') junte cientos de líneas distintas.
    # - extreme_ratio + high_score_for_extreme: si la referencia se aleja más de
    #   'extreme_ratio' del valor de la BD, se exige un score muy alto; si no, se descarta.
    # - source_disagree_ratio: si el comparativo y el consolidado difieren más de
    #   este factor, se descarta el comparativo y se usa el gasto real (Consolidado).
    agg_min_score: float = field(default_factory=lambda: float(os.getenv("AGG_MIN_SCORE", "80")))
    extreme_ratio: float = field(default_factory=lambda: float(os.getenv("EXTREME_RATIO", "2.0")))
    high_score_for_extreme: float = field(
        default_factory=lambda: float(os.getenv("HIGH_SCORE_FOR_EXTREME", "90"))
    )
    source_disagree_ratio: float = field(
        default_factory=lambda: float(os.getenv("SOURCE_DISAGREE_RATIO", "1.8"))
    )
    # --- Estimador de la referencia (CAMBIO 1) ---
    # Antes se usaba el MÁXIMO de las apariciones. El máximo es un estadístico de
    # orden que NO converge: con las 2.033 facturas de 'Ayudante' el máximo es el
    # p100 y CRECE con el tamaño de la muestra (a más evidencia, más caro). Eso
    # explicó +$280 M ponderados de sobrecosto en el reporte 20260710.
    # Valores: p75 (recomendado) | p90 | mediana | promedio | max (legacy).
    ref_estimator: str = field(default_factory=lambda: os.getenv("REF_ESTIMATOR", "p75"))
    ref_quantile: float = field(default_factory=lambda: _get_float("REF_QUANTILE", 0.75))
    # Winsorización de colas antes del cuantil (topa, no elimina). 0 = apagado.
    ref_winsor_pct: float = field(default_factory=lambda: _get_float("REF_WINSOR_PCT", 5.0))
    # Vida media (días) del peso por recencia de cada aparición. 0 = sin decaimiento.
    ref_recency_halflife_days: float = field(
        default_factory=lambda: _get_float("REF_RECENCY_HALFLIFE_DAYS", 365.0)
    )
    # Ponderar además por la CANTIDAD facturada. Apagado por defecto: sesga la
    # referencia hacia precios de compra al por mayor (con descuento por volumen),
    # que no son el precio de lista que se publica en el APU.
    ref_weight_by_qty: bool = field(
        default_factory=lambda: os.getenv("REF_WEIGHT_BY_QTY", "false").lower() == "true"
    )

    # --- Cordura de aplicación (CAMBIO 2) ---
    # La banda [BD/max_price_ratio, BD*max_price_ratio] es INFRANQUEABLE: nada se
    # escribe fuera de ella, ni siquiera cuando Gemini "respalda el máximo". En el
    # reporte 20260710 los 10 únicos ítems aplicados fuera de banda (hasta 30x)
    # entraron TODOS por esa puerta, porque el arbitraje ponía sospechoso=False.
    enforce_band: bool = field(
        default_factory=lambda: os.getenv("ENFORCE_BAND", "true").lower() == "true"
    )
    # El arbitraje adopta el MENOR entre el máximo interno y el precio de mercado
    # (no infla cuando el mercado está por debajo del interno).
    arbitrate_take_min: bool = field(
        default_factory=lambda: os.getenv("ARBITRATE_TAKE_MIN", "true").lower() == "true"
    )

    # --- Unidades y presentación (CAMBIOS 3-5) ---
    # No cruzar ítems con unidad no dimensional (%, Glb) ni con precio de BD <= 0:
    # no existe un "precio unitario" que un mercado pueda refutar.
    skip_non_dimensional_units: bool = field(
        default_factory=lambda: os.getenv("SKIP_NON_DIMENSIONAL_UNITS", "true").lower() == "true"
    )
    # Normalizar cada aparición (y el precio web de Gemini) a la unidad del WH
    # usando el factor de presentación (1/4 gl, cuñete = 5 gl, bulto = 50 kg...).
    normalize_presentation: bool = field(
        default_factory=lambda: os.getenv("NORMALIZE_PRESENTATION", "true").lower() == "true"
    )
    # Guardarraíl absoluto por familia de ítem (config/plausibilidad.yaml).
    unit_plausibility: bool = field(
        default_factory=lambda: os.getenv("UNIT_PLAUSIBILITY", "true").lower() == "true"
    )
    plausibilidad_config_path: str = field(
        default_factory=lambda: os.getenv("PLAUSIBILIDAD_CONFIG_PATH", "config/plausibilidad.yaml")
    )

    # Escritura en el Sheet: columna O = precio actualizado, columna P = año.
    # (No se escribe 'Precio de Lista' porque es una fórmula.)
    write_price_col: str = field(default_factory=lambda: os.getenv("WRITE_PRICE_COL", "O"))
    write_year_col: str = field(default_factory=lambda: os.getenv("WRITE_YEAR_COL", "P"))
    update_year: str = field(default_factory=lambda: os.getenv("UPDATE_YEAR", ""))
    # Modo de actualización: 'actual' (compara/escribe al año actual, sin proyectar)
    # o 'siguiente' (proyecta al año entrante con IPC para material e incremento
    # del salario mínimo para mano de obra; ambos se ingresan manualmente en la UI).
    update_mode: str = field(default_factory=lambda: os.getenv("UPDATE_MODE", "actual"))
    smlv_increase: float = field(default_factory=lambda: float(os.getenv("SMLV_INCREASE", "0.0")))

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
    wh_col_clasificacion: str = field(default_factory=lambda: os.getenv("WH_COL_CLASIFICACION", "Clasificación"))
    # Procesar solo actividades activas (Clasificación == 'Activa').
    only_active: bool = field(default_factory=lambda: os.getenv("ONLY_ACTIVE", "true").lower() == "true")
    wh_active_value: str = field(default_factory=lambda: os.getenv("WH_ACTIVE_VALUE", "Activa"))
    # Clasificaciones de la columna Clasificación que SÍ se procesan (coma-separado).
    # Por defecto Activa y Duplicado.
    wh_clasif_incluir: str = field(
        default_factory=lambda: os.getenv("WH_CLASIF_INCLUIR", "Activa,Duplicado")
    )

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

    # --- Gemini para cruces dudosos e investigación de precios ---
    use_gemini: bool = field(
        default_factory=lambda: os.getenv("USE_GEMINI", "false").lower() == "true"
    )
    # API key del Gemini Developer API (forma de auth de este proyecto). Si está
    # vacía, las clases caen a Vertex AI con ADC.
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
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

    # --- Gemini: investigación de precio en internet (fallback sin fuente) ---
    # Cuando un ítem activo NO encuentra ninguna fuente interna (consolidado ni
    # comparativo) que refute su precio, se le pide a Gemini (con grounding de
    # Google Search) un precio de referencia de mercado + unidad + enlace de la
    # fuente. El resultado se registra como referencia y el enlace queda en
    # 'fuente_que_refuta'. Es FAIL-SOFT: si Vertex/grounding no está, se omite.
    use_gemini_price_research: bool = field(
        default_factory=lambda: os.getenv("USE_GEMINI_PRICE_RESEARCH", "false").lower() == "true"
    )
    # Tope de ítems a investigar por corrida (controla costo/latencia).
    gemini_price_max_items: int = field(
        default_factory=lambda: int(os.getenv("GEMINI_PRICE_MAX_ITEMS", "80"))
    )
    # Confianza mínima (0-100) de Gemini para aceptar el precio web hallado.
    gemini_price_min_confidence: int = field(
        default_factory=lambda: int(os.getenv("GEMINI_PRICE_MIN_CONFIDENCE", "60"))
    )
    # Diferencia (%) sobre la BD a partir de la cual un precio se marca "sospechoso"
    # y NO se auto-aplica (queda visible para revisión). Subirlo aplica más cambios.
    suspicious_pct_threshold: float = field(
        default_factory=lambda: float(os.getenv("SUSPICIOUS_PCT_THRESHOLD", "50"))
    )
    # Cuando el promedio interno queda "sospechoso" (>umbral vs BD), consultar a
    # Gemini un precio de mercado para arbitrar si tiene razón el PROMEDIO o la BD
    # (gana el más cercano al precio web). Requiere use_gemini_price_research.
    gemini_arbitrate_suspicious: bool = field(
        default_factory=lambda: os.getenv("GEMINI_ARBITRATE_SUSPICIOUS", "true").lower() == "true"
    )
    # Cuando la referencia cae DENTRO de la banda de cordura [BD/ratio, BD·ratio],
    # se auto-aplica aunque supere el umbral de "sospechoso" (maximiza actualizaciones
    # dentro del rango que TÚ definiste con MAX_PRICE_RATIO). Solo lo que se sale de la
    # banda queda para arbitraje de Gemini / revisión. Poner en false vuelve al
    # comportamiento estricto (bloquear todo lo >SUSPICIOUS_PCT_THRESHOLD).
    auto_apply_within_band: bool = field(
        default_factory=lambda: os.getenv("AUTO_APPLY_WITHIN_BAND", "true").lower() == "true"
    )
    # Cuando el WH tiene una marca/modelo distintivo y NINGUNA aparición interna la
    # comparte (cruce genérico/otra marca, p.ej. WH 'Sanitario aquaplus' vs consolidado
    # 'Sanitario Corona'), preferir la búsqueda en internet con Gemini del producto
    # exacto en vez de usar el precio de otra marca.
    prefer_web_on_brand_mismatch: bool = field(
        default_factory=lambda: os.getenv("PREFER_WEB_ON_BRAND_MISMATCH", "true").lower() == "true"
    )
    # Intervalo mínimo (segundos) entre llamadas a Gemini, para no exceder el rate-limit
    # de Vertex (grounding) y evitar 429 RESOURCE_EXHAUSTED. 0 = sin throttle.
    # Default 0.5 s: probado en Cloud Shell (OK=10, FALLOS=0). gemini-2.5-flash usa
    # Dynamic Shared Quota, así que el 429 se evita espaciando, no subiendo cuota.
    gemini_min_interval_sec: float = field(
        default_factory=lambda: _get_float("GEMINI_MIN_INTERVAL_SEC", 0.5)
    )
    # Todo precio hallado en internet DEBE venir con un enlace directo (http) que lo
    # respalde. Si el enlace no se puede resolver, el precio se RECHAZA (no se usa
    # como referencia). Requisito de auditoría del negocio.
    gemini_require_link: bool = field(
        default_factory=lambda: os.getenv("GEMINI_REQUIRE_LINK", "true").lower() == "true"
    )
    # % de IVA a descontar cuando la fuente web reporta el precio CON IVA incluido.
    iva_pct: float = field(default_factory=lambda: float(os.getenv("IVA_PCT", "19")))
    # Resolver el redirect de Vertex (vertexaisearch.../grounding-api-redirect/...)
    # al enlace DIRECTO de la fuente. Añade una petición HTTP por fuente.
    gemini_resolve_links: bool = field(
        default_factory=lambda: os.getenv("GEMINI_RESOLVE_LINKS", "true").lower() == "true"
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

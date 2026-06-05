"""Analítica del comparativo: mapeo 1 a 1, outliers y comparativo regional."""
from __future__ import annotations

import io
from typing import Optional

import numpy as np
import pandas as pd

from .nlp_mapper import normalize


# ---------------------------------------------------------------------------
# 1. Mapeo 1 a 1 (ya construido en el pipeline; aquí solo se formatea)
# ---------------------------------------------------------------------------

def build_mapping_report(matches: pd.DataFrame) -> pd.DataFrame:
    """Detalle del cruce warehouse <-> comparativo con confidence score."""
    cols = [
        "codigo", "descripcion_wh", "und_wh", "grupo",
        "candidato_comparativo", "score", "metodo", "unidad_coincide",
        "valor_wh", "valor_wh_ipc", "mejor_precio_comparativo",
        "mejor_precio_ipc", "region_mejor_precio", "nuevo_valor", "actualizado",
    ]
    existing = [c for c in cols if c in matches.columns]
    out = matches[existing].copy()
    return out.sort_values("score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Análisis de outliers por ítem (IQR + Z-Score)
# ---------------------------------------------------------------------------

def outlier_analysis(comparativos: pd.DataFrame, z_thresh: float = 3.0) -> pd.DataFrame:
    """Identifica precios atípicos por ítem usando IQR y Z-Score.

    Agrupa por ítem normalizado (todas las regiones/proveedores juntos) y marca
    cada observación como outlier por IQR (1.5*IQR) y/o por |z| > z_thresh.
    """
    if comparativos.empty:
        return pd.DataFrame()

    df = comparativos.copy()
    df["item_norm"] = df["descripcion"].map(normalize)
    df = df[df["precio"].notna() & df["item_norm"].ne("")]

    rows = []
    for item, g in df.groupby("item_norm"):
        precios = g["precio"].astype(float)
        if len(precios) < 3:
            # Sin suficientes observaciones para estadística robusta.
            continue
        q1, q3 = precios.quantile(0.25), precios.quantile(0.75)
        iqr = q3 - q1
        low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mean, std = precios.mean(), precios.std(ddof=0)
        for idx, p in precios.items():
            z = (p - mean) / std if std and std > 0 else 0.0
            is_iqr = bool(p < low or p > high)
            is_z = bool(abs(z) > z_thresh)
            if is_iqr or is_z:
                rows.append(
                    {
                        "item_norm": item,
                        "descripcion": g.loc[idx, "descripcion"],
                        "region": g.loc[idx, "region"],
                        "proveedor": g.loc[idx, "proveedor"],
                        "precio": round(float(p), 2),
                        "mediana_item": round(float(precios.median()), 2),
                        "q1": round(float(q1), 2),
                        "q3": round(float(q3), 2),
                        "iqr": round(float(iqr), 2),
                        "limite_inf": round(float(low), 2),
                        "limite_sup": round(float(high), 2),
                        "z_score": round(float(z), 2),
                        "outlier_iqr": is_iqr,
                        "outlier_zscore": is_z,
                        "n_observaciones": int(len(precios)),
                    }
                )
    return pd.DataFrame(rows).sort_values(["item_norm", "precio"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Comparativo regional (dispersión y mediana por ítem y región)
# ---------------------------------------------------------------------------

def regional_comparison(comparativos: pd.DataFrame) -> pd.DataFrame:
    """Tabla larga: por ítem y región -> mediana, min, max, std, n."""
    if comparativos.empty:
        return pd.DataFrame()
    df = comparativos.copy()
    df["item_norm"] = df["descripcion"].map(normalize)
    df = df[df["precio"].notna() & df["item_norm"].ne("")]

    agg = (
        df.groupby(["item_norm", "region"])["precio"]
        .agg(["count", "min", "median", "max", "mean", "std"])
        .reset_index()
        .rename(
            columns={
                "count": "n",
                "min": "precio_min",
                "median": "precio_mediana",
                "max": "precio_max",
                "mean": "precio_promedio",
                "std": "desv_std",
            }
        )
    )
    # Una descripción representativa por ítem (la más frecuente).
    repr_desc = (
        df.groupby("item_norm")["descripcion"]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else s.iloc[0])
        .reset_index()
        .rename(columns={"descripcion": "descripcion"})
    )
    agg = agg.merge(repr_desc, on="item_norm", how="left")
    for c in ["precio_min", "precio_mediana", "precio_max", "precio_promedio", "desv_std"]:
        agg[c] = agg[c].round(2)
    return agg.sort_values(["item_norm", "region"]).reset_index(drop=True)


def regional_pivot(comparativos: pd.DataFrame) -> pd.DataFrame:
    """Tabla pivote: filas=ítem, columnas=región, valores=mediana de precio.

    Incluye una columna de dispersión inter-regional (coef. de variación).
    """
    reg = regional_comparison(comparativos)
    if reg.empty:
        return pd.DataFrame()
    pivot = reg.pivot_table(
        index="item_norm", columns="region", values="precio_mediana", aggfunc="first"
    )
    desc = reg.drop_duplicates("item_norm").set_index("item_norm")["descripcion"]
    pivot.insert(0, "descripcion", desc)
    # Dispersión inter-regional sobre las medianas regionales.
    region_cols = [c for c in pivot.columns if c != "descripcion"]
    medianas = pivot[region_cols]
    pivot["mediana_global"] = medianas.median(axis=1).round(2)
    pivot["dispersion_cv_%"] = (
        (medianas.std(axis=1) / medianas.mean(axis=1) * 100).round(1)
    )
    return pivot.reset_index()


# ---------------------------------------------------------------------------
# Exportación del reporte (xlsx en memoria)
# ---------------------------------------------------------------------------

def build_excel_report(
    mapping: pd.DataFrame,
    outliers: pd.DataFrame,
    regional: pd.DataFrame,
    regional_pivot_df: pd.DataFrame,
) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        (mapping if not mapping.empty else pd.DataFrame({"info": ["sin datos"]})).to_excel(
            writer, sheet_name="Mapping 1 a 1", index=False
        )
        (outliers if not outliers.empty else pd.DataFrame({"info": ["sin outliers"]})).to_excel(
            writer, sheet_name="Analisis Outliers", index=False
        )
        (regional if not regional.empty else pd.DataFrame({"info": ["sin datos"]})).to_excel(
            writer, sheet_name="Comparativo Regional", index=False
        )
        if not regional_pivot_df.empty:
            regional_pivot_df.to_excel(writer, sheet_name="Pivot Regional", index=False)
    buffer.seek(0)
    return buffer.getvalue()

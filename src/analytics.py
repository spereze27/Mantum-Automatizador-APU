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
    """Detalle del cruce warehouse <-> fuente, con la fuente que refuta el
    precio, el enlace a ella y la diferencia frente al escenario solo-IPC."""
    cols = [
        "codigo", "descripcion_wh", "und_wh", "grupo", "categoria",
        "candidato_comparativo", "score", "metodo", "unidad_coincide",
        "valor_wh", "valor_wh_ipc", "mejor_precio_comparativo", "region_mejor_precio",
        "fuente_que_refuta", "proveedor_fuente", "tipo_fuente", "enlace_fuente",
        "diferencia_vs_ipc", "pct_diferencia", "warehouse_por_debajo_del_mercado",
        "nuevo_valor", "actualizado",
    ]
    existing = [c for c in cols if c in matches.columns]
    out = matches[existing].copy()
    if out.empty or "score" not in out.columns:
        return out
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
    if not rows:
        return pd.DataFrame()
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

def _categoria_frame(stats: dict) -> pd.DataFrame:
    pc = (stats or {}).get("por_categoria", {})
    filas = []
    for cat in ("Material", "Mano de obra", "Viáticos"):
        d = pc.get(cat, {})
        filas.append({
            "Categoría": cat,
            "Ítems cruzados": d.get("cruces", 0),
            "Más barato que IPC": d.get("mas_barato_que_ipc", 0),
            "Más caro que IPC": d.get("mas_caro_que_ipc", 0),
            "Ahorro potencial (COP)": d.get("ahorro_potencial", 0),
            "Sobrecosto potencial (COP)": d.get("sobrecosto_potencial", 0),
            "Diferencia neta (COP)": d.get("diferencia_neta", 0),
        })
    return pd.DataFrame(filas)


def build_excel_report(
    mapping: pd.DataFrame,
    outliers: pd.DataFrame,
    regional: pd.DataFrame,
    regional_pivot_df: pd.DataFrame,
    stats: Optional[dict] = None,
    conclusiones: Optional[list] = None,
) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        # Resumen ejecutivo primero.
        if stats:
            _stats_to_frame(stats, conclusiones).to_excel(
                writer, sheet_name="Resumen Ejecutivo", index=False
            )
            _categoria_frame(stats).to_excel(
                writer, sheet_name="Resumen por Categoria", index=False
            )
        (mapping if not mapping.empty else pd.DataFrame({"info": ["sin datos"]})).to_excel(
            writer, sheet_name="Mapping y Refutacion", index=False
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


# ---------------------------------------------------------------------------
# Estadísticos y conclusiones
# ---------------------------------------------------------------------------

def _region_cost_index(comparativos: pd.DataFrame) -> pd.DataFrame:
    """Índice de costo por región: para cada ítem presente en varias regiones,
    se calcula precio_region / mediana_global_item; el promedio por región da un
    índice >1 (más caro que el promedio) o <1 (más económico)."""
    if comparativos.empty:
        return pd.DataFrame()
    df = comparativos.copy()
    df["item_norm"] = df["descripcion"].map(normalize)
    df = df[df["precio"].notna() & df["item_norm"].ne("")]
    med_region = df.groupby(["item_norm", "region"])["precio"].median().reset_index()
    med_global = df.groupby("item_norm")["precio"].median().rename("med_global").reset_index()
    merged = med_region.merge(med_global, on="item_norm")
    merged = merged[merged["med_global"] > 0]
    merged["ratio"] = merged["precio"] / merged["med_global"]
    idx = (
        merged.groupby("region")
        .agg(indice_costo=("ratio", "mean"), items=("item_norm", "nunique"))
        .reset_index()
    )
    # Solo regiones con respaldo suficiente.
    idx = idx[idx["items"] >= 3]
    idx["indice_costo"] = idx["indice_costo"].round(3)
    return idx.sort_values("indice_costo", ascending=False).reset_index(drop=True)


def compute_stats(matches: pd.DataFrame, comparativos: pd.DataFrame) -> dict:
    s: dict = {}
    # Fuentes analizadas.
    if not comparativos.empty and "archivo" in comparativos.columns:
        s["fuentes_analizadas"] = int(comparativos["archivo"].nunique())
        s["fuentes_listado"] = sorted(comparativos["archivo"].dropna().unique().tolist())
    else:
        s["fuentes_analizadas"] = 0
        s["fuentes_listado"] = []
    s["regiones_analizadas"] = (
        sorted(comparativos["region"].dropna().unique().tolist())
        if not comparativos.empty else []
    )
    s["registros_precio"] = int(len(comparativos))

    # Top regiones más caras / económicas.
    idx = _region_cost_index(comparativos)
    if not idx.empty:
        s["top5_regiones_mas_costosas"] = idx.head(5)[["region", "indice_costo"]].values.tolist()
        s["top5_regiones_mas_economicas"] = (
            idx.tail(5).sort_values("indice_costo")[["region", "indice_costo"]].values.tolist()
        )
    else:
        s["top5_regiones_mas_costosas"] = []
        s["top5_regiones_mas_economicas"] = []

    # Precios por debajo / por encima del escenario solo-IPC.
    if not matches.empty and "diferencia_vs_ipc" in matches.columns:
        m = matches[matches["actualizado"] == True].copy()  # noqa: E712
        m = m[m["diferencia_vs_ipc"].notna()]
        s["cruces_validos"] = int(len(m))
        # mercado por DEBAJO del IPC = se consigue más barato (diferencia > 0).
        below = m[m["diferencia_vs_ipc"] > 0]
        above = m[m["diferencia_vs_ipc"] < 0]
        s["items_mercado_mas_barato_que_ipc"] = int(len(below))
        s["items_mercado_mas_caro_que_ipc"] = int(len(above))
        s["ahorro_potencial_total"] = round(float(below["diferencia_vs_ipc"].sum()), 0)
        s["sobrecosto_potencial_total"] = round(float(-above["diferencia_vs_ipc"].sum()) + 0.0, 0)
        s["diferencia_neta_total"] = round(float(m["diferencia_vs_ipc"].sum()), 0)

        # Segmentación por categoría: Material | Mano de obra | Viáticos.
        por_cat = {}
        cat_col = "categoria" if "categoria" in m.columns else None
        cats = ["Material", "Mano de obra", "Viáticos"]
        for cat in cats:
            sub = m[m[cat_col] == cat] if cat_col else m.iloc[0:0]
            below_c = sub[sub["diferencia_vs_ipc"] > 0]
            above_c = sub[sub["diferencia_vs_ipc"] < 0]
            por_cat[cat] = {
                "cruces": int(len(sub)),
                "mas_barato_que_ipc": int(len(below_c)),
                "mas_caro_que_ipc": int(len(above_c)),
                "ahorro_potencial": round(float(below_c["diferencia_vs_ipc"].sum()), 0),
                "sobrecosto_potencial": round(float(-above_c["diferencia_vs_ipc"].sum()) + 0.0, 0),
                "diferencia_neta": round(float(sub["diferencia_vs_ipc"].sum()), 0),
            }
        s["por_categoria"] = por_cat
    else:
        s.update({
            "cruces_validos": 0,
            "items_mercado_mas_barato_que_ipc": 0,
            "items_mercado_mas_caro_que_ipc": 0,
            "ahorro_potencial_total": 0,
            "sobrecosto_potencial_total": 0,
            "diferencia_neta_total": 0,
        })
        s["por_categoria"] = {}
    return s


def build_conclusions(s: dict) -> list:
    def cop(x):
        try:
            return "$" + f"{float(x):,.0f}".replace(",", ".")
        except Exception:
            return str(x)

    c = []
    c.append(
        f"Se analizaron {s.get('fuentes_analizadas', 0)} fuentes de datos "
        f"({s.get('registros_precio', 0)} precios) en "
        f"{len(s.get('regiones_analizadas', []))} regiones."
    )
    c.append(
        f"De {s.get('cruces_validos', 0)} ítems cruzados, en "
        f"{s.get('items_mercado_mas_barato_que_ipc', 0)} el mercado ofrece un precio MENOR "
        f"al que daría aplicar solo el IPC (oportunidad de ahorro), y en "
        f"{s.get('items_mercado_mas_caro_que_ipc', 0)} el mercado está por ENCIMA del IPC."
    )
    c.append(
        f"Ahorro potencial identificado: {cop(s.get('ahorro_potencial_total', 0))}. "
        f"Sobrecosto potencial: {cop(s.get('sobrecosto_potencial_total', 0))}. "
        f"Diferencia neta: {cop(s.get('diferencia_neta_total', 0))} "
        "(positivo = el mercado está, en neto, por debajo del ajuste por IPC)."
    )
    if s.get("top5_regiones_mas_costosas"):
        caras = ", ".join(f"{r} ({i:.2f})" for r, i in s["top5_regiones_mas_costosas"][:3])
        c.append(f"Regiones más costosas (índice vs. mediana nacional): {caras}.")
    if s.get("top5_regiones_mas_economicas"):
        baratas = ", ".join(f"{r} ({i:.2f})" for r, i in s["top5_regiones_mas_economicas"][:3])
        c.append(f"Regiones más económicas: {baratas}.")
    pc = s.get("por_categoria", {})
    for cat in ("Material", "Mano de obra", "Viáticos"):
        if cat in pc and pc[cat]["cruces"] > 0:
            d = pc[cat]
            c.append(
                f"[{cat}] {d['cruces']} ítems · ahorro {cop(d['ahorro_potencial'])} · "
                f"sobrecosto {cop(d['sobrecosto_potencial'])} · neta {cop(d['diferencia_neta'])}."
            )
    return c


def _stats_to_frame(s: dict, conclusiones: Optional[list]) -> pd.DataFrame:
    filas = [
        ("Fuentes de datos analizadas", s.get("fuentes_analizadas", 0)),
        ("Registros de precio", s.get("registros_precio", 0)),
        ("Regiones analizadas", len(s.get("regiones_analizadas", []))),
        ("Ítems cruzados (válidos)", s.get("cruces_validos", 0)),
        ("Ítems con mercado más barato que IPC", s.get("items_mercado_mas_barato_que_ipc", 0)),
        ("Ítems con mercado más caro que IPC", s.get("items_mercado_mas_caro_que_ipc", 0)),
        ("Ahorro potencial total (COP)", s.get("ahorro_potencial_total", 0)),
        ("Sobrecosto potencial total (COP)", s.get("sobrecosto_potencial_total", 0)),
        ("Diferencia neta total (COP)", s.get("diferencia_neta_total", 0)),
    ]
    for i, (r, v) in enumerate(s.get("top5_regiones_mas_costosas", []), 1):
        filas.append((f"Región más costosa #{i}", f"{r} (índice {v:.2f})"))
    for i, (r, v) in enumerate(s.get("top5_regiones_mas_economicas", []), 1):
        filas.append((f"Región más económica #{i}", f"{r} (índice {v:.2f})"))
    for i, txt in enumerate(conclusiones or [], 1):
        filas.append((f"Conclusión {i}", txt))
    return pd.DataFrame(filas, columns=["Métrica", "Valor"])

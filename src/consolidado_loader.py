"""Carga del Consolidado de controles presupuestales (gasto real).

Es un archivo transaccional grande (~170k filas) con el costo REALMENTE pagado
por ítem/actividad de mantenimiento. Se vincula con el resto de fuentes por la
DESCRIPCIÓN del ítem (mismo motor NLP), y aporta la región vía el mapeo
Sede -> Región/Ciudad de la hoja 'Sedes'.

Salida: DataFrame tidy con el MISMO esquema que los comparativos, más metadatos
útiles para el reporte (fecha, factura, tipo de costo):
  region | proveedor | descripcion | unidad | precio | archivo | formato |
  fuente_tipo | fecha | factura | tipo_costo | sede
"""
from __future__ import annotations

import io
from typing import Optional

import pandas as pd

from .comparativos_loader import _slug, to_number

CONSOLIDADO_FORMAT = "consolidado_gasto_real"


def _clean_city(x: str) -> str:
    if not x:
        return ""
    s = str(x).strip()
    # "Medellín (Colombia)" -> "Medellín"
    return s.split("(")[0].strip()


def _build_sede_region(xls: pd.ExcelFile) -> dict:
    """Mapa Sede(normalizada) -> Región legible, desde la hoja 'Sedes'."""
    if "Sedes" not in xls.sheet_names:
        return {}
    df = xls.parse("Sedes", header=0, dtype=object)
    cols = {str(c).strip().lower(): c for c in df.columns}
    sede_c = cols.get("sede")
    ciudad_c = cols.get("ciudad")
    region_c = cols.get("región") or cols.get("region")
    if sede_c is None:
        return {}
    mapping = {}
    for _, r in df.iterrows():
        sede = r.get(sede_c)
        if not sede:
            continue
        ciudad = _clean_city(r.get(ciudad_c)) if ciudad_c else ""
        region = str(r.get(region_c)).strip() if region_c and r.get(region_c) else ""
        mapping[_slug(sede)] = ciudad or region or "Sin región"
    return mapping


def parse_consolidado(content: bytes, filename: str) -> pd.DataFrame:
    xls = pd.ExcelFile(io.BytesIO(content))
    if "Consolidado" not in xls.sheet_names:
        return pd.DataFrame()

    sede_region = _build_sede_region(xls)
    df = xls.parse("Consolidado", header=0, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]

    def col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_desc = col("Descripción", "Descripcion")
    c_precio = col("Vlr. Unitario", "Vlr Unitario", "Valor Unitario")
    c_prov = col("Proveedor")
    c_sede = col("Sede")
    c_fecha = col("Fecha")
    c_fact = col("# Factura", "Factura")
    c_tipocosto = col("Tipo de Costo")
    c_unidad = col("Unidad", "Und")
    c_cant = col("Cantidad", "Cant.", "Cant")
    if c_desc is None or c_precio is None:
        return pd.DataFrame()

    def _to_qty(x):
        """Cantidad/consumo: coma decimal, punto de miles. No tiene piso (1, 2 válidos)."""
        s = str(x).strip().replace(" ", "")
        if not s or s.lower() == "nan":
            return None
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            v = float(s)
            return v if v > 0 else None
        except Exception:
            return None

    out = pd.DataFrame()
    out["descripcion"] = df[c_desc].astype(str).str.strip()
    out["precio"] = df[c_precio].map(to_number)
    out["cantidad"] = df[c_cant].map(_to_qty) if c_cant else None
    out["proveedor"] = df[c_prov].astype(str).str.strip() if c_prov else ""
    out["sede"] = df[c_sede].astype(str).str.strip() if c_sede else ""
    out["region"] = out["sede"].map(lambda s: sede_region.get(_slug(s), "Sin región"))
    out["unidad"] = df[c_unidad].astype(str).str.strip() if c_unidad else ""
    out["fecha"] = df[c_fecha].astype(str).str.slice(0, 10) if c_fecha else ""
    out["factura"] = df[c_fact].astype(str).str.strip() if c_fact else ""
    out["tipo_costo"] = df[c_tipocosto].astype(str).str.strip() if c_tipocosto else ""
    out["archivo"] = filename
    out["formato"] = CONSOLIDADO_FORMAT
    out["fuente_tipo"] = "Gasto real (Consolidado)"

    out = out[out["descripcion"].ne("") & out["precio"].notna()]
    return out.reset_index(drop=True)


def load_consolidado_from_gcs(
    bucket_name: str, prefix: str, storage_client=None
) -> pd.DataFrame:
    """Descarga y parsea todos los Consolidados bajo `prefix` en el bucket."""
    from google.cloud import storage

    client = storage_client or storage.Client()
    frames = []
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        if blob.name.endswith("/") or not blob.name.lower().endswith((".xlsx", ".xlsm")):
            continue
        filename = blob.name.split("/")[-1]
        try:
            df = parse_consolidado(blob.download_as_bytes(), filename)
            if not df.empty:
                df["gcs_path"] = blob.name
                frames.append(df)
                print(f"[consolidado] {filename}: {len(df)} registros de gasto real.")
        except Exception as exc:
            print(f"[consolidado] ERROR en {filename}: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_consolidado_from_file(path: str) -> pd.DataFrame:
    with open(path, "rb") as fh:
        return parse_consolidado(fh.read(), path.split("/")[-1])

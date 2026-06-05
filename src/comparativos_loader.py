"""Carga y normalización de los archivos comparativos desde GCS.

Soporta múltiples formatos heterogéneos (xlsx, xlsm, csv, pdf). Como cada
proveedor entrega un layout distinto, se usa:
  - configuración por archivo (config/comparativos_config.yaml), y
  - autodetección de la fila de encabezado por palabras clave.

Salida: un DataFrame "tidy" (largo) con columnas:
  region | proveedor | descripcion | unidad | precio | archivo | formato
Cada fila es un precio observado de un ítem en una región por un proveedor.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yaml

# pdfplumber y openpyxl se importan de forma perezosa para acelerar el arranque.


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

@dataclass
class FileRule:
    match: str
    region: str
    format: str
    sheet: Optional[object] = None
    header_keywords: Optional[list] = None
    desc_aliases: Optional[list] = None
    unit_aliases: Optional[list] = None
    price_aliases: Optional[list] = None
    exclude_aliases: Optional[list] = None
    best_price_alias: Optional[str] = None


class ComparativosConfig:
    def __init__(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        self.defaults = raw.get("defaults", {})
        self.rules = [FileRule(**r) for r in raw.get("files", [])]

    def rule_for(self, filename: str) -> Optional[FileRule]:
        low = filename.lower()
        for r in self.rules:
            if r.match.lower() in low:
                return self._with_defaults(r)
        return None

    def _with_defaults(self, r: FileRule) -> FileRule:
        r.header_keywords = r.header_keywords or self.defaults.get("header_keywords", [])
        r.desc_aliases = r.desc_aliases or self.defaults.get("desc_aliases", [])
        r.unit_aliases = r.unit_aliases or self.defaults.get("unit_aliases", [])
        r.price_aliases = r.price_aliases or self.defaults.get("price_aliases", [])
        r.exclude_aliases = r.exclude_aliases or self.defaults.get("exclude_aliases", [])
        r.best_price_alias = r.best_price_alias or self.defaults.get("best_price_alias")
        return r


# ---------------------------------------------------------------------------
# Utilidades de parsing
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"[^\d,.\-]")


def to_number(value) -> Optional[float]:
    """Convierte strings tipo '$ 1.234.567,89' o '12,300' a float COP."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v >= 5 else None
    s = str(value).strip()
    if not s or s.upper() in {"#N/D", "N/D", "NA", "-"}:
        return None
    s = _MONEY_RE.sub("", s)
    if not s:
        return None
    # Heurística separador miles/decimales (formato COP: punto miles, coma decimal).
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # coma como decimal si hay 1-2 dígitos tras ella, si no es miles.
        if re.search(r",\d{1,2}$", s):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        # solo puntos: si parece miles (xxx.xxx) quitarlos.
        if re.search(r"\.\d{3}(\.\d{3})*$", s) and not re.search(r"\.\d{1,2}$", s):
            s = s.replace(".", "")
    try:
        num = float(s)
        # Piso para descartar ruido (cantidades=1, factores IVA=0.19, etc.).
        return num if num >= 5 else None
    except ValueError:
        return None


def _norm_header(x) -> str:
    import unicodedata
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _detect_header_row(df: pd.DataFrame, keywords: list) -> Optional[int]:
    kws = [_norm_header(k) for k in keywords]
    for i in range(min(len(df), 30)):
        cells = [_norm_header(c) for c in df.iloc[i].tolist()]
        joined = " | ".join(cells)
        if any(k in joined for k in kws):
            return i
    return None


def _find_col(cols_norm: list, aliases: list) -> Optional[int]:
    # Prioridad por alias: se evalúa el primer alias contra todas las columnas
    # antes de pasar al siguiente. Así 'descripcion del item' gana sobre 'item'.
    for a in aliases:
        an = _norm_header(a)
        if not an:
            continue
        for idx, c in enumerate(cols_norm):
            if an in c:
                return idx
    return None


# ---------------------------------------------------------------------------
# Parsers por formato
# ---------------------------------------------------------------------------

def _tidy_rows(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    df = df[df["descripcion"].astype(str).str.strip().ne("")]
    df = df[df["precio"].notna()]
    return df.reset_index(drop=True)


def _parse_generic_table(raw: pd.DataFrame, rule: FileRule, filename: str) -> pd.DataFrame:
    """Parser tabular genérico: detecta encabezado, columna desc/unidad y todas
    las columnas de precio (cada una es un proveedor)."""
    hr = _detect_header_row(raw, rule.header_keywords)
    if hr is None:
        return pd.DataFrame()
    header = [_norm_header(c) for c in raw.iloc[hr].tolist()]
    body = raw.iloc[hr + 1 :].reset_index(drop=True)

    desc_i = _find_col(header, rule.desc_aliases)
    unit_i = _find_col(header, rule.unit_aliases)
    if desc_i is None:
        return pd.DataFrame()

    excl = [_norm_header(a) for a in (rule.exclude_aliases or [])]

    def _is_excluded(col_name: str) -> bool:
        return any(a and a in col_name for a in excl)

    # Columnas de precio: las que matchean price_aliases o están a la derecha de
    # la unidad y contienen valores numéricos. Se descartan columnas de
    # cantidad/total/IVA por nombre de encabezado.
    price_cols: list[int] = []
    for idx, c in enumerate(header):
        if idx in (desc_i, unit_i) or _is_excluded(c):
            continue
        if any(_norm_header(a) in c for a in rule.price_aliases):
            price_cols.append(idx)
    if not price_cols:
        # fallback: columnas numéricas a la derecha de la descripción, excluyendo
        # las de cantidad/total/IVA por nombre.
        for idx in range(desc_i + 1, raw.shape[1]):
            if idx < len(header) and _is_excluded(header[idx]):
                continue
            col_vals = body.iloc[:, idx].map(to_number)
            if col_vals.notna().sum() >= max(3, len(body) * 0.2):
                price_cols.append(idx)

    records = []
    for _, row in body.iterrows():
        desc = row.iloc[desc_i] if desc_i < len(row) else None
        if desc is None or str(desc).strip() == "":
            continue
        unit = row.iloc[unit_i] if (unit_i is not None and unit_i < len(row)) else ""
        for pc in price_cols:
            if pc >= len(row):
                continue
            price = to_number(row.iloc[pc])
            if price is None:
                continue
            proveedor = header[pc] if pc < len(header) else f"col{pc}"
            records.append(
                {
                    "region": rule.region,
                    "proveedor": proveedor,
                    "descripcion": str(desc).strip(),
                    "unidad": str(unit).strip() if unit is not None else "",
                    "precio": price,
                    "archivo": filename,
                    "formato": rule.format,
                }
            )
    return _tidy_rows(records)


def parse_xlsx(content: bytes, rule: FileRule, filename: str) -> pd.DataFrame:
    xls = pd.ExcelFile(io.BytesIO(content))
    sheets = xls.sheet_names
    if rule.sheet is not None:
        sheets = [rule.sheet] if not isinstance(rule.sheet, int) else [xls.sheet_names[rule.sheet]]
    frames = []
    for sh in sheets:
        raw = xls.parse(sheet_name=sh, header=None, dtype=object)
        if raw.empty:
            continue
        frames.append(_parse_generic_table(raw, rule, f"{filename}::{sh}"))
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def parse_csv(content: bytes, rule: FileRule, filename: str) -> pd.DataFrame:
    for sep in (";", ",", "\t"):
        try:
            raw = pd.read_csv(io.BytesIO(content), sep=sep, header=None, dtype=object, engine="python")
            if raw.shape[1] >= 2:
                out = _parse_generic_table(raw, rule, filename)
                if not out.empty:
                    return out
        except Exception:
            continue
    return pd.DataFrame()


def parse_pdf(content: bytes, rule: FileRule, filename: str) -> pd.DataFrame:
    import pdfplumber

    frames = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue
                raw = pd.DataFrame(table)
                out = _parse_generic_table(raw, rule, filename)
                if not out.empty:
                    frames.append(out)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# Orquestación de carga
# ---------------------------------------------------------------------------

def _dispatch(content: bytes, filename: str, rule: FileRule) -> pd.DataFrame:
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return parse_xlsx(content, rule, filename)
    if ext == ".csv":
        return parse_csv(content, rule, filename)
    if ext == ".pdf":
        return parse_pdf(content, rule, filename)
    return pd.DataFrame()


def load_from_gcs(bucket_name: str, prefix: str, config_path: str, storage_client=None) -> pd.DataFrame:
    """Descarga todos los comparativos del bucket y devuelve un DataFrame tidy."""
    from google.cloud import storage

    cfg = ComparativosConfig(config_path)
    client = storage_client or storage.Client()
    bucket = client.bucket(bucket_name)

    frames = []
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        if blob.name.endswith("/"):
            continue
        filename = os.path.basename(blob.name)
        rule = cfg.rule_for(filename)
        if rule is None:
            print(f"[comparativos] sin regla para '{filename}', se omite.")
            continue
        content = blob.download_as_bytes()
        try:
            df = _dispatch(content, filename, rule)
            if not df.empty:
                frames.append(df)
                print(f"[comparativos] {filename}: {len(df)} precios ({rule.region}).")
            else:
                print(f"[comparativos] {filename}: 0 filas parseadas.")
        except Exception as exc:
            print(f"[comparativos] ERROR en {filename}: {exc}")

    if not frames:
        return pd.DataFrame(
            columns=["region", "proveedor", "descripcion", "unidad", "precio", "archivo", "formato"]
        )
    return pd.concat(frames, ignore_index=True)


def load_from_dir(local_dir: str, config_path: str) -> pd.DataFrame:
    """Igual que load_from_gcs pero leyendo de un directorio local (tests/dev)."""
    cfg = ComparativosConfig(config_path)
    frames = []
    for name in os.listdir(local_dir):
        rule = cfg.rule_for(name)
        if rule is None:
            continue
        with open(os.path.join(local_dir, name), "rb") as fh:
            content = fh.read()
        df = _dispatch(content, name, rule)
        if not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

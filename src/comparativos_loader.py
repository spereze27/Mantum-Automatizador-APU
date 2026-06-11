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
        fn = _slug(filename)
        for r in self.rules:
            if _slug(r.match) in fn:
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
    """Convierte a float COP. Convención colombiana ESTRICTA:
    punto = separador de miles, coma = separador decimal.
    Ej: '1.774' -> 1774 ; '2.075,56' -> 2075.56 ; '$ 379.015' -> 379015.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v >= 5 else None
    s = str(value).strip()
    if not s or s.upper() in {"#N/D", "N/D", "NA", "-", "#REF!", "#VALUE!"}:
        return None
    s = _MONEY_RE.sub("", s)  # deja solo dígitos, . , -
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s:
        # coma decimal -> los puntos son miles
        s = s.replace(".", "").replace(",", ".")
    else:
        # solo puntos (o ninguno) -> en COP los puntos son miles
        s = s.replace(".", "")
    try:
        v = float(s)
        # Piso para descartar ruido (cantidades=1, factores IVA, etc.).
        return v if v >= 5 else None
    except ValueError:
        return None


def _norm_header(x) -> str:
    import unicodedata
    s = "" if x is None else str(x)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _slug(s: str) -> str:
    """Reduce un nombre a solo alfanuméricos en minúscula, para comparar
    nombres de archivo de forma robusta a espacios, guiones, puntos y mayúsculas.
    Ej: 'Barranquilla V.02.pdf' -> 'barranquillav02pdf' contiene 'barranquillav02'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _slug(s: str) -> str:
    """Reduce un nombre a solo letras/dígitos en minúscula para comparar nombres
    de archivo de forma robusta a espacios, puntos, guiones y mayúsculas.
    Ej: 'Barranquilla V.02.pdf' -> 'barranquillav02pdf'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


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

MAX_PRICE_COP = 100_000_000  # tope de cordura: descarta teléfonos/NITs/garbage.

_PROV_NOISE = [
    "telefono", "direccion", "nit", "vunitario", "vrunitariototal", "valorunitario",
    "vunitario", "unitario", "total", "antesdeiva", "iva", "credito", "dias",
    "medida", "cantidad", "vtotal", "nan", "mejor", "precio", "costo", "directo", "vr",
]


def _clean_provider(label: str) -> str:
    """Limpia la etiqueta de la columna de precio para dejar el nombre del
    proveedor (quita 'telefono', cifras sueltas, 'v/unitario', etc.)."""
    toks = re.split(r"[\s/]+", str(label).strip())
    keep = []
    for t in toks:
        tn = _norm_header(t)
        if not tn or tn.isdigit() or any(n in tn for n in _PROV_NOISE):
            continue
        keep.append(t)
    return " ".join(keep).strip() or "Cotización"


def _tidy_rows(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    df = df[df["descripcion"].astype(str).str.strip().ne("")]
    df = df[df["precio"].notna()]
    df = df[df["precio"] <= MAX_PRICE_COP]  # quita teléfonos/NITs/totales absurdos
    return df.reset_index(drop=True)


def _combine_header(raw: pd.DataFrame, hr: int) -> tuple[list, int]:
    """Combina encabezados repartidos en varias filas (ej. 'VR UNITARIO' arriba
    y 'TOTAL' abajo) en un solo encabezado por columna. Devuelve (header, fila_datos)."""
    headers = [_norm_header(c) for c in raw.iloc[hr].tolist()]
    ncols = len(headers)
    data_start = hr + 1
    for k in range(hr + 1, min(hr + 4, len(raw))):
        rowvals = raw.iloc[k].tolist()
        numeric = sum(1 for v in rowvals if to_number(v) is not None)
        nonempty = sum(1 for v in rowvals if pd.notna(v) and str(v).strip() != "")
        # Fila de continuación de encabezado: tiene texto pero casi nada numérico.
        if nonempty > 0 and numeric <= max(1, int(nonempty * 0.3)):
            for i, v in enumerate(rowvals):
                if i < ncols and pd.notna(v) and str(v).strip() != "":
                    add = _norm_header(v)
                    if add and add not in headers[i]:
                        headers[i] = (headers[i] + " " + add).strip()
            data_start = k + 1
        else:
            data_start = k
            break
    return headers, data_start


# Columnas que NUNCA son el precio unitario final (se excluyen siempre).
_HARD_EXCLUDE = [
    "iva", "costodirecto", "costo directo", "subtotal", "antesdeiva", "antes de iva",
    "descuento", "dscto", "cant", "cantidad", "consumo", "desperdicio",
    "vlrtotal", "vlr total", "valortotal", "valor total", "vrtotal", "vr total",
    "fecha", "nro", "telefono", "direccion", "nit",
]


def _select_price_cols(header: list, rule: "FileRule") -> list:
    """Selecciona columna(s) de precio unitario CON IVA, evitando IVA/%/costo directo/totales de fila.
    Prioridad: 0) best_price_alias del config (override exacto)  1) 'unitario total'
    2) 'vr/valor unitario'  3) 'TOTAL' (patrón antes de iva/iva/total)  4) alias del config."""
    norm = [c.replace(" ", "") for c in header]

    def is_iva_or_pct(cc):  # el IVA o cualquier columna de porcentaje NUNCA es el precio
        return "iva" in cc or "%" in cc or "porcentaje" in cc

    def is_unitario(cc):
        return "unitario" in cc and not is_iva_or_pct(cc)

    def is_rowtotal(cc):  # total de fila (unit x cantidad), no es precio unitario
        return any(t in cc for t in ["vtotal", "vlrtotal", "valortotal", "vrtotal", "subtotal"])

    def hard_excluded(cc):
        if is_iva_or_pct(cc):
            return True
        if "unitario" in cc:           # un 'unitario' no se excluye por colisión
            return False
        return any(_norm_header(a).replace(" ", "") in cc for a in _HARD_EXCLUDE)

    # 0) Override explícito del config: el alias de "mejor precio"/columna final.
    #    Máxima prioridad y verificable por archivo.
    if rule.best_price_alias:
        bpa = _norm_header(rule.best_price_alias).replace(" ", "")
        ov = [i for i, cc in enumerate(norm) if bpa and bpa in cc and not is_iva_or_pct(cc)]
        if ov:
            return ov
    # 1) Precio unitario total (con IVA): máxima prioridad heurística.
    ut = [i for i, cc in enumerate(norm) if "unitario" in cc and "total" in cc and not is_iva_or_pct(cc)]
    if ut:
        return ut
    # 2) Vr/Valor unitario por proveedor.
    vu = [i for i, cc in enumerate(norm) if is_unitario(cc)]
    if vu:
        return vu
    # 3) Patrón "ANTES DE IVA / IVA / TOTAL": el precio con IVA es la columna TOTAL.
    hay_iva = any("iva" in cc for cc in norm)
    if hay_iva:
        tot = [i for i, cc in enumerate(norm)
               if "total" in cc and not is_rowtotal(cc) and not is_iva_or_pct(cc) and "unitario" not in cc]
        if tot:
            return tot
    # 4) Alias del config, excluyendo lo prohibido y los totales de fila.
    pa = [i for i, cc in enumerate(norm)
          if any(_norm_header(a).replace(" ", "") in cc for a in (rule.price_aliases or []))
          and not hard_excluded(cc) and not is_rowtotal(cc)]
    return pa


def _parse_generic_table(raw: pd.DataFrame, rule: FileRule, filename: str) -> pd.DataFrame:
    """Parser tabular genérico: detecta encabezado (incluso multi-fila), columna
    desc/unidad y la columna de precio unitario correcta (con IVA, no el IVA)."""
    hr = _detect_header_row(raw, rule.header_keywords)
    if hr is None:
        return pd.DataFrame()
    header, data_start = _combine_header(raw, hr)
    body = raw.iloc[data_start:].reset_index(drop=True)

    desc_i = _find_col(header, rule.desc_aliases)
    unit_i = _find_col(header, rule.unit_aliases)
    if desc_i is None:
        return pd.DataFrame()

    price_cols = _select_price_cols(header, rule)
    if price_cols:
        cols_elegidas = [header[i] if i < len(header) else f"col{i}" for i in price_cols]
        print(f"[precio] {filename}: columna(s) de precio = {cols_elegidas}")
    if not price_cols:
        # Último recurso: columnas numéricas a la derecha de la descripción,
        # excluyendo por nombre cantidad/total/IVA/costo directo.
        excl = [_norm_header(a) for a in (rule.exclude_aliases or [])] + \
               [_norm_header(a) for a in _HARD_EXCLUDE]
        for idx in range(desc_i + 1, raw.shape[1]):
            cname = header[idx] if idx < len(header) else ""
            cc = cname.replace(" ", "")
            if "unitario" in cc and "total" in cc:
                pass  # nunca excluir el unitario total
            elif any(a and a.replace(" ", "") in cc for a in excl):
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
            col_label = header[pc] if pc < len(header) else f"col{pc}"
            records.append(
                {
                    "region": rule.region,
                    "proveedor": col_label,
                    "descripcion": str(desc).strip(),
                    "unidad": str(unit).strip() if unit is not None else "",
                    "precio": price,
                    "archivo": filename,
                    "formato": rule.format,
                    "columna_precio": col_label,
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
                df["gcs_path"] = blob.name
                df["fuente_tipo"] = "Cotización proveedor"
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

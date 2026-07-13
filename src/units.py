"""Unidades, presentaciones y estimadores robustos de precio.

Este módulo concentra TRES responsabilidades que antes estaban dispersas (y
duplicadas) en `pipeline.py`, y que eran la causa raíz de los errores de
magnitud del reporte 20260710 (Cemento 50 Kg a $35.500/Kg = 30x, Tornillo a
$5.042/Und = 29x, Pegacor a $48.000/Kg = 17x):

1. `canon_unit()`  -> UNA sola canonización de unidad (antes había dos: la buena,
   `_unit_canon`, y la pobre, `_canon_unit`, que hacía fallar los niveles T0/T1
   de `_ref_estandarizada` en 526 de 752 ítems).

2. `factor_presentacion()` -> cuántas unidades del warehouse cubre el precio de
   una aparición. Antes solo se DETECTABA la otra presentación para EXCLUIRLA
   (`_MULT_RE`); ahora se CONVIERTE:
       "1/4 galón" a $37.500   -> factor 0.25 -> $150.000 / gl
       "cuñete"    a $426.900  -> factor 5    -> $85.380  / gl
       "Cemento 50 Kg" a $35.500 -> factor 50 -> $710     / kg

3. `referencia_robusta()` -> el estimador. Se abandona el MÁXIMO (order statistic
   que NO converge: con n=2.033 facturas de 'Ayudante' el máximo es el p100 y
   crece con el tamaño de la muestra) por un CUANTIL ALTO WINSORIZADO, ponderado
   por recencia.

Además `rango_plausible()` da un guardarraíl ABSOLUTO por familia de ítem
(config/plausibilidad.yaml), para el caso en el que ni la BD ni el mercado son
confiables por sí solos.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# 1. Unidades canónicas
# ---------------------------------------------------------------------------

# Unidades SIN dimensión física: no tienen "precio unitario" que se pueda cruzar
# contra un mercado. En el reporte anterior "Transporte Materiales" (unidad '%',
# valor_wh = 0) recibió $130.000 -> +$9,6 M ponderados. No se procesan.
NON_DIMENSIONAL = {"", "%", "pct", "porcentaje", "glb", "global", "gbl", "na", "n/a"}

_ALIAS = {
    "m3": "m3", "mt3": "m3", "mts3": "m3", "metrocubico": "m3", "metroscubicos": "m3",
    "m2": "m2", "mt2": "m2", "mts2": "m2", "metrocuadrado": "m2", "metroscuadrados": "m2",
    "m": "m", "ml": "m", "mt": "m", "mts": "m", "metro": "m", "metros": "m", "mlineal": "m",
    "mm": "mm", "milimetro": "mm", "milimetros": "mm",
    "cm": "cm", "centimetro": "cm", "centimetros": "cm",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogramo": "kg", "kilogramos": "kg",
    "gr": "gr", "g": "gr", "gramo": "gr", "gramos": "gr",
    "ton": "ton", "tonelada": "ton", "toneladas": "ton", "tn": "ton",
    "gl": "gl", "gal": "gl", "galon": "gl", "galones": "gl", "galn": "gl",
    "lb": "lb", "lbs": "lb", "libra": "lb", "libras": "lb",
    "in": "in", "pulg": "in", "pulgada": "in", "pulgadas": "in",
    "l": "l", "lt": "l", "lts": "l", "litro": "l", "litros": "l",
    "ml_u": "ml_u", "cc": "ml_u", "mililitro": "ml_u", "mililitros": "ml_u",
    "und": "und", "un": "und", "ud": "und", "unidad": "und", "unidades": "und",
    "u": "und", "c": "und", "cu": "und", "pza": "und", "pieza": "und",
    "caja": "caja", "cja": "caja", "cjs": "caja", "cajas": "caja",
    "bulto": "bulto", "bto": "bulto", "bultos": "bulto",
    "rollo": "rollo", "rollos": "rollo", "rll": "rollo",
    "kit": "kit", "juego": "kit", "jgo": "kit", "juegos": "kit",
    "par": "par", "pares": "par",
    "paq": "paq", "paquete": "paq", "paquetes": "paq",
    "hr": "hr", "hora": "hr", "horas": "hr", "h": "hr", "hh": "hr",
    "dia": "dia", "dias": "dia", "jornal": "dia", "jornales": "dia",
    "viaje": "viaje", "viajes": "viaje", "vje": "viaje",
    "cunete": "cunete", "cunetes": "cunete", "caneca": "cunete", "canecas": "cunete",
}


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def canon_unit(s) -> str:
    """Unidad canónica a partir de una celda de unidad. '' si es ambigua o
    no reconocible (no se usa para filtrar). ÚNICA fuente de verdad: no
    reimplementar esto en otro módulo."""
    u = _strip_accents(s).strip().lower()
    u = u.replace("³", "3").replace("²", "2").replace('"', "in")
    u = re.sub(r"[^a-z0-9]", "", u)
    return _ALIAS.get(u, "")


def es_dimensional(und) -> bool:
    """True si la unidad admite un precio unitario cruzable contra el mercado."""
    raw = re.sub(r"[^a-z0-9%]", "", _strip_accents(und).strip().lower())
    if raw in NON_DIMENSIONAL:
        return False
    return bool(canon_unit(und))


def detect_unit_in_text(text: str) -> str:
    """Detecta una unidad fuerte dentro de una descripción ('ARENA ... POR M3')."""
    t = " " + re.sub(r"[^a-z0-9 ]", " ", _strip_accents(text).lower()) + " "
    for tok, canon in [(" m3 ", "m3"), (" m2 ", "m2"), (" kg ", "kg"), (" gl ", "gl"),
                       (" galon ", "gl"), (" galones ", "gl"), (" lb ", "lb"),
                       (" ml ", "m"), (" mts ", "m"), (" m ", "m")]:
        if tok in t:
            return canon
    return ""


# ---------------------------------------------------------------------------
# 2. Factor de presentación (normalización a la unidad del warehouse)
# ---------------------------------------------------------------------------

# Familia -> {unidad canónica: cuántas unidades BASE de la familia contiene}.
_FAMILIAS = {
    "masa": {"gr": 1.0, "kg": 1000.0, "lb": 453.592, "ton": 1_000_000.0},
    "volumen": {"ml_u": 1.0, "l": 1000.0, "gl": 3785.41, "cunete": 5 * 3785.41},
    "longitud": {"mm": 1.0, "cm": 10.0, "in": 25.4, "m": 1000.0},
    "area": {"m2": 1.0},
    "vol_obra": {"m3": 1.0},
    "conteo": {"und": 1.0},
    "tiempo": {"hr": 1.0, "dia": 8.0},  # 1 jornal = 8 h
}
_UNIT2FAM = {u: f for f, d in _FAMILIAS.items() for u in d}

# Envases con contenido implícito. Solo se usan si la unidad del WH pertenece a
# la MISMA familia (un 'cuñete' solo tiene sentido convertido a gl/l).
_ENVASES = {
    "cunete": ("volumen", 5 * 3785.41),      # cuñete/caneca = 5 galones
    "caneca": ("volumen", 5 * 3785.41),
    "bulto": ("masa", 50 * 1000.0),          # bulto de cemento = 50 kg
    "saco": ("masa", 50 * 1000.0),
}

_FRACCIONES = {"1/8": 0.125, "1/4": 0.25, "1/2": 0.5, "3/4": 0.75, "1/16": 0.0625}

_UNIDS_TXT = (
    r"(?:gl|gal|galones?|gal[oó]n|kg|kilos?|kilogramos?|gr|gramos?|lb|libras?|"
    r"lt|lts|litros?|l|ml|cc|mm|cm|m2|m3|mts?|metros?|m|ton|toneladas?)"
)
# "x 27 Kg", "50 Kg", "1.2 m", "500L", "300ml"
_RE_QTY_UNIT = re.compile(r"(?:x\s*)?(\d+(?:[.,]\d+)?)\s*(" + _UNIDS_TXT + r")\b")
# "1/4 galón", "1/2 kg"
_RE_FRAC_UNIT = re.compile(r"(\d+/\d+)\s*(" + _UNIDS_TXT + r")?\b")


def _norm_txt(s) -> str:
    return re.sub(r"\s+", " ", _strip_accents(s).lower()).strip()


def factor_presentacion(descripcion, unidad_apar, unidad_wh) -> tuple[float, str]:
    """Cuántas unidades del warehouse cubre el precio de esta aparición.

    Devuelve (factor, explicación). `precio_normalizado = precio / factor`.
    factor = 1.0 cuando la aparición ya está en la unidad del WH (o no se puede
    determinar con confianza: NO se inventa nada).

    Ejemplos (unidad_wh='gl'):
        "Viniltex 1/4 galón"     -> (0.25, "1/4 gl")
        "Viniltex cuñete"        -> (5.0,  "cuñete = 5 gl")
    Ejemplos (unidad_wh='kg'):
        "Cemento gris x 50 Kg"   -> (50.0, "50 kg")
        "Pegacor blanco (25 Kg)" -> (25.0, "25 kg")
    """
    wh = canon_unit(unidad_wh)
    if not wh or wh not in _UNIT2FAM:
        return 1.0, ""
    fam = _UNIT2FAM[wh]
    base_wh = _FAMILIAS[fam][wh]  # unidades base de la familia por 1 unidad WH
    txt = _norm_txt(descripcion)

    # ORDEN DE PRECEDENCIA: lo EXPLÍCITO del texto gana sobre el envase genérico.
    # "Pegacor blanco (25 Kg)" con unidad declarada 'BULTO' son 25 kg, no los 50 kg
    # del bulto estándar de cemento.

    # (b) Fracción explícita en el texto: "1/4 galón", "1/2".
    for m in _RE_FRAC_UNIT.finditer(txt):
        frac = _FRACCIONES.get(m.group(1))
        if not frac:
            continue
        u = canon_unit(m.group(2)) if m.group(2) else wh
        if u and _UNIT2FAM.get(u) == fam:
            f = frac * _FAMILIAS[fam][u] / base_wh
            if f > 0:
                return round(f, 6), f"{m.group(1)} {u} = {_fmt_f(f)} {wh}"

    # (c) Cantidad + unidad en el texto: "x 27 Kg", "50 Kg", "500 L", "6 m".
    #     Se toma la coincidencia de MAYOR factor (la presentación dominante),
    #     evitando confundir "1/4" con el diámetro de un tubo.
    mejor = None
    for m in _RE_QTY_UNIT.finditer(txt):
        try:
            qty = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        u = canon_unit(m.group(2))
        if not u or _UNIT2FAM.get(u) != fam or qty <= 0:
            continue
        f = qty * _FAMILIAS[fam][u] / base_wh
        if f <= 0 or abs(f - 1.0) < 1e-9:
            continue
        if mejor is None or f > mejor[0]:
            mejor = (f, f"{_fmt_f(qty)} {u} = {_fmt_f(f)} {wh}")
    if mejor:
        return round(mejor[0], 6), mejor[1]

    # (d) Envase nombrado en el texto ("cuñete", "caneca", "bulto").
    for pal, (efam, cont) in _ENVASES.items():
        if efam == fam and re.search(r"\b" + pal + r"s?\b", txt):
            f = cont / base_wh
            if f > 0:
                return round(f, 6), f"{pal} = {_fmt_f(f)} {wh}"

    # (e) Último recurso: la unidad DECLARADA de la aparición ('BULTO', 'CUÑETE',
    #     o una unidad de la misma familia que el WH, p.ej. litros contra galones).
    ua = canon_unit(unidad_apar)
    if ua and ua != wh:
        env = _ENVASES.get(ua)
        if env and env[0] == fam:
            f = env[1] / base_wh
            if f > 0:
                return round(f, 6), f"unidad '{ua}' = {_fmt_f(f)} {wh}"
        if ua in _UNIT2FAM and _UNIT2FAM[ua] == fam:
            f = _FAMILIAS[fam][ua] / base_wh
            if f > 0 and abs(f - 1.0) > 1e-9:
                return round(f, 6), f"unidad '{ua}' = {_fmt_f(f)} {wh}"

    return 1.0, ""


def _fmt_f(x: float) -> str:
    return f"{x:g}"


# ---------------------------------------------------------------------------
# 3. Plausibilidad absoluta por familia de ítem (config/plausibilidad.yaml)
# ---------------------------------------------------------------------------

_PLAUS_CACHE: Optional[list] = None


def cargar_plausibilidad(path: str = "config/plausibilidad.yaml") -> list:
    """Lee las reglas [patron, unidad, min, max]. Fail-soft: sin archivo, sin reglas."""
    global _PLAUS_CACHE
    if _PLAUS_CACHE is not None:
        return _PLAUS_CACHE
    reglas = []
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for r in (data.get("reglas") or []):
            reglas.append({
                "patron": re.compile(str(r["patron"]), re.I),
                "unidad": canon_unit(r.get("unidad", "")),
                "min": float(r["min"]),
                "max": float(r["max"]),
                "nombre": str(r.get("nombre", r["patron"])),
            })
    except Exception as exc:  # pragma: no cover
        print(f"[units] plausibilidad no cargada ({exc}); se continúa sin guardarraíl absoluto.")
    _PLAUS_CACHE = reglas
    return reglas


def rango_plausible(descripcion, unidad, path: str = "config/plausibilidad.yaml"):
    """(min, max, nombre) del rango de precio unitario esperado, o None si no hay
    regla para este ítem/unidad. Es un guardarraíl ABSOLUTO: sirve justo cuando
    NI la BD NI el mercado son confiables por sí solos (caso Cemento 50 Kg)."""
    u = canon_unit(unidad)
    if not u:
        return None
    txt = _norm_txt(descripcion)
    for r in cargar_plausibilidad(path):
        if r["unidad"] == u and r["patron"].search(txt):
            return r["min"], r["max"], r["nombre"]
    return None


def es_plausible(precio, descripcion, unidad, path: str = "config/plausibilidad.yaml") -> bool:
    """False solo si hay una regla explícita y el precio la viola."""
    rg = rango_plausible(descripcion, unidad, path)
    if rg is None or precio is None:
        return True
    lo, hi, _ = rg
    return lo <= float(precio) <= hi


# ---------------------------------------------------------------------------
# 4. Estimador robusto (reemplaza al MÁXIMO)
# ---------------------------------------------------------------------------

def _peso_recencia(fecha, halflife_days: float, hoy: Optional[dt.date] = None) -> float:
    """Decaimiento exponencial por antigüedad. 1.0 si no hay fecha o está apagado."""
    if not halflife_days or halflife_days <= 0:
        return 1.0
    s = str(fecha or "").strip()[:10]
    if not s:
        return 1.0
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
            break
        except ValueError:
            d = None
    if d is None:
        return 1.0
    hoy = hoy or dt.date.today()
    dias = hoy.toordinal() - d.toordinal()
    if dias <= 0:
        return 1.0
    return 0.5 ** (dias / float(halflife_days))


def _weighted_quantile(valores, pesos, q: float) -> float:
    """Cuantil ponderado (interpolación por CDF). valores/pesos NO vacíos."""
    pares = sorted(zip(valores, pesos), key=lambda p: p[0])
    total = sum(p for _, p in pares)
    if total <= 0:
        vals = [v for v, _ in pares]
        idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
        return float(vals[idx])
    acum = 0.0
    objetivo = q * total
    for v, p in pares:
        acum += p
        if acum >= objetivo:
            return float(v)
    return float(pares[-1][0])


def _winsorize(valores, pct: float):
    """Recorta las colas al percentil pct / (100-pct) (no las elimina: las topa)."""
    if not valores or pct <= 0:
        return list(valores)
    vs = sorted(valores)
    n = len(vs)
    if n < 4:
        return list(valores)
    lo_i = max(0, int(math.floor((pct / 100.0) * (n - 1))))
    hi_i = min(n - 1, int(math.ceil((1 - pct / 100.0) * (n - 1))))
    lo, hi = vs[lo_i], vs[hi_i]
    return [min(max(v, lo), hi) for v in valores]


def referencia_robusta(
    precios: Iterable[float],
    pesos: Optional[Iterable[float]] = None,
    estimator: str = "p75",
    quantile: float = 0.75,
    winsor_pct: float = 5.0,
) -> tuple[Optional[float], int]:
    """Precio de referencia a partir de las apariciones YA normalizadas a la
    unidad del WH.

    `estimator`:
      - 'p75' / 'p90' / 'mediana' / 'promedio' -> cuantil (o media) ponderado
        sobre valores WINSORIZADOS. Es lo recomendado: converge con n.
      - 'max' -> comportamiento legacy (MÁXIMO). NO usar: el máximo de n=2.033
        facturas es el p100; crece con el tamaño de la muestra y por eso el
        reporte anterior infló 'Ayudante' y 'Oficial' en +$280 M ponderados.

    Devuelve (referencia, n_usadas).
    """
    ps = [float(p) for p in (precios or []) if p is not None and float(p) > 0]
    if not ps:
        return None, 0
    ws = [float(w) for w in (pesos or [])] if pesos is not None else [1.0] * len(ps)
    if len(ws) != len(ps):
        ws = [1.0] * len(ps)

    est = str(estimator or "p75").strip().lower()
    if est == "max":
        return round(max(ps), 2), len(ps)

    vals = _winsorize(ps, winsor_pct)
    if est in ("promedio", "mean", "avg"):
        tot = sum(ws) or float(len(vals))
        ref = sum(v * w for v, w in zip(vals, ws)) / tot
        return round(ref, 2), len(vals)

    q = quantile
    if est in ("mediana", "median", "p50"):
        q = 0.50
    elif est == "p75":
        q = 0.75
    elif est == "p90":
        q = 0.90
    q = min(max(float(q), 0.0), 1.0)
    return round(_weighted_quantile(vals, ws, q), 2), len(vals)


def peso_aparicion(fecha=None, cantidad=None, halflife_days: float = 365.0,
                   weight_by_qty: bool = False) -> float:
    """Peso de una aparición: recencia (siempre) x cantidad (opcional).

    OJO con `weight_by_qty`: ponderar por cantidad sesga la referencia hacia los
    precios de COMPRA AL POR MAYOR (con descuento por volumen), que NO son el
    precio de lista que se quiere publicar en el APU. Por eso viene apagado por
    defecto (REF_WEIGHT_BY_QTY=false) aunque está implementado.
    """
    w = _peso_recencia(fecha, halflife_days)
    if weight_by_qty:
        try:
            q = float(cantidad or 0)
            if q > 0:
                w *= 1.0 + math.log1p(q)
        except (TypeError, ValueError):
            pass
    return max(w, 1e-6)

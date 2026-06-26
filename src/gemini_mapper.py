"""Resolución de cruces e investigación de precios con Gemini.

Dos clases:
  - GeminiResolver: segunda pasada para cruces dudosos (elige el mejor candidato).
  - GeminiPriceResearcher: ÚLTIMO recurso cuando un ítem no tiene fuente interna o
    cuando TODAS las fuentes quedaron fuera del rango de cordura; busca un precio de
    referencia EN INTERNET (grounding Google Search) y devuelve precio + unidad +
    ENLACE de la fuente.

Backend unificado con el SDK `google-genai` (un solo camino de código):
  1) Si hay GEMINI_API_KEY  -> Client(api_key=...)            (Gemini Developer API)
  2) Si NO hay api_key       -> Client(vertexai=True, project, location)  (Vertex AI
     con ADC = la service account con la que corre Cloud Run; requiere el rol
     roles/aiplatform.user y la API aiplatform habilitada).
El grounding de Google Search es idéntico en ambos modos.

Ambas clases son FAIL-SOFT (si no hay backend, enabled=False y devuelven None) y
tienen un circuit breaker: tras 3 fallos seguidos (p.ej. sin cuota) se apagan por
el resto de la corrida para no inflar el tiempo del pipeline.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Backend unificado (google-genai: API key  ó  Vertex AI/ADC)                  #
# --------------------------------------------------------------------------- #
def _build_backend(api_key, project, location, model_name):
    """Devuelve (client, model_name, types_mod) o (None, None, None)."""
    try:
        from google import genai
        from google.genai import types as gtypes
    except Exception as exc:  # pragma: no cover
        print(f"[gemini] SDK google-genai no disponible: {exc}")
        return None, None, None

    # Timeout corto y SIN reintentos: si no hay cuota (429) o hay error, que falle
    # RÁPIDO (lo combinamos con el circuit breaker de cada clase).
    try:
        http = gtypes.HttpOptions(
            timeout=20000,  # ms
            retry_options=gtypes.HttpRetryOptions(attempts=1),
        )
    except Exception:  # versiones viejas del SDK sin esos campos
        http = None

    try:
        if api_key:
            client = genai.Client(api_key=api_key, http_options=http) if http else genai.Client(api_key=api_key)
            print("[gemini] backend: API key (Gemini Developer API)")
        else:
            kwargs = dict(vertexai=True, project=project, location=location)
            if http:
                kwargs["http_options"] = http
            client = genai.Client(**kwargs)
            print(f"[gemini] backend: Vertex AI (ADC) project={project} location={location}")
        return client, model_name, gtypes
    except Exception as exc:  # pragma: no cover
        print(f"[gemini] no se pudo inicializar el cliente: {exc}")
        return None, None, None


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _grounding_url(resp) -> Optional[str]:
    """Primer enlace de las citas de grounding. Robusto a Vertex y Developer API:
    intenta grounding_chunks[].web.uri y, si vienen vacíos (caso común en Vertex),
    extrae los href del HTML de search_entry_point.rendered_content."""
    try:
        cand = resp.candidates[0]
        gm = getattr(cand, "grounding_metadata", None)
        if gm is None:
            return None
        # 1) grounding_chunks -> web.uri / retrieved_context.uri
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri:
                return uri
            rc = getattr(ch, "retrieved_context", None)
            uri = getattr(rc, "uri", None) if rc else None
            if uri:
                return uri
        # 2) Fallback: href dentro del search_entry_point renderizado.
        sep = getattr(gm, "search_entry_point", None)
        rendered = getattr(sep, "rendered_content", None) if sep else None
        if rendered:
            m = re.search(r'href="([^"]+)"', rendered)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _grounding_titles(resp) -> str:
    """Nombres/dominios de las fuentes citadas (para mostrar como 'fuente')."""
    titles = []
    try:
        gm = getattr(resp.candidates[0], "grounding_metadata", None)
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            t = getattr(web, "title", None) if web else None
            if t and t not in titles:
                titles.append(t)
    except Exception:
        pass
    return ", ".join(titles[:3])


def _mk_config(t, **kw):
    """Construye GenerateContentConfig desactivando el 'thinking' (gemini-2.5 puede
    consumir todo el presupuesto de tokens pensando y devolver text=None). Si la
    versión del SDK no soporta thinking_config, lo arma sin él."""
    try:
        return t.GenerateContentConfig(thinking_config=t.ThinkingConfig(thinking_budget=0), **kw)
    except Exception:
        return t.GenerateContentConfig(**kw)


@dataclass
class GeminiChoice:
    index: Optional[int]
    confidence: float
    reason: str


_PROMPT = """Eres un experto en insumos de construcción y mantenimiento en Colombia.
Te doy un ÍTEM del catálogo maestro y una lista de CANDIDATOS de proveedores.
Decide cuál candidato es EXACTAMENTE el mismo producto/insumo (misma cosa, mismo
tipo, dimensión y unidad equivalentes). Ignora marca si el producto es el mismo.
Si NINGÚN candidato corresponde con seguridad, responde index = null.

ÍTEM: "{item}" (unidad: "{unit}")

CANDIDATOS:
{candidates}

Responde SOLO un JSON válido, sin texto extra, con esta forma:
{{"index": <número del candidato o null>, "confidence": <0-100>, "reason": "<breve>"}}
"""


class GeminiResolver:
    def __init__(
        self,
        project: str,
        location: str = "us-central1",
        model_name: str = "gemini-2.5-flash",
        api_key: Optional[str] = None,
    ) -> None:
        self._client, self._model_name, self._types = _build_backend(
            api_key, project, location, model_name
        )
        self.enabled = self._client is not None
        self._fail_streak = 0
        self._max_fails = 3

    def resolve(self, item: str, unit: str, candidates: list[dict]) -> Optional[GeminiChoice]:
        if not self.enabled or not candidates:
            return None
        listado = "\n".join(
            f"{i}: \"{c['raw']}\" (unidad: \"{c.get('unit','')}\")"
            for i, c in enumerate(candidates)
        )
        prompt = _PROMPT.format(item=item, unit=unit, candidates=listado)
        try:
            t = self._types
            resp = self._client.models.generate_content(
                model=self._model_name, contents=prompt,
                config=_mk_config(t, temperature=0.0, max_output_tokens=512,
                                  response_mime_type="application/json"),
            )
            text = getattr(resp, "text", "") or ""
            data = _parse_json(text)
            self._fail_streak = 0
            if data is None:
                return None
            idx = data.get("index", None)
            idx = int(idx) if isinstance(idx, (int, float)) or (isinstance(idx, str) and idx.isdigit()) else None
            if idx is not None and not (0 <= idx < len(candidates)):
                idx = None
            return GeminiChoice(
                index=idx,
                confidence=float(data.get("confidence", 0) or 0),
                reason=str(data.get("reason", ""))[:200],
            )
        except Exception as exc:  # pragma: no cover
            self._fail_streak += 1
            print(f"[gemini] error resolviendo '{item[:40]}': {exc}")
            if self._fail_streak >= self._max_fails:
                self.enabled = False
                print(f"[gemini] DESHABILITADO tras {self._fail_streak} fallos seguidos "
                      f"(¿sin cuota / sin permiso aiplatform?). El resto de la corrida "
                      f"continúa SIN Gemini.")
            return None

    _parse_json = staticmethod(_parse_json)


@dataclass
class PriceResearch:
    precio: Optional[float]
    unidad: str
    fuente_url: str
    fuente_nombre: str
    confianza: float
    notas: str


_PRICE_PROMPT = """Eres un experto en precios de insumos de construcción y
mantenimiento en COLOMBIA (precios en pesos colombianos, COP). Busca EN INTERNET
el precio UNITARIO de mercado MÁS REPRESENTATIVO y ACTUAL para el siguiente ítem.

ÍTEM: "{item}"
UNIDAD esperada: "{unit}"

Reglas:
- Devuelve el precio en COP por la unidad indicada (o la unidad real del producto
  si difiere; en ese caso indícala en "unidad").
- Si el precio típico incluye IVA, da el valor ANTES de IVA si puedes estimarlo;
  si no, deja el precio tal cual y acláralo en "notas".
- Prefiere fuentes colombianas (Homecenter/Sodimac, Constructor, Ferreterías,
  catálogos de fabricante, marketplaces locales). INCLUYE EL ENLACE de la fuente.
- Si NO encuentras un precio creíble, responde precio = null.

Responde al final SOLO un JSON válido con esta forma (sin texto extra después):
{{"precio": <número COP o null>, "unidad": "<unidad>", "fuente_url": "<enlace>",
  "fuente_nombre": "<comercio/fuente>", "confidence": <0-100>, "notas": "<breve>"}}
"""


class GeminiPriceResearcher:
    """Busca un precio de referencia en internet con Gemini + Google Search."""

    def __init__(
        self,
        project: str,
        location: str = "us-central1",
        model_name: str = "gemini-2.5-flash",
        api_key: Optional[str] = None,
    ) -> None:
        self._cache: dict[str, Optional[PriceResearch]] = {}
        self._client, self._model_name, self._types = _build_backend(
            api_key, project, location, model_name
        )
        self.enabled = self._client is not None
        self._fail_streak = 0
        self._max_fails = 3

    @staticmethod
    def _to_cop(value) -> Optional[float]:
        """Normaliza un precio a float COP. Acepta número o cadena tipo
        '$ 12.500' / '12,500' / '12500.0' / '$1.078.250'. Convención CO: el punto
        suele ser separador de MILES y la coma, decimal."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else None
        raw = str(value)
        tok = next((t for t in re.split(r"\s+", raw) if re.search(r"\d", t)), raw)
        s = re.sub(r"[^\d,.\-]", "", tok)
        if not s:
            return None
        has_dot, has_comma = "." in s, "," in s
        if has_dot and has_comma:
            s = s.replace(".", "").replace(",", ".")
        elif has_comma:
            ent, _, dec = s.rpartition(",")
            s = (ent + "." + dec) if len(dec) in (1, 2) else (ent + dec)
        elif has_dot:
            if s.count(".") > 1:
                s = s.replace(".", "")
            else:
                ent, _, dec = s.rpartition(".")
                s = (ent + dec) if len(dec) == 3 else (ent + "." + dec)
        try:
            v = float(s)
            return v if v > 0 else None
        except ValueError:
            return None

    def research_price(self, item: str, unit: str = "") -> Optional[PriceResearch]:
        if not self.enabled or not str(item).strip():
            return None
        key = f"{str(item).strip().lower()}|{str(unit).strip().lower()}"
        if key in self._cache:
            return self._cache[key]
        prompt = _PRICE_PROMPT.format(item=item, unit=unit or "")
        result: Optional[PriceResearch] = None
        try:
            t = self._types
            resp = self._client.models.generate_content(
                model=self._model_name, contents=prompt,
                config=_mk_config(t, temperature=0.0, max_output_tokens=900,
                                  tools=[t.Tool(google_search=t.GoogleSearch())]),
            )
            text = getattr(resp, "text", "") or ""
            self._fail_streak = 0
            data = _parse_json(text)
            if data is not None:
                precio = self._to_cop(data.get("precio"))
                if precio is not None:
                    url = _grounding_url(resp) or str(data.get("fuente_url", "") or "")
                    nombre = _grounding_titles(resp) or str(data.get("fuente_nombre", "") or "Referencia web").strip()
                    result = PriceResearch(
                        precio=precio,
                        unidad=str(data.get("unidad", "") or unit or "").strip(),
                        fuente_url=url,
                        fuente_nombre=nombre,
                        confianza=float(data.get("confidence", 0) or 0),
                        notas=str(data.get("notas", "") or "")[:200],
                    )
        except Exception as exc:  # pragma: no cover
            self._fail_streak += 1
            print(f"[gemini-precio] error investigando '{str(item)[:40]}': {exc}")
            if self._fail_streak >= self._max_fails:
                self.enabled = False
                print(f"[gemini-precio] DESHABILITADO tras {self._fail_streak} fallos "
                      f"seguidos (¿sin cuota / sin permiso aiplatform?).")
            result = None
        self._cache[key] = result
        return result

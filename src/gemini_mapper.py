"""Resolución de cruces e investigación de precios con Gemini.

Dos clases:
  - GeminiResolver: segunda pasada para cruces dudosos (elige el mejor candidato).
  - GeminiPriceResearcher: ÚLTIMO recurso cuando un ítem no tiene ninguna fuente
    interna; busca un precio de referencia EN INTERNET (grounding Google Search) y
    devuelve precio + unidad + enlace de la fuente.

Autenticación (en este orden):
  1) API KEY del Gemini Developer API vía la variable GEMINI_API_KEY (SDK
     `google-genai`). Es la forma usada en este proyecto.
  2) Vertex AI con ADC (la runtime SA de Cloud Run) como respaldo, si no hay key.

Ambas clases son de carga perezosa y FAIL-SOFT: si no hay backend disponible,
`enabled=False` y los métodos devuelven None (el pipeline conserva el resultado
del fuzzy / el valor de la BD).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Construcción de backend (API key google-genai  ó  Vertex AI)                 #
# --------------------------------------------------------------------------- #
def _build_backend(api_key, project, location, model_name, use_search):
    """Devuelve (backend, handle, types_mod). backend ∈ {'genai','vertex',None}.

    - 'genai': handle = (client, model_name); types_mod = google.genai.types
    - 'vertex': handle = (model, GenerationConfig); types_mod = None
    """
    # 1) API key (Gemini Developer API)
    if api_key:
        try:
            from google import genai
            from google.genai import types as gtypes

            client = genai.Client(api_key=api_key)
            return "genai", (client, model_name, use_search), gtypes
        except Exception as exc:  # pragma: no cover
            print(f"[gemini] API key presente pero SDK google-genai no disponible: {exc}")

    # 2) Vertex AI (ADC)
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel

        vertexai.init(project=project, location=location)
        GenerationConfig = __import__(
            "vertexai.generative_models", fromlist=["GenerationConfig"]
        ).GenerationConfig
        tools = None
        if use_search:
            from vertexai.generative_models import Tool, grounding

            tools = [Tool.from_google_search_retrieval(grounding.GoogleSearchRetrieval())]
        model = GenerativeModel(model_name, tools=tools) if tools else GenerativeModel(model_name)
        return "vertex", (model, GenerationConfig), None
    except Exception as exc:  # pragma: no cover
        print(f"[gemini] sin backend disponible (ni API key ni Vertex): {exc}")
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
        model_name: str = "gemini-2.0-flash",
        api_key: Optional[str] = None,
    ) -> None:
        self._backend, self._handle, self._types = _build_backend(
            api_key, project, location, model_name, use_search=False
        )
        self.enabled = self._backend is not None

    def resolve(self, item: str, unit: str, candidates: list[dict]) -> Optional[GeminiChoice]:
        if not self.enabled or not candidates:
            return None
        listado = "\n".join(
            f"{i}: \"{c['raw']}\" (unidad: \"{c.get('unit','')}\")"
            for i, c in enumerate(candidates)
        )
        prompt = _PROMPT.format(item=item, unit=unit, candidates=listado)
        try:
            text = self._generate(prompt, json_mode=True, max_tokens=256)
            data = _parse_json(text)
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
            print(f"[gemini] error resolviendo '{item[:40]}': {exc}")
            return None

    def _generate(self, prompt: str, json_mode: bool, max_tokens: int):
        """Genera texto con el backend activo (sin grounding)."""
        if self._backend == "genai":
            client, model_name, _ = self._handle
            t = self._types
            kwargs = dict(temperature=0.0, max_output_tokens=max_tokens)
            if json_mode:
                kwargs["response_mime_type"] = "application/json"
            resp = client.models.generate_content(
                model=model_name, contents=prompt,
                config=t.GenerateContentConfig(**kwargs),
            )
            return getattr(resp, "text", "") or ""
        # vertex
        model, GenerationConfig = self._handle
        kwargs = dict(temperature=0.0, max_output_tokens=max_tokens)
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        resp = model.generate_content(prompt, generation_config=GenerationConfig(**kwargs))
        return getattr(resp, "text", "") or ""

    # Compat: algunos tests viejos llaman GeminiResolver._parse_json
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
  catálogos de fabricante, marketplaces locales). Incluye el enlace.
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
        model_name: str = "gemini-2.0-flash",
        api_key: Optional[str] = None,
    ) -> None:
        self._cache: dict[str, Optional[PriceResearch]] = {}
        self._backend, self._handle, self._types = _build_backend(
            api_key, project, location, model_name, use_search=True
        )
        self.enabled = self._backend is not None

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

    def _grounding_url(self, resp) -> Optional[str]:
        """Primer enlace de las citas de grounding (formato genai o vertex)."""
        try:
            cand = resp.candidates[0]
            gm = getattr(cand, "grounding_metadata", None)
            chunks = getattr(gm, "grounding_chunks", None) or []
            for ch in chunks:
                web = getattr(ch, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri:
                    return uri
        except Exception:
            pass
        return None

    def _generate_grounded(self, prompt: str):
        """Genera con grounding de Google Search. Devuelve (text, resp)."""
        if self._backend == "genai":
            client, model_name, _ = self._handle
            t = self._types
            resp = client.models.generate_content(
                model=model_name, contents=prompt,
                config=t.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=512,
                    tools=[t.Tool(google_search=t.GoogleSearch())],
                ),
            )
            return getattr(resp, "text", "") or "", resp
        # vertex (la tool ya se inyectó en el modelo)
        model, GenerationConfig = self._handle
        resp = model.generate_content(
            prompt, generation_config=GenerationConfig(temperature=0.0, max_output_tokens=512)
        )
        return getattr(resp, "text", "") or "", resp

    def research_price(self, item: str, unit: str = "") -> Optional[PriceResearch]:
        if not self.enabled or not str(item).strip():
            return None
        key = f"{str(item).strip().lower()}|{str(unit).strip().lower()}"
        if key in self._cache:
            return self._cache[key]
        prompt = _PRICE_PROMPT.format(item=item, unit=unit or "")
        result: Optional[PriceResearch] = None
        try:
            text, resp = self._generate_grounded(prompt)
            data = _parse_json(text)
            if data is not None:
                precio = self._to_cop(data.get("precio"))
                if precio is not None:
                    url = self._grounding_url(resp) or str(data.get("fuente_url", "") or "")
                    result = PriceResearch(
                        precio=precio,
                        unidad=str(data.get("unidad", "") or unit or "").strip(),
                        fuente_url=url,
                        fuente_nombre=str(data.get("fuente_nombre", "") or "Referencia web").strip(),
                        confianza=float(data.get("confidence", 0) or 0),
                        notas=str(data.get("notas", "") or "")[:200],
                    )
        except Exception as exc:  # pragma: no cover
            print(f"[gemini-precio] error investigando '{str(item)[:40]}': {exc}")
            result = None
        self._cache[key] = result
        return result

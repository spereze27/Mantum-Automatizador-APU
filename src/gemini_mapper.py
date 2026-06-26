"""Resolutor de cruces con Gemini (Vertex AI).

Se usa como SEGUNDA PASADA solo para ítems donde el fuzzy queda en zona dudosa.
Para acotar costo/latencia, a Gemini NO se le pasa todo el catálogo: se le pasa
el ítem del warehouse y una lista corta de candidatos (top-K del fuzzy), y debe
elegir cuál es el MISMO producto (o NINGUNO), devolviendo índice y confianza.

Autenticación: usa ADC (la runtime SA de Cloud Run). Requiere:
  - API aiplatform.googleapis.com habilitada (Terraform la habilita).
  - Rol roles/aiplatform.user en la runtime SA (Terraform lo concede).
Es de carga perezosa y FAIL-SOFT: si Vertex no está disponible, devuelve None y
el pipeline conserva el resultado fuzzy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class GeminiChoice:
    index: Optional[int]      # índice del candidato elegido, o None
    confidence: float          # 0-100
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
    ) -> None:
        self.enabled = False
        self._model = None
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel

            vertexai.init(project=project, location=location)
            self._model = GenerativeModel(model_name)
            self._GenerationConfig = __import__(
                "vertexai.generative_models", fromlist=["GenerationConfig"]
            ).GenerationConfig
            self.enabled = True
        except Exception as exc:  # pragma: no cover - dependencia/entorno opcional
            print(f"[gemini] deshabilitado: {exc}")
            self.enabled = False

    def resolve(self, item: str, unit: str, candidates: list[dict]) -> Optional[GeminiChoice]:
        """candidates: lista de dicts con al menos 'raw' y 'unit'."""
        if not self.enabled or not candidates:
            return None
        listado = "\n".join(
            f"{i}: \"{c['raw']}\" (unidad: \"{c.get('unit','')}\")"
            for i, c in enumerate(candidates)
        )
        prompt = _PROMPT.format(item=item, unit=unit, candidates=listado)
        try:
            resp = self._model.generate_content(
                prompt,
                generation_config=self._GenerationConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    max_output_tokens=256,
                ),
            )
            data = self._parse_json(resp.text)
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

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        if not text:
            return None
        t = text.strip()
        # Quita fences ```json ... ``` si aparecen.
        t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.MULTILINE).strip()
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
class PriceResearch:
    precio: Optional[float]    # precio de referencia en COP (antes de IVA si es posible)
    unidad: str                # unidad del precio hallado (p.ej. 'und', 'm', 'kg', 'gl')
    fuente_url: str            # enlace a la fuente (preferido: grounding de Google)
    fuente_nombre: str         # nombre/comercio de la fuente
    confianza: float           # 0-100
    notas: str


_PRICE_PROMPT = """Eres un experto en precios de insumos de construcción y
mantenimiento en COLOMBIA (precios en pesos colombianos, COP). Busca en internet
el precio UNITARIO de mercado MÁS REPRESENTATIVO y ACTUAL para el siguiente ítem.

ÍTEM: "{item}"
UNIDAD esperada: "{unit}"

Reglas:
- Devuelve el precio en COP por la unidad indicada (o la unidad real del producto
  si difiere; en ese caso indícala en "unidad").
- Si el precio típico incluye IVA, da el valor ANTES de IVA si puedes estimarlo;
  si no, deja el precio tal cual y acláralo en "notas".
- Prefiere fuentes colombianas (Homecenter/Sodimac, Constructor, Ferreterías,
  catálogos de fabricante, marketplaces locales).
- Si NO encuentras un precio creíble, responde precio = null.

Responde SOLO un JSON válido, sin texto extra:
{{"precio": <número COP o null>, "unidad": "<unidad>", "fuente_url": "<enlace>",
  "fuente_nombre": "<comercio/fuente>", "confidence": <0-100>, "notas": "<breve>"}}
"""


class GeminiPriceResearcher:
    """Busca un precio de referencia en internet usando Gemini con grounding de
    Google Search (Vertex AI). Se usa como ÚLTIMO recurso cuando un ítem no tiene
    ninguna fuente interna (consolidado/comparativo) que refute su precio.

    FAIL-SOFT: si Vertex AI o el grounding no están disponibles, `enabled=False`
    y `research_price` devuelve None (el pipeline conserva el valor de la BD).
    """

    def __init__(
        self,
        project: str,
        location: str = "us-central1",
        model_name: str = "gemini-2.0-flash",
    ) -> None:
        self.enabled = False
        self._model = None
        self._GenerationConfig = None
        self._cache: dict[str, Optional[PriceResearch]] = {}
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel, Tool, grounding

            vertexai.init(project=project, location=location)
            # Herramienta de grounding con Google Search (permite citar enlaces).
            search_tool = Tool.from_google_search_retrieval(
                grounding.GoogleSearchRetrieval()
            )
            self._model = GenerativeModel(model_name, tools=[search_tool])
            self._GenerationConfig = __import__(
                "vertexai.generative_models", fromlist=["GenerationConfig"]
            ).GenerationConfig
            self.enabled = True
        except Exception as exc:  # pragma: no cover - dependencia/entorno opcional
            print(f"[gemini-precio] deshabilitado: {exc}")
            self.enabled = False

    @staticmethod
    def _to_cop(value) -> Optional[float]:
        """Normaliza un precio devuelto por Gemini a float COP. Acepta número o
        cadena tipo '$ 12.500' / '12,500' / '12500.0'."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else None
        raw = str(value)
        # Primer token que contenga dígitos (evita que '/m2', 'COP', etc. peguen
        # cifras espurias al número).
        tok = next((t for t in re.split(r"\s+", raw) if re.search(r"\d", t)), raw)
        s = re.sub(r"[^\d,.\-]", "", tok)
        if not s:
            return None
        has_dot, has_comma = "." in s, "," in s
        if has_dot and has_comma:
            # punto = miles, coma = decimal (convención CO)
            s = s.replace(".", "").replace(",", ".")
        elif has_comma:
            ent, _, dec = s.rpartition(",")
            s = (ent + "." + dec) if len(dec) in (1, 2) else (ent + dec)
        elif has_dot:
            if s.count(".") > 1:
                s = s.replace(".", "")  # varios puntos => miles (1.078.250)
            else:
                ent, _, dec = s.rpartition(".")
                # 3 dígitos tras el punto => miles (12.500 -> 12500);
                # 1-2 dígitos => decimal real (12500.0 -> 12500.0).
                s = (ent + dec) if len(dec) == 3 else (ent + "." + dec)
        try:
            v = float(s)
            return v if v > 0 else None
        except ValueError:
            return None

    def _grounding_url(self, resp) -> Optional[str]:
        """Extrae el primer enlace de las citas de grounding, si las hay."""
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

    def research_price(self, item: str, unit: str = "") -> Optional[PriceResearch]:
        if not self.enabled or not str(item).strip():
            return None
        key = f"{str(item).strip().lower()}|{str(unit).strip().lower()}"
        if key in self._cache:
            return self._cache[key]
        prompt = _PRICE_PROMPT.format(item=item, unit=unit or "")
        result: Optional[PriceResearch] = None
        try:
            # No se fuerza response_mime_type=json porque es incompatible con el
            # grounding de Google Search; se parsea el JSON del texto.
            resp = self._model.generate_content(
                prompt,
                generation_config=self._GenerationConfig(temperature=0.0, max_output_tokens=512),
            )
            data = GeminiResolver._parse_json(getattr(resp, "text", "") or "")
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

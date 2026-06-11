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

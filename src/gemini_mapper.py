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

    # Reintentos con backoff SOLO en errores transitorios (429 rate-limit, 503, 500):
    # así un pico de rate-limit no tumba la corrida. Si es cuota agotada de verdad,
    # el circuit breaker (con cooldown) de cada clase acota el tiempo perdido.
    try:
        http = gtypes.HttpOptions(
            timeout=20000,  # ms
            retry_options=gtypes.HttpRetryOptions(
                attempts=3, initial_delay=1.0, max_delay=8.0, exp_base=2.0,
                http_status_codes=[429, 500, 503],
            ),
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


def _best_grounding_url(resp) -> Optional[str]:
    """Elige el enlace del chunk cuyo segmento citado contiene un PRECIO ($ o un
    número de 3+ dígitos). Así el enlace corresponde a donde se ve el precio. Si no
    lo encuentra, cae al primer enlace (grounding_chunks/rendered_content)."""
    try:
        cand = resp.candidates[0]
        gm = getattr(cand, "grounding_metadata", None)
        chunks = list(getattr(gm, "grounding_chunks", None) or []) if gm else []
        supports = list(getattr(gm, "grounding_supports", None) or []) if gm else []
        price_re = re.compile(r"\$|\d{3,}")
        for sup in supports:
            seg = getattr(sup, "segment", None)
            seg_text = getattr(seg, "text", None) if seg else None
            if seg_text and price_re.search(seg_text):
                idxs = getattr(sup, "grounding_chunk_indices", None) or []
                for ci in idxs:
                    if 0 <= ci < len(chunks):
                        web = getattr(chunks[ci], "web", None)
                        uri = getattr(web, "uri", None) if web else None
                        if uri:
                            return uri
    except Exception:
        pass
    return _grounding_url(resp)


def _is_bad_link(u: str) -> bool:
    """URL inservible como referencia directa: no http, búsqueda de Google,
    captcha o página de consentimiento."""
    u = u or ""
    return (
        not u.startswith("http")
        or "google.com/search" in u
        or "/sorry/" in u
        or "consent.google" in u
    )


def _follow(url: str, timeout: float = 6.0) -> str:
    """Sigue la redirección HTTP y devuelve la URL final (sea cual sea) o ''."""
    try:
        import requests
        r = requests.get(url, allow_redirects=True, timeout=timeout, stream=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        final = r.url
        try:
            r.close()
        except Exception:
            pass
        return final or ""
    except Exception as exc:  # pragma: no cover
        print(f"[gemini-precio] no se pudo resolver el redirect: {exc}")
        return ""


def _pick_url(raw: str, model_url: str, resolve_links: bool) -> str:
    """Elige el mejor enlace al PRODUCTO:
    1) si el grounding dio un redirect de Vertex, lo resuelve al enlace DIRECTO;
    2) si ese redirect lleva a una búsqueda de Google/captcha (o no resuelve),
       usa la ficha del producto que devolvió el modelo (fuente_url);
    3) como último recurso, conserva el redirect."""
    raw = raw or ""
    model_url = (model_url or "").strip()
    is_redirect = "grounding-api-redirect" in raw
    if resolve_links and is_redirect:
        final = _follow(raw)
        if final and not _is_bad_link(final) and "grounding-api-redirect" not in final:
            return final  # enlace directo verificado
        if model_url and not _is_bad_link(model_url):
            return model_url  # ficha del producto dada por el modelo
        return raw  # último recurso
    if raw and not _is_bad_link(raw) and not is_redirect:
        return raw  # ya era directo
    if model_url and not _is_bad_link(model_url):
        return model_url
    return raw or model_url


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
    producto: str = ""  # nombre EXACTO del producto/ficha hallado en la búsqueda
    calculo: str = ""   # cómo se obtuvo el precio (escalado por unidad, si aplica)
    precio_base: Optional[float] = None  # precio de la unidad base (antes de escalar)
    unidad_base: str = ""  # unidad base usada para escalar (p.ej. 'm', 'kg', 'unidad')
    escalado: bool = False  # True si el precio se obtuvo escalando una unidad base


_PRICE_PROMPT = """Eres un experto en precios de insumos de construcción y
mantenimiento en COLOMBIA (precios en pesos colombianos, COP). Busca EN INTERNET
el precio UNITARIO de mercado MÁS REPRESENTATIVO y ACTUAL para el siguiente ítem.

ÍTEM: "{item}"
UNIDAD esperada: "{unit}"
{ref_linea}
Reglas IMPORTANTES:
- El precio debe estar EXPLÍCITO y PUBLICADO en la página fuente (un valor numérico
  visible en una página de VENTA o CATÁLOGO con precio listado). NO uses páginas que
  solo describen el producto, piden "cotizar", muestran un formulario de contacto o
  no muestran un precio claro.
- Prefiere comercios colombianos con precio listado: Homecenter/Sodimac, Easy,
  Constructor, Mercado Libre Colombia, Falabella, ferreterías en línea, catálogos de
  fabricante con precio. La URL debe llevar a la ficha del producto con su precio.
- Devuelve el precio en COP por la unidad indicada (o la unidad real del producto si
  difiere; en ese caso indícala en "unidad").
- ESCALADO POR UNIDAD (para aumentar coincidencias): si NO encuentras la presentación
  EXACTA del ítem (p.ej. "tubería de 6 m"), busca el precio de una presentación
  estándar o por unidad base (p.ej. "$X por metro", "tubo de 1 m", "por kg", "por
  litro", "por m2") y ESCÁLALO a la cantidad/dimensión del ítem:
  precio_final = precio_base × cantidad. Ejemplo: ítem "Tubería PVC 6 m", encuentras
  "Tubería PVC $12.000/m" → precio = 72.000, precio_base = 12.000, unidad_base = "m",
  calculo = "$12.000/m × 6 m = $72.000". Usa el escalado SOLO cuando no exista la
  presentación exacta, y solo si el escalado es razonable (lineal); no escales si el
  precio no es proporcional (p.ej. accesorios, equipos). Indica "escalado": true.
- Si el precio típico incluye IVA, da el valor ANTES de IVA si puedes estimarlo; si
  no, deja el precio tal cual y acláralo en "notas".
- Si hay varias fuentes creíbles con precio explícito{ref_pref}, elige la MÁS
  representativa; NO inventes ni fuerces un valor para que coincida.
- Si NO encuentras un precio creíble y EXPLÍCITO (ni directo ni escalable), responde
  precio = null.

Responde al final SOLO un JSON válido con esta forma (sin texto extra después):
{{"precio": <número COP final o null>, "unidad": "<unidad del ítem>",
  "precio_base": <número COP de la unidad base o null>, "unidad_base": "<unidad base>",
  "escalado": <true/false>, "calculo": "<cómo se calculó, p.ej. $12.000/m × 6 m>",
  "fuente_url": "<enlace a la ficha con el precio>", "fuente_nombre": "<comercio/fuente>",
  "producto": "<nombre EXACTO del producto/ficha que tiene ese precio>",
  "confidence": <0-100>, "notas": "<breve>"}}
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
        self._max_fails = 5
        self._paused = 0
        self._cooldown = 40

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

    def research_price(self, item: str, unit: str = "", referencia_bd=None,
                       resolve_links: bool = True) -> Optional[PriceResearch]:
        if not self.enabled or not str(item).strip():
            return None
        if self._paused > 0:
            self._paused -= 1  # en cooldown tras varios fallos: se salta esta llamada
            return None
        key = f"{str(item).strip().lower()}|{str(unit).strip().lower()}"
        if key in self._cache:
            return self._cache[key]
        # Línea de referencia del WH para que prefiera valores cercanos (sin forzar).
        ref_linea, ref_pref = "", ""
        try:
            if referencia_bd and float(referencia_bd) > 0:
                ref = float(referencia_bd)
                ref_linea = (f"PRECIO DE REFERENCIA INTERNO (base de datos): "
                             f"~${ref:,.0f} COP por '{unit or 'unidad'}'.\n")
                ref_pref = (", prioriza la que esté en un rango razonable del precio de "
                            "referencia interno (mismo orden de magnitud)")
        except Exception:
            pass
        prompt = _PRICE_PROMPT.format(item=item, unit=unit or "",
                                      ref_linea=ref_linea, ref_pref=ref_pref)
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
                    raw = _best_grounding_url(resp) or ""
                    model_url = str(data.get("fuente_url", "") or "")
                    url = _pick_url(raw, model_url, resolve_links)
                    nombre = _grounding_titles(resp) or str(data.get("fuente_nombre", "") or "Referencia web").strip()
                    result = PriceResearch(
                        precio=precio,
                        unidad=str(data.get("unidad", "") or unit or "").strip(),
                        fuente_url=url,
                        fuente_nombre=nombre,
                        confianza=float(data.get("confidence", 0) or 0),
                        notas=str(data.get("notas", "") or "")[:200],
                        producto=str(data.get("producto", "") or "")[:160],
                        calculo=str(data.get("calculo", "") or "").strip()[:200],
                        precio_base=self._to_cop(data.get("precio_base")),
                        unidad_base=str(data.get("unidad_base", "") or "").strip()[:30],
                        escalado=bool(data.get("escalado", False)),
                    )
        except Exception as exc:  # pragma: no cover
            self._fail_streak += 1
            print(f"[gemini-precio] error investigando '{str(item)[:40]}': {exc}")
            if self._fail_streak >= self._max_fails:
                self._paused = self._cooldown
                self._fail_streak = 0
                print(f"[gemini-precio] EN PAUSA {self._cooldown} llamadas tras varios "
                      f"fallos seguidos (rate-limit/cuota); reintentará luego.")
            result = None
        self._cache[key] = result
        return result

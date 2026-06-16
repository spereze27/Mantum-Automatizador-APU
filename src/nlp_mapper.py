"""Mapeo fuerte de ítems (NLP) entre warehouse y comparativos.

Estrategia recomendada (Científico de Datos):
  1) Normalización determinística por REGEX: unidades, símbolos, fracciones,
     acentos, espacios. Esto sube el match del ~60% al ~90%+ ANTES de aplicar
     distancia de edición.
  2) Matching difuso con thefuzz (Levenshtein/token_set_ratio).
  3) (Opcional) Segunda pasada con embeddings multilingües para casos donde el
     fuzzy se queda corto pero el significado es el mismo.

Cada ítem del warehouse se cruza 1 a 1 con el mejor candidato del catálogo de
comparativos, devolviendo un confidence score.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from thefuzz import fuzz, process

# ---------------------------------------------------------------------------
# 1. Diccionarios de estandarización
# ---------------------------------------------------------------------------

# Unidades -> token canónico. El orden importa: las claves más largas/ambiguas
# se resuelven primero. Se aplican como reemplazos sobre el texto ya en minúscula.
_UNIT_PATTERNS: list[tuple[str, str]] = [
    # pulgadas
    (r'\bpulgadas?\b', ' in '),
    (r'\bpulg\b', ' in '),
    (r'\bplg\b', ' in '),
    (r'(?<=\d)\s*["”“]', ' in '),   # 2" , 2 ”
    (r'(?<=\d)\s*\'\'', ' in '),    # 2''
    (r'\binch(?:es)?\b', ' in '),
    # milímetros / centímetros / metros
    (r'\bmilimetros?\b', ' mm '),
    (r'\bmilímetros?\b', ' mm '),
    (r'\bmts?\b', ' m '),
    (r'\bmetros?\b', ' m '),
    (r'\bcentimetros?\b', ' cm '),
    (r'\bcentímetros?\b', ' cm '),
    (r'\bml\b', ' m '),             # metro lineal -> m
    # volumen / masa
    (r'\bgalones?\b', ' gl '),
    (r'\bgal\b', ' gl '),
    (r'\bgl\b', ' gl '),
    (r'\bmililitros?\b', ' ml '),
    (r'\blitros?\b', ' l '),
    (r'\bkilogramos?\b', ' kg '),
    (r'\bkilos?\b', ' kg '),
    (r'\bkgm\b', ' kg '),
    (r'\bgramos?\b', ' g '),
    # unidades de conteo
    (r'\bunidad(?:es)?\b', ' und '),
    (r'\bund\b', ' und '),
    (r'\bun\b', ' und '),
    (r'\bu\b', ' und '),
    (r'\bpaquetes?\b', ' paq '),
    (r'\bpaq\b', ' paq '),
    (r'\bcuñetes?\b', ' cunete '),
]

# Sinónimos de producto frecuentes (mapea variantes comerciales a un lema).
_SYNONYMS: list[tuple[str, str]] = [
    (r'\bm\.?o\.?\b', ' mano de obra '),
    (r'\bmano obra\b', ' mano de obra '),
    (r'\bml\b', ' m '),
    (r'\bmangueras?\b', ' manguera '),
    (r'\bvarillas?\b', ' varilla '),
    (r'\btubos?\b', ' tubo '),
    (r'\bcodos?\b', ' codo '),
    (r'\bbrochas?\b', ' brocha '),
    (r'\bbrocas?\b', ' broca '),
    (r'\brodillos?\b', ' rodillo '),
    (r'\bpinturas?\b', ' pintura '),
]

# Fracciones unicode -> ascii.
_FRACTIONS = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(text: Optional[str]) -> str:
    """Normaliza un nombre de ítem a una forma canónica comparable."""
    if not text:
        return ""
    t = str(text).lower()
    # Reemplazo de fracciones unicode y no-break space.
    t = t.replace("\xa0", " ")
    for k, v in _FRACTIONS.items():
        t = t.replace(k, v)
    t = _strip_accents(t)
    # Limpieza de nóminas/facturas de mano de obra: quita 'NOMINA', el rango de
    # fechas ('desde el 1 al 31 de mayo') y meses, para que el ROL (oficial pintor,
    # ayudante, etc.) domine el match en vez del ruido de nombres/fechas.
    if "nomina" in t:
        t = re.sub(r'\bnomina\b', ' ', t)
        t = re.sub(r'\bdesde\b.*', ' ', t)  # corta todo desde 'desde el ... de mes'
    t = re.sub(r'\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|'
               r'septiembre|setiembre|octubre|noviembre|diciembre)\b', ' ', t)
    # Normaliza separadores típicos: '*', 'x', 'por' entre dimensiones.
    t = re.sub(r'(?<=\d)\s*[x×]\s*(?=\d)', ' x ', t)
    t = re.sub(r'\bpor\b', ' x ', t)
    t = t.replace("*", " x ")
    # Unidades y sinónimos.
    for pat, repl in _UNIT_PATTERNS:
        t = re.sub(pat, repl, t)
    for pat, repl in _SYNONYMS:
        t = re.sub(pat, repl, t)
    # Quita todo lo que no sea alfanumérico, espacio, punto, slash o guion.
    t = re.sub(r'[^a-z0-9 ./\-]', ' ', t)
    # Normaliza decimales con coma a punto.
    t = re.sub(r'(?<=\d),(?=\d)', '.', t)
    # Colapsa espacios.
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# ---------------------------------------------------------------------------
# 2. Estructuras de matching
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    matched: bool
    candidate_raw: Optional[str]
    candidate_norm: Optional[str]
    score: float                 # 0-100 (fuzzy) o 0-100 (embedding reescalado)
    method: str                  # 'fuzzy' | 'embedding' | 'none'
    unit_match: bool


class ItemMatcher:
    """Empareja descripciones del warehouse contra un catálogo de comparativos.

    El catálogo se entrega como lista de (raw, unit). Internamente se precalcula
    la forma normalizada y, opcionalmente, los embeddings.
    """

    def __init__(
        self,
        catalog: Iterable[tuple[str, str]],
        fuzzy_threshold: int = 82,
        use_embeddings: bool = False,
        embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
        embedding_threshold: float = 0.62,
    ) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.use_embeddings = use_embeddings
        self.embedding_threshold = embedding_threshold

        self._raw: list[str] = []
        self._norm: list[str] = []
        self._units: list[str] = []
        seen: set[str] = set()
        for raw, unit in catalog:
            n = normalize(raw)
            if not n or n in seen:
                continue
            seen.add(n)
            self._raw.append(raw)
            self._norm.append(n)
            self._units.append(normalize(unit))

        self._embed_model = None
        self._embeddings = None
        if self.use_embeddings and self._norm:
            self._init_embeddings(embedding_model)

    def _init_embeddings(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np  # noqa

            self._embed_model = SentenceTransformer(model_name)
            self._embeddings = self._embed_model.encode(
                self._norm, normalize_embeddings=True, show_progress_bar=False
            )
        except Exception as exc:  # pragma: no cover - dependencia opcional
            # Falla suave: si no hay modelo, se sigue solo con fuzzy.
            self.use_embeddings = False
            self._embed_model = None
            self._embeddings = None
            print(f"[nlp_mapper] embeddings deshabilitados: {exc}")

    @staticmethod
    def _number_mismatch(query_norm: str, cand_norm: str) -> bool:
        """True si ambos textos tienen números (códigos/medidas) y NO comparten
        ninguno. Ej.: 'viniltex 1520' vs 'viniltex 1501' -> son productos
        distintos aunque el texto se parezca. Blinda el match contra códigos."""
        import re as _r
        qn = set(_r.findall(r"\d+", query_norm))
        cn = set(_r.findall(r"\d+", cand_norm))
        if not qn or not cn:
            return False  # si alguno no tiene números, no aplica
        return qn.isdisjoint(cn)

    @staticmethod
    def _is_generic_candidate(query_norm: str, cand_norm: str) -> bool:
        """True si el candidato es un subconjunto GENÉRICO del insumo: sus tokens
        significativos (>=3 chars) están todos en el insumo, pero al insumo le
        sobran tokens distintivos. Ej.: insumo 'oficial carpintero' vs candidato
        'oficial' -> genérico (no debe cruzar). Evita el doble conteo de roles."""
        def toks(s):
            return {t for t in str(s).split() if len(t) >= 3}
        q, c = toks(query_norm), toks(cand_norm)
        if not q or not c:
            return False
        return c < q  # subconjunto propio: candidato más genérico que el insumo

    def match_many(self, description: str, unit: str = "", limit: int = 15,
                   min_score: float = None) -> list[dict]:
        """Devuelve TODAS las variantes del catálogo que cruzan con el insumo por
        encima del umbral (no solo la mejor). Sirve para juntar el mismo insumo
        descrito distinto en varias fuentes. `min_score` permite exigir un umbral
        más alto para la AGREGACIÓN (evita sobre-agrupar términos genéricos)."""
        norm = normalize(description)
        if not norm or not self._norm:
            return []
        thr = self.fuzzy_threshold if min_score is None else min_score

        def _blended(a: str, b: str) -> float:
            return 0.7 * fuzz.token_set_ratio(a, b) + 0.3 * fuzz.token_sort_ratio(a, b)

        scored = process.extract(norm, self._norm, scorer=_blended, limit=limit)
        out = []
        for cand_norm, score in scored:
            if score < thr:
                continue
            if self._is_generic_candidate(norm, cand_norm) or self._number_mismatch(norm, cand_norm):
                continue  # candidato genérico o con código/medida distinta
            idx = self._norm.index(cand_norm)
            out.append({
                "norm": cand_norm,
                "raw": self._raw[idx],
                "score": float(score),
                "unit_match": self._unit_match(unit, idx),
            })
        return out

    def match(self, description: str, unit: str = "") -> MatchResult:
        norm = normalize(description)
        if not norm or not self._norm:
            return MatchResult(False, None, None, 0.0, "none", False)

        # --- Pasada 1: fuzzy ---
        # Scorer combinado: token_set_ratio capta reordenamientos/extras, pero
        # puede dar 100 cuando un texto es subconjunto trivial del otro (ej. un
        # código "a 3"). Lo mezclamos con token_sort_ratio para penalizar eso.
        def _blended(a: str, b: str) -> float:
            return 0.7 * fuzz.token_set_ratio(a, b) + 0.3 * fuzz.token_sort_ratio(a, b)

        best = process.extract(norm, self._norm, scorer=_blended, limit=10)
        for cand_norm, score in best:
            score = float(score)
            if score < self.fuzzy_threshold:
                break  # vienen ordenados; nada más alcanza el umbral
            if self._is_generic_candidate(norm, cand_norm) or self._number_mismatch(norm, cand_norm):
                continue  # candidato genérico o con código/medida distinta: siguiente
            idx = self._norm.index(cand_norm)
            return MatchResult(
                matched=True,
                candidate_raw=self._raw[idx],
                candidate_norm=cand_norm,
                score=score,
                method="fuzzy",
                unit_match=self._unit_match(unit, idx),
            )

        # --- Pasada 2: embeddings (opcional) ---
        if self.use_embeddings and self._embeddings is not None:
            import numpy as np

            q = self._embed_model.encode([norm], normalize_embeddings=True)[0]
            sims = self._embeddings @ q  # coseno (vectores ya normalizados)
            idx = int(np.argmax(sims))
            sim = float(sims[idx])
            if sim >= self.embedding_threshold:
                return MatchResult(
                    matched=True,
                    candidate_raw=self._raw[idx],
                    candidate_norm=self._norm[idx],
                    score=round(sim * 100, 2),
                    method="embedding",
                    unit_match=self._unit_match(unit, idx),
                )

        # Sin match aceptable: devolvemos el mejor fuzzy como referencia (no matched).
        if best:
            cn, sc = best[0][0], float(best[0][1])
            idx = self._norm.index(cn)
            return MatchResult(False, self._raw[idx], cn, sc, "fuzzy", False)
        return MatchResult(False, None, None, 0.0, "none", False)

    def _unit_match(self, unit: str, idx: int) -> bool:
        u = normalize(unit)
        if not u:
            return False
        return u == self._units[idx] or u in self._units[idx] or self._units[idx] in u

    def top_candidates(self, description: str, k: int = 8) -> list[dict]:
        """Devuelve los k mejores candidatos fuzzy (para pasarle a Gemini)."""
        norm = normalize(description)
        if not norm or not self._norm:
            return []
        scored = process.extract(norm, self._norm, scorer=fuzz.token_set_ratio, limit=k)
        out = []
        for cand_norm, score in scored:
            idx = self._norm.index(cand_norm)
            out.append(
                {
                    "index": idx,
                    "raw": self._raw[idx],
                    "norm": cand_norm,
                    "unit": self._units[idx],
                    "fuzzy_score": float(score),
                }
            )
        return out

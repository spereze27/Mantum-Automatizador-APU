"""Pruebas del mapeo NLP. Ejecutar: pytest -q"""
from src.nlp_mapper import normalize, ItemMatcher


def test_normalize_units():
    assert normalize('broca 2 pulg') == normalize('broca 2"')
    assert normalize('broca 2 in') == normalize('broca 2 pulgadas')
    assert "in" in normalize('tubo de 1/2"')


def test_normalize_accents_and_symbols():
    assert normalize("Codo de presión 90° de 2 in") == normalize("codo de presion 90 de 2 in")


def test_matcher_basic():
    catalog = [
        ("BROCHA DE 2 IN", "und"),
        ("CODO DE PRESION DE 90 DE 2 in", "und"),
        ("PINTURA VINILTEX BLANCO", "gl"),
    ]
    m = ItemMatcher(catalog, fuzzy_threshold=80)
    r = m.match('Brocha 2"', "Und")
    assert r.matched
    assert "BROCHA" in r.candidate_raw


def test_matcher_no_match():
    m = ItemMatcher([("CEMENTO GRIS 50 kg", "und")], fuzzy_threshold=90)
    r = m.match("tornillo autoperforante", "und")
    assert not r.matched


def test_mano_de_obra_prefijo_cruza_profesion():
    """'M.O. <profesión>' (mano de obra) debe cruzar con la profesión base.
    Aplica a todas las profesiones (oficial, ayudante, pintor, ...)."""
    catalog = [
        ("M.O. Oficial", ""),
        ("M.O. Oficial Pintor", ""),
        ("M.O. Ayudante", ""),
        ("M.O. Pintor", ""),
    ]
    m = ItemMatcher(catalog, fuzzy_threshold=77)
    assert m.match("Oficial", "").matched
    assert m.match("Oficial pintor", "").matched
    assert m.match("Ayudante", "").matched
    assert m.match("Pintor", "").matched
    # La etiqueta de categoría no debe convertir 'mano' en sustantivo cabeza.
    assert ItemMatcher._head_mismatch("oficial pintor", "mano de obra oficial pintor") is False
    # 'M.O. oficial' vs 'oficial' es la MISMA profesión (no genérico).
    assert ItemMatcher._is_generic_candidate("mano de obra oficial", "oficial") is False
    # Pero 'oficial carpintero' vs 'oficial' SÍ sigue siendo genérico.
    assert ItemMatcher._is_generic_candidate("oficial carpintero", "oficial") is True

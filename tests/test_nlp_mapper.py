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

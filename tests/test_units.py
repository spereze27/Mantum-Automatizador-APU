"""Tests de units.py con los casos REALES que fallaron en el reporte 20260710."""
import pytest

from src.units import (
    canon_unit,
    es_dimensional,
    es_plausible,
    factor_presentacion,
    peso_aparicion,
    rango_plausible,
    referencia_robusta,
)

PLAUS = "config/plausibilidad.yaml"


# --- BUG 2: una sola canonización, con sinónimos ---------------------------
@pytest.mark.parametrize("crudo,esperado", [
    ("Gal", "gl"), ("Galón", "gl"), ("galones", "gl"), ("GL", "gl"),
    ("Und", "und"), ("UNIDAD", "und"), ("un", "und"),
    ("Kg", "kg"), ("KILOS", "kg"),
    ("M3", "m3"), ("mts2", "m2"), ("Día", "dia"), ("HH", "hr"),
])
def test_canon_unit_sinonimos(crudo, esperado):
    assert canon_unit(crudo) == esperado


def test_galon_matchea_con_gl():
    # El fallo exacto del reporte: "Galón" no matcheaba con "Gl" -> T0/T1 vacíos.
    assert canon_unit("Galón") == canon_unit("Gl") == "gl"


# --- CAMBIO 3: unidades no dimensionales ----------------------------------
@pytest.mark.parametrize("und", ["%", "Glb", "GLOBAL", "", "  "])
def test_unidades_no_dimensionales(und):
    assert es_dimensional(und) is False


@pytest.mark.parametrize("und", ["Gal", "Kg", "m3", "Und", "hr"])
def test_unidades_dimensionales(und):
    assert es_dimensional(und) is True


# --- BUG 3: factor de presentación ----------------------------------------
def test_cuarto_de_galon():
    # Viniltex: 1/4 gal a $37.500 -> $150.000/gl
    f, _ = factor_presentacion("Pintura Viniltex Blanco 1/4 Galón", "und", "Gal")
    assert f == pytest.approx(0.25)
    assert round(37_500 / f) == 150_000


def test_cunete_son_cinco_galones():
    f, _ = factor_presentacion("Viniltex blanco cuñete", "und", "Gal")
    assert f == pytest.approx(5.0)
    assert round(426_900 / f) == 85_380


def test_dos_galones():
    f, _ = factor_presentacion("Esmalte x 2 galones", "und", "Gal")
    assert f == pytest.approx(2.0)
    assert round(180_000 / f) == 90_000


def test_cemento_50kg_a_precio_por_kilo():
    # El error de 30x: $35.500 era el precio del BULTO, no del kilo.
    f, _ = factor_presentacion("CEMENTO GRIS ARGOS X 50 KG", "und", "Kg")
    assert f == pytest.approx(50.0)
    assert round(35_500 / f) == 710


def test_pegacor_25kg():
    f, _ = factor_presentacion("Pegacor blanco (25 Kg)", "BULTO", "Kg")
    assert f == pytest.approx(25.0)


def test_unidad_bulto_declarada():
    f, _ = factor_presentacion("Cemento gris", "BULTO", "Kg")
    assert f == pytest.approx(50.0)


def test_sin_presentacion_factor_uno():
    f, expl = factor_presentacion("Viniltex blanco", "Gal", "Gal")
    assert f == 1.0 and expl == ""


def test_no_convierte_entre_familias_distintas():
    # "2 pulg" es un diámetro, no una presentación de un precio por metro.
    f, _ = factor_presentacion("Tubo presión 2 pulg", "m", "Kg")
    assert f == 1.0


# --- Guardarraíl absoluto --------------------------------------------------
def test_plausibilidad_rechaza_cemento_por_bulto():
    # $35.500/Kg de cemento: fuera de rango. Esto es lo que Gemini "verificó".
    assert es_plausible(35_500, "Cemento 50 Kg", "Kg", PLAUS) is False
    assert es_plausible(1_166, "Cemento 50 Kg", "Kg", PLAUS) is True


def test_plausibilidad_rechaza_tornillo_a_5000():
    assert es_plausible(5_042, "Tornillo estructural", "Und", PLAUS) is False
    assert es_plausible(171, "Tornillo estructural", "Und", PLAUS) is True


def test_plausibilidad_mano_de_obra():
    assert es_plausible(23_940, "Ayudante", "hr", PLAUS) is True
    assert es_plausible(230_000, "Ayudante", "hr", PLAUS) is False


def test_sin_regla_no_bloquea():
    assert rango_plausible("Bebedero 500L", "und", PLAUS) is None
    assert es_plausible(999_999, "Bebedero 500L", "und", PLAUS) is True


# --- Estimador robusto -----------------------------------------------------
def test_max_es_sensible_al_tamano_de_muestra_y_p75_no():
    """El corazón del problema: el máximo CRECE con n; el cuantil converge."""
    import random
    random.seed(3)
    base = [10_000 * random.lognormvariate(0, 0.25) for _ in range(2000)]
    pocos, muchos = base[:20], base
    max_pocos, _ = referencia_robusta(pocos, None, "max")
    max_muchos, _ = referencia_robusta(muchos, None, "max")
    p75_pocos, _ = referencia_robusta(pocos, None, "p75")
    p75_muchos, _ = referencia_robusta(muchos, None, "p75")
    assert max_muchos > max_pocos * 1.2          # el máximo se dispara con n
    assert abs(p75_muchos / p75_pocos - 1) < 0.15  # el p75 es estable


def test_winsorizacion_topa_la_factura_de_urgencia():
    precios = [100.0] * 20 + [1_000_000.0]
    ref_max, _ = referencia_robusta(precios, None, "max")
    ref_p75, _ = referencia_robusta(precios, None, "p75", winsor_pct=5)
    assert ref_max == 1_000_000.0
    assert ref_p75 == 100.0


def test_peso_por_recencia_favorece_lo_reciente():
    import datetime as dt
    hoy = dt.date.today()
    reciente = (hoy - dt.timedelta(days=10)).isoformat()
    viejo = (hoy - dt.timedelta(days=1095)).isoformat()
    w_r = peso_aparicion(reciente, None, 365.0)
    w_v = peso_aparicion(viejo, None, 365.0)
    assert w_r > w_v * 5
    # Con dos precios opuestos, la referencia se inclina hacia el reciente.
    ref, _ = referencia_robusta([5_000, 10_000], [w_v, w_r], "mediana", winsor_pct=0)
    assert ref == 10_000


def test_sin_precios_devuelve_none():
    assert referencia_robusta([], None, "p75") == (None, 0)
    assert referencia_robusta([0, -5], None, "p75") == (None, 0)

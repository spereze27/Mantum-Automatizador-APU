"""Tests de la puerta de decisión: banda infranqueable + arbitraje vinculante.

Reproducen los 2 bugs gemelos del reporte 20260710 sin tocar GCS/Sheets: se
replica exactamente el predicado `aplicar` de pipeline.py.
"""
import pytest

from src.units import es_plausible

PLAUS = "config/plausibilidad.yaml"


class S:  # settings mínimos
    max_price_ratio = 3.0
    enforce_band = True
    auto_apply_within_band = True
    unit_plausibility = True
    plausibilidad_config_path = PLAUS


def aplicar(precio_ref, valor_wh, desc, und, sospechoso, arbitraje_rechazo, s=S):
    """Réplica literal del predicado de pipeline.run_pipeline."""
    dentro_banda = False
    if precio_ref is not None and valor_wh and valor_wh > 0:
        r = precio_ref / valor_wh
        dentro_banda = (1.0 / s.max_price_ratio) <= r <= s.max_price_ratio
    ref_plausible = True
    if precio_ref is not None and s.unit_plausibility:
        ref_plausible = es_plausible(precio_ref, desc, und, s.plausibilidad_config_path)
    return (
        precio_ref is not None
        and not arbitraje_rechazo
        and ref_plausible
        and (dentro_banda or not s.enforce_band)
        and (not sospechoso or s.auto_apply_within_band)
    )


# --- BUG 1: el veredicto del arbitraje es VINCULANTE ------------------------
def test_bug1_viniltex_no_se_aplica_si_gemini_manda_mantener_wh():
    """Caso real: BD $90.423, referencia $210.000 (dentro de banda [30.141, 271.269]).
    Gemini dijo 'se mantiene el warehouse' y el código lo ignoró: aplicó $210.000."""
    assert aplicar(210_000, 90_422.92, "Viniltex", "Gal",
                   sospechoso=True, arbitraje_rechazo=True) is False


def test_bug1_sin_rechazo_del_arbitraje_si_se_aplica():
    assert aplicar(210_000, 90_422.92, "Viniltex", "Gal",
                   sospechoso=True, arbitraje_rechazo=False) is True


# --- BUG 1 (rama gemela): la banda es INFRANQUEABLE -------------------------
def test_cemento_30x_no_entra_ni_con_respaldo_de_gemini():
    """El arbitraje ponía sospechoso=False y saltaba la banda: entró a 30x."""
    assert aplicar(35_500, 1_166.09, "Cemento 50 Kg", "Kg",
                   sospechoso=False, arbitraje_rechazo=False) is False


def test_tornillo_29x_bloqueado():
    assert aplicar(5_042.02, 171.32, "Tornillo estructural", "Und",
                   sospechoso=False, arbitraje_rechazo=False) is False


def test_pegacor_17x_bloqueado():
    assert aplicar(48_000, 2_794.40, "Pegacor blanco (25 Kg)", "Kg",
                   sospechoso=False, arbitraje_rechazo=False) is False


def test_cemento_normalizado_si_se_aplica():
    """Tras normalizar (35.500 / 50 kg = 710/kg) el precio SÍ es aplicable."""
    assert aplicar(710, 1_166.09, "Cemento 50 Kg", "Kg",
                   sospechoso=False, arbitraje_rechazo=False) is True


# --- Casos normales que deben seguir pasando -------------------------------
def test_actualizacion_normal_dentro_de_banda():
    assert aplicar(19_253, 12_954.52, "Oficial", "hr",
                   sospechoso=True, arbitraje_rechazo=False) is True


def test_sin_referencia_no_aplica():
    assert aplicar(None, 10_000, "X", "und", False, False) is False


def test_bajada_extrema_tambien_bloqueada():
    """'Valvula de rodilla': BD $1.123.942 -> $238.655 (0,21x) también sale de banda."""
    assert aplicar(238_655, 1_123_942.40, "Valvula de rodilla", "und",
                   sospechoso=True, arbitraje_rechazo=False) is False

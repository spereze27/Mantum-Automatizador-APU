"""Tests offline para GeminiPriceResearcher (sin Vertex AI real).

Cubre la normalización de precios a COP (_to_cop) y el parseo de la respuesta
JSON. La llamada a Vertex se simula; aquí NO se hace red."""
import sys
import types

# Stub de vertexai para poder importar el módulo sin la dependencia instalada.
for _m in ("vertexai", "vertexai.generative_models"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from src.gemini_mapper import GeminiPriceResearcher, GeminiResolver  # noqa: E402


def test_to_cop_formatos_colombianos():
    f = GeminiPriceResearcher._to_cop
    assert f("$ 12.500") == 12500
    assert f("$1.078.250") == 1078250
    assert f("12500.0") == 12500
    assert f("3.450") == 3450
    assert f("COP 5.116 por metro") == 5116
    assert f("$ 27.177 /m2") == 27177
    assert abs(f("9,07") - 9.07) < 1e-6
    assert f(4232) == 4232


def test_to_cop_invalidos():
    f = GeminiPriceResearcher._to_cop
    assert f(None) is None
    assert f("abc") is None
    assert f("0") is None
    assert f(-5) is None


def test_parse_json_con_fences():
    data = GeminiResolver._parse_json('```json\n{"precio": 1000, "unidad": "und"}\n```')
    assert data is not None and data["precio"] == 1000 and data["unidad"] == "und"


def test_parse_json_con_texto_alrededor():
    data = GeminiResolver._parse_json('Claro:\n{"precio": 2500, "confidence": 80} listo')
    assert data is not None and data["precio"] == 2500

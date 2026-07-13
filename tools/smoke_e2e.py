#!/usr/bin/env python3
"""Smoke test end-to-end de run_pipeline SIN red.

Stubea Sheets/GCS y reconstruye los casos exactos que fallaron en el reporte
20260710, para verificar que las correcciones hacen lo que dicen.
Uso: python3 tools/smoke_e2e.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for mod in ("gspread", "google", "google.cloud", "google.oauth2",
            "google.oauth2.service_account", "google.cloud.storage"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["google.cloud"].storage = sys.modules["google.cloud.storage"]

os.environ.update({
    "GCP_PROJECT_ID": "x", "GCS_BUCKET_NAME": "b", "WAREHOUSE_SHEET_URL": "u",
    "DRY_RUN": "true", "USE_GEMINI_PRICE_RESEARCH": "false",
    "MAX_PRICE_RATIO": "3", "SUSPICIOUS_PCT_THRESHOLD": "50",
})

import pandas as pd  # noqa: E402

from src import pipeline  # noqa: E402
from src.config import Settings  # noqa: E402

WH = pd.DataFrame([
    ("1", "Viniltex",              "Gal", 90422.92, "Material",     "Activa"),
    ("2", "Cemento 50 Kg",         "Kg",   1166.09, "Material",     "Activa"),
    ("3", "Tornillo estructural",  "Und",   171.32, "Material",     "Activa"),
    ("4", "Transporte Materiales", "%",       0.00, "Material",     "Activa"),
    ("5", "Ayudante",              "hr",  16280.58, "Mano de obra", "Activa"),
], columns=["Código", "Nombre", "Und", "Precio de Lista", "Grupo", "Clasificación"])
WH["_sheet_row"] = range(4, 4 + len(WH))


def _f(desc, und, precio, fecha="2025-09-01", cant=1):
    return {"region": "Medellín", "proveedor": "Prov", "descripcion": desc,
            "unidad": und, "precio": precio, "archivo": "consolidado.xlsx",
            "formato": "consolidado_gasto_real",
            "fuente_tipo": "Gasto real (Consolidado)", "gcs_path": "c/x.xlsx",
            "columna_precio": "", "cantidad": cant, "fecha": fecha}


CONS = pd.DataFrame(
    # Viniltex: mezcla de presentaciones (el caso del usuario)
    [_f("VINILTEX BLANCO 1/4 GALON", "UND", 37_500) for _ in range(6)]
    + [_f("VINILTEX BLANCO GALON", "GL", 92_000) for _ in range(12)]
    + [_f("VINILTEX BLANCO GALON", "GL", 105_000) for _ in range(5)]
    + [_f("VINILTEX BLANCO CUÑETE", "UND", 426_900) for _ in range(2)]
    + [_f("VINILTEX BLANCO GALON", "GL", 210_000)]          # factura de urgencia
    # Cemento: el consolidado factura BULTOS, la BD está por Kg
    + [_f("CEMENTO GRIS ARGOS X 50 KG", "UND", 35_500, cant=10) for _ in range(20)]
    + [_f("CEMENTO GRIS ARGOS X 50 KG", "UND", 33_000, cant=10) for _ in range(14)]
    # Tornillo: una compra por caja colada como unidad
    + [_f("TORNILLO ESTRUCTURAL", "UND", 180) for _ in range(8)]
    + [_f("TORNILLO ESTRUCTURAL", "UND", 5_042.02) for _ in range(2)]
    # Transporte % (no debe procesarse)
    + [_f("TRANSPORTE MATERIALES", "UND", 130_000) for _ in range(5)]
    # Ayudante: 2.000 facturas, con cola de horas extra
    + [_f("AYUDANTE", "HR", 15_500 + (i % 40) * 90, cant=8) for i in range(2000)]
    + [_f("AYUDANTE", "HR", 23_940, cant=8) for _ in range(6)]
)

class FakeSheet:
    def __init__(self, *a, **k):
        pass

    def read(self):
        return WH.copy()

    def batch_update_by_letter(self, *a, **k):
        return 0


pipeline.WarehouseSheet = FakeSheet
pipeline.load_from_gcs = lambda *a, **k: pd.DataFrame()
cl = types.ModuleType("src.consolidado_loader")
cl.load_consolidado_from_gcs = lambda *a, **k: CONS.copy()
sys.modules["src.consolidado_loader"] = cl

# Captura del detalle del cruce (lo que iría a la hoja 'Mapping y Refutacion').
CAPTURA = {}
_orig = pipeline.analytics.build_mapping_report


def _capturar(matches):
    CAPTURA["m"] = matches.copy()
    return _orig(matches)


pipeline.analytics.build_mapping_report = _capturar

res = pipeline.run_pipeline(Settings())
m = CAPTURA["m"]

COLS = ["descripcion_wh", "und_wh", "valor_wh", "precio_referencia", "nuevo_valor",
        "actualizado", "dentro_de_banda", "arbitraje_rechazo", "referencia_plausible",
        "apariciones_normalizadas", "escala_bd_detectada", "motivo_no_procesable"]
pd.set_option("display.width", 250)
print(m[COLS].to_string(index=False))
print()
for _, r in m.iterrows():
    print(f"- {r['descripcion_wh']}: {str(r['de_donde_salio_el_precio'])[:230]}")

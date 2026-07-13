#!/usr/bin/env python3
"""Backtest temporal del estimador de referencia.

Por qué existe: hasta ahora no había forma de decir "este cambio mejoró". Cada
iteración era una opinión. Esto convierte la discusión en una métrica.

Idea: partir el Consolidado (gasto real) en una fecha de corte. Con las facturas
ANTERIORES al corte se calcula la referencia que el pipeline habría publicado; se
compara contra lo que la empresa REALMENTE pagó DESPUÉS del corte (la mediana del
período de validación, que es el mejor proxy del precio "verdadero").

Métricas:
  MAPE   : error absoluto porcentual medio (menor = mejor)
  wMAPE  : el mismo error PONDERADO por el gasto real -> es el que importa al negocio
  SESGO  : error medio con signo. POSITIVO = la referencia SOBRECOSTEA.
           El estimador 'max' tiene sesgo estructural positivo; ese es el punto.

Uso:
    python3 tools/backtest.py --consolidado ruta/al/consolidado.xlsx \
        --corte 2025-07-01 --estimadores max,p90,p75,mediana

Sin --consolidado, corre una demo sintética que muestra el sesgo del máximo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.units import peso_aparicion, referencia_robusta  # noqa: E402


def _mape(errores):
    return round(100 * statistics.fmean([abs(e) for e in errores]), 2) if errores else float("nan")


def _sesgo(errores):
    return round(100 * statistics.fmean(errores), 2) if errores else float("nan")


def evaluar(grupos, estimador, quantile=0.75, winsor=5.0, halflife=365.0):
    """grupos: lista de dicts {item, train:[{precio,fecha,cantidad}], real, gasto}."""
    errs, w_num, w_den = [], 0.0, 0.0
    for g in grupos:
        precios = [t["precio"] for t in g["train"]]
        pesos = [peso_aparicion(t.get("fecha"), t.get("cantidad"), halflife, False)
                 for t in g["train"]]
        ref, _ = referencia_robusta(precios, pesos, estimador, quantile, winsor)
        if not ref or not g["real"]:
            continue
        e = (ref - g["real"]) / g["real"]   # + = la referencia sobrecostea
        errs.append(e)
        w_num += abs(e) * g["gasto"]
        w_den += g["gasto"]
    return {
        "estimador": estimador,
        "n_items": len(errs),
        "MAPE_%": _mape(errs),
        "wMAPE_%": round(100 * w_num / w_den, 2) if w_den else float("nan"),
        "SESGO_%": _sesgo(errs),
    }


def construir_grupos(df, corte: dt.date, min_train=3, min_test=2):
    import pandas as pd
    df = df.copy()
    df["_f"] = pd.to_datetime(df["fecha"], errors="coerce")
    df = df.dropna(subset=["_f", "precio", "descripcion"])
    grupos = []
    for item, sub in df.groupby(df["descripcion"].astype(str).str.strip().str.lower()):
        tr = sub[sub["_f"].dt.date < corte]
        te = sub[sub["_f"].dt.date >= corte]
        if len(tr) < min_train or len(te) < min_test:
            continue
        real = float(te["precio"].median())
        cant = pd.to_numeric(te.get("cantidad"), errors="coerce").fillna(1).sum()
        grupos.append({
            "item": item,
            "train": [
                {"precio": float(r["precio"]), "fecha": str(r["_f"].date()),
                 "cantidad": r.get("cantidad")}
                for _, r in tr.iterrows()
            ],
            "real": real,
            "gasto": float(real * max(cant, 1)),
        })
    return grupos


def demo():
    """Datos sintéticos: precio real 10.000 con dispersión log-normal + una factura
    atípica (compra de urgencia). Muestra por qué 'max' no sirve como estimador."""
    import random
    random.seed(7)
    grupos = []
    for i in range(120):
        real = 10_000 * random.uniform(0.8, 1.2)
        n = random.choice([4, 8, 20, 60, 300])   # tamaños de muestra distintos
        train = [{"precio": real * random.lognormvariate(0, 0.25),
                  "fecha": "2025-03-01", "cantidad": 1} for _ in range(n)]
        if random.random() < 0.3:                 # 30% con una compra de urgencia
            train.append({"precio": real * random.uniform(2.5, 4.0),
                          "fecha": "2025-03-01", "cantidad": 1})
        grupos.append({"item": f"item_{i}", "train": train, "real": real,
                       "gasto": real * random.randint(1, 500)})
    return grupos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--consolidado", help="ruta al Consolidado .xlsx")
    ap.add_argument("--corte", default="", help="fecha de corte YYYY-MM-DD")
    ap.add_argument("--estimadores", default="max,p90,p75,mediana")
    args = ap.parse_args()

    if args.consolidado:
        from src.consolidado_loader import load_consolidado_from_file
        df = load_consolidado_from_file(args.consolidado)
        corte = dt.date.fromisoformat(args.corte) if args.corte else (
            dt.date.today() - dt.timedelta(days=180)
        )
        grupos = construir_grupos(df, corte)
        print(f"Corte: {corte} | ítems con train>=3 y test>=2: {len(grupos)}")
    else:
        grupos = demo()
        print(f"DEMO sintética (sin --consolidado): {len(grupos)} ítems")

    filas = [evaluar(grupos, e.strip()) for e in args.estimadores.split(",") if e.strip()]
    anchos = ["estimador", "n_items", "MAPE_%", "wMAPE_%", "SESGO_%"]
    print("\n" + " | ".join(f"{c:>10}" for c in anchos))
    print("-" * 60)
    for f in filas:
        print(" | ".join(f"{str(f[c]):>10}" for c in anchos))
    print("\nSESGO_% positivo = la referencia SOBRECOSTEA frente a lo realmente pagado.")
    print("Criterio de aceptación sugerido: |SESGO| < 10% y wMAPE mínimo.")


if __name__ == "__main__":
    main()

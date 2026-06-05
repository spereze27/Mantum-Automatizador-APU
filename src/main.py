"""Entrypoint del servicio para Cloud Run (FastAPI).

Endpoints:
  GET  /            -> info del servicio
  GET  /health      -> healthcheck (para Cloud Run / load balancer)
  POST /run         -> ejecuta el pipeline completo (idóneo para Cloud Scheduler)

Cloud Run inyecta el puerto en la variable PORT. El servidor escucha en 0.0.0.0.
La ejecución del pipeline puede tardar; se recomienda configurar el timeout del
servicio Cloud Run acorde (ver Terraform).
"""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .config import get_settings
from .pipeline import run_pipeline

app = FastAPI(title="APU Comparativo Mantenimiento", version="1.0.0")


@app.get("/")
def root():
    return {"service": "apu-comparativo-mtto", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/run")
def run():
    settings = get_settings()
    try:
        result = run_pipeline(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    status_code = 200 if not result.errors else 207  # 207: éxito parcial.
    return JSONResponse(status_code=status_code, content=asdict(result))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port)

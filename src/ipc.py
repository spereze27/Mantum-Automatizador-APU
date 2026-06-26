"""Aplicación del factor de ajuste por IPC.

El factor se calcula como (1 + variacion_ipc). Se aplica para llevar valores de
un año base al año objetivo de comparación.
"""
from __future__ import annotations

from typing import Optional


def ipc_factor(ipc_variation: float) -> float:
    """Devuelve el multiplicador. Ej: variacion 0.0528->1.0528."""
    return 1.0 + float(ipc_variation)


def apply_ipc(value: Optional[float], ipc_variation: float, enabled: bool = True) -> Optional[float]:
    """Ajusta un valor por IPC. Si está deshabilitado o el valor es None, lo
    devuelve sin cambios."""
    if value is None:
        return None
    if not enabled:
        return float(value)
    return round(float(value) * ipc_factor(ipc_variation), 2)

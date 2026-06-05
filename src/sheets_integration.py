"""Integración con Google Sheets (warehouse) usando gspread.

Autenticación:
  - En Cloud Run: usa las credenciales por defecto del entorno (ADC) que
    corresponden a la Service Account del servicio. No requiere archivo de clave.
  - En local/CI: si se define GCP_SA_KEY (JSON completo) o GOOGLE_APPLICATION_CREDENTIALS
    (ruta a archivo), se usan esas credenciales.

IMPORTANTE: la Service Account debe tener acceso de "Editor" compartido sobre el
Google Sheet del warehouse (ver README).
"""
from __future__ import annotations

import json
from typing import Optional

import gspread
import pandas as pd

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _credentials(sa_key_json: str = "", sa_key_path: str = ""):
    from google.oauth2.service_account import Credentials
    import google.auth

    if sa_key_json:
        info = json.loads(sa_key_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    if sa_key_path:
        return Credentials.from_service_account_file(sa_key_path, scopes=SCOPES)
    # ADC (Cloud Run runtime SA).
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


class WarehouseSheet:
    def __init__(
        self,
        sheet_url: str,
        tab: str,
        header_row: int = 2,
        sa_key_json: str = "",
        sa_key_path: str = "",
    ) -> None:
        self.tab = tab
        self.header_row = header_row
        self._gc = gspread.authorize(_credentials(sa_key_json, sa_key_path))
        self._sh = self._gc.open_by_url(sheet_url)
        self._ws = self._sh.worksheet(tab)
        self._header: list[str] = []

    def read(self) -> pd.DataFrame:
        """Lee la hoja respetando que el encabezado está en `header_row`."""
        values = self._ws.get_all_values()
        if len(values) < self.header_row:
            return pd.DataFrame()
        self._header = [str(h).strip() for h in values[self.header_row - 1]]
        data = values[self.header_row :]
        df = pd.DataFrame(data, columns=self._header)
        # Guarda el número de fila real en el Sheet para escrituras puntuales.
        df["_sheet_row"] = range(self.header_row + 1, self.header_row + 1 + len(df))
        return df

    def column_index(self, column_name: str) -> Optional[int]:
        """Índice 1-based de la columna en el Sheet (para gspread)."""
        for i, h in enumerate(self._header):
            if h.strip().lower() == column_name.strip().lower():
                return i + 1
        return None

    def batch_update_column(self, column_name: str, updates: dict[int, float]) -> int:
        """Actualiza una columna en filas específicas.

        updates: {numero_fila_sheet: nuevo_valor}
        Devuelve cantidad de celdas actualizadas. Usa batch_update para eficiencia.
        """
        col_idx = self.column_index(column_name)
        if col_idx is None:
            raise ValueError(f"Columna '{column_name}' no encontrada en el encabezado.")
        cells = []
        for row, value in updates.items():
            if value is None:
                continue
            cell = gspread.Cell(row=row, col=col_idx, value=value)
            cells.append(cell)
        if cells:
            self._ws.update_cells(cells, value_input_option="USER_ENTERED")
        return len(cells)

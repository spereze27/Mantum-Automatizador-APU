# Imagen base slim para Cloud Run.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Dependencias del sistema necesarias para pdfplumber (pdfminer) y Levenshtein.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

# Cloud Run define PORT; uvicorn debe escuchar en 0.0.0.0:$PORT.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 75

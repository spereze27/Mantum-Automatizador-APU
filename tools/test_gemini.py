#!/usr/bin/env python3
"""Prueba rápida de la API key de Gemini + Google Search grounding.

Uso (NO pegues la key en el código; pásala por variable de entorno):

    pip install google-genai
    export GEMINI_API_KEY="TU_KEY_NUEVA"
    python3 tools/test_gemini.py

Valida 3 cosas:
  1) Que la key y el modelo (gemini-2.5-flash) responden (sin grounding).
  2) Que el grounding de Google Search funciona y devuelve enlaces.
  3) Que GeminiPriceResearcher (el del servicio) regresa precio + unidad + URL.
"""
import os
import sys

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not API_KEY:
    print("ERROR: define GEMINI_API_KEY en el entorno (no en el código).")
    sys.exit(1)

print(f"Modelo: {MODEL}")

# --- 1) Llamada básica ---
try:
    from google import genai
    from google.genai import types
except Exception as exc:
    print(f"ERROR importando google-genai: {exc}\n  -> pip install google-genai")
    sys.exit(1)

client = genai.Client(api_key=API_KEY)
try:
    r = client.models.generate_content(
        model=MODEL,
        contents="Responde solo con la palabra: ok",
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10),
    )
    print(f"[1] Básico OK -> {r.text!r}")
except Exception as exc:
    print(f"[1] FALLO llamada básica: {exc}")
    print("    Si dice 404/model not found: el modelo no está disponible para tu key.")
    print("    Prueba GEMINI_MODEL=gemini-3.5-flash o gemini-3.1-flash-lite.")
    sys.exit(1)

# --- 2) Grounding con Google Search ---
try:
    r = client.models.generate_content(
        model=MODEL,
        contents="¿Cuál es el precio aproximado en COP de un bulto de cemento gris en Colombia? Cita la fuente.",
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=400,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    print(f"[2] Grounding OK -> {(r.text or '')[:160]!r}")
    try:
        gm = r.candidates[0].grounding_metadata
        chunks = getattr(gm, "grounding_chunks", None) or []
        urls = [getattr(getattr(c, "web", None), "uri", None) for c in chunks]
        urls = [u for u in urls if u]
        print(f"    Enlaces de grounding: {urls[:3] if urls else 'NINGUNO (¿grounding no habilitado para la key?)'}")
    except Exception as e2:
        print(f"    No se pudo leer grounding_metadata: {e2}")
except Exception as exc:
    print(f"[2] FALLO grounding: {exc}")
    print("    El grounding de Google Search puede requerir facturación habilitada en el proyecto de la key.")

# --- 3) La clase real del servicio ---
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from src.gemini_mapper import GeminiPriceResearcher
    pr = GeminiPriceResearcher(project="", api_key=API_KEY, model_name=MODEL)
    print(f"[3] GeminiPriceResearcher.enabled = {pr.enabled} (backend={pr._backend})")
    res = pr.research_price("Mortero seco pega pañete 40 kilos", "bulto")
    print(f"    research_price -> {res}")
except Exception as exc:
    print(f"[3] FALLO GeminiPriceResearcher: {exc}")

print("\nListo. Si [1] y [2] pasan, redeploy con USE_GEMINI_PRICE_RESEARCH=true y la key como secret.")

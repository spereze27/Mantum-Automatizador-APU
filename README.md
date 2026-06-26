# Microservicio APU — Comparativo de Mantenimiento (GCP Cloud Run)

Consolida, limpia y compara precios unitarios (APU) de mantenimiento. Lee el
**warehouse** desde Google Sheets, descarga **comparativos** desde un bucket de
GCS, aplica **IPC**, hace **mapeo 1 a 1 (NLP)**, actualiza el Sheet y genera un
**reporte analítico** (Excel en GCS) con: mapeo 1 a 1, análisis de outliers y
comparativo regional.


https://apu-comparativo-mtto-285116077661.us-central1.run.app/
---

## 1. Estructura del proyecto

```
apu-svc/
├── README.md
├── Dockerfile
├── requirements.txt
├── .env.example
├── .dockerignore  / .gitignore
├── src/
│   ├── main.py                 # FastAPI (entrypoint Cloud Run)
│   ├── config.py               # carga de variables de entorno
│   ├── ipc.py                  # factor de ajuste IPC
│   ├── nlp_mapper.py           # normalización regex + fuzzy + embeddings
│   ├── comparativos_loader.py  # descarga GCS + parseo multi-formato (xlsx/csv/pdf)
│   ├── sheets_integration.py   # lectura/escritura del warehouse (gspread)
│   ├── analytics.py            # mapeo, outliers (IQR/Z), pivote regional
│   └── pipeline.py             # orquestación end-to-end
├── config/
│   └── comparativos_config.yaml  # región + formato por archivo comparativo
├── terraform/
│   ├── versions.tf  variables.tf  main.tf  iam.tf  outputs.tf
│   └── terraform.tfvars.example
├── .github/workflows/deploy.yml  # CI/CD build + deploy
└── tests/test_nlp_mapper.py
```

## 2. Lógica de negocio

- **Warehouse**: hoja `BD APU MTTO`, encabezados en la **fila 2**. Se evalúan
  los insumos cuyo `Grupo` ∈ {Material, Mano de obra, Transporte, Equipo} con
  `Vr. Unitario` numérico.
- **Normalización NLP**: primero un diccionario REGEX estandariza unidades
  (`pulg`, `"`, `in` → `in`; `mts/m/ml` → `m`; `gl/galón`; `kg`; etc.), acentos,
  fracciones y símbolos. Luego `thefuzz` (Levenshtein, scorer combinado
  token_set + token_sort para evitar falsos positivos de subconjunto) y,
  opcionalmente, embeddings multilingües como segunda pasada.
- **IPC**: factor `(1 + IPC_VARIATION)`. Se aplica al warehouse y/o a los
  comparativos según `APPLY_IPC_TO_*`.
- **Actualización**: si hay cruce válido, el `Vr. Unitario` se actualiza al
  **mejor precio comparativo**; si no, se mantiene el valor del warehouse
  ajustado por IPC. Con `DRY_RUN=true` no se escribe nada (modo auditoría).
- **Reporte**: `gs://<bucket>/reportes/reporte_apu_<timestamp>.xlsx` con 4 hojas
  (Mapping 1 a 1, Análisis Outliers, Comparativo Regional, Pivot Regional).

## 3. Despliegue

### 3.1 Infraestructura (Terraform)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edita tus valores
terraform init
terraform apply
```

Crea: APIs, Artifact Registry, bucket GCS, SA de runtime, SA de despliegue (con
sus roles mínimos) y el servicio Cloud Run.

**Paso manual obligatorio (Sheets):** Sheets no se autoriza por IAM de proyecto.
Comparte el Google Sheet como **Editor** con el correo de la runtime SA:

```bash
terraform output service_account_email
# -> apu-comparativo-sa@<PROJECT_ID>.iam.gserviceaccount.com
```

Obtén la clave de la SA de despliegue para el secret de GitHub:

```bash
terraform output -raw deployer_sa_key_base64 | base64 -d   # este JSON va a GCP_SA_KEY
```

### 3.2 Cargar comparativos al bucket

```bash
gsutil cp ./comparativos/* gs://<BUCKET>/comparativos/
```
Registra cada archivo nuevo en `config/comparativos_config.yaml` (región + formato).

### 3.3 CI/CD (GitHub Actions)

Push a `main` → build de la imagen, push a Artifact Registry y deploy a Cloud
Run automáticamente (`.github/workflows/deploy.yml`).

### 3.4 Ejecutar el pipeline

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -X POST -H "Authorization: Bearer $TOKEN" https://<cloud-run-url>/run
```
Programable con **Cloud Scheduler** (agrega su SA a `invoker_members` en Terraform).

## 4. Variables de entorno y GitHub Secrets

### GitHub → Settings → Secrets and variables → Actions
| Secret | Descripción |
|---|---|
| `GCP_SA_KEY` | JSON de la SA de despliegue (`apu-deployer-sa`). |
| `GCP_PROJECT_ID` | ID del proyecto GCP. |
| `GCP_REGION` | Región (ej. `us-central1`). |
| `GCS_BUCKET_NAME` | Nombre del bucket de comparativos/reportes. |
| `WAREHOUSE_SHEET_URL` | URL del Google Sheet del warehouse. |
| `IPC_VARIATION` | Variación IPC decimal (ej. `0.0528`). |

### Variables inyectadas en Cloud Run (no secretas)
`GCP_PROJECT_ID`, `GCP_REGION`, `GCS_BUCKET_NAME`, `GCS_INPUT_PREFIX`,
`GCS_OUTPUT_PREFIX`, `WAREHOUSE_SHEET_URL`, `WAREHOUSE_TAB` (=`BD APU MTTO`),
`WAREHOUSE_HEADER_ROW` (=`2`), `IPC_VARIATION`, `APPLY_IPC_TO_WAREHOUSE`,
`APPLY_IPC_TO_COMPARATIVOS`, `FUZZY_THRESHOLD`, `USE_EMBEDDINGS`, `DRY_RUN`.

> En Cloud Run **no** se usa archivo de clave: la autenticación a GCS y Sheets
> es vía ADC con la runtime SA. `GCP_SA_KEY` solo se usa en CI/CD y en local.

## 5. Desarrollo local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # completa valores; exporta o usa python-dotenv
export GOOGLE_APPLICATION_CREDENTIALS=./sa-key.json   # o GCP_SA_KEY
uvicorn src.main:app --reload --port 8080
pytest -q
```

## 6. Notas

- **Embeddings**: con `USE_EMBEDDINGS=true` se usa `sentence-transformers`
  (descomenta en `requirements.txt`; aumenta el tamaño de la imagen).
- **Calidad de parseo**: cada comparativo se mapea por nombre en
  `comparativos_config.yaml`. Las columnas de cantidad/total/IVA se excluyen y se
  aplica un piso de precio para descartar ruido.
- **Workload Identity Federation** es la alternativa recomendada a la clave JSON
  para CI/CD (el workflow ya pide `id-token: write`).
- **Fallback de precio en internet (Gemini)**: con
  `USE_GEMINI_PRICE_RESEARCH=true`, los ítems activos que NO encuentran ninguna
  fuente interna (consolidado/comparativo) que refute su precio se consultan a
  Gemini con *grounding* de Google Search; el precio de referencia hallado (con su
  unidad) queda en `precio_referencia`/`como_se_calculo` y el enlace de la fuente
  en `fuente_que_refuta`/`enlace_fuente`. Controles: `GEMINI_PRICE_MAX_ITEMS`
  (tope de ítems por corrida) y `GEMINI_PRICE_MIN_CONFIDENCE`. La guardia de >50%
  evita auto-aplicar precios web muy distintos de la BD, pero el precio y el enlace
  quedan visibles en el reporte para revisión. Requiere la API `aiplatform`
  habilitada y rol `roles/aiplatform.user` en la SA runtime (ya en Terraform).

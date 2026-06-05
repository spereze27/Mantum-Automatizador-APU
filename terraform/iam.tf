# ==========================================================================
# Service Account del runtime de Cloud Run
# (Autentica contra GCS y Google Sheets vía ADC)
# ==========================================================================
resource "google_service_account" "apu_sa" {
  account_id   = "apu-comparativo-sa"
  display_name = "APU Comparativo - Cloud Run runtime SA"
  description  = "SA usada por el servicio para leer/escribir GCS y Sheets."
}

# Acceso al bucket: leer comparativos y escribir reportes (objectAdmin acotado al bucket).
resource "google_storage_bucket_iam_member" "sa_bucket_object_admin" {
  bucket = google_storage_bucket.comparativos.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.apu_sa.email}"
}

# NOTA sobre Google Sheets:
# El acceso a Sheets NO se concede con un rol IAM de proyecto. La SA debe ser
# COMPARTIDA como "Editor" directamente en el documento de Google Sheets.
# Tras `terraform apply`, comparte el Sheet con el correo:
#   apu-comparativo-sa@<PROJECT_ID>.iam.gserviceaccount.com
# (output `service_account_email`).

# ==========================================================================
# Service Account para el despliegue (GitHub Actions / CI)
# ==========================================================================
resource "google_service_account" "deployer_sa" {
  account_id   = "apu-deployer-sa"
  display_name = "APU Deployer - CI/CD SA"
  description  = "SA usada por GitHub Actions para build & deploy."
}

# Roles mínimos para construir y desplegar.
resource "google_project_iam_member" "deployer_roles" {
  for_each = toset([
    "roles/run.admin",                       # desplegar/actualizar Cloud Run
    "roles/artifactregistry.writer",         # push de imágenes
    "roles/iam.serviceAccountUser",          # actuar como la runtime SA
    "roles/storage.admin",                    # gestionar artefactos de build
    "roles/cloudbuild.builds.editor",        # builds (si se usa Cloud Build)
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer_sa.email}"
}

# El deployer debe poder usar la runtime SA al desplegar Cloud Run.
resource "google_service_account_iam_member" "deployer_actas_runtime" {
  service_account_id = google_service_account.apu_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer_sa.email}"
}

# ==========================================================================
# Clave de la SA de despliegue (para GitHub Secret GCP_SA_KEY)
# Alternativa recomendada: Workload Identity Federation (sin clave).
# ==========================================================================
resource "google_service_account_key" "deployer_key" {
  service_account_id = google_service_account.deployer_sa.name
}

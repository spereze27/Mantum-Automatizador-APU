# ==========================================================================
# APIs necesarias
# ==========================================================================
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "aiplatform.googleapis.com",
    "sheets.googleapis.com",
    "drive.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ==========================================================================
# Artifact Registry (imágenes Docker)
# ==========================================================================
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.artifact_repo
  description   = "Imágenes del microservicio APU"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

# ==========================================================================
# Nombre de bucket: autogenerado y único si no se especifica en tfvars.
# ==========================================================================
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  bucket_name = var.bucket_name != "" ? var.bucket_name : "mantum-apu-${random_id.bucket_suffix.hex}"
}

# ==========================================================================
# Bucket de comparativos + reportes
# ==========================================================================
resource "google_storage_bucket" "comparativos" {
  name                        = local.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# Carpetas lógicas (placeholders) dentro del bucket.
resource "google_storage_bucket_object" "input_prefix" {
  name    = "comparativos/"
  content = " "
  bucket  = google_storage_bucket.comparativos.name
}

resource "google_storage_bucket_object" "output_prefix" {
  name    = "reportes/"
  content = " "
  bucket  = google_storage_bucket.comparativos.name
}

# El servicio Cloud Run NO se gestiona aquí: lo crea/actualiza el pipeline
# de CI/CD con `gcloud run deploy` (evita el problema de imagen inexistente).

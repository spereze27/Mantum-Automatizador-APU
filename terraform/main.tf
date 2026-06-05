# ==========================================================================
# APIs necesarias
# ==========================================================================
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
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
# Bucket de comparativos + reportes
# ==========================================================================
resource "google_storage_bucket" "comparativos" {
  name                        = var.bucket_name
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

# ==========================================================================
# Cloud Run service
# ==========================================================================
resource "google_cloud_run_v2_service" "apu" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.apu_sa.email

    # El pipeline puede tardar; ampliar timeout y recursos.
    timeout = "900s"

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}/${var.service_name}:${var.image_tag}"

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.comparativos.name
      }
      env {
        name  = "GCS_INPUT_PREFIX"
        value = "comparativos/"
      }
      env {
        name  = "GCS_OUTPUT_PREFIX"
        value = "reportes/"
      }
      env {
        name  = "WAREHOUSE_SHEET_URL"
        value = var.warehouse_sheet_url
      }
      env {
        name  = "WAREHOUSE_TAB"
        value = "BD APU MTTO"
      }
      env {
        name  = "WAREHOUSE_HEADER_ROW"
        value = "2"
      }
      env {
        name  = "IPC_VARIATION"
        value = var.ipc_variation
      }
      env {
        name  = "APPLY_IPC_TO_WAREHOUSE"
        value = "true"
      }
      env {
        name  = "APPLY_IPC_TO_COMPARATIVOS"
        value = "false"
      }
      env {
        name  = "FUZZY_THRESHOLD"
        value = "82"
      }
      env {
        name  = "USE_EMBEDDINGS"
        value = "false"
      }
      env {
        name  = "DRY_RUN"
        value = "false"
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.images,
  ]
}

# Invocadores autorizados (no público por defecto).
resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(var.invoker_members)
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.apu.name
  role     = "roles/run.invoker"
  member   = each.value
}

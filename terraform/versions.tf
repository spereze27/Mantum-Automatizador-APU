terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }

  # Recomendado: backend remoto en GCS para el state (descomentar y crear bucket).
  # backend "gcs" {
  #   bucket = "mi-proyecto-tfstate"
  #   prefix = "apu-microservice"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  description = "ID del proyecto de GCP."
  type        = string
}

variable "region" {
  description = "Región de despliegue."
  type        = string
  default     = "us-central1"
}

variable "bucket_name" {
  description = "Nombre del bucket. Si se deja vacío, Terraform genera uno único automáticamente."
  type        = string
  default     = ""
}

variable "artifact_repo" {
  description = "Nombre del repositorio en Artifact Registry."
  type        = string
  default     = "apu-images"
}

variable "github_repo" {
  description = "Repositorio de GitHub autorizado para WIF, formato 'owner/repo'."
  type        = string
}

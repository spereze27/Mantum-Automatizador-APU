variable "project_id" {
  description = "ID del proyecto de GCP."
  type        = string
}

variable "region" {
  description = "Región de despliegue."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Nombre del servicio Cloud Run."
  type        = string
  default     = "apu-comparativo-mtto"
}

variable "bucket_name" {
  description = "Nombre global único del bucket de comparativos/reportes."
  type        = string
}

variable "artifact_repo" {
  description = "Nombre del repositorio en Artifact Registry."
  type        = string
  default     = "apu-images"
}

variable "warehouse_sheet_url" {
  description = "URL del Google Sheet del warehouse."
  type        = string
}

variable "ipc_variation" {
  description = "Variación del IPC como fracción decimal (ej 0.0528)."
  type        = string
  default     = "0.0528"
}

variable "image_tag" {
  description = "Tag de la imagen a desplegar."
  type        = string
  default     = "latest"
}

variable "invoker_members" {
  description = "Miembros autorizados a invocar el servicio (ej Cloud Scheduler SA)."
  type        = list(string)
  default     = []
}

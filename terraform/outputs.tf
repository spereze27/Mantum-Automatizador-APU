output "service_account_email" {
  description = "Correo de la runtime SA. COMPARTE el Google Sheet como Editor con este correo."
  value       = google_service_account.apu_sa.email
}

output "deployer_sa_email" {
  description = "Correo de la SA de despliegue (CI/CD)."
  value       = google_service_account.deployer_sa.email
}

output "deployer_sa_key_base64" {
  description = "Clave JSON (base64) de la SA de despliegue para el secret GCP_SA_KEY."
  value       = google_service_account_key.deployer_key.private_key
  sensitive   = true
}

output "bucket_name" {
  value = google_storage_bucket.comparativos.name
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}"
}

output "cloud_run_url" {
  value = google_cloud_run_v2_service.apu.uri
}

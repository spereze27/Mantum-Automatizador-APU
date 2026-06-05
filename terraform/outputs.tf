output "service_account_email" {
  description = "Correo de la runtime SA. COMPARTE el Google Sheet como Editor con este correo."
  value       = google_service_account.apu_sa.email
}

output "deployer_sa_email" {
  description = "Correo de la SA de despliegue (CI/CD). Va en el secret DEPLOYER_SA_EMAIL."
  value       = google_service_account.deployer_sa.email
}

output "wif_provider" {
  description = "Recurso del proveedor WIF. Va en el secret WIF_PROVIDER del workflow."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "bucket_name" {
  value = google_storage_bucket.comparativos.name
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}"
}

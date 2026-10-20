provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Credentials come from Application Default Credentials or gcloud.
  # This block intentionally has no credentials, access_token, or key file.
}

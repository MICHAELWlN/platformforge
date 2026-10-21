output "service_account_email" {
  description = "Delivery service account. It has no JSON key."
  value       = google_service_account.delivery.email
}

output "workload_identity_provider" {
  description = "Full provider resource name for google-github-actions/auth."
  value       = google_iam_workload_identity_pool_provider.github.name
}

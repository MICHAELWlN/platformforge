output "node_service_account_email" {
  description = "Worker-node service account email."
  value       = google_service_account.nodes.email
}

output "node_service_account_name" {
  description = "Worker-node service account resource name."
  value       = google_service_account.nodes.name
}

output "workload_service_account_email" {
  description = "Workload service account email. This account has no project IAM roles."
  value       = google_service_account.workload.email
}

output "workload_identity_pool" {
  description = "Workload Identity pool attached to the cluster."
  value       = local.workload_pool
}

output "workload_identity_member" {
  description = "IAM member in the Workload Identity binding."
  value       = local.workload_member
}

output "kubernetes_service_account_annotation" {
  description = "Value of iam.gke.io/gcp-service-account for the Kubernetes service account."
  value       = google_service_account.workload.email
}

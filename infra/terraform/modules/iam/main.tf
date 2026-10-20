locals {
  node_account_id     = "${var.name_prefix}-nodes"
  workload_account_id = "${var.name_prefix}-wi"
  workload_pool       = "${var.project_id}.svc.id.goog"
  workload_member     = "serviceAccount:${var.project_id}.svc.id.goog[${var.workload_k8s_namespace}/${var.workload_k8s_service_account}]"

  # The node identity can write logs and metrics and describe its own
  # metadata. It cannot administer the cluster, read Secret Manager, or
  # pull from Artifact Registry. Application pods use GKE_METADATA, so
  # they do not inherit this account.
  node_roles = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/stackdriver.resourceMetadata.writer",
  ])
}

resource "google_service_account" "nodes" {
  project      = var.project_id
  account_id   = local.node_account_id
  display_name = "PlatformForge GKE nodes"
  description  = "Least-privilege identity for PlatformForge worker nodes. No JSON key is created."

  lifecycle {
    precondition {
      condition     = length(local.node_account_id) >= 6 && length(local.node_account_id) <= 30
      error_message = "Derived node service-account id must be 6-30 characters. Shorten name_prefix."
    }
  }
}

resource "google_project_iam_member" "nodes" {
  for_each = local.node_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_service_account" "workload" {
  project      = var.project_id
  account_id   = local.workload_account_id
  display_name = "PlatformForge workload identity"
  description  = "Impersonated via GKE Workload Identity Federation. No project roles are granted."

  lifecycle {
    precondition {
      condition     = length(local.workload_account_id) >= 6 && length(local.workload_account_id) <= 30
      error_message = "Derived workload service-account id must be 6-30 characters. Shorten name_prefix."
    }
  }
}

# roles/iam.workloadIdentityUser is the binding GKE documents for a
# Kubernetes service account. It is not roles/iam.serviceAccountTokenCreator,
# and it does not grant any Google API on its own.
resource "google_service_account_iam_member" "workload_identity_user" {
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.workload_member
}

locals {
  delivery_account_id = "platformforge-dev-cd"
}

resource "google_project_service" "delivery" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ])

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "platformforge-github"
  display_name              = "PlatformForge GitHub Actions"
  description               = "OIDC for one GitHub repository. No service-account keys."

  depends_on = [google_project_service.delivery]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions"
  description                        = "main branch, gke-delivery environment, one repository."

  attribute_mapping = {
    "google.subject"        = "assertion.sub"
    "attribute.repository"  = "assertion.repository"
    "attribute.ref"         = "assertion.ref"
    "attribute.environment" = "assertion.environment"
  }

  attribute_condition = join(" && ", [
    "assertion.repository == \"${var.github_repository}\"",
    "assertion.ref == \"refs/heads/main\"",
    "assertion.environment == \"gke-delivery\"",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "delivery" {
  project      = var.project_id
  account_id   = local.delivery_account_id
  display_name = "PlatformForge GitHub delivery"
  description  = "Impersonated by GitHub Actions OIDC. No JSON key is created."
}

resource "google_service_account_iam_member" "github_wif" {
  service_account_id = google_service_account.delivery.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Upload and tag. This role cannot delete image versions.
resource "google_project_iam_custom_role" "publisher" {
  project     = var.project_id
  role_id     = "platformforgeDeliveryPublisher"
  title       = "PlatformForge image publisher"
  description = "Push digests to Artifact Registry without deleting versions."
  permissions = [
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.uploadArtifacts",
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.dockerimages.get",
    "artifactregistry.dockerimages.list",
    "artifactregistry.tags.create",
    "artifactregistry.tags.get",
    "artifactregistry.tags.list",
    "artifactregistry.tags.update",
    "artifactregistry.versions.get",
    "artifactregistry.versions.list",
  ]
}

resource "google_artifact_registry_repository_iam_member" "publisher" {
  project    = var.project_id
  location   = var.region
  repository = var.repository_id
  role       = google_project_iam_custom_role.publisher.id
  member     = "serviceAccount:${google_service_account.delivery.email}"
}

# get-credentials for one cluster. Kubernetes RBAC, applied separately, is the object boundary.
resource "google_project_iam_custom_role" "cluster_get" {
  project     = var.project_id
  role_id     = "platformforgeDeliveryClusterGet"
  title       = "PlatformForge cluster credential read"
  description = "Read the existing PlatformForge cluster so kubectl can authenticate."
  permissions = [
    "container.clusters.get",
  ]
}

resource "google_project_iam_member" "cluster_get" {
  project = var.project_id
  role    = google_project_iam_custom_role.cluster_get.id
  member  = "serviceAccount:${google_service_account.delivery.email}"

  condition {
    title       = "platformforge-dev only"
    description = "Restrict credential read to the existing zonal cluster."
    # Zonal clusters use zones in the IAM resource name. The live selfLink is
    # projects/platformforge-dev-mn2026/zones/us-central1-a/clusters/platformforge-dev.
    # A regional cluster would use locations instead. Project id, not project number.
    expression = join(" && ", [
      "resource.type == \"container.googleapis.com/Cluster\"",
      "resource.name == \"projects/${var.project_id}/zones/${var.zone}/clusters/${var.cluster_name}\"",
    ])
  }
}

output "project_id" {
  description = "GCP project that would own the resources."
  value       = var.project_id
}

output "region" {
  description = "Region of the subnet, Cloud Router, and Cloud NAT."
  value       = var.region
}

output "zone" {
  description = "Zone of the zonal control plane and the node pool."
  value       = var.zone
}

output "network_name" {
  description = "VPC name."
  value       = module.network.network_name
}

output "network_self_link" {
  description = "VPC self link."
  value       = module.network.network_self_link
}

output "subnet_name" {
  description = "Node subnet name."
  value       = module.network.subnet_name
}

output "subnet_self_link" {
  description = "Node subnet self link."
  value       = module.network.subnet_self_link
}

output "nodes_cidr" {
  description = "Primary CIDR used by worker nodes."
  value       = module.network.nodes_cidr
}

output "pods_cidr" {
  description = "Secondary CIDR used by pods."
  value       = module.network.pods_cidr
}

output "services_cidr" {
  description = "Secondary CIDR used by ClusterIP services."
  value       = module.network.services_cidr
}

output "pods_range_name" {
  description = "Secondary range name for pods."
  value       = module.network.pods_range_name
}

output "services_range_name" {
  description = "Secondary range name for services."
  value       = module.network.services_range_name
}

output "router_name" {
  description = "Cloud Router that hosts Cloud NAT."
  value       = module.network.router_name
}

output "nat_name" {
  description = "Cloud NAT gateway used by private nodes."
  value       = module.network.nat_name
}

output "node_network_tag" {
  description = "Network tag on worker nodes. Firewall rules target this tag."
  value       = module.network.node_network_tag
}

output "cluster_name" {
  description = "GKE cluster name."
  value       = module.gke.cluster_name
}

output "cluster_location" {
  description = "GKE location. A zone means one zonal control plane."
  value       = module.gke.cluster_location
}

output "cluster_endpoint" {
  description = "Control-plane endpoint address reported by GKE. Empty until apply."
  value       = module.gke.cluster_endpoint
}

output "cluster_private_endpoint" {
  description = "Private control-plane address inside the VPC peering."
  value       = module.gke.cluster_private_endpoint
}

output "cluster_public_endpoint" {
  description = "Public control-plane address. Empty when enable_private_endpoint is true."
  value       = module.gke.cluster_public_endpoint
}

output "cluster_ca_certificate" {
  description = "Base64 cluster CA certificate. Sensitive. Empty until apply."
  value       = module.gke.cluster_ca_certificate
  sensitive   = true
}

output "node_pool_name" {
  description = "Explicit node pool name."
  value       = module.gke.node_pool_name
}

output "node_service_account_email" {
  description = "Service account used by worker nodes. Not used by application pods."
  value       = module.iam.node_service_account_email
}

output "workload_service_account_email" {
  description = "GCP service account that one Kubernetes service account may impersonate. It has no project roles."
  value       = module.iam.workload_service_account_email
}

output "workload_identity_pool" {
  description = "GKE Workload Identity pool. GKE creates this pool from the cluster setting; it is not a separate external identity pool."
  value       = module.gke.workload_identity_pool
}

output "workload_identity_member" {
  description = "IAM member allowed to impersonate the workload service account."
  value       = module.iam.workload_identity_member
}

output "kubernetes_service_account_annotation" {
  description = "Annotation to put on the Kubernetes service account after the cluster exists."
  value       = module.iam.kubernetes_service_account_annotation
}

output "control_plane_access" {
  description = "How operators reach the Kubernetes API."
  value       = var.enable_private_endpoint ? "private-endpoint-only" : "public-endpoint-authorized-networks"
}

output "get_credentials_command" {
  description = "Writes a kubeconfig context. Run it only after apply, then show the context and approve it before any PlatformForge command. Do not point integration tests at this context."
  value       = "gcloud container clusters get-credentials ${module.gke.cluster_name} --zone ${var.zone} --project ${var.project_id}"
}

output "deletion_protection" {
  description = "Whether Terraform will refuse to destroy the cluster and node pool."
  value       = var.deletion_protection
}

output "github_delivery_service_account" {
  description = "GitHub delivery service account email, or null while enable_github_delivery is false."
  value       = try(module.github_delivery[0].service_account_email, null)
}

output "github_workload_identity_provider" {
  description = "Workload Identity Provider resource name for GitHub Actions, or null while delivery is disabled."
  value       = try(module.github_delivery[0].workload_identity_provider, null)
}

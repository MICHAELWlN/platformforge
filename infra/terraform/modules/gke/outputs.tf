output "cluster_name" {
  description = "GKE cluster name."
  value       = google_container_cluster.cluster.name
}

output "cluster_location" {
  description = "Cluster location."
  value       = google_container_cluster.cluster.location
}

output "cluster_id" {
  description = "Cluster resource id."
  value       = google_container_cluster.cluster.id
}

output "cluster_endpoint" {
  description = "Endpoint address GKE reports for clients."
  value       = google_container_cluster.cluster.endpoint
}

output "cluster_private_endpoint" {
  description = "Private control-plane address."
  value       = google_container_cluster.cluster.private_cluster_config[0].private_endpoint
}

output "cluster_public_endpoint" {
  description = "Public control-plane address, if the public endpoint is enabled."
  value       = google_container_cluster.cluster.private_cluster_config[0].public_endpoint
}

output "cluster_ca_certificate" {
  description = "Base64 cluster CA certificate."
  value       = google_container_cluster.cluster.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

output "node_pool_name" {
  description = "Node pool name."
  value       = google_container_node_pool.nodes.name
}

output "workload_identity_pool" {
  description = "Workload Identity pool configured on the cluster."
  value       = google_container_cluster.cluster.workload_identity_config[0].workload_pool
}

output "network_name" {
  description = "VPC name."
  value       = google_compute_network.vpc.name
}

output "network_self_link" {
  description = "VPC self link."
  value       = google_compute_network.vpc.self_link
}

output "subnet_name" {
  description = "Node subnet name."
  value       = google_compute_subnetwork.nodes.name
}

output "subnet_self_link" {
  description = "Node subnet self link."
  value       = google_compute_subnetwork.nodes.self_link
}

output "nodes_cidr" {
  description = "Primary node CIDR."
  value       = var.nodes_cidr
}

output "pods_cidr" {
  description = "Pod secondary CIDR."
  value       = var.pods_cidr
}

output "services_cidr" {
  description = "Service secondary CIDR."
  value       = var.services_cidr
}

output "pods_range_name" {
  description = "Pod secondary range name."
  value       = var.pods_range_name
}

output "services_range_name" {
  description = "Service secondary range name."
  value       = var.services_range_name
}

output "router_name" {
  description = "Cloud Router name."
  value       = google_compute_router.nat.name
}

output "nat_name" {
  description = "Cloud NAT name."
  value       = google_compute_router_nat.nat.name
}

output "node_network_tag" {
  description = "Network tag applied to worker nodes."
  value       = local.node_network_tag
}

variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the subnet, router, and NAT."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for network resource names."
  type        = string
}

variable "nodes_cidr" {
  description = "Primary subnet CIDR for nodes."
  type        = string
}

variable "pods_cidr" {
  description = "Secondary CIDR for pods."
  type        = string
}

variable "services_cidr" {
  description = "Secondary CIDR for services."
  type        = string
}

variable "pods_range_name" {
  description = "Name of the pod secondary range."
  type        = string
}

variable "services_range_name" {
  description = "Name of the service secondary range."
  type        = string
}

variable "master_ipv4_cidr" {
  description = "/28 CIDR of the GKE control plane."
  type        = string
}

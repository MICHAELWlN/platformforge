variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "zone" {
  description = "Zonal location of the control plane and the node pool."
  type        = string
}

variable "name_prefix" {
  description = "Cluster name and prefix for the node pool."
  type        = string
}

variable "network_self_link" {
  description = "VPC self link."
  type        = string
}

variable "subnet_self_link" {
  description = "Node subnet self link."
  type        = string
}

variable "pods_range_name" {
  description = "Secondary range name for pods."
  type        = string
}

variable "services_range_name" {
  description = "Secondary range name for services."
  type        = string
}

variable "master_ipv4_cidr" {
  description = "/28 control-plane CIDR."
  type        = string
}

variable "node_service_account_email" {
  description = "Service account email for worker nodes."
  type        = string
}

variable "node_network_tag" {
  description = "Network tag that firewall rules target."
  type        = string
}

variable "labels" {
  description = "GCP labels for the cluster and nodes."
  type        = map(string)
}

variable "release_channel" {
  description = "GKE release channel."
  type        = string
}

variable "node_machine_type" {
  description = "Worker machine type."
  type        = string
}

variable "node_min_count" {
  description = "Autoscaler minimum nodes per zone."
  type        = number
}

variable "node_max_count" {
  description = "Autoscaler maximum nodes per zone."
  type        = number
}

variable "initial_node_count" {
  description = "Initial node count. Used at create only."
  type        = number
}

variable "disk_size_gb" {
  description = "Boot disk size in GB."
  type        = number
}

variable "disk_type" {
  description = "Boot disk type."
  type        = string
}

variable "use_spot_nodes" {
  description = "Whether the pool uses Spot VMs."
  type        = bool
}

variable "max_pods_per_node" {
  description = "Maximum pods per node."
  type        = number
}

variable "maintenance_start_time_utc" {
  description = "Daily maintenance start, HH:MM UTC."
  type        = string
}

variable "deletion_protection" {
  description = "Protect the cluster and node pool from Terraform destroy."
  type        = bool
}

variable "enable_private_endpoint" {
  description = "Disable the public control-plane endpoint."
  type        = bool
}

variable "master_authorized_cidrs" {
  description = "CIDRs allowed to call the control-plane endpoint."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
}

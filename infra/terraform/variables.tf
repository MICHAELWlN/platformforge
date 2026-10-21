variable "project_id" {
  description = "Existing GCP project ID. This configuration does not create a project or attach billing."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters: a lowercase letter, then lowercase letters, digits, or hyphens, and not end with a hyphen."
  }
}

variable "region" {
  description = "Region for the subnet, Cloud Router, and Cloud NAT. Default is us-central1 (Iowa), a low list-price US region."
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]$", var.region))
    error_message = "region must look like us-central1."
  }
}

variable "zone" {
  description = "Zone for the zonal GKE control plane and the node pool. Must be in region. A zonal cluster is the topology eligible for the GKE free-tier management credit."
  type        = string
  default     = "us-central1-a"

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]-[a-z]$", var.zone))
    error_message = "zone must look like us-central1-a. It must also be in region; that check runs at plan time."
  }
}

variable "name_prefix" {
  description = "Prefix for resource names. 3-20 characters so derived firewall names and service-account IDs stay within GCP limits."
  type        = string
  default     = "platformforge-dev"

  validation {
    condition = alltrue([
      can(regex("^[a-z][a-z0-9-]{2,19}$", var.name_prefix)),
      !endswith(var.name_prefix, "-"),
    ])
    error_message = "name_prefix must be 3-20 characters, start with a letter, use lowercase letters, digits, or hyphens, and not end with a hyphen."
  }
}

variable "nodes_cidr" {
  description = "Primary CIDR of the node subnet."
  type        = string
  default     = "10.10.0.0/22"

  validation {
    condition     = can(cidrhost(var.nodes_cidr, 0))
    error_message = "nodes_cidr must be a valid CIDR."
  }
}

variable "pods_cidr" {
  description = "Secondary range for pod IPs. VPC-native alias IPs, not routes."
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition     = can(cidrhost(var.pods_cidr, 0))
    error_message = "pods_cidr must be a valid CIDR."
  }
}

variable "services_cidr" {
  description = "Secondary range for ClusterIP services."
  type        = string
  default     = "10.30.0.0/20"

  validation {
    condition     = can(cidrhost(var.services_cidr, 0))
    error_message = "services_cidr must be a valid CIDR."
  }
}

variable "pods_range_name" {
  description = "Name of the pod secondary range on the subnet."
  type        = string
  default     = "pods"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}$", var.pods_range_name))
    error_message = "pods_range_name must be a short lowercase DNS-like label."
  }
}

variable "services_range_name" {
  description = "Name of the service secondary range on the subnet."
  type        = string
  default     = "services"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,30}$", var.services_range_name))
    error_message = "services_range_name must be a short lowercase DNS-like label."
  }
}

variable "master_ipv4_cidr" {
  description = "RFC1918 /28 for the control-plane VPC peering. Must not overlap the node, pod, or service ranges."
  type        = string
  default     = "172.16.0.32/28"

  validation {
    condition     = can(cidrhost(var.master_ipv4_cidr, 0)) && cidrnetmask(var.master_ipv4_cidr) == "255.255.255.240"
    error_message = "master_ipv4_cidr must be an IPv4 /28."
  }
}

variable "master_authorized_cidrs" {
  description = "IPv4 CIDRs allowed to reach the control-plane endpoint. Use the operator's address as a /32. 0.0.0.0/0 and prefixes broader than /24 are rejected."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))

  validation {
    condition = alltrue([
      length(var.master_authorized_cidrs) > 0,
      length(var.master_authorized_cidrs) <= 10,
      alltrue([
        for c in var.master_authorized_cidrs : alltrue([
          can(cidrhost(c.cidr_block, 0)),
          !strcontains(c.cidr_block, ":"),
          !startswith(c.cidr_block, "0.0.0.0/"),
          tonumber(split("/", c.cidr_block)[1]) >= 24,
          tonumber(split("/", c.cidr_block)[1]) <= 32,
          can(regex("^[A-Za-z][A-Za-z0-9-]{0,62}$", c.display_name)),
        ])
      ]),
    ])
    error_message = "Provide 1-10 IPv4 CIDRs, each between /24 and /32, with a display name. 0.0.0.0/0 is not allowed."
  }
}

variable "enable_private_endpoint" {
  description = "When true, the public control-plane endpoint is disabled. kubectl then works only from a network that can reach the private endpoint. Default false keeps a public endpoint limited to master_authorized_cidrs."
  type        = bool
  default     = false
}

variable "release_channel" {
  description = "GKE release channel. REGULAR is the default because its current minors overlap Istio 1.31's supported Kubernetes versions. The exact patch is chosen by GKE at apply time."
  type        = string
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE", "EXTENDED"], var.release_channel)
    error_message = "release_channel must be RAPID, REGULAR, STABLE, or EXTENDED."
  }
}

variable "node_machine_type" {
  description = "Machine type for the worker pool. e2-standard-4 (4 vCPU, 16 GB) is the default so one node can schedule the existing mesh and observability requests."
  type        = string
  default     = "e2-standard-4"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]+(-[a-z0-9]+)+$", var.node_machine_type))
    error_message = "node_machine_type must look like e2-standard-4."
  }
}

variable "node_min_count" {
  description = "Minimum nodes per zone. This dev stack requires at least 1."
  type        = number
  default     = 1

  validation {
    condition     = var.node_min_count >= 1 && var.node_min_count <= 3 && floor(var.node_min_count) == var.node_min_count
    error_message = "node_min_count must be an integer from 1 to 3."
  }
}

variable "node_max_count" {
  description = "Maximum nodes per zone. Capped at 3 so a typo cannot scale this dev pool."
  type        = number
  default     = 2

  validation {
    condition     = var.node_max_count >= 1 && var.node_max_count <= 3 && floor(var.node_max_count) == var.node_max_count
    error_message = "node_max_count must be an integer from 1 to 3."
  }
}

variable "initial_node_count" {
  description = "Nodes created with the pool. Ignored after create so the autoscaler can change the size without recreating the pool."
  type        = number
  default     = 1

  validation {
    condition     = var.initial_node_count >= 1 && var.initial_node_count <= 3 && floor(var.initial_node_count) == var.initial_node_count
    error_message = "initial_node_count must be an integer from 1 to 3."
  }
}

variable "disk_size_gb" {
  description = "Boot disk size in GB for each node. No extra persistent disks are created."
  type        = number
  default     = 50

  validation {
    condition     = var.disk_size_gb >= 30 && var.disk_size_gb <= 100 && floor(var.disk_size_gb) == var.disk_size_gb
    error_message = "disk_size_gb must be an integer from 30 to 100."
  }
}

variable "disk_type" {
  description = "Boot disk type. pd-balanced is the default. pd-standard is cheaper and slower for image pulls."
  type        = string
  default     = "pd-balanced"

  validation {
    condition     = contains(["pd-balanced", "pd-standard", "pd-ssd"], var.disk_type)
    error_message = "disk_type must be pd-balanced, pd-standard, or pd-ssd."
  }
}

variable "use_spot_nodes" {
  description = "When true, the pool uses Spot VMs. They can be preempted with little notice. Default false."
  type        = bool
  default     = false
}

variable "max_pods_per_node" {
  description = "Pod cap per node. 64 fits the current system, mesh, and observability pods with headroom. GKE carves the pod CIDR from this value."
  type        = number
  default     = 64

  validation {
    condition     = contains([32, 64, 110], var.max_pods_per_node)
    error_message = "max_pods_per_node must be 32, 64, or 110."
  }
}

variable "maintenance_start_time_utc" {
  description = "Daily maintenance window start, HH:MM in UTC."
  type        = string
  default     = "07:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.maintenance_start_time_utc))
    error_message = "maintenance_start_time_utc must be HH:MM in UTC."
  }
}

variable "deletion_protection" {
  description = "When true, terraform destroy refuses to delete the cluster and the node pool until this is set to false and applied."
  type        = bool
  default     = true
}

variable "workload_k8s_namespace" {
  description = "Namespace of the Kubernetes service account allowed to impersonate the workload GCP service account. This is not granted to orders-service or inventory-service."
  type        = string
  default     = "platformforge-dev"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.workload_k8s_namespace)) && length(var.workload_k8s_namespace) <= 63
    error_message = "workload_k8s_namespace must be a DNS label of at most 63 characters."
  }
}

variable "workload_k8s_service_account" {
  description = "Kubernetes service account allowed to impersonate the workload GCP service account. Create it after the cluster exists. It is separate from the mesh identities."
  type        = string
  default     = "platformforge-gcp"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.workload_k8s_service_account)) && length(var.workload_k8s_service_account) <= 63
    error_message = "workload_k8s_service_account must be a DNS label of at most 63 characters."
  }
}

variable "enable_github_delivery" {
  description = "Create the GitHub Actions workload-identity pool and delivery service account. Default false so an ordinary apply does not change IAM."
  type        = bool
  default     = false
}

variable "github_repository" {
  description = "GitHub owner/name allowed to deploy. Required only when enable_github_delivery is true. There is no default repository."
  type        = string
  default     = ""

  validation {
    condition = (
      !var.enable_github_delivery ||
      can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    )
    error_message = "Set github_repository to owner/name before enabling GitHub delivery."
  }
}

variable "labels" {
  description = "GCP resource labels applied to the cluster and nodes. Replacing this variable replaces the whole map."
  type        = map(string)
  default = {
    project     = "platformforge"
    environment = "dev"
    managed-by  = "terraform"
  }

  validation {
    condition = alltrue([
      length(var.labels) > 0,
      length(var.labels) <= 16,
      alltrue([
        for k, v in var.labels : alltrue([
          can(regex("^[a-z][a-z0-9_-]{0,62}$", k)),
          can(regex("^[a-z0-9_-]{1,63}$", v)),
        ])
      ]),
    ])
    error_message = "labels must be 1-16 entries. Keys and values are lowercase letters, digits, underscores, and hyphens. Keys must start with a letter."
  }
}

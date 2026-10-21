resource "terraform_data" "guards" {
  input = "${var.region}/${var.zone}"

  lifecycle {
    precondition {
      condition     = startswith(var.zone, "${var.region}-")
      error_message = "zone must be in region."
    }

    precondition {
      condition = alltrue([
        var.node_min_count <= var.initial_node_count,
        var.initial_node_count <= var.node_max_count,
        var.node_min_count <= var.node_max_count,
      ])
      error_message = "Require node_min_count <= initial_node_count <= node_max_count."
    }
  }
}

resource "google_project_service" "required" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ])

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

module "network" {
  source = "./modules/network"

  project_id          = var.project_id
  region              = var.region
  name_prefix         = var.name_prefix
  nodes_cidr          = var.nodes_cidr
  pods_cidr           = var.pods_cidr
  services_cidr       = var.services_cidr
  pods_range_name     = var.pods_range_name
  services_range_name = var.services_range_name
  master_ipv4_cidr    = var.master_ipv4_cidr

}

module "iam" {
  source = "./modules/iam"

  project_id                   = var.project_id
  name_prefix                  = var.name_prefix
  workload_k8s_namespace       = var.workload_k8s_namespace
  workload_k8s_service_account = var.workload_k8s_service_account

  depends_on = [google_project_service.required]
}

module "gke" {
  source = "./modules/gke"

  project_id                 = var.project_id
  zone                       = var.zone
  name_prefix                = var.name_prefix
  network_self_link          = module.network.network_self_link
  subnet_self_link           = module.network.subnet_self_link
  pods_range_name            = module.network.pods_range_name
  services_range_name        = module.network.services_range_name
  master_ipv4_cidr           = var.master_ipv4_cidr
  node_service_account_email = module.iam.node_service_account_email
  node_network_tag           = module.network.node_network_tag
  labels                     = var.labels
  release_channel            = var.release_channel
  node_machine_type          = var.node_machine_type
  node_min_count             = var.node_min_count
  node_max_count             = var.node_max_count
  initial_node_count         = var.initial_node_count
  disk_size_gb               = var.disk_size_gb
  disk_type                  = var.disk_type
  use_spot_nodes             = var.use_spot_nodes
  max_pods_per_node          = var.max_pods_per_node
  maintenance_start_time_utc = var.maintenance_start_time_utc
  deletion_protection        = var.deletion_protection
  enable_private_endpoint    = var.enable_private_endpoint
  master_authorized_cidrs    = var.master_authorized_cidrs

  depends_on = [google_project_service.required]
}

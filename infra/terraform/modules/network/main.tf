locals {
  node_network_tag = "${var.name_prefix}-node"

  # Terraform 1.16 has cidrhost but not cidrcontains. Compare integer
  # bounds instead. Powers of two through 2^32 are exact in this number type.
  cidr_ranges = {
    nodes    = var.nodes_cidr
    pods     = var.pods_cidr
    services = var.services_cidr
    master   = var.master_ipv4_cidr
  }

  cidr_start = {
    for name, cidr in local.cidr_ranges : name => (
      tonumber(split(".", cidrhost(cidr, 0))[0]) * 16777216 +
      tonumber(split(".", cidrhost(cidr, 0))[1]) * 65536 +
      tonumber(split(".", cidrhost(cidr, 0))[2]) * 256 +
      tonumber(split(".", cidrhost(cidr, 0))[3])
    )
  }

  cidr_size = {
    for name, cidr in local.cidr_ranges : name => pow(2, 32 - tonumber(split("/", cidr)[1]))
  }

  cidr_pairs = [
    ["nodes", "pods"],
    ["nodes", "services"],
    ["nodes", "master"],
    ["pods", "services"],
    ["pods", "master"],
    ["services", "master"],
  ]

  cidrs_overlap = anytrue([
    for pair in local.cidr_pairs : alltrue([
      local.cidr_start[pair[0]] <= local.cidr_start[pair[1]] + local.cidr_size[pair[1]] - 1,
      local.cidr_start[pair[1]] <= local.cidr_start[pair[0]] + local.cidr_size[pair[0]] - 1,
    ])
  ])

  cidrs_are_network_addresses = alltrue([
    for cidr in local.cidr_ranges : split("/", cidr)[0] == cidrhost(cidr, 0)
  ])
}

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = var.name_prefix
  description             = "Dedicated VPC for the PlatformForge development cluster. Auto subnets are off so the default network's allow-ssh and allow-internal rules are not created."
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  # The default route to the internet gateway stays. Cloud NAT uses it for egress.
  # Private nodes still have no inbound public address.
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "nodes" {
  project                  = var.project_id
  name                     = "${var.name_prefix}-nodes"
  description              = "GKE node subnet with alias ranges for pods and ClusterIP services."
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.nodes_cidr
  private_ip_google_access = true
  stack_type               = "IPV4_ONLY"

  secondary_ip_range {
    range_name    = var.pods_range_name
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = var.services_range_name
    ip_cidr_range = var.services_cidr
  }

  # VPC flow logs are a vended log with no free allotment. They stay off
  # on this dev subnet. See docs/CLOUD_COSTS.md.

  lifecycle {
    precondition {
      condition     = local.cidrs_are_network_addresses
      error_message = "nodes_cidr, pods_cidr, services_cidr, and master_ipv4_cidr must be network addresses, such as 10.10.0.0/22."
    }

    precondition {
      condition     = !local.cidrs_overlap
      error_message = "nodes_cidr, pods_cidr, services_cidr, and master_ipv4_cidr must not overlap."
    }
  }
}

resource "google_compute_router" "nat" {
  project = var.project_id
  name    = "${var.name_prefix}-router"
  region  = var.region
  network = google_compute_network.vpc.id

  bgp {
    asn = 64514
  }
}

resource "google_compute_router_nat" "nat" {
  project                            = var.project_id
  name                               = "${var.name_prefix}-nat"
  router                             = google_compute_router.nat.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.nodes.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  # ERRORS_ONLY avoids logging every translation. NAT logs are vended
  # network logs and have no Cloud Logging free allotment.
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Control plane to nodes. GKE's own rules cover kubelet and common
# webhooks. Port 15017 is the Istio sidecar injector, which those rules
# do not include. Without it, injection webhooks fail on a private-node cluster.
resource "google_compute_firewall" "control_plane_to_nodes" {
  project     = var.project_id
  name        = "${var.name_prefix}-cp-to-nodes"
  description = "Allow the GKE control plane to reach kubelet and admission webhooks, including Istio's injector on tcp/15017."
  network     = google_compute_network.vpc.id
  direction   = "INGRESS"
  priority    = 1000

  source_ranges = [var.master_ipv4_cidr]
  target_tags   = [local.node_network_tag]

  allow {
    protocol = "tcp"
    ports    = ["443", "8443", "10250", "15017"]
  }
}

# Custom-mode VPCs do not get the auto-mode allow-internal rule.
# GKE also creates its own intra-cluster rules at apply time. This rule
# makes the intended node and pod path explicit. It is additive.
resource "google_compute_firewall" "nodes_and_pods" {
  project     = var.project_id
  name        = "${var.name_prefix}-nodes-and-pods"
  description = "Allow node and pod traffic to worker nodes. Does not allow the public internet."
  network     = google_compute_network.vpc.id
  direction   = "INGRESS"
  priority    = 1000

  source_ranges = [var.nodes_cidr, var.pods_cidr]
  target_tags   = [local.node_network_tag]

  allow {
    protocol = "tcp"
  }

  allow {
    protocol = "udp"
  }

  allow {
    protocol = "icmp"
  }
}

# Implied allow-egress already permits this. The rule records the
# control-plane ports and stays in front of any later lower-priority deny.
resource "google_compute_firewall" "nodes_to_control_plane" {
  project     = var.project_id
  name        = "${var.name_prefix}-nodes-to-cp"
  description = "Allow workers to reach the control plane on the Kubernetes API and konnectivity ports."
  network     = google_compute_network.vpc.id
  direction   = "EGRESS"
  priority    = 1000

  destination_ranges = [var.master_ipv4_cidr]
  target_tags        = [local.node_network_tag]

  allow {
    protocol = "tcp"
    ports    = ["443", "8132"]
  }
}

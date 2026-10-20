resource "google_container_cluster" "cluster" {
  project  = var.project_id
  name     = var.name_prefix
  location = var.zone

  description = "PlatformForge development cluster. Zonal Standard GKE, private nodes, Workload Identity. No application load balancer is created here."

  # A zone location is one control plane. That is the topology the GKE
  # free-tier credit can cover. It is also a single zone of failure.
  remove_default_node_pool = true
  initial_node_count       = 1

  network    = var.network_self_link
  subnetwork = var.subnet_self_link

  networking_mode             = "VPC_NATIVE"
  datapath_provider           = "ADVANCED_DATAPATH"
  enable_intranode_visibility = true

  # Dataplane V2 enforces NetworkPolicy. enabled = false keeps the Calico
  # add-on off. The add-on conflicts with ADVANCED_DATAPATH.
  network_policy {
    enabled = false
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
    stack_type                    = "IPV4"
  }

  default_max_pods_per_node = var.max_pods_per_node

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.master_ipv4_cidr

    master_global_access_config {
      enabled = false
    }
  }

  # The public endpoint stays reachable only from these CIDRs, and only
  # with Google credentials. gcp_public_cidrs_access_enabled = false
  # stops GKE from also allowing every Google public address.
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled      = false
    private_endpoint_enforcement_enabled = true

    dynamic "cidr_blocks" {
      for_each = var.master_authorized_cidrs
      content {
        cidr_block   = cidr_blocks.value.cidr_block
        display_name = cidr_blocks.value.display_name
      }
    }
  }

  # The DNS control-plane name is a second door on recent GKE versions.
  # External traffic to it is off. The IP endpoint remains, behind the
  # authorized-network list above.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = false
    }

    ip_endpoints_config {
      enabled = true
    }
  }

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  anonymous_authentication_config {
    mode = "LIMITED"
  }

  rbac_binding_config {
    enable_insecure_binding_system_unauthenticated = false
    enable_insecure_binding_system_authenticated   = false
  }

  enable_legacy_abac      = false
  enable_shielded_nodes   = true
  enable_kubernetes_alpha = false
  enable_tpu              = false
  # enable_autopilot is omitted. Provider 8.7 rejects the argument, even
  # when false, next to Standard-only settings such as remove_default_node_pool.
  # The API default is a Standard cluster.

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = var.maintenance_start_time_utc
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]

    managed_prometheus {
      enabled = false
    }
  }

  gateway_api_config {
    channel = "CHANNEL_DISABLED"
  }

  vertical_pod_autoscaling {
    enabled = false
  }

  # Node auto-provisioning would create extra pools and extra VMs.
  cluster_autoscaling {
    enabled = false
  }

  addons_config {
    http_load_balancing {
      disabled = true
    }

    horizontal_pod_autoscaling {
      disabled = false
    }

    # Dataplane V2 enforces NetworkPolicy. The Calico addon conflicts with it.
    network_policy_config {
      disabled = true
    }

    # The driver is on so a later approved PVC can bind. This configuration
    # does not create a PersistentVolume or an extra disk.
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }

    gcp_filestore_csi_driver_config {
      enabled = false
    }

    gcs_fuse_csi_driver_config {
      enabled = false
    }

    gke_backup_agent_config {
      enabled = false
    }

    config_connector_config {
      enabled = false
    }

    dns_cache_config {
      enabled = false
    }

    stateful_ha_config {
      enabled = false
    }
  }

  # Blocks Service spec.externalIPs. It does not block Service type
  # LoadBalancer; PlatformForge never renders that type.
  service_external_ips_config {
    enabled = false
  }

  resource_labels = var.labels

  deletion_protection = var.deletion_protection

  lifecycle {
    precondition {
      condition     = var.node_min_count <= var.initial_node_count && var.initial_node_count <= var.node_max_count
      error_message = "initial_node_count must be between node_min_count and node_max_count."
    }
  }
}

resource "google_container_node_pool" "nodes" {
  project  = var.project_id
  name     = "${var.name_prefix}-pool"
  location = var.zone
  cluster  = google_container_cluster.cluster.name

  initial_node_count        = var.initial_node_count
  ignore_node_count_changes = true

  autoscaling {
    min_node_count = var.node_min_count
    max_node_count = var.node_max_count
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Surge 1 keeps a node up during an upgrade. On a 1-node pool that is a
  # second VM for the length of the upgrade.
  upgrade_settings {
    strategy        = "SURGE"
    max_surge       = 1
    max_unavailable = 0
  }

  max_pods_per_node = var.max_pods_per_node

  network_config {
    enable_private_nodes = true
  }

  node_config {
    machine_type    = var.node_machine_type
    image_type      = "COS_CONTAINERD"
    spot            = var.use_spot_nodes
    service_account = var.node_service_account_email
    tags            = [var.node_network_tag]
    resource_labels = var.labels

    boot_disk {
      size_gb   = var.disk_size_gb
      disk_type = var.disk_type
    }

    # cloud-platform is the scope Google documents for a custom node
    # service account. The account's IAM roles are the privilege boundary.
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    kubelet_config {
      insecure_kubelet_readonly_port_enabled = "FALSE"
    }

    metadata = {
      disable-legacy-endpoints = "true"
      block-project-ssh-keys   = "true"
    }

    labels = {
      "platformforge.dev/managed-by" = "terraform"
    }
  }

  # initial_node_count forces a new pool if it changes later. The
  # autoscaler owns the size after create.
  lifecycle {
    ignore_changes = [initial_node_count]
  }

  deletion_policy = var.deletion_protection ? "PREVENT" : "DELETE"
}

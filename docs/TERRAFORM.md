# Terraform

The GKE development stack is `infra/terraform/`. It is a Terraform root, not a
published module. `terraform apply` has not been run. Validation is recorded in
[TEST_EVIDENCE.md](../TEST_EVIDENCE.md). How to operate the cluster, and the
approval required before any create, is in [GKE_DEPLOYMENT.md](GKE_DEPLOYMENT.md).
Prices are in [CLOUD_COSTS.md](CLOUD_COSTS.md).

## Versions

| Tool | Constraint | What was installed for validation |
| --- | --- | --- |
| Terraform | `>= 1.10.0, < 2.0.0` | 1.16.5 (darwin_arm64) |
| hashicorp/google | `~> 8.7` | 8.7.0, pinned in `.terraform.lock.hcl` |

Provider 8.7 is the registry release from 13 October 2026. Arguments used in
this root were checked against that provider's `google_container_cluster` and
`google_container_node_pool` docs. `enable_autopilot` is omitted: in 8.7 the
argument conflicts with Standard-only settings even when the value is `false`.
The API default is a Standard cluster.

There is no `google-beta` provider and no Kubernetes provider. Terraform stops
at GCP objects. Istio and the observability charts stay `kubectl` and Helm.

## Layout

```
infra/terraform/
  versions.tf                 Terraform and provider constraints, local backend
  providers.tf                google provider; no credentials block
  variables.tf                project, location, CIDRs, size, labels
  main.tf                     API enablement and module wiring
  outputs.tf
  terraform.tfvars.example    placeholders; not auto-loaded
  backend.tf.example          GCS backend; not loaded until copied to backend.tf
  .terraform.lock.hcl         commit this
  modules/network/            VPC, subnet, Cloud Router, Cloud NAT, firewall
  modules/iam/                node SA, workload SA, Workload Identity binding
  modules/gke/                zonal cluster and the explicit node pool
```

`google_project_service.required` enables `compute`, `container`, `iam`,
`logging`, and `monitoring`. `disable_on_destroy` is false, so a later destroy
does not turn those APIs off for the rest of the project. Enabling an API is
part of apply and needs the same approval as the cluster.

## State

The backend is local:

```hcl
backend "local" {
  path = "terraform.tfstate"
}
```

`*.tfstate`, `*.tfplan`, `.terraform/`, and `infra/terraform/terraform.tfvars`
are gitignored. `.terraform.lock.hcl` is not. The provider block has no
`credentials`, `access_token`, or key file. Use Application Default Credentials.

Local state is not encrypted by Terraform. It stays off the remote only if it
is not committed, and it is protected at rest only by the machine's disk
encryption. Before anyone else uses this root, copy `backend.tf.example` to
`backend.tf` after a bucket exists with:

- the same region as the cluster
- uniform bucket-level access
- public access prevention enforced
- object versioning
- no `allUsers` or `allAuthenticatedUsers` binding

The bucket is not a resource in this root. Creating it is a separate, approved
change, and its own state must not live in the bucket it creates. Switching
backends is `terraform init -migrate-state` from `infra/terraform/`.

## What each module owns

**network.** Custom-mode VPC, regional routing, one subnet with Private Google
Access and secondary ranges for pods and services. Cloud Router ASN 64514 and
Cloud NAT (`AUTO_ONLY`, all ranges on that subnet, logs `ERRORS_ONLY`). Three
firewall rules target the node tag `<name_prefix>-node`:

| Rule | Direction | Purpose |
| --- | --- | --- |
| `<prefix>-cp-to-nodes` | ingress from the control-plane /28 | tcp 443, 8443, 10250, and 15017 (Istio injector) |
| `<prefix>-nodes-and-pods` | ingress from the node and pod CIDRs | tcp, udp, icmp |
| `<prefix>-nodes-to-cp` | egress to the control-plane /28 | tcp 443 and 8132 |

GKE also creates its own firewall rules at apply time. Ours are additive. They
do not replace those rules, and they do not open the nodes to the internet.
There is no SSH rule and no load-balancer health-check rule.

A precondition on the subnet rejects overlapping CIDRs and CIDRs that are not
network addresses. Terraform 1.16 has `cidrhost` and does not have
`cidrcontains`, so the check compares integer bounds. That precondition runs
at plan time. `terraform validate` does not execute it. The default ranges do
not overlap; that arithmetic was checked outside Terraform and is recorded in
TEST_EVIDENCE.md.

**iam.** Two service accounts, no JSON keys.

| Account | ID | Project roles |
| --- | --- | --- |
| Nodes | `<name_prefix>-nodes` | `logging.logWriter`, `monitoring.metricWriter`, `stackdriver.resourceMetadata.writer` |
| Workload | `<name_prefix>-wi` | none |

The workload account has `roles/iam.workloadIdentityUser` for
`serviceAccount:<project>.svc.id.goog[<namespace>/<kubernetes SA>]`. The
default member is `platformforge-dev/platformforge-gcp`. It is not
`orders-service` or `inventory-service`. Those mesh identities stay without a
Google binding. The role lets that one Kubernetes service account impersonate
the GCP account. It does not grant a Google API. `roles/iam.serviceAccountTokenCreator`
is not used.

This is GKE Workload Identity Federation: the pool is `<project>.svc.id.goog`,
set on the cluster with `workload_identity_config`. It is not a
`google_iam_workload_identity_pool` for GitHub or another external issuer.
That resource is a different product and is not created here.

**gke.** Zonal Standard cluster (`location` is the zone). The default node
pool is created only because the API requires one, then removed. The lasting
pool is `google_container_node_pool.nodes`. Private nodes, Workload Identity,
Dataplane V2, shielded nodes with secure boot, the GKE Metadata Server, client
certificates off, anonymous authentication `LIMITED`, and the HTTP load
balancing add-on off. Managed Service for Prometheus is off. Logging and
monitoring components are `SYSTEM_COMPONENTS` only. Node auto-provisioning is
off. The pool autoscales between `node_min_count` and `node_max_count` (default
1 and 2), both capped at 3 by variable validation.

## Variables that matter

`project_id` and `master_authorized_cidrs` have no defaults. Copy
`terraform.tfvars.example` to `terraform.tfvars` and replace both. The example
CIDR `203.0.113.10/32` is a documentation address. A prefix broader than /24,
and `0.0.0.0/0`, is rejected.

| Variable | Default | Effect |
| --- | --- | --- |
| `region` | `us-central1` | Subnet, router, NAT |
| `zone` | `us-central1-a` | Zonal control plane and nodes |
| `name_prefix` | `platformforge-dev` | Resource names, 3–20 characters |
| `nodes_cidr` | `10.10.0.0/22` | Node primary range |
| `pods_cidr` | `10.20.0.0/16` | Pod alias range |
| `services_cidr` | `10.30.0.0/20` | ClusterIP range |
| `master_ipv4_cidr` | `172.16.0.32/28` | Control-plane peering |
| `release_channel` | `REGULAR` | GKE picks the patch at create |
| `node_machine_type` | `e2-standard-4` | 4 vCPU, 16 GB |
| `disk_size_gb` / `disk_type` | `50` / `pd-balanced` | Boot disk only |
| `use_spot_nodes` | `false` | Spot is opt-in |
| `max_pods_per_node` | `64` | Also 32 or 110 |
| `enable_private_endpoint` | `false` | See GKE_DEPLOYMENT.md |
| `deletion_protection` | `true` | Destroy needs a prior apply with `false` |
| `labels` | project, environment, managed-by | Replacing the variable replaces the whole map |

`zone` must be inside `region`. That comparison is a plan-time precondition,
because a variable validation block cannot read another variable.

## Commands

From the repository root:

```bash
make terraform-validate
```

Or, from `infra/terraform/`:

```bash
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
```

`init -backend=false` downloads the provider and does not configure the local
backend. A normal `terraform init` (without `-backend=false`) configures the
local backend and still does not create GCP resources.

These commands do not prove the stack can be created. They prove the
configuration parses, the provider schema accepts the arguments, and the
module references line up. They do not check quotas, org policies, IAM on the
caller, stock, or the overlap precondition. `terraform plan` was not run:
this machine had no credentialed `gcloud` account and no project.

Do not run `terraform apply` or `terraform destroy` unless that exact action
has been approved. See [GKE_DEPLOYMENT.md](GKE_DEPLOYMENT.md).

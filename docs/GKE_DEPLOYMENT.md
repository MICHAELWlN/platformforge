# GKE development environment

Terraform for one dedicated GKE development cluster. The configuration is in
[`infra/terraform/`](../infra/terraform/). It has been formatted and validated.
It has not been applied. No GCP resource has been created, and the local kind
cluster was not changed.

This does not deploy PlatformForge workloads, Istio, or the observability
stack. Those stay on the procedures in [ISTIO.md](ISTIO.md) and
[OBSERVABILITY.md](OBSERVABILITY.md), aimed at whatever cluster you have
approved. Module layout and state are in [TERRAFORM.md](TERRAFORM.md). List
prices are in [CLOUD_COSTS.md](CLOUD_COSTS.md).

## What the default stack is

| Choice | Value | Why |
| --- | --- | --- |
| Mode | GKE Standard, not Autopilot | Istio CNI runs a privileged DaemonSet in `istio-system`. Autopilot's policy is a poor fit, and the VM bill is easier to predict |
| Topology | Zonal, `us-central1-a` | One control plane. The GKE free-tier credit can cover that fee. The zone is also a single point of failure |
| Release channel | `REGULAR` | On 2026-10-20 the public GKE release notes listed Regular minors 1.34–1.36, default create `1.36.4-gke.1495002`. Istio 1.31 supports Kubernetes 1.32–1.37. Regular is inside that range. The kind cluster is 1.37, which is a Rapid minor, not the Regular default |
| Nodes | Private, `e2-standard-4`, pool size 1–2 | No external IP. 4 vCPU and 16 GB covers the requests already in this repo on one node. See sizing below |
| Identity | Workload Identity pool `<project>.svc.id.goog` | Node SA writes logs and metrics only. Pods use the GKE Metadata Server and do not inherit the node SA |
| Ingress | None | HTTP load-balancing add-on off. No forwarding rule, Gateway, or external Service IP |
| Datapath | Dataplane V2 | NetworkPolicy without the Calico add-on, which conflicts with this datapath |

The exact Kubernetes patch is chosen by GKE at create time. It was not read
from the GKE API. This machine had no project and no credentialed account.

### Sizing

Requests already declared in the repo, before GKE system pods:

| Workload | Requests |
| --- | --- |
| inventory v0.1.0 ×2, v0.2.0 ×1, orders ×2 | 250m CPU, 320Mi |
| Five Istio sidecars at the install config | 100m CPU, 320Mi |
| istiod and the CNI plugin | 150m CPU, 320Mi |
| Prometheus, Grafana, Loki, Tempo, Alloy | 220m CPU, about 960Mi |

That is roughly 720m CPU and 1.9 GiB, plus small reloaders. On kind, the whole
node including system pods was 1860m CPU and 2562Mi of requests. An
`e2-standard-2` (2 vCPU, 8 GB) can be tight once GKE's own pods are added.
`e2-standard-4` is the default so one node can schedule the mesh and the
telemetry stack. `max_pods_per_node` is 64. A count near 30 (system, mesh,
apps, telemetry) does not fit comfortably in 32.

Boot disk is 50 GB `pd-balanced`. No extra disk is created. Telemetry on kind
uses emptyDir; this root does not add a PersistentVolume.

## Public control-plane endpoint

`enable_private_endpoint` defaults to `false`. Worker nodes are private.
The Kubernetes API has a public address.

What limits that address:

- `master_authorized_cidrs` is required. `0.0.0.0/0` and anything broader than
  a /24 are rejected. The expected value is the operator's current IPv4 as a
  /32.
- `gcp_public_cidrs_access_enabled` is false, so GKE does not also allow every
  Google public address.
- Authorized networks are enforced on the private endpoint as well.
- Client certificates are not issued. Anonymous authentication is `LIMITED`.
- The DNS control-plane endpoint does not accept external traffic. Clients use
  the IP endpoint.
- Calls still need Google credentials (`container.clusters.get` and the usual
  Kubernetes RBAC). An open network path is not anonymous cluster-admin.

What this does not do: the endpoint is on the internet. A mistake that widens
the CIDR, or a stolen credential used from an allowed address, is enough to
reach the API. Cloud Shell and other Google clients whose addresses change
will not match a /32.

The alternative is `enable_private_endpoint = true`. The public address goes
away. `kubectl` then works only from a network that can route to the private
endpoint (a VM in the VPC, VPN, or Interconnect). This root does not create a
bastion. A bastion would be another VM and another approval. That is why the
default keeps the public endpoint and spends the control on the allow list
instead of a second machine.

Master global access is off. The private endpoint is not exposed to other
regions.

## Networking and firewall

| Range | CIDR |
| --- | --- |
| Nodes | `10.10.0.0/22` |
| Pods | `10.20.0.0/16` |
| Services | `10.30.0.0/20` |
| Control plane | `172.16.0.32/28` |

The VPC is custom mode, so it does not inherit the auto-mode rules that allow
SSH from `0.0.0.0/0`. Private Google Access is on. Cloud NAT is egress-only
for the subnet, including pod addresses, so nodes can pull images and a pod
can reach the internet. There is no default-deny egress rule. Locking egress
down needs an allow list for the control plane, DNS, the metadata server, and
Google APIs, and that list was not tested against a live cluster.

Port 15017 from the control-plane /28 to the node tag is explicit because
Istio's injector webhook listens there and GKE's own master rule does not
include it. GKE will still add its own rules when the cluster is created.
Those rules are additive. NodePorts are not opened to the internet. Nodes
have no external IP, and project SSH keys are blocked on the instances. There
is no IAP SSH rule.

`service_external_ips_config` rejects `Service.spec.externalIPs`. It does not
reject `Service` type `LoadBalancer`. PlatformForge never renders that type.
A person with `kubectl` access still could. The GCE Ingress controller is
disabled, so a Kubernetes Ingress does not create an HTTP load balancer.

## Identity

The node service account is `<name_prefix>-nodes` (default
`platformforge-dev-nodes`). Its roles are log writer, metric writer, and
resource-metadata writer. It is not `roles/editor` and it is not
`roles/container.admin`. The node OAuth scope is `cloud-platform`, which is
the scope Google documents for a custom node account. The IAM roles are the
privilege boundary. Artifact Registry reader is not granted; that registry
does not exist yet.

Pods see the GKE Metadata Server (`GKE_METADATA`), not the node's account.
Legacy metadata endpoints are disabled.

The workload service account `<name_prefix>-wi` has no project roles. After
the cluster exists, and only on that cluster, a separate Kubernetes service
account can be created and annotated. Do not apply this to kind:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: platformforge-gcp
  namespace: platformforge-dev
  annotations:
    iam.gke.io/gcp-service-account: platformforge-dev-wi@PROJECT_ID.iam.gserviceaccount.com
```

`orders-service` and `inventory-service` are intentionally not in that
binding. Mesh SPIFFE identity and Google Workload Identity are different
systems. Granting this GCP account any role is a separate approval. Until
then, impersonation has nothing to call.

## Prerequisites

- A GCP project that already exists and already has billing attached. This
  Terraform does not create a project and does not link billing. Linking
  billing is what makes the project able to incur charges, and it needs its
  own approval.
- Terraform 1.10 or newer. 1.16.5 was used here. `gcloud` SDK 590 was
  installed and was not logged in.
- Quota in the chosen zone for two `e2-standard-4` VMs (the upgrade surge),
  not only one, plus one external IP for Cloud NAT.
- The operator's current public IPv4.

This machine, on 2026-10-20: `gcloud auth list` reported no credentialed
accounts, and `gcloud config get-value project` was unset.

### Permissions for the identity that runs apply

These predefined roles are sufficient and broader than the resources this
root creates. A custom role was not added, because this configuration does
not know who the operator is and should not grant project IAM to itself.
They were not tested with an apply.

- `roles/serviceusage.serviceUsageAdmin` to enable the five APIs
- `roles/compute.networkAdmin` for the VPC, subnet, firewall, router, and NAT
- `roles/container.admin` for the cluster and node pool
- `roles/iam.serviceAccountAdmin` to create the two service accounts
- `roles/iam.serviceAccountUser` on the node service account, so it can be
  attached to the VMs
- `roles/resourcemanager.projectIamAdmin` to grant the three node roles

Do not grant any of these to the node account or the workload account.

Org policies can still reject the apply. Examples that this design already
matches: shielded VM required, no external IP on VMs, no service-account keys.
An org policy that forbids a public control-plane endpoint will reject the
default. Set `enable_private_endpoint = true` only if you also have a path
into the VPC.

### Authentication

The provider has no key and no token. From a machine where you intend to
plan or apply:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project PROJECT_ID
```

Copy `infra/terraform/terraform.tfvars.example` to `terraform.tfvars`, set
`project_id`, and replace `203.0.113.10/32` with your address. That file is
gitignored.

## Commands that do not create resources

```bash
make terraform-validate
```

That is `terraform fmt -check`, `terraform init -backend=false`, and
`terraform validate`. Details and the state backend are in
[TERRAFORM.md](TERRAFORM.md).

`terraform plan` is read-only with respect to GCP, and it was not run here.
Run it only after the authentication steps above, from `infra/terraform/`,
and read the plan before any apply.

## Apply

Do not run this until a person has explicitly approved creating the billable
resources and enabling the five APIs.

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

`deletion_protection` defaults to true. Apply will still create the cluster.
A later destroy will fail until that flag is changed. See below.

IAM bindings can take a short time to propagate. If the first node-pool
create fails on permission, run apply again after the node roles exist. Do
not work around that by granting the node account a broader role.

### After apply: kubeconfig

`terraform output get_credentials_command` prints a `gcloud container clusters
get-credentials` invocation. Running it writes a context. It does not deploy
anything. Then:

```bash
kubectl config current-context
```

PlatformForge prints that context and waits for approval before it talks to
the API. Pass `--approve-context` only when the name matches exactly.
`make integration` refuses anything that is not a `kind-*` context. Do not
point it at GKE.

With the public endpoint, `get-credentials` works from an address on the
allow list. With `enable_private_endpoint = true`, it works only from inside
the VPC.

Private nodes and, once Istio is installed, STRICT mTLS mean the same access
pattern as kind: in-cluster clients or `kubectl port-forward`, not a public
Service and not the API-server proxy.

### Workloads, mesh, and telemetry

Not part of apply.

1. Confirm the cluster's minor against the Istio supported-releases page
   before `istioctl install`. Istio 1.31.2 is what this repo uses. The GKE
   Istio add-on is not enabled and should not be.
2. `istio-system` still needs Pod Security `privileged` for the CNI plugin.
   `platformforge-dev` stays `restricted` and STRICT. Do not open another
   namespace to injection.
3. Images are `platformforge/*`, loaded into kind with `kind load`. GKE
   cannot pull them. Artifact Registry is not in this root. Publishing images
   is a later, separate approval.
4. Observability stays ClusterIP, in `platformforge-observability`, with
   injection off. Do not point Prometheus at a public address. Cloud Logging
   is system components only so it does not replace Loki and does not ingest
   every workload line.

## Verification

`terraform validate` does not prove the cluster can be created. After an
approved apply, check at least:

- `gcloud container clusters describe` shows private nodes, the Workload
  Identity pool, and no Autopilot.
- Nodes have no `EXTERNAL-IP`.
- The only Services you add are ClusterIP.
- `terraform output` shows the node service account and the workload account.
- The workload account has no project roles (`gcloud projects get-iam-policy`).

Do not treat an HTTP 200 as proof of mTLS. That rule is unchanged from
[ISTIO.md](ISTIO.md).

## Destroy

Destroy is a separate approval, same as apply. `deletion_protection` defaults
to true, and the node pool's deletion policy is `PREVENT` in that case.

1. Confirm `kubectl config current-context` is the GKE context you mean, not
   `kind-platformforge`. This procedure does not delete the kind cluster.
2. Set `deletion_protection = false` in `terraform.tfvars`.
3. `terraform apply` so the flag is written to state. That apply is itself a
   change and needs approval.
4. `terraform destroy`.
5. The five APIs stay enabled (`disable_on_destroy = false`). They do not
   accrue the cluster fee once the cluster is gone.
6. Local `terraform.tfstate` remains on disk. Remove it only after destroy
   has succeeded and you do not need the record. Deleting the state file
   without destroy leaves the cloud resources in place and untracked.

A GCS backend, if you added one, is not destroyed by this root.

## Known limitations

- One zone. Control plane and nodes fail together if the zone does.
- The kind cluster is Kubernetes 1.37. GKE Regular, as published on
  2026-10-20, creates 1.36. Re-check both the GKE channel and the Istio
  matrix before installing the mesh. Dataplane V2 with Istio 1.31.2 has not
  been run in this repo.
- Intranode visibility is on so same-node pod traffic is visible to the VPC.
  Flow logs are off, so that traffic is not being shipped to Cloud Logging.
- Google Groups for GKE is not configured. There is no Workspace domain in
  this project. Checkov CKV_GCP_65 reports it.
- Binary Authorization is not configured. Images are not signed and are not
  in a registry. Enforcing it would block every pod. Checkov CKV_GCP_66
  reports it.
- Checkov CKV_GCP_69 fails on the cluster resource because the default node
  pool is removed and the cluster has no `node_config`. The explicit node
  pool sets `GKE_METADATA`, and that check passes on the node pool. Putting
  `node_config` on the cluster as well would configure a pool Terraform
  deletes immediately.
- Checkov CKV_GCP_26 fails because subnet flow logs are off. See
  [CLOUD_COSTS.md](CLOUD_COSTS.md).
- Egress through NAT is open. A compromised pod can reach the internet.
- Local Terraform state until a private versioned bucket is approved.
- No admission policy denies `Service` type `LoadBalancer`.
- The overlap check and the zone-in-region check run at plan time, and plan
  was not run.

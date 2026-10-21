# GKE development environment

Terraform for one dedicated GKE development cluster. The configuration is in
[`infra/terraform/`](../infra/terraform/). The cluster `platformforge-dev` in
`us-central1-a` is running at `v1.36.4-gke.1391002`. Istio 1.31.2, the
application Deployments, and the observability charts are installed. Day-to-day
operation and the incident drill are in [GKE_OPERATIONS.md](GKE_OPERATIONS.md)
and [GKE_INCIDENT_DEMO.md](GKE_INCIDENT_DEMO.md). The local kind cluster was
not changed.

`platformforge deploy` still refuses this context. Installs use `kubectl`,
`istioctl`, and Helm with
`--context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.
Module layout and state are in [TERRAFORM.md](TERRAFORM.md). List prices are
in [CLOUD_COSTS.md](CLOUD_COSTS.md).

## What the default stack is

| Choice | Value | Why |
| --- | --- | --- |
| Mode | GKE Standard, not Autopilot | Istio CNI runs a privileged DaemonSet in `istio-system`. Autopilot's policy is a poor fit, and the VM bill is easier to predict |
| Topology | Zonal, `us-central1-a` | One control plane. The GKE free-tier credit can cover that fee. The zone is also a single point of failure |
| Release channel | `REGULAR` | Inspected running version `v1.36.4-gke.1391002` on 2026-10-20. Istio 1.31 supports Kubernetes 1.32–1.37. The kind cluster is 1.37, which is a Rapid minor, not this cluster |
| Nodes | Private, `e2-standard-4`, pool size 1–2 | No external IP. 4 vCPU and 16 GB covers the requests already in this repo on one node. See sizing below |
| Identity | Workload Identity pool `<project>.svc.id.goog` | Node SA writes logs and metrics only. Pods use the GKE Metadata Server and do not inherit the node SA |
| Ingress | None | HTTP load-balancing add-on off. No forwarding rule, Gateway, or external Service IP |
| Datapath | Dataplane V2 | NetworkPolicy without the Calico add-on, which conflicts with this datapath |

The exact Kubernetes patch is chosen by GKE at create time. A readiness pass
later on 2026-10-20 had a credentialed account and project
`platformforge-dev-mn2026`, and `gcloud container get-server-config` still
could not run: `container.googleapis.com` is disabled, and this assessment
did not enable it. The public [GKE release schedule](https://cloud.google.com/kubernetes-engine/docs/release-schedule)
was re-read that day. Regular has had 1.36 since 2026-06-09. Regular
availability for 1.37 is an approximate date of 2026-10-21, so 1.37 is not
the Regular minor on the day of this check. The patch string
`1.36.4-gke.1495002` remains the earlier public release-notes citation. It
was not re-read from the API. Istio 1.31's support matrix, re-read the same
day, is still Kubernetes 1.32–1.37, so a Regular 1.34, 1.35, or 1.36 cluster
is inside that matrix. See the readiness section below.

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

## Readiness assessment (2026-10-20)

A read-only pass checked the account, the project, and this configuration.
Nothing was applied. No API was enabled. No GCP resource was created. No
Terraform state file was written. `terraform plan` was not run. Evidence is
in [TEST_EVIDENCE.md](../TEST_EVIDENCE.md). The authorized address is only in
gitignored `terraform.tfvars`. It is not copied here.

### Verified

- Active account `michaelnguyen3790@gmail.com`, project
  `platformforge-dev-mn2026`, Terraform 1.16.5, provider `hashicorp/google`
  8.7.0, `gcloud` SDK 590.0.0.
- Billing is enabled on an open billing account. A second project on that
  account, `project-60f4c264-06bf-4137-83e`, also has billing enabled.
  `container.googleapis.com` is disabled on both, so no cluster could be
  listed. The GKE free-tier credit is the expected case. See
  [CLOUD_COSTS.md](CLOUD_COSTS.md).
- Application Default Credentials exist, are user credentials, use this
  project as the quota project, and can mint a token. The token was not
  recorded.
- `terraform.tfvars` sets this project, `us-central1`, and `us-central1-a`.
  Its single authorized CIDR is a public IPv4 `/32` and matched this
  machine's egress address at check time.
- The caller has `roles/owner`. Project-level `testIamPermissions` granted
  the service-usage, network, cluster, service-account, and project-IAM
  permissions this root needs. `container.nodePools.create` is not a
  project-level permission and was not in that call. Owner includes node-pool
  create.
- The project has no folder or organization parent. Org policies are not an
  additional gate on this project.
- `logging.googleapis.com` and `monitoring.googleapis.com` are enabled.
  `compute.googleapis.com`, `container.googleapis.com`, and
  `iam.googleapis.com` are not. `gcloud iam service-accounts list` returned
  zero accounts. `gcloud storage buckets list` returned no names.
- `terraform fmt -check -recursive`, `terraform init -backend=false`, and
  `terraform validate` succeeded. Default CIDRs do not overlap, and the zone
  is inside the region. Those two checks were repeated outside Terraform.
  `validate` still does not execute the plan-time preconditions.
- Istio 1.31 supports Kubernetes 1.32–1.37. GKE Regular's available minors
  through 1.36 sit inside that range.

### Assumptions

- "API disabled" is Google's standard `SERVICE_DISABLED` text ("has not been
  used before or it is disabled"). It is strong evidence this project has no
  VPC and no cluster. It is not a returned resource list.
- The $25 monthly budget is the figure given for this assessment. The Cloud
  Billing Budget API is disabled here, so the budget object was not re-read.
  Whatever the amount, a budget alert does not cap spend.
- List prices in [CLOUD_COSTS.md](CLOUD_COSTS.md) were not re-queried from
  the billing catalog.
- While `remove_default_node_pool` is true, GKE still creates a default pool
  of one node and then deletes it. This configuration does not set that
  pool's machine type. Plan on a short-lived extra VM, in addition to the
  `e2-standard-4`, during create.
- The first time Compute Engine is enabled in a project with no organization,
  Google creates the `default` VPC and its default firewall rules, including
  SSH from `0.0.0.0/0`. That network is not where these nodes run. This root
  does not delete it.

### Blockers before any apply

- No approval yet for API enablement, `terraform plan`, or `terraform apply`.
- Regional quotas could not be read while Compute Engine is disabled. Apply
  can still fail on CPU, address, instance, or disk quota.
- `terraform plan` cannot refresh Compute or GKE resources until those APIs
  answer. Running it before that is not a useful review.
- The fixed list-price estimate with the free tier available is about
  $107.51 per month before traffic. A $25 alert will fire. It will not stop
  the charge.
- Workloads are a separate problem. This root does not create Artifact
  Registry. `Makefile` builds `linux/arm64` images. `e2-standard-4` is x86.

### Accepted security tradeoffs

Unchanged from the sections above, and still accepted for this dev stack:
public control-plane endpoint limited to the operator `/32`; NAT egress
without a default-deny rule; flow logs off; no Binary Authorization; no
Google Groups; no admission policy against `Service` type `LoadBalancer`;
local Terraform state; the operator identity is `roles/owner`, which is
broader than the six roles listed below. The default VPC's public SSH rule,
if Google creates it when Compute Engine is enabled, is a side effect to
delete only under a separate approval. It does not attach to these private
nodes.

## Prerequisites

- A GCP project that already exists and already has billing attached. This
  Terraform does not create a project and does not link billing.
  `platformforge-dev-mn2026` exists, is `ACTIVE`, and has billing enabled.
  That was checked on 2026-10-20. Linking billing is what makes a project
  able to incur charges.
- Terraform 1.10 or newer. 1.16.5 was used here. `gcloud` SDK 590.0.0 is
  installed. The active account is logged in. See the readiness section.
- Quota in `us-central1` for two `e2-standard-4` VMs (the upgrade surge),
  not only one, plus one external IP for Cloud NAT, plus a short-lived
  default-pool VM during create. Those quotas have not been read yet.
- The operator's current public IPv4 as a `/32`. The gitignored tfvars value
  matched egress at the readiness check. Re-check it if the address changes
  before apply.

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

`platformforge-dev-mn2026` has no organization or folder parent, so no org
policy is attached to it today. If the project is later moved under an
organization, policies can still reject the apply. Examples that this design
already matches: shielded VM required, no external IP on VMs, no
service-account keys. An org policy that forbids a public control-plane
endpoint will reject the default. Set `enable_private_endpoint = true` only
if you also have a path into the VPC.

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

`terraform plan` is read-only with respect to GCP when the APIs it reads are
already enabled. It was not run. The account and project now exist.
`compute.googleapis.com` and `container.googleapis.com` are still disabled,
so a plan cannot refresh the VPC or the cluster yet. Enable those APIs only
with approval, read quotas, then run plan from `infra/terraform/` with no
`-out` file, and read it before any apply. Do not treat plan approval as
apply approval.

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

## Workload preparation (install completed afterward)

The inspection below is the pre-install record. The install itself is in
[GKE_OPERATIONS.md](GKE_OPERATIONS.md). Images in use are the gitignored lock
at `deploy/gke/image-lock.yaml`.

Inspected on 2026-10-20 with context
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`. The current
context was that name. No object was created. `platformforge deploy` now
refuses every context that does not start with `kind-`, including this one,
so an approved flag cannot land workloads here by mistake.

### What the node can hold

One node, `e2-standard-4`, `linux/amd64`, kubelet `v1.36.4-gke.1391002`,
Ready, no external IP, internal address `10.10.0.3`.

| Measure | Value |
| --- | --- |
| Allocatable CPU | 3920m |
| Allocatable memory | 13591632 Ki (13273 Mi) |
| Allocatable ephemeral storage | 18828602911 bytes |
| Allocatable pods | 64 |
| System pods | 13, all in `kube-system` |
| System requests | 665m CPU, 1169.5 Mi memory |
| Live usage at inspection | 183m CPU, 1174 Mi |
| Prepared workload requests | 730m CPU, 1952 Mi, 12 pods |
| Combined requests | 1395m CPU, about 3122 Mi, 25 pods |

The prepared requests fit on this node with about 2525m CPU and about 10 GiB
of memory still unrequested. That is requests, not a load test. Limits are
higher than requests and can throttle. They do not by themselves force a
second node.

The prepared workload is inventory v0.1.0 (2), inventory v0.2.0 (1), orders
(2), one sidecar request per application pod, istiod, the CNI agent,
Prometheus, Grafana, Loki, Tempo, and Alloy including its reloader. The
incident manifest under `services/manifests/incidents/` is not in that sum.

No PersistentVolume or claim exists. The default StorageClass is
`standard-rwo` (`pd.csi.storage.gke.io`). A later PVC would create a billable
disk. The GKE Helm overlays keep persistence off.

### Compatibility

| Piece | Finding |
| --- | --- |
| Kubernetes 1.36.4 and Istio 1.31.2 | Inside Istio's supported range, 1.32–1.37. Native sidecars stay at the Istio 1.31 default. They are not turned off |
| Istio CNI on Dataplane V2 | The dataplane is Cilium `anetd` `v1.19.4-gke.85`. Its host paths are `/home/kubernetes/bin` and `/etc/cni/net.d`. `deploy/gke/istio-operator.yaml` chains Istio's CNI there and does not replace Cilium. An Antrea controller pod is also running; it is not the pod dataplane |
| CNI priority | GKE will not schedule `system-node-critical` in `istio-system` without `deploy/gke/istio-cni-resourcequota.yaml`. The DaemonSet stays out of `kube-system` |
| PeerAuthentication, AuthorizationPolicy, VirtualService, DestinationRule | Unchanged. STRICT stays namespace-scoped. The allowed principal stays `cluster.local/ns/platformforge-dev/sa/orders-service`. The trust domain is not switched to the GKE project |
| Port 15017 | Already allowed from the control-plane `/28` by the Terraform firewall. Do not edit that rule to install Istio |
| Images | The node is amd64. `make images` still builds `linux/arm64` for kind. `make images-gke` builds `linux/amd64` and does not push. The pinned base-image indexes include `linux/amd64` |

`deploy/istio/istio-operator.yaml` is still the kind file. Do not install it
on GKE.

### Deployment sequence

Nothing in this list has been run except the read-only preflight.

1. `make gke-preflight`. It refuses a kind context and any context other than the pinned GKE name. It only runs `kubectl get` and `kubectl version`.
2. Approve enabling `artifactregistry.googleapis.com`.
3. Approve creating Docker repository `platformforge` in `us-central1`.
4. Approve `roles/artifactregistry.reader` on `platformforge-dev-nodes@platformforge-dev-mn2026.iam.gserviceaccount.com` only.
5. On the operator machine, `gcloud auth configure-docker us-central1-docker.pkg.dev`. Do not commit the Docker config.
6. `make images-gke`, then `make images-gke VERSION=0.1.0` so inventory `0.1.0` exists. Confirm each local image is `linux/amd64`.
7. Push only those three tags. `make images-gke-publish` prints the approvals and exits 2. It does not push.
8. Read each digest back from Artifact Registry. Write `deploy/gke/image-lock.yaml` (gitignored) with those digests. `python -m platformforge.gke check-lock deploy/gke/image-lock.yaml`.
9. `python -m platformforge.gke render --lock deploy/gke/image-lock.yaml services/manifests/inventory-service.yaml services/manifests/inventory-service-v0.2.0.yaml services/manifests/orders-service.yaml`. Review the stdout. Do not pipe it to `kubectl apply`.
10. After a separate approval, and only with `--context` set to the pinned GKE context: create `istio-system` from `deploy/istio/istio-system-namespace.yaml`, apply `deploy/gke/istio-cni-resourcequota.yaml`, then `istioctl install -f deploy/gke/istio-operator.yaml`. Do not create a cluster-admin binding.
11. Apply `deploy/observability/namespace.yaml`, then each chart with its kind values file and the matching file under `deploy/gke/observability/`. Apply `deploy/observability/tempo-networkpolicy.yaml` and `deploy/observability/prometheus-rbac.yaml`. No disk and no public Service.
12. Apply `deploy/istio/platformforge-dev/` only after the mesh is up, still namespace-scoped. Create `platformforge-dev` through the existing renderer (restricted Pod Security, injection on). Apply `deploy/gke/workload-identity-serviceaccount.yaml` only if that identity is actually needed. Do not put the Google annotation on `orders-service` or `inventory-service`.
13. Deploy the three rendered manifests. The CLI will refuse this context until that guard is deliberately changed.

### Verification commands

```bash
kubectl config current-context
make gke-preflight
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev get nodes -o wide
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev get svc -A
python -m platformforge.gke publish-requirements
```

`get nodes` must show `EXTERNAL-IP` as `<none>` and `amd64`. `get svc` must
show only `ClusterIP`. `publish-requirements` exits 2.

### Cleanup

This preparation created no cluster objects. Kind cleanup is unchanged and
must use `--context kind-platformforge`. GKE cluster deletion remains the
Terraform destroy procedure, which `deletion_protection` blocks until a
separate approval. Do not delete the kind cluster as part of GKE cleanup.

## Known limitations

- One zone. Control plane and nodes fail together if the zone does.
- The kind cluster is Kubernetes 1.37. On 2026-10-20 the GKE release
  schedule shows 1.36 available on Regular, and 1.37 not yet available
  there. The exact create patch was not read from the API. Re-check both
  the GKE channel and the Istio matrix before installing the mesh.
  Dataplane V2 with Istio 1.31.2 has not been run in this repo.
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

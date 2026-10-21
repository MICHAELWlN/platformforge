# PlatformForge

PlatformForge is an internal developer platform portfolio project. A developer
describes a service in a small YAML manifest. The CLI validates it and renders
a hardened Kubernetes Deployment, ServiceAccount, and ClusterIP Service in one
allowlisted namespace. Those services are running on a private zonal Google
Kubernetes Engine cluster with Istio, a canary controller, and a read-only
investigation API.

This is a single-node development cluster. It is a working demonstration, and
it is not a production platform. Current scope is in
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Read this in 60 seconds

- **Real GKE deployment.** Cluster `platformforge-dev` in `us-central1-a`
  (`v1.36.4-gke.1391002`): one private `e2-standard-4` node, Cloud NAT, and
  Workload Identity. Workload images are immutable Artifact Registry digests.
- **Automated rollback.** The canary controller compares stable and canary
  Prometheus samples and writes a reviewed 100/0 VirtualService when the
  canary error rate crosses its threshold.
- **Least-privilege Kubernetes RBAC.** The controller may get two named
  Deployments and get or update one named VirtualService. The MCP reader may
  read pods, logs, events, and Deployments in `platformforge-dev`, and may
  get only `virtualservices/inventory-service`.
- **Workload Identity Federation.** GitHub Actions authenticates to Google
  with OIDC. CI has no service-account JSON key.
- **GitHub Actions stops before apply.** The delivery workflow is manual and
  review-gated. It can publish a digest. It then refuses to apply manifests.
- **Observability.** Prometheus, Grafana, Loki, Tempo, and Alloy run as
  ClusterIP services. Application traces go to Tempo. Pod logs go to Loki.
- **AI-assisted investigation.** A stdio MCP server reads Kubernetes,
  Prometheus, Loki, and Tempo. `verified_root_cause` stays null.
- **Reproducible experiments.** Kind has a five-experiment controller loop.
  GKE has the recorded faulty-canary drill below.

Inventory routing on the cluster is 100 percent `v0.1.0` and 0 percent
`v0.2.0`. A Cursor session has called the MCP server against GKE, including
the rejection checks. `get_routing` read weights 100 and 0.

## Demo video

> **Final demo URL:** `TODO_FINAL_DEMO_VIDEO_URL`

Replace `TODO_FINAL_DEMO_VIDEO_URL` with the published recording. The shot
list is [docs/FINAL_DEMO.md](docs/FINAL_DEMO.md). The spoken script is
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

## Measured GKE failure

On 2026-10-21 a faulty inventory canary (`INVENTORY_FAILURE_PERCENT=100`)
took a share of live orders traffic on the pinned GKE cluster. The client
sent 400 requests:

| Result | Count |
| --- | --- |
| HTTP 200 | 356 |
| HTTP 502 | 44 |
| Total | 400 |

The controller's own sample at the decision was 26 canary errors out of 26
canary requests, an error ratio of 100 percent. It recorded action
`rollback`. The resulting controller state was `stable`. The cumulative
rollback counter increased from 1 to 2.

That decision JSON includes `episode_age` of 30.229 seconds. The field is
the controller's episode clock. Client recovery time was not measured from
it. The 26-request sample is the controller's scraped delta at that poll.
It is a separate count from the 44 client 502s.

`verified_root_cause` remains null. The tools can show which version
returned the errors and that a rollback was logged. They do not certify a
cause.

An earlier drill on the same cluster used a different sample: 363 HTTP 200
and 37 HTTP 502 at the client, a controller sample of 16/16, and a rollback
counter moving from 0 to 1. That run is written up in
[docs/GKE_CANARY_EXPERIMENT.md](docs/GKE_CANARY_EXPERIMENT.md).

## Architecture

```text
ServiceManifest YAML
        |
        v
platformforge CLI  ---- kind only ---->  Deployment, ServiceAccount, ClusterIP
        |
        v
namespace platformforge-dev  (restricted Pod Security, Istio sidecar)
   orders-service  --mTLS-->  inventory-service
                              stable v0.1.0  |  canary v0.2.0
                              reviewed VirtualService weights
   canary-controller  --reads Prometheus-->  writes stable or promote route

namespace platformforge-observability  (no sidecar, ClusterIP, emptyDir)
   Prometheus   Grafana   Loki   Tempo   Alloy

laptop
   stdio MCP server
        reader kubeconfig (short-lived token) + loopback telemetry ports
```

| Piece | Where |
| --- | --- |
| CLI, schema, deployer, canary controller, MCP server | `src/platformforge/` |
| `inventory-service` and `orders-service` | `services/` |
| Reviewed mesh policy | `deploy/istio/platformforge-dev/` |
| Prometheus, Grafana, Loki, Tempo, Alloy | `deploy/observability/` |
| GKE controller and MCP reader | `deploy/gke/` |
| Kind controller and MCP reader | `deploy/kind/` |
| VPC, NAT, zonal GKE, IAM, GitHub OIDC | `infra/terraform/` |
| CI and manual delivery | `.github/workflows/` |

The component narrative is [ARCHITECTURE.md](ARCHITECTURE.md).

## GKE infrastructure

Terraform in `infra/terraform/` describes one zonal Standard cluster in a
custom VPC: private nodes, a public control plane limited to named CIDRs,
Workload Identity, a least-privilege node service account, and Cloud NAT.
No load balancer is declared. Application Services stay ClusterIP.

What is running:

| Item | Value |
| --- | --- |
| Project / cluster / zone | `platformforge-dev-mn2026` / `platformforge-dev` / `us-central1-a` |
| Version | `v1.36.4-gke.1391002`, one `e2-standard-4`, `linux/amd64` |
| Registry | `us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge` |
| Istio | 1.31.2, sidecar mode, CNI, no ingress or egress gateway |
| Controller image | `canary-controller@sha256:b7e8af0a35fd1d968ee3aacf701701e15da8ff600d6360662885f6c5846cc6a2` |

`platformforge deploy` refuses any kube context that does not start with
`kind-`. GKE installs were `kubectl`, `istioctl`, and Helm against the pinned
context
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`. Real image
digests for the app images live in gitignored `deploy/gke/image-lock.yaml`.
Layout and operations: [docs/TERRAFORM.md](docs/TERRAFORM.md),
[docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md),
[docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md).

## GitHub Actions

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main`.
It lints, runs unit tests, validates Terraform, checks manifests, scans for
secrets, runs `pip-audit`, and builds both `linux/amd64` images with a
non-root smoke run and a Trivy scan. It has no cloud token and no cluster.

`.github/workflows/delivery.yml` is `workflow_dispatch` only. Environment
`gke-delivery` requires a reviewer and allows only `main`. The publish job
uses Workload Identity Federation to push a digest to the existing Artifact
Registry repository. The next job checks the pinned project, cluster, and
context, then runs `python -m platformforge.delivery refuse-apply`. One
approved `0.2.0` publish succeeded. That publish did not roll the cluster.
A later dispatch still needs the reviewer. See [docs/CI_CD.md](docs/CI_CD.md).

## Istio service mesh

In `platformforge-dev`:

- **STRICT mTLS.** Sidecar-to-sidecar traffic is TLSv1.3. SPIFFE identity is
  `spiffe://cluster.local/ns/platformforge-dev/sa/<service>`. Plaintext from
  a pod outside the mesh is rejected.
- **Authorization.** `inventory-service` accepts the `orders-service`
  identity, and only `GET /inventory/*` on port 8080.
- **Canary subsets.** DestinationRule subsets `v0-1-0` and `v0-2-0`. Reviewed
  VirtualServices set 100/0 (stable), 90/10 (canary), or 0/100 (promote).

`serviceAccountName` must equal the manifest name, because that name is the
SPIFFE identity. PlatformForge does not render Istio objects. Mesh files are
reviewed YAML under `deploy/istio/platformforge-dev/`. Install and checks:
[docs/ISTIO.md](docs/ISTIO.md).

## Canary controller

`platformforge.canary_controller` polls every 15 seconds. On GKE the
Deployment is digest-pinned, runs as UID 65532, and is not injected with a
sidecar. Prometheus job `platformforge-canary` scrapes its ClusterIP.

The process does not select the 90/10 file. An operator applies that file to
start a shift. The first successful read of 90/10 stores a Prometheus
baseline and holds. Later polls use the increase since that baseline.

On the running GKE Deployment the gates are:

| Condition | Route write |
| --- | --- |
| Weights already 100/0 | None. The episode resets |
| Canary error ratio above 0.05 | Reviewed stable file, 100/0 |
| p95 regression above 5 and a gap above 200 ms | Reviewed stable file |
| Missing telemetry through the 120 second window | Reviewed stable file |
| At least 10 canary requests inside the thresholds | Reviewed promote file, 0/100 |

GKE also requires `PLATFORMFORGE_CANARY_GKE=1` and the pod service-account
token. Setting the kind context variable on GKE does not authorize a write.
The controller cannot delete `inventory-service-v0-1-0`. Kind experiments:
[docs/CANARY_CONTROLLER.md](docs/CANARY_CONTROLLER.md). Decision rules:
[docs/AUTOMATED_CANARY.md](docs/AUTOMATED_CANARY.md) and
[docs/AUTOMATED_ROLLBACK.md](docs/AUTOMATED_ROLLBACK.md).

## Prometheus, Grafana, Loki, and Tempo

The telemetry stack lives in `platformforge-observability` with
`istio-injection=disabled`. Prometheus scrapes Istio sidecar metrics on port
15020 and the controller's `/metrics`. Alloy tails `platformforge-dev` pod
logs into Loki. Both FastAPI services export OTLP traces to Tempo when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. A failed export does not fail the
request. Spans do not record bodies, headers, or credentials.

Grafana is provisioned with those datasources and the dashboard
`PlatformForge services` (`platformforge-overview`). On GKE, Grafana
`/api/health` returned database ok for version 13.2.4, and the three
datasource health checks returned HTTP 200. The browser UI was not used for
that check, and the admin password is the chart-generated Secret.

Disks are emptyDir. There is no PersistentVolume. Nothing in this stack is
NodePort, LoadBalancer, or Ingress. Port-forward to `127.0.0.1` is a local
debug step. Details: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) and
[docs/TRACING.md](docs/TRACING.md).

## Read-only MCP investigation

`python -m platformforge.mcp_server` is a stdio server. It has no HTTP
transport. Live reads use a kubeconfig whose user is
`platformforge-mcp-reader`. Kind and GKE each have their own token. A GKE
call without `PLATFORMFORGE_MCP_KUBECONFIG` is rejected. Telemetry URLs are
loopback HTTP only. The server does not open port-forwards.

Logs and metrics are untrusted data. The server does not execute
instructions found in them. `investigate_incident` keeps `verified_root_cause`
null. `get_routing` is a get of one named VirtualService and reports
`writable: false`.

Tool behavior, redaction, and the live checks:
[docs/MCP_SERVER.md](docs/MCP_SERVER.md),
[docs/MCP_SECURITY.md](docs/MCP_SECURITY.md),
[docs/MCP_INCIDENT_INVESTIGATION.md](docs/MCP_INCIDENT_INVESTIGATION.md),
[docs/MCP_GKE_INTEGRATION.md](docs/MCP_GKE_INTEGRATION.md).

## Security controls

- Manifests reject unknown fields, credential-like environment names,
  `:latest`, and any Service that is not ClusterIP.
- The namespace allowlist is code: `platformforge-dev` only.
- Rendered pods are non-root, drop all capabilities, use a read-only root
  filesystem, and set `seccompProfile: RuntimeDefault`.
- The namespace enforces the restricted Pod Security profile.
- Istio STRICT and the inventory AuthorizationPolicy stay in place for
  telemetry. The observability namespace is not injected.
- Controller and MCP identities are namespace Roles with named-resource
  limits on the VirtualService. No ClusterRoleBinding is attached to either
  ServiceAccount.
- GitHub delivery uses Workload Identity Federation bound to `main`,
  repository `MICHAELWlN/platformforge`, and environment `gke-delivery`.
- CI runs `python -m platformforge.secretscan`. Built images are scanned with Trivy.
- Terraform state, tfvars, plan files, kubeconfigs, and the image lock are
  gitignored. The provider block has no key file.

## Testing and validation

```bash
make lint          # ruff check + format check
make test          # unit tests; kind and GKE tests stay skipped
make terraform-validate
python -m platformforge.secretscan
```

`make integration` is opt-in and runs only when the current context is
`kind-platformforge`. `make mcp-gke` and `pytest -m gke` are the pinned GKE
context only. Recorded commands are in [TEST_EVIDENCE.md](TEST_EVIDENCE.md).

## Known limitations

- `platformforge deploy` refuses the GKE context. Re-deploys there are
  `kubectl apply` of rendered YAML on the pinned context.
- The delivery workflow refuses apply. Publishing a digest does not roll a
  Deployment.
- The controller does not start the 90/10 shift. Promotion of the GKE canary
  to 100 percent has not been done.
- `verified_root_cause` stays null on every investigation.
- Controller counters reset when the pod is replaced.
- The GKE latency drill is not measured. The running inventory digest does
  not set `INVENTORY_DELAY_MS`.
- Telemetry uses emptyDir. A Prometheus restart drops the in-memory TSDB.
- One zonal node. A preemption or a disruptive upgrade takes the mesh with it.
- The MCP operator still mints the reader token and opens the port-forwards.
- No license file is in the repository yet.

## Demo

The four-minute path does not inject a failure. With the kube context already
pinned to the GKE cluster:

```bash
make demo-preflight
make demo-reader
make demo-connect
make demo-controller
```

`make demo-connect` keeps Prometheus, Loki, Tempo, and Grafana on loopback
and restarts a forward that dies. Grafana is `http://127.0.0.1:3000`,
dashboard `PlatformForge services`. Sign in locally. Do not put the admin
password in a recording. Stop the forwards with `make demo-connect-stop`.

A live failure changes the canary and the route. It needs a fresh approval
and the `CONFIRM_*` switches documented in
[docs/FINAL_DEMO.md](docs/FINAL_DEMO.md). Recovery notes:
[docs/DEMO_TROUBLESHOOTING.md](docs/DEMO_TROUBLESHOOTING.md). The recording
checklist is [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md).

## Cleanup and cost

The cluster is running and it bills while it exists. [docs/CLOUD_COSTS.md](docs/CLOUD_COSTS.md)
is a list-price estimate from 2026-10-20, not an invoice: about **$108 per
month** for one on-demand `e2-standard-4`, a 50 GB boot disk, and Cloud NAT
when the GKE management free tier covers this account, or about **$181 per
month** when it does not. That page does not include Artifact Registry
storage. A budget alert does not stop spend. There is no scale-to-zero
schedule.

`terraform destroy`, deleting the namespace, and deleting the kind cluster
are manual and need an explicit approval. This repository does not run them.
Kind cleanup, when you intend it, is the namespace delete in
[docs/ISTIO.md](docs/ISTIO.md). GKE teardown is
[docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md).

## Reference

Operator detail for the CLI, the manifest, and local kind. The portfolio
summary is above.

## Quick start

Requirements: Python 3.11+ (developed on 3.13), and for cluster commands a kubeconfig context
(developed against kind v0.33 / Kubernetes v1.37).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate

platformforge validate examples/demo-web.yaml
platformforge deploy examples/demo-web.yaml --dry-run          # render only, no cluster access
platformforge deploy examples/demo-web.yaml --create-namespace  # prompts to approve kube context
platformforge status examples/demo-web.yaml
platformforge logs   examples/demo-web.yaml --tail 50
```

Every command that talks to a cluster first prints the current kube context (name, cluster,
API server URL, user, kubeconfig path) and asks for confirmation. For scripts, pass
`--approve-context <name>`; it must exactly match the current context or the command aborts.

## Commands

| Command | Cluster access | Purpose |
| --- | --- | --- |
| `validate MANIFEST` | none | Schema + policy validation |
| `deploy MANIFEST` | read/write, one namespace | Create or update Deployment + ClusterIP Service, wait for rollout |
| `status MANIFEST` | read-only | Deployment replicas/conditions, Service, Pods, health |
| `logs MANIFEST` | read-only | Last `--tail` lines (1–5000) from each pod |

`deploy` options: `--create-namespace`, `--wait/--no-wait` (default wait), `--timeout 10..900`
(default 120 s), `--dry-run`, `--approve-context NAME`.

Exit codes: `0` success, `1` invalid manifest, `2` CLI usage error, `3` kube context missing or
not approved, `4` Kubernetes API / ownership / not-deployed error, `5` rollout timeout.

## Service manifest

```yaml
apiVersion: platformforge.dev/v1alpha1
kind: ServiceManifest
metadata:
  name: demo-web                 # DNS-1035 label, <= 63 chars
  namespace: platformforge-dev   # must be in the code-level allowlist
spec:
  image: nginxinc/nginx-unprivileged:1.29-alpine   # explicit tag or @sha256 digest; no :latest
  replicas: 2                    # 1..5
  port: 8080                     # 1024..65535 (non-root)
  env:                           # plain values only; credential-like names are rejected
    - name: LOG_LEVEL
      value: info
  resources:                     # required; requests <= limits; limits <= 2 CPU / 2Gi
    requests: {cpu: 50m, memory: 32Mi}
    limits: {cpu: 200m, memory: 64Mi}
  probes:                        # both required; HTTP GET on the service port
    readiness: {path: /, initialDelaySeconds: 2, periodSeconds: 5}
    liveness: {path: /, initialDelaySeconds: 10, periodSeconds: 10}
  # Optional (Milestone 3):
  version: v0.1.0                # vMAJOR.MINOR.PATCH, each 0..999; versions the Deployment
  serviceAccountName: demo-web   # must equal metadata.name
  termination:
    gracePeriodSeconds: 30       # 1..300
    preStopSleepSeconds: 5       # 0..60, must be < gracePeriodSeconds
```

Unknown fields anywhere are rejected, so raw Kubernetes objects (or fields such as
`hostNetwork`, `securityContext`, `valueFrom`, `type: LoadBalancer`) cannot be smuggled in.

- **`version`** names the Deployment `<name>-<version with dots as dashes>` (for example,
  `inventory-service-v0-1-0`) and labels its pods `app.kubernetes.io/version`. The Service
  still selects on name only, so it spans every version; Istio subsets pick versions.
  `metadata.name` plus the version suffix must fit in 63 characters.
- **`serviceAccountName`** makes PlatformForge create (or update, if it owns it) a
  ServiceAccount with token automount disabled and no RBAC bindings, and run the pods under
  it. The name is fixed to `metadata.name`, so a manifest cannot borrow another workload's
  mesh identity.
- **`termination`** sets `terminationGracePeriodSeconds` and an optional native
  `preStop: sleep` (no shell command).

## What gets deployed

- **Namespace** (only with `--create-namespace`): labeled
  `pod-security.kubernetes.io/enforce=restricted` and `istio-injection=enabled`.
- **ServiceAccount** (only with `serviceAccountName`): `automountServiceAccountToken: false`.
- **Deployment**: rolling update (`maxSurge: 1`, `maxUnavailable: 0`), readiness + liveness
  probes, requests/limits, `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all capabilities dropped,
  no service-account token automount, `/tmp` as a 64Mi `emptyDir`.
- **Service**: `ClusterIP` only. PlatformForge never renders NodePort, LoadBalancer, Ingress, or
  external IPs.

All objects carry `app.kubernetes.io/managed-by=platformforge`. PlatformForge refuses to
update (or report on) an existing Deployment/Service/ServiceAccount of the same name that
lacks this label.

If adding `version` renames a Deployment, `deploy` warns about the old Deployment whose pods
the Service still selects. PlatformForge never deletes it; removing it is a manual step,
once the new Deployment is serving.

## Microservices

Two FastAPI services live under `services/` and are deployed with PlatformForge itself.

| Service | Endpoints |
| --- | --- |
| `inventory-service` | `GET /healthz`, `GET /readyz`, `GET /version`, `GET /inventory/{item_id}` |
| `orders-service` | `GET /healthz`, `GET /readyz`, `GET /version`, `GET /orders/check/{item_id}` |

`orders-service` calls `inventory-service` at
`http://inventory-service.platformforge-dev.svc.cluster.local:8080` (ClusterIP Service DNS).
Demo catalog items: `widget` (42), `gadget` (0), `gizmo` (7).

| `orders-service` response | When |
| --- | --- |
| `200 {"orderable": ...}` | inventory responded with the item |
| `404 item_not_found` | inventory returned 404 |
| `422` | `item_id` fails `^[a-z0-9][a-z0-9-]{0,63}$` (inventory is not called) |
| `502 inventory_forbidden` | inventory (or its sidecar) returned 403, e.g. an Istio `RBAC: access denied` |
| `502 inventory_bad_status` / `inventory_bad_response` | other unexpected status (e.g. 500) or malformed body |
| `503 inventory_unavailable` | connection refused / reset / DNS failure, or upstream 503 (Envoy `no_healthy_upstream`) |
| `504 inventory_timeout` | no response within `INVENTORY_TIMEOUT_SECONDS`, or upstream 504 |

Error bodies include the `request_id`. Every response carries an `X-Request-ID` header: a
well-formed inbound ID (`[A-Za-z0-9._-]{1,128}`) is reused, otherwise one is generated. The
ID is forwarded to `inventory-service` and appears in both services' JSON logs.

Configuration (environment variables, set in the PlatformForge manifest):

| Variable | Service | Default | Bounds |
| --- | --- | --- | --- |
| `INVENTORY_URL` | orders | `http://inventory-service:8080` | absolute http(s); no credentials, query, or fragment |
| `INVENTORY_TIMEOUT_SECONDS` | orders | `2` | 0.1–30 |
| `INVENTORY_FAILURE_PERCENT` | inventory | `0` | 0–100; share of `GET /inventory/*` answered with 500 (failure drills only; health endpoints unaffected) |
| `LOG_LEVEL` | both | `info` | debug, info, warning, error |
| `PORT` | both | `8080` | 1024–65535 |

### Build, load, deploy (local kind)

```bash
make images kind-load                  # 0.2.0 of both services (docker build linux/arm64 + kind load)
make images kind-load VERSION=0.1.0    # inventory 0.1.0, the stable version (also builds an unused orders 0.1.0)
platformforge deploy services/manifests/inventory-service.yaml          # Deployment inventory-service-v0-1-0
platformforge deploy services/manifests/inventory-service-v0.2.0.yaml   # canary Deployment inventory-service-v0-2-0
platformforge deploy services/manifests/orders-service.yaml
platformforge status services/manifests/orders-service.yaml
```

The `inventory-service:0.1.0` image is built from the current source. It includes the
failure knob (off by default) and the OpenTelemetry exporter. Rebuild and reload it
after tracing changes; the stable Deployment serves 100% of inventory traffic.

## Service mesh (Istio, local kind)

Istio 1.31.2 (the supported release for Kubernetes 1.37) runs in sidecar mode with the Istio
CNI plugin, so `platformforge-dev` keeps the `restricted` Pod Security level. In
`platformforge-dev`:

- **STRICT mTLS**: plaintext from a non-mesh pod is reset. Sidecar-to-sidecar traffic is
  TLSv1.3 with SPIFFE identities `spiffe://cluster.local/ns/platformforge-dev/sa/<service>`.
- **Authorization**: `inventory-service` accepts only the `orders-service` identity, and only
  for `GET /inventory/*` on port 8080. Other identities get `403 RBAC: access denied`.
- **Canary**: `inventory-service` v0.1.0 and v0.2.0 run side by side. A DestinationRule
  defines subsets, and one of two reviewed VirtualServices sets the weights (100/0 stable,
  90/10 canary).

Install commands, configuration files, and verification: [docs/ISTIO.md](docs/ISTIO.md).
Release and rollback procedure: [docs/CANARY_RELEASE.md](docs/CANARY_RELEASE.md). Failure
drill: [docs/INCIDENT_RUNBOOK.md](docs/INCIDENT_RUNBOOK.md).

## Observability (local kind)

Prometheus, Grafana, Loki, Tempo, and Alloy run in `platformforge-observability`
(ClusterIP only, no Istio sidecar). Prometheus scrapes Istio sidecar metrics on port
15020. Both FastAPI services export OTLP/HTTP traces to Tempo and write `trace_id` and
`span_id` next to the request ID. Alloy tails `platformforge-dev` pod logs into Loki.
Grafana is provisioned with those three datasources and a service dashboard.

Install, versions, and the emptyDir caveat: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
Trace shape: [docs/TRACING.md](docs/TRACING.md). Investigation and cleanup:
[docs/OBSERVABILITY_RUNBOOK.md](docs/OBSERVABILITY_RUNBOOK.md).

Istio objects are applied with `kubectl` from `deploy/istio/`. PlatformForge manifests cannot
contain them.

Under STRICT mTLS, `kubectl port-forward` to a pod still works (it enters through the pod's
loopback). `kubectl proxy` / API-server service proxy URLs do not, because the API server
is a plaintext client.

Images: multi-stage `services/Dockerfile`, build stage `python:3.13-slim-trixie`, runtime
`gcr.io/distroless/python3-debian13:nonroot` (no shell, no package manager, UID 65532), both
pinned by digest. Runtime dependencies are exactly pinned in `services/requirements.txt`.

## GCP development environment

`infra/terraform/` is a modular Terraform root for one zonal GKE Standard cluster in a
dedicated VPC: private worker nodes, a public control-plane endpoint limited to named
CIDRs, Workload Identity, a least-privilege node service account, and Cloud NAT so those
nodes can pull images. The default location is `us-central1-a`. No load balancer is
declared. Application Services stay ClusterIP; that rule is unchanged.

The cluster `platformforge-dev` (`v1.36.4-gke.1391002`) is running with Istio 1.31.2,
STRICT mTLS, the 100/0 stable inventory route, and Prometheus, Grafana, Loki, Tempo, and
Alloy. Images are `linux/amd64` digests in Artifact Registry
`us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge`. The real digests are
in gitignored `deploy/gke/image-lock.yaml`. `make images` still builds `linux/arm64` for
kind. `platformforge deploy` refuses any context that does not start with `kind-`; the
GKE install used `kubectl`, `istioctl`, and Helm with an explicit context. Operations,
the incident drill, and cleanup are in [docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md)
and [docs/GKE_INCIDENT_DEMO.md](docs/GKE_INCIDENT_DEMO.md). Prices are in
[docs/CLOUD_COSTS.md](docs/CLOUD_COSTS.md). Terraform layout is in
[docs/TERRAFORM.md](docs/TERRAFORM.md).

## Testing

```bash
make lint                                # ruff check + format check (src, services, tests)
make test                                # unit tests; integration tests are skipped

# Integration tests: kind contexts only, current context must match, nothing is deleted.
# Service and mesh tests need both image builds above and Istio installed (docs/ISTIO.md).
make integration                         # = PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
```

- `tests/integration/test_services_kind.py::test_08` simulates an outage by scaling every
  inventory version to 0 replicas, then restores them.
- `tests/integration/test_services_mesh.py` verifies Istio Phases A–F in order. It applies only
  the reviewed files under `deploy/istio/platformforge-dev/`, runs the canary and failure
  drill, and leaves the stable configuration (100% v0.1.0) in place.
- `tests/integration/test_observability.py` checks Prometheus scrapes, a real metric
  increase, Loki, Tempo, trace parentage, Grafana datasource health, and the canary
  drill through Prometheus. It restores stable routing and the healthy canary.
- The tests create two client pods in `platformforge-dev`, `pf-test-client-mesh` (sidecar,
  `default` identity) and `pf-test-client-plain` (no sidecar), and reuse them. The tests
  never delete them.

CI (`.github/workflows/ci.yml`) runs Ruff, unit tests, Terraform validation,
manifest checks, a secret scan, `pip-audit`, and both `linux/amd64` image
builds with a read-only smoke run and a Trivy scan. It uses no secrets and no
cluster.
`.github/workflows/delivery.yml` is manual. Environment `gke-delivery`
requires a review and allows only `main`. One approved `0.2.0` publish
succeeded. The workflow still refuses to apply to GKE. See
[docs/CI_CD.md](docs/CI_CD.md).

Recorded results are in [TEST_EVIDENCE.md](TEST_EVIDENCE.md).

## Cleanup (manual, destructive)

Integration tests intentionally leave `platformforge-dev` in place. To remove everything
PlatformForge created on the local kind cluster, review and run:

```bash
kubectl --context kind-platformforge delete namespace platformforge-dev
```

Removing Istio itself is described in [docs/ISTIO.md](docs/ISTIO.md#cleanup-manual-destructive-requires-explicit-approval).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, trust boundaries
- [docs/ISTIO.md](docs/ISTIO.md): mesh installation, configuration, verification, cleanup
- [docs/CANARY_RELEASE.md](docs/CANARY_RELEASE.md): canary procedure and measured split
- [docs/INCIDENT_RUNBOOK.md](docs/INCIDENT_RUNBOOK.md): canary failure drill and remediation
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md): telemetry install, versions, resources
- [docs/TRACING.md](docs/TRACING.md): W3C propagation and what spans record
- [docs/OBSERVABILITY_RUNBOOK.md](docs/OBSERVABILITY_RUNBOOK.md): drill investigation and cleanup
- [docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md): GKE design, access, apply, and destroy
- [docs/CI_CD.md](docs/CI_CD.md): CI jobs, GitHub OIDC, and the gated publish workflow
- [docs/MCP_SERVER.md](docs/MCP_SERVER.md): read-only local MCP server and its tools
- [docs/MCP_SECURITY.md](docs/MCP_SECURITY.md): allowlists, redaction, and auth assumptions
- [docs/MCP_INCIDENT_INVESTIGATION.md](docs/MCP_INCIDENT_INVESTIGATION.md): how to read an investigation report
- [docs/MCP_AI_DEMO.md](docs/MCP_AI_DEMO.md): 45–60 second stdio investigation demo
- [docs/FINAL_DEMO.md](docs/FINAL_DEMO.md): four-minute recording procedure
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md): spoken script
- [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md): recording checklist
- [docs/DEMO_TROUBLESHOOTING.md](docs/DEMO_TROUBLESHOOTING.md): demo recovery without new cloud resources
- [docs/MCP_LIVE_INTEGRATION.md](docs/MCP_LIVE_INTEGRATION.md): kind reader identity and live tool results
- [docs/MCP_GKE_INTEGRATION.md](docs/MCP_GKE_INTEGRATION.md): GKE reader identity and live tool results
- [docs/PROGRESSIVE_DELIVERY.md](docs/PROGRESSIVE_DELIVERY.md): canary decision rules
- [docs/AUTOMATED_CANARY.md](docs/AUTOMATED_CANARY.md): Prometheus comparison of stable and canary
- [docs/AUTOMATED_ROLLBACK.md](docs/AUTOMATED_ROLLBACK.md): kind route restore and state machine
- [docs/CANARY_EXPERIMENT.md](docs/CANARY_EXPERIMENT.md): kind failure drill, timings, and metrics
- [docs/CANARY_CONTROLLER.md](docs/CANARY_CONTROLLER.md): kind reconciliation loop, scrape, and five experiments
- [docs/GKE_CANARY_EXPERIMENT.md](docs/GKE_CANARY_EXPERIMENT.md): measured GKE rollback of a faulty inventory canary
- [docs/ROLLBACK_RUNBOOK.md](docs/ROLLBACK_RUNBOOK.md): restore the last known-good route
- [docs/TERRAFORM.md](docs/TERRAFORM.md): Terraform layout, state, and commands
- [docs/CLOUD_COSTS.md](docs/CLOUD_COSTS.md): list-price estimate for the default stack
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md): what is and is not implemented
- [TEST_EVIDENCE.md](TEST_EVIDENCE.md): commands run and their results
- [AGENTS.md](AGENTS.md): rules for AI coding agents working in this repository

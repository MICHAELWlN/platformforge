# PlatformForge

PlatformForge is a small internal developer platform. A service is described
in a validated YAML manifest. The CLI renders a hardened Kubernetes
Deployment, ServiceAccount, and ClusterIP Service into one allowlisted
namespace. The same two services, Istio, a canary controller, and a read-only
investigation API were verified on a private zonal GKE cluster on
2026-10-21.

It is a single-node lab, not a production platform. Leaving the cluster up
creates cloud charges until it is destroyed.

## Demo video

> **Demo video:** `TODO_FINAL_DEMO_VIDEO_URL`

The recording is not published yet. Replace that token with the URL. The
shot list is [docs/FINAL_DEMO.md](docs/FINAL_DEMO.md). The spoken script is
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md). There are no screenshots in the
repository. The Grafana UI was not captured; datasource health was checked
through the HTTP API.

## Key engineering achievements

- **GKE lab, privately networked.** Zonal Standard cluster
  `platformforge-dev` in `us-central1-a` (`v1.36.4-gke.1391002`), one private
  `e2-standard-4`, Cloud NAT, and Workload Identity. Workload images are
  Artifact Registry digests.
- **Routing rollback, measured.** The canary controller compares Prometheus
  samples and writes a reviewed 100/0 VirtualService when the canary error
  ratio crosses 0.05. It does not deploy or delete workloads.
- **Least-privilege RBAC.** The controller may get two named Deployments and
  get or update one named VirtualService. The MCP reader may read pods, logs,
  events, and Deployments in `platformforge-dev`, and may get only
  `virtualservices/inventory-service`.
- **Workload Identity Federation.** GitHub Actions authenticates to Google
  with OIDC. There is no service-account JSON key in CI.
- **CI validates. Delivery publishes and stops.** The manual delivery
  workflow can push a digest. It then refuses to apply manifests.
- **STRICT mesh and private telemetry.** Istio mTLS, an inventory
  AuthorizationPolicy, and Prometheus, Grafana, Loki, Tempo, and Alloy on
  ClusterIP only.
- **Read-only investigation.** Fourteen stdio MCP tools. `verified_root_cause`
  stays null.

Inventory routing at the last check was 100 percent `v0.1.0` and 0 percent
`v0.2.0`.

## Architecture overview

```text
ServiceManifest
    -> platformforge CLI (validate, render; deploy is kind-only)
    -> Deployment + ServiceAccount + ClusterIP
         platformforge-dev
            Istio sidecar, STRICT mTLS
            orders-service -> inventory-service v0.1.0 | v0.2.0
            canary-controller reads Prometheus, writes a reviewed route
         platformforge-observability
            Prometheus, Grafana, Loki, Tempo, Alloy (no sidecar, emptyDir)
laptop
    stdio MCP -> short-lived reader token + loopback telemetry
```

```text
src/platformforge/          CLI, deployer, canary controller, MCP server
services/                   inventory-service and orders-service
deploy/istio/               reviewed mesh policy
deploy/observability/       Prometheus values and the Grafana dashboard
deploy/gke/                 GKE controller and MCP reader
deploy/kind/                kind controller and MCP reader
infra/terraform/            VPC, NAT, zonal GKE, IAM, GitHub OIDC
.github/workflows/          CI and the manual delivery workflow
scripts/demo/               local demo helpers
docs/                       install, incident, and evidence write-ups
```

Component detail is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Technologies

| Area | Stack |
| --- | --- |
| Services | Python 3.13, FastAPI, OpenTelemetry, distroless non-root images |
| Kubernetes | kind for the local lab; GKE Standard 1.36 for the cloud lab |
| Mesh | Istio 1.31.2, sidecar mode, CNI, STRICT mTLS |
| Cloud | Terraform, Cloud NAT, Workload Identity, Artifact Registry, GitHub OIDC |
| CI | GitHub Actions, Ruff, pytest, `pip-audit`, Trivy |
| Telemetry | Prometheus, Grafana, Loki, Tempo, Alloy |
| Investigation | MCP over stdio, 14 read-only tools |

## End-to-end reliability workflow

1. A developer changes service code or a ServiceManifest.
2. GitHub Actions CI lints, runs unit tests, validates Terraform and
   manifests, scans for secrets, audits pins, and builds both `linux/amd64`
   images. CI has no cloud token and no cluster.
3. A reviewer can dispatch delivery. Workload Identity Federation pushes an
   image digest to the existing Artifact Registry repository.
4. The same workflow then runs `python -m platformforge.delivery refuse-apply`.
   GitHub Actions does not deploy workloads. A human applies rendered YAML
   with `kubectl` on the pinned GKE context. `platformforge deploy` accepts
   only `kind-*` contexts.
5. An operator applies the reviewed 90/10 VirtualService to start a canary.
   The controller does not select that file and does not start a deployment.
6. Prometheus scrapes Istio request metrics and the controller `/metrics`
   endpoint. The controller stores a baseline on the first 90/10 read.
7. If the canary error ratio rises above 0.05, or latency regresses past the
   configured gates, the controller replaces the route with the reviewed
   stable VirtualService. Missing telemetry through the 120 second window
   also rolls the route back.
8. Grafana is provisioned with the service dashboard. The measured drill
   checked Grafana's HTTP health, not a browser session.
9. An operator can point a stdio MCP client at Kubernetes, Prometheus, Loki,
   and Tempo. The server is read-only. `verified_root_cause` stays null.

## Measured incident experiment

On 2026-10-21 a faulty inventory canary (`INVENTORY_FAILURE_PERCENT=100`)
received a share of live orders traffic on the pinned GKE cluster. The
client sent 400 requests:

| Result | Count |
| --- | --- |
| HTTP 200 | 356 |
| HTTP 502 | 44 |
| Total | 400 |

The controller's sample at the decision was 26 canary errors out of 26
canary requests, an error ratio of 100 percent. It recorded action
`rollback`. The resulting controller state was `stable`. The cumulative
rollback counter increased from 1 to 2.

The decision JSON includes `episode_age` of 30.229 seconds. That field is
the controller's episode clock. It is not a measured end-to-end recovery
duration. The 26-request sample is the scraped delta at that poll. It is a
separate count from the 44 client 502s.

**How routing was restored.** The controller wrote the reviewed file
`deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml`. That
VirtualService keeps subset `v0-1-0` at weight 100 and subset `v0-2-0` at
weight 0. Istio programs the caller sidecars from that object. A later read
showed weights 100 and 0. The time from the decision to sidecar convergence
was not measured on this drill. The controller did not roll the inventory
Deployment. The faulty canary process stays in place until an operator
applies the healthy manifest. Stable inventory continued to serve the
successful requests.

An earlier drill on the same cluster, with a different sample, is in
[docs/GKE_CANARY_EXPERIMENT.md](docs/GKE_CANARY_EXPERIMENT.md): 363 HTTP 200,
37 HTTP 502, a controller sample of 16/16, and a rollback counter moving
from 0 to 1. Do not mix those timings with this drill.

**MCP investigation.** The server exposes 14 read-only tools: `list_pods`,
`describe_pod`, `get_pod_logs`, `list_deployments`, `get_deployment_status`,
`list_kubernetes_events`, `get_service_status`, `get_routing`,
`query_incident_metrics`, `query_prometheus`, `query_loki`, `query_tempo`,
`get_service_health`, and `investigate_incident`. A read-only pass after
this drill saw the route at 100/0 and the rollback counter at 2.
`verified_root_cause` remains null. That pass did not return a successful
`investigate_incident` report body. Logs and metrics are untrusted. The
tools can show which version returned errors and that a rollback was logged.
They do not certify why.

## Security architecture

- Manifests reject unknown fields, credential-like environment names,
  `:latest`, and any Service other than ClusterIP.
- The namespace allowlist is code: `platformforge-dev` only. Existing objects
  must carry `app.kubernetes.io/managed-by=platformforge`.
- Rendered pods are non-root, drop all capabilities, use a read-only root
  filesystem, and set `seccompProfile: RuntimeDefault`. The namespace
  enforces restricted Pod Security.
- Istio STRICT mTLS stays on. `inventory-service` allows the
  `orders-service` identity, and only `GET /inventory/*` on port 8080.
  SPIFFE identity comes from `serviceAccountName`, which must equal the
  manifest name.
- The observability namespace is not injected. Telemetry Services are
  ClusterIP. Disks are emptyDir.
- Controller and MCP identities are namespace Roles. No ClusterRoleBinding
  is attached to either ServiceAccount. The MCP server has no HTTP transport
  and does not open port-forwards.
- GitHub delivery uses Workload Identity Federation bound to repository
  `MICHAELWlN/platformforge`, ref `main`, and environment `gke-delivery`.
- CI runs `python -m platformforge.secretscan`. Built images are scanned
  with Trivy. Terraform state, tfvars, plan files, kubeconfigs, private
  keys, and `deploy/gke/image-lock.yaml` are gitignored.

## CI/CD architecture

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main`.
Permissions are `contents: read`. Actions are pinned to commit SHAs.

`.github/workflows/delivery.yml` is `workflow_dispatch` only. Environment
`gke-delivery` requires a reviewer and allows only `main`. The publish job
uses OIDC. The gate job checks the pinned project, cluster, and context,
then refuses apply. One approved `0.2.0` publish succeeded (Actions run
39546044442) and did not roll the cluster. A later dispatch still needs the
reviewer.

The latest CI run on `main`, [39581870193](https://github.com/MICHAELWlN/platformforge/actions/runs/39581870193),
completed successfully for commit `150b903`. Documentation edits after that
commit are not part of that run. See [docs/CI_CD.md](docs/CI_CD.md).

## Testing and validation

```bash
make lint
make test                 # unit tests; kind and GKE tests stay skipped
make terraform-validate   # fmt, init -backend=false, validate; no plan or apply
python -m platformforge.secretscan
```

`make integration` runs only when the current context is `kind-platformforge`.
`make mcp-gke` and `pytest -m gke` use the pinned GKE context only.

Local `make test` on 2026-10-21: 411 passed, 70 skipped, 0 failed. Those
skipped tests are the live kind and GKE checks. Recorded commands are in
[TEST_EVIDENCE.md](TEST_EVIDENCE.md).

| Capability | Code | Unit tests | kind | GKE |
| --- | --- | --- | --- | --- |
| CLI deploy into `platformforge-dev` | Yes | Yes | Yes | CLI refuses the context |
| inventory and orders services | Yes | Yes | Yes | Installed from digests |
| STRICT mTLS and inventory authorization | Reviewed YAML | Policy tests | Yes | Yes |
| Prometheus, Grafana, Loki, Tempo, Alloy | Helm values | Partial | Yes | API health checked; UI not opened |
| Canary controller | Yes | Yes | Five experiments, including promote | Error rollback measured; promote and latency drill not run |
| GitHub Actions CI | Yes | Workflow tests | Not used | Not used |
| Image publish | Yes | Yes | Not used | One `0.2.0` publish; apply refused |
| MCP, 14 tools | Yes | Yes | Original 12 live; kind `get_routing` rule not applied | Original 12 live, plus `get_routing` |

## Local reproduction

Prerequisites: Python 3.13, Docker, kind, kubectl, and make. No cloud
account is required for the unit tests.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate
make lint
make test
platformforge validate examples/demo-web.yaml
platformforge deploy examples/demo-web.yaml --dry-run
```

A kind cluster, Istio, and the telemetry charts are separate installs.
Follow [docs/ISTIO.md](docs/ISTIO.md) and [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
`make images kind-load` builds `linux/arm64` images for kind. Integration
tests do not delete the client pods or the namespace. Do not point them at
GKE.

Copy `.cursor/mcp.example.json` to gitignored `.cursor/mcp.json` before using
the MCP server in Cursor. The example has no token. The reader kubeconfig
belongs in `/tmp`, mode `0600`, and is not committed.

## Cloud deployment

Cloud resources cost money for as long as they exist.
[docs/CLOUD_COSTS.md](docs/CLOUD_COSTS.md) is a list-price estimate from
2026-10-20, not an invoice: about $108 per month for one on-demand
`e2-standard-4`, a 50 GB boot disk, and Cloud NAT when the GKE management
free tier covers the billing account, or about $181 per month when it does
not. Artifact Registry storage is outside that estimate. A budget alert does
not stop spend. There is no scale-to-zero schedule.

`terraform apply`, `terraform destroy`, and `gcloud` commands that create
resources need an explicit approval. `make terraform-validate` does not
create anything.

```bash
make terraform-validate
```

Copy `infra/terraform/terraform.tfvars.example` to the gitignored
`terraform.tfvars` and set the operator CIDR there. Do not commit tfvars,
Terraform state, plan files, kubeconfigs, or service-account keys.

`platformforge deploy` will not target the GKE context. The verified install
used `kubectl`, `istioctl`, and Helm on
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`. Procedure:
[docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md) and
[docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md). Teardown is manual and is
documented in the same deployment guide. This repository does not run it.

## Known limitations

- GitHub Actions refuses to apply manifests. Publishing a digest does not
  roll a Deployment.
- The controller does not start the 90/10 shift and does not deploy pods.
  The GKE canary was not promoted to 100 percent. The GKE latency drill was
  not measured.
- `episode_age` is not recovery time. Client recovery time was not measured
  for the 400-request drill.
- `verified_root_cause` stays null. The MCP operator still mints the reader
  token and opens the port-forwards.
- Controller counters reset when the pod is replaced. Telemetry disks are
  emptyDir.
- One zonal node. A preemption or a disruptive upgrade takes the lab with it.
- The demo video URL and screenshots are not in the repository.
- No license file is committed.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, trust boundaries
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md): chronological record of what was verified
- [TEST_EVIDENCE.md](TEST_EVIDENCE.md): commands and results
- [docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md): GKE design, access, and destroy
- [docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md): what was installed on the cluster
- [docs/GKE_CANARY_EXPERIMENT.md](docs/GKE_CANARY_EXPERIMENT.md): the earlier measured rollback
- [docs/CANARY_CONTROLLER.md](docs/CANARY_CONTROLLER.md): kind reconciliation loop
- [docs/CI_CD.md](docs/CI_CD.md): CI jobs and the gated publish workflow
- [docs/ISTIO.md](docs/ISTIO.md): mesh install and verification
- [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md): telemetry install
- [docs/MCP_SERVER.md](docs/MCP_SERVER.md): the 14 read-only tools
- [docs/MCP_SECURITY.md](docs/MCP_SECURITY.md): allowlists and redaction
- [docs/CLOUD_COSTS.md](docs/CLOUD_COSTS.md): list-price estimate
- [docs/FINAL_DEMO.md](docs/FINAL_DEMO.md): recording procedure

The operator reference below has the manifest schema, command exit codes, and
local kind procedures. The full doc index is at the end of that reference.

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
  defines subsets, and a reviewed VirtualService sets the weights (100/0 stable,
  90/10 canary, or 0/100 promote).

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

On 2026-10-21 the cluster `platformforge-dev` (`v1.36.4-gke.1391002`) was verified with Istio 1.31.2,
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

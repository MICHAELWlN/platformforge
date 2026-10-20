# PlatformForge

PlatformForge is an internal developer platform (IDP) portfolio project. Developers describe a
service in a small, validated YAML manifest; the `platformforge` CLI renders it into hardened
Kubernetes resources and deploys it into an allowlisted namespace.

> **Status: Milestone 3 (CLI + two FastAPI microservices + CI + Istio service mesh).**
> Verified against a local kind cluster only: STRICT mTLS, per-service workload identity, an
> identity-based AuthorizationPolicy, and a 90/10 canary with a failure drill. Nothing here is
> production-ready, and GKE, Terraform, observability, and MCP are **not implemented yet**.
> The CI workflow has been linted and its steps replayed locally, but it has not yet run on
> GitHub (no remote). See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

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

The `inventory-service:0.1.0` image used in the recorded runs was built during Milestone 2.
Rebuilding it from the current source adds only the failure knob, which is off by default.

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

Istio objects are applied with `kubectl` from `deploy/istio/`. PlatformForge manifests cannot
contain them.

Under STRICT mTLS, `kubectl port-forward` to a pod still works (it enters through the pod's
loopback). `kubectl proxy` / API-server service proxy URLs do not, because the API server
is a plaintext client.

Images: multi-stage `services/Dockerfile`, build stage `python:3.13-slim-trixie`, runtime
`gcr.io/distroless/python3-debian13:nonroot` (no shell, no package manager, UID 65532), both
pinned by digest. Runtime dependencies are exactly pinned in `services/requirements.txt`.

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
- The tests create two client pods in `platformforge-dev`, `pf-test-client-mesh` (sidecar,
  `default` identity) and `pf-test-client-plain` (no sidecar), and reuse them. The tests
  never delete them.

CI (`.github/workflows/ci.yml`) runs Ruff, unit tests, and both image builds with a
read-only / no-capabilities smoke run. It uses no secrets, no cloud credentials, and no cluster.

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
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md): what is and is not implemented
- [TEST_EVIDENCE.md](TEST_EVIDENCE.md): commands run and their results
- [AGENTS.md](AGENTS.md): rules for AI coding agents working in this repository

# PlatformForge

PlatformForge is an internal developer platform (IDP) portfolio project. Developers describe a
service in a small, validated YAML manifest; the `platformforge` CLI renders it into hardened
Kubernetes resources and deploys it into an allowlisted namespace.

> **Status: Milestone 2 (CLI + deployment engine + two FastAPI microservices + CI).** Verified
> against a local kind cluster only. Nothing here is production-ready, and GKE, Terraform,
> Istio, observability, and MCP are **not implemented yet**. The CI workflow has been linted
> and its steps replayed locally, but it has not yet run on GitHub (no remote).
> See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

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
```

Unknown fields anywhere are rejected, so raw Kubernetes objects (or fields such as
`hostNetwork`, `securityContext`, `valueFrom`, `type: LoadBalancer`) cannot be smuggled in.

## What gets deployed

- **Namespace** (only with `--create-namespace`): labeled
  `pod-security.kubernetes.io/enforce=restricted`.
- **Deployment**: rolling update (`maxSurge: 1`, `maxUnavailable: 0`), readiness + liveness
  probes, requests/limits, `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all capabilities dropped,
  no service-account token automount, `/tmp` as a 64Mi `emptyDir`.
- **Service**: `ClusterIP` only. PlatformForge never renders NodePort, LoadBalancer, Ingress, or
  external IPs.

All objects carry `app.kubernetes.io/managed-by=platformforge`. PlatformForge refuses to
update (or report on) an existing Deployment/Service of the same name that lacks this label.

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
| `502 inventory_bad_status` / `inventory_bad_response` | unexpected status or malformed body |
| `503 inventory_unavailable` | connection refused / reset / DNS failure |
| `504 inventory_timeout` | no response within `INVENTORY_TIMEOUT_SECONDS` |

Error bodies include the `request_id`. Every response carries an `X-Request-ID` header: a
well-formed inbound ID (`[A-Za-z0-9._-]{1,128}`) is reused, otherwise one is generated. The
ID is forwarded to `inventory-service` and appears in both services' JSON logs.

Configuration (environment variables, set in the PlatformForge manifest):

| Variable | Service | Default | Bounds |
| --- | --- | --- | --- |
| `INVENTORY_URL` | orders | `http://inventory-service:8080` | absolute http(s); no credentials, query, or fragment |
| `INVENTORY_TIMEOUT_SECONDS` | orders | `2` | 0.1–30 |
| `LOG_LEVEL` | both | `info` | debug, info, warning, error |
| `PORT` | both | `8080` | 1024–65535 |

### Build, load, deploy (local kind)

```bash
make images        # docker build --platform linux/arm64, one target per service
make kind-load     # kind load docker-image ... --name platformforge
platformforge deploy services/manifests/inventory-service.yaml
platformforge deploy services/manifests/orders-service.yaml
platformforge status services/manifests/orders-service.yaml
```

Images: multi-stage `services/Dockerfile`, build stage `python:3.13-slim-trixie`, runtime
`gcr.io/distroless/python3-debian13:nonroot` (no shell, no package manager, UID 65532), both
pinned by digest. Runtime dependencies are exactly pinned in `services/requirements.txt`.

## Testing

```bash
make lint                                # ruff check + format check (src, services, tests)
make test                                # unit tests; integration tests are skipped

# Integration tests: kind contexts only, current context must match, nothing is deleted.
# Service tests need `make images kind-load` first.
make integration                         # = PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
```

`tests/integration/test_services_kind.py::test_08` simulates an outage by scaling
`inventory-service` to 0 replicas, then restores it with `platformforge deploy`.

CI (`.github/workflows/ci.yml`) runs Ruff, unit tests, and both image builds with a
read-only / no-capabilities smoke run. It uses no secrets, no cloud credentials, and no cluster.

Recorded results are in [TEST_EVIDENCE.md](TEST_EVIDENCE.md).

## Cleanup (manual, destructive)

Integration tests intentionally leave `platformforge-dev` in place. To remove everything
PlatformForge created on the local kind cluster, review and run:

```bash
kubectl --context kind-platformforge delete namespace platformforge-dev
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, trust boundaries
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md): what is and is not implemented
- [TEST_EVIDENCE.md](TEST_EVIDENCE.md): commands run and their results
- [AGENTS.md](AGENTS.md): rules for AI coding agents working in this repository

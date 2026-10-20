# Architecture

This document describes what exists today (Milestones 1 and 2) and, separately, the target
architecture. Only the first two sections are implemented.

## Milestone 1: implemented

```
 developer
    |
    v
 platformforge CLI (Typer)             src/platformforge/cli.py
    |
    |-- validate --------> manifest.py: YAML (safe, no aliases, <=64 KiB)
    |                        -> Pydantic ServiceManifest (strict, extra=forbid)
    |                        -> namespace allowlist, name/image/resource/probe policy
    |
    |-- deploy --dry-run -> resources.py: pure builders -> YAML to stdout (no cluster)
    |
    |-- deploy/status/logs
            |
            v
       kubecontext.py: read kubeconfig YAML -> display context -> require approval
            |            (interactive y/N, or --approve-context NAME == current context)
            v
       kubernetes client pinned to approved context name
            |
            v
       deployer.py: allowlist re-check -> ownership preflight -> create or replace
            |        -> poll Deployment status until rollout complete
            v
       Kubernetes API (kind-platformforge)  ->  namespace platformforge-dev
                                                 |- Deployment demo-web (2 replicas)
                                                 |- Service demo-web (ClusterIP)
```

### Modules

| Module | Responsibility | Talks to cluster |
| --- | --- | --- |
| `manifest.py` | Schema, allowlist, quantity parsing, YAML loading | No |
| `resources.py` | Render Deployment / Service / Namespace objects | No |
| `kubecontext.py` | Show current context, approval, build API clients | Builds clients only |
| `deployer.py` | Create/replace, ownership checks, rollout wait, status, logs | Yes |
| `cli.py` | Commands, output, exit codes | Via the above |

### Design decisions

- **Own manifest format, not Kubernetes YAML.** Developers cannot pass arbitrary Kubernetes
  objects. The platform decides security context, Service type, labels, and rollout strategy.
- **Strict Pydantic models** (`strict=True`, `extra="forbid"`, frozen). Type coercion is off
  (e.g. `replicas: "2"` is rejected); CPU accepts a YAML int as a convenience.
- **Allowlist in code.** `ALLOWED_NAMESPACES = {"platformforge-dev"}`. Not configurable by
  environment variable or flag, so it cannot be widened without a reviewed change.
- **Defense in depth.** The allowlist is checked at manifest load and again at every
  `deployer` entry point.
- **Ownership label.** Existing objects without `app.kubernetes.io/managed-by=platformforge`
  are never replaced. Ownership is checked for both Deployment and Service *before* any
  write, and re-checked on 409 conflict to cover races.
- **Create-or-replace instead of server-side apply.** Replace gives full-object semantics
  (removed env vars are actually removed). Service `clusterIP(s)` are carried over because
  they are immutable. Server-side apply is a candidate for a later milestone.
- **Context display before credential loading.** `kubecontext.current_context()` parses
  kubeconfig YAML itself so the API server URL is shown before any exec credential plugin
  (e.g. `gke-gcloud-auth-plugin`) runs. The client is then built for that exact context name,
  so a concurrent `kubectl config use-context` cannot redirect the operation.
- **No shell-outs.** All cluster access is through the official `kubernetes` Python client.

### Rollout completion

A rollout is complete when `status.observedGeneration >= metadata.generation` and
`updatedReplicas == availableReplicas == replicas == spec.replicas`. This mirrors the
conditions `kubectl rollout status` uses, including waiting for old replicas to terminate.

## Milestone 2: implemented (microservice foundation)

```
 namespace platformforge-dev (Pod Security: restricted)

   client (tests: kube-apiserver service proxy; no public exposure)
      |
      v
   Service orders-service (ClusterIP :8080)
      |
      v
   orders-service pods (x2)
      |  GET /inventory/{id}, X-Request-ID forwarded, 2 s timeout
      |  INVENTORY_URL=http://inventory-service.platformforge-dev.svc.cluster.local:8080
      v
   Service inventory-service (ClusterIP :8080)
      |
      v
   inventory-service pods (x2)
```

Both Deployments and Services are created by `platformforge deploy` from
`services/manifests/*.yaml`. No CLI or schema changes were needed: the existing manifest
fields (image, replicas, port, env, resources, probes) were sufficient.

### Code layout

| Path | Responsibility |
| --- | --- |
| `services/pf_service_common/requestid.py` | Validate or generate request IDs; `ContextVar` holding the current ID |
| `services/pf_service_common/logs.py` | JSON formatter; log-record factory that stamps the request ID at record creation; routes uvicorn logs through the same handler |
| `services/pf_service_common/app.py` | Pure-ASGI middleware (request ID in/out, one access log line per request); `/healthz`, `/readyz`, `/version`; OpenAPI/docs disabled |
| `services/pf_service_common/server.py` | Process entry: configure logging, run uvicorn (no server header, no proxy-header trust) |
| `services/inventory_service/app.py` | Static demo catalog; `GET /inventory/{item_id}` |
| `services/orders_service/settings.py` | Validated env config (`INVENTORY_URL`, `INVENTORY_TIMEOUT_SECONDS`) |
| `services/orders_service/app.py` | `GET /orders/check/{item_id}`; maps upstream failures to 404/502/503/504 |

### Design decisions

- **Shared library, separate images.** `pf_service_common` is copied into both images;
  each image contains only its own service package plus the shared one.
- **Readiness is local only.** `/readyz` is true between app startup and shutdown and does
  not call inventory-service. If readiness followed the upstream, an inventory outage would
  pull every orders-service replica out of its Service (cascading failure); instead each
  request fails fast with a precise status. Verified live: orders stayed 2/2 Ready while
  inventory was at 0 replicas.
- **Failure mapping.** `httpx.TimeoutException` → 504, other `TransportError` (refused,
  reset, DNS) → 503, other request errors, non-200/404 statuses, malformed bodies, or a
  mismatched `item_id` → 502. No retries and no redirect following in this milestone.
- **Input validation before any upstream call.** `item_id` must match
  `^[a-z0-9][a-z0-9-]{0,63}$`; the upstream URL is the configured base plus a fixed path.
- **Request IDs.** Inbound `X-Request-ID` is accepted only if it matches
  `[A-Za-z0-9._-]{1,128}` (prevents log injection and header abuse); otherwise a UUID4 hex
  is generated. It is echoed in the response, included in error bodies, forwarded upstream,
  and present on every log line emitted while handling the request.
- **Logging.** One JSON object per line on stdout (`ts`, `level`, `service`, `logger`,
  `message`, `request_id`, plus structured fields). Probe requests are logged at DEBUG to
  keep INFO logs useful.

### Container images

| Property | How |
| --- | --- |
| Minimal | Distroless Python 3.13 runtime (no shell, no package manager); 28.5 MB as stored on the kind node |
| Non-root | `USER 65532:65532`; pod also has `runAsNonRoot: true` |
| Read-only root FS | Bytecode precompiled at build time, `PYTHONDONTWRITEBYTECODE=1`; only `/tmp` (emptyDir) is writable |
| No capabilities | PlatformForge drops `ALL`; verified `CapEff: 0000000000000000`, `NoNewPrivs: 1` in a live pod |
| Reproducible inputs | Base images pinned by digest; all Python deps pinned exactly; `--only-binary=:all:` |

### CI

`.github/workflows/ci.yml`: three jobs (Ruff, unit tests, matrix image build + smoke run).
`permissions: contents: read`, actions pinned to commit SHAs, `persist-credentials: false`,
no secrets, no cloud credentials, no cluster.

## Target architecture: not yet implemented

The following are planned and **do not exist in the repository yet**:

- Terraform-managed GCP: VPC, GKE (private nodes, Workload Identity), Artifact Registry
- GKE workload deployment through the same CLI
- Istio with STRICT mTLS, traffic management, and canary deployments
- OpenTelemetry instrumentation; Prometheus, Loki, Tempo, Grafana
- CD (image publishing and deployment from CI)
- Read-only MCP server for Kubernetes diagnostics
- Controlled incident simulation, AI-assisted investigation, rollback

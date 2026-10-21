# Architecture

This document describes what exists today. Milestones 1–4 run on a local kind
cluster. The GKE cluster `platformforge-dev` in `us-central1-a` is running the
same platform shape: digest-pinned workloads, Istio STRICT, the observability
stack, and the canary controller. Terraform for that environment is
`infra/terraform/`. Milestone 5 below is the live cluster.

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
| `resources.py` | Render Deployment / Service / Namespace / ServiceAccount objects | No |
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
`services/manifests/*.yaml`. Milestone 2 needed no CLI or schema changes: the existing
manifest fields (image, replicas, port, env, resources, probes) were sufficient. Milestone 3
adds the mesh in front of this path and changes the test client to an in-cluster pod; see
below.

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

`.github/workflows/ci.yml` lints, tests, validates Terraform and manifests, audits
`services/requirements.txt`, and builds both `linux/amd64` images.
`permissions: contents: read`, actions pinned to commit SHAs, `persist-credentials: false`,
no secrets, no cloud credentials, no cluster. Delivery is a separate manual
workflow. GitHub OIDC for `MICHAELWlN/platformforge` is applied, and one
`0.2.0` publish succeeded. That publish did not roll the cluster. See
[docs/CI_CD.md](docs/CI_CD.md). Canary decisions live in
`platformforge.delivery` and do not call the cluster
([docs/PROGRESSIVE_DELIVERY.md](docs/PROGRESSIVE_DELIVERY.md)).
`platformforge.canary` compares stable and canary Prometheus samples.
`platformforge.canary_apply` installs a reviewed 100/0, 90/10, or 0/100
VirtualService on `kind-platformforge` only. `platformforge.canary_controller`
polls that route on kind, serves `GET /metrics`, and writes only the stable
or promote file. The same loop runs on GKE from the digest-pinned Deployment
in `deploy/gke/canary-controller.yaml`. That process also requires
`PLATFORMFORGE_CANARY_GKE=1` and the pod service-account token. It does not
select the 90/10 file. See [docs/AUTOMATED_CANARY.md](docs/AUTOMATED_CANARY.md),
[docs/AUTOMATED_ROLLBACK.md](docs/AUTOMATED_ROLLBACK.md), and
[docs/CANARY_CONTROLLER.md](docs/CANARY_CONTROLLER.md).

### Read-only MCP

`python -m platformforge.mcp_server` is a stdio MCP server (`mcp` 2.3.0). It
lists and reads allowlisted namespaces. `get_routing` reads only
`virtualservices/inventory-service` and only with get. `query_incident_metrics`
keeps cumulative counters separate from a 15-to-900-second increase and does
not treat NaN as zero. Kind and GKE live checks each use their own
`platformforge-mcp-reader` token. A GKE call without that token is rejected.
It does not deploy, listen on a port, or start when the package is imported.
See [docs/MCP_SERVER.md](docs/MCP_SERVER.md),
[docs/MCP_LIVE_INTEGRATION.md](docs/MCP_LIVE_INTEGRATION.md), and
[docs/MCP_GKE_INTEGRATION.md](docs/MCP_GKE_INTEGRATION.md).

## Milestone 3: implemented (Istio service mesh, local kind)

```
 istio-system (PSA privileged; installed by istioctl, not PlatformForge)
   istiod 1.31.2 ── xDS config + SPIFFE certificates (24h) ──┐
   istio-cni-node (DaemonSet; sets up pod traffic redirection) │
                                                              │
 platformforge-dev (PSA restricted, istio-injection=enabled)  │
   PeerAuthentication default: STRICT                         │
                                                              v
   pf-test-client-mesh (sa/default) ──mTLS──┐     every pod: app + native istio-proxy sidecar
                                            v
   Service orders-service ──> orders-service pods (sa/orders-service, x2)
                                  |  app -> localhost sidecar (plain HTTP)
                                  |  sidecar -> sidecar: mTLS TLSv1.3,
                                  |  SAN spiffe://cluster.local/ns/platformforge-dev/sa/orders-service
                                  v
   Service inventory-service ── VirtualService (weights) + DestinationRule (subsets by version label)
          |                          |
          | 90 (stable: 100)         | 10 (stable: 0)
          v                          v
   inventory-service-v0-1-0 (x2)   inventory-service-v0-2-0 (x1)     both sa/inventory-service
          \__ AuthorizationPolicy inventory-service (ALLOW only:
              principal .../sa/orders-service, GET /inventory/*, port 8080)

   pf-test-client-plain (no sidecar) ──plaintext──> any pod: TCP accepted, then reset
   kubelet probes ──> :15020/app-health/<container>/... (sidecar agent) ──> app on localhost
```

### Responsibilities

| Concern | Owner | Where |
| --- | --- | --- |
| Istio control plane, CNI | istioctl, operator | `deploy/istio/istio-operator.yaml`, `istio-system-namespace.yaml` |
| Sidecar injection scope | namespace label | `istio-injection=enabled` on `platformforge-dev` only; `--create-namespace` sets it |
| Workload identity | PlatformForge | `serviceAccountName` → ServiceAccount (no token automount, no RBAC) + `spec.serviceAccountName` |
| Version labels and Deployment names | PlatformForge | `version` → `app.kubernetes.io/version` and `<name>-vX-Y-Z` |
| Graceful termination | PlatformForge | `termination` → `terminationGracePeriodSeconds`, native `preStop: sleep` |
| mTLS mode, authorization, routing | reviewed YAML, applied with kubectl | `deploy/istio/platformforge-dev/*.yaml` |

PlatformForge does not render Istio objects. The routing and security policy is a small,
fixed set of reviewed files. Letting the CLI accept them would turn it into an
arbitrary-manifest executor, which this project deliberately avoids.

### Trust boundaries under the mesh

- **Identity is bound to the ServiceAccount.** The manifest can only name a ServiceAccount
  equal to `metadata.name`. One service therefore cannot claim another's SPIFFE identity
  through PlatformForge. Someone with direct `kubectl` access to the namespace still can.
- **The ServiceAccounts have no API permissions.** Pods mount only Istio's `istio-ca`
  audience token, which istiod accepts for certificate signing. The Kubernetes API audience
  token is not mounted.
- **STRICT applies to every sidecar in the namespace.** Clients without a sidecar, including
  the kube-apiserver service proxy, cannot get responses.
- **Authorization is per workload.** inventory-service has an ALLOW policy, so anything not
  matched is denied. orders-service and demo-web have no policy, so any mTLS peer is
  allowed.
- **Probes bypass mTLS and authorization**, by design. The kubelet calls the sidecar agent
  on port 15020, which forwards to the app over localhost.

### Versioned Deployments and the canary

A manifest with `version` renders Deployment `<name>-<version>`. Pods are labeled
`app.kubernetes.io/version`; the Deployment selector includes the version, and the Service
selector does not. Several versions can therefore run behind one Service without their
ReplicaSets fighting over pods. Without Istio routing, the Service spreads traffic across
pods (≈1/3 to a 1-replica canary next to 2 stable replicas). With the VirtualService, the
caller's sidecar picks the subset by weight, independent of replica counts. See
[docs/CANARY_RELEASE.md](docs/CANARY_RELEASE.md).

`find_overlapping_deployments` warns when another managed Deployment, such as the
pre-version `inventory-service`, selects the same pods. PlatformForge never deletes
workloads.

### Failure mapping under the mesh

orders-service now also maps statuses synthesized by its own sidecar:
- 503 (`no_healthy_upstream`) → 503 `inventory_unavailable`.
- 504 → 504 `inventory_timeout`.
- 403 (`RBAC: access denied`) → 502 `inventory_forbidden`.
- Other non-200 statuses, such as an injected 500, → 502 `inventory_bad_status`.

## Milestone 4: implemented (observability, local kind)

```
pf-test-client-mesh
        |  mTLS + W3C traceparent
        v
orders-service sidecar  --->  OTLP/gRPC :4317  Tempo
        |                              ^
        v                              | OTLP/HTTP :4318
orders-service (FastAPI spans) --------+
        |  mTLS + traceparent
        v
inventory-service sidecar + FastAPI spans
        |
        +--> sidecar :15020 /stats/prometheus --> Prometheus --> Grafana
        +--> stdout JSON (request_id, trace_id, span_id) --> Alloy --> Loki --> Grafana
```

The telemetry namespace is `platformforge-observability`. It is not injected into the
mesh. Application traffic still enters only through `platformforge-dev`, where
PeerAuthentication remains STRICT and the inventory AuthorizationPolicy is unchanged.

Prometheus discovery is limited to that namespace by a RoleBinding onto a ClusterRole
that can list pods, services, endpoints, and endpointslices. Alloy's Role in the same
namespace can read `pods` and `pods/log`. Loki, Tempo, and Grafana have no Kubernetes
API permissions. Details, chart versions, and the emptyDir consequence are in
[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

## Milestone 5: GKE cluster with the platform installed

```
 operator laptop (CIDR on the authorized-network list)
      |  TLS + Google credentials
      v
 public control-plane endpoint (authorized networks; DNS endpoint external traffic off)
      |
      v
 zonal Standard GKE control plane in us-central1-a (private nodes)
      |
      v
 VPC platformforge-dev (custom mode, regional routing)
   subnet 10.10.0.0/22
     pods     10.20.0.0/16
     services 10.30.0.0/20
   worker nodes: no external IP, Cloud NAT for egress, Private Google Access
   Workload Identity pool PROJECT.svc.id.goog
     node SA  -> log and metric write only
     workload SA -> no project roles; one Kubernetes SA may impersonate it
```

Terraform in `infra/terraform/` builds that shape and nothing else. The cluster
is running at `v1.36.4-gke.1391002` with one private `e2-standard-4` node.
Istio 1.31.2 (CNI chained onto Dataplane V2, native sidecars, no gateways),
the two services, and the observability stack are installed. Application images
are digest-pinned from the private Artifact Registry repository `platformforge`
in `us-central1`. Mesh policy is the same reviewed files used on kind: STRICT
mTLS, inventory authorization limited to `orders-service`, and stable routing
at 100/0. Kind remains the arm64 lab and was not modified. Details are in
[docs/GKE_DEPLOYMENT.md](docs/GKE_DEPLOYMENT.md) and
[docs/GKE_OPERATIONS.md](docs/GKE_OPERATIONS.md).

## Limits that remain

- `platformforge deploy` refuses any context that does not start with `kind-`.
  GKE re-deploys are `kubectl apply` on the pinned context.
- The delivery workflow can publish an image digest. It still refuses to apply
  manifests.
- The GKE controller has not promoted `v0.2.0` to 100 percent. A later
  faulty-canary drill raised the cumulative rollback counter from 1 to 2 and
  left the controller state `stable`. `episode_age` 30.229 seconds on that
  decision is the episode clock. `platformforge-canary` is the scrape job.
  Terraform was not changed for the controller.
- MCP investigations leave `verified_root_cause` null.
- No GCP IAM role narrows the operator identity that mints the MCP reader token.
- `deploy/kind/mcp-reader.yaml` has the same `get` rule and was not applied to kind.

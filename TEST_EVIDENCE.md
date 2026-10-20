# Test Evidence

All results below were produced on 2026-10-20 on the development machine. Output is
abridged; nothing was edited to change a result. Milestone 1 evidence comes first;
Milestone 2 evidence is in the second half of this file.

## Environment

| Component | Version |
| --- | --- |
| OS | macOS (darwin 25.5.0), Apple Silicon |
| Python (venv) | 3.13.16 (`/opt/homebrew/bin/python3.13`) |
| typer / pydantic / kubernetes / PyYAML | 0.27.3 / 2.14.1 / 37.0.1 / 6.0.3 |
| pytest / ruff | 9.1.1 / 0.17.1 |
| kind | v0.33.0, node image `kindest/node:v1.37.0` (1 control-plane node) |
| kubectl client | v1.36.1 |
| Kube context | `kind-platformforge` (only context in kubeconfig), server `https://127.0.0.1:49954` |

## 1. Static checks

```
$ ruff check src tests && ruff format --check src tests
All checks passed!
14 files already formatted
```

## 2. Unit tests (Kubernetes API mocked)

```
$ pytest
135 passed, 10 skipped in 0.71s        # 10 skipped = integration tests (not opted in)
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_manifest.py` | 80 | names, namespace allowlist, raw k8s rejection, unknown fields, image pinning, resource bounds, probes, env credential heuristic, YAML aliases / multi-doc / python tags, file limits |
| `test_deployer.py` | 23 | create path, update path, namespace required/created, ownership refusal before any write, 409 race re-check, allowlist re-check at every entry point, rollout logic and timeout, status, logs |
| `test_cli.py` | 16 | help, validate, dry-run without cluster, invalid manifest never contacts cluster, declined/mismatched approval, exit codes |
| `test_kubecontext.py` | 8 | kubeconfig parsing and merge order, approval rules |
| `test_resources.py` | 8 | labels, selector, probes, resources, security context, ClusterIP-only Service, PSA labels |

### Defects found and fixed during testing

1. **Lint**: three lines over 100 chars. Fixed with `ruff format`.
2. **Integration test defect**: `test_05_service_is_cluster_ip` used `spec.external_i_ps`
   (the attribute name in older kubernetes clients). In kubernetes client 37.0.1, models are
   Pydantic-based and the attribute is `spec.external_ips`, so the test raised
   `AttributeError`. This was a test bug, not a product bug. Fixed. The same test was also
   moved from the deprecated v1 Endpoints API to `discovery.k8s.io/v1` EndpointSlices.

### Not performed

- A mutation check (temporarily disabling the allowlist and ownership checks to confirm the
  tests fail) was blocked by the agent's sandbox reviewer and was not run. The relevant tests
  do assert that no create/replace calls occur when those checks trigger.

## 3. CLI validation (manual)

```
$ platformforge validate examples/demo-web.yaml
OK: demo-web (namespace=platformforge-dev, image=nginxinc/nginx-unprivileged:1.29-alpine, replicas=2, port=8080)
exit=0

$ platformforge validate badns.yaml            # namespace: kube-system
error: manifest validation failed:
  metadata.namespace: namespace 'kube-system' is not allowlisted (allowed: platformforge-dev)
exit=1

$ platformforge validate badmulti.yaml         # name: Demo_Web, image :latest
error: manifest validation failed:
  metadata.name: must be a DNS-1035 label: ...
  spec.image: must not use the mutable ':latest' tag
exit=1

$ platformforge validate raw.yaml              # raw apps/v1 Deployment with hostNetwork
error: manifest validation failed:
  apiVersion: Input should be 'platformforge.dev/v1alpha1'
  kind: Input should be 'ServiceManifest'
  spec.image: Field required
  ...
  spec.template: Extra inputs are not permitted
exit=1

$ platformforge deploy badns.yaml --approve-context kind-platformforge
error: manifest validation failed:
  metadata.namespace: namespace 'kube-system' is not allowlisted (allowed: platformforge-dev)
exit=1                                         # rejected before any cluster contact

$ platformforge deploy examples/demo-web.yaml --dry-run
(Deployment + ClusterIP Service YAML printed; exit=0; no cluster access)
```

## 4. Integration tests (live kind cluster)

```
$ PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
```

**Run 1 (first deploy, fresh cluster):** 9 passed, 1 failed (`test_05`, test defect above).
`test_02_deploy_creates_and_rolls_out` passed: namespace created, Deployment and Service
created, rollout reached 2/2 available within the 240 s budget.

**Run 2 (after fix):**

```
test_01_wrong_context_approval_is_refused PASSED
test_02_deploy_creates_and_rolls_out PASSED
test_03_namespace_has_restricted_pod_security PASSED
test_04_live_deployment_matches_manifest PASSED
test_05_service_is_cluster_ip PASSED
test_06_pods_ready_and_running PASSED
test_07_redeploy_is_idempotent_update PASSED
test_08_status_reports_healthy PASSED
test_09_logs_returns_pod_output PASSED
test_10_no_platformforge_objects_outside_allowlist PASSED
10 passed, 135 deselected in 0.61s
```

**Full run (unit + integration):** `145 passed in 0.94s`.

Run 2 exercised the update path (`Deployment/demo-web: updated`), because Run 1 had already
created the resources.

## 5. Live cluster state after tests

```
$ echo n | platformforge status examples/demo-web.yaml
Kubernetes context:
  context:    kind-platformforge
  cluster:    kind-platformforge
  server:     https://127.0.0.1:49954
  user:       kind-platformforge
  kubeconfig: /Users/michaelnguyen/.kube/config
Use Kubernetes context 'kind-platformforge'? [y/N]: error: kube context not approved; no cluster calls were made
exit=3

$ echo y | platformforge status examples/demo-web.yaml
Service:    demo-web (namespace platformforge-dev)
Image:      nginxinc/nginx-unprivileged:1.29-alpine
Replicas:   desired=2 updated=2 ready=2 available=2
Conditions: Available=True (MinimumReplicasAvailable), Progressing=True (NewReplicaSetAvailable)
Endpoint:   ClusterIP 10.96.89.204
Pods:
  demo-web-7d4489dfbb-9jtn8  phase=Running ready=True restarts=0 node=platformforge-control-plane
  demo-web-7d4489dfbb-zrz8q  phase=Running ready=True restarts=0 node=platformforge-control-plane
Health:     HEALTHY
exit=0

$ platformforge logs examples/demo-web.yaml --approve-context kind-platformforge --tail 3
==> demo-web-7d4489dfbb-9jtn8 <==
10.244.0.1 - - [20/Oct/2026:20:58:43 +0000] "GET / HTTP/1.1" 200 896 "-" "kube-probe/1.37" "-"
...
==> demo-web-7d4489dfbb-zrz8q <==
...
exit=0

$ kubectl get ns platformforge-dev --show-labels
platformforge-dev   Active   app.kubernetes.io/managed-by=platformforge,...,pod-security.kubernetes.io/enforce=restricted

$ kubectl -n platformforge-dev get deploy,rs,svc,pods,endpointslices
deployment.apps/demo-web              2/2   2   2
replicaset.apps/demo-web-7d4489dfbb   2     2   2      # single ReplicaSet: re-deploy did not trigger a new rollout
service/demo-web                      ClusterIP   10.96.89.204   <none>   8080/TCP
pod/demo-web-7d4489dfbb-9jtn8         1/1   Running   0
pod/demo-web-7d4489dfbb-zrz8q         1/1   Running   0
endpointslice.discovery.k8s.io/demo-web-jrr75   IPv4   8080   10.244.0.5,10.244.0.6

$ kubectl get deploy,svc -A -l app.kubernetes.io/managed-by=platformforge
platformforge-dev   deployment.apps/demo-web
platformforge-dev   service/demo-web

$ kubectl -n platformforge-dev get events --field-selector type=Warning
No resources found in platformforge-dev namespace.
```

The pods were admitted under the `restricted` Pod Security Standard, which confirms on a
real API server that the rendered pod spec complies with it.

## What this evidence does not show

- Behavior on GKE or any multi-node / production cluster.
- Behavior under RBAC-restricted credentials (kind admin credentials were used).
- Ownership refusal against a real pre-existing unmanaged object (covered by unit tests only).
- In-cluster HTTP traffic through the Service (probes passing and ready EndpointSlices are the
  evidence of serving). *Milestone 2 now covers this; see below.*

---

# Milestone 2: microservice foundation

## M2 environment additions

| Component | Version |
| --- | --- |
| fastapi / starlette / uvicorn / httpx | 0.156.2 / 1.9.0 / 0.54.0 / 0.28.1 |
| Build image | `python:3.13-slim-trixie@sha256:70729b46…` |
| Runtime image | `gcr.io/distroless/python3-debian13:nonroot@sha256:ed91505d…` (Python 3.13.5, UID 65532) |
| actionlint | 1.7.12 (Docker image `rhysd/actionlint:1.7.12`) |

## M2.1 Image builds (local, arm64)

```
$ make images        # docker build --platform linux/arm64 --provenance=false --target <svc> ... services
naming to docker.io/platformforge/inventory-service:0.1.0 done
naming to docker.io/platformforge/orders-service:0.1.0 done

$ docker image inspect platformforge/<svc>:0.1.0
inventory-service: os=linux arch=arm64 user=65532:65532 entrypoint=["/usr/bin/python3.13"] cmd=["-m","inventory_service"]
orders-service:    os=linux arch=arm64 user=65532:65532 entrypoint=["/usr/bin/python3.13"] cmd=["-m","orders_service"]

$ make kind-load
Image: "platformforge/inventory-service:0.1.0" ... not yet present on node "platformforge-control-plane", loading...
Image: "platformforge/orders-service:0.1.0" ... not yet present on node "platformforge-control-plane", loading...

$ docker exec platformforge-control-plane crictl images | grep platformforge
docker.io/platformforge/inventory-service   0.1.0   3274a1f332e0b   28.5MB
docker.io/platformforge/orders-service      0.1.0   b35369790bfdb   28.5MB
```

Docker Desktop's `docker image ls` reports 130 MB per image (its containerd store counts
differently); 28.5 MB is the size as stored on the kind node.

## M2.2 Hardened local container smoke test (before Kubernetes)

Both containers ran with `--read-only --tmpfs /tmp --cap-drop ALL --security-opt
no-new-privileges --memory 128m` on a private Docker network (no published ports):

```
GET inventory-service:8080/inventory/widget -> 200 {"item_id":"widget","name":"Widget","quantity":42,"in_stock":true}
GET ord-smoke:8080/orders/check/widget      -> 200 {"item_id":"widget","orderable":true,"quantity_available":42}
GET ord-smoke:8080/orders/check/gadget      -> 200 {"item_id":"gadget","orderable":false,"quantity_available":0}
GET ord-smoke:8080/orders/check/nope        -> 404 {"detail":{"error":"item_not_found","request_id":"smoke-28302"}}
GET ord-smoke:8080/orders/check/BAD%20ID    -> 422 (pattern mismatch; inventory not called)
--- inventory container stopped
GET orders/check/widget -> 503 {"detail":{"error":"inventory_unavailable",...,"request_id":"smoke-down"}}
GET orders/readyz       -> 200 {"status":"ready"}
orders-service memory: 37.66MiB / 128MiB
```

This informed the manifest sizing (requests 64Mi, limits 128Mi).

## M2.3 Unit tests

```
$ pytest -q tests/services
77 passed, 1 warning in 0.28s
```

| File | Tests | Covers |
| --- | --- | --- |
| `tests/services/test_common.py` | 33 | request-ID validation (incl. newline/quote injection), JSON formatter, record factory, `configure_logging`, `env_int`, health/ready/version, readiness before start/after shutdown, docs disabled, request-ID echo/generation, probe log suppression, context reset |
| `tests/services/test_inventory.py` | 9 | found / out of stock / 404 / invalid IDs (422) |
| `tests/services/test_orders.py` | 35 | upstream URL + header propagation, every failure mapping (503/504/502), no retries/redirects, warning log context, invalid IDs never reach upstream, readiness independent of inventory, timeout/base URL wiring, in-process end-to-end with the real inventory app, settings validation |

The warning is Starlette 1.9's `StarletteDeprecationWarning` recommending `httpx2` for its
TestClient. It is test-only and was left visible rather than suppressed.

## M2.4 PlatformForge manifest validation (existing CLI, unchanged)

```
$ platformforge validate services/manifests/inventory-service.yaml
OK: inventory-service (namespace=platformforge-dev, image=platformforge/inventory-service:0.1.0, replicas=2, port=8080)
$ platformforge validate services/manifests/orders-service.yaml
OK: orders-service (namespace=platformforge-dev, image=platformforge/orders-service:0.1.0, replicas=2, port=8080)
```

## M2.5 Integration tests (live kind)

**Run 1:** 20 passed, 2 failed (`test_05`, `test_08`). Both failures were the same **test
defect**: the helper converted response headers to a plain `dict`, but the API server proxy
(Go) canonicalizes header names to `X-Request-Id`, so the lookup for `X-Request-ID` returned
`None`. Response bodies in the same run already showed the propagated ID
(`'request_id': 'it-2ac26fe1a1dd4c82'`), and the outage (503) and restore (2/2 available)
both happened. Fixed by lowercasing header names in the helper.

**Run 2 (services file only):** `12 passed in 8.52s`, with this captured output:

```
inventory log: {'level': 'info', 'service': 'inventory-service', 'logger': 'pf.access', 'message': 'request completed',
                'request_id': 'it-0c44b0c1b98d4fe5', 'method': 'GET', 'path': '/inventory/widget', 'status': 200, ...}
orders log:    {'level': 'info', 'service': 'orders-service', 'logger': 'orders', 'message': 'inventory-service responded',
                'request_id': 'it-0c44b0c1b98d4fe5',
                'upstream': 'http://inventory-service.platformforge-dev.svc.cluster.local:8080',
                'path': '/inventory/widget', 'duration_ms': 3.06, 'status': 200, ...}
during outage:  503 {'detail': {'error': 'inventory_unavailable', 'message': 'inventory-service is unavailable', 'request_id': 'it-97bd7772324941a6'}}
after recovery: 200 {'item_id': 'widget', 'orderable': True, 'quantity_available': 42}
```

## M2.6 Manual in-cluster verification (kubectl, independent of test code)

```
$ kubectl -n platformforge-dev get deploy,svc,endpointslices
deployment.apps/inventory-service   2/2   2   2
deployment.apps/orders-service      2/2   2   2
service/inventory-service   ClusterIP   10.96.78.95    <none>   8080/TCP
service/orders-service      ClusterIP   10.96.136.93   <none>   8080/TCP
endpointslice.../inventory-service-4k8ps   IPv4   8080   10.244.0.13,10.244.0.14
endpointslice.../orders-service-gvqvb      IPv4   8080   10.244.0.10,10.244.0.9

$ kubectl exec orders-service-6d99b95c88-jqvp7 -- python3 (urllib, X-Request-ID: manual-check-1)
200 http://inventory-service.platformforge-dev.svc.cluster.local:8080/inventory/widget {"item_id":"widget","name":"Widget","quantity":42,"in_stock":true}
200 http://inventory-service.platformforge-dev.svc.cluster.local:8080/version {"service":"inventory-service","version":"0.1.0","git_sha":"2c15c78-dirty"}
200 http://orders-service.platformforge-dev.svc.cluster.local:8080/orders/check/gizmo {"item_id":"gizmo","orderable":true,"quantity_available":7}
200 http://orders-service.platformforge-dev.svc.cluster.local:8080/orders/check/gadget {"item_id":"gadget","orderable":false,"quantity_available":0}
dns: inventory-service.platformforge-dev.svc.cluster.local -> 10.96.78.95

$ kubectl logs -l app.kubernetes.io/name=inventory-service | grep manual-check-1
{"level": "info", "service": "inventory-service", "logger": "pf.access", "message": "request completed", "request_id": "manual-check-1", "path": "/inventory/gizmo", "status": 200, ...}
(4 lines total: the direct calls and the calls made on behalf of orders-service)

Pod security context (live):  {"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}}
Container security context:   {"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true,"runAsNonRoot":true}
Resources:                    {"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}
/proc/1/status:               Uid 65532, Gid 65532, CapPrm 0000000000000000, CapEff 0000000000000000, NoNewPrivs 1
Write to /app/x:              OSError: [Errno 30] Read-only file system
```

## M2.7 Manual failure and recovery

```
[21:07:47] baseline: 200 {"item_id":"widget","orderable":true,"quantity_available":42}
[21:07:48] $ kubectl -n platformforge-dev scale deployment/inventory-service --replicas=0
[21:07:50] outage probe 1: 504 {"detail":{"error":"inventory_timeout",...,"request_id":"manual-outage-1"}}
[21:07:53] outage probe 2: 503 {"detail":{"error":"inventory_unavailable",...,"request_id":"manual-outage-2"}}
[21:07:53] orders readyz during outage: 200 {"status":"ready"}
           inventory-service 0/0, orders-service 2/2
[21:07:53] $ platformforge deploy services/manifests/inventory-service.yaml --approve-context kind-platformforge
           Deployment/inventory-service: updated ... Rollout complete: 2/2 available
[21:08:00] recovery probe 1: 200 {"item_id":"widget","orderable":true,"quantity_available":42}

orders warning log:
{"level": "warning", "service": "orders-service", "message": "inventory-service unavailable", "request_id": "manual-outage-2",
 "upstream": "http://inventory-service.platformforge-dev.svc.cluster.local:8080", "duration_ms": 1.19, "error": "ConnectError"}
```

Observation: the first probe after scale-down got **504** after the 2 s timeout, not 503.
While inventory pods were terminating, the connection attempt went unanswered; once their
endpoints were removed, connections were refused immediately (1.19 ms) and orders returned
503. Both are the intended mappings. A `preStop` delay would narrow this window (see
limitations). Recovery: about 7 s from the start of `platformforge deploy` to the first 200.

## M2.8 CI workflow

```
$ docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 .github/workflows/ci.yml
(first run) ci.yml:77:9: shellcheck SC2034: i appears unused     -> fixed (loop var renamed to _)
(second run) actionlint_exit=0

$ rg "secrets\.|GOOGLE|gcloud|kubeconfig" .github/
no secrets/GCP/kube references in workflow
```

The workflow has **not run on GitHub** (the repository has no remote). Each job was
replayed locally:

| CI job | Local replay | Result |
| --- | --- | --- |
| Ruff | fresh venv, `pip install -e '.[dev]'`, `ruff check` + `ruff format --check` on src/services/tests | All checks passed; 35 files formatted |
| Unit tests | same fresh venv, `pytest -m "not integration"` | 212 passed, 22 deselected |
| Images (matrix) | `docker build --platform linux/amd64 ...` (emulated, as GitHub runners are amd64), then the exact smoke script (port 18080 locally instead of 8080) | both: `arch=amd64`, user check ok, `/readyz` ready, `/healthz` ok, version check ok |

## M2.9 Final complete run

```
$ ruff check src services tests && ruff format --check src services tests
All checks passed!
35 files already formatted

$ PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -v
tests/integration/test_kind_deploy.py   10/10 PASSED   (Milestone 1)
tests/integration/test_services_kind.py 12/12 PASSED   (Milestone 2)
======================== 234 passed, 1 warning in 9.58s ========================
```

234 = 135 original unit + 10 original integration + 77 service unit + 12 service integration.

Cluster state after the run (`kubectl -n platformforge-dev get deploy,svc,pods`):

```
deployment.apps/demo-web            2/2   2   2
deployment.apps/inventory-service   2/2   2   2
deployment.apps/orders-service      2/2   2   2
service/demo-web            ClusterIP   10.96.89.204   <none>   8080/TCP
service/inventory-service   ClusterIP   10.96.78.95    <none>   8080/TCP
service/orders-service      ClusterIP   10.96.136.93   <none>   8080/TCP
all 6 pods Running, READY 1/1, RESTARTS 0
```

## What Milestone 2 evidence does not show

- CI running on GitHub-hosted runners (not yet pushed).
- arm64 image build in CI (built locally only).
- Behavior under load, or memory headroom beyond a single idle measurement (37.66 MiB).
- The 504 timeout path driven deterministically in-cluster (it occurred naturally during
  scale-down; deterministic coverage is in unit tests).
- Any traffic encryption or service identity (no mesh yet).

# Milestone 3: Istio service mesh

All M3 results were produced on 2026-10-20 against `kind-platformforge`. The current context
was displayed before every cluster command. Output is abridged, not edited.

## M3 environment additions

| Component | Version / value |
| --- | --- |
| Kubernetes server | v1.37.0 (kind, 1 node, arm64, containerd 2.3.4) |
| Node capacity | 12 CPU, 8124256Ki allocatable memory; requests after M3 (Istio + all workloads + test pods): 1630m CPU (13%), 1570Mi (19%) |
| Istio / istioctl | 1.31.2 (supported for Kubernetes 1.32–1.37; 1.30 is not supported on 1.37) |
| istioctl download | `istio-1.31.2-osx-arm64.tar.gz`, SHA-256 `c02f9390ff8300b550c7b13230feae80adebe31aa7136f2930a1918502c626d3`, identical to the release `.sha256` file |
| Images | `platformforge/inventory-service:0.1.0` (unchanged, stable), `inventory-service:0.2.0`, `orders-service:0.2.0` |

## M3.1 Installation (Phase A)

```
$ istioctl x precheck --context kind-platformforge
✔ No issues found when checking the cluster. Istio is safe to install or upgrade!

$ kubectl apply -f deploy/istio/istio-system-namespace.yaml
$ istioctl install -f deploy/istio/istio-operator.yaml --context kind-platformforge -y
(completed; istiod 1/1 and istio-cni-node 1/1 ready, 0 restarts)

$ kubectl -n istio-system get pods      # re-checked after all test runs
istio-cni-node-trql8     true   0   docker.io/istio/install-cni:1.31.2
istiod-748c99cf6-7xj58   true   0   docker.io/istio/pilot:1.31.2

$ istioctl version
client version: 1.31.2
control plane version: 1.31.2
data plane version: 1.31.2 (8 proxies)

$ docker exec platformforge-control-plane cat /etc/cni/net.d/10-kindnet.conflist   # plugin types
['ptp', 'portmap', 'istio-cni']        # Istio CNI chained after kindnet
```

Rendered-manifest review (`istioctl manifest generate`, before install): only `istio-system`
namespaced objects; one ClusterIP Service; injection webhooks only match
`istio-injection=enabled` / `istio.io/rev` namespaces or `sidecar.istio.io/inject=true` pods.

Injection and controlled restarts (one Deployment at a time, each waited to completion):

```
$ kubectl label namespace platformforge-dev istio-injection=enabled
$ kubectl get ns -L istio-injection,pod-security.kubernetes.io/enforce
NAME                 STATUS   AGE   ISTIO-INJECTION   ENFORCE
default              Active   55m
istio-system         Active   23m                     privileged
kube-node-lease      Active   55m
kube-public          Active   55m
kube-system          Active   55m
local-path-storage   Active   55m
platformforge-dev    Active   46m   enabled           restricted
$ kubectl rollout restart / rollout status  deploy/{inventory-service,orders-service,demo-web}
deployment "inventory-service" successfully rolled out   (same for orders-service, demo-web)
$ kubectl get events --field-selector reason=FailedCreate     # none
$ kubectl label --dry-run=server --overwrite ns platformforge-dev pod-security.kubernetes.io/enforce-version=v1.37
namespace/platformforge-dev labeled (server dry run)          # no PSA warnings for any meshed pod
```

Every pod: init containers `istio-validation`, `istio-proxy` (`restartPolicy: Always`, i.e.
native sidecar); `istio-proxy` runs UID 1337, `runAsNonRoot`, `allowPrivilegeEscalation:
false`, read-only root FS, `capabilities.drop: [ALL]`; no `istio-init`. `istioctl
proxy-status`: all six sidecars subscribed to istiod 1.31.2 (CDS, LDS, EDS, RDS).

Existing functionality after injection (still PERMISSIVE): integration suite 20/22.
- `test_kind_deploy::test_09` failed once and was not a regression. My `kubectl rollout
  restart` had added a `restartedAt` annotation, so the suite's first `platformforge deploy`
  caused a real rollout, and `logs` saw the old pods while they terminated. On re-run (no
  annotation) the file passed 10/10.
- `test_services_kind::test_08` failed with a real behavior change. During the outage,
  orders' sidecar returned `503 no_healthy_upstream` (Envoy flag `UH`), and orders v0.1.0
  mapped any non-200 status to `502 inventory_bad_status`. Fixed in orders v0.2.0, which maps
  upstream 503 → 503 `inventory_unavailable`.

## M3.2 Versioned migration of inventory-service (approved deletion)

The user approved deleting only `deployment/inventory-service` after verification gates. A
continuous probe (≈10 req/s, orders pod → orders app → orders sidecar → inventory) ran
21:31:01–21:33:51 UTC across the whole migration.

| Gate | Result |
| --- | --- |
| Before | Service `inventory-service` ClusterIP `10.96.78.95`, uid `a75cea79-…`, selector `{managed-by, name}` |
| `platformforge deploy services/manifests/inventory-service.yaml` | `ServiceAccount/inventory-service: created`, `Deployment/inventory-service-v0-1-0: created`, `Service/inventory-service: updated`, warning about the overlapping legacy Deployment, `Rollout complete: 2/2 available` |
| 1. v0-1-0 Ready | replicas=2 ready=2 available=2 updated=2, generation 1/1 |
| 2. Service routes to new pods | EndpointSlice: 4 ready endpoints (2 legacy, 2 v0.1.0). 40 tagged requests: legacy pods served 25, v0.1.0 pods 3 + 12 (counted by request ID in app logs) |
| 3. Selector matches intended pods | Service selector selects 2 legacy + 2 v0.1.0 pods, all Ready |
| 4. orders → inventory | 40/40 HTTP 200 |
| 5. Service/ClusterIP preserved | `10.96.78.95`, same uid |
| 6. Delete | `kubectl delete deployment inventory-service` (21:31:36); target confirmed as `platformforge-dev/Deployment/inventory-service`, managed-by=platformforge |
| 7. Nothing else deleted | demo-web, inventory-service-v0-1-0, orders-service Deployments; all 3 Services; both new ServiceAccounts present |
| 8. Availability after | 3 × 20 requests after the delete: 60/60 HTTP 200 |

Downtime: **none observed.** The probe's own summary was lost because the background `kubectl
exec` was killed when its launching shell returned. The same traffic was reconstructed from
the orders sidecar access log, by `probe-N` request ID: **1580 requests, 1580 × 200**,
21:31:01.597–21:33:51.437. By upstream pod IP: legacy pods `10.244.0.37` (135, last at
21:31:36.487) and `10.244.0.38` (112, last at 21:31:36.696); v0.1.0 pods `10.244.0.41` (651)
and `10.244.0.42` (682), through to the end. This measures the orders → inventory hop as seen
by orders' sidecar. (Re-derived from the same logs after the test runs, with identical
totals.)

## M3.3 Workload identity (Phase B)

```
$ platformforge deploy services/manifests/orders-service.yaml --approve-context kind-platformforge
ServiceAccount/orders-service: created
Deployment/orders-service: updated
Rollout complete: 2/2 available

$ istioctl proxy-config secret <pod> -o json | (decode "default" cert) | openssl x509 -noout -ext subjectAltName -startdate -enddate
inventory-service-v0-1-0-698799fd78-bx7g5  URI:spiffe://cluster.local/ns/platformforge-dev/sa/inventory-service  Oct 20 21:40:13 → Oct 21 21:42:13 2026 GMT
orders-service-6ccbbfb5f4-6kfpp            URI:spiffe://cluster.local/ns/platformforge-dev/sa/orders-service     Oct 20 21:28:46 → Oct 21 21:30:46 2026 GMT
demo-web-7d4489dfbb-76687                  URI:spiffe://cluster.local/ns/platformforge-dev/sa/default            Oct 20 21:20:49 → Oct 21 21:22:49 2026 GMT
(SAN extension marked critical; demo-web declares no identity and keeps "default")

pods: automountServiceAccountToken=False; projected token audiences=['istio-ca'] only

$ kubectl auth can-i --list --as=system:serviceaccount:platformforge-dev:orders-service
selfsubject{reviews,accessreviews,rulesreviews} create; clustertrustbundles get/list/watch;
non-resource discovery/health URLs get          # Kubernetes defaults for every authenticated user
$ kubectl auth can-i {get pods,list secrets,get configmaps,create pods,list services,
                      get authorizationpolicies.security.istio.io} --as=…:{orders,inventory}-service
no (12/12)
bindings referencing platformforge-dev subjects: []
```

## M3.4 STRICT mTLS (Phase C)

Control (no PeerAuthentication yet, i.e. Istio's default PERMISSIVE), from the non-mesh test
pod `pf-test-client-plain`:

```
PERMISSIVE raw plaintext -> inventory pod IP:8080:  connected=true, received "HTTP/1.1 200 OK ..."
PERMISSIVE plaintext -> inventory Service:   200 {"item_id":"widget",...}
```

After `kubectl apply -f deploy/istio/platformforge-dev/peer-authentication.yaml`:

```
STRICT raw plaintext -> each inventory/orders pod IP: connected=true, no HTTP response (reset)
STRICT plaintext -> inventory Service:    connection reset
STRICT plaintext -> orders Service:       connection reset
STRICT orders identity -> inventory:      200
STRICT mesh client -> orders -> inventory: 200

$ istioctl x describe pod inventory-service-v0-1-0-698799fd78-bx7g5 -n platformforge-dev   # after all runs
DestinationRule: inventory-service ... Matching subsets: v0-1-0 (Non-matching subsets v0-2-0)
VirtualService: inventory-service
   Route to host "inventory-service.platformforge-dev.svc.cluster.local" subset "v0-1-0" with weight 100%
RBAC policies: ns[platformforge-dev]-policy[inventory-service]-rule[0]
Effective PeerAuthentication:
   Workload mTLS mode: STRICT
Applied PeerAuthentication:
   default.platformforge-dev
```

The same request (`mtls-evidence-0`), as logged by both sidecars:

```
orders outbound:    upstream_cluster=outbound|8080||inventory-service…  upstream_tls_version=TLSv1.3
                    upstream_peer_uri_san=spiffe://cluster.local/ns/platformforge-dev/sa/inventory-service
inventory inbound:  downstream_tls_version=TLSv1.3
                    downstream_peer_uri_san=spiffe://cluster.local/ns/platformforge-dev/sa/orders-service
```

inventory's `istio_requests_total` (reporter=destination). At the time of the STRICT checks,
the two v0.1.0 pods showed 688 and 722 requests from `sa/orders-service` with
`connection_security_policy=mutual_tls`, plus exactly 2 with `none`: the two PERMISSIVE
plaintext control requests above. Those pods were replaced during the test runs. The
current pods, re-checked after run 3, show only `mutual_tls`:

```
inventory-service-v0-1-0-…-bx7g5: (orders-service, mutual_tls, 200) 1006, (orders-service, mutual_tls, 403) 1
inventory-service-v0-1-0-…-tgz2t: (orders-service, mutual_tls, 200) 940,  (orders-service, mutual_tls, 403) 3,
                                  (default, mutual_tls, 403) 1
inventory-service-v0-2-0-…-jg29d: no requests since the stable route was restored
```

The 403s are the run 3 authorization negative tests (d3 and d4).

Envoy's raw TLS handshake counters are not exported: Istio's default stats inclusion list
trims them. The TLS evidence above therefore comes from access logs and Istio's standard
metrics.

Operator access under STRICT (checked after run 3):

```
$ kubectl -n platformforge-dev port-forward deploy/orders-service 18080:8080 &
$ curl http://127.0.0.1:18080/orders/check/widget
{"item_id":"widget","orderable":true,"quantity_available":42} HTTP 200
$ curl http://127.0.0.1:18080/version
{"service":"orders-service","version":"0.2.0","git_sha":"afcaf45-dirty"} HTTP 200
```

Additional finding: a meshed client that calls a **pod IP** directly is routed by its own
sidecar through `PassthroughCluster` **without** mTLS (`upstream_tls_version=null`), and STRICT
rejects it (`503`, `UC`, `upstream_reset_before_response_started`). Initially I wrote this
as a positive control, and it failed. The test now asserts the rejection, and the positive
control uses the Service name instead.

## M3.5 Authorization (Phase D)

Policy (`deploy/istio/platformforge-dev/inventory-authorization.yaml`): selector
`app.kubernetes.io/name=inventory-service`, ALLOW, one rule: principal
`cluster.local/ns/platformforge-dev/sa/orders-service`, `GET`, `/inventory/*`, port 8080.

| Caller | Request | Result |
| --- | --- | --- |
| orders-service identity | `GET /inventory/widget` | 200 |
| test client → orders → inventory | `GET /orders/check/widget` × 20 | 20 × 200 |
| `sa/default` (meshed test client) | `GET /inventory/widget` | **403** `RBAC: access denied`; inventory log: `downstream_peer_uri_san=…/sa/default`, `TLSv1.3`, `response_code_details` starts with `rbac_access_denied` |
| orders-service identity | `GET /version`, `GET /healthz`, `POST` and `DELETE /inventory/widget` | **403** each |
| non-mesh plaintext client | any | connection reset (M3.4) |
| `sa/default` → orders-service (not covered by the policy) | `GET /healthz` | 200: the policy is workload-scoped, not namespace-wide |
| kubelet | readiness/liveness | probes rewritten to `:15020/app-health/inventory-service/{readyz,livez}`; pods Ready, 0 restarts; `platformforge status` HEALTHY |

Only one AuthorizationPolicy exists in the namespace. `istioctl analyze -n platformforge-dev`:
`✔ No validation issues found`.

## M3.6 Canary release (Phase E)

`deploy/istio/platformforge-dev/inventory-destinationrule.yaml` (subsets by
`app.kubernetes.io/version`), then `platformforge deploy services/manifests/inventory-service-v0.2.0.yaml`:
`Deployment/inventory-service-v0-2-0: created`, 1/1 available. inventory Service then had
3 Ready endpoints (2 × v0.1.0, 1 × v0.2.0).

Counts are `istio_requests_total` deltas from both orders sidecars (reporter=source), labeled
by `destination_version`. Each sample was taken after the live Envoy route config on both
orders sidecars showed the expected weights.

| Routing | N | v0.1.0 | v0.2.0 | Non-200 |
| --- | --- | --- | --- | --- |
| stable (100/0), control, runs 1–4 | 200 each | 200 | **0** | 0 |
| canary (90/10), run 1 | 1000 | 902 | 98 (9.8%) | 0 |
| canary (90/10), run 2 | 1000 | 887 | 113 (11.3%) | 0 |
| canary (90/10), run 3 | 1000 | 919 | 81 (8.1%) | 0 |
| canary (90/10), run 4 (final) | 1000 | 912 | 88 (8.8%) | 0 |
| **canary total** | 4000 | 3620 | **380 (9.5%)** | 0 |

Binomial expectation per sample: 100 ± 9.49 (1 sd). The samples lie at −0.2, +1.4, −2.0,
and −1.3 sd. Pod-proportional balancing would give ≈333. See docs/CANARY_RELEASE.md.

## M3.7 Canary failure and recovery (Phase F)

| Step | Run 1 | Run 2 | Run 3 | Run 4 (final) |
| --- | --- | --- | --- | --- |
| Faulty canary (`INVENTORY_FAILURE_PERCENT=100`), 500 requests at 90/10 | 458 × 200, **42 × 502** | 451 × 200, **49 × 502** | 454 × 200, **46 × 502** | 456 × 200, **44 × 502** |
| Mesh view of the same window | v0.1.0 200 × 458; v0.2.0 500 × 42 | v0.1.0 200 × 451; v0.2.0 500 × 49 | v0.1.0 200 × 454; v0.2.0 500 × 46 | v0.1.0 200 × 456; v0.2.0 500 × 44 |
| Apply stable VirtualService: sidecars converged | 1.1 s | 1.1 s | 1.1 s | 1.1 s |
| 300 requests after remediation | 300 × 200, v0.2.0: 0 | same | same | same |
| Redeploy healthy v0.2.0, keep stable routing | env `0`, in-pod lookup 200, VS 100/0, 50 × 200 | same | same | same |

All 502 bodies were `inventory_bad_status`, and the canary pod logged `injected failure`
for each. See docs/INCIDENT_RUNBOOK.md.

## M3.8 Unit tests

```
$ pytest -m "not integration"
289 passed, 55 deselected
```

| File | Tests | Covers |
| --- | --- | --- |
| `tests/unit/test_mesh_fields.py` (new) | 65 | `version` / `serviceAccountName` / `termination`: accepted values; rejection tests (bad versions, non-string, name length boundary 63/64, SA ≠ metadata.name incl. `default`/`orders-service`/`istiod`, snake_case key, termination bounds and ordering, `command` smuggling, other identity/label fields, allowlist still enforced); rendering (versioned names/labels/selectors, Service spans versions, identity-only SA, preStop sleep, grace period, unchanged output when unset, security defaults kept, namespace injection label); deployer (SA before Deployment, SA ownership preflight and 409 re-check, versioned preflight/status/logs/rollout names, overlap detection, allowlist re-check); CLI (dry-run includes SA, validate output) |
| `tests/services/test_inventory.py` | 9 → 18 | failure knob off by default, 100% fails lookups but not health, partial %, invalid env/argument rejected |
| `tests/services/test_orders.py` | 35 → 38 | upstream 503 → 503 `inventory_unavailable`, 504 → 504 `inventory_timeout`, 403 → 502 `inventory_forbidden`, 401 → 502 `inventory_bad_status` |

All 212 pre-existing unit tests still pass. One M2 case was replaced: upstream 503 used to
give `502 inventory_bad_status` and now gives `503 inventory_unavailable` (an intended
behavior change).

## M3.9 Integration tests

`tests/integration/test_services_kind.py` was migrated from the API-server proxy (which
STRICT now blocks) to in-cluster calls from the meshed test client. Its assertions are
otherwise kept: orders reports version 0.2.0; inventory is now `inventory-service-v0-1-0`;
the outage test scales every inventory version to 0 and restores each. The two
parametrized `test_04` cases became one test (inventory endpoints are checked in-pod), so
the file has 11 tests instead of 12.

`tests/integration/test_services_mesh.py` (new, 34 tests): Phases A–F as above.

```
$ PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration -v
run 1: 53 passed, 1 failed   (test_c3: the pod-IP passthrough finding in M3.4; test rewritten)
run 2 (full suite): 344 passed in 68.96s
run 3: 55 passed, 289 deselected in 62.70s
run 4 (final full suite, below): 344 passed in 69.08s
```

## M3.10 Final complete run (after all documentation changes)

```
$ make lint
.venv/bin/ruff check src services tests
All checks passed!
.venv/bin/ruff format --check src services tests
38 files already formatted

$ kubectl config current-context
kind-platformforge
$ PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -v -s
tests/integration/test_kind_deploy.py     10/10 PASSED   (Milestone 1)
tests/integration/test_services_kind.py   11/11 PASSED   (Milestone 2, migrated)
tests/integration/test_services_mesh.py   34/34 PASSED   (Milestone 3)
test_c2[inventory-service] inventory-service-v0-1-0-…-qxfkw {'connected': True, 'error': 'ConnectionResetError: [Errno 104] Connection reset by peer'}
test_e3 canary split over 1000: v0.1.0=912 (91.2%) v0.2.0=88 (8.8%)
test_f1 during incident: client statuses={200: 456, 502: 44} mesh delta={('v0.1.0', '200'): 456, ('v0.2.0', '500'): 44}
test_f2 sidecars converged on stable routing in 1.1s; after remediation: {('v0.1.0', '200'): 300}
=================== 344 passed, 1 warning in 69.08s (0:01:09) ===================
```

The one warning is Starlette's TestClient deprecation notice (see M2).

344 = 289 unit + 55 integration.
- Unit: 212 pre-M3 plus 77 new (65 mesh fields, 9 inventory, 3 orders). By file: manifest
  80, deployer 23, CLI 16, kubecontext 8, resources 8, mesh fields 65, services common 33,
  inventory 18, orders 38.
- Integration: 10 M1 + 11 M2 (migrated) + 34 M3. All 234 pre-M3 test scenarios still run;
  the only count change is the merged `test_04`.

Cluster state after the runs:

```
deployment.apps/demo-web                   2/2   nginxinc/nginx-unprivileged:1.29-alpine
deployment.apps/inventory-service-v0-1-0   2/2   platformforge/inventory-service:0.1.0
deployment.apps/inventory-service-v0-2-0   1/1   platformforge/inventory-service:0.2.0
deployment.apps/orders-service             2/2   platformforge/orders-service:0.2.0
service/{demo-web,inventory-service,orders-service}   ClusterIP (10.96.89.204, 10.96.78.95, 10.96.136.93)
serviceaccount/{default,inventory-service,orders-service}
all pods Ready, RESTARTS 0, sidecar Ready (except the plaintext test client, by design)
peerauthentication/default STRICT; authorizationpolicy/inventory-service ALLOW;
destinationrule/inventory-service; virtualservice/inventory-service v0-1-0=100 v0-2-0=0
namespaces with istio-injection=enabled: platformforge-dev only
```

# Milestone 4: observability

Date: 2026-10-20. Context: `kind-platformforge`. No cluster was recreated. Docker memory
was not increased.

## M4.1 Capacity and install

Before the stack, the kind node was using about 1.9 GiB of 7.748 GiB and allocated
requests were 1630m CPU / 1570Mi memory. After install, `kubectl describe node` reported
requests 1860m CPU (15%) and 2562Mi memory (32%), limits 9350m CPU (77%) and 6420Mi
memory (80%). `docker stats` showed `platformforge-control-plane` at 3.792 GiB / 7.748 GiB.

`helm list -n platformforge-observability`:

| Release | Chart | App |
| --- | --- | --- |
| prometheus | prometheus-29.37.0 | v3.15.0 |
| grafana | grafana-14.0.1 | 13.2.4 |
| loki | loki-18.17.1 | 3.7.8 |
| tempo | tempo-3.2.0 | 3.1.0 |
| alloy | alloy-1.14.0 | v1.21.0 |

All five pods were Ready. Alloy's first install crash-looped with `mkdir /tmp/alloy: read-only file system`. Revision 2 mounts an emptyDir there. Grafana, Loki, Tempo, and Prometheus came up on the first install.

From `pf-test-client-plain`, TCP to `tempo` ports 9411 and 14268 timed out. Ports 4318 and 3200 connected. VirtualService `inventory-service` was left at v0-1-0 weight 100, v0-2-0 weight 0.

## M4.2 Metrics, logs, traces, Grafana

Prometheus active targets included orders-service and both inventory Deployments, scrape URL `http://<pod ip>:15020/stats/prometheus`, health `up`. A query of `istio_requests_total` returned source `orders-service` to destination workload `inventory-service-v0-1-0`, `destination_version=v0.1.0`, `response_code=200`, and orders-service as a destination (`destination_version=unknown`, because that manifest sets no `spec.version`).

Grafana `/api/health` returned database ok, version 13.2.4. Datasource health for Prometheus, Loki, and Tempo returned status OK. The dashboard **PlatformForge services** rendered in a browser at `http://127.0.0.1:3000/d/platformforge-overview` with request-rate series for inventory `v0.1.0` / `v0.2.0` and orders, and the sidecar-target stat showing 8.

`pytest tests/integration/test_observability.py -s` on 2026-10-20:

```
trace: request_id=obs-trace-31028d8d76aa trace_id=20f98e2b62278403629b59cfd4da5f07 orders_ts=2026-10-20T22:39:11.544+00:00 inventory_ts=2026-10-20T22:39:11.543+00:00
drill: client_502=1 client_200=39 prom_v0.2.0_5xx 54.0->55.0 prom_v0.1.0_5xx 0->0
recovery: prom_v0.2.0_5xx stayed 55.0 -> 55.0
5 passed in 92.80s
```

The 54 already counted an earlier drill in the same Prometheus process. This sample added one inventory `v0.2.0` 5xx and no `v0.1.0` 5xx. After the VirtualService returned to stable and 20 further requests returned HTTP 200, the `v0.2.0` 5xx counter stayed at 55. The test then redeployed the healthy canary.

## M4.3 Complete suite

`PLATFORMFORGE_IT_CONTEXT=kind-platformforge .venv/bin/pytest -q` on the final tree:

```
352 passed, 1 warning in 170.00s
```

An earlier run of the same command, before the observability test printed the drill line above, was `352 passed, 1 warning in 153.19s`. Both are 292 unit tests and 60 integration tests (`pytest --collect-only -m integration` collected 60/352). The warning is the pre-existing Starlette `httpx` TestClient deprecation. No test was skipped. `ruff check` and `ruff format --check` on `src`, `services`, and `tests` passed.

## What Milestone 4 evidence does not show

- Retention across a pod restart. The stores are emptyDir.
- A production sampling rate. This lab samples 100% of `platformforge-dev`.
- Authentication on Prometheus, Loki, or Tempo.
- GKE, Terraform, or any billable cloud resource.

# Milestone 5: GKE Terraform (not provisioned)

Date: 2026-10-20. No `terraform apply`, no `terraform plan`, no `terraform destroy`,
and no `gcloud` create. The kind cluster was not contacted. No GCP resource was
created. Application tests were not re-run; no Python or Kubernetes manifest under
`src/`, `services/`, `deploy/`, or `tests/` was changed.

## M5.1 Local tools and GCP account

```
$ terraform version
Terraform v1.16.5
on darwin_arm64

$ gcloud version
Google Cloud SDK 590.0.0

$ gcloud auth list
No credentialed accounts.

$ gcloud config get-value project
(unset)
```

`gcloud config list` showed only `disable_usage_reporting = True` and the active
configuration `default`. There was no account and no project to query for GKE
versions, quotas, or the billing catalog. GKE channel versions cited in
`docs/GKE_DEPLOYMENT.md` are from the public release notes, not from
`gcloud container get-server-config`.

## M5.2 Format, init, validate

Working directory `infra/terraform/`. Provider `hashicorp/google` 8.7.0 was
installed by init and recorded in `.terraform.lock.hcl`.

```
$ terraform fmt -check -recursive
(no output, exit 0)

$ terraform init -backend=false -input=false
Initializing modules...
- iam in modules/iam
- network in modules/network
- gke in modules/gke
Initializing provider plugins...
- Finding hashicorp/google versions matching "~> 8.7"...
- Installing hashicorp/google v8.7.0...
- Installed hashicorp/google v8.7.0 (signed by HashiCorp)
Terraform has been successfully initialized!

$ terraform validate
Success! The configuration is valid.
```

`init -backend=false` does not configure the local backend and does not talk to
GCP. `validate` checks configuration and provider schema. It does not execute
plan-time preconditions (CIDR overlap, zone inside region) and it does not
prove the stack can be created.

Earlier schema failures, fixed before the successful validate:

- `google_compute_subnetwork.stack_type = "IPV4"` was rejected. The subnet
  argument accepts `IPV4_ONLY`. The cluster's `ip_allocation_policy.stack_type`
  remains `IPV4`, which this provider accepts.
- `cidrcontains` is not a function in Terraform 1.16.5. Overlap is computed
  from integer bounds.
- `enable_autopilot = false` conflicted with `remove_default_node_pool` and
  other Standard-only arguments. The argument is omitted. The API default is
  Standard.

## M5.3 CIDR arithmetic

The overlap precondition is not executed by `validate`. The same integer bounds
the configuration uses were evaluated in Python for the default CIDRs. Every
pair was disjoint. `10.10.0.0/22` overlapped an injected `10.10.1.0/24`, so the
comparison is capable of failing. `172.16.0.32/28` covered 16 addresses
(2886729760 through 2886729775). This is not a `terraform plan`.

## M5.4 Checkov

checkov 3.3.30 was installed in a temporary virtualenv outside the repository
and pointed at `infra/terraform`, skipping `.terraform`. It is not a project
dependency.

```
Passed checks: 59, Failed checks: 4, Skipped checks: 0
```

| Check | Resource | Disposition |
| --- | --- | --- |
| CKV_GCP_65 Google Groups for GKE | cluster | Accepted. No Workspace domain is available. A fabricated group name would not be a real control |
| CKV_GCP_66 Binary Authorization | cluster | Accepted. Images are unsigned and are not in a registry. Enforcement would block pods. The API is not enabled |
| CKV_GCP_69 GKE Metadata Server | cluster | The explicit node pool sets `mode = GKE_METADATA` and was not in the failed set. The cluster resource has no `node_config` because `remove_default_node_pool` is true. Not "fixed" by adding `node_config` on a pool Terraform deletes |
| CKV_GCP_26 subnet flow logs | subnet | Accepted. Flow logs are vended and have no free allotment. See docs/CLOUD_COSTS.md |

An earlier scan, before `variables.tf` parsed and before Dataplane V2 was
declared the way checkov expects, also failed CKV_GCP_61 (intranode
visibility), CKV_GCP_12 (network policy), and CKV_GCP_21 (labels), and reported
a parse error on `variables.tf` (`&&` at the start of a line). The final scan
had no parse error. `enable_intranode_visibility = true` and
`network_policy { enabled = false }` with `datapath_provider = ADVANCED_DATAPATH`
cleared CKV_GCP_61 and CKV_GCP_12 without enabling the Calico add-on.
`var.labels` cleared CKV_GCP_21 once the variables file parsed.

## What Milestone 5 evidence does not show

- `terraform plan` or `terraform apply`. No cluster, VPC, NAT gateway, or IAM
  binding exists because of this change.
- That the overlap precondition or the zone-in-region precondition pass inside
  Terraform. Only the equivalent arithmetic was run, and only for the defaults.
- Quotas, org policies, or the caller's IAM.
- A price from this project's billing account. docs/CLOUD_COSTS.md is list
  prices from public pages.
- Istio or the observability stack on GKE.
- Any change to the kind cluster or to the 352 passing tests recorded for
  Milestone 4.

## What Milestone 3 evidence does not show

- Anything on GKE, multi-node, or multi-cluster; only a single-node kind cluster.
- Behavior under load. The largest sample was 1000 sequential requests.
- Certificate rotation, root CA rotation, or an external CA. Only the issued 24h
  certificates were observed.
- Packet-level capture of ciphertext. Encryption is evidenced by both Envoys' TLS version,
  peer certificates, and Istio's `connection_security_policy`, not by tcpdump.
- Authorization for orders-service or demo-web; only inventory-service has a policy.
- Egress restrictions or NetworkPolicy.

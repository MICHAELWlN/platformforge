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

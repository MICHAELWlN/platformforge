# Test Evidence

All results below were produced on 2026-10-20 on the development machine. Output is
abridged; nothing was edited to change a result.

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
  evidence of serving).

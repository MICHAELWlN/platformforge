# MCP live integration on kind

The read-only server was exercised against `kind-platformforge` on 2026-10-21.
The current kubeconfig context stayed
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.
No GKE object was changed. Application Deployments and Istio policy were not
edited.

## RBAC

`deploy/kind/mcp-reader.yaml` creates `platformforge-mcp-reader` in
`platformforge-dev`:

- ServiceAccount, with token automount off.
- Role: get and list pods, services, and events; get `pods/log`; get
  endpoints; get and list `apps/deployments`.
- RoleBinding to that ServiceAccount in the same namespace.

`watch` is not granted. Secrets, ConfigMaps, exec, scale, and every other
namespace are outside the Role. Apply it only with
`--context kind-platformforge`. Do not apply this file to GKE.

## Authentication

The live test asks the kind API for a one-hour TokenRequest. That issuance
uses the kind admin client. The MCP client then loads a temporary kubeconfig
whose only user is `platformforge-mcp-reader` and whose only credential is
the token. `PLATFORMFORGE_MCP_KUBECONFIG` points at that file.
`persist_config` is false, so the operator kubeconfig is not rewritten.
The file is mode `0600`, lives under the test temp directory, and is not
committed.

The server rejects the file unless the context is `kind-platformforge` and
the user is `platformforge-mcp-reader`.

SelfSubjectAccessReview with the token allowed pod, log, Service, Endpoints,
Event, and Deployment reads in `platformforge-dev`. It denied Secret reads,
pod exec, Deployment patch, scale, and pod list in
`platformforge-observability`. A direct Secret read, Deployment patch, and
pod exec each returned HTTP 403. The patch did not change `orders-service`.

## Port-forwards

The operator, not the reader, opens these tunnels. They bind `127.0.0.1`:

```bash
kubectl --context kind-platformforge -n platformforge-observability \
  port-forward --address 127.0.0.1 svc/prometheus-server 9090:80
kubectl --context kind-platformforge -n platformforge-observability \
  port-forward --address 127.0.0.1 svc/loki 3100:3100
kubectl --context kind-platformforge -n platformforge-observability \
  port-forward --address 127.0.0.1 svc/tempo 3200:3200
```

```bash
export PLATFORMFORGE_PROMETHEUS_URL=http://127.0.0.1:9090
export PLATFORMFORGE_LOKI_URL=http://127.0.0.1:3100
export PLATFORMFORGE_TEMPO_URL=http://127.0.0.1:3200
```

During the check, `/-/ready`, `/ready`, and Tempo `/ready` each returned
HTTP 200. Stop the three `kubectl port-forward` processes when finished.
Do not replace them with a NodePort or LoadBalancer.

## Run the suite

```bash
make test
PLATFORMFORGE_MCP_LIVE=1 .venv/bin/pytest tests/integration/test_mcp_live.py -v
```

`make test` skips the live module. `make mcp-live` runs only that file.
On 2026-10-21, `make test` was 351 passed and 66 skipped. The live file was
2 passed.

## Tool results

All twelve tools were called through the MCP client as the reader.

| Tool | Result |
| --- | --- |
| `list_pods` | Nine pods Running, 0 restarts. Mesh pods 2/2, including native `istio-proxy`. `pf-test-client-plain` 1/1 |
| `describe_pod` | Canary containers `istio-proxy` and `inventory-service` |
| `get_pod_logs` | Untrusted tail. No request id in the last 15 minutes |
| `list_deployments` | demo-web 2/2, inventory v0.1.0 2/2, inventory v0.2.0 1/1, orders 2/2 |
| `get_deployment_status` | Canary Available. Image `platformforge/inventory-service:0.2.0`, `immutable: false` |
| `list_kubernetes_events` | 0 events |
| `get_service_status` | `inventory-service` ClusterIP, port 8080, 3 ready addresses, `public: false` |
| `query_prometheus` | `istio_requests_total` source series: 200=4269, 403=14, 500=112, 404=2, 503=2 |
| `query_loki` | 3 lines in 15 minutes, all `container=istio-proxy` |
| `query_tempo` | No retained trace. A 32-zero id was reported missing |
| `get_service_health` | `kubernetes_ready: true`, Prometheus present |
| `investigate_incident` | `verified_root_cause: null` |

`list_pods` for `kube-system` was rejected by the namespace allowlist.

## Investigation

Verified from the API: both inventory Deployments have every desired replica
available, and the three inventory pods are Running 2/2 with 0 restarts.

Supporting metric, not a cause: the Prometheus counter for
`destination_service_name="inventory-service"` and `reporter="source"` still
holds 112 samples labeled 500 and 2 labeled 503. Those are cumulative
counters from earlier traffic, including the prior failure drill. This pass
did not send a new failing request.

Loki's last 15 minutes for `app="inventory-service"` are three sidecar lines:

```text
info xdsproxy connected to delta upstream XDS server: istiod.istio-system.svc:15012
```

The same window for `container="inventory-service"` returned no streams.
Tempo search returned no traces. There is no request id or trace id in the
retained window to join a 500 to one span. The report's only explanation is:

```text
Unverified hypothesis: HTTP 5xx samples are present. They are not a verified root cause.
```

Missing:

```text
Tempo: trace id was not found; this is not a healthy zero.
```

## Cleanup

Stop the port-forwards. Leave the reader ServiceAccount in place unless a
human asks to remove it. Removal is:

```bash
kubectl --context kind-platformforge -n platformforge-dev \
  delete serviceaccount,role,rolebinding platformforge-mcp-reader
```

That delete was not run.

# MCP live integration on GKE

The read-only server was exercised against
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev` on 2026-10-21.
The kind cluster was not contacted. No Deployment, VirtualService, or
AuthorizationPolicy was edited. No GCP resource was created.

## RBAC

`deploy/gke/mcp-reader.yaml` creates `platformforge-mcp-reader` in
`platformforge-dev` on this cluster only. It is a different object from the
kind ServiceAccount and does not reuse that token.

The Role grants get and list on pods, services, and events, get on
`pods/log` and endpoints, and get and list on deployments. It does not grant
`watch`, Secrets, exec, patch, scale, or delete. Token automount is off.

Apply it only after checking the current context is the pinned GKE context:

```bash
kubectl config current-context
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev \
  apply -f deploy/gke/mcp-reader.yaml
```

## Authentication

The operator client requests a one-hour TokenRequest. The MCP client then
loads a temporary kubeconfig whose only user is `platformforge-mcp-reader`
and whose only credential is that token. The file has no
`gke-gcloud-auth-plugin` exec block and no client certificate.
`PLATFORMFORGE_MCP_KUBECONFIG` points at it. `persist_config` is false.

A GKE tool call without that variable is rejected. The server does not fall
back to the operator kubeconfig for the pinned GKE context. The kubeconfig
user must be `platformforge-mcp-reader`.

SelfSubjectAccessReview with the token allowed the reads above and denied
Secrets, exec, patch, scale, delete, and pod list in
`platformforge-observability` and `kube-system`. Direct Secret read,
Deployment patch, pod delete, and pod exec each returned HTTP 403.

## Port-forwards

The operator opens these tunnels. They bind `127.0.0.1`:

```bash
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev \
  -n platformforge-observability port-forward --address 127.0.0.1 \
  svc/prometheus-server 9090:80
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev \
  -n platformforge-observability port-forward --address 127.0.0.1 svc/loki 3100:3100
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev \
  -n platformforge-observability port-forward --address 127.0.0.1 svc/tempo 3200:3200
```

During the check, Prometheus `/-/ready`, Loki `/ready`, and Tempo `/ready`
each returned HTTP 200. Stop the three processes when finished.

## Run

```bash
make test
PLATFORMFORGE_MCP_GKE=1 .venv/bin/pytest tests/integration/test_mcp_gke.py -v
```

`make test` skips this module. `make mcp-gke` runs only that file.
On 2026-10-21, `make test` was 352 passed and 68 skipped. The GKE file was
2 passed in 3.77s.

## Tool results

| Tool | Result |
| --- | --- |
| `list_pods` | Inventory canary Running 2/2, 0 restarts, on the GKE node |
| `describe_pod` | Containers `istio-proxy` and `inventory-service` |
| `get_pod_logs` | Untrusted tail. No request id in 15 minutes |
| `list_deployments` | inventory v0.1.0 2/2, inventory v0.2.0 1/1, orders 2/2 |
| `get_deployment_status` | Canary Available. Image digest `sha256:e995d076aa876ac8c8d1b854e9364256b83a52162a871c63bfefffb28778abe4`, `immutable: true` |
| `list_kubernetes_events` | 0 events |
| `get_service_status` | `inventory-service` ClusterIP, port 8080, 3 ready addresses, `public: false` |
| `query_prometheus` | `istio_requests_total` source series: 200=457, 403=7, 500=26 |
| `query_loki` | 0 lines for `app="inventory-service"` in 15 minutes |
| `query_tempo` | No retained trace |
| `get_service_health` | `kubernetes_ready: true`, Prometheus present |
| `investigate_incident` | `verified_root_cause: null` |

`list_pods` for `kube-system` was rejected by the namespace allowlist.

## Investigation

Verified from the API: both inventory Deployments are fully available and the
pods are Running 2/2 with 0 restarts. The canary image is digest-pinned.

The Prometheus counter still holds 26 samples labeled 500 and 7 labeled 403.
Those are cumulative samples from earlier traffic. This pass did not send a
failing request. The report labels the 5xx samples as an unverified hypothesis.

Loki has no `inventory-service` lines in the last 15 minutes. The same window
contains two `istio-proxy` lines, for `orders-service` and
`pf-test-client-mesh`, both xdsproxy connections. Tempo search returned no
traces. There is no request id or trace id to join a 500 to one span.

After the run, inventory routing was still `v0-1-0=100` and `v0-2-0=0`.

## Cleanup

Stop the port-forwards and delete the temporary kubeconfig. The one-hour
token was not written into the repo. Leave the ServiceAccount unless a human
asks to remove it:

```bash
kubectl --context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev \
  -n platformforge-dev delete serviceaccount,role,rolebinding platformforge-mcp-reader
```

That delete was not run.

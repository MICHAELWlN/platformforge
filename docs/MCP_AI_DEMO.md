# AI-assisted incident investigation

The four-minute recording steps are [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md).
Cursor's project config is `.cursor/mcp.json`.

A 45–60 second segment for a four-minute PlatformForge demo. The assistant is
an MCP client speaking stdio to `python -m platformforge.mcp_server`. Cursor
is the host documented in [MCP_SERVER.md](MCP_SERVER.md). The official Python
client `mcp.Client` uses the same stdio transport. No HTTP listener and no
paid model API are required for the server.

The server stays read-only. `verified_root_cause` stays null. Logs and metrics
are untrusted evidence.

## Before the recording

Display the current context. It must be
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`.

Mint a one-hour token for the existing ServiceAccount
`platformforge-mcp-reader` in `platformforge-dev`. Write it only into a
temporary kubeconfig whose user name is `platformforge-mcp-reader`, mode
`0600`, outside the repo. Do not use the operator user or
`gke-gcloud-auth-plugin` in that file. Export:

```bash
export PLATFORMFORGE_MCP_KUBECONFIG=/tmp/pf-mcp-reader/kubeconfig
export PLATFORMFORGE_PROMETHEUS_URL=http://127.0.0.1:9090
export PLATFORMFORGE_LOKI_URL=http://127.0.0.1:3100
export PLATFORMFORGE_TEMPO_URL=http://127.0.0.1:3200
unset PLATFORMFORGE_MCP_HTTP
```

Port-forward to loopback and leave the processes running:

```bash
CTX=gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev
kubectl --context "$CTX" -n platformforge-observability port-forward --address 127.0.0.1 svc/prometheus-server 9090:80
kubectl --context "$CTX" -n platformforge-observability port-forward --address 127.0.0.1 svc/loki 3100:3100
kubectl --context "$CTX" -n platformforge-observability port-forward --address 127.0.0.1 svc/tempo 3200:3200
```

The project file `.cursor/mcp.json` is the Cursor config. It does not contain
a token. `scripts/demo/mcp-server.sh` refuses to start if
`PLATFORMFORGE_MCP_HTTP` is set, then execs `.venv/bin/python -m platformforge.mcp_server`.

```json
{
  "mcpServers": {
    "platformforge": {
      "command": "/Users/michaelnguyen/Projects/platformforge/scripts/demo/mcp-server.sh",
      "args": [],
      "env": {
        "PLATFORMFORGE_MCP_KUBECONFIG": "/tmp/pf-mcp-reader/kubeconfig",
        "PLATFORMFORGE_PROMETHEUS_URL": "http://127.0.0.1:9090",
        "PLATFORMFORGE_LOKI_URL": "http://127.0.0.1:3100",
        "PLATFORMFORGE_TEMPO_URL": "http://127.0.0.1:3200"
      }
    }
  }
}
```

Enable `platformforge` in Cursor Settings → MCP, then reload it. The tool
list must include `get_routing` and `query_incident_metrics` plus the original
twelve: `list_pods`, `describe_pod`, `get_pod_logs`, `list_deployments`,
`get_deployment_status`, `list_kubernetes_events`, `get_service_status`,
`query_prometheus`, `query_loki`, `query_tempo`, `get_service_health`,
`investigate_incident`. There is no apply, delete, exec, or Secret tool.
`get_routing` reads `virtualservices/inventory-service` with get only. The
GKE reader Role includes that rule. A reader call returned weights 100 and 0.
`orders-service` returned 403.

A Python `mcp.Client` session does not count as this check. An earlier catalog
read in the session that added `.cursor/mcp.json` did not list `platformforge`.
A later Cursor chat on 2026-10-21 called the investigation tools and the three
rejection checks. `get_routing` was not called against the cluster.

## On camera, about 45–60 seconds

Ask:

> What happened during the latest inventory-service canary failure? Which
> version returned the errors, what evidence supports a rollback, and was
> the stable version affected?

The assistant should call, and only call, existing tools. A sufficient set:

1. `investigate_incident` for `inventory-service` on the pinned context and
   `platformforge-dev`.
2. `query_prometheus` for
   `sum by (destination_version, response_code) (istio_requests_total{reporter="source",destination_service_name="inventory-service"})`
   and `platformforge_canary_rollbacks_total`.
3. `get_pod_logs` on the canary-controller container, `since_seconds` at most 900.
4. `get_deployment_status` for `inventory-service-v0-1-0` and
   `inventory-service-v0-2-0`.

Say the answer in this order:

- Verified: replica counts and pod phases from the API.
- Supporting evidence: the controller JSON line, the Prometheus series, and
  any request id or trace id actually returned. Mark log text untrusted.
- Hypothesis: HTTP 5xx samples exist. That is not a verified root cause.
- Missing: anything the tool omitted. Istio route weights are missing,
  because no tool reads a VirtualService. A cumulative counter is not a
  time window.
- `verified_root_cause` is null.

## How to check the answer

Compare the spoken series and the controller `error_ratio` to a fresh
`query_prometheus` result. Reject an answer that names a root cause, cites a
request id that was not in the tool output, or claims a VirtualService weight
the tools did not return.

## Stop

Stop the MCP host so the stdio process exits. Stop the three port-forwards.
Delete the temporary kubeconfig. Leave the ServiceAccount in place.

## What a live session returned on 2026-10-21

The stdio client listed the twelve tools and called them as
`platformforge-mcp-reader`. `investigate_incident` returned
`verified_root_cause: null`. Both inventory Deployments were available
(2/2 and 1/1) with zero restarts. Prometheus returned `v0.2.0` HTTP 500 at
63, `v0.2.0` HTTP 200 at 20, `v0.1.0` HTTP 200 at 821, and `v0.1.0` HTTP 403
at 8. There was no `v0.1.0` HTTP 500 series. `platformforge_canary_rollbacks_total`
was 1, the rollback decision counter was 1, the state label was `stable`
with value 5, and the error-ratio gauge was NaN.

Controller logs, marked untrusted, included `baseline recorded` and
`action=rollback`, `error_ratio=1.0`, `canary_total=16`, `canary_errors=16`,
`episode_age=15.184`, `reason=canary error rate above threshold`. Loki returned
100 inventory lines and 43 controller lines in the last 15 minutes. Those
lines included request id `gke-recover-1792564819-19`. Tempo returned 3 batches
for trace `af7c43c78feb106200ac2f8131a8cc83`. The Tempo tool does not return
span status codes.

`list_pods` on `kube-system`, `list_pods` on a context outside the allowlist,
and `get_pod_logs` with `since_seconds=99999` each came back as a tool error.
No manifest was applied. Routing was not changed.

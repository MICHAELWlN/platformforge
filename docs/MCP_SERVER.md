# MCP server

PlatformForge exposes a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server for local investigation. It uses the official Python SDK `mcp` 2.3.0
(`MCPServer`) and speaks stdio. Importing the package does not listen, and
there is no HTTP transport.

## Install and start

```bash
.venv/bin/pip install -e '.[mcp]'
.venv/bin/python -m platformforge.mcp_server
```

The process waits on stdin. A client launches it. Do not leave it running as a
service. `PLATFORMFORGE_MCP_HTTP` is rejected if set.

Cursor, or another MCP host, can use this stdio command:

```json
{
  "mcpServers": {
    "platformforge": {
      "command": "/absolute/path/to/platformforge/.venv/bin/python",
      "args": ["-m", "platformforge.mcp_server"]
    }
  }
}
```

Set `PLATFORMFORGE_MCP_KUBECONFIG` to a temporary file whose user is
`platformforge-mcp-reader`. The server rejects any other user in that file.
For the pinned GKE context the variable is required. Kind without it uses the
kubeconfig user for `kind-platformforge`. The server does not create
credentials, IAM bindings, or port-forwards. See
[MCP_LIVE_INTEGRATION.md](MCP_LIVE_INTEGRATION.md) and
[MCP_GKE_INTEGRATION.md](MCP_GKE_INTEGRATION.md).

## Tools

Every tool requires `context`. Allowed values are `kind-platformforge` and
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`. Kubernetes
tools also require `namespace`: `platformforge-dev` or
`platformforge-observability`. Any other context or namespace is rejected.

| Tool | What it returns |
| --- | --- |
| `list_pods` | Name, namespace, phase, ready containers, restarts, node |
| `describe_pod` | Status, container state, requests, limits, redacted env, events |
| `get_pod_logs` | At most 200 lines and 15 minutes. Text is marked untrusted |
| `list_deployments` | Desired, available, and updated replicas |
| `get_deployment_status` | Conditions and whether each image is digest-pinned |
| `list_kubernetes_events` | Recent events in one namespace |
| `get_service_status` | Service type, ports, ready endpoint count |
| `get_routing` | Weights of VirtualService `inventory-service` only. Get only. `writable` is false |
| `query_incident_metrics` | Cumulative counters and one 15-to-900-second increase. NaN is unset, not zero |
| `query_prometheus` | One instant PromQL query, at most 20 series |
| `query_loki` | One LogQL range over the last 15 minutes, at most 100 lines |
| `query_tempo` | One trace by a 16- or 32-character hex id |
| `get_service_health` | Deployment readiness plus a fixed Istio request query |
| `investigate_incident` | Classified evidence. `verified_root_cause` is always null |

## Observability URLs

Prometheus, Loki, and Tempo are ClusterIP Services. The server queries only
`http` URLs on `127.0.0.1`, `localhost`, or `::1`:

```bash
export PLATFORMFORGE_PROMETHEUS_URL=http://127.0.0.1:9090
export PLATFORMFORGE_LOKI_URL=http://127.0.0.1:3100
export PLATFORMFORGE_TEMPO_URL=http://127.0.0.1:3200
```

The operator starts those port-forwards. If a URL is unset, the tool reports
missing telemetry. An empty result is not treated as a healthy zero.

## Example

`investigate_incident` for a not-ready inventory canary returns verified
replica counts, untrusted log text, and hypotheses labeled
`Unverified hypothesis`. It does not name a root cause. A log line that says
to delete a namespace stays inside `untrusted` text.

## Troubleshooting

- `context is not allowlisted`: the tool argument is not one of the two contexts.
- `telemetry URL must be http on 127.0.0.1`: the URL is unset, remote, or HTTPS.
- `query contains characters this server does not accept`: PromQL or LogQL used
  a character outside the bounded set, or LogQL has no `{selector}`.
- `kubernetes API 403`: the kubeconfig user cannot read that object. The server
  does not grant RBAC.
- The CLI `platformforge deploy` still refuses non-kind contexts. This server
  does not deploy.

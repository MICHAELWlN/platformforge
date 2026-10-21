# Demo troubleshooting

Fixes that stay inside the existing cluster. None of these create a node, a
disk, a load balancer, or an IAM binding.

## The assistant has no PlatformForge tools

`.cursor/mcp.json` only points Cursor at `scripts/demo/mcp-server.sh`. This
chat does not see the tools until Cursor enables that server and reloads it.
Check the MCP panel for `platformforge` and for a start error.

The server exits immediately if `PLATFORMFORGE_MCP_HTTP` is set. Unset it.

A GKE tool call without `/tmp/pf-mcp-reader/kubeconfig` is rejected. Run
`make demo-reader` and reload the MCP server. The token lasts one hour. The
file is mode `0600` and the user is `platformforge-mcp-reader`. Do not point
the file at the operator kubeconfig.

## Prometheus, Loki, or Tempo is missing

`make demo-evidence` says Prometheus is not on `127.0.0.1:9090` when the
forward died. A short-lived shell will take `nohup` children with it. Leave
`scripts/demo/port-forwards.sh --wait` running in a terminal, or run
`make demo-forwards` from a terminal you keep open.

Empty Loki in a 15-minute window is missing telemetry, not a healthy zero.
The Tempo tool returns a batch count, not span HTTP status. There is no MCP
tool for a VirtualService, so the assistant must not state current weights
from MCP.

`platformforge_canary_error_ratio` is NaN after the route is back at 100/0.
That is the unset gauge, not a measured zero.

## The controller is not Ready

`make demo-controller` shows the Deployment. A missing image digest, a refused
context, or a Prometheus URL outside the allowlist leaves the pod unready.
Do not copy `deploy/kind/canary-controller.yaml` onto GKE.

## The failure drill is the wrong way around

Apply the faulty canary and wait until it is Ready before the 90/10 file.
Otherwise a healthy canary can be promoted. The controller never selects the
90/10 file itself. `scripts/demo/fail-canary.sh` does those two applies and
refuses to run until `CONFIRM_FAILURE=yes`.

## Put the cluster back

```bash
CONFIRM_RESTORE=yes ./scripts/demo/restore-healthy.sh
make demo-preflight
make demo-stop
```

Expect failure percent 0, weights `v0-1-0=100` and `v0-2-0=0`, and STRICT.
The restore script does not delete the stable Deployment. `make demo-stop`
kills only pid file entries whose command line contains `port-forward`.
Delete `/tmp/pf-mcp-reader/kubeconfig` when the recording is over.

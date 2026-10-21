# Four-minute demo runbook

Recording order for the existing GKE cluster. Prep is off camera. The failure
section stays behind `CONFIRM_FAILURE=yes` and is not part of a silent prep.
Do not dispatch a new GitHub Actions run, apply Terraform, or change IAM
during the recording unless that step was approved again.

Context, on every command:

`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`

## Windows

1. Terminal in the repo, context already the pinned GKE context.
2. Browser on Grafana at `http://127.0.0.1:3000`, dashboard **PlatformForge services** (`/d/platformforge-overview`). Sign in locally. Do not read the admin password into the terminal recording.
3. Cursor chat with the `platformforge` MCP server enabled. The config is `.cursor/mcp.json`. It speaks stdio through `scripts/demo/mcp-server.sh`.
4. Optional: the delivery workflow page for `MICHAELWlN/platformforge`, workflow `Delivery`, showing a past successful publish. Do not start a new run on camera.

## Prep, about 3 minutes, not on the four-minute clock

```bash
kubectl config current-context
make demo-preflight
make demo-reader
make demo-forwards
make demo-controller
```

Expect the context line above, node Ready, inventory 2/2 and 1/1, orders 2/2,
controller 1/1, route `v0-1-0=100` and `v0-2-0=0`, STRICT, failure percent 0,
no LoadBalancer or NodePort, no PVC, and a controller log `route is already stable`.
`make demo-forwards` prints `telemetry ready` and, with the terminal left open
via `scripts/demo/port-forwards.sh --wait`, keeps Prometheus `:9090`, Loki `:3100`,
Tempo `:3200`, and Grafana `:3000` on `127.0.0.1`.

Reload the `platformforge` MCP server in Cursor after the kubeconfig exists.
The tool list must include `get_routing` and `query_incident_metrics`. If those
tools are not visible to the assistant, the AI segment is not ready. Do not
narrate tool results you did not see. `get_routing` needs the approved reader
Role change before it can return weights.

## On camera, about 4 minutes

| Clock | Segment | Do | Expect |
| --- | --- | --- | --- |
| 0:00–0:25 | GKE | `kubectl get nodes` and the preflight route line | One Ready node, `v1.36.4-gke.1391002`, weights 100 and 0 |
| 0:25–0:45 | GitHub Actions | Show the past Delivery run. Say apply is still refused | A succeeded publish. No new dispatch |
| 0:45–1:10 | Istio | `kubectl -n platformforge-dev get peerauthentication default` and the VirtualService weights | STRICT, `v0-1-0=100`, `v0-2-0=0` |
| 1:10–1:50 | Failure | Only with `CONFIRM_FAILURE=yes ./scripts/demo/fail-canary.sh`, then `CONFIRM_TRAFFIC=yes ./scripts/demo/traffic.sh 400` | Canary env `100`, route 90/10, mixed HTTP 200 and 502 |
| 1:50–2:20 | Grafana | Dashboard panels for canary error rate, traffic split, rollback count, rollout state | Series move only after the failure. Before it, rollbacks stay at the previous count |
| 2:20–2:45 | Rollback | `make demo-controller` and `make demo-evidence` | A JSON line `action=rollback`. Do not invent the sidecar time if this run did not measure it |
| 2:45–3:35 | MCP | Ask the question in [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Tool calls, `verified_root_cause` null, no invented route weights |
| 3:35–4:00 | Recovery | `CONFIRM_RESTORE=yes ./scripts/demo/restore-healthy.sh` then 20 confirmed traffic requests | Failure percent 0, route 100/0, HTTP 200 |

The 1:10 failure block is the recording experiment. Skip it when the goal is
only to show the already recovered cluster. In that cut, use the retained
evidence in `docs/GKE_CANARY_EXPERIMENT.md` and say those timings were measured
on 2026-10-21, not in this take.

## Recovery if the take goes wrong

```bash
CONFIRM_RESTORE=yes ./scripts/demo/restore-healthy.sh
make demo-preflight
```

That applies the reviewed stable VirtualService and the healthy canary
manifest. It does not delete `inventory-service-v0-1-0`.

## Cleanup

```bash
make demo-stop
rm -f /tmp/pf-mcp-reader/kubeconfig
```

Stop the Cursor MCP server from the Cursor MCP panel so the stdio process
exits. Leave the controller, the reader ServiceAccount, STRICT, and the
workloads in place.

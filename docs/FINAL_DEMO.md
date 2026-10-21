# Final demonstration

Spoken cut without a new failure: **4 minutes**. A live failure injection is
about **6 minutes**, because the canary rollout, the traffic burst, and one
controller poll do not fit in the shorter slots. This page does not inject a
failure and does not change the route.

Context, on every command:

`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`

GitHub Actions publishes a digest to the existing Artifact Registry repository
and still refuses to apply manifests. What is running on GKE was rendered from
`deploy/gke/image-lock.yaml` and applied with kubectl on the pinned context.
Do not dispatch a new Delivery run during the recording.

## Windows

1. Terminal in the repo, context already the pinned GKE context.
2. Browser at `http://127.0.0.1:3000`, dashboard **PlatformForge services**
   (`/d/platformforge-overview`). Sign in locally. Do not read the admin
   password into the recording. The panels already show stable versus canary
   traffic, HTTP 5xx, p95 latency, rollback count, rollout state, controller
   error ratio, latency regression, and decision counters. NaN on the two
   controller gauges means unset, not zero. Those sentences are in the repo
   dashboard JSON and are not on the live Grafana ConfigMap until that
   ConfigMap replace is approved.
3. Cursor with the `platformforge` MCP server enabled from `.cursor/mcp.json`.

## Prep, off camera

```bash
kubectl config current-context
make demo-preflight
make demo-reader
bash scripts/demo/port-forwards.sh --wait
```

Leave the last command running. It must print `telemetry ready`. Prometheus
is `127.0.0.1:9090`, Loki `:3100`, Tempo `:3200`, Grafana `:3000`.

In a second terminal:

```bash
make demo-controller
```

Expect route `v0-1-0=100`, `v0-2-0=0`, STRICT, `INVENTORY_FAILURE_PERCENT=0`,
controller Ready, and a log line `route is already stable`.

Reload `platformforge` in Cursor Settings → MCP. The list includes
`get_routing` and `query_incident_metrics` plus the original twelve tools.
There is no apply, delete, exec, or Secret tool.

`get_routing` is get-only for `virtualservices/inventory-service`. That rule is
on the live Role. A direct call returned `v0-1-0=100` and `v0-2-0=0`. Reload
the MCP server so the running Cursor process exposes the tool.

## On camera, 4 minutes, recovered cluster

| Clock | Show | Say |
| --- | --- | --- |
| 0:00 | `kubectl get nodes` and the preflight route line | One private zonal node. Terraform is not applied in this take. |
| 0:25 | A past Delivery run, no new dispatch | OIDC publish writes a digest. The workflow still refuses apply. |
| 0:45 | PeerAuthentication STRICT and the route line | Orders may GET `/inventory/*`. Stable is 100 percent. Canary is ready at 0 percent. |
| 1:10 | Grafana traffic, 5xx, and p95 panels | These are Istio source metrics for `inventory-service`. An empty range is missing data, not health. |
| 1:40 | Rollback counter, state, and decision panels | The rollback counter is cumulative. State `stable` is the controller gauge, not a VirtualService read. |
| 2:10 | Controller log from `make demo-controller` | The latest lines say the route is already stable. Quote a rollback line only if this tail still contains it. |
| 2:30 | The MCP question below | Wait for tool calls. `verified_root_cause` stays null. |
| 3:30 | `make demo-preflight` | Failure percent 0, weights 100 and 0, STRICT. |

Ask:

```text
Investigate the latest inventory-service canary failure on PlatformForge GKE.
Context gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev.
Namespace platformforge-dev. Service inventory-service.
Use get_routing, query_incident_metrics with window_seconds 900,
Kubernetes, Prometheus, Loki, and Tempo.
Separate cumulative counters from windowed increases.
NaN is unset, not zero. An empty log or trace is missing evidence.
Do not state a verified root cause, a recovery time, or a route weight
unless a tool returned it.
```

## Live failure, only when explicitly approved

This changes the canary and the route. It is not part of prep.

```bash
CONFIRM_FAILURE=yes ./scripts/demo/fail-canary.sh
CONFIRM_TRAFFIC=yes ./scripts/demo/traffic.sh 400
make demo-controller
make demo-evidence
CONFIRM_RESTORE=yes ./scripts/demo/restore-healthy.sh
make demo-preflight
```

`fail-canary.sh` applies the faulty canary, waits until it is Ready, then
applies the reviewed 90/10 file. The controller does not select that file.
The restore applies the stable VirtualService and the healthy canary. It does
not delete `inventory-service-v0-1-0`. Without the matching `CONFIRM_*` value
each script exits 2.

## Cleanup

```bash
make demo-stop
rm -f /tmp/pf-mcp-reader/kubeconfig
```

Stop the MCP server from the Cursor MCP panel. Leave the controller, the
reader ServiceAccount, STRICT, and the workloads in place.

## Before the next recording

1. The reader Role is applied. Reload MCP and call `get_routing`. The direct
   reader call returned subsets `v0-1-0` weight 100 and `v0-2-0` weight 0.
2. Replacing the Grafana ConfigMap is optional. The live panels already use
   the Istio and `platformforge_canary_*` series. The NaN descriptions are
   only in the repo JSON until that replace is approved.
3. Do not run `terraform apply`, do not dispatch Delivery, and do not inject
   the failure until the take that is supposed to show it.

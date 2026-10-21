# Kind canary experiment

Date: 2026-10-21. Context used for every cluster call:
`kind-platformforge`. The kubeconfig current context stayed
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev` before, during,
and after the run. GKE objects were not changed. Terraform was not run.

## Baseline

Before the drill:

- VirtualService `inventory-service`: `v0-1-0=100`, `v0-2-0=0`
- `inventory-service-v0-2-0` env `INVENTORY_FAILURE_PERCENT=0`
- `inventory-service-v0-1-0` ready 2/2
- Services in `platformforge-dev` are ClusterIP

The test applied the stable route and the healthy canary manifest again, then
checked the failure percent was `0`.

## Fault and traffic

The test deployed `services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml`
through the Kubernetes client (`persist_config=False`) and waited for rollout.
`INVENTORY_FAILURE_PERCENT` became `100`. Readiness stayed ready because
`/readyz` does not call the failing handler.

It then applied `inventory-virtualservice-canary.yaml` and waited until the
orders sidecars reported 90/10.

Two hundred `GET` requests went through `pf-test-client-mesh` to
`orders-service.platformforge-dev.svc.cluster.local:8080/orders/check/widget`.

| Client status | Count |
| --- | --- |
| 200 | 178 |
| 502 | 22 |

The 22 failures are the orders response when inventory returns HTTP 500.

## Prometheus decision

The analyzer polled `http://127.0.0.1:19090`, a port-forward of
`svc/prometheus-server` in `platformforge-observability`. The query window
was 60 seconds. The decision was recorded 6.252 seconds after the requests
started.

`increase(istio_requests_total{reporter="source",destination_service_name="inventory-service"}[60s])`:

| Version | Total | HTTP 5xx | Ratio |
| --- | --- | --- | --- |
| `v0.2.0` | 15.999644452345503 | 15.999644452345503 | 1.0 |
| `v0.1.0` | 125.33054821003977 | 0.0 | 0.0 |

`increase` extrapolates, so these totals are not the client counts. The canary
ratio is 1.0, above the 0.05 limit. Action: `rollback`. Reason: `canary error
rate above threshold`.

The `Analysis` stored `p95_ms` as null for both versions. That 60-second
histogram was not used. After recovery, the same parser over a 300-second
window of `istio_request_duration_milliseconds_bucket` returned:

| Version | p95 |
| --- | --- |
| `v0.1.0` | 0.496 ms |
| `v0.2.0` | 2.800 ms |

Regression against the stable p95 is 4.64. That figure is a follow-up query.
It was not an input to the rollback decision.

## Correlation

The first five failing client request ids were:

- `canary-26209-1`
- `canary-26209-8`
- `canary-26209-11`
- `canary-26209-39`
- `canary-26209-45`

The `inventory-service` container log on the canary pod contained
`canary-26209-1` together with trace ids
`0f187d9415579fefc66df97e18027a9b` and `7376b0af67defab452d1b880be9d16bd`.
Tempo was not queried. No id was invented.

## Recovery

Stable VirtualService apply until the orders sidecars reported weight 100 on
`v0-1-0`: **1.158 seconds**. A second apply of the same file left the state
at `stable`. `inventory-service-v0-1-0` was still 2/2.

Thirty more orders requests, while the canary was still on the faulty
manifest, all returned HTTP 200.

The `finally` block deployed `services/manifests/inventory-service-v0.2.0.yaml`.
After that rollout, `INVENTORY_FAILURE_PERCENT` was `0`, weights were
`v0-1-0=100` `v0-2-0=0`, and the canary Deployment was 1/1.

The whole pytest was **1 passed in 28.68s**. That duration includes the healthy
rollout, the faulty rollout, the requests, the rollback, and the restore.

The port-forward was stopped. Port 19090 was free afterward.

## Dashboard

`platformforge-dashboards` in `platformforge-observability` on kind was
replaced from `deploy/observability/dashboards/platformforge.json`. The
ConfigMap lists these added panels:

- Canary versus stable error rate
- Canary versus stable p95 latency
- Inventory traffic distribution
- Canary rollbacks
- Canary rollout state

The first three use Istio series Prometheus already scrapes. The rollback and
state panels query `platformforge_canary_rollbacks_total` and
`platformforge_canary_state`. This one-shot drill rendered those series in
process and did not scrape them. The controller run below added the scrape
and three more panels. The GKE dashboard ConfigMap was not changed.

## Command

```bash
make canary-kind
```

`make test` skips this drill. The skip is not a pass.

## Controller experiments

The five-experiment run is recorded in
[docs/CANARY_CONTROLLER.md](CANARY_CONTROLLER.md). Command:

```bash
make canary-controller-kind
```

The controller Deployment must already be ready. `make test` skips this file
too. On 2026-10-21 that opt-in run passed in 335.84s. `make canary-kind`
remains the one-shot drill above.

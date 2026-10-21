# Progressive delivery

Inventory traffic stays on the Istio VirtualService in `platformforge-dev`.
Stable routing is `v0-1-0=100` and `v0-2-0=0`. The candidate is the
`inventory-service-v0-2-0` Deployment, pinned by digest. v0.1.0 is not removed
when a candidate is promoted.

`src/platformforge/delivery.py` decides the next step from an observation. It
does not query Prometheus or call kubectl. A caller supplies the sample.

## Sequence

1. Confirm the context is
   `gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`, the project
   is `platformforge-dev-mn2026`, and the cluster is `platformforge-dev`.
2. Confirm the candidate image is
   `us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge/inventory-service@sha256:<64 hex>`
   and the architecture is `linux/amd64`.
3. Take the delivery lock. A second delivery inside one hour is refused.
4. Roll the canary Deployment to that digest while the VirtualService is still
   100/0, and wait until it is Ready.
5. Apply `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml`
   (90/10).
6. Send a bounded number of `GET /orders/check/widget` requests.
7. Read `istio_requests_total` for the canary subset from Prometheus.
8. Call `decide`. Promote, roll back, or hold. Do not treat an empty Prometheus
   result as zero errors.

## Thresholds

`CanaryThresholds` defaults, all overridable:

| Limit | Default |
| --- | --- |
| Maximum HTTP 5xx ratio | 0.05 |
| Minimum HTTP 2xx ratio | 0.95 |
| Minimum requests | 50 |
| Minimum observation | 60 seconds |

Decision order:

- A timeout, a failed authorization check, or a not-Ready canary rolls back.
- Missing telemetry rolls back. An empty or failed Prometheus payload is
  missing telemetry.
- If any requests have arrived and the 5xx ratio is above the limit, or the
  2xx ratio is below the limit, roll back even before the minimum sample.
- Below the minimum sample, hold while the window is open. When the window
  closes without enough requests, roll back.
- Otherwise promote: shift the VirtualService to `v0-1-0=0` and `v0-2-0=100`.
  The v0.1.0 Deployment stays so rollback still has a target.

Promotion renders that VirtualService in memory (`promoted_virtualservice`).
It is not the steady-state file. The steady-state file remains 100/0 to
v0.1.0 until a person applies a promotion.

## Publishing gate

Image publish is the manual workflow in [CI_CD.md](CI_CD.md). It does not
route traffic. `GCP_DELIVERY_ENABLED` stays unset until the private GitHub
repository exists and the `gke-delivery` environment shows a required
reviewer and a `main`-only branch rule. This document's decision function
still does not call the cluster.

## Approval

`require_manual_approval` accepts only
`PLATFORMFORGE_DELIVERY_APPROVED=1` on `workflow_dispatch` from
`refs/heads/main`. A pull request is refused. `refuse_apply` still exits 2,
so the decision library cannot change the cluster by itself.

## What is not proven live

No canary from this milestone was applied to GKE. The 90/10 measurement and
the faulty-canary drill in [GKE_INCIDENT_DEMO.md](GKE_INCIDENT_DEMO.md) are
from the earlier manual install. The decision function is covered by unit
tests, including rollback on missing telemetry.

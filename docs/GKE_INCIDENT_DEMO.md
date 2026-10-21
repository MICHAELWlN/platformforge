# GKE incident demonstration

Controlled failure of the inventory canary on the running GKE cluster, then
recovery. The faulty manifest is
`services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml`. It is the
healthy v0.2.0 manifest with `INVENTORY_FAILURE_PERCENT=100`. Readiness stays
green. Orders maps an inventory HTTP 500 to HTTP 502.

Do this only on context
`gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev`. Restore the
stable VirtualService and the healthy canary even if a later query fails.

## Steps

1. Confirm `inventory-virtualservice-stable.yaml` is applied (`v0-1-0=100`,
   `v0-2-0=0`) and the canary env `INVENTORY_FAILURE_PERCENT` is `0`.
2. Record `istio_requests_total` for inventory HTTP 500.
3. Apply `inventory-virtualservice-canary.yaml` (90/10).
4. Render the faulty manifest through `python -m platformforge.gke render`
   with `deploy/gke/image-lock.yaml`, then `platformforge deploy --dry-run`,
   then `kubectl apply`. The image digest stays the healthy 0.2.0 digest.
5. Send orders checks for `/orders/check/widget` with a unique `x-request-id`
   prefix.
6. Read Prometheus, Loki, and Tempo.
7. Apply the stable VirtualService, then the healthy v0.2.0 manifest.
8. Send another short burst and confirm HTTP 200.

## Result on 2026-10-20

Baseline Prometheus destination counters included HTTP 500 at 15 from an
earlier partial burst. The measured 100-request burst returned
`{"200": 89, "502": 11}`. After the scrape, the inventory HTTP 500 counter
was 26. That is the 11 new failures on top of the 15 already recorded.

Loki, from the inventory container:

```json
{"message": "injected failure", "request_id": "gke-drill-1792543945-86", "trace_id": "2f2eabaed68d6f58f9f946ffb8594dca", "item_id": "widget"}
```

Tempo trace `2f2eabaed68d6f58f9f946ffb8594dca` contained both services. Spans
included `GET /orders/check/{item_id}` HTTP 502 and `GET /inventory/{item_id}`
HTTP 500, both with request id `gke-drill-1792543945-86`.

Grafana's API reported the Prometheus, Loki, and Tempo datasources healthy
before the burst. Prometheus had 7 targets up.

Recovery applied stable weights `v0-1-0=100 v0-2-0=0` and set
`INVENTORY_FAILURE_PERCENT` back to `0`. The next 30 orders checks were all
HTTP 200.

A healthy 90/10 measurement, taken before the faulty canary and then restored,
was 180 requests to v0.1.0 and 20 to v0.2.0 out of 200.

## What not to leave behind

Do not leave the canary VirtualService or `INVENTORY_FAILURE_PERCENT=100`
applied. The steady state is stable routing and the healthy canary manifest.

# Automated canary analysis

`platformforge.canary` compares one stable version with one canary version.
It returns `promote`, `hold`, or `rollback`. The caller supplies Prometheus
samples. The module does not deploy and does not delete a workload.

`platformforge.delivery.decide` is unchanged. It still judges one caller-supplied
window and never opens a socket. The version comparison lives in `canary.analyze`.

## Inputs

| Input | Bound |
| --- | --- |
| Stable and canary version labels | `destination_version`, for this lab `v0.1.0` and `v0.2.0` |
| Prometheus base URL | `http://127.0.0.1`, `http://localhost`, or the in-cluster `prometheus-server.platformforge-observability.svc` name. No other host, no userinfo, no path |
| Evaluation window | Integer seconds, 15 through 900. The PromQL range is built from that integer |
| Minimum canary requests | At least 1. The kind drill used 20. The library default is 50 |
| Maximum HTTP 5xx ratio | 0 through 1. Default 0.05 |
| Maximum p95 regression | `(canary - stable) / stable`. Default 0.50 |
| Readiness | Caller-supplied. A canary that is not ready rolls back |

## Queries

Both queries use the source sidecar and one service name. The service name may
contain only letters, digits, and hyphens.

```promql
sum by (destination_version, response_code) (
  increase(istio_requests_total{
    reporter="source",
    destination_service_name="inventory-service"
  }[60s])
)

sum by (le, destination_version) (
  increase(istio_request_duration_milliseconds_bucket{
    reporter="source",
    destination_service_name="inventory-service"
  }[60s])
)
```

`increase` is an absolute count for the window. An empty vector is missing
telemetry. It is not a healthy zero.

## Error rate

For each `destination_version`, total is the sum of every `response_code`.
Errors are the sum of codes that start with `5`. The ratio is `errors / total`
when `total > 0`. A zero total has no ratio, so the code does not divide.

## p95

Buckets are cumulative `(le, count)` pairs. Non-finite counts are skipped.
The 0.95 rank is interpolated inside the bucket that crosses it. The `+Inf`
bucket does not invent a latency past the last finite bound. A histogram whose
total is 0 has no p95.

Regression is `(canary_p95 - stable_p95) / stable_p95` when `stable_p95 > 0`.
A zero stable latency has no regression ratio.

## Decision order

1. Prometheus timeout: hold while the evaluation window is still open, otherwise rollback.
2. Canary not ready: rollback.
3. Stable or canary series missing: hold while the window is open, otherwise rollback.
4. Negative counts, or errors greater than total: rollback.
5. Canary error ratio above the limit: rollback, including before the minimum sample.
6. Canary total below the minimum sample: hold while the window is open, otherwise rollback.
7. p95 missing for either version: hold while the window is open, otherwise rollback.
8. p95 regression above the limit, and the absolute gap above `min_p95_delta_ms`: rollback. The library default of that floor is 0. The kind controller sets it to 200 ms.
9. Otherwise promote.

Every result keeps the samples, the ratio, the regression, and any request or
trace ids the caller actually found. The analyzer does not invent ids.

## Metrics text

`CanaryMetrics.render` writes Prometheus text for evaluation count, promotion
count, rollback count, decisions by action, the last error ratio, the last
p95 regression, and the rollout state. A missing ratio or regression is `NaN`,
not zero. The one-shot drill still renders that text in process. The kind
controller serves it on `GET /metrics` and Prometheus job `platformforge-canary`
scrapes that ClusterIP. Pushgateway stays disabled.

## What this module does not do

The analyzer does not open a socket and does not promote by itself. The
controller accepts `kind-platformforge`, and the pinned GKE context only when
`PLATFORMFORGE_CANARY_GKE=1`, in-cluster config, and the pod token are present.
The reviewed files are 100/0, 90/10, and 0/100. The controller never selects
the 90/10 file. GitHub Actions still refuses `kubectl apply`. The measured GKE
rollback is [docs/GKE_CANARY_EXPERIMENT.md](GKE_CANARY_EXPERIMENT.md).

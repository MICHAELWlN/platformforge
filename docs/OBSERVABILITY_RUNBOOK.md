# Observability runbook

How to look at a canary failure with the three signals, and how to remove the stack.
Cleanup commands delete data and are not part of normal operation. Do not run them
without an explicit approval.

## Investigate a canary failure

Stable routing is VirtualService `inventory-service` with subset `v0-1-0` weight 100
and `v0-2-0` weight 0. The drill manifest is
`services/manifests/incidents/inventory-service-v0.2.0-faulty.yaml` (`INVENTORY_FAILURE_PERCENT=100`).
orders-service maps that HTTP 500 to HTTP 502 `inventory_bad_status`.

1. Confirm the baseline. Grafana dashboard **PlatformForge services**
   (`/d/platformforge-overview`) should show inventory `v0.1.0` carrying the traffic
   and the 5xx panel near zero. Prometheus:

   ```promql
   sum by (destination_version, response_code) (
     istio_requests_total{
       reporter="source",
       destination_service_name="inventory-service"
     }
   )
   ```

2. Shift 10% to the canary by applying
   `deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml`. Wait until
   the orders sidecars report weights 90/10 before measuring.
3. Deploy the faulty manifest with `platformforge deploy` and
   `--approve-context kind-platformforge`. Readiness stays green, because `/readyz`
   is not failed.
4. Send traffic through orders-service (`GET /orders/check/widget`), not at a pod IP.
5. In Prometheus, `destination_version="v0.2.0"` gains `response_code="500"`.
   `v0.1.0` does not. Grafana's error-rate and canary panels use the same series.
   On 2026-10-20 a 40-request sample returned 39x HTTP 200 and 1x HTTP 502.
   `istio_requests_total` for inventory `v0.2.0` 5xx moved from 54 to 55.
   The `v0.1.0` 5xx counter stayed 0. The 54 already included an earlier drill on
   the same counters.
6. Take the failing response's `x-request-id`. In Grafana's Loki datasource:

   ```logql
   {namespace="platformforge-dev", container="inventory-service"} |= "<request id>"
   ```

   The JSON line has `request_id`, `trace_id`, and `span_id`, and a real `ts`.
   The same `trace_id` is on the orders application log and on the orders sidecar
   access log (`container="istio-proxy"`).
7. Open that `trace_id` in Tempo. The inventory server span's parent chain includes
   the orders server span `GET /orders/check/{item_id}`. The derived field on the
   Loki datasource and Tempo's `tracesToLogsV2` query do this from the UI.
8. Restore stable routing by applying
   `deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml`. Wait for
   the orders sidecars to report weight 100 on `v0-1-0`. New requests should be
   HTTP 200, and the `v0.2.0` 5xx counter should stop increasing. The 2026-10-20
   check held at 55 after the shift and another 20 successful requests.
9. Redeploy `services/manifests/inventory-service-v0.2.0.yaml` so the canary process
   is healthy again (`INVENTORY_FAILURE_PERCENT=0`) while it still receives no traffic.

`tests/integration/test_observability.py::test_canary_errors_show_in_prometheus_and_recover`
performs steps 2–9 and restores the healthy canary even when an assertion fails.

## Where to query from

Grafana, Prometheus, Loki, and Tempo are ClusterIP. From a workstation, port-forward
only. Do not change the Service type.

```bash
kubectl --context kind-platformforge -n platformforge-observability \
  port-forward svc/grafana 3000:80
```

Anonymous viewers can open the dashboard. The admin password, if needed, is
`kubectl get secret -n platformforge-observability grafana -o jsonpath='{.data.admin-password}'`
and must not be committed.

In-cluster checks use `pf-test-client-plain` (no sidecar) for the telemetry
namespace and `pf-test-client-mesh` for the applications. STRICT mTLS rejects the
plaintext client calling orders or inventory.

## Cleanup (destructive, requires approval)

These commands delete the telemetry namespace, its emptyDir data, and the Prometheus
RoleBinding in `platformforge-dev`. They do not delete application workloads.

```bash
helm uninstall grafana prometheus alloy tempo loki -n platformforge-observability
kubectl --context kind-platformforge delete namespace platformforge-observability
kubectl --context kind-platformforge delete clusterrole platformforge-prometheus
```

Reverting Istio tracing is a separate `istioctl install` of an operator file that
does not list `otel-tracing`, plus deletion of Telemetry `tracing`. Do that only
with approval. Leave PeerAuthentication STRICT and the inventory AuthorizationPolicy
in place.

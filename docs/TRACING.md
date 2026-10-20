# Distributed tracing

One request from the mesh test client through orders-service to inventory-service
produces one trace. Istio samples it and the applications continue it.

## Path

1. `pf-test-client-mesh` sends `GET /orders/check/widget` with `x-request-id`.
2. The client's sidecar starts a span and injects W3C `traceparent` / `tracestate`.
3. The orders sidecar continues that span on the inbound listener.
4. The orders FastAPI server span is a child of the inbound sidecar span.
   `HTTPXClientInstrumentor` creates a client span and injects W3C headers on the
   call to inventory-service. The existing `x-request-id` header is still set by
   the application.
5. The orders outbound sidecar, the inventory inbound sidecar, and the inventory
   FastAPI server span continue the same trace.

`OTEL_TRACES_SAMPLER=parentbased_always_on`, so an application span is recorded when
Istio sampled the parent. Istio's Telemetry resource samples 100% of
`platformforge-dev` and does not let a non-UUID `x-request-id` override that decision
(`useRequestIdForTraceSampling: false`).

## Export

Applications export OTLP/HTTP to
`http://tempo.platformforge-observability.svc.cluster.local:4318` only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. Unit tests leave it unset, so they do not try
to reach Tempo. The batch processor delay is 1s (`OTEL_BSP_SCHEDULE_DELAY`). A failed
export does not fail the HTTP request.

Istio exports OTLP/gRPC to the same Tempo Service on port 4317. The provider is
`otel-tracing` in `deploy/istio/istio-operator.yaml`. The legacy Zipkin tracer is
disabled with `defaultConfig.tracing: {}`.

## What a span is allowed to carry

Spans do not record request or response bodies, and they do not capture HTTP headers.
The only extra attribute is `platformforge.request_id`, copied from the request ID
the service already accepted or generated. `/healthz` and `/readyz` are not traced.
ASGI receive/send spans are not created.

Logs emitted during a request include `request_id`, `trace_id` (32 hex), and
`span_id` (16 hex) when the current span context is valid. Those fields are how Loki
lines are matched to a Tempo trace. They are not Loki labels.

## Checked relationship

`tests/integration/test_observability.py::test_distributed_trace_correlates_logs`
sends one request and asserts:

- orders and inventory application logs share `request_id` and `trace_id`
- the orders sidecar access log in Loki has the same `trace_id`
- the Tempo trace contains `service.name` `orders-service` and `inventory-service`
- walking parent span IDs from the inventory server span reaches the orders server span
- `platformforge.request_id` equals the caller-supplied request ID
- no `http.request.header.*` attributes are present

A run on 2026-10-20 recorded `request_id=obs-trace-31028d8d76aa`,
`trace_id=20f98e2b62278403629b59cfd4da5f07`, orders log time
`2026-10-20T22:39:11.544+00:00`, inventory log time `2026-10-20T22:39:11.543+00:00`.

"""OpenTelemetry traces for PlatformForge services.

Spans are created in-process so logs can carry trace and span IDs. They are exported
only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (the in-cluster Tempo receiver).
Export is best-effort: a failing collector must not fail a request.

Nothing here records request or response bodies, or HTTP headers. The only extra
span attribute is the already-validated request ID.
"""

from __future__ import annotations

import atexit
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Health probes are excluded. A comma-separated list of regexes, matched against the path.
_EXCLUDED_URLS = "healthz,readyz"

# Tests opt in and read ended spans. Production leaves this unset, so the list stays empty.
captured_spans: list[ReadableSpan] = []


class _CaptureProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None) -> None:
        return

    def on_end(self, span: ReadableSpan) -> None:
        if os.environ.get("PF_OTEL_CAPTURE") == "1":
            captured_spans.append(span)

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def configure_telemetry(app: FastAPI, service: str) -> TracerProvider:
    """Instrument ``app`` and return the provider the process should export with.

    Each service process calls this once. A second call in the same process (the
    in-process orders -> inventory unit test) gets its own provider so the two
    services keep distinct ``service.name`` resources.
    """
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.version": os.environ.get("SERVICE_VERSION", "dev"),
            }
        )
    )
    provider.add_span_processor(_CaptureProcessor())
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        # The HTTP exporter reads the endpoint from the environment and appends /v1/traces.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        atexit.register(provider.shutdown)
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(provider)
    FastAPIInstrumentor().instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_EXCLUDED_URLS,
        exclude_spans=["receive", "send"],
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
    )
    return provider

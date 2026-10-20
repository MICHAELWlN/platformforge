"""Trace context, parent-child spans, and what must not be recorded."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from orders_service.app import create_app
from orders_service.settings import Settings
from pf_service_common.logs import JsonFormatter
from pf_service_common.requestid import REQUEST_ID_HEADER
from pf_service_common.telemetry import captured_spans
from tests.services.conftest import access_records
from tests.services.test_orders import UPSTREAM, Upstream, _item

SETTINGS = Settings(inventory_url=UPSTREAM, inventory_timeout_seconds=0.5)


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setenv("PF_OTEL_CAPTURE", "1")
    captured_spans.clear()
    yield captured_spans
    captured_spans.clear()


def _names(spans):
    return [span.name for span in spans]


def test_traced_request_logs_trace_and_span_ids(capture, caplog):
    caplog.set_level("INFO")
    app_client = TestClient(create_app(SETTINGS, transport=httpx.MockTransport(Upstream(_item()))))
    with app_client as client:
        response = client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-trace"})
    assert response.status_code == 200
    (record,) = access_records(caplog, "/orders/check/widget")
    assert len(record.trace_id) == 32
    assert len(record.span_id) == 16
    int(record.trace_id, 16)
    line = json.loads(JsonFormatter("orders-service").format(record))
    assert line["trace_id"] == record.trace_id
    assert line["span_id"] == record.span_id
    assert line["request_id"] == "rid-trace"


def test_health_probes_are_not_traced(capture, caplog):
    caplog.set_level("DEBUG")
    transport = httpx.MockTransport(Upstream(_item()))
    with TestClient(create_app(SETTINGS, transport=transport)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
    assert capture == []
    health = access_records(caplog, "/healthz")
    assert health and all(record.trace_id is None for record in health)


def test_outbound_call_is_a_child_of_the_server_span(capture):
    upstream = Upstream(_item())
    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        response = client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-child"})
    assert response.status_code == 200
    (request,) = upstream.requests
    traceparent = request.headers["traceparent"]
    version, trace_id, span_id, _flags = traceparent.split("-")
    assert version == "00"
    assert len(trace_id) == 32 and len(span_id) == 16

    server = [
        span for span in capture if span.attributes.get("platformforge.request_id") == "rid-child"
    ]
    assert len(server) == 1
    (server_span,) = server
    children = [
        span
        for span in capture
        if span.parent and span.parent.span_id == server_span.context.span_id
    ]
    assert children, _names(capture)
    child = children[0]
    assert format(child.context.trace_id, "032x") == trace_id
    assert format(child.context.span_id, "016x") == span_id
    assert format(server_span.context.trace_id, "032x") == trace_id
    assert child.parent.span_id == server_span.context.span_id
    # The propagated request ID is unchanged, and no body or credential header is recorded.
    assert request.headers[REQUEST_ID_HEADER] == "rid-child"
    blob = " ".join(f"{key}={value}" for span in capture for key, value in span.attributes.items())
    assert "authorization" not in blob.lower()
    assert "quantity" not in blob
    assert "widget" in blob  # the path is a span attribute; the body is not

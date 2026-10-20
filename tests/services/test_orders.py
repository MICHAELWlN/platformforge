from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from inventory_service.app import create_app as create_inventory_app
from orders_service.app import create_app
from orders_service.settings import Settings
from pf_service_common.requestid import REQUEST_ID_HEADER
from tests.services.conftest import access_records

UPSTREAM = "http://inventory-service.platformforge-dev.svc.cluster.local:8080"
SETTINGS = Settings(inventory_url=UPSTREAM, inventory_timeout_seconds=0.5)


class Upstream:
    """Records requests made by orders-service and replies with a canned response."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _client(handler, settings=SETTINGS):
    upstream = Upstream(handler)
    return TestClient(create_app(settings, transport=httpx.MockTransport(upstream))), upstream


def _item(quantity=42, item_id="widget"):
    return lambda _: httpx.Response(
        200, json={"item_id": item_id, "name": "W", "quantity": quantity, "in_stock": quantity > 0}
    )


def test_orderable_item_and_upstream_request():
    client, upstream = _client(_item(42))
    with client:
        response = client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-ok"})
    assert response.status_code == 200
    assert response.json() == {"item_id": "widget", "orderable": True, "quantity_available": 42}
    (request,) = upstream.requests
    assert str(request.url) == f"{UPSTREAM}/inventory/widget"
    assert request.headers[REQUEST_ID_HEADER] == "rid-ok"
    assert response.headers[REQUEST_ID_HEADER] == "rid-ok"


def test_generated_request_id_is_propagated_upstream():
    client, upstream = _client(_item(1))
    with client:
        response = client.get("/orders/check/widget")
    generated = response.headers[REQUEST_ID_HEADER]
    assert len(generated) == 32
    assert upstream.requests[0].headers[REQUEST_ID_HEADER] == generated


def test_out_of_stock_not_orderable():
    client, _ = _client(_item(0))
    with client:
        assert client.get("/orders/check/widget").json()["orderable"] is False


def test_upstream_404_maps_to_404():
    client, _ = _client(lambda _: httpx.Response(404, json={"detail": {"error": "item_not_found"}}))
    with client:
        response = client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-404"})
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "item_not_found", "request_id": "rid-404"}


def _raise(exc_type):
    def handler(request):
        raise exc_type("simulated", request=request)

    return handler


@pytest.mark.parametrize(
    ("handler", "status", "error"),
    [
        (_raise(httpx.ConnectError), 503, "inventory_unavailable"),
        (_raise(httpx.RemoteProtocolError), 503, "inventory_unavailable"),
        (_raise(httpx.ConnectTimeout), 504, "inventory_timeout"),
        (_raise(httpx.ReadTimeout), 504, "inventory_timeout"),
        (_raise(httpx.DecodingError), 502, "inventory_bad_response"),
        (lambda _: httpx.Response(500, text="boom"), 502, "inventory_bad_status"),
        (lambda _: httpx.Response(503), 502, "inventory_bad_status"),
        (
            lambda _: httpx.Response(302, headers={"Location": "http://evil"}),
            502,
            "inventory_bad_status",
        ),
        (lambda _: httpx.Response(200, text="not json"), 502, "inventory_bad_response"),
        (lambda _: httpx.Response(200, json={"item_id": "widget"}), 502, "inventory_bad_response"),
        (_item(5, item_id="other"), 502, "inventory_bad_response"),
    ],
)
def test_upstream_failures(caplog, handler, status, error):
    caplog.set_level(logging.INFO)
    client, upstream = _client(handler)
    with client:
        response = client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-fail"})
    assert response.status_code == status
    body = response.json()["detail"]
    assert body["error"] == error
    assert body["request_id"] == "rid-fail"
    assert response.headers[REQUEST_ID_HEADER] == "rid-fail"
    assert len(upstream.requests) == 1  # no retries, no redirect following
    access = access_records(caplog, "/orders/check/widget")
    assert access[0].fields["status"] == status
    assert access[0].request_id == "rid-fail"


def test_unavailable_logs_warning_with_context(caplog):
    caplog.set_level(logging.INFO)
    client, _ = _client(_raise(httpx.ConnectError))
    with client:
        client.get("/orders/check/widget", headers={REQUEST_ID_HEADER: "rid-warn"})
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == "inventory-service unavailable"
    assert warning.request_id == "rid-warn"
    assert warning.fields["error"] == "ConnectError"
    assert warning.fields["upstream"] == UPSTREAM
    assert "duration_ms" in warning.fields


@pytest.mark.parametrize("item_id", ["WIDGET", "..", "a" * 65, "x%2F..%2Fadmin"])
def test_invalid_item_id_never_calls_upstream(item_id):
    client, upstream = _client(_item())
    with client:
        assert client.get(f"/orders/check/{item_id}").status_code in (404, 422)
    assert upstream.requests == []


def test_readiness_does_not_depend_on_inventory():
    client, upstream = _client(_raise(httpx.ConnectError))
    with client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert upstream.requests == []


def test_http_client_uses_configured_timeout_and_base_url():
    settings = Settings(inventory_url="http://inv.example:9000", inventory_timeout_seconds=0.25)
    app = create_app(settings, transport=httpx.MockTransport(_item()))
    with TestClient(app):
        client: httpx.AsyncClient = app.state.inventory
        assert client.timeout == httpx.Timeout(0.25)
        assert str(client.base_url) == "http://inv.example:9000"
        assert client.follow_redirects is False


def test_end_to_end_with_real_inventory_app_propagates_request_id(caplog):
    """orders-service -> real inventory-service ASGI app, in-process."""
    caplog.set_level(logging.INFO)
    transport = httpx.ASGITransport(app=create_inventory_app())
    with TestClient(create_app(SETTINGS, transport=transport)) as client:
        ok = client.get("/orders/check/gizmo", headers={REQUEST_ID_HEADER: "e2e-rid-1"})
        missing = client.get("/orders/check/nothing", headers={REQUEST_ID_HEADER: "e2e-rid-2"})
    assert ok.status_code == 200
    assert ok.json() == {"item_id": "gizmo", "orderable": True, "quantity_available": 7}
    assert missing.status_code == 404
    inventory_access = access_records(caplog, "/inventory/gizmo")
    assert [r.request_id for r in inventory_access] == ["e2e-rid-1"]
    assert access_records(caplog, "/inventory/nothing")[0].request_id == "e2e-rid-2"


# Settings


def test_settings_defaults():
    s = Settings.from_env({})
    assert s.inventory_url == "http://inventory-service:8080"
    assert s.inventory_timeout_seconds == 2.0


def test_settings_from_env():
    s = Settings.from_env({"INVENTORY_URL": f"{UPSTREAM}/", "INVENTORY_TIMEOUT_SECONDS": "1.5"})
    assert s.inventory_url == UPSTREAM
    assert s.inventory_timeout_seconds == 1.5


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"INVENTORY_URL": "ftp://inventory"}, "absolute http"),
        ({"INVENTORY_URL": "inventory-service:8080"}, "absolute http"),
        ({"INVENTORY_URL": "http://"}, "absolute http"),
        ({"INVENTORY_URL": "http://user:pw@inventory:8080"}, "credentials"),
        ({"INVENTORY_URL": "http://inventory:8080?x=1"}, "query"),
        ({"INVENTORY_TIMEOUT_SECONDS": "0"}, "between"),
        ({"INVENTORY_TIMEOUT_SECONDS": "31"}, "between"),
        ({"INVENTORY_TIMEOUT_SECONDS": "nan"}, "between"),
        ({"INVENTORY_TIMEOUT_SECONDS": "inf"}, "between"),
        ({"INVENTORY_TIMEOUT_SECONDS": "two"}, "number"),
    ],
)
def test_settings_rejects_invalid(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)

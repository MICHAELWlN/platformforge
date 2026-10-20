from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from inventory_service.app import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_item_in_stock(client):
    response = client.get("/inventory/widget")
    assert response.status_code == 200
    assert response.json() == {
        "item_id": "widget",
        "name": "Widget",
        "quantity": 42,
        "in_stock": True,
    }


def test_item_out_of_stock(client):
    assert client.get("/inventory/gadget").json()["in_stock"] is False


def test_unknown_item_404(client):
    response = client.get("/inventory/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": {"error": "item_not_found"}}


@pytest.mark.parametrize("item_id", ["Widget", "-widget", "a" * 65, "wid_get", "wid%20get"])
def test_invalid_item_id_422(client, item_id):
    assert client.get(f"/inventory/{item_id}").status_code == 422


def test_service_endpoints(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/version").json()["service"] == "inventory-service"


def test_failure_injection_off_by_default(monkeypatch):
    monkeypatch.delenv("INVENTORY_FAILURE_PERCENT", raising=False)
    with TestClient(create_app()) as c:
        assert all(c.get("/inventory/widget").status_code == 200 for _ in range(50))


def test_full_failure_injection_fails_lookups_but_not_health(monkeypatch, caplog):
    monkeypatch.setenv("INVENTORY_FAILURE_PERCENT", "100")
    with TestClient(create_app()) as c:
        response = c.get("/inventory/widget")
        assert response.status_code == 500
        assert response.json() == {"detail": {"error": "injected_failure"}}
        assert c.get("/inventory/does-not-exist").status_code == 500
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200
    assert any(r.getMessage() == "injected failure" for r in caplog.records)


def test_partial_failure_injection(monkeypatch):
    monkeypatch.setattr("inventory_service.app.random.uniform", lambda a, b: 30.0)
    with TestClient(create_app(failure_percent=25)) as c:
        assert c.get("/inventory/widget").status_code == 200
    with TestClient(create_app(failure_percent=31)) as c:
        assert c.get("/inventory/widget").status_code == 500


@pytest.mark.parametrize("raw", ["-1", "101", "ten", "50.5"])
def test_invalid_failure_percent_env_rejected(monkeypatch, raw):
    monkeypatch.setenv("INVENTORY_FAILURE_PERCENT", raw)
    with pytest.raises(ValueError, match="INVENTORY_FAILURE_PERCENT"):
        create_app()


@pytest.mark.parametrize("value", [-1, 101])
def test_invalid_failure_percent_argument_rejected(value):
    with pytest.raises(ValueError, match="between 0 and 100"):
        create_app(failure_percent=value)

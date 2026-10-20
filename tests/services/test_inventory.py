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

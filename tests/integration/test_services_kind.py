"""Integration tests for orders-service and inventory-service on a local kind cluster.

Prerequisite: images built and loaded into kind (`make images kind-load`).

Services are deployed with the PlatformForge CLI itself. HTTP calls from the test go
through the Kubernetes API server's service proxy, so nothing is exposed outside the
cluster; orders-service reaches inventory-service via in-cluster Service DNS.

test_08 temporarily scales inventory-service to 0 replicas to simulate an outage, then
restores it with `platformforge deploy`. Nothing is deleted.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

import pytest
from kubernetes.client.exceptions import ApiException

from platformforge import deployer
from platformforge.kubecontext import KubeClients
from platformforge.manifest import load_manifest
from tests.conftest import REPO_ROOT
from tests.integration.conftest import NAMESPACE, run_cli

pytestmark = pytest.mark.integration

MANIFESTS = REPO_ROOT / "services" / "manifests"
INVENTORY = MANIFESTS / "inventory-service.yaml"
ORDERS = MANIFESTS / "orders-service.yaml"
INVENTORY_DNS = "http://inventory-service.platformforge-dev.svc.cluster.local:8080"


@pytest.fixture(scope="module")
def kube_with_images(kube: KubeClients) -> KubeClients:
    on_node = {
        name
        for node in kube.core.list_node().items
        for img in node.status.images
        for name in img.names
    }
    for path in (INVENTORY, ORDERS):
        image = f"docker.io/{load_manifest(path).spec.image}"
        if image not in on_node:
            pytest.fail(f"{image} is not loaded on the kind node; run `make images kind-load`")
    return kube


def http_get(kube: KubeClients, service: str, path: str, request_id: str):
    """GET http://<service>:http/<path> via the API server proxy -> (status, headers, json).

    Header names are lowercased: the API server (Go) canonicalizes them, e.g. X-Request-Id.
    """
    try:
        resp = kube.core.connect_get_namespaced_service_proxy_with_path(
            f"{service}:http",
            NAMESPACE,
            path.lstrip("/"),
            _headers={"X-Request-ID": request_id},
            _preload_content=False,
            _request_timeout=15,
        )
        return resp.status, _lower(resp.headers), json.loads(resp.data)
    except ApiException as exc:
        body = json.loads(exc.body) if exc.body else None
        return exc.status, _lower(exc.headers or {}), body


def _lower(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def wait_until(predicate: Callable[[], object], timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            return result
        time.sleep(interval)


def json_log_lines(kube: KubeClients, manifest_path) -> list[dict]:
    lines = []
    for pod_logs in deployer.get_logs(kube.core, load_manifest(manifest_path), tail_lines=500):
        for line in pod_logs.text.splitlines():
            try:
                lines.append(json.loads(line) | {"_pod": pod_logs.pod})
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON log line from {pod_logs.pod}: {line!r}")
    return lines


def rid() -> str:
    return f"it-{uuid.uuid4().hex[:16]}"


def test_01_deploy_inventory_with_platformforge(kube_with_images):
    result = run_cli("deploy", str(INVENTORY), "--create-namespace", "--timeout", "180")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Rollout complete: 2/2 available" in result.output


def test_02_deploy_orders_with_platformforge(kube_with_images):
    result = run_cli("deploy", str(ORDERS), "--timeout", "180")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Rollout complete: 2/2 available" in result.output


@pytest.mark.parametrize("manifest", [INVENTORY, ORDERS], ids=["inventory", "orders"])
def test_03_status_healthy(kube_with_images, manifest):
    result = run_cli("status", str(manifest))
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "desired=2 updated=2 ready=2 available=2" in result.output
    assert "Endpoint:   ClusterIP" in result.output
    assert "Health:     HEALTHY" in result.output


@pytest.mark.parametrize("service", ["inventory-service", "orders-service"])
def test_04_health_and_version_endpoints(kube_with_images, service):
    assert http_get(kube_with_images, service, "/healthz", rid())[0] == 200
    assert http_get(kube_with_images, service, "/readyz", rid())[0] == 200
    status, _, body = http_get(kube_with_images, service, "/version", rid())
    assert status == 200
    assert body["service"] == service
    assert body["version"] == "0.1.0"


def test_05_orders_calls_inventory_and_propagates_request_id(kube_with_images):
    request_id = rid()
    status, headers, body = http_get(
        kube_with_images, "orders-service", "/orders/check/widget", request_id
    )
    print(status, body)
    assert status == 200
    assert body == {"item_id": "widget", "orderable": True, "quantity_available": 42}
    assert headers.get("x-request-id") == request_id

    def inventory_saw_it():
        return [
            line
            for line in json_log_lines(kube_with_images, INVENTORY)
            if line.get("request_id") == request_id and line.get("path") == "/inventory/widget"
        ]

    inventory_lines = wait_until(inventory_saw_it, timeout=15)
    assert len(inventory_lines) == 1, "inventory-service did not log the propagated request ID"
    assert inventory_lines[0]["status"] == 200
    print("inventory log:", inventory_lines[0])

    orders_lines = [
        line
        for line in json_log_lines(kube_with_images, ORDERS)
        if line.get("request_id") == request_id
        and line.get("message") == "inventory-service responded"
    ]
    assert len(orders_lines) == 1
    assert orders_lines[0]["upstream"] == INVENTORY_DNS
    print("orders log:", orders_lines[0])


def test_06_unknown_item_is_404(kube_with_images):
    status, _, body = http_get(
        kube_with_images, "orders-service", "/orders/check/no-such-item", rid()
    )
    assert status == 404
    assert body["detail"]["error"] == "item_not_found"


def test_07_invalid_item_rejected_by_orders(kube_with_images):
    status, _, _ = http_get(kube_with_images, "orders-service", "/orders/check/NOT_VALID", rid())
    assert status == 422


def test_08_inventory_outage_returns_503_then_recovers(kube_with_images):
    kube = kube_with_images
    dep = kube.apps.read_namespaced_deployment("inventory-service", NAMESPACE)
    assert dep.metadata.labels.get("app.kubernetes.io/managed-by") == "platformforge"

    kube.apps.patch_namespaced_deployment_scale(
        "inventory-service",
        NAMESPACE,
        {"spec": {"replicas": 0}},
        _content_type="application/merge-patch+json",
    )
    try:

        def outage_observed():
            result = http_get(kube, "orders-service", "/orders/check/widget", rid())
            return result if result[0] == 503 else None

        outage = wait_until(outage_observed, timeout=90, interval=2)
        assert outage, "orders-service never reported inventory-service as unavailable"
        status, headers, body = outage
        print("during outage:", status, body)
        assert body["detail"]["error"] == "inventory_unavailable"
        assert body["detail"]["request_id"].startswith("it-")
        assert body["detail"]["request_id"] == headers.get("x-request-id")

        # orders-service stays Ready: an upstream outage must not cascade into its own
        # readiness and remove it from its Service.
        assert http_get(kube, "orders-service", "/readyz", rid())[0] == 200
        orders = kube.apps.read_namespaced_deployment("orders-service", NAMESPACE)
        assert orders.status.available_replicas == 2
    finally:
        restore = run_cli("deploy", str(INVENTORY), "--timeout", "180")
        print(restore.output)
    assert restore.exit_code == 0, restore.output
    assert "Deployment/inventory-service: updated" in restore.output
    assert "Rollout complete: 2/2 available" in restore.output

    def recovered():
        result = http_get(kube, "orders-service", "/orders/check/widget", rid())
        return result if result[0] == 200 else None

    recovery = wait_until(recovered, timeout=60, interval=2)
    assert recovery, "orders-service did not recover after inventory-service was restored"
    print("after recovery:", recovery[0], recovery[2])
    assert recovery[2]["orderable"] is True


@pytest.mark.parametrize("manifest", [INVENTORY, ORDERS], ids=["inventory", "orders"])
def test_09_healthy_after_recovery(kube_with_images, manifest):
    result = run_cli("status", str(manifest))
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Health:     HEALTHY" in result.output
    assert "restarts=0" in result.output
    assert "restarts=1" not in result.output

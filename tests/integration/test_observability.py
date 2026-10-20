"""Milestone 4: metrics, logs, and traces from real platformforge-dev traffic.

Requires the observability stack from docs/OBSERVABILITY.md and the mesh left by
test_services_mesh.py: STRICT mTLS, inventory authorized for orders, stable routing,
healthy canary. These tests do not skip when that stack is missing; they fail.
The canary drill restores stable routing and the healthy canary before returning.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import uuid

import pytest

from platformforge.kubecontext import KubeClients
from tests.conftest import REPO_ROOT
from tests.integration import mesh
from tests.integration.conftest import run_cli

pytestmark = pytest.mark.integration

MANIFESTS = REPO_ROOT / "services" / "manifests"
CANARY = MANIFESTS / "inventory-service-v0.2.0.yaml"
CANARY_FAULTY = MANIFESTS / "incidents" / "inventory-service-v0.2.0-faulty.yaml"
OBS = "platformforge-observability"
PROM = f"http://prometheus-server.{OBS}.svc.cluster.local"
LOKI = f"http://loki.{OBS}.svc.cluster.local:3100"
TEMPO = f"http://tempo.{OBS}.svc.cluster.local:3200"
GRAFANA = f"http://grafana.{OBS}.svc.cluster.local"
STABLE = {mesh.subset_cluster("v0-1-0"): 100}
CANARY_SPLIT = {mesh.subset_cluster("v0-1-0"): 90, mesh.subset_cluster("v0-2-0"): 10}


@pytest.fixture(scope="module")
def k(kube: KubeClients) -> KubeClients:
    mesh.ensure_client_pod(kube, mesh.MESH_CLIENT, mesh=True)
    mesh.ensure_client_pod(kube, mesh.PLAIN_CLIENT, mesh=False)
    return kube


def _get(k: KubeClients, url: str) -> dict:
    result = mesh.http(
        k,
        mesh.PLAIN_CLIENT,
        "client",
        url,
        count=1,
        bodies=True,
        body_limit=500_000,
        timeout=20,
    )[0]
    assert result["status"] == 200, result
    return json.loads(result["body"])


def _prom(k: KubeClients, expr: str) -> list[dict]:
    url = f"{PROM}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    body = _get(k, url)
    assert body["status"] == "success", body
    return body["data"]["result"]


def _counter(k: KubeClients, expr: str) -> float:
    rows = _prom(k, expr)
    return sum(float(row["value"][1]) for row in rows)


def _wait_counter(k: KubeClients, expr: str, before: float, timeout: float = 30) -> float:
    deadline = time.monotonic() + timeout
    while True:
        value = _counter(k, expr)
        if value > before:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"{expr} stayed at {value} (before {before})")
        time.sleep(2)


def _loki(k: KubeClients, query: str) -> list[dict]:
    end = time.time_ns()
    start = end - 15 * 60 * 1_000_000_000
    params = urllib.parse.urlencode(
        {"query": query, "start": str(start), "end": str(end), "limit": "20"}
    )
    url = f"{LOKI}/loki/api/v1/query_range?{params}"
    body = _get(k, url)
    assert body["status"] == "success", body
    return body["data"]["result"]


def _wait_loki(k: KubeClients, query: str, timeout: float = 30) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        streams = _loki(k, query)
        if streams:
            return streams
        if time.monotonic() > deadline:
            pytest.fail(f"Loki returned no streams for {query}")
        time.sleep(2)


def _span_id(value: str | None) -> str:
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded).hex()


def test_prometheus_scrapes_istio_sidecars(k):
    targets = _get(k, f"{PROM}/api/v1/targets")["data"]["activeTargets"]
    up = [
        t
        for t in targets
        if t["health"] == "up"
        and t["labels"].get("namespace") == "platformforge-dev"
        and t["scrapeUrl"].endswith(":15020/stats/prometheus")
    ]
    pods = {t["labels"].get("pod", "") for t in up}
    assert any(name.startswith("orders-service") for name in pods), pods
    assert any(name.startswith("inventory-service-v0-1-0") for name in pods), pods
    assert any(name.startswith("inventory-service-v0-2-0") for name in pods), pods
    assert all(not t["lastError"] for t in up)


def test_request_metrics_move_for_both_services(k):
    expr = (
        'sum(istio_requests_total{reporter="source",source_workload="orders-service",'
        'destination_service_name="inventory-service",destination_version="v0.1.0",'
        'response_code="200"})'
    )
    before = _counter(k, expr)
    sent = mesh.http(
        k,
        mesh.MESH_CLIENT,
        "client",
        f"{mesh.ORDERS_URL}/orders/check/widget",
        count=5,
        rid_prefix=f"obs-metric-{uuid.uuid4().hex[:8]}",
    )
    assert {row["status"] for row in sent} == {200}
    after = _wait_counter(k, expr, before)
    assert after >= before + 1
    rows = _prom(
        k,
        'istio_requests_total{reporter="source",destination_service_namespace="platformforge-dev"}',
    )
    labels = [row["metric"] for row in rows]
    assert any(
        row.get("destination_canonical_service") == "orders-service" and row.get("response_code")
        for row in labels
    )
    assert any(
        row.get("destination_version") == "v0.1.0"
        and row.get("destination_workload", "").startswith("inventory-service")
        for row in labels
    )


def test_distributed_trace_correlates_logs(k):
    request_id = f"obs-trace-{uuid.uuid4().hex[:12]}"
    sent = mesh.http(
        k,
        mesh.MESH_CLIENT,
        "client",
        f"{mesh.ORDERS_URL}/orders/check/widget",
        count=1,
        rid_prefix=request_id,
    )[0]
    assert sent["status"] == 200, sent

    orders = _wait_loki(
        k, f'{{namespace="platformforge-dev", container="orders-service"}} |= "{request_id}"'
    )
    inventory = _wait_loki(
        k, f'{{namespace="platformforge-dev", container="inventory-service"}} |= "{request_id}"'
    )
    orders_line = json.loads(orders[0]["values"][0][1])
    inventory_line = json.loads(inventory[0]["values"][0][1])
    assert orders_line["request_id"] == request_id
    assert inventory_line["request_id"] == request_id
    assert orders_line["trace_id"] == inventory_line["trace_id"]
    assert len(orders_line["trace_id"]) == 32
    assert len(orders_line["span_id"]) == 16
    assert orders_line["ts"].startswith("20")
    print(
        f"trace: request_id={request_id} trace_id={orders_line['trace_id']} "
        f"orders_ts={orders_line['ts']} inventory_ts={inventory_line['ts']}"
    )

    proxy = _wait_loki(
        k,
        f'{{namespace="platformforge-dev", container="istio-proxy", app="orders-service"}}'
        f' |= "{request_id}"',
    )
    proxy_line = json.loads(proxy[0]["values"][0][1])
    assert proxy_line["request_id"] == request_id
    assert proxy_line["trace_id"] == orders_line["trace_id"]

    trace = _get(k, f"{TEMPO}/api/traces/{orders_line['trace_id']}")
    spans = []
    for batch in trace["batches"]:
        resource = {
            item["key"]: next(iter(item["value"].values()))
            for item in batch.get("resource", {}).get("attributes", [])
        }
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attrs = {
                    item["key"]: next(iter(item["value"].values()))
                    for item in span.get("attributes") or []
                }
                spans.append(
                    {
                        "service": resource.get("service.name"),
                        "name": span.get("name"),
                        "id": _span_id(span.get("spanId")),
                        "parent": _span_id(span.get("parentSpanId")),
                        "attrs": attrs,
                    }
                )
    services = {span["service"] for span in spans}
    assert "orders-service" in services
    assert "inventory-service" in services
    orders_server = next(
        span
        for span in spans
        if span["service"] == "orders-service" and "orders/check" in span["name"]
    )
    inventory_server = next(
        span
        for span in spans
        if span["service"] == "inventory-service" and span["name"].startswith("GET")
    )
    parents = {span["id"]: span["parent"] for span in spans}
    seen = set()
    current = inventory_server["id"]
    while current and current not in seen:
        seen.add(current)
        current = parents.get(current, "")
    assert orders_server["id"] in seen
    assert orders_server["attrs"].get("platformforge.request_id") == request_id
    header_keys = [
        key
        for span in spans
        for key in span["attrs"]
        if key.lower().startswith("http.request.header")
    ]
    assert header_keys == []


def test_grafana_datasources_and_dashboard(k):
    health = _get(k, f"{GRAFANA}/api/health")
    assert health["database"] == "ok"
    for uid in ("prometheus", "loki", "tempo"):
        status = _get(k, f"{GRAFANA}/api/datasources/uid/{uid}/health")
        assert status["status"] == "OK", status
    dashboard = _get(k, f"{GRAFANA}/api/dashboards/uid/platformforge-overview")["dashboard"]
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert {
        "Request rate by destination and version",
        "Error rate (5xx) by destination and version",
        "Request latency p95",
        "Inventory canary split by destination version",
        "Service health (successful requests / all)",
        "Sidecar scrape targets up",
    } <= titles
    expr = dashboard["panels"][0]["targets"][0]["expr"]
    assert _prom(k, expr), expr


def test_canary_errors_show_in_prometheus_and_recover(k):
    orders = [pod.metadata.name for pod in mesh.pods_of(k, "app.kubernetes.io/name=orders-service")]
    v02_5xx = (
        'sum(istio_requests_total{reporter="source",destination_service_name="inventory-service",'
        'destination_version="v0.2.0",response_code=~"5.."})'
    )
    v01_5xx = (
        'sum(istio_requests_total{reporter="source",destination_service_name="inventory-service",'
        'destination_version="v0.1.0",response_code=~"5.."})'
    )
    try:
        mesh.apply_mesh_config(k, "inventory-virtualservice-canary.yaml")
        mesh.wait_for_weights(k, orders, CANARY_SPLIT)
        result = run_cli("deploy", str(CANARY_FAULTY), "--timeout", "180")
        assert result.exit_code == 0, result.output
        mesh.wait_for_weights(k, orders, CANARY_SPLIT)
        before_bad = _counter(k, v02_5xx)
        before_good = _counter(k, v01_5xx)
        sent = mesh.http(
            k,
            mesh.MESH_CLIENT,
            "client",
            f"{mesh.ORDERS_URL}/orders/check/widget",
            count=40,
            rid_prefix=f"obs-drill-{uuid.uuid4().hex[:8]}",
            bodies=True,
            body_limit=500,
        )
        failed = [row for row in sent if row["status"] == 502]
        assert failed, [row["status"] for row in sent]
        assert all(row["status"] in (200, 502) for row in sent)
        after_bad = _wait_counter(k, v02_5xx, before_bad)
        after_good = _counter(k, v01_5xx)
        print(
            f"drill: client_502={len(failed)} client_200={len(sent) - len(failed)} "
            f"prom_v0.2.0_5xx {before_bad}->{after_bad} prom_v0.1.0_5xx {before_good}->{after_good}"
        )
        assert after_good == before_good
        assert after_bad >= before_bad + 1

        failed_id = failed[0]["rid"]
        streams = _wait_loki(
            k,
            f'{{namespace="platformforge-dev", container="inventory-service"}} |= "{failed_id}"',
        )
        line = json.loads(streams[0]["values"][0][1])
        assert line["request_id"] == failed_id
        trace = _get(k, f"{TEMPO}/api/traces/{line['trace_id']}")
        services = set()
        for batch in trace["batches"]:
            for item in batch.get("resource", {}).get("attributes", []):
                if item["key"] == "service.name":
                    services.add(next(iter(item["value"].values())))
        assert "inventory-service" in services
        assert "orders-service" in services

        mesh.apply_mesh_config(k, "inventory-virtualservice-stable.yaml")
        mesh.wait_for_weights(k, orders, STABLE)
        time.sleep(20)
        recovered_bad = _counter(k, v02_5xx)
        recovered = mesh.http(
            k,
            mesh.MESH_CLIENT,
            "client",
            f"{mesh.ORDERS_URL}/orders/check/widget",
            count=20,
            rid_prefix=f"obs-recover-{uuid.uuid4().hex[:8]}",
        )
        assert {row["status"] for row in recovered} == {200}
        time.sleep(20)
        stayed = _counter(k, v02_5xx)
        print(f"recovery: prom_v0.2.0_5xx stayed {recovered_bad} -> {stayed}")
        assert stayed == recovered_bad
    finally:
        mesh.apply_mesh_config(k, "inventory-virtualservice-stable.yaml")
        restored = run_cli("deploy", str(CANARY), "--timeout", "180")
        assert restored.exit_code == 0, restored.output
        mesh.wait_for_weights(k, orders, STABLE)
        canary = mesh.pods_of(
            k, "app.kubernetes.io/name=inventory-service,app.kubernetes.io/version=v0.2.0"
        )
        env = {item.name: item.value for item in canary[0].spec.containers[0].env}
        assert env["INVENTORY_FAILURE_PERCENT"] == "0"

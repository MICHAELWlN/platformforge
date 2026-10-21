"""Five kind experiments against the running canary controller.

Set PLATFORMFORGE_CANARY_IT=1. The controller Deployment must already be ready
on kind-platformforge. This module loads that context with persist_config=False.
It restores the stable route and the healthy canary before returning.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from platformforge import kubecontext
from platformforge.canary_apply import KIND_CONTEXT, apply_reviewed_route, controller_route
from platformforge.deployer import deploy, wait_for_rollout
from platformforge.kubecontext import KubeClients
from platformforge.manifest import load_manifest
from tests.conftest import REPO_ROOT
from tests.integration import mesh

pytestmark = pytest.mark.skipif(
    os.environ.get("PLATFORMFORGE_CANARY_IT") != "1",
    reason="set PLATFORMFORGE_CANARY_IT=1 to run the kind controller experiments",
)

NAMESPACE = "platformforge-dev"
MANIFESTS = REPO_ROOT / "services" / "manifests"
HEALTHY = MANIFESTS / "inventory-service-v0.2.0.yaml"
FAULTY = MANIFESTS / "incidents" / "inventory-service-v0.2.0-faulty.yaml"
SLOW = MANIFESTS / "incidents" / "inventory-service-v0.2.0-slow.yaml"
STABLE = {mesh.subset_cluster("v0-1-0"): 100}
CANARY = {mesh.subset_cluster("v0-1-0"): 90, mesh.subset_cluster("v0-2-0"): 10}
PROMOTED = {mesh.subset_cluster("v0-2-0"): 100}
PROM_URL = "http://prometheus-server.platformforge-observability.svc.cluster.local"


def _clients() -> KubeClients:
    api = config.new_client_from_config(context=KIND_CONTEXT, persist_config=False)
    return KubeClients(core=client.CoreV1Api(api), apps=client.AppsV1Api(api))


def _route(clients: KubeClients, stable: int, canary: int) -> None:
    from platformforge.canary_apply import reviewed_route

    apply_reviewed_route(
        clients,
        context=KIND_CONTEXT,
        namespace=NAMESPACE,
        filename=reviewed_route(stable, canary),
    )


def _env(clients: KubeClients, name: str) -> str | None:
    deployment = clients.apps.read_namespaced_deployment("inventory-service-v0-2-0", NAMESPACE)
    for container in deployment.spec.template.spec.containers:
        if container.name != "inventory-service":
            continue
        for item in container.env or []:
            if item.name == name:
                return item.value
    return None


def _orders(clients: KubeClients) -> list[str]:
    return [
        pod.metadata.name for pod in mesh.pods_of(clients, "app.kubernetes.io/name=orders-service")
    ]


def _logs_since(clients: KubeClients, since: int) -> list[dict]:
    text = clients.core.read_namespaced_pod_log(
        _controller_pod(clients),
        NAMESPACE,
        container="canary-controller",
        since_seconds=max(since, 1),
    )
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _controller_pod(clients: KubeClients) -> str:
    pods = clients.core.list_namespaced_pod(
        NAMESPACE, label_selector="app.kubernetes.io/name=canary-controller"
    ).items
    ready = [pod.metadata.name for pod in pods if pod.status.phase == "Running"]
    if not ready:
        pytest.fail("canary-controller is not running")
    return ready[0]


def _wait_decision(clients: KubeClients, started: float, predicate, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        since = max(int(time.time() - started) + 2, 1)
        seen = [row for row in _logs_since(clients, since) if row.get("event") == "decision"]
        for row in seen:
            if predicate(row):
                return row
        time.sleep(5)
    pytest.fail(f"controller decision was not observed: {seen[-3:]}")


def _traffic(clients: KubeClients, count: int, prefix: str) -> list[dict]:
    return mesh.http(
        clients,
        mesh.MESH_CLIENT,
        "client",
        f"{mesh.ORDERS_URL}/orders/check/widget",
        count=count,
        rid_prefix=prefix,
        bodies=False,
        timeout=8,
    )


def _deploy(clients: KubeClients, path) -> None:
    manifest = load_manifest(path)
    deploy(clients, manifest)
    wait_for_rollout(clients.apps, manifest, timeout_seconds=180)


def _restore(clients: KubeClients, orders: list[str]) -> None:
    _route(clients, 100, 0)
    _deploy(clients, HEALTHY)
    mesh.wait_for_weights(clients, orders, STABLE, timeout=60)
    observed = time.time()
    _wait_decision(
        clients,
        observed,
        lambda item: item.get("reason") == "route is already stable",
        45,
    )


def test_five_controller_experiments() -> None:
    started_context = kubecontext.current_context().name
    print(f"current_context={started_context} experiment_context={KIND_CONTEXT}")
    clients = _clients()
    mesh.ensure_client_pod(clients, mesh.MESH_CLIENT, mesh=True)
    orders = _orders(clients)
    assert orders
    evidence: dict[str, object] = {}
    try:
        _restore(clients, orders)
        assert _env(clients, "INVENTORY_FAILURE_PERCENT") == "0"
        evidence["1_healthy"] = _experiment_healthy(clients, orders)
        _restore(clients, orders)
        evidence["2_faulty"] = _experiment_faulty(clients, orders)
        _restore(clients, orders)
        evidence["3_latency"] = _experiment_latency(clients, orders)
        _restore(clients, orders)
        evidence["4_missing"] = _experiment_missing(clients, orders)
        _restore(clients, orders)
        evidence["5_repeat"] = _experiment_repeat(clients)
        evidence["metrics"] = _scrape_metrics(clients)
        print("CONTROLLER_EVIDENCE " + json.dumps(evidence))
        assert evidence["1_healthy"]["action"] == "promote"
        assert evidence["2_faulty"]["action"] == "rollback"
        assert "error rate" in evidence["2_faulty"]["reason"]
        assert evidence["3_latency"]["action"] == "rollback"
        reason = evidence["3_latency"]["reason"]
        assert "p95" in reason or "latency" in reason
        assert evidence["4_missing"]["promoted"] is False
        assert evidence["4_missing"]["ended"] == "100/0"
        assert evidence["5_repeat"]["applied"] == 2
        assert evidence["5_repeat"]["stable_ready"] >= 1
        assert evidence["5_repeat"]["sa_can_delete"] is False
        assert evidence["5_repeat"]["sa_can_update_route"] is True
        assert evidence["metrics"]["rollbacks"] >= 1
    finally:
        _restore_controller_url(clients)
        _restore(clients, orders)
        assert kubecontext.current_context().name == started_context


def _wait_baseline(clients: KubeClients, started: float) -> None:
    _wait_decision(clients, started, lambda item: item.get("reason") == "baseline recorded", 90)


def _experiment_healthy(clients: KubeClients, orders: list[str]) -> dict:
    mark = time.time()
    _route(clients, 90, 10)
    mesh.wait_for_weights(clients, orders, CANARY, timeout=90)
    _wait_baseline(clients, mark)
    sent = _traffic(clients, 400, f"healthy-{int(mark)}")
    row = _wait_decision(
        clients,
        mark,
        lambda item: item.get("route_action") in {"promote", "rollback"},
        200,
    )
    weights = _sidecar(clients, orders)
    return {
        "action": row.get("action"),
        "reason": row.get("reason"),
        "error_ratio": row.get("error_ratio"),
        "p95_regression": row.get("p95_regression"),
        "stable_p95_ms": row.get("stable_p95_ms"),
        "canary_p95_ms": row.get("canary_p95_ms"),
        "canary_total": row.get("canary_total"),
        "episode_age": row.get("episode_age"),
        "statuses": _count(sent),
        "weights": weights,
    }


def _experiment_faulty(clients: KubeClients, orders: list[str]) -> dict:
    mark = time.time()
    _deploy(clients, FAULTY)
    assert _env(clients, "INVENTORY_FAILURE_PERCENT") == "100"
    _route(clients, 90, 10)
    mesh.wait_for_weights(clients, orders, CANARY, timeout=90)
    _wait_baseline(clients, mark)
    sent = _traffic(clients, 400, f"faulty-{int(mark)}")
    row = _wait_decision(
        clients,
        mark,
        lambda item: item.get("route_action") == "rollback",
        200,
    )
    started = time.monotonic()
    mesh.wait_for_weights(clients, orders, STABLE, timeout=60)
    recovery = time.monotonic() - started
    recovered = _traffic(clients, 20, f"faulty-recover-{int(time.time())}")
    return {
        "action": row.get("action"),
        "reason": row.get("reason"),
        "error_ratio": row.get("error_ratio"),
        "canary_total": row.get("canary_total"),
        "canary_errors": row.get("canary_errors"),
        "statuses": _count(sent),
        "recovery_seconds": round(recovery, 3),
        "recovered": _count(recovered),
    }


def _experiment_latency(clients: KubeClients, orders: list[str]) -> dict:
    mark = time.time()
    _deploy(clients, SLOW)
    assert _env(clients, "INVENTORY_DELAY_MS") == "1000"
    assert _env(clients, "INVENTORY_FAILURE_PERCENT") == "0"
    _route(clients, 90, 10)
    mesh.wait_for_weights(clients, orders, CANARY, timeout=90)
    _wait_baseline(clients, mark)
    sent = _traffic(clients, 220, f"slow-{int(mark)}")
    row = _wait_decision(
        clients,
        mark,
        lambda item: item.get("route_action") == "rollback",
        200,
    )
    mesh.wait_for_weights(clients, orders, STABLE, timeout=60)
    recovered = _traffic(clients, 20, f"slow-recover-{int(time.time())}")
    return {
        "action": row.get("action"),
        "reason": row.get("reason"),
        "error_ratio": row.get("error_ratio"),
        "p95_regression": row.get("p95_regression"),
        "stable_p95_ms": row.get("stable_p95_ms"),
        "canary_p95_ms": row.get("canary_p95_ms"),
        "canary_total": row.get("canary_total"),
        "statuses": _count(sent),
        "recovered": _count(recovered),
    }


def _experiment_missing(clients: KubeClients, orders: list[str]) -> dict:
    _set_prom(clients, "http://127.0.0.1:9")
    _wait_controller(clients)
    time.sleep(20)
    stayed = _logs_since(clients, 25)
    promoted = any(row.get("route_action") == "promote" for row in stayed)
    _route(clients, 90, 10)
    opened = time.monotonic()
    mesh.wait_for_weights(clients, orders, CANARY, timeout=60)
    mesh.wait_for_weights(clients, orders, STABLE, timeout=200)
    return {
        "promoted": promoted,
        "fail_closed_seconds": round(time.monotonic() - opened, 3),
        "ended": "100/0",
        "sample": [row.get("reason") for row in stayed if row.get("event") == "decision"][:3],
    }


def _experiment_repeat(clients: KubeClients) -> dict:
    mark = time.time()
    for _ in range(2):
        apply_reviewed_route(
            clients,
            context=KIND_CONTEXT,
            namespace=NAMESPACE,
            filename=controller_route("rollback"),
        )
    time.sleep(20)
    since = int(time.time() - mark) + 2
    rows = [row for row in _logs_since(clients, since) if row.get("event") == "decision"]
    holds = [row for row in rows if row.get("reason") == "route is already stable"]
    deployment = clients.apps.read_namespaced_deployment("inventory-service-v0-1-0", NAMESPACE)
    return {
        "applied": 2,
        "stable_holds": len(holds),
        "stable_ready": deployment.status.ready_replicas or 0,
        "route_actions": [row.get("route_action") for row in holds],
        "sa_can_delete": _sa_allowed(clients, "delete", "deployments", "apps"),
        "sa_can_update_route": _sa_allowed(
            clients, "update", "virtualservices", "networking.istio.io"
        ),
    }


def _scrape_metrics(clients: KubeClients) -> dict:
    del clients
    proc = _forward()
    try:
        deadline = time.monotonic() + 45
        last = ""
        query = urllib.parse.urlencode({"query": "platformforge_canary_rollbacks_total"})
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:19091/api/v1/query?{query}", timeout=5
                ) as response:
                    text = response.read().decode()
            except OSError as exc:
                last = str(exc)
                time.sleep(5)
                continue
            value = _metric_value(text)
            if value >= 1:
                return {"rollbacks": value, "body": text[:400]}
            last = text[:300]
            time.sleep(5)
        return {"rollbacks": _metric_value(last), "body": last}
    finally:
        _stop(proc)


def _metric_value(body: str) -> float:
    try:
        payload = json.loads(body[body.find("{") :])
    except json.JSONDecodeError:
        return 0
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0
    return float(result[0]["value"][1])


def _set_prom(clients: KubeClients, url: str) -> None:
    deployment = clients.apps.read_namespaced_deployment("canary-controller", NAMESPACE)
    for container in deployment.spec.template.spec.containers:
        if container.name != "canary-controller":
            continue
        for item in container.env or []:
            if item.name == "PLATFORMFORGE_PROMETHEUS_URL":
                item.value = url
    clients.apps.replace_namespaced_deployment("canary-controller", NAMESPACE, deployment)
    _wait_controller(clients)


def _restore_controller_url(clients: KubeClients) -> None:
    try:
        current = _controller_prom(clients)
    except ApiException:
        return
    if current == PROM_URL:
        return
    _set_prom(clients, PROM_URL)


def _controller_prom(clients: KubeClients) -> str | None:
    deployment = clients.apps.read_namespaced_deployment("canary-controller", NAMESPACE)
    for container in deployment.spec.template.spec.containers:
        for item in container.env or []:
            if item.name == "PLATFORMFORGE_PROMETHEUS_URL":
                return item.value
    return None


def _wait_controller(clients: KubeClients) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pods = [
            pod
            for pod in clients.core.list_namespaced_pod(
                NAMESPACE, label_selector="app.kubernetes.io/name=canary-controller"
            ).items
            if pod.metadata.deletion_timestamp is None
        ]
        statuses = pods[0].status.container_statuses if len(pods) == 1 else None
        if statuses and all(status.ready for status in statuses):
            return
        time.sleep(2)
    pytest.fail("canary-controller did not become ready")


def _sa_allowed(clients: KubeClients, verb: str, resource: str, group: str) -> bool:
    review = client.V1SubjectAccessReview(
        spec=client.V1SubjectAccessReviewSpec(
            user="system:serviceaccount:platformforge-dev:platformforge-canary-controller",
            resource_attributes=client.V1ResourceAttributes(
                namespace=NAMESPACE,
                verb=verb,
                group=group,
                resource=resource,
                name="inventory-service",
            ),
        )
    )
    result = client.AuthorizationV1Api(clients.core.api_client).create_subject_access_review(review)
    return bool(result.status and result.status.allowed)


def _sidecar(clients: KubeClients, orders: list[str]) -> dict:
    return {pod: mesh.route_weights(clients, pod) for pod in orders}


def _count(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _forward() -> subprocess.Popen[bytes]:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.fail("kubectl is not on PATH")
    proc = subprocess.Popen(  # noqa: S603
        [
            kubectl,
            "--context",
            KIND_CONTEXT,
            "-n",
            "platformforge-observability",
            "port-forward",
            "svc/prometheus-server",
            "19091:80",
            "--address",
            "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:19091/-/ready", timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return proc
        except OSError:
            time.sleep(0.4)
    proc.kill()
    pytest.fail("prometheus port-forward did not become ready")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

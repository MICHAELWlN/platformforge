"""Opt-in canary failure drill on kind-platformforge.

Set PLATFORMFORGE_CANARY_IT=1. The test loads that context with
persist_config=False, so the current kubeconfig context is not changed.
It deploys the existing faulty canary, routes 10 percent, and rolls back
only after Prometheus samples fail the analyzer. The healthy canary and
the stable VirtualService are restored in a finally block. GKE is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from platformforge import kubecontext
from platformforge.canary import (
    AnalysisThresholds,
    CanaryError,
    CanaryMetrics,
    RolloutState,
    analyze,
    http_get,
    latency_query,
    query_prometheus,
    requests_query,
    samples_from_prometheus,
    transition,
)
from platformforge.canary_apply import KIND_CONTEXT, apply_reviewed_route, reviewed_route
from platformforge.deployer import deploy, wait_for_rollout
from platformforge.kubecontext import KubeClients
from platformforge.manifest import load_manifest
from tests.conftest import REPO_ROOT
from tests.integration import mesh

_LIVE = pytest.mark.skipif(
    os.environ.get("PLATFORMFORGE_CANARY_IT") != "1",
    reason="set PLATFORMFORGE_CANARY_IT=1 to run the kind canary drill",
)

NAMESPACE = "platformforge-dev"
PROM = "http://127.0.0.1:19090"
STABLE_WEIGHTS = {mesh.subset_cluster("v0-1-0"): 100}
CANARY_WEIGHTS = {mesh.subset_cluster("v0-1-0"): 90, mesh.subset_cluster("v0-2-0"): 10}
MANIFESTS = REPO_ROOT / "services" / "manifests"
HEALTHY = MANIFESTS / "inventory-service-v0.2.0.yaml"
FAULTY = MANIFESTS / "incidents" / "inventory-service-v0.2.0-faulty.yaml"
LIMITS = AnalysisThresholds(
    max_error_ratio=0.05,
    max_p95_regression=0.50,
    min_requests=20,
    evaluation_seconds=60,
)


def _clients() -> KubeClients:
    api = config.new_client_from_config(context=KIND_CONTEXT, persist_config=False)
    return KubeClients(core=client.CoreV1Api(api), apps=client.AppsV1Api(api))


def _failure_percent(clients: KubeClients) -> str | None:
    deployment = clients.apps.read_namespaced_deployment("inventory-service-v0-2-0", NAMESPACE)
    for container in deployment.spec.template.spec.containers:
        if container.name != "inventory-service":
            continue
        for item in container.env or []:
            if item.name == "INVENTORY_FAILURE_PERCENT":
                return item.value
    return None


def _orders(clients: KubeClients) -> list[str]:
    pods = mesh.pods_of(clients, "app.kubernetes.io/name=orders-service")
    return [pod.metadata.name for pod in pods]


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
            "19090:80",
            "--address",
            "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{PROM}/-/ready", timeout=1) as response:  # noqa: S310
                if response.status == 200:
                    return proc
        except OSError:
            time.sleep(0.5)
    stderr = proc.stderr.read().decode() if proc.stderr is not None else ""
    proc.kill()
    pytest.fail(f"prometheus port-forward did not become ready: {stderr}")


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _route(clients: KubeClients, stable: int, canary: int) -> None:
    apply_reviewed_route(
        clients,
        context=KIND_CONTEXT,
        namespace=NAMESPACE,
        filename=reviewed_route(stable, canary),
    )


def _query(seconds: int) -> tuple[object, object]:
    requests = query_prometheus(
        PROM, requests_query("inventory-service", seconds), timeout=5, fetch=http_get
    )
    latency = query_prometheus(
        PROM, latency_query("inventory-service", seconds), timeout=5, fetch=http_get
    )
    return requests, latency


@_LIVE
def test_faulty_canary_rolls_back_from_prometheus() -> None:
    started_context = kubecontext.current_context().name
    print(f"current_context={started_context} experiment_context={KIND_CONTEXT}")
    clients = _clients()
    mesh.ensure_client_pod(clients, mesh.MESH_CLIENT, mesh=True)
    orders = _orders(clients)
    assert orders, "orders-service has no pods"
    proc: subprocess.Popen[bytes] | None = None
    evidence: dict[str, object] = {"experiment_context": KIND_CONTEXT}
    try:
        _route(clients, 100, 0)
        mesh.wait_for_weights(clients, orders, STABLE_WEIGHTS)
        healthy = load_manifest(HEALTHY)
        deploy(clients, healthy)
        wait_for_rollout(clients.apps, healthy, timeout_seconds=180)
        assert _failure_percent(clients) == "0"
        stable = clients.apps.read_namespaced_deployment("inventory-service-v0-1-0", NAMESPACE)
        assert (stable.status.available_replicas or 0) >= 1
        proc = _forward()

        faulty = load_manifest(FAULTY)
        deploy(clients, faulty)
        wait_for_rollout(clients.apps, faulty, timeout_seconds=180)
        assert _failure_percent(clients) == "100"
        state = transition(RolloutState.BASELINE, "start")
        _route(clients, 90, 10)
        mesh.wait_for_weights(clients, orders, CANARY_WEIGHTS)
        state = transition(state, "analyze")

        sent_at = time.monotonic()
        sent = mesh.http(
            clients,
            mesh.MESH_CLIENT,
            "client",
            f"{mesh.ORDERS_URL}/orders/check/widget",
            count=200,
            rid_prefix=f"canary-{int(sent_at)}",
            bodies=False,
        )
        failed = [row for row in sent if row["status"] == 502]
        assert failed, [row["status"] for row in sent]
        request_ids = tuple(row["rid"] for row in failed[:5])

        deadline = time.monotonic() + 90
        decision = None
        requests_payload: object = None
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - sent_at)
            try:
                requests_payload, latency_payload = _query(60)
                stable_sample, canary_sample = samples_from_prometheus(
                    requests_payload,
                    latency_payload,
                    stable_version="v0.1.0",
                    canary_version="v0.2.0",
                )
                decision = analyze(
                    stable=stable_sample,
                    canary=canary_sample,
                    thresholds=LIMITS,
                    observation_seconds=min(elapsed, LIMITS.evaluation_seconds),
                    ready=True,
                    request_ids=request_ids,
                )
            except (OSError, CanaryError) as exc:
                decision = exc
            if getattr(decision, "action", None) is not None and decision.reason.startswith(
                "canary error rate"
            ):
                break
            time.sleep(5)
        assert decision is not None and not isinstance(decision, Exception), decision
        assert decision.reason == "canary error rate above threshold", (
            decision,
            requests_payload,
        )
        metrics = CanaryMetrics()
        state = transition(state, "rollback")
        metrics.record(decision, time.monotonic() - sent_at, state)

        rollback_started = time.monotonic()
        _route(clients, 100, 0)
        mesh.wait_for_weights(clients, orders, STABLE_WEIGHTS, timeout=60)
        recovery_seconds = time.monotonic() - rollback_started
        state = transition(state, "confirm")
        again = transition(state, "rollback")
        _route(clients, 100, 0)
        mesh.wait_for_weights(clients, orders, STABLE_WEIGHTS, timeout=30)
        assert again is RolloutState.STABLE
        still = clients.apps.read_namespaced_deployment("inventory-service-v0-1-0", NAMESPACE)
        assert still.metadata.name == "inventory-service-v0-1-0"

        recovered = mesh.http(
            clients,
            mesh.MESH_CLIENT,
            "client",
            f"{mesh.ORDERS_URL}/orders/check/widget",
            count=30,
            rid_prefix=f"recover-{int(time.monotonic())}",
            bodies=False,
        )
        assert {row["status"] for row in recovered} == {200}, recovered

        trace_ids = _trace_ids(clients, request_ids[0])
        evidence = {
            "experiment_context": KIND_CONTEXT,
            "current_context_unchanged": kubecontext.current_context().name == started_context,
            "canary_total": decision.canary.total,
            "canary_errors": decision.canary.errors,
            "error_ratio": decision.error_ratio,
            "stable_total": decision.stable.total,
            "stable_errors": decision.stable.errors,
            "p95_canary_ms": decision.canary.p95_ms,
            "p95_stable_ms": decision.stable.p95_ms,
            "decision": decision.action.value,
            "reason": decision.reason,
            "request_ids": list(request_ids),
            "trace_ids": list(trace_ids),
            "client_502": len(failed),
            "client_200_during_canary": sum(row["status"] == 200 for row in sent),
            "recovery_seconds": round(recovery_seconds, 3),
            "recovered_200": len(recovered),
            "rollout_state": again.value,
            "metrics": metrics.render(),
        }
        print("CANARY_EVIDENCE " + json.dumps(evidence))
    finally:
        try:
            _route(clients, 100, 0)
            restored = load_manifest(HEALTHY)
            deploy(clients, restored)
            wait_for_rollout(clients.apps, restored, timeout_seconds=180)
            assert _failure_percent(clients) == "0"
            mesh.wait_for_weights(clients, orders, STABLE_WEIGHTS, timeout=60)
        finally:
            _stop(proc)
            finished = kubecontext.current_context().name
            print(f"current_context_after={finished}")
            assert finished == started_context


def _trace_ids(clients: KubeClients, request_id: str) -> tuple[str, ...]:
    pods = mesh.pods_of(clients, "app.kubernetes.io/name=inventory-service")
    found: list[str] = []
    for pod in pods:
        if "v0-2-0" not in (pod.metadata.name or ""):
            continue
        try:
            text = clients.core.read_namespaced_pod_log(
                pod.metadata.name,
                NAMESPACE,
                container="inventory-service",
                since_seconds=300,
            )
        except ApiException:
            continue
        for line in text.splitlines():
            if request_id not in line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            trace_id = payload.get("trace_id")
            if isinstance(trace_id, str) and len(trace_id) == 32:
                found.append(trace_id)
    return tuple(found[:3])


def test_dashboard_configmap_contains_canary_panels() -> None:
    """The reviewed dashboard file is what Grafana provisions. This does not apply it."""
    path = Path(REPO_ROOT) / "deploy" / "observability" / "dashboards" / "platformforge.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert "Canary versus stable error rate" in titles
    assert "Canary versus stable p95 latency" in titles
    assert "Inventory traffic distribution" in titles
    assert "Canary rollbacks" in titles
    assert "Canary rollout state" in titles

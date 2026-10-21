"""Opt-in checks against the existing GKE cluster.

These tests do not import the kind integration suite. They run only when
PLATFORMFORGE_GKE_CONTEXT is the pinned context and that context is current.
They refuse every kind context. The canary test restores stable weights.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from platformforge.gke import GKE_CONTEXT

os.environ["PATH"] = "/opt/homebrew/share/google-cloud-sdk/bin:" + os.environ.get("PATH", "")

NS = "platformforge-dev"
PY = "/usr/bin/python3.13"
pytestmark = pytest.mark.gke


def _required_context() -> str:
    wanted = os.environ.get("PLATFORMFORGE_GKE_CONTEXT")
    if not wanted:
        pytest.skip("set PLATFORMFORGE_GKE_CONTEXT to the pinned GKE context")
    if wanted != GKE_CONTEXT or wanted.startswith("kind-"):
        pytest.fail(f"refusing GKE tests for context {wanted!r}")
    current = subprocess.run(  # noqa: S603
        ["kubectl", "config", "current-context"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current != wanted:
        pytest.fail(f"current context is {current!r}, expected {wanted!r}")
    return wanted


@pytest.fixture(scope="module")
def ctx() -> str:
    return _required_context()


def kubectl(ctx: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["kubectl", "--context", ctx, *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_workloads_are_ready_and_private(ctx: str) -> None:
    pods = json.loads(kubectl(ctx, "-n", NS, "get", "pods", "-o", "json"))
    names = {item["metadata"]["name"] for item in pods["items"]}
    assert any(name.startswith("orders-service-") for name in names)
    assert any(name.startswith("inventory-service-v0-1-0-") for name in names)
    assert any(name.startswith("inventory-service-v0-2-0-") for name in names)
    for item in pods["items"]:
        if item["metadata"]["name"].startswith("pf-test-client"):
            continue
        assert item["status"]["containerStatuses"]
        assert all(status["ready"] for status in item["status"]["containerStatuses"])
        assert sum(status["restartCount"] for status in item["status"]["containerStatuses"]) == 0
    services = kubectl(ctx, "get", "svc", "-A", "--no-headers")
    assert "LoadBalancer" not in services
    assert "NodePort" not in services
    assert kubectl(ctx, "get", "pvc", "-A", "--no-headers").strip() == ""


def test_stable_route_and_healthy_canary(ctx: str) -> None:
    weights = kubectl(
        ctx,
        "-n",
        NS,
        "get",
        "virtualservice",
        "inventory-service",
        "-o",
        "jsonpath={range .spec.http[0].route[*]}{.destination.subset}={.weight} {end}",
    )
    assert weights.strip() == "v0-1-0=100 v0-2-0=0"
    failure = kubectl(
        ctx,
        "-n",
        NS,
        "get",
        "deploy",
        "inventory-service-v0-2-0",
        "-o",
        'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="INVENTORY_FAILURE_PERCENT")].value}',
    )
    assert failure == "0"
    mode = kubectl(
        ctx,
        "-n",
        NS,
        "get",
        "peerauthentication",
        "default",
        "-o",
        "jsonpath={.spec.mtls.mode}",
    )
    assert mode == "STRICT"


def test_authorization_and_plaintext(ctx: str) -> None:
    pod = json.loads(
        kubectl(
            ctx,
            "-n",
            NS,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=orders-service",
            "-o",
            "json",
        )
    )["items"][0]["metadata"]["name"]
    allowed = kubectl(
        ctx,
        "-n",
        NS,
        "exec",
        pod,
        "-c",
        "orders-service",
        "--",
        PY,
        "-c",
        "import urllib.request; "
        "r=urllib.request.urlopen('http://inventory-service."
        "platformforge-dev.svc.cluster.local:8080/inventory/widget', timeout=5); "
        "print(r.status)",
    ).strip()
    assert allowed == "200"
    denied = kubectl(
        ctx,
        "-n",
        NS,
        "exec",
        "pf-test-client-mesh",
        "-c",
        "client",
        "--",
        PY,
        "-c",
        "import urllib.request, urllib.error\n"
        "try:\n"
        " urllib.request.urlopen('http://inventory-service."
        "platformforge-dev.svc.cluster.local:8080/inventory/widget', timeout=5)\n"
        "except urllib.error.HTTPError as exc:\n"
        " print(exc.code)\n",
    ).strip()
    assert denied == "403"
    plain = kubectl(
        ctx,
        "-n",
        NS,
        "exec",
        "pf-test-client-plain",
        "-c",
        "client",
        "--",
        PY,
        "-c",
        "import socket\n"
        "s=socket.create_connection(('inventory-service.platformforge-dev.svc.cluster.local',8080),5)\n"
        "s.sendall(b'GET /inventory/widget HTTP/1.1\\r\\n"
        "Host: inventory-service\\r\\nConnection: close\\r\\n\\r\\n')\n"
        "s.settimeout(5)\n"
        "try:\n"
        " print(s.recv(100))\n"
        "except ConnectionResetError:\n"
        " print('reset')\n",
    ).strip()
    assert plain == "reset"


def test_observability_still_has_the_drill(ctx: str) -> None:
    pod = json.loads(
        kubectl(
            ctx,
            "-n",
            NS,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=orders-service",
            "-o",
            "json",
        )
    )["items"][0]["metadata"]["name"]
    query = (
        "import json,urllib.parse,urllib.request\n"
        'q=\'sum(istio_requests_total{reporter="source",response_code="500",'
        'destination_canonical_service="inventory-service"})\'\n'
        "url='http://prometheus-server.platformforge-observability.svc.cluster.local"
        "/api/v1/query?query='+urllib.parse.quote(q)\n"
        "body=json.loads(urllib.request.urlopen(url, timeout=10).read().decode())\n"
        "total=sum(int(float(row['value'][1])) for row in body['data']['result'])\n"
        "print(total)\n"
    )
    total = int(
        kubectl(ctx, "-n", NS, "exec", pod, "-c", "orders-service", "--", PY, "-c", query).strip()
    )
    assert total >= 11
    loki_query = (
        "import json,urllib.parse,urllib.request\n"
        'q=\'{namespace="platformforge-dev",app="inventory-service"} |= "injected failure"\'\n'
        "url='http://loki.platformforge-observability.svc.cluster.local:3100"
        "/loki/api/v1/query_range?limit=1&query='+urllib.parse.quote(q)\n"
        "body=json.loads(urllib.request.urlopen(url, timeout=10).read().decode())\n"
        "line=json.loads(body['data']['result'][0]['values'][0][1])\n"
        "print(line['trace_id'])\n"
    )
    trace_id = kubectl(
        ctx, "-n", NS, "exec", pod, "-c", "orders-service", "--", PY, "-c", loki_query
    ).strip()
    assert len(trace_id) == 32
    tempo = (
        "import json,urllib.request\n"
        f"url='http://tempo.platformforge-observability.svc.cluster.local:3200"
        f"/api/traces/{trace_id}'\n"
        "req=urllib.request.Request(url, headers={'Accept':'application/json'})\n"
        "trace=json.loads(urllib.request.urlopen(req, timeout=10).read().decode())\n"
        "names=set()\n"
        "for batch in trace.get('batches') or []:\n"
        "  for attr in batch.get('resource',{}).get('attributes',[]):\n"
        "    if attr.get('key')=='service.name':\n"
        "      names.add(attr['value'].get('stringValue'))\n"
        "print(','.join(sorted(names)))\n"
    )
    names = kubectl(ctx, "-n", NS, "exec", pod, "-c", "orders-service", "--", PY, "-c", tempo)
    assert "orders-service" in names
    assert "inventory-service" in names

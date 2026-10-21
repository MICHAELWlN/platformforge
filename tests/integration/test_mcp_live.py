"""Opt-in read-only MCP checks against kind-platformforge.

Set PLATFORMFORGE_MCP_LIVE=1. The MCP client uses a one-hour token for
platformforge-mcp-reader. Token issuance uses the kind admin client and does
not change the current kubeconfig context. Telemetry URLs must already be
loopback port-forwards. This module is skipped by make test.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from kubernetes import client, config
from kubernetes.client import ApiException
from mcp import Client

from platformforge.mcp_server.server import build_server

pytestmark = pytest.mark.skipif(
    os.environ.get("PLATFORMFORGE_MCP_LIVE") != "1",
    reason="set PLATFORMFORGE_MCP_LIVE=1 to run read-only kind MCP checks",
)

CONTEXT = "kind-platformforge"
NAMESPACE = "platformforge-dev"
READER = "platformforge-mcp-reader"
PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
TEMPO = "http://127.0.0.1:3200"
_TRACE = re.compile(r"\b[0-9a-f]{32}\b")
_REQUEST = re.compile(r"request_id[\"=: ]+([A-Za-z0-9-]{8,})")


@pytest.fixture(scope="module")
def reader_kubeconfig(tmp_path_factory: pytest.TempPathFactory) -> Path:
    server, ca = _kind_cluster()
    token = _mint_token()
    path = tmp_path_factory.mktemp("mcp-reader") / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": CONTEXT,
                "clusters": [
                    {
                        "name": CONTEXT,
                        "cluster": {
                            "server": server,
                            "certificate-authority-data": ca,
                        },
                    }
                ],
                "contexts": [
                    {
                        "name": CONTEXT,
                        "context": {"cluster": CONTEXT, "user": READER},
                    }
                ],
                "users": [{"name": READER, "user": {"token": token}}],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def reader(reader_kubeconfig: Path) -> client.CoreV1Api:
    api = config.new_client_from_config(
        config_file=str(reader_kubeconfig),
        context=CONTEXT,
        persist_config=False,
    )
    return client.CoreV1Api(api)


def test_reader_rbac(reader: client.CoreV1Api, reader_kubeconfig: Path) -> None:
    api = config.new_client_from_config(
        config_file=str(reader_kubeconfig),
        context=CONTEXT,
        persist_config=False,
    )
    auth = client.AuthorizationV1Api(api)
    apps = client.AppsV1Api(api)
    expects = {
        ("get", "pods", "", "platformforge-dev", ""): True,
        ("list", "pods", "", "platformforge-dev", ""): True,
        ("get", "pods", "log", "platformforge-dev", ""): True,
        ("get", "services", "", "platformforge-dev", ""): True,
        ("get", "endpoints", "", "platformforge-dev", ""): True,
        ("list", "events", "", "platformforge-dev", ""): True,
        ("get", "deployments", "", "platformforge-dev", "apps"): True,
        ("list", "deployments", "", "platformforge-dev", "apps"): True,
        ("get", "secrets", "", "platformforge-dev", ""): False,
        ("create", "pods", "exec", "platformforge-dev", ""): False,
        ("patch", "deployments", "", "platformforge-dev", "apps"): False,
        ("update", "deployments", "scale", "platformforge-dev", "apps"): False,
        ("list", "pods", "", "platformforge-observability", ""): False,
        ("list", "secrets", "", "platformforge-dev", ""): False,
    }
    for (verb, resource, subresource, namespace, group), allowed in expects.items():
        review = auth.create_self_subject_access_review(
            client.V1SelfSubjectAccessReview(
                spec=client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=client.V1ResourceAttributes(
                        namespace=namespace,
                        verb=verb,
                        group=group,
                        resource=resource,
                        subresource=subresource or None,
                    )
                )
            )
        )
        assert review.status is not None
        assert review.status.allowed is allowed, (verb, resource, subresource, namespace)

    pods = reader.list_namespaced_pod(NAMESPACE, _request_timeout=5)
    assert pods.items
    deployments = apps.list_namespaced_deployment(NAMESPACE, _request_timeout=5)
    assert deployments.items
    service = reader.read_namespaced_service("inventory-service", NAMESPACE, _request_timeout=5)
    assert service.spec is not None
    assert service.spec.type == "ClusterIP"
    events = reader.list_namespaced_event(NAMESPACE, _request_timeout=5)
    assert events.items is not None
    _deny(
        lambda: reader.read_namespaced_secret("platformforge-mcp-denied", NAMESPACE),
    )
    _deny(
        lambda: apps.patch_namespaced_deployment(
            "orders-service",
            NAMESPACE,
            {"metadata": {"annotations": {"platformforge.dev/mcp-probe": "no"}}},
        )
    )
    pod_name = pods.items[0].metadata.name
    _deny(
        lambda: reader.connect_get_namespaced_pod_exec(
            pod_name,
            NAMESPACE,
            command=["true"],
            stderr=False,
            stdin=False,
            stdout=False,
            tty=False,
        )
    )


def test_mcp_tools_and_incident(reader_kubeconfig: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORMFORGE_MCP_KUBECONFIG", str(reader_kubeconfig))
    monkeypatch.setenv("PLATFORMFORGE_PROMETHEUS_URL", PROM)
    monkeypatch.setenv("PLATFORMFORGE_LOKI_URL", LOKI)
    monkeypatch.setenv("PLATFORMFORGE_TEMPO_URL", TEMPO)
    _require_http(f"{PROM}/-/ready")
    _require_http(f"{LOKI}/ready")
    _require_http(f"{TEMPO}/ready")

    async def scenario() -> dict[str, Any]:
        async with Client(build_server()) as mcp:
            listed = await mcp.list_tools()
            names = {tool.name for tool in listed.tools}
            assert len(names) == 12
            pods = await _ok(mcp, "list_pods", {"context": CONTEXT, "namespace": NAMESPACE})
            pod_name = next(
                row["name"]
                for row in pods["pods"]
                if str(row["name"]).startswith("inventory-service-v0-2-0")
            )
            described = await _ok(
                mcp,
                "describe_pod",
                {"context": CONTEXT, "namespace": NAMESPACE, "pod": pod_name},
            )
            container = _app_container(described)
            logs = await _ok(
                mcp,
                "get_pod_logs",
                {
                    "context": CONTEXT,
                    "namespace": NAMESPACE,
                    "pod": pod_name,
                    "container": container,
                    "tail_lines": 20,
                    "since_seconds": 900,
                },
            )
            deployments = await _ok(
                mcp, "list_deployments", {"context": CONTEXT, "namespace": NAMESPACE}
            )
            status = await _ok(
                mcp,
                "get_deployment_status",
                {
                    "context": CONTEXT,
                    "namespace": NAMESPACE,
                    "name": "inventory-service-v0-2-0",
                },
            )
            events = await _ok(
                mcp, "list_kubernetes_events", {"context": CONTEXT, "namespace": NAMESPACE}
            )
            service = await _ok(
                mcp,
                "get_service_status",
                {"context": CONTEXT, "namespace": NAMESPACE, "name": "inventory-service"},
            )
            prom = await _ok(
                mcp,
                "query_prometheus",
                {
                    "context": CONTEXT,
                    "query": (
                        "sum by (response_code) (istio_requests_total"
                        '{reporter="source",destination_service_name="inventory-service"})'
                    ),
                },
            )
            loki = await _ok(
                mcp,
                "query_loki",
                {
                    "context": CONTEXT,
                    "query": '{namespace="platformforge-dev",app="inventory-service"}',
                },
            )
            trace_id = _trace_id(logs) or _tempo_search()
            tempo = await _ok(mcp, "query_tempo", {"context": CONTEXT, "trace_id": trace_id})
            health = await _ok(
                mcp,
                "get_service_health",
                {"context": CONTEXT, "namespace": NAMESPACE, "service": "inventory-service"},
            )
            incident = await _ok(
                mcp,
                "investigate_incident",
                {
                    "context": CONTEXT,
                    "namespace": NAMESPACE,
                    "service": "inventory-service",
                    "symptom": "read-only check of retained telemetry",
                    "trace_id": trace_id,
                },
            )
            denied = await mcp.call_tool(
                "list_pods",
                {"context": CONTEXT, "namespace": "kube-system"},
            )
            assert denied.is_error is True
        return {
            "pods": pods,
            "described_containers": [row["name"] for row in described["pod"]["containers"]],
            "logs_untrusted": logs["logs"]["untrusted"],
            "deployments": deployments["deployments"],
            "deployment_status": status,
            "event_count": len(events["events"]),
            "service": service,
            "prometheus": prom,
            "loki_lines": len(loki.get("lines") or []),
            "loki_telemetry": loki.get("telemetry"),
            "tempo": tempo,
            "trace_found": bool(_trace_id(logs)) or _tempo_search() != "0" * 32,
            "request_id_present": _request_id(logs) is not None,
            "health_ready": health["kubernetes_ready"],
            "health_metrics": health["metrics"].get("telemetry"),
            "incident": {
                "verified_root_cause": incident["verified_root_cause"],
                "verified": incident["verified"],
                "possible_explanations": incident["possible_explanations"],
                "missing": incident["missing"],
            },
        }

    import asyncio

    summary = asyncio.run(scenario())
    canary = next(
        row for row in summary["pods"]["pods"] if str(row["name"]).startswith("inventory-service")
    )
    assert canary["containers"] == 2
    assert canary["ready_containers"] == 2
    assert "istio-proxy" in summary["described_containers"]
    assert summary["logs_untrusted"] is True
    assert summary["service"]["type"] == "ClusterIP"
    assert summary["service"]["public"] is False
    assert summary["service"]["ready_addresses"] > 0
    assert summary["incident"]["verified_root_cause"] is None
    assert summary["deployment_status"]["ready"] is True
    evidence = os.environ.get("PLATFORMFORGE_MCP_EVIDENCE", "")
    if evidence:
        Path(evidence).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    assert "token" not in json.dumps(summary)


def _ok(mcp: Client, name: str, arguments: dict[str, Any]) -> Any:
    async def call() -> dict[str, Any]:
        result = await mcp.call_tool(name, arguments)
        assert result.is_error is False, name
        body = result.structured_content
        assert isinstance(body, dict)
        return body

    return call()


def _app_container(described: dict[str, Any]) -> str:
    names = [str(row["name"]) for row in described["pod"]["containers"]]
    for name in names:
        if name != "istio-proxy":
            return name
    raise AssertionError("pod has no application container")


def _trace_id(logs: dict[str, Any]) -> str:
    text = json.dumps(logs)
    found = _TRACE.search(text)
    return found.group(0) if found else ""


def _request_id(logs: dict[str, Any]) -> str | None:
    found = _REQUEST.search(json.dumps(logs))
    return found.group(1) if found else None


def _tempo_search() -> str:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{TEMPO}/api/search?limit=5", timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read(65536).decode("utf-8"))
    except Exception:
        return "0" * 32
    traces = payload.get("traces") if isinstance(payload, dict) else None
    if not isinstance(traces, list):
        return "0" * 32
    for trace in traces:
        if isinstance(trace, dict) and _TRACE.fullmatch(str(trace.get("traceID", ""))):
            return str(trace["traceID"])
    return "0" * 32


def _require_http(url: str) -> None:
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        if response.status != 200:
            raise AssertionError(f"{url} returned {response.status}")


def _deny(action: Any) -> None:
    try:
        action()
    except ApiException as exc:
        assert exc.status == 403
        return
    raise AssertionError("call was allowed")


def _mint_token() -> str:
    admin = config.new_client_from_config(context=CONTEXT, persist_config=False)
    body = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(expiration_seconds=3600)
    )
    response = client.CoreV1Api(admin).create_namespaced_service_account_token(
        READER,
        NAMESPACE,
        body,
        _request_timeout=5,
    )
    token = "" if response.status is None else str(response.status.token or "")
    if not token:
        raise AssertionError("token request returned an empty token")
    return token


def _kind_cluster() -> tuple[str, str]:
    raw = Path("~/.kube/config").expanduser().read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    cluster_name = ""
    for entry in data.get("contexts") or []:
        if entry.get("name") == CONTEXT:
            cluster_name = str((entry.get("context") or {}).get("cluster", ""))
    for entry in data.get("clusters") or []:
        if entry.get("name") != cluster_name:
            continue
        cluster = entry.get("cluster") or {}
        server = str(cluster.get("server", ""))
        encoded = cluster.get("certificate-authority-data")
        if not encoded and cluster.get("certificate-authority"):
            encoded = base64.b64encode(
                Path(str(cluster["certificate-authority"])).read_bytes()
            ).decode("ascii")
        if not server or not encoded:
            break
        return server, str(encoded)
    raise AssertionError("kind-platformforge cluster CA was not found")

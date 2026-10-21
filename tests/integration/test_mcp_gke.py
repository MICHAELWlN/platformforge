"""Opt-in read-only MCP checks against the pinned GKE cluster.

Set PLATFORMFORGE_MCP_GKE=1. Kubernetes calls use a one-hour token for
platformforge-mcp-reader. Token issuance uses the operator GKE client and
does not change the current kubeconfig context. This module does not contact
kind. make test skips it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from kubernetes import client, config
from mcp import Client

from platformforge.gke import GKE_CONTEXT
from platformforge.mcp_server.server import build_server
from tests.integration.test_mcp_live import (
    LOKI,
    PROM,
    TEMPO,
    _app_container,
    _deny,
    _ok,
    _request_id,
    _require_http,
    _tempo_search,
    _trace_id,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PLATFORMFORGE_MCP_GKE") != "1",
    reason="set PLATFORMFORGE_MCP_GKE=1 to run read-only GKE MCP checks",
)

NAMESPACE = "platformforge-dev"
READER = "platformforge-mcp-reader"


@pytest.fixture(scope="module")
def reader_kubeconfig(tmp_path_factory: pytest.TempPathFactory) -> Path:
    server, ca = _gke_cluster()
    token = _mint_token()
    path = tmp_path_factory.mktemp("mcp-gke-reader") / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": GKE_CONTEXT,
                "clusters": [
                    {
                        "name": GKE_CONTEXT,
                        "cluster": {
                            "server": server,
                            "certificate-authority-data": ca,
                        },
                    }
                ],
                "contexts": [
                    {
                        "name": GKE_CONTEXT,
                        "context": {"cluster": GKE_CONTEXT, "user": READER},
                    }
                ],
                "users": [{"name": READER, "user": {"token": token}}],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    text = path.read_text(encoding="utf-8")
    assert "gke-gcloud-auth-plugin" not in text
    assert "client-certificate-data" not in text
    return path


def test_reader_rbac(reader_kubeconfig: Path) -> None:
    api = config.new_client_from_config(
        config_file=str(reader_kubeconfig),
        context=GKE_CONTEXT,
        persist_config=False,
    )
    reader = client.CoreV1Api(api)
    auth = client.AuthorizationV1Api(api)
    apps = client.AppsV1Api(api)
    expects = {
        ("get", "pods", "", NAMESPACE, ""): True,
        ("list", "pods", "", NAMESPACE, ""): True,
        ("get", "pods", "log", NAMESPACE, ""): True,
        ("get", "services", "", NAMESPACE, ""): True,
        ("get", "endpoints", "", NAMESPACE, ""): True,
        ("list", "events", "", NAMESPACE, ""): True,
        ("get", "deployments", "", NAMESPACE, "apps"): True,
        ("list", "deployments", "", NAMESPACE, "apps"): True,
        ("get", "secrets", "", NAMESPACE, ""): False,
        ("create", "pods", "exec", NAMESPACE, ""): False,
        ("patch", "deployments", "", NAMESPACE, "apps"): False,
        ("update", "deployments", "scale", NAMESPACE, "apps"): False,
        ("delete", "pods", "", NAMESPACE, ""): False,
        ("delete", "deployments", "", NAMESPACE, "apps"): False,
        ("list", "pods", "", "platformforge-observability", ""): False,
        ("list", "pods", "", "kube-system", ""): False,
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
    pods = reader.list_namespaced_pod(NAMESPACE, _request_timeout=15)
    assert pods.items
    _deny(lambda: reader.read_namespaced_secret("platformforge-mcp-denied", NAMESPACE))
    _deny(
        lambda: apps.patch_namespaced_deployment(
            "orders-service",
            NAMESPACE,
            {"metadata": {"annotations": {"platformforge.dev/mcp-probe": "no"}}},
        )
    )
    _deny(lambda: reader.delete_namespaced_pod("platformforge-mcp-denied", NAMESPACE))
    _deny(
        lambda: reader.connect_get_namespaced_pod_exec(
            pods.items[0].metadata.name,
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
            assert len({tool.name for tool in listed.tools}) == 12
            pods = await _ok(mcp, "list_pods", {"context": GKE_CONTEXT, "namespace": NAMESPACE})
            pod_name = next(
                row["name"]
                for row in pods["pods"]
                if str(row["name"]).startswith("inventory-service-v0-2-0")
            )
            described = await _ok(
                mcp,
                "describe_pod",
                {"context": GKE_CONTEXT, "namespace": NAMESPACE, "pod": pod_name},
            )
            logs = await _ok(
                mcp,
                "get_pod_logs",
                {
                    "context": GKE_CONTEXT,
                    "namespace": NAMESPACE,
                    "pod": pod_name,
                    "container": _app_container(described),
                    "tail_lines": 20,
                    "since_seconds": 900,
                },
            )
            deployments = await _ok(
                mcp, "list_deployments", {"context": GKE_CONTEXT, "namespace": NAMESPACE}
            )
            status = await _ok(
                mcp,
                "get_deployment_status",
                {
                    "context": GKE_CONTEXT,
                    "namespace": NAMESPACE,
                    "name": "inventory-service-v0-2-0",
                },
            )
            events = await _ok(
                mcp,
                "list_kubernetes_events",
                {"context": GKE_CONTEXT, "namespace": NAMESPACE},
            )
            service = await _ok(
                mcp,
                "get_service_status",
                {"context": GKE_CONTEXT, "namespace": NAMESPACE, "name": "inventory-service"},
            )
            prom = await _ok(
                mcp,
                "query_prometheus",
                {
                    "context": GKE_CONTEXT,
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
                    "context": GKE_CONTEXT,
                    "query": '{namespace="platformforge-dev",app="inventory-service"}',
                },
            )
            trace_id = _trace_id(logs) or _tempo_search()
            tempo = await _ok(mcp, "query_tempo", {"context": GKE_CONTEXT, "trace_id": trace_id})
            health = await _ok(
                mcp,
                "get_service_health",
                {
                    "context": GKE_CONTEXT,
                    "namespace": NAMESPACE,
                    "service": "inventory-service",
                },
            )
            incident = await _ok(
                mcp,
                "investigate_incident",
                {
                    "context": GKE_CONTEXT,
                    "namespace": NAMESPACE,
                    "service": "inventory-service",
                    "symptom": "read-only check of retained telemetry",
                    "trace_id": trace_id,
                },
            )
            denied = await mcp.call_tool(
                "list_pods",
                {"context": GKE_CONTEXT, "namespace": "kube-system"},
            )
            assert denied.is_error is True
        return {
            "described_containers": [row["name"] for row in described["pod"]["containers"]],
            "logs_untrusted": logs["logs"]["untrusted"],
            "deployments": deployments["deployments"],
            "deployment_status": status,
            "event_count": len(events["events"]),
            "service": {
                "type": service["type"],
                "public": service["public"],
                "ready_addresses": service["ready_addresses"],
                "ports": service["ports"],
            },
            "prometheus": prom,
            "loki_lines": len(loki.get("lines") or []),
            "loki_telemetry": loki.get("telemetry"),
            "tempo": tempo,
            "trace_found": bool(_trace_id(logs)) or _tempo_search() != "0" * 32,
            "request_id_present": _request_id(logs) is not None,
            "health_ready": health["kubernetes_ready"],
            "health_metrics": health["metrics"].get("telemetry"),
            "pod_ready": next(
                row
                for row in pods["pods"]
                if str(row["name"]).startswith("inventory-service-v0-2-0")
            ),
            "incident": {
                "verified_root_cause": incident["verified_root_cause"],
                "verified": incident["verified"],
                "possible_explanations": incident["possible_explanations"],
                "missing": incident["missing"],
            },
        }

    import asyncio

    summary = asyncio.run(scenario())
    assert summary["pod_ready"]["ready_containers"] == 2
    assert "istio-proxy" in summary["described_containers"]
    assert summary["logs_untrusted"] is True
    image = summary["deployment_status"]["images"][0]
    assert image["immutable"] is True
    assert "@sha256:" in str(image["image"])
    assert summary["service"]["type"] == "ClusterIP"
    assert summary["service"]["public"] is False
    assert summary["service"]["ready_addresses"] > 0
    assert summary["incident"]["verified_root_cause"] is None
    evidence = os.environ.get("PLATFORMFORGE_MCP_EVIDENCE", "")
    if evidence:
        Path(evidence).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    assert "token" not in json.dumps(summary).lower()


def _mint_token() -> str:
    admin = config.new_client_from_config(context=GKE_CONTEXT, persist_config=False)
    body = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(expiration_seconds=3600)
    )
    response = client.CoreV1Api(admin).create_namespaced_service_account_token(
        READER,
        NAMESPACE,
        body,
        _request_timeout=30,
    )
    token = "" if response.status is None else str(response.status.token or "")
    if not token:
        raise AssertionError("token request returned an empty token")
    return token


def _gke_cluster() -> tuple[str, str]:
    raw = Path("~/.kube/config").expanduser().read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    cluster_name = ""
    for entry in data.get("contexts") or []:
        if entry.get("name") == GKE_CONTEXT:
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
    raise AssertionError("pinned GKE cluster CA was not found")

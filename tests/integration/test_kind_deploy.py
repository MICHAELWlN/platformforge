"""Integration tests for the PlatformForge CLI against a local kind cluster.

See tests/integration/conftest.py for the opt-in and safety rules.
"""

from __future__ import annotations

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from platformforge import cli
from tests.conftest import EXAMPLE_MANIFEST
from tests.integration.conftest import NAMESPACE, runner
from tests.integration.conftest import run_cli as _run

pytestmark = pytest.mark.integration


def test_01_wrong_context_approval_is_refused(kube):
    result = runner.invoke(
        cli.app, ["status", str(EXAMPLE_MANIFEST), "--approve-context", "kind-not-this-one"]
    )
    assert result.exit_code == cli.EXIT_CONTEXT_NOT_APPROVED, result.output


def test_02_deploy_creates_and_rolls_out(kube):
    result = _run("deploy", str(EXAMPLE_MANIFEST), "--create-namespace", "--timeout", "240")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Deployment/demo-web:" in result.output
    assert "Service/demo-web:" in result.output
    assert "Rollout complete: 2/2 available" in result.output


def test_03_namespace_has_restricted_pod_security(kube):
    ns = kube.core.read_namespace(NAMESPACE)
    assert ns.metadata.labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_04_live_deployment_matches_manifest(kube):
    dep = kube.apps.read_namespaced_deployment("demo-web", NAMESPACE)
    assert dep.metadata.labels["app.kubernetes.io/managed-by"] == "platformforge"
    pod = dep.spec.template.spec
    container = pod.containers[0]
    assert container.image == "nginxinc/nginx-unprivileged:1.29-alpine"
    assert container.readiness_probe.http_get.path == "/"
    assert container.readiness_probe.period_seconds == 5
    assert container.liveness_probe.initial_delay_seconds == 10
    assert container.resources.requests == {"cpu": "50m", "memory": "32Mi"}
    assert container.resources.limits == {"cpu": "200m", "memory": "64Mi"}
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.allow_privilege_escalation is False
    assert pod.automount_service_account_token is False


def test_05_service_is_cluster_ip(kube):
    svc = kube.core.read_namespaced_service("demo-web", NAMESPACE)
    assert svc.spec.type == "ClusterIP"
    assert not svc.spec.external_ips
    assert not svc.spec.load_balancer_ip
    slices = client.DiscoveryV1Api(kube.core.api_client).list_namespaced_endpoint_slice(
        NAMESPACE, label_selector="kubernetes.io/service-name=demo-web"
    )
    ready = [
        e for s in slices.items for e in (s.endpoints or []) if e.conditions and e.conditions.ready
    ]
    assert len(ready) == 2


def test_06_pods_ready_and_running(kube):
    pods = kube.core.list_namespaced_pod(
        NAMESPACE,
        label_selector="app.kubernetes.io/name=demo-web,app.kubernetes.io/managed-by=platformforge",
    )
    running = [
        p for p in pods.items if p.status.phase == "Running" and not p.metadata.deletion_timestamp
    ]
    assert len(running) == 2
    for pod in running:
        assert all(cs.ready for cs in pod.status.container_statuses)


def test_07_redeploy_is_idempotent_update(kube):
    result = _run("deploy", str(EXAMPLE_MANIFEST), "--timeout", "240")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "Namespace/platformforge-dev: exists" in result.output
    assert "Deployment/demo-web: updated" in result.output
    assert "Service/demo-web: updated" in result.output


def test_08_status_reports_healthy(kube):
    result = _run("status", str(EXAMPLE_MANIFEST))
    print(result.output)
    assert result.exit_code == 0, result.output
    assert "desired=2 updated=2 ready=2 available=2" in result.output
    assert "Endpoint:   ClusterIP" in result.output
    assert "Health:     HEALTHY" in result.output


def test_09_logs_returns_pod_output(kube):
    result = _run("logs", str(EXAMPLE_MANIFEST), "--tail", "20")
    print(result.output)
    assert result.exit_code == 0, result.output
    assert result.output.count("==> demo-web-") == 2
    # Readiness/liveness probes hit "/" via kube-probe; nginx logs each request.
    assert "kube-probe" in result.output


def test_10_no_platformforge_objects_outside_allowlist(kube):
    selector = "app.kubernetes.io/managed-by=platformforge"
    deployments = kube.apps.list_deployment_for_all_namespaces(label_selector=selector)
    services = kube.core.list_service_for_all_namespaces(label_selector=selector)
    namespaces = {o.metadata.namespace for o in deployments.items + services.items}
    assert namespaces == {NAMESPACE}
    with pytest.raises(ApiException) as exc:
        kube.apps.read_namespaced_deployment("demo-web", "default")
    assert exc.value.status == 404

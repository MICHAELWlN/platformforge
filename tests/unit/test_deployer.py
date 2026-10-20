from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from platformforge import deployer
from platformforge.kubecontext import KubeClients
from platformforge.manifest import ServiceManifest

MANAGED = {"app.kubernetes.io/managed-by": "platformforge"}


def _obj(labels=None, resource_version="7"):
    obj = MagicMock()
    obj.metadata = client.V1ObjectMeta(labels=labels, resource_version=resource_version)
    obj.spec.cluster_ip = "10.96.0.10"
    obj.spec.cluster_ips = ["10.96.0.10"]
    return obj


@pytest.fixture
def clients():
    core, apps = MagicMock(spec=client.CoreV1Api), MagicMock(spec=client.AppsV1Api)
    # Default: namespace exists, no existing Deployment/Service.
    core.read_namespaced_service.side_effect = ApiException(status=404)
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    return KubeClients(core=core, apps=apps)


def test_deploy_creates_resources(clients, manifest):
    applied = deployer.deploy(clients, manifest)
    assert [(a.kind, a.action) for a in applied] == [
        ("Namespace", "exists"),
        ("Deployment", "created"),
        ("Service", "created"),
    ]
    ns, body = clients.apps.create_namespaced_deployment.call_args.args
    assert ns == "platformforge-dev"
    assert body.metadata.name == "demo-web"
    clients.core.create_namespace.assert_not_called()


def test_deploy_requires_existing_namespace_by_default(clients, manifest):
    clients.core.read_namespace.side_effect = ApiException(status=404)
    with pytest.raises(deployer.DeployError, match="--create-namespace"):
        deployer.deploy(clients, manifest)
    clients.core.create_namespace.assert_not_called()
    clients.apps.create_namespaced_deployment.assert_not_called()


def test_deploy_creates_namespace_when_requested(clients, manifest):
    clients.core.read_namespace.side_effect = ApiException(status=404)
    applied = deployer.deploy(clients, manifest, create_namespace=True)
    assert applied[0].action == "created"
    body = clients.core.create_namespace.call_args.args[0]
    assert body.metadata.name == "platformforge-dev"
    assert body.metadata.labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_deploy_updates_managed_resources(clients, manifest):
    clients.apps.create_namespaced_deployment.side_effect = ApiException(status=409)
    clients.core.create_namespaced_service.side_effect = ApiException(status=409)
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = _obj(MANAGED, "11")
    clients.core.read_namespaced_service.side_effect = None
    clients.core.read_namespaced_service.return_value = _obj(MANAGED, "12")

    applied = deployer.deploy(clients, manifest)

    assert [a.action for a in applied[1:]] == ["updated", "updated"]
    dep_body = clients.apps.replace_namespaced_deployment.call_args.args[2]
    assert dep_body.metadata.resource_version == "11"
    svc_body = clients.core.replace_namespaced_service.call_args.args[2]
    assert svc_body.metadata.resource_version == "12"
    assert svc_body.spec.cluster_ip == "10.96.0.10"
    assert svc_body.spec.type == "ClusterIP"


@pytest.mark.parametrize("labels", [None, {}, {"app.kubernetes.io/managed-by": "helm"}])
def test_deploy_refuses_unmanaged_deployment(clients, manifest, labels):
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = _obj(labels)
    with pytest.raises(deployer.OwnershipError, match="Deployment 'demo-web'"):
        deployer.deploy(clients, manifest)
    clients.apps.create_namespaced_deployment.assert_not_called()
    clients.apps.replace_namespaced_deployment.assert_not_called()
    clients.core.create_namespaced_service.assert_not_called()


def test_deploy_refuses_unmanaged_service_before_any_write(clients, manifest):
    clients.core.read_namespaced_service.side_effect = None
    clients.core.read_namespaced_service.return_value = _obj({"team": "other"})
    with pytest.raises(deployer.OwnershipError, match="Service 'demo-web'"):
        deployer.deploy(clients, manifest)
    clients.apps.create_namespaced_deployment.assert_not_called()
    clients.apps.replace_namespaced_deployment.assert_not_called()


def test_apply_deployment_conflict_rechecks_ownership(clients, manifest):
    # Object appears between preflight and create (race): still refuse if unmanaged.
    clients.apps.create_namespaced_deployment.side_effect = ApiException(status=409)
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = _obj({})
    with pytest.raises(deployer.OwnershipError):
        deployer.apply_deployment(clients.apps, manifest)
    clients.apps.replace_namespaced_deployment.assert_not_called()


def test_non_conflict_api_errors_propagate(clients, manifest):
    clients.apps.create_namespaced_deployment.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        deployer.deploy(clients, manifest)


def test_entry_points_recheck_allowlist(clients, manifest):
    rogue = ServiceManifest.model_construct(
        api_version=manifest.api_version,
        kind=manifest.kind,
        metadata=manifest.metadata.model_construct(name="x", namespace="kube-system"),
        spec=manifest.spec,
    )
    for call in (
        lambda: deployer.deploy(clients, rogue, create_namespace=True),
        lambda: deployer.apply_deployment(clients.apps, rogue),
        lambda: deployer.apply_service(clients.core, rogue),
        lambda: deployer.get_status(clients, rogue),
        lambda: deployer.get_logs(clients.core, rogue, 10),
        lambda: deployer.wait_for_rollout(clients.apps, rogue, 1),
    ):
        with pytest.raises(deployer.DeployError, match="not allowlisted"):
            call()
    assert not clients.core.mock_calls
    assert not clients.apps.mock_calls


def _deployment(generation, observed, replicas, updated, available, total=None):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(generation=generation),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(),
            template=client.V1PodTemplateSpec(),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=observed,
            updated_replicas=updated,
            available_replicas=available,
            replicas=replicas if total is None else total,
        ),
    )


@pytest.mark.parametrize(
    ("dep", "expected"),
    [
        (_deployment(2, 2, 2, 2, 2), True),
        (_deployment(3, 2, 2, 2, 2), False),  # controller has not observed latest spec
        (_deployment(2, 2, 2, 1, 2), False),
        (_deployment(2, 2, 2, 2, 1), False),
        (_deployment(2, 2, 2, 2, 2, total=3), False),  # old replica still terminating
    ],
)
def test_rollout_complete(dep, expected):
    assert deployer.rollout_complete(dep) is expected


def test_wait_for_rollout_polls_until_complete(clients, manifest):
    clients.apps.read_namespaced_deployment_status.side_effect = [
        _deployment(1, 1, 2, 1, 0),
        _deployment(1, 1, 2, 2, 1),
        _deployment(1, 1, 2, 2, 2),
    ]
    sleeps = []
    result = deployer.wait_for_rollout(
        clients.apps, manifest, 60, poll_seconds=1, clock=lambda: 0.0, sleep=sleeps.append
    )
    assert result.status.available_replicas == 2
    assert sleeps == [1, 1]


def test_wait_for_rollout_times_out(clients, manifest):
    clients.apps.read_namespaced_deployment_status.return_value = _deployment(1, 1, 2, 2, 0)
    ticks = iter([0.0, 5.0, 11.0])
    with pytest.raises(deployer.RolloutTimeout, match="available=0"):
        deployer.wait_for_rollout(
            clients.apps, manifest, 10, clock=lambda: next(ticks), sleep=lambda _: None
        )


def test_get_status_not_deployed(clients, manifest):
    st = deployer.get_status(clients, manifest)
    assert st.deployed is False
    assert st.healthy is False


def test_get_status_refuses_unmanaged(clients, manifest):
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = _obj({})
    with pytest.raises(deployer.OwnershipError):
        deployer.get_status(clients, manifest)


def test_get_logs_bounds(clients, manifest):
    for tail in (0, deployer.MAX_LOG_LINES + 1):
        with pytest.raises(deployer.DeployError, match="tail"):
            deployer.get_logs(clients.core, manifest, tail)


def test_get_logs_uses_label_selector_and_container(clients, manifest):
    pod = client.V1Pod(metadata=client.V1ObjectMeta(name="demo-web-abc"))
    clients.core.list_namespaced_pod.return_value = client.V1PodList(items=[pod])
    clients.core.read_namespaced_pod_log.return_value = "hello\n"
    results = deployer.get_logs(clients.core, manifest, 50)
    assert results[0].text == "hello\n"
    kwargs = clients.core.list_namespaced_pod.call_args.kwargs
    assert kwargs["label_selector"] == (
        "app.kubernetes.io/name=demo-web,app.kubernetes.io/managed-by=platformforge"
    )
    clients.core.read_namespaced_pod_log.assert_called_once_with(
        "demo-web-abc", "platformforge-dev", container="demo-web", tail_lines=50
    )


def test_get_logs_reports_per_pod_errors(clients, manifest):
    pod = client.V1Pod(metadata=client.V1ObjectMeta(name="demo-web-abc"))
    clients.core.list_namespaced_pod.return_value = client.V1PodList(items=[pod])
    clients.core.read_namespaced_pod_log.side_effect = ApiException(status=400, reason="Bad")
    results = deployer.get_logs(clients.core, manifest, 50)
    assert results[0].error == "400 Bad"

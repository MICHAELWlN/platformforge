"""Milestone 3 manifest fields: version, serviceAccountName, termination."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from typer.testing import CliRunner

from platformforge import deployer
from platformforge.cli import app
from platformforge.kubecontext import KubeClients
from platformforge.manifest import ManifestError, ServiceManifest, load_manifest
from platformforge.resources import (
    build_deployment,
    build_namespace,
    build_service,
    build_service_account,
    label_selector,
    to_dict,
)
from tests.conftest import REPO_ROOT

MANIFESTS = REPO_ROOT / "services" / "manifests"
MANAGED = {"app.kubernetes.io/managed-by": "platformforge"}


@pytest.fixture
def mesh_dict(manifest_dict):
    manifest_dict["metadata"]["name"] = "inventory-service"
    manifest_dict["spec"]["version"] = "v0.2.0"
    manifest_dict["spec"]["serviceAccountName"] = "inventory-service"
    manifest_dict["spec"]["termination"] = {"gracePeriodSeconds": 30, "preStopSleepSeconds": 5}
    return manifest_dict


@pytest.fixture
def mesh(mesh_dict) -> ServiceManifest:
    return ServiceManifest.model_validate(mesh_dict)


def _load(write_manifest, data):
    return load_manifest(write_manifest(data))


# --- validation -----------------------------------------------------------------------


def test_mesh_fields_accepted(mesh):
    assert mesh.spec.version == "v0.2.0"
    assert mesh.spec.service_account_name == "inventory-service"
    assert mesh.spec.termination.grace_period_seconds == 30
    assert mesh.deployment_name == "inventory-service-v0-2-0"


def test_fields_are_optional(manifest):
    assert manifest.spec.version is None
    assert manifest.spec.service_account_name is None
    assert manifest.spec.termination is None
    assert manifest.deployment_name == "demo-web"


@pytest.mark.parametrize(
    "version",
    ["0.2.0", "v0.2", "v0.2.0.1", "v01.2.0", "v0.2.0-rc1", "v1000.0.0", "V0.2.0", "latest", ""],
)
def test_invalid_versions_rejected(write_manifest, mesh_dict, version):
    mesh_dict["spec"]["version"] = version
    with pytest.raises(ManifestError, match="spec.version"):
        _load(write_manifest, mesh_dict)


def test_version_must_be_a_string(write_manifest, mesh_dict):
    mesh_dict["spec"]["version"] = 1.0
    with pytest.raises(ManifestError, match="spec.version"):
        _load(write_manifest, mesh_dict)


def test_versioned_deployment_name_length_bounded(write_manifest, mesh_dict):
    # "-v0-2-0" adds 7 characters: 56 + 7 = 63 is the limit.
    mesh_dict["metadata"]["name"] = mesh_dict["spec"]["serviceAccountName"] = "a" * 56
    assert len(_load(write_manifest, mesh_dict).deployment_name) == 63
    mesh_dict["metadata"]["name"] = mesh_dict["spec"]["serviceAccountName"] = "a" * 57
    with pytest.raises(ManifestError, match="exceeds 63 characters"):
        _load(write_manifest, mesh_dict)


@pytest.mark.parametrize(
    "sa",
    ["orders-service", "default", "istiod", "inventory-service-v2", "Inventory-Service", ""],
)
def test_service_account_must_match_service_name(write_manifest, mesh_dict, sa):
    mesh_dict["spec"]["serviceAccountName"] = sa
    with pytest.raises(ManifestError, match="must equal metadata.name"):
        _load(write_manifest, mesh_dict)


def test_service_account_snake_case_key_rejected(write_manifest, mesh_dict):
    mesh_dict["spec"]["service_account_name"] = mesh_dict["spec"].pop("serviceAccountName")
    with pytest.raises(ManifestError, match="service_account_name"):
        _load(write_manifest, mesh_dict)


@pytest.mark.parametrize(
    ("termination", "error"),
    [
        ({"gracePeriodSeconds": 0}, "greater than or equal to 1"),
        ({"gracePeriodSeconds": 301}, "less than or equal to 300"),
        ({"gracePeriodSeconds": 30, "preStopSleepSeconds": -1}, "greater than or equal to 0"),
        ({"gracePeriodSeconds": 90, "preStopSleepSeconds": 61}, "less than or equal to 60"),
        ({"gracePeriodSeconds": 10, "preStopSleepSeconds": 10}, "less than gracePeriodSeconds"),
        ({"preStopSleepSeconds": 5}, "gracePeriodSeconds"),
        ({"gracePeriodSeconds": "30"}, "valid integer"),
        ({"gracePeriodSeconds": 30, "command": ["sh", "-c", "sleep 5"]}, "Extra inputs"),
    ],
)
def test_termination_bounds(write_manifest, mesh_dict, termination, error):
    mesh_dict["spec"]["termination"] = termination
    with pytest.raises(ManifestError, match=error):
        _load(write_manifest, mesh_dict)


@pytest.mark.parametrize(
    "field",
    ["serviceAccount", "automountServiceAccountToken", "labels", "annotations", "podLabels"],
)
def test_other_identity_and_label_fields_rejected(write_manifest, mesh_dict, field):
    mesh_dict["spec"][field] = "x"
    with pytest.raises(ManifestError, match="Extra inputs"):
        _load(write_manifest, mesh_dict)


def test_namespace_allowlist_still_enforced_with_mesh_fields(write_manifest, mesh_dict):
    mesh_dict["metadata"]["namespace"] = "istio-system"
    with pytest.raises(ManifestError, match="not allowlisted"):
        _load(write_manifest, mesh_dict)


@pytest.mark.parametrize(
    ("path", "deployment"),
    [
        ("inventory-service.yaml", "inventory-service-v0-1-0"),
        ("inventory-service-v0.2.0.yaml", "inventory-service-v0-2-0"),
        ("incidents/inventory-service-v0.2.0-faulty.yaml", "inventory-service-v0-2-0"),
        # orders-service is not canaried; it keeps its original, unversioned Deployment.
        ("orders-service.yaml", "orders-service"),
    ],
)
def test_service_manifests_declare_identity(path, deployment):
    m = load_manifest(MANIFESTS / path)
    assert m.spec.service_account_name == m.name
    assert m.deployment_name == deployment
    assert m.spec.termination is not None


# --- rendering ------------------------------------------------------------------------


def test_versioned_deployment_labels_and_selector(mesh):
    dep = to_dict(build_deployment(mesh))
    labels = {
        "app.kubernetes.io/name": "inventory-service",
        "app.kubernetes.io/managed-by": "platformforge",
        "app.kubernetes.io/version": "v0.2.0",
    }
    assert dep["metadata"]["name"] == "inventory-service-v0-2-0"
    assert dep["metadata"]["labels"] == labels
    assert dep["spec"]["selector"]["matchLabels"] == labels
    assert dep["spec"]["template"]["metadata"]["labels"] == labels


def test_service_selector_spans_versions(mesh):
    svc = to_dict(build_service(mesh))
    assert svc["metadata"]["name"] == "inventory-service"
    assert svc["spec"]["selector"] == {
        "app.kubernetes.io/name": "inventory-service",
        "app.kubernetes.io/managed-by": "platformforge",
    }
    assert svc["spec"]["type"] == "ClusterIP"


def test_pod_identity_and_token_hardening(mesh):
    pod = to_dict(build_deployment(mesh))["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "inventory-service"
    assert pod["automountServiceAccountToken"] is False


def test_service_account_is_identity_only(mesh):
    sa = to_dict(build_service_account(mesh))
    assert sa == {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "inventory-service",
            "namespace": "platformforge-dev",
            "labels": {
                "app.kubernetes.io/name": "inventory-service",
                "app.kubernetes.io/managed-by": "platformforge",
            },
        },
        "automountServiceAccountToken": False,
    }


def test_termination_rendering(mesh):
    pod = to_dict(build_deployment(mesh))["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 30
    assert pod["containers"][0]["lifecycle"] == {"preStop": {"sleep": {"seconds": 5}}}


def test_zero_pre_stop_renders_no_hook(write_manifest, mesh_dict):
    mesh_dict["spec"]["termination"] = {"gracePeriodSeconds": 20}
    pod = to_dict(build_deployment(_load(write_manifest, mesh_dict)))["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 20
    assert "lifecycle" not in pod["containers"][0]


def test_unset_fields_render_like_before(manifest):
    pod = to_dict(build_deployment(manifest))["spec"]["template"]["spec"]
    for key in ("serviceAccountName", "terminationGracePeriodSeconds"):
        assert key not in pod
    assert "lifecycle" not in pod["containers"][0]


def test_mesh_fields_keep_security_defaults(mesh):
    pod = to_dict(build_deployment(mesh))["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_versioned_label_selector(mesh):
    assert label_selector(mesh) == (
        "app.kubernetes.io/name=inventory-service,app.kubernetes.io/managed-by=platformforge,"
        "app.kubernetes.io/version=v0.2.0"
    )


def test_namespace_opts_into_sidecar_injection():
    labels = to_dict(build_namespace("platformforge-dev"))["metadata"]["labels"]
    assert labels["istio-injection"] == "enabled"
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


# --- deployer -------------------------------------------------------------------------


def _obj(labels=None, resource_version="7"):
    obj = MagicMock()
    obj.metadata = client.V1ObjectMeta(labels=labels, resource_version=resource_version)
    obj.spec.cluster_ip = "10.96.0.10"
    obj.spec.cluster_ips = ["10.96.0.10"]
    return obj


@pytest.fixture
def clients():
    core, apps = MagicMock(spec=client.CoreV1Api), MagicMock(spec=client.AppsV1Api)
    core.read_namespaced_service.side_effect = ApiException(status=404)
    core.read_namespaced_service_account.side_effect = ApiException(status=404)
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    return KubeClients(core=core, apps=apps)


def test_deploy_creates_service_account_before_deployment(clients, mesh):
    order = []
    clients.core.create_namespaced_service_account.side_effect = lambda *a, **k: order.append("sa")
    clients.apps.create_namespaced_deployment.side_effect = lambda *a, **k: order.append("dep")
    applied = deployer.deploy(clients, mesh)
    assert [(a.kind, a.name, a.action) for a in applied] == [
        ("Namespace", "platformforge-dev", "exists"),
        ("ServiceAccount", "inventory-service", "created"),
        ("Deployment", "inventory-service-v0-2-0", "created"),
        ("Service", "inventory-service", "created"),
    ]
    assert order == ["sa", "dep"]


def test_deploy_without_service_account_creates_none(clients, manifest):
    deployer.deploy(clients, manifest)
    clients.core.create_namespaced_service_account.assert_not_called()
    clients.core.read_namespaced_service_account.assert_not_called()


def test_deploy_updates_managed_service_account(clients, mesh):
    clients.core.create_namespaced_service_account.side_effect = ApiException(status=409)
    clients.core.read_namespaced_service_account.side_effect = None
    clients.core.read_namespaced_service_account.return_value = _obj(MANAGED, "21")
    applied = deployer.deploy(clients, mesh)
    assert ("ServiceAccount", "updated") in [(a.kind, a.action) for a in applied]
    name, ns, body = clients.core.replace_namespaced_service_account.call_args.args
    assert (name, ns) == ("inventory-service", "platformforge-dev")
    assert body.metadata.resource_version == "21"
    assert body.automount_service_account_token is False


@pytest.mark.parametrize("labels", [None, {}, {"app.kubernetes.io/managed-by": "helm"}])
def test_deploy_refuses_unmanaged_service_account_before_any_write(clients, mesh, labels):
    clients.core.read_namespaced_service_account.side_effect = None
    clients.core.read_namespaced_service_account.return_value = _obj(labels)
    with pytest.raises(deployer.OwnershipError, match="ServiceAccount 'inventory-service'"):
        deployer.deploy(clients, mesh)
    clients.core.create_namespaced_service_account.assert_not_called()
    clients.core.replace_namespaced_service_account.assert_not_called()
    clients.apps.create_namespaced_deployment.assert_not_called()


def test_apply_service_account_conflict_rechecks_ownership(clients, mesh):
    clients.core.create_namespaced_service_account.side_effect = ApiException(status=409)
    clients.core.read_namespaced_service_account.side_effect = None
    clients.core.read_namespaced_service_account.return_value = _obj({})
    with pytest.raises(deployer.OwnershipError):
        deployer.apply_service_account(clients.core, mesh)
    clients.core.replace_namespaced_service_account.assert_not_called()


def test_apply_service_account_requires_declared_identity(clients, manifest):
    with pytest.raises(deployer.DeployError, match="serviceAccountName"):
        deployer.apply_service_account(clients.core, manifest)


def test_preflight_checks_versioned_deployment_name(clients, mesh):
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = _obj({})
    with pytest.raises(deployer.OwnershipError, match="inventory-service-v0-2-0"):
        deployer.deploy(clients, mesh)
    assert clients.apps.read_namespaced_deployment.call_args.args[0] == "inventory-service-v0-2-0"


def test_mesh_entry_points_recheck_allowlist(clients, mesh):
    rogue = ServiceManifest.model_construct(
        api_version=mesh.api_version,
        kind=mesh.kind,
        metadata=mesh.metadata.model_construct(name="inventory-service", namespace="default"),
        spec=mesh.spec,
    )
    for call in (
        lambda: deployer.apply_service_account(clients.core, rogue),
        lambda: deployer.find_overlapping_deployments(clients.apps, rogue),
    ):
        with pytest.raises(deployer.DeployError, match="not allowlisted"):
            call()
    assert not clients.core.mock_calls
    assert not clients.apps.mock_calls


def _dep(name, selector, template_labels):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels=selector),
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=template_labels)),
        ),
    )


def test_find_overlapping_deployments(clients, mesh):
    base = {"app.kubernetes.io/name": "inventory-service", **MANAGED}
    v1 = {**base, "app.kubernetes.io/version": "v0.1.0"}
    v2 = {**base, "app.kubernetes.io/version": "v0.2.0"}
    clients.apps.list_namespaced_deployment.return_value = client.V1DeploymentList(
        items=[
            _dep("inventory-service", base, base),  # legacy unversioned: selects our pods
            _dep("inventory-service-v0-1-0", v1, v1),  # sibling version: disjoint
            _dep("inventory-service-v0-2-0", v2, v2),  # ourselves
        ]
    )
    assert deployer.find_overlapping_deployments(clients.apps, mesh) == ["inventory-service"]
    kwargs = clients.apps.list_namespaced_deployment.call_args.kwargs
    assert kwargs["label_selector"] == (
        "app.kubernetes.io/name=inventory-service,app.kubernetes.io/managed-by=platformforge"
    )


def test_unversioned_manifest_reports_versioned_overlap(clients, manifest):
    base = {"app.kubernetes.io/name": "demo-web", **MANAGED}
    v1 = {**base, "app.kubernetes.io/version": "v1.0.0"}
    clients.apps.list_namespaced_deployment.return_value = client.V1DeploymentList(
        items=[_dep("demo-web-v1-0-0", v1, v1)]
    )
    assert deployer.find_overlapping_deployments(clients.apps, manifest) == ["demo-web-v1-0-0"]


def test_status_reads_versioned_deployment_and_identity(clients, mesh):
    dep = build_deployment(mesh)
    dep.status = client.V1DeploymentStatus(replicas=2, available_replicas=2)
    clients.apps.read_namespaced_deployment.side_effect = None
    clients.apps.read_namespaced_deployment.return_value = dep
    clients.core.list_namespaced_pod.return_value = client.V1PodList(items=[])
    st = deployer.get_status(clients, mesh)
    assert clients.apps.read_namespaced_deployment.call_args.args[0] == "inventory-service-v0-2-0"
    assert (st.deployment, st.version, st.service_account) == (
        "inventory-service-v0-2-0",
        "v0.2.0",
        "inventory-service",
    )
    assert (
        "app.kubernetes.io/version=v0.2.0"
        in (clients.core.list_namespaced_pod.call_args.kwargs["label_selector"])
    )


def test_logs_select_only_this_version(clients, mesh):
    clients.core.list_namespaced_pod.return_value = client.V1PodList(items=[])
    deployer.get_logs(clients.core, mesh, 10)
    selector = clients.core.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert selector.endswith("app.kubernetes.io/version=v0.2.0")


def test_wait_for_rollout_uses_deployment_name(clients, mesh):
    dep = build_deployment(mesh)
    dep.metadata.generation = 1
    dep.status = client.V1DeploymentStatus(
        observed_generation=1, replicas=2, updated_replicas=2, available_replicas=2
    )
    clients.apps.read_namespaced_deployment_status.return_value = dep
    deployer.wait_for_rollout(clients.apps, mesh, 10)
    assert clients.apps.read_namespaced_deployment_status.call_args.args == (
        "inventory-service-v0-2-0",
        "platformforge-dev",
    )


# --- CLI ------------------------------------------------------------------------------


def test_cli_dry_run_includes_service_account(write_manifest, mesh_dict):
    result = CliRunner().invoke(app, ["deploy", str(write_manifest(mesh_dict)), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert result.output.index("kind: ServiceAccount") < result.output.index("kind: Deployment")
    assert "name: inventory-service-v0-2-0" in result.output


def test_cli_validate_reports_version_and_identity(write_manifest, mesh_dict):
    result = CliRunner().invoke(app, ["validate", str(write_manifest(mesh_dict))])
    assert result.exit_code == 0, result.output
    assert "version=v0.2.0" in result.output
    assert "serviceAccount=inventory-service" in result.output

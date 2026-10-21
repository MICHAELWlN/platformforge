"""GKE preparation checks. These tests do not contact a cluster."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from platformforge.gke import (
    GKE_CONTEXT,
    GKE_REGISTRY,
    GkePrepError,
    exposure_findings,
    fits_on_inspected_node,
    gke_image,
    kubectl_argv,
    load_image_lock,
    main,
    planned_requests,
    publish_refused,
    render_manifest,
    require_gke_context,
    run_preflight,
)
from tests.conftest import REPO_ROOT

DIGEST = "sha256:" + ("ab" * 32)


def _lock(tmp_path: Path) -> Path:
    path = tmp_path / "image-lock.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "registry": GKE_REGISTRY,
                "images": {
                    "inventory-service": {"0.1.0": DIGEST, "0.2.0": DIGEST},
                    "orders-service": {"0.2.0": DIGEST},
                },
            }
        )
    )
    return path


def test_context_must_be_explicit_and_pinned():
    assert require_gke_context(GKE_CONTEXT) == GKE_CONTEXT
    with pytest.raises(GkePrepError, match="required"):
        require_gke_context(None)
    with pytest.raises(GkePrepError, match="required"):
        require_gke_context("  ")
    with pytest.raises(GkePrepError, match="kind"):
        require_gke_context("kind-platformforge")
    with pytest.raises(GkePrepError, match="refusing context"):
        require_gke_context("gke_other_us-central1-a_platformforge-dev")


def test_kubectl_argv_is_read_only_and_pins_context():
    argv = kubectl_argv(GKE_CONTEXT, ["get", "nodes"])
    assert argv[:3] == ["kubectl", "--context", GKE_CONTEXT]
    assert "apply" not in argv
    with pytest.raises(GkePrepError, match="get or kubectl version"):
        kubectl_argv(GKE_CONTEXT, ["delete", "namespace", "platformforge-dev"])
    with pytest.raises(GkePrepError, match="kind"):
        kubectl_argv("kind-platformforge", ["get", "nodes"])


def test_image_reference_is_digest_pinned_to_the_project_registry():
    ref = gke_image("inventory-service", "0.2.0", DIGEST)
    assert ref == f"{GKE_REGISTRY}/inventory-service@{DIGEST}"
    assert ":latest" not in ref
    with pytest.raises(GkePrepError, match="published sha256"):
        gke_image("orders-service", "0.2.0", "sha256:" + ("0" * 64))
    with pytest.raises(GkePrepError, match="not one of"):
        gke_image("demo-web", "0.2.0", DIGEST)


def test_example_lock_is_rejected(tmp_path: Path):
    example = REPO_ROOT / "deploy" / "gke" / "image-lock.example.yaml"
    with pytest.raises(GkePrepError, match="placeholder"):
        load_image_lock(example)
    locked = load_image_lock(_lock(tmp_path))
    assert locked[("orders-service", "0.2.0")].endswith(DIGEST)


def test_render_replaces_kind_tag_and_keeps_security_fields(tmp_path: Path):
    source = (REPO_ROOT / "services" / "manifests" / "orders-service.yaml").read_text()
    rendered = yaml.safe_load(render_manifest(source, load_image_lock(_lock(tmp_path))))
    assert rendered["spec"]["image"] == f"{GKE_REGISTRY}/orders-service@{DIGEST}"
    assert rendered["metadata"]["namespace"] == "platformforge-dev"
    assert rendered["spec"]["serviceAccountName"] == "orders-service"
    assert "iam.gke.io/gcp-service-account" not in source


def test_preflight_refuses_a_public_service():
    def runner(argv: list[str]) -> str:
        if argv[-1].startswith("custom-columns"):
            return "NS NAME TYPE\nplatformforge-dev web LoadBalancer\n"
        return "ok\n"

    with pytest.raises(GkePrepError, match="public Service"):
        run_preflight(GKE_CONTEXT, runner=runner)


def test_preflight_accepts_clusterip_only():
    def runner(argv: list[str]) -> str:
        if "custom-columns" in argv[-1]:
            return "NS NAME TYPE\nkube-system kube-dns ClusterIP\n"
        return "ok\n"

    report = run_preflight(GKE_CONTEXT, runner=runner)
    assert "ClusterIP" in report


def test_prepared_files_have_no_public_exposure():
    root = REPO_ROOT / "deploy" / "gke"
    offenders = []
    for path in root.rglob("*.yaml"):
        findings = exposure_findings(path.read_text(encoding="utf-8"))
        if findings:
            offenders.append((path.name, findings))
    assert offenders == []


def test_workload_identity_is_not_an_application_identity():
    document = yaml.safe_load(
        (REPO_ROOT / "deploy" / "gke" / "workload-identity-serviceaccount.yaml").read_text()
    )
    assert document["metadata"]["name"] == "platformforge-gcp"
    assert document["metadata"]["namespace"] == "platformforge-dev"
    assert document["automountServiceAccountToken"] is False
    annotation = document["metadata"]["annotations"]["iam.gke.io/gcp-service-account"]
    assert annotation.startswith("platformforge-dev-wi@")
    assert "orders-service" not in annotation
    assert "inventory-service" not in annotation


def test_gke_istio_operator_keeps_minimal_cni_and_kind_file_is_unchanged():
    gke = yaml.safe_load((REPO_ROOT / "deploy" / "gke" / "istio-operator.yaml").read_text())
    kind = (REPO_ROOT / "deploy" / "istio" / "istio-operator.yaml").read_text()
    assert gke["spec"]["profile"] == "minimal"
    assert gke["spec"]["components"]["cni"]["enabled"] is True
    assert gke["spec"]["components"]["cni"]["namespace"] == "istio-system"
    assert gke["spec"]["values"]["cni"]["chained"] is True
    assert gke["spec"]["values"]["cni"]["cniBinDir"] == "/home/kubernetes/bin"
    assert gke["spec"]["values"]["cni"]["cniConfDir"] == "/etc/cni/net.d"
    assert gke["spec"]["values"]["pilot"]["cni"]["enabled"] is True
    assert "platformforge-observability" in gke["spec"]["values"]["cni"]["excludeNamespaces"]
    assert "cniBinDir" not in kind
    assert "PILOT_ENABLE_NODE_UNTAINT" not in yaml.safe_dump(gke)
    policy_path = REPO_ROOT / "deploy" / "istio" / "platformforge-dev" / "peer-authentication.yaml"
    policy = yaml.safe_load(policy_path.read_text())
    assert policy["spec"]["mtls"]["mode"] == "STRICT"
    auth = yaml.safe_load(
        (
            REPO_ROOT / "deploy" / "istio" / "platformforge-dev" / "inventory-authorization.yaml"
        ).read_text()
    )
    principal = auth["spec"]["rules"][0]["from"][0]["source"]["principals"][0]
    assert principal == "cluster.local/ns/platformforge-dev/sa/orders-service"


def test_observability_overlays_do_not_add_disks_or_public_services():
    overlay = REPO_ROOT / "deploy" / "gke" / "observability"
    for path in overlay.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "LoadBalancer" not in text
        assert "enabled: true" not in text
    grafana = yaml.safe_load((overlay / "grafana-values.yaml").read_text())
    assert grafana["grafana.ini"]["auth.anonymous"]["enabled"] is False
    assert grafana["persistence"]["enabled"] is False
    prometheus = yaml.safe_load((overlay / "prometheus-values.yaml").read_text())
    assert prometheus["server"]["persistentVolume"]["enabled"] is False
    assert prometheus["prometheus-node-exporter"]["enabled"] is False


def test_requests_fit_on_the_inspected_node():
    planned = planned_requests(REPO_ROOT)
    assert planned.cpu_m == 730
    assert planned.memory_mi == 1952
    assert planned.pods == 12
    assert fits_on_inspected_node(planned)


def test_makefile_gke_build_is_amd64_and_does_not_push():
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = text.split("images-gke:", maxsplit=1)[1].split("\nimages-gke-publish:", maxsplit=1)[0]
    assert "--platform linux/amd64" in recipe
    assert "$(PLATFORM)" not in recipe
    assert "docker push" not in text
    assert "artifacts repositories create" not in text


def test_publish_command_refuses(capsys: pytest.CaptureFixture[str]):
    assert main(["publish-requirements"]) == 2
    captured = capsys.readouterr()
    assert "not approved" in captured.err
    assert "artifactregistry.googleapis.com" in captured.err
    assert publish_refused().startswith("Image publishing is not approved.")

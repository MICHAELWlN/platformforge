"""Static checks for the digest-pinned GKE canary controller manifest."""

from __future__ import annotations

from pathlib import Path

import yaml

from platformforge.gke import GKE_CONTEXT

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "deploy" / "gke" / "canary-controller.yaml"
KIND_MANIFEST = REPO / "deploy" / "kind" / "canary-controller.yaml"
_FORBIDDEN = (
    "LoadBalancer",
    "NodePort",
    "hostNetwork",
    "hostPort",
    "externalIPs",
    "kind: Ingress",
    "kind: ClusterRole",
    "cluster-admin",
    "secrets",
)


def _docs() -> list[dict]:
    return list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))


def test_gke_manifest_is_private_and_digest_pinned() -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    assert "image-pushed: true" in text
    assert "image-pushed: false" not in text
    assert "kind-platformforge" not in text
    for word in _FORBIDDEN:
        assert word not in text
    docs = _docs()
    role = next(doc for doc in docs if doc["kind"] == "Role")
    rules = role["rules"]
    assert rules[0]["verbs"] == ["get"]
    assert rules[0]["resourceNames"] == [
        "inventory-service-v0-1-0",
        "inventory-service-v0-2-0",
    ]
    assert rules[1]["verbs"] == ["get", "update"]
    assert rules[1]["resourceNames"] == ["inventory-service"]
    assert all("delete" not in rule["verbs"] for rule in rules)
    deploy = next(doc for doc in docs if doc["kind"] == "Deployment")
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["PLATFORMFORGE_CANARY_CONTEXT"] == GKE_CONTEXT
    assert env["PLATFORMFORGE_CANARY_GKE"] == "1"
    assert env["PLATFORMFORGE_CANARY_IN_CLUSTER"] == "1"
    assert container["image"] == (
        "us-central1-docker.pkg.dev/platformforge-dev-mn2026/platformforge/"
        "canary-controller@sha256:b7e8af0a35fd1d968ee3aacf701701e15da8ff600d6360662885f6c5846cc6a2"
    )
    service = next(doc for doc in docs if doc["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"
    annotations = deploy["spec"]["template"]["metadata"]["annotations"]
    assert annotations["sidecar.istio.io/inject"] == "false"
    assert container["resources"]["requests"] == {"cpu": "50m", "memory": "64Mi"}
    assert "delete" not in text.lower() or "cannot delete" in text.lower()


def test_kind_manifest_does_not_enable_the_gke_gate() -> None:
    text = KIND_MANIFEST.read_text(encoding="utf-8")
    assert "PLATFORMFORGE_CANARY_GKE" not in text
    assert "value: kind-platformforge" in text

from __future__ import annotations

import pytest

from platformforge.manifest import (
    ManifestError,
    cpu_to_millicores,
    load_manifest,
    memory_to_bytes,
    parse_manifest,
)
from tests.conftest import EXAMPLE_MANIFEST


def _load(write_manifest, data):
    return load_manifest(write_manifest(data))


def test_example_manifest_is_valid():
    m = load_manifest(EXAMPLE_MANIFEST)
    assert m.name == "demo-web"
    assert m.namespace == "platformforge-dev"
    assert m.spec.replicas == 2
    assert m.spec.probes.readiness.initial_delay_seconds == 2


@pytest.mark.parametrize(
    "name",
    [
        "Demo",
        "1demo",
        "demo-",
        "-demo",
        "demo_web",
        "demo.web",
        "a" * 64,
        "",
        "demo web",
        "demo;rm -rf /",
        "$(whoami)",
    ],
)
def test_invalid_service_names_rejected(write_manifest, manifest_dict, name):
    manifest_dict["metadata"]["name"] = name
    with pytest.raises(ManifestError, match="metadata.name"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize("name", ["a", "demo", "orders-service", "svc1", "a" * 63])
def test_valid_service_names_accepted(write_manifest, manifest_dict, name):
    manifest_dict["metadata"]["name"] = name
    assert _load(write_manifest, manifest_dict).name == name


@pytest.mark.parametrize(
    ("namespace", "reason"),
    [
        ("default", "not allowlisted"),
        ("kube-system", "not allowlisted"),
        ("platformforge-prod", "not allowlisted"),
        ("PlatformForge-Dev", "DNS-1123"),
        ("platformforge-dev/../kube-system", "DNS-1123"),
        ("", "DNS-1123"),
    ],
)
def test_namespace_allowlist_enforced(write_manifest, manifest_dict, namespace, reason):
    manifest_dict["metadata"]["namespace"] = namespace
    with pytest.raises(ManifestError, match=reason):
        _load(write_manifest, manifest_dict)


def test_raw_kubernetes_deployment_rejected(write_manifest):
    raw = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "x", "namespace": "platformforge-dev"},
        "spec": {"template": {"spec": {"hostNetwork": True}}},
    }
    with pytest.raises(ManifestError) as exc:
        _load(write_manifest, raw)
    assert "apiVersion" in str(exc.value)
    assert "kind" in str(exc.value)


@pytest.mark.parametrize(
    "path",
    [
        ("spec", "hostNetwork"),
        ("spec", "securityContext"),
        ("spec", "serviceType"),
        ("metadata", "labels"),
        ("spec", "resources", "limits", "nvidia.com/gpu"),
        ("spec", "probes", "readiness", "exec"),
    ],
)
def test_unknown_fields_rejected(write_manifest, manifest_dict, path):
    target = manifest_dict
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = {"anything": True}
    with pytest.raises(ManifestError, match="Extra inputs are not permitted"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize(
    ("image", "ok"),
    [
        ("nginx:1.29", True),
        ("ghcr.io/org/app:v1.2.3", True),
        ("localhost:5000/app:dev", True),
        ("app@sha256:" + "a" * 64, True),
        ("app:latest@sha256:" + "b" * 64, True),
        ("nginx", False),
        ("nginx:latest", False),
        ("NGINX:1.0", False),
        ("nginx:1.0; rm -rf /", False),
        ("nginx:1.0 --privileged", False),
        ("", False),
    ],
)
def test_image_reference_validation(write_manifest, manifest_dict, image, ok):
    manifest_dict["spec"]["image"] = image
    if ok:
        assert _load(write_manifest, manifest_dict).spec.image == image
    else:
        with pytest.raises(ManifestError, match="spec.image"):
            _load(write_manifest, manifest_dict)


@pytest.mark.parametrize(
    ("requests", "limits", "error"),
    [
        ({"cpu": "300m", "memory": "32Mi"}, {"cpu": "200m", "memory": "64Mi"}, "requests.cpu"),
        ({"cpu": "50m", "memory": "128Mi"}, {"cpu": "200m", "memory": "64Mi"}, "requests.memory"),
        ({"cpu": "50m", "memory": "32Mi"}, {"cpu": "3", "memory": "64Mi"}, "limits.cpu"),
        ({"cpu": "50m", "memory": "32Mi"}, {"cpu": "1", "memory": "4Gi"}, "limits.memory"),
        ({"cpu": "0m", "memory": "32Mi"}, {"cpu": "1", "memory": "64Mi"}, "greater than zero"),
        ({"cpu": "abc", "memory": "32Mi"}, {"cpu": "1", "memory": "64Mi"}, "invalid CPU"),
        ({"cpu": "50m", "memory": "32MB"}, {"cpu": "1", "memory": "64Mi"}, "invalid memory"),
    ],
)
def test_resource_bounds(write_manifest, manifest_dict, requests, limits, error):
    manifest_dict["spec"]["resources"] = {"requests": requests, "limits": limits}
    with pytest.raises(ManifestError, match=error):
        _load(write_manifest, manifest_dict)


def test_resources_required(write_manifest, manifest_dict):
    del manifest_dict["spec"]["resources"]
    with pytest.raises(ManifestError, match="spec.resources: Field required"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize("probe", ["readiness", "liveness"])
def test_probes_required(write_manifest, manifest_dict, probe):
    del manifest_dict["spec"]["probes"][probe]
    with pytest.raises(ManifestError, match=f"spec.probes.{probe}: Field required"):
        _load(write_manifest, manifest_dict)


def test_integer_cpu_accepted(write_manifest, manifest_dict):
    manifest_dict["spec"]["resources"]["limits"]["cpu"] = 1
    assert _load(write_manifest, manifest_dict).spec.resources.limits.cpu == "1"


@pytest.mark.parametrize("path", ["health", "/health?x=1", "/a b", "http://evil.example/"])
def test_probe_path_validation(write_manifest, manifest_dict, path):
    manifest_dict["spec"]["probes"]["readiness"]["path"] = path
    with pytest.raises(ManifestError, match="spec.probes.readiness.path"):
        _load(write_manifest, manifest_dict)


def test_probe_snake_case_keys_rejected(write_manifest, manifest_dict):
    probe = manifest_dict["spec"]["probes"]["liveness"]
    probe["initial_delay_seconds"] = probe.pop("initialDelaySeconds")
    with pytest.raises(ManifestError, match="initial_delay_seconds"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize("replicas", [0, 6, -1])
def test_replica_bounds(write_manifest, manifest_dict, replicas):
    manifest_dict["spec"]["replicas"] = replicas
    with pytest.raises(ManifestError, match="spec.replicas"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize("port", [80, 0, 70000])
def test_port_bounds(write_manifest, manifest_dict, port):
    manifest_dict["spec"]["port"] = port
    with pytest.raises(ManifestError, match="spec.port"):
        _load(write_manifest, manifest_dict)


def test_string_replicas_rejected_in_strict_mode(write_manifest, manifest_dict):
    manifest_dict["spec"]["replicas"] = "2"
    with pytest.raises(ManifestError, match="spec.replicas"):
        _load(write_manifest, manifest_dict)


@pytest.mark.parametrize("name", ["DB_PASSWORD", "api_key", "GITHUB_TOKEN", "AWS_SECRET_ACCESS"])
def test_credential_like_env_rejected(write_manifest, manifest_dict, name):
    manifest_dict["spec"]["env"] = [{"name": name, "value": "x"}]
    with pytest.raises(ManifestError, match="credential"):
        _load(write_manifest, manifest_dict)


def test_env_secret_ref_rejected(write_manifest, manifest_dict):
    manifest_dict["spec"]["env"] = [{"name": "X", "valueFrom": {"secretKeyRef": {}}}]
    with pytest.raises(ManifestError, match="Extra inputs"):
        _load(write_manifest, manifest_dict)


def test_duplicate_env_rejected(write_manifest, manifest_dict):
    manifest_dict["spec"]["env"] = [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]
    with pytest.raises(ManifestError, match="duplicate env var names: A"):
        _load(write_manifest, manifest_dict)


def test_yaml_aliases_rejected():
    text = "a: &x [1, 2]\nb: [*x, *x]\n"
    with pytest.raises(ManifestError, match="aliases"):
        parse_manifest(text)


def test_multi_document_rejected():
    text = EXAMPLE_MANIFEST.read_text() + "\n---\n" + EXAMPLE_MANIFEST.read_text()
    with pytest.raises(ManifestError, match="invalid YAML"):
        parse_manifest(text)


def test_python_object_tags_rejected():
    with pytest.raises(ManifestError, match="invalid YAML"):
        parse_manifest("!!python/object/apply:os.system ['echo pwned']")


@pytest.mark.parametrize("text", ["[]", "just a string", "", "42"])
def test_non_mapping_rejected(text):
    with pytest.raises(ManifestError, match="single YAML mapping"):
        parse_manifest(text)


def test_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.yaml")


def test_oversized_file(write_manifest):
    path = write_manifest("# " + "x" * 70_000)
    with pytest.raises(ManifestError, match="limit"):
        load_manifest(path)


def test_quantity_parsers():
    assert cpu_to_millicores("250m") == 250
    assert cpu_to_millicores("1") == 1000
    assert cpu_to_millicores("0.5") == 500
    assert memory_to_bytes("64Mi") == 64 * 1024**2
    assert memory_to_bytes("1Gi") == 1024**3
    assert memory_to_bytes("1024") == 1024

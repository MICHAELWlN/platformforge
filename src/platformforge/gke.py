"""GKE deployment preparation. Nothing here contacts a cluster unless preflight is run.

Preflight is read-only and only accepts the pinned GKE context. It does not create
namespaces, install Istio, push images, or change IAM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from platformforge.manifest import ServiceManifest, cpu_to_millicores, memory_to_bytes

GKE_CONTEXT = "gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
GKE_PROJECT = "platformforge-dev-mn2026"
GKE_REGION = "us-central1"
GKE_REPOSITORY = "platformforge"
GKE_REGISTRY = f"{GKE_REGION}-docker.pkg.dev/{GKE_PROJECT}/{GKE_REPOSITORY}"

# Measured on the single e2-standard-4 on 2026-10-20. Requests, not live usage.
# Memory is 13591632 Ki of allocatable, truncated to whole MiB.
# System memory requests were 1169.5 Mi and are rounded up.
INSPECTED_ALLOCATABLE_CPU_M = 3920
INSPECTED_ALLOCATABLE_MEMORY_MI = 13273
INSPECTED_ALLOCATABLE_EPHEMERAL_BYTES = 18_828_602_911
INSPECTED_SYSTEM_CPU_M = 665
INSPECTED_SYSTEM_MEMORY_MI = 1170
INSPECTED_POD_CAPACITY = 64
INSPECTED_SYSTEM_PODS = 13

APP_MANIFESTS = (
    "services/manifests/inventory-service.yaml",
    "services/manifests/inventory-service-v0.2.0.yaml",
    "services/manifests/orders-service.yaml",
)
ISTIO_OPERATOR = "deploy/gke/istio-operator.yaml"
OBSERVABILITY_REQUESTS = (
    ("deploy/observability/prometheus-values.yaml", ("server", "resources")),
    ("deploy/observability/grafana-values.yaml", ("resources",)),
    ("deploy/observability/loki-values.yaml", ("singleBinary", "resources")),
    ("deploy/observability/tempo-values.yaml", ("tempo", "resources")),
    ("deploy/observability/alloy-values.yaml", ("alloy", "resources")),
    ("deploy/observability/alloy-values.yaml", ("configReloader", "resources")),
)
REQUIRED_IMAGES = (
    ("inventory-service", "0.1.0"),
    ("inventory-service", "0.2.0"),
    ("orders-service", "0.2.0"),
)
_LOCAL_IMAGE = re.compile(r"^platformforge/(?P<name>[a-z0-9-]+):(?P<version>\d+\.\d+\.\d+)$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PLACEHOLDER_DIGEST = "sha256:" + ("0" * 64)
_FORBIDDEN = (
    "LoadBalancer",
    "NodePort",
    "hostNetwork: true",
    "kind: Ingress",
    "externalIPs:",
)

PUBLISH_APPROVALS = (
    "Enable artifactregistry.googleapis.com on platformforge-dev-mn2026.",
    "Create a Docker Artifact Registry repository named platformforge in us-central1. "
    "That stores image bytes and is billable.",
    "Grant roles/artifactregistry.reader to "
    "platformforge-dev-nodes@platformforge-dev-mn2026.iam.gserviceaccount.com. "
    "Do not grant it to the workload service account, orders-service, or inventory-service.",
    "Authenticate Docker to us-central1-docker.pkg.dev on the operator machine. "
    "Do not commit the Docker config.",
    "Build with linux/amd64, push only those three tags, and record each sha256 digest.",
    "A later, separate approval is required before any kubectl apply or istioctl install.",
)


class GkePrepError(Exception):
    """Raised when a GKE preparation check refuses to continue."""


@dataclass(frozen=True)
class ResourceTotals:
    cpu_m: int
    memory_mi: int
    pods: int


def require_gke_context(context: str | None) -> str:
    """Accept only the pinned GKE context. Kind and implicit context are refused."""
    if context is None or context.strip() == "":
        raise GkePrepError("a Kubernetes context is required; the current context is not used")
    if context.startswith("kind-"):
        raise GkePrepError(f"refusing kind context {context!r}; this path is GKE-only")
    if context != GKE_CONTEXT:
        raise GkePrepError(f"refusing context {context!r}; expected {GKE_CONTEXT!r}")
    return context


def gke_image(service: str, version: str, digest: str) -> str:
    """Return a digest-pinned Artifact Registry reference. Tags are not accepted."""
    if (service, version) not in REQUIRED_IMAGES:
        raise GkePrepError(f"{service}:{version} is not one of the three GKE images")
    if not _DIGEST.fullmatch(digest) or digest == _PLACEHOLDER_DIGEST:
        raise GkePrepError(
            f"digest for {service}:{version} must be a published sha256, not a placeholder"
        )
    return f"{GKE_REGISTRY}/{service}@{digest}"


def load_image_lock(path: Path) -> dict[tuple[str, str], str]:
    """Load service/version digests. Empty or tag-only values are rejected."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GkePrepError(f"{path} is not a mapping")
    if data.get("registry") != GKE_REGISTRY:
        raise GkePrepError(f"registry must be {GKE_REGISTRY}")
    images = data.get("images")
    if not isinstance(images, dict):
        raise GkePrepError("images must be a mapping of service to version digests")
    found: dict[tuple[str, str], str] = {}
    for service, versions in images.items():
        if not isinstance(versions, dict):
            raise GkePrepError(f"images.{service} must map versions to digests")
        for version, digest in versions.items():
            found[(str(service), str(version))] = gke_image(str(service), str(version), str(digest))
    missing = [
        f"{name}:{version}" for name, version in REQUIRED_IMAGES if (name, version) not in found
    ]
    if missing:
        raise GkePrepError("image lock is missing " + ", ".join(missing))
    return found


def render_manifest(source: str, images: dict[tuple[str, str], str]) -> str:
    """Replace a kind image tag with the locked digest and re-validate the manifest."""
    data = yaml.safe_load(source)
    if not isinstance(data, dict):
        raise GkePrepError("manifest is not a mapping")
    spec = data.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("image"), str):
        raise GkePrepError("manifest has no spec.image")
    match = _LOCAL_IMAGE.fullmatch(spec["image"])
    if match is None:
        raise GkePrepError(
            f"source image {spec['image']!r} must be a local platformforge/<service>:<version> tag"
        )
    key = (match.group("name"), match.group("version"))
    try:
        spec["image"] = images[key]
    except KeyError as exc:
        raise GkePrepError(f"no digest locked for {key[0]}:{key[1]}") from exc
    manifest = ServiceManifest.model_validate(data)
    if manifest.spec.image != spec["image"]:
        raise GkePrepError("rendered image did not survive validation")
    return yaml.safe_dump(data, sort_keys=False)


def exposure_findings(text: str) -> list[str]:
    """Return public-exposure markers. A hit means the document is not safe to apply."""
    return [marker for marker in _FORBIDDEN if marker in text]


def _nested(document: dict, path: tuple[str, ...]) -> dict:
    current: object = document
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise GkePrepError(f"missing {'/'.join(path)}")
        current = current[key]
    if not isinstance(current, dict):
        raise GkePrepError(f"{'/'.join(path)} is not a mapping")
    return current


def _quantity_pair(resources: dict) -> tuple[int, int]:
    requests = resources.get("requests")
    if not isinstance(requests, dict):
        raise GkePrepError("resources.requests is missing")
    cpu = cpu_to_millicores(str(requests["cpu"]))
    memory_mi = memory_to_bytes(str(requests["memory"])) // (1024 * 1024)
    return cpu, memory_mi


def planned_requests(root: Path) -> ResourceTotals:
    """Sum the requests of the prepared GKE workload. Scheduling uses requests, not limits."""
    cpu = memory = pods = 0
    app_pods = 0
    for relative in APP_MANIFESTS:
        manifest = ServiceManifest.model_validate(
            yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        )
        one_cpu, one_mem = _quantity_pair(
            {
                "requests": {
                    "cpu": manifest.spec.resources.requests.cpu,
                    "memory": manifest.spec.resources.requests.memory,
                }
            }
        )
        cpu += one_cpu * manifest.spec.replicas
        memory += one_mem * manifest.spec.replicas
        app_pods += manifest.spec.replicas
        pods += manifest.spec.replicas
    operator = yaml.safe_load((root / ISTIO_OPERATOR).read_text(encoding="utf-8"))
    spec = operator["spec"]
    values = spec["values"]
    proxy_cpu, proxy_mem = _quantity_pair(values["global"]["proxy"]["resources"])
    cpu += proxy_cpu * app_pods
    memory += proxy_mem * app_pods
    pilot_cpu, pilot_mem = _quantity_pair(spec["components"]["pilot"]["k8s"]["resources"])
    cni_cpu, cni_mem = _quantity_pair(values["cni"]["resources"])
    cpu += pilot_cpu + cni_cpu
    memory += pilot_mem + cni_mem
    pods += 2
    for relative, path in OBSERVABILITY_REQUESTS:
        document = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        part_cpu, part_mem = _quantity_pair(_nested(document, path))
        cpu += part_cpu
        memory += part_mem
        if path[0] != "configReloader":
            pods += 1
    return ResourceTotals(cpu_m=cpu, memory_mi=memory, pods=pods)


def fits_on_inspected_node(planned: ResourceTotals) -> bool:
    """True when planned requests plus the inspected system requests fit on one node."""
    cpu = planned.cpu_m + INSPECTED_SYSTEM_CPU_M
    memory = planned.memory_mi + INSPECTED_SYSTEM_MEMORY_MI
    pods = planned.pods + INSPECTED_SYSTEM_PODS
    return (
        cpu <= INSPECTED_ALLOCATABLE_CPU_M
        and memory <= INSPECTED_ALLOCATABLE_MEMORY_MI
        and pods <= INSPECTED_POD_CAPACITY
    )


def kubectl_argv(context: str, args: Sequence[str]) -> list[str]:
    """Build a read-only kubectl command. Mutations are rejected before the context check."""
    if not args or args[0] not in {"get", "version"}:
        raise GkePrepError("preflight may only run kubectl get or kubectl version")
    pinned = require_gke_context(context)
    return ["kubectl", "--context", pinned, *args]


def _kubectl_env() -> dict[str, str]:
    env = os.environ.copy()
    if shutil.which("gke-gcloud-auth-plugin", path=env.get("PATH")):
        return env
    gcloud = shutil.which("gcloud", path=env.get("PATH"))
    if gcloud is None:
        return env
    completed = subprocess.run(  # noqa: S603
        [gcloud, "info", "--format=value(installation.sdk_root)"],
        check=False,
        capture_output=True,
        text=True,
    )
    root = completed.stdout.strip()
    plugin = Path(root) / "bin" / "gke-gcloud-auth-plugin" if root else None
    if plugin is not None and plugin.is_file():
        env["PATH"] = str(plugin.parent) + os.pathsep + env.get("PATH", "")
    return env


def run_preflight(context: str, runner: Callable[[list[str]], str] | None = None) -> str:
    """Read nodes, namespaces, and services. Refuse a public Service type."""
    calls = (
        ["version", "--client=false"],
        ["get", "nodes", "-o", "wide"],
        ["get", "ns"],
        [
            "get",
            "svc",
            "-A",
            "-o",
            "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,TYPE:.spec.type",
        ],
    )
    argv = [kubectl_argv(context, args) for args in calls]

    def execute(command: list[str]) -> str:
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            env=_kubectl_env(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise GkePrepError(f"{' '.join(command)} failed: {detail}")
        return completed.stdout

    run = runner or execute
    chunks = [run(command) for command in argv]
    services = chunks[-1]
    for line in services.splitlines()[1:]:
        if line.endswith("LoadBalancer") or line.endswith("NodePort"):
            raise GkePrepError(f"public Service is present: {line}")
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    """CLI for preflight, lock checks, and manifest rendering. Publishing stays refused."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m platformforge.gke")
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--context", required=True)
    check = sub.add_parser("check-lock")
    check.add_argument("lock", type=Path)
    render = sub.add_parser("render")
    render.add_argument("--lock", required=True, type=Path)
    render.add_argument("manifests", nargs="+", type=Path)
    sub.add_parser("publish-requirements")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            sys.stdout.write(run_preflight(args.context))
        elif args.command == "check-lock":
            locked = load_image_lock(args.lock)
            for service, version in REQUIRED_IMAGES:
                print(f"{service}:{version} {locked[(service, version)]}")
        elif args.command == "render":
            locked = load_image_lock(args.lock)
            documents = [
                render_manifest(path.read_text(encoding="utf-8"), locked) for path in args.manifests
            ]
            sys.stdout.write("\n".join(documents))
        else:
            print(publish_refused(), file=sys.stderr)
            return 2
    except (GkePrepError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def publish_refused() -> str:
    """Image publishing is prepared and stays blocked until a person approves it."""
    lines = [
        "Image publishing is not approved.",
        "This command does not enable APIs, create a repository, change IAM, or push.",
        "Required approvals, in order:",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(PUBLISH_APPROVALS, start=1))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

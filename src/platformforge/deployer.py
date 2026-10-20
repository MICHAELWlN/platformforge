"""Namespace-scoped deployment engine built on the official Kubernetes Python client."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from platformforge.kubecontext import KubeClients
from platformforge.manifest import ServiceManifest, validate_namespace
from platformforge.resources import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    build_deployment,
    build_namespace,
    build_service,
    build_service_account,
    label_selector,
    labels_for,
    pod_labels,
)

FIELD_MANAGER = "platformforge"
MAX_LOG_LINES = 5000


class DeployError(Exception):
    """Raised when a deployment operation cannot be completed."""


class OwnershipError(DeployError):
    """Raised when an existing object is not managed by PlatformForge."""


class RolloutTimeout(DeployError):
    """Raised when a rollout does not complete in time."""


@dataclass(frozen=True)
class AppliedResource:
    kind: str
    name: str
    action: str  # "created" | "updated" | "exists"


def _assert_allowed(namespace: str) -> None:
    # Defense in depth: manifests are validated on load, but every API entry point
    # re-checks the allowlist before touching the cluster.
    try:
        validate_namespace(namespace)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc


def _assert_managed(
    obj: client.V1Deployment | client.V1Service | client.V1ServiceAccount, kind: str, name: str
) -> None:
    labels = (obj.metadata.labels or {}) if obj.metadata else {}
    if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
        raise OwnershipError(
            f"{kind} {name!r} exists but is not labeled {MANAGED_BY_LABEL}={MANAGED_BY_VALUE}; "
            "refusing to modify a workload PlatformForge does not own"
        )


def ensure_namespace(core: client.CoreV1Api, namespace: str, create: bool) -> AppliedResource:
    _assert_allowed(namespace)
    try:
        core.read_namespace(namespace)
        return AppliedResource("Namespace", namespace, "exists")
    except ApiException as exc:
        if exc.status != 404:
            raise
    if not create:
        raise DeployError(
            f"namespace {namespace!r} does not exist; re-run with --create-namespace to create it"
        )
    core.create_namespace(build_namespace(namespace), field_manager=FIELD_MANAGER)
    return AppliedResource("Namespace", namespace, "created")


def apply_service_account(core: client.CoreV1Api, manifest: ServiceManifest) -> AppliedResource:
    _assert_allowed(manifest.namespace)
    name = manifest.spec.service_account_name
    if name is None:
        raise DeployError(f"{manifest.name!r} does not declare spec.serviceAccountName")
    body = build_service_account(manifest)
    try:
        core.create_namespaced_service_account(
            manifest.namespace, body, field_manager=FIELD_MANAGER
        )
        return AppliedResource("ServiceAccount", name, "created")
    except ApiException as exc:
        if exc.status != 409:
            raise
    existing = core.read_namespaced_service_account(name, manifest.namespace)
    _assert_managed(existing, "ServiceAccount", name)
    body.metadata.resource_version = existing.metadata.resource_version
    core.replace_namespaced_service_account(
        name, manifest.namespace, body, field_manager=FIELD_MANAGER
    )
    return AppliedResource("ServiceAccount", name, "updated")


def apply_deployment(apps: client.AppsV1Api, manifest: ServiceManifest) -> AppliedResource:
    _assert_allowed(manifest.namespace)
    name = manifest.deployment_name
    body = build_deployment(manifest)
    try:
        apps.create_namespaced_deployment(manifest.namespace, body, field_manager=FIELD_MANAGER)
        return AppliedResource("Deployment", name, "created")
    except ApiException as exc:
        if exc.status != 409:
            raise
    existing = apps.read_namespaced_deployment(name, manifest.namespace)
    _assert_managed(existing, "Deployment", name)
    body.metadata.resource_version = existing.metadata.resource_version
    apps.replace_namespaced_deployment(name, manifest.namespace, body, field_manager=FIELD_MANAGER)
    return AppliedResource("Deployment", name, "updated")


def apply_service(core: client.CoreV1Api, manifest: ServiceManifest) -> AppliedResource:
    _assert_allowed(manifest.namespace)
    body = build_service(manifest)
    try:
        core.create_namespaced_service(manifest.namespace, body, field_manager=FIELD_MANAGER)
        return AppliedResource("Service", manifest.name, "created")
    except ApiException as exc:
        if exc.status != 409:
            raise
    existing = core.read_namespaced_service(manifest.name, manifest.namespace)
    _assert_managed(existing, "Service", manifest.name)
    body.metadata.resource_version = existing.metadata.resource_version
    # clusterIP(s) are immutable; carry them over so replace does not try to change them.
    body.spec.cluster_ip = existing.spec.cluster_ip
    body.spec.cluster_ips = existing.spec.cluster_ips
    core.replace_namespaced_service(
        manifest.name, manifest.namespace, body, field_manager=FIELD_MANAGER
    )
    return AppliedResource("Service", manifest.name, "updated")


def preflight_ownership(clients: KubeClients, manifest: ServiceManifest) -> None:
    """Fail before any write if a same-named object we would write is not ours."""
    _assert_allowed(manifest.namespace)
    targets = [
        ("Deployment", manifest.deployment_name, clients.apps.read_namespaced_deployment),
        ("Service", manifest.name, clients.core.read_namespaced_service),
    ]
    if manifest.spec.service_account_name is not None:
        targets.append(
            (
                "ServiceAccount",
                manifest.spec.service_account_name,
                clients.core.read_namespaced_service_account,
            )
        )
    for kind, name, read in targets:
        try:
            existing = read(name, manifest.namespace)
        except ApiException as exc:
            if exc.status == 404:
                continue
            raise
        _assert_managed(existing, kind, name)


def deploy(
    clients: KubeClients, manifest: ServiceManifest, create_namespace: bool = False
) -> list[AppliedResource]:
    namespace = ensure_namespace(clients.core, manifest.namespace, create_namespace)
    preflight_ownership(clients, manifest)
    applied = [namespace]
    # The ServiceAccount must exist before pods referencing it are created.
    if manifest.spec.service_account_name is not None:
        applied.append(apply_service_account(clients.core, manifest))
    applied.append(apply_deployment(clients.apps, manifest))
    applied.append(apply_service(clients.core, manifest))
    return applied


def find_overlapping_deployments(apps: client.AppsV1Api, manifest: ServiceManifest) -> list[str]:
    """Names of other managed Deployments of this service whose selectors overlap ours.

    Overlapping selectors arise when migrating an unversioned service to versioned
    Deployments; Kubernetes permits them but documents that the controllers may conflict.
    """
    _assert_allowed(manifest.namespace)
    selector = ",".join(f"{k}={v}" for k, v in labels_for(manifest).items())
    ours = pod_labels(manifest).items()
    overlapping = []
    for dep in apps.list_namespaced_deployment(manifest.namespace, label_selector=selector).items:
        if dep.metadata.name == manifest.deployment_name:
            continue
        template_meta = dep.spec.template.metadata
        their_selector = (dep.spec.selector.match_labels or {}).items()
        their_pods = ((template_meta.labels if template_meta else None) or {}).items()
        if their_selector <= ours or ours <= their_pods:
            overlapping.append(dep.metadata.name)
    return sorted(overlapping)


def rollout_complete(deployment: client.V1Deployment) -> bool:
    spec_replicas = deployment.spec.replicas or 0
    status = deployment.status
    if status is None:
        return False
    return (
        (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
        and (status.updated_replicas or 0) == spec_replicas
        and (status.available_replicas or 0) == spec_replicas
        and (status.replicas or 0) == spec_replicas
    )


def wait_for_rollout(
    apps: client.AppsV1Api,
    manifest: ServiceManifest,
    timeout_seconds: float,
    poll_seconds: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> client.V1Deployment:
    _assert_allowed(manifest.namespace)
    deadline = clock() + timeout_seconds
    name = manifest.deployment_name
    while True:
        deployment = apps.read_namespaced_deployment_status(name, manifest.namespace)
        if rollout_complete(deployment):
            return deployment
        if clock() >= deadline:
            status = deployment.status
            raise RolloutTimeout(
                f"rollout of {name!r} not complete after {timeout_seconds:.0f}s "
                f"(updated={status.updated_replicas or 0}, "
                f"available={status.available_replicas or 0}, "
                f"desired={deployment.spec.replicas})"
            )
        sleep(poll_seconds)


@dataclass
class PodStatus:
    name: str
    phase: str
    ready: bool
    restarts: int
    node: str


@dataclass
class ServiceStatus:
    name: str
    namespace: str
    deployed: bool
    deployment: str = ""
    version: str = ""
    service_account: str = ""
    desired: int = 0
    updated: int = 0
    ready: int = 0
    available: int = 0
    image: str = ""
    conditions: list[str] = field(default_factory=list)
    service_type: str = ""
    cluster_ip: str = ""
    pods: list[PodStatus] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.deployed and self.desired > 0 and self.available == self.desired


def _pod_status(pod: client.V1Pod) -> PodStatus:
    statuses = (pod.status.container_statuses or []) if pod.status else []
    return PodStatus(
        name=pod.metadata.name,
        phase=(pod.status.phase if pod.status else None) or "Unknown",
        ready=bool(statuses) and all(s.ready for s in statuses),
        restarts=sum(s.restart_count or 0 for s in statuses),
        node=(pod.spec.node_name if pod.spec else None) or "",
    )


def get_status(clients: KubeClients, manifest: ServiceManifest) -> ServiceStatus:
    _assert_allowed(manifest.namespace)
    name = manifest.deployment_name
    result = ServiceStatus(
        name=manifest.name,
        namespace=manifest.namespace,
        deployed=False,
        deployment=name,
        version=manifest.spec.version or "",
    )
    try:
        dep = clients.apps.read_namespaced_deployment(name, manifest.namespace)
    except ApiException as exc:
        if exc.status == 404:
            return result
        raise
    _assert_managed(dep, "Deployment", name)
    status = dep.status or client.V1DeploymentStatus()
    result.deployed = True
    result.service_account = dep.spec.template.spec.service_account_name or "default"
    result.desired = dep.spec.replicas or 0
    result.updated = status.updated_replicas or 0
    result.ready = status.ready_replicas or 0
    result.available = status.available_replicas or 0
    result.image = dep.spec.template.spec.containers[0].image
    result.conditions = [
        f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
        for c in (status.conditions or [])
    ]
    try:
        svc = clients.core.read_namespaced_service(manifest.name, manifest.namespace)
        result.service_type = svc.spec.type or ""
        result.cluster_ip = svc.spec.cluster_ip or ""
    except ApiException as exc:
        if exc.status != 404:
            raise
    pods = clients.core.list_namespaced_pod(
        manifest.namespace, label_selector=label_selector(manifest)
    )
    result.pods = sorted((_pod_status(p) for p in pods.items), key=lambda p: p.name)
    return result


@dataclass
class PodLogs:
    pod: str
    text: str
    error: str | None = None


def get_logs(core: client.CoreV1Api, manifest: ServiceManifest, tail_lines: int) -> list[PodLogs]:
    _assert_allowed(manifest.namespace)
    if not 1 <= tail_lines <= MAX_LOG_LINES:
        raise DeployError(f"tail must be between 1 and {MAX_LOG_LINES}")
    pods = core.list_namespaced_pod(manifest.namespace, label_selector=label_selector(manifest))
    results = []
    for pod in sorted(pods.items, key=lambda p: p.metadata.name):
        name = pod.metadata.name
        try:
            text = core.read_namespaced_pod_log(
                name, manifest.namespace, container=manifest.name, tail_lines=tail_lines
            )
            results.append(PodLogs(pod=name, text=text or ""))
        except ApiException as exc:
            results.append(PodLogs(pod=name, text="", error=f"{exc.status} {exc.reason}"))
    return results

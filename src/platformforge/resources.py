"""Render a validated ServiceManifest into Kubernetes API objects.

These functions are pure: they never contact a cluster.
"""

from __future__ import annotations

from kubernetes import client

from platformforge.manifest import HttpProbe, ServiceManifest

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "platformforge"
NAME_LABEL = "app.kubernetes.io/name"
# Istio derives the canonical revision (and DestinationRule subsets select) on this label.
VERSION_LABEL = "app.kubernetes.io/version"
PORT_NAME = "http"


def labels_for(manifest: ServiceManifest) -> dict[str, str]:
    """Service-level labels; the Service selector spans every version of the service."""
    return {NAME_LABEL: manifest.name, MANAGED_BY_LABEL: MANAGED_BY_VALUE}


def pod_labels(manifest: ServiceManifest) -> dict[str, str]:
    """Deployment selector and pod labels; versioned services add the version label."""
    labels = labels_for(manifest)
    if manifest.spec.version is not None:
        labels[VERSION_LABEL] = manifest.spec.version
    return labels


def label_selector(manifest: ServiceManifest) -> str:
    return ",".join(f"{key}={value}" for key, value in pod_labels(manifest).items())


def build_namespace(name: str) -> client.V1Namespace:
    return client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                # Opt in to Istio sidecar injection (a no-op if Istio is not installed).
                "istio-injection": "enabled",
            },
        )
    )


def build_service_account(manifest: ServiceManifest) -> client.V1ServiceAccount:
    """Identity-only ServiceAccount: no RBAC bindings and no API token automount."""
    return client.V1ServiceAccount(
        api_version="v1",
        kind="ServiceAccount",
        metadata=client.V1ObjectMeta(
            name=manifest.spec.service_account_name,
            namespace=manifest.namespace,
            labels=labels_for(manifest),
        ),
        automount_service_account_token=False,
    )


def _probe(probe: HttpProbe) -> client.V1Probe:
    return client.V1Probe(
        http_get=client.V1HTTPGetAction(path=probe.path, port=PORT_NAME, scheme="HTTP"),
        initial_delay_seconds=probe.initial_delay_seconds,
        period_seconds=probe.period_seconds,
        timeout_seconds=probe.timeout_seconds,
        failure_threshold=probe.failure_threshold,
    )


def build_deployment(manifest: ServiceManifest) -> client.V1Deployment:
    spec = manifest.spec
    labels = pod_labels(manifest)
    lifecycle = None
    if spec.termination is not None and spec.termination.pre_stop_sleep_seconds > 0:
        # Native sleep handler: the distroless images have no shell for an exec hook.
        lifecycle = client.V1Lifecycle(
            pre_stop=client.V1LifecycleHandler(
                sleep=client.V1SleepAction(seconds=spec.termination.pre_stop_sleep_seconds)
            )
        )
    container = client.V1Container(
        name=manifest.name,
        image=spec.image,
        image_pull_policy="IfNotPresent",
        ports=[client.V1ContainerPort(name=PORT_NAME, container_port=spec.port, protocol="TCP")],
        env=[client.V1EnvVar(name=item.name, value=item.value) for item in spec.env] or None,
        resources=client.V1ResourceRequirements(
            requests={"cpu": spec.resources.requests.cpu, "memory": spec.resources.requests.memory},
            limits={"cpu": spec.resources.limits.cpu, "memory": spec.resources.limits.memory},
        ),
        readiness_probe=_probe(spec.probes.readiness),
        liveness_probe=_probe(spec.probes.liveness),
        lifecycle=lifecycle,
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            run_as_non_root=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        volume_mounts=[client.V1VolumeMount(name="tmp", mount_path="/tmp")],  # noqa: S108
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        service_account_name=spec.service_account_name,
        automount_service_account_token=False,
        termination_grace_period_seconds=(
            spec.termination.grace_period_seconds if spec.termination is not None else None
        ),
        enable_service_links=False,
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        ),
        volumes=[
            client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource(size_limit="64Mi"))
        ],
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=manifest.deployment_name, namespace=manifest.namespace, labels=labels
        ),
        spec=client.V1DeploymentSpec(
            replicas=spec.replicas,
            revision_history_limit=5,
            selector=client.V1LabelSelector(match_labels=labels),
            strategy=client.V1DeploymentStrategy(
                type="RollingUpdate",
                rolling_update=client.V1RollingUpdateDeployment(max_surge=1, max_unavailable=0),
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec
            ),
        ),
    )


def build_service(manifest: ServiceManifest) -> client.V1Service:
    """Build an internal-only ClusterIP Service. Public exposure is never rendered."""
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=manifest.name, namespace=manifest.namespace, labels=labels_for(manifest)
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector=labels_for(manifest),
            ports=[
                client.V1ServicePort(
                    name=PORT_NAME, port=manifest.spec.port, target_port=PORT_NAME, protocol="TCP"
                )
            ],
        ),
    )


def to_dict(obj: object) -> dict:
    """Serialize a Kubernetes client model to a plain dict (camelCase keys)."""
    return client.ApiClient().sanitize_for_serialization(obj)

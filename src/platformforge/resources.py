"""Render a validated ServiceManifest into Kubernetes API objects.

These functions are pure: they never contact a cluster.
"""

from __future__ import annotations

from kubernetes import client

from platformforge.manifest import HttpProbe, ServiceManifest

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "platformforge"
NAME_LABEL = "app.kubernetes.io/name"
PORT_NAME = "http"


def labels_for(manifest: ServiceManifest) -> dict[str, str]:
    return {NAME_LABEL: manifest.name, MANAGED_BY_LABEL: MANAGED_BY_VALUE}


def label_selector(manifest: ServiceManifest) -> str:
    return ",".join(f"{key}={value}" for key, value in labels_for(manifest).items())


def build_namespace(name: str) -> client.V1Namespace:
    return client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
        )
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
    labels = labels_for(manifest)
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
        automount_service_account_token=False,
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
            name=manifest.name, namespace=manifest.namespace, labels=labels
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

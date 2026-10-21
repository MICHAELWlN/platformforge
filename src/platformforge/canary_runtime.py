"""In-cluster reads and reviewed-route writes for the canary controller.

The process identity is the pod ServiceAccount. Kind starts with the kind
context env. GKE also requires PLATFORMFORGE_CANARY_GKE=1. It has no delete method.
"""

from __future__ import annotations

import os

from kubernetes import client, config

from platformforge.canary import CanaryError
from platformforge.canary_apply import (
    apply_reviewed_route,
    controller_context_allowed,
    controller_route,
)
from platformforge.canary_controller import ControllerConfig, parse_weights
from platformforge.kubecontext import KubeClients

_GROUP = ("networking.istio.io", "v1", "virtualservices")
_CANARY_DEPLOYMENT = "inventory-service-v0-2-0"


class ClusterRuntime:
    """Kubernetes calls used by the reconciler. One object, no retry wrapper."""

    def __init__(self, settings: ControllerConfig) -> None:
        controller_context_allowed(settings.context, settings.namespace)
        if os.environ.get("PLATFORMFORGE_CANARY_IN_CLUSTER") != "1":
            raise CanaryError("controller runtime requires PLATFORMFORGE_CANARY_IN_CLUSTER=1")
        config.load_incluster_config()
        api = client.ApiClient()
        self._settings = settings
        self._clients = KubeClients(core=client.CoreV1Api(api), apps=client.AppsV1Api(api))
        self._custom = client.CustomObjectsApi(api)

    def read_weights(self) -> tuple[int, int]:
        group, version, plural = _GROUP
        body = self._custom.get_namespaced_custom_object(
            group, version, self._settings.namespace, plural, "inventory-service"
        )
        if not isinstance(body, dict):
            raise CanaryError("virtualservice response was not a mapping")
        return parse_weights(body)

    def read_ready(self) -> bool:
        deployment = self._clients.apps.read_namespaced_deployment(
            _CANARY_DEPLOYMENT, self._settings.namespace
        )
        status = deployment.status
        ready = 0 if status is None else (status.ready_replicas or 0)
        return ready >= 1

    def apply_action(self, action: str) -> None:
        apply_reviewed_route(
            self._clients,
            context=self._settings.context,
            namespace=self._settings.namespace,
            filename=controller_route(action),
        )

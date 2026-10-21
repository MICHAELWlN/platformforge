"""Apply reviewed inventory routes. Never deletes a Deployment.

Kind experiments accept only kind-platformforge. The pinned GKE context is
accepted only when the process is inside a pod and PLATFORMFORGE_CANARY_GKE=1.
A laptop process cannot set that flag and reach the API token path.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from platformforge.canary import CanaryError
from platformforge.gke import GKE_CONTEXT
from platformforge.kubecontext import KubeClients

KIND_CONTEXT = "kind-platformforge"
NAMESPACE = "platformforge-dev"
_GKE_FLAG = "PLATFORMFORGE_CANARY_GKE"
_SA_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


def _mesh_dir() -> Path:
    override = os.environ.get("PLATFORMFORGE_MESH_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "deploy" / "istio" / "platformforge-dev"


_STABLE = "inventory-virtualservice-stable.yaml"
_CANARY = "inventory-virtualservice-canary.yaml"
_PROMOTE = "inventory-virtualservice-promote.yaml"
_GROUP = ("networking.istio.io", "v1", "virtualservices")
_FILES = frozenset({_STABLE, _CANARY, _PROMOTE})


def require_experiment(context: str, namespace: str) -> None:
    """Refuse every context other than kind and every other namespace."""
    if context != KIND_CONTEXT:
        raise CanaryError(f"canary experiment refuses context {context!r}")
    if namespace != NAMESPACE:
        raise CanaryError(f"canary experiment refuses namespace {namespace!r}")


def controller_context_allowed(context: str, namespace: str) -> None:
    """Allow kind, or the pinned GKE context inside the controller pod."""
    if context == KIND_CONTEXT:
        require_experiment(context, namespace)
        return
    if namespace != NAMESPACE:
        raise CanaryError(f"canary controller refuses namespace {namespace!r}")
    if context != GKE_CONTEXT:
        raise CanaryError(f"canary controller refuses context {context!r}")
    if os.environ.get(_GKE_FLAG) != "1":
        raise CanaryError("GKE canary controller is not enabled")
    if os.environ.get("PLATFORMFORGE_CANARY_IN_CLUSTER") != "1":
        raise CanaryError("GKE canary controller requires in-cluster config")
    if not _SA_TOKEN.is_file():
        raise CanaryError("GKE canary controller requires the pod service account token")


def reviewed_route(stable_weight: int, canary_weight: int) -> str:
    """Return the reviewed file for 100/0, 90/10, or 0/100. Other weights are refused."""
    if (stable_weight, canary_weight) == (100, 0):
        return _STABLE
    if (stable_weight, canary_weight) == (90, 10):
        return _CANARY
    if (stable_weight, canary_weight) == (0, 100):
        return _PROMOTE
    raise CanaryError(f"refusing weights {stable_weight}/{canary_weight}")


def controller_route(action: str) -> str:
    """Map a controller action to a reviewed file. The controller never selects 90/10."""
    if action == "rollback":
        return _STABLE
    if action == "promote":
        return _PROMOTE
    raise CanaryError(f"controller refuses route action {action!r}")


def apply_reviewed_route(
    clients: KubeClients,
    *,
    context: str,
    namespace: str,
    filename: str,
) -> None:
    """Replace the inventory VirtualService from one reviewed file."""
    controller_context_allowed(context, namespace)
    if filename not in _FILES:
        raise CanaryError(f"refusing mesh file {filename!r}")
    body = yaml.safe_load((_mesh_dir() / filename).read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise CanaryError("reviewed route was not a mapping")
    meta = body.get("metadata") or {}
    if meta.get("namespace") != NAMESPACE or meta.get("name") != "inventory-service":
        raise CanaryError("reviewed route does not target inventory-service")
    if "delete" in filename:
        raise CanaryError("canary experiment cannot delete workloads")
    group, version, plural = _GROUP
    api = client.CustomObjectsApi(clients.core.api_client)
    try:
        existing = api.get_namespaced_custom_object(
            group, version, NAMESPACE, plural, "inventory-service"
        )
    except ApiException as exc:
        raise CanaryError(f"virtualservice read failed {exc.status}") from exc
    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    api.replace_namespaced_custom_object(
        group, version, NAMESPACE, plural, "inventory-service", body
    )

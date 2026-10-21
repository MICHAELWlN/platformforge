"""Read-only Kubernetes views. Callers must already check context and namespace."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from kubernetes.client import ApiException

from platformforge.canary import CanaryError
from platformforge.canary_controller import parse_weights
from platformforge.gke import GKE_CONTEXT
from platformforge.kubecontext import KubeClients, load_clients
from platformforge.mcp_server.policy import (
    MAX_LOG_LINES,
    REQUEST_TIMEOUT_SECONDS,
    McpError,
    bound_lines,
    redact,
    refuse_kubernetes_call,
    require_context,
    require_namespace,
    require_since,
    sensitive_env_name,
    untrusted,
)

_TIMEOUT = REQUEST_TIMEOUT_SECONDS
_READER_USER = "platformforge-mcp-reader"
_READER_ENV = "PLATFORMFORGE_MCP_KUBECONFIG"
_READER_CONTEXTS = frozenset({"kind-platformforge", GKE_CONTEXT})


class ReadOnlyApi:
    """Delegate only methods that this server has not classified as mutating or secret."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def __getattr__(self, name: str) -> Any:
        refuse_kubernetes_call(name)
        return getattr(self._api, name)


def clients_for(context: str) -> KubeClients:
    require_context(context)
    return load_clients_checked(context)


def load_clients_checked(context: str) -> KubeClients:
    """Build clients for an allowlisted context. Tests replace this function."""
    return load_clients_for(context)


def load_clients_for(context: str) -> KubeClients:
    from platformforge.kubecontext import KubeContext

    require_context(context)
    path = os.environ.get(_READER_ENV, "").strip()
    if not path:
        if context == GKE_CONTEXT:
            raise McpError(
                "GKE MCP calls require PLATFORMFORGE_MCP_KUBECONFIG for platformforge-mcp-reader"
            )
        # The name is the only field load_clients uses.
        return load_clients(KubeContext(context, "", "", "", ""))
    if context not in _READER_CONTEXTS:
        raise McpError("reader kubeconfig is only accepted for an allowlisted context")
    if _kubeconfig_user(path, context) != _READER_USER:
        raise McpError("MCP kubeconfig user must be platformforge-mcp-reader")
    from kubernetes import client, config

    api = config.new_client_from_config(
        config_file=path,
        context=context,
        persist_config=False,
    )
    return KubeClients(
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        custom=client.CustomObjectsApi(api),
    )


def _kubeconfig_user(path: str, context: str) -> str:
    file = Path(path)
    try:
        loaded = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise McpError("MCP kubeconfig path is not readable") from exc
    if not isinstance(loaded, dict):
        raise McpError("MCP kubeconfig is not a mapping")
    for entry in loaded.get("contexts") or []:
        if not isinstance(entry, dict) or entry.get("name") != context:
            continue
        nested = entry.get("context") or {}
        if isinstance(nested, dict):
            return str(nested.get("user", ""))
    raise McpError("MCP kubeconfig does not define the requested context")


def _invoke(view: ReadOnlyApi, name: str, /, *args: Any, **kwargs: Any) -> Any:
    return call_read(getattr(view, name), *args, **kwargs)


def list_pods(core: Any, namespace: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    view = ReadOnlyApi(core)
    response = _invoke(view, "list_namespaced_pod", namespace, _request_timeout=_TIMEOUT)
    pods = [_pod_summary(pod) for pod in response.items or []]
    return {"namespace": namespace, "pods": pods[:50], "truncated": len(pods) > 50}


def describe_pod(core: Any, namespace: str, pod: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    _require_object_name(pod)
    view = ReadOnlyApi(core)
    body = _invoke(view, "read_namespaced_pod", pod, namespace, _request_timeout=_TIMEOUT)
    events = _invoke(
        view,
        "list_namespaced_event",
        namespace,
        field_selector=f"involvedObject.name={pod}",
        _request_timeout=_TIMEOUT,
    )
    return {
        "pod": _pod_detail(body),
        "events": [_event_summary(item) for item in (events.items or [])[:30]],
    }


def get_pod_logs(
    core: Any,
    namespace: str,
    pod: str,
    *,
    container: str | None = None,
    tail_lines: int = 50,
    since_seconds: int | None = None,
) -> dict[str, object]:
    namespace = require_namespace(namespace)
    _require_object_name(pod)
    if container is not None:
        _require_object_name(container)
    tail_lines = int(tail_lines)
    if not 1 <= tail_lines <= MAX_LOG_LINES:
        raise McpError(f"line limit must be from 1 to {MAX_LOG_LINES}")
    since_seconds = require_since(since_seconds)
    view = ReadOnlyApi(core)
    kwargs: dict[str, object] = {
        "tail_lines": tail_lines,
        "_request_timeout": _TIMEOUT,
    }
    if container is not None:
        kwargs["container"] = container
    if since_seconds is not None:
        kwargs["since_seconds"] = since_seconds
    text = _invoke(view, "read_namespaced_pod_log", pod, namespace, **kwargs) or ""
    return {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "logs": untrusted(bound_lines(str(text), tail_lines)),
    }


def list_deployments(apps: Any, namespace: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    view = ReadOnlyApi(apps)
    response = _invoke(view, "list_namespaced_deployment", namespace, _request_timeout=_TIMEOUT)
    rows = [_deployment_summary(item) for item in response.items or []]
    return {"namespace": namespace, "deployments": rows[:50]}


def get_deployment_status(apps: Any, namespace: str, name: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    _require_object_name(name)
    view = ReadOnlyApi(apps)
    body = _invoke(view, "read_namespaced_deployment", name, namespace, _request_timeout=_TIMEOUT)
    summary = _deployment_summary(body)
    summary["conditions"] = _conditions(body)
    summary["images"] = _images(body)
    return summary


def list_events(core: Any, namespace: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    view = ReadOnlyApi(core)
    response = _invoke(view, "list_namespaced_event", namespace, _request_timeout=_TIMEOUT)
    rows = [_event_summary(item) for item in (response.items or [])[:30]]
    return {"namespace": namespace, "events": rows}


def get_service_status(core: Any, namespace: str, name: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    _require_object_name(name)
    view = ReadOnlyApi(core)
    body = _invoke(view, "read_namespaced_service", name, namespace, _request_timeout=_TIMEOUT)
    endpoints = _invoke(
        view, "read_namespaced_endpoints", name, namespace, _request_timeout=_TIMEOUT
    )
    service_type = str(getattr(body.spec, "type", "") or "")
    return {
        "namespace": namespace,
        "name": name,
        "type": service_type,
        "cluster_ip": str(getattr(body.spec, "cluster_ip", "") or ""),
        "ports": _ports(body),
        "ready_addresses": _ready_addresses(endpoints),
        "public": service_type in {"LoadBalancer", "NodePort"},
        "note": "Call the Service DNS name. A pod IP bypasses STRICT mTLS.",
    }


_ROUTE_NAME = "inventory-service"
_ROUTE_GROUP = ("networking.istio.io", "v1", "virtualservices")


def get_routing(custom: Any, namespace: str) -> dict[str, object]:
    """Read the inventory VirtualService. The only API method used is get."""
    namespace = require_namespace(namespace)
    if namespace != "platformforge-dev":
        raise McpError("inventory routing is only readable in platformforge-dev")
    view = ReadOnlyApi(custom)
    group, version, plural = _ROUTE_GROUP
    body = _invoke(
        view,
        "get_namespaced_custom_object",
        group,
        version,
        namespace,
        plural,
        _ROUTE_NAME,
        _request_timeout=_TIMEOUT,
    )
    if not isinstance(body, dict):
        raise McpError("virtualservice response was not a mapping")
    try:
        stable, canary = parse_weights(body)
    except CanaryError as exc:
        raise McpError("virtualservice shape is not the reviewed inventory route") from exc
    return {
        "namespace": namespace,
        "name": _ROUTE_NAME,
        "stable_subset": "v0-1-0",
        "canary_subset": "v0-2-0",
        "stable_weight": stable,
        "canary_weight": canary,
        "destinations": [
            {"subset": "v0-1-0", "weight": stable, "role": "stable"},
            {"subset": "v0-2-0", "weight": canary, "role": "canary"},
        ],
        "writable": False,
    }


def deployments_for_service(apps: Any, namespace: str, service: str) -> list[dict[str, object]]:
    listed = list_deployments(apps, namespace)
    rows = listed["deployments"]
    if not isinstance(rows, list):
        raise McpError("deployment list was not a list")
    return [
        row
        for row in rows
        if isinstance(row, dict) and _matches_service(str(row.get("name", "")), service)
    ]


def pods_for_service(core: Any, namespace: str, service: str) -> dict[str, object]:
    namespace = require_namespace(namespace)
    view = ReadOnlyApi(core)
    response = _invoke(
        view,
        "list_namespaced_pod",
        namespace,
        label_selector=f"app.kubernetes.io/name={service}",
        _request_timeout=_TIMEOUT,
    )
    pods = [_pod_summary(pod) for pod in response.items or []]
    return {"namespace": namespace, "pods": pods[:50]}


def call_read(action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return action(*args, **kwargs)
    except ApiException as exc:
        raise McpError(f"kubernetes API {exc.status} {exc.reason}") from exc
    except McpError:
        raise
    except Exception as exc:
        raise McpError(redact(type(exc).__name__)) from exc


def _require_object_name(name: str) -> None:
    if not name or "/" in name or len(name) > 253:
        raise McpError(f"object name {name!r} is not accepted")


def _matches_service(deployment: str, service: str) -> bool:
    return deployment == service or deployment.startswith(f"{service}-")


def _pod_summary(pod: Any) -> dict[str, object]:
    statuses = _container_statuses(pod)
    ready = sum(1 for item in statuses if getattr(item, "ready", False))
    return {
        "name": _meta_name(pod),
        "namespace": _meta_namespace(pod),
        "phase": str(getattr(pod.status, "phase", "") or ""),
        "ready_containers": ready,
        "containers": len(statuses),
        "restarts": sum(int(getattr(item, "restart_count", 0) or 0) for item in statuses),
        "node": str(getattr(pod.spec, "node_name", "") or ""),
    }


def _pod_detail(pod: Any) -> dict[str, object]:
    summary = _pod_summary(pod)
    summary["containers"] = [_container_detail(item) for item in _spec_containers(pod)]
    summary["container_states"] = [_container_state(item) for item in _container_statuses(pod)]
    return summary


def _container_detail(container: Any) -> dict[str, object]:
    resources = getattr(container, "resources", None)
    return {
        "name": str(getattr(container, "name", "")),
        "image": str(getattr(container, "image", "")),
        "requests": _resource_map(getattr(resources, "requests", None)),
        "limits": _resource_map(getattr(resources, "limits", None)),
        "env": [_env_row(item) for item in (getattr(container, "env", None) or [])],
    }


def _env_row(item: Any) -> dict[str, object]:
    name = str(getattr(item, "name", ""))
    value_from = getattr(item, "value_from", None)
    if value_from is not None or sensitive_env_name(name):
        return {"name": name, "value": "[redacted]"}
    return {"name": name, "value": redact(str(getattr(item, "value", "") or ""))}


def _container_state(status: Any) -> dict[str, object]:
    state = getattr(status, "state", None)
    waiting = getattr(state, "waiting", None)
    terminated = getattr(state, "terminated", None)
    running = getattr(state, "running", None)
    if waiting is not None:
        kind, reason = "waiting", str(getattr(waiting, "reason", "") or "")
    elif terminated is not None:
        kind, reason = "terminated", str(getattr(terminated, "reason", "") or "")
    elif running is not None:
        kind, reason = "running", ""
    else:
        kind, reason = "unknown", ""
    return {
        "name": str(getattr(status, "name", "")),
        "ready": bool(getattr(status, "ready", False)),
        "restart_count": int(getattr(status, "restart_count", 0) or 0),
        "state": kind,
        "reason": reason,
    }


def _deployment_summary(body: Any) -> dict[str, object]:
    status = body.status
    desired = int(getattr(body.spec, "replicas", 0) or 0)
    available = int(getattr(status, "available_replicas", 0) or 0)
    updated = int(getattr(status, "updated_replicas", 0) or 0)
    return {
        "name": _meta_name(body),
        "namespace": _meta_namespace(body),
        "desired": desired,
        "available": available,
        "updated": updated,
        "ready": available >= desired and desired > 0,
    }


def _conditions(body: Any) -> list[dict[str, str]]:
    rows = []
    for item in getattr(body.status, "conditions", None) or []:
        rows.append(
            {
                "type": str(getattr(item, "type", "")),
                "status": str(getattr(item, "status", "")),
                "reason": str(getattr(item, "reason", "") or ""),
            }
        )
    return rows


def _images(body: Any) -> list[dict[str, object]]:
    rows = []
    for container in _spec_containers(getattr(body.spec, "template", None)):
        image = str(getattr(container, "image", ""))
        digest = ""
        if "@sha256:" in image:
            digest = image.split("@", 1)[1]
        rows.append(
            {
                "name": str(getattr(container, "name", "")),
                "image": image,
                "digest": digest,
                "immutable": bool(digest),
            }
        )
    return rows


def _ports(body: Any) -> list[dict[str, object]]:
    rows = []
    for port in getattr(body.spec, "ports", None) or []:
        rows.append(
            {
                "name": str(getattr(port, "name", "") or ""),
                "port": int(getattr(port, "port", 0) or 0),
                "protocol": str(getattr(port, "protocol", "") or ""),
            }
        )
    return rows


def _ready_addresses(endpoints: Any) -> int:
    total = 0
    for subset in getattr(endpoints, "subsets", None) or []:
        total += len(getattr(subset, "addresses", None) or [])
    return total


def _event_summary(item: Any) -> dict[str, object]:
    message = str(getattr(item, "message", "") or "")
    return {
        "type": str(getattr(item, "type", "") or ""),
        "reason": str(getattr(item, "reason", "") or ""),
        "count": int(getattr(item, "count", 0) or 0),
        "involved": str(getattr(getattr(item, "involved_object", None), "name", "") or ""),
        "message": untrusted(message),
    }


def _resource_map(value: Any) -> dict[str, str]:
    if not value:
        return {}
    return {str(key): str(item) for key, item in dict(value).items()}


def _container_statuses(pod: Any) -> list[Any]:
    """App containers plus native sidecars. Completed init containers are omitted."""
    native = _native_sidecar_names(pod)
    statuses = [
        item
        for item in (getattr(pod.status, "init_container_statuses", None) or [])
        if str(getattr(item, "name", "")) in native
    ]
    statuses.extend(getattr(pod.status, "container_statuses", None) or [])
    return statuses


def _spec_containers(owner: Any) -> list[Any]:
    spec = getattr(owner, "spec", owner)
    return _native_sidecar_specs(spec) + list(getattr(spec, "containers", None) or [])


def _native_sidecar_specs(spec: Any) -> list[Any]:
    rows = []
    for container in getattr(spec, "init_containers", None) or []:
        if str(getattr(container, "restart_policy", "") or "") == "Always":
            rows.append(container)
    return rows


def _native_sidecar_names(pod: Any) -> set[str]:
    return {str(getattr(item, "name", "")) for item in _native_sidecar_specs(pod.spec)}


def _meta_name(body: Any) -> str:
    return str(getattr(body.metadata, "name", "") or "")


def _meta_namespace(body: Any) -> str:
    return str(getattr(body.metadata, "namespace", "") or "")

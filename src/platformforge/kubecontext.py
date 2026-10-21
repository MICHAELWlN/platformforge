"""Kubernetes context discovery, display, and approval.

The current context is read directly from kubeconfig YAML so it can be shown to the user
*before* any credentials are loaded or exec credential plugins (e.g. gke-gcloud-auth-plugin)
are run. API clients are then built for the approved context name only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from kubernetes import client, config


class ContextError(Exception):
    """Raised when the kube context cannot be determined or is not approved."""


@dataclass(frozen=True)
class KubeContext:
    name: str
    cluster: str
    user: str
    server: str
    kubeconfig: str


@dataclass(frozen=True)
class KubeClients:
    core: client.CoreV1Api
    apps: client.AppsV1Api
    custom: client.CustomObjectsApi | None = None


def _kubeconfig_paths() -> list[Path]:
    env = os.environ.get("KUBECONFIG")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    return [Path("~/.kube/config").expanduser()]


def current_context() -> KubeContext:
    """Return the active context using kubeconfig merge order (first file wins)."""
    docs: list[tuple[Path, dict]] = []
    for path in _kubeconfig_paths():
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                docs.append((path, data))
    if not docs:
        raise ContextError("no kubeconfig found (checked KUBECONFIG and ~/.kube/config)")

    current = next((d.get("current-context") for _, d in docs if d.get("current-context")), None)
    if not current:
        raise ContextError("kubeconfig has no current-context set")

    def find(section: str, name: str) -> tuple[Path, dict] | None:
        for path, data in docs:
            for entry in data.get(section) or []:
                if isinstance(entry, dict) and entry.get("name") == name:
                    return path, entry
        return None

    found = find("contexts", current)
    if found is None:
        raise ContextError(f"current-context {current!r} is not defined in kubeconfig")
    path, ctx_entry = found
    ctx = ctx_entry.get("context") or {}
    cluster_name = str(ctx.get("cluster", ""))
    cluster_found = find("clusters", cluster_name)
    server = ""
    if cluster_found is not None:
        server = str((cluster_found[1].get("cluster") or {}).get("server", ""))
    return KubeContext(
        name=str(current),
        cluster=cluster_name,
        user=str(ctx.get("user", "")),
        server=server or "<unknown>",
        kubeconfig=str(path),
    )


def describe(ctx: KubeContext) -> str:
    return (
        "Kubernetes context:\n"
        f"  context:    {ctx.name}\n"
        f"  cluster:    {ctx.cluster}\n"
        f"  server:     {ctx.server}\n"
        f"  user:       {ctx.user}\n"
        f"  kubeconfig: {ctx.kubeconfig}"
    )


def require_approval(
    ctx: KubeContext,
    approve_context: str | None,
    confirm: Callable[[str], bool],
) -> None:
    """Require explicit approval to use ``ctx``.

    ``approve_context`` is a non-interactive approval that must exactly match the current
    context name; otherwise ``confirm`` is called to prompt the user. After approval,
    only ``kind-*`` contexts may proceed. GKE inspection and later installs go through
    ``deploy/gke`` with an explicit context, not through this CLI.
    """
    if approve_context is not None:
        if approve_context != ctx.name:
            raise ContextError(
                f"--approve-context {approve_context!r} does not match current context "
                f"{ctx.name!r}; refusing to continue"
            )
    elif not confirm(f"Use Kubernetes context {ctx.name!r}?"):
        raise ContextError("kube context not approved; no cluster calls were made")
    if not ctx.name.startswith("kind-"):
        raise ContextError(
            f"context {ctx.name!r} is not a kind cluster; deploy, status, and logs "
            "refuse every non-kind context. GKE is prepared under deploy/gke and is "
            "not applied by this CLI."
        )


def load_clients(ctx: KubeContext) -> KubeClients:
    """Build API clients pinned to the approved context name."""
    api_client = config.new_client_from_config(context=ctx.name)
    return KubeClients(core=client.CoreV1Api(api_client), apps=client.AppsV1Api(api_client))

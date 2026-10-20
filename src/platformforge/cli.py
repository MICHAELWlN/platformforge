"""PlatformForge developer CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from platformforge import __version__, deployer, kubecontext
from platformforge.manifest import ManifestError, ServiceManifest, load_manifest
from platformforge.resources import build_deployment, build_service, to_dict

EXIT_INVALID_MANIFEST = 1
EXIT_CONTEXT_NOT_APPROVED = 3
EXIT_KUBERNETES_ERROR = 4
EXIT_ROLLOUT_TIMEOUT = 5

app = typer.Typer(
    help="PlatformForge: deploy and inspect services on the PlatformForge platform.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

ManifestArg = Annotated[
    Path, typer.Argument(help="Path to a PlatformForge ServiceManifest YAML file.")
]
ApproveContextOpt = Annotated[
    str | None,
    typer.Option(
        "--approve-context",
        help="Approve the current kube context non-interactively. Must exactly match the "
        "current context name, otherwise the command aborts.",
    ),
]


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=code)


def _load(path: Path) -> ServiceManifest:
    try:
        return load_manifest(path)
    except ManifestError as exc:
        raise _fail(str(exc), EXIT_INVALID_MANIFEST) from exc


def _connect(approve_context: str | None) -> kubecontext.KubeClients:
    try:
        ctx = kubecontext.current_context()
        typer.echo(kubecontext.describe(ctx), err=True)
        kubecontext.require_approval(
            ctx, approve_context, confirm=lambda msg: typer.confirm(msg, default=False, err=True)
        )
        return kubecontext.load_clients(ctx)
    except (kubecontext.ContextError, ConfigException) as exc:
        raise _fail(str(exc), EXIT_CONTEXT_NOT_APPROVED) from exc


def _api_error(exc: ApiException) -> typer.Exit:
    return _fail(f"Kubernetes API error: {exc.status} {exc.reason}", EXIT_KUBERNETES_ERROR)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"platformforge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """PlatformForge developer CLI."""


@app.command()
def validate(manifest: ManifestArg) -> None:
    """Validate a service manifest without contacting any cluster."""
    m = _load(manifest)
    typer.secho(
        f"OK: {m.name} (namespace={m.namespace}, image={m.spec.image}, "
        f"replicas={m.spec.replicas}, port={m.spec.port})",
        fg=typer.colors.GREEN,
    )


@app.command()
def deploy(
    manifest: ManifestArg,
    approve_context: ApproveContextOpt = None,
    create_namespace: Annotated[
        bool, typer.Option(help="Create the allowlisted namespace if it does not exist.")
    ] = False,
    wait: Annotated[bool, typer.Option(help="Wait for the rollout to complete.")] = True,
    timeout: Annotated[
        int, typer.Option(min=10, max=900, help="Rollout wait timeout in seconds.")
    ] = 120,
    dry_run: Annotated[
        bool,
        typer.Option(help="Print the rendered resources and exit without contacting a cluster."),
    ] = False,
) -> None:
    """Deploy a service into its allowlisted namespace."""
    m = _load(manifest)
    if dry_run:
        docs = [to_dict(build_deployment(m)), to_dict(build_service(m))]
        typer.echo(yaml.safe_dump_all(docs, sort_keys=False), nl=False)
        return

    clients = _connect(approve_context)
    try:
        applied = deployer.deploy(clients, m, create_namespace=create_namespace)
        for item in applied:
            typer.echo(f"{item.kind}/{item.name}: {item.action}")
        if wait:
            typer.echo(f"Waiting up to {timeout}s for rollout of {m.name}...")
            dep = deployer.wait_for_rollout(clients.apps, m, timeout_seconds=timeout)
            typer.secho(
                f"Rollout complete: {dep.status.available_replicas}/{dep.spec.replicas} available",
                fg=typer.colors.GREEN,
            )
    except deployer.RolloutTimeout as exc:
        raise _fail(str(exc), EXIT_ROLLOUT_TIMEOUT) from exc
    except deployer.DeployError as exc:
        raise _fail(str(exc), EXIT_KUBERNETES_ERROR) from exc
    except ApiException as exc:
        raise _api_error(exc) from exc


@app.command()
def status(manifest: ManifestArg, approve_context: ApproveContextOpt = None) -> None:
    """Show Deployment, Service, and Pod status for a service."""
    m = _load(manifest)
    clients = _connect(approve_context)
    try:
        st = deployer.get_status(clients, m)
    except deployer.DeployError as exc:
        raise _fail(str(exc), EXIT_KUBERNETES_ERROR) from exc
    except ApiException as exc:
        raise _api_error(exc) from exc

    if not st.deployed:
        typer.echo(f"{st.name}: not deployed in namespace {st.namespace}")
        raise typer.Exit(code=EXIT_KUBERNETES_ERROR)
    typer.echo(f"Service:    {st.name} (namespace {st.namespace})")
    typer.echo(f"Image:      {st.image}")
    typer.echo(
        f"Replicas:   desired={st.desired} updated={st.updated} "
        f"ready={st.ready} available={st.available}"
    )
    typer.echo(f"Conditions: {', '.join(st.conditions) or '-'}")
    typer.echo(f"Endpoint:   {st.service_type or '-'} {st.cluster_ip or '-'}")
    typer.echo("Pods:")
    for pod in st.pods:
        typer.echo(
            f"  {pod.name}  phase={pod.phase} ready={pod.ready} "
            f"restarts={pod.restarts} node={pod.node or '-'}"
        )
    health = "HEALTHY" if st.healthy else "DEGRADED"
    typer.secho(
        f"Health:     {health}", fg=typer.colors.GREEN if st.healthy else typer.colors.YELLOW
    )


@app.command()
def logs(
    manifest: ManifestArg,
    approve_context: ApproveContextOpt = None,
    tail: Annotated[
        int, typer.Option(min=1, max=deployer.MAX_LOG_LINES, help="Lines per pod.")
    ] = 100,
) -> None:
    """Print recent logs from every pod of a service."""
    m = _load(manifest)
    clients = _connect(approve_context)
    try:
        results = deployer.get_logs(clients.core, m, tail_lines=tail)
    except deployer.DeployError as exc:
        raise _fail(str(exc), EXIT_KUBERNETES_ERROR) from exc
    except ApiException as exc:
        raise _api_error(exc) from exc
    if not results:
        typer.echo(f"No pods found for {m.name} in namespace {m.namespace}")
        raise typer.Exit(code=EXIT_KUBERNETES_ERROR)
    for item in results:
        typer.secho(f"==> {item.pod} <==", bold=True)
        if item.error:
            typer.secho(f"(logs unavailable: {item.error})", fg=typer.colors.YELLOW)
        else:
            typer.echo(item.text, nl=not item.text.endswith("\n"))

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from platformforge import cli, deployer, kubecontext
from platformforge.kubecontext import KubeContext
from tests.conftest import EXAMPLE_MANIFEST

runner = CliRunner()
CTX = KubeContext("kind-test", "kind-test", "kind-test", "https://127.0.0.1:6443", "/x")


@pytest.fixture
def fake_cluster(monkeypatch):
    """Stub context discovery and client loading; record whether clients were built."""
    state = {"loaded": False, "clients": MagicMock()}
    monkeypatch.setattr(kubecontext, "current_context", lambda: CTX)

    def load(ctx):
        assert ctx is CTX
        state["loaded"] = True
        return state["clients"]

    monkeypatch.setattr(kubecontext, "load_clients", load)
    return state


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "deploy", "status", "logs"):
        assert command in result.output


def test_validate_ok():
    result = runner.invoke(cli.app, ["validate", str(EXAMPLE_MANIFEST)])
    assert result.exit_code == 0, result.output
    assert "OK: demo-web" in result.output


def test_validate_invalid(write_manifest, manifest_dict):
    manifest_dict["metadata"]["namespace"] = "kube-system"
    result = runner.invoke(cli.app, ["validate", str(write_manifest(manifest_dict))])
    assert result.exit_code == cli.EXIT_INVALID_MANIFEST
    assert "not allowlisted" in result.output


def test_dry_run_renders_without_cluster(fake_cluster):
    result = runner.invoke(cli.app, ["deploy", str(EXAMPLE_MANIFEST), "--dry-run"])
    assert result.exit_code == 0, result.output
    docs = list(yaml.safe_load_all(result.output))
    assert [d["kind"] for d in docs] == ["Deployment", "Service"]
    assert fake_cluster["loaded"] is False


def test_invalid_manifest_never_contacts_cluster(fake_cluster, write_manifest, manifest_dict):
    manifest_dict["metadata"]["namespace"] = "default"
    path = str(write_manifest(manifest_dict))
    for args in (["deploy", path], ["status", path], ["logs", path]):
        result = runner.invoke(cli.app, [*args, "--approve-context", "kind-test"])
        assert result.exit_code == cli.EXIT_INVALID_MANIFEST
    assert fake_cluster["loaded"] is False


@pytest.mark.parametrize("command", ["deploy", "status", "logs"])
def test_context_declined_aborts(fake_cluster, command):
    result = runner.invoke(cli.app, [command, str(EXAMPLE_MANIFEST)], input="n\n")
    assert result.exit_code == cli.EXIT_CONTEXT_NOT_APPROVED
    assert "context:    kind-test" in result.output
    assert "server:     https://127.0.0.1:6443" in result.output
    assert fake_cluster["loaded"] is False


@pytest.mark.parametrize("command", ["deploy", "status", "logs"])
def test_context_flag_mismatch_aborts(fake_cluster, command):
    result = runner.invoke(
        cli.app, [command, str(EXAMPLE_MANIFEST), "--approve-context", "gke-prod"]
    )
    assert result.exit_code == cli.EXIT_CONTEXT_NOT_APPROVED
    assert "does not match" in result.output
    assert fake_cluster["loaded"] is False


def test_deploy_interactive_approval(fake_cluster, monkeypatch):
    calls = {}

    def fake_deploy(clients, m, create_namespace):
        calls["create_namespace"] = create_namespace
        return [deployer.AppliedResource("Deployment", m.name, "created")]

    monkeypatch.setattr(deployer, "deploy", fake_deploy)
    result = runner.invoke(
        cli.app, ["deploy", str(EXAMPLE_MANIFEST), "--no-wait", "--create-namespace"], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert calls["create_namespace"] is True
    assert "Deployment/demo-web: created" in result.output


def test_deploy_ownership_error_exit_code(fake_cluster, monkeypatch):
    def boom(*_, **__):
        raise deployer.OwnershipError("not ours")

    monkeypatch.setattr(deployer, "deploy", boom)
    result = runner.invoke(
        cli.app, ["deploy", str(EXAMPLE_MANIFEST), "--approve-context", "kind-test"]
    )
    assert result.exit_code == cli.EXIT_KUBERNETES_ERROR
    assert "not ours" in result.output


def test_deploy_timeout_exit_code(fake_cluster, monkeypatch):
    monkeypatch.setattr(deployer, "deploy", lambda *a, **k: [])

    def timeout(*_, **__):
        raise deployer.RolloutTimeout("slow")

    monkeypatch.setattr(deployer, "wait_for_rollout", timeout)
    result = runner.invoke(
        cli.app, ["deploy", str(EXAMPLE_MANIFEST), "--approve-context", "kind-test"]
    )
    assert result.exit_code == cli.EXIT_ROLLOUT_TIMEOUT


def test_logs_tail_bounds_enforced_by_cli(fake_cluster):
    result = runner.invoke(
        cli.app,
        ["logs", str(EXAMPLE_MANIFEST), "--approve-context", "kind-test", "--tail", "999999"],
    )
    assert result.exit_code == 2
    assert fake_cluster["loaded"] is False


def test_status_not_deployed(fake_cluster, monkeypatch):
    monkeypatch.setattr(
        deployer,
        "get_status",
        lambda clients, m: deployer.ServiceStatus(m.name, m.namespace, deployed=False),
    )
    result = runner.invoke(
        cli.app, ["status", str(EXAMPLE_MANIFEST), "--approve-context", "kind-test"]
    )
    assert result.exit_code == cli.EXIT_KUBERNETES_ERROR
    assert "not deployed" in result.output

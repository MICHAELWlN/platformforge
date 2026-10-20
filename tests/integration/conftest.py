"""Shared safety gate for integration tests against a local kind cluster.

Opt in by setting PLATFORMFORGE_IT_CONTEXT to the kind context name, e.g.

    PLATFORMFORGE_IT_CONTEXT=kind-platformforge pytest -m integration

Safety rules enforced here:
  * the context must start with ``kind-`` and must equal the current kube context;
  * only the allowlisted ``platformforge-dev`` namespace is written to;
  * nothing is deleted. Resources are left in place for inspection (see README for cleanup).
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from platformforge import cli, kubecontext

IT_CONTEXT = os.environ.get("PLATFORMFORGE_IT_CONTEXT")
NAMESPACE = "platformforge-dev"
runner = CliRunner()


@pytest.fixture(scope="session")
def kube() -> kubecontext.KubeClients:
    if not IT_CONTEXT:
        pytest.skip("set PLATFORMFORGE_IT_CONTEXT=kind-<name> to run integration tests")
    if not IT_CONTEXT.startswith("kind-"):
        pytest.fail(f"refusing to run integration tests against non-kind context {IT_CONTEXT!r}")
    ctx = kubecontext.current_context()
    if ctx.name != IT_CONTEXT:
        pytest.fail(f"current context is {ctx.name!r}, expected {IT_CONTEXT!r}")
    return kubecontext.load_clients(ctx)


def run_cli(*args: str):
    """Invoke the platformforge CLI with non-interactive approval of the IT context."""
    return runner.invoke(cli.app, [*args, "--approve-context", IT_CONTEXT])

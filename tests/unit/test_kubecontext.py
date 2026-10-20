from __future__ import annotations

import pytest
import yaml

from platformforge import kubecontext
from platformforge.kubecontext import ContextError, KubeContext


def _kubeconfig(path, current="kind-test", server="https://127.0.0.1:6443"):
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": current,
                "contexts": [
                    {"name": "kind-test", "context": {"cluster": "kind-test", "user": "kind-test"}},
                    {"name": "prod", "context": {"cluster": "prod", "user": "admin"}},
                ],
                "clusters": [
                    {"name": "kind-test", "cluster": {"server": server}},
                    {"name": "prod", "cluster": {"server": "https://prod.example"}},
                ],
                "users": [
                    # An exec plugin must never be run just to display the context.
                    {"name": "admin", "user": {"exec": {"command": "/bin/false"}}},
                ],
            }
        )
    )
    return path


def test_current_context_reads_kubeconfig(tmp_path, monkeypatch):
    cfg = _kubeconfig(tmp_path / "config")
    monkeypatch.setenv("KUBECONFIG", str(cfg))
    ctx = kubecontext.current_context()
    assert ctx == KubeContext(
        name="kind-test",
        cluster="kind-test",
        user="kind-test",
        server="https://127.0.0.1:6443",
        kubeconfig=str(cfg),
    )
    assert "https://127.0.0.1:6443" in kubecontext.describe(ctx)


def test_current_context_merges_multiple_files(tmp_path, monkeypatch):
    first = tmp_path / "a"
    first.write_text(yaml.safe_dump({"current-context": "prod"}))
    second = _kubeconfig(tmp_path / "b", current="kind-test")
    monkeypatch.setenv("KUBECONFIG", f"{first}:{second}")
    ctx = kubecontext.current_context()
    assert ctx.name == "prod"
    assert ctx.server == "https://prod.example"


def test_missing_kubeconfig(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing"))
    with pytest.raises(ContextError, match="no kubeconfig"):
        kubecontext.current_context()


def test_undefined_current_context(tmp_path, monkeypatch):
    cfg = _kubeconfig(tmp_path / "config", current="ghost")
    monkeypatch.setenv("KUBECONFIG", str(cfg))
    with pytest.raises(ContextError, match="not defined"):
        kubecontext.current_context()


CTX = KubeContext("kind-test", "kind-test", "kind-test", "https://127.0.0.1:6443", "/x")


def test_approval_flag_must_match():
    with pytest.raises(ContextError, match="does not match"):
        kubecontext.require_approval(CTX, "prod", confirm=lambda _: True)


def test_approval_flag_match_skips_prompt():
    def confirm(_):
        raise AssertionError("should not prompt")

    kubecontext.require_approval(CTX, "kind-test", confirm=confirm)


def test_interactive_approval_declined():
    with pytest.raises(ContextError, match="not approved"):
        kubecontext.require_approval(CTX, None, confirm=lambda _: False)


def test_interactive_approval_accepted():
    prompts = []
    kubecontext.require_approval(CTX, None, confirm=lambda m: prompts.append(m) or True)
    assert prompts == ["Use Kubernetes context 'kind-test'?"]

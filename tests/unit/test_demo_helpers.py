"""Static checks for the demo helpers. They do not contact a cluster."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "scripts" / "demo"
MCP = REPO / ".cursor" / "mcp.example.json"


def _text(name: str) -> str:
    return (DEMO / name).read_text(encoding="utf-8")


def test_mutating_helpers_require_confirmation() -> None:
    assert 'CONFIRM_FAILURE:-}" != "yes"' in _text("fail-canary.sh")
    assert 'CONFIRM_RESTORE:-}" != "yes"' in _text("restore-healthy.sh")
    assert 'CONFIRM_TRAFFIC:-}" != "yes"' in _text("traffic.sh")
    for name in ("fail-canary.sh", "restore-healthy.sh"):
        text = _text(name)
        assert "kubectl delete" not in text
        assert "inventory-service-v0-1-0" not in text
        assert "LoadBalancer" in text


def test_read_only_helpers_do_not_apply() -> None:
    for name in (
        "preflight.sh",
        "port-forwards.sh",
        "connect.sh",
        "stop-forwards.sh",
        "controller-ready.sh",
        "collect-evidence.sh",
        "reader-token.sh",
        "mcp-server.sh",
    ):
        text = _text(name)
        assert "kubectl apply" not in text
        assert "kubectl delete" not in text
        assert "terraform" not in text
        assert "LoadBalancer" not in text or name == "preflight.sh"


def test_port_forwards_are_loopback() -> None:
    text = _text("port-forwards.sh")
    assert "--address 127.0.0.1" in text
    every_interface = "0.0.0.0"  # noqa: S104
    assert every_interface not in text


def test_cursor_mcp_config_is_stdio_and_has_no_token() -> None:
    raw = MCP.read_text(encoding="utf-8")
    config = json.loads(raw)
    server = config["mcpServers"]["platformforge"]
    assert server["command"] == "scripts/demo/mcp-server.sh"
    assert server["args"] == []
    assert "url" not in server
    env = server["env"]
    reader_kubeconfig = "/tmp/pf-mcp-reader/kubeconfig"  # noqa: S108
    assert env["PLATFORMFORGE_MCP_KUBECONFIG"] == reader_kubeconfig
    assert env["PLATFORMFORGE_PROMETHEUS_URL"] == "http://127.0.0.1:9090"
    assert env["PLATFORMFORGE_LOKI_URL"] == "http://127.0.0.1:3100"
    assert env["PLATFORMFORGE_TEMPO_URL"] == "http://127.0.0.1:3200"
    assert "PLATFORMFORGE_MCP_HTTP" not in env
    lowered = raw.lower()
    assert "/users/" not in lowered
    assert "/home/" not in lowered
    assert "eyj" not in lowered
    assert "bearer " not in lowered
    assert "begin private" not in lowered

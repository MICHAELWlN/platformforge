"""Supervisor decisions with fake processes. These tests do not contact a cluster."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts" / "demo" / "connect_supervisor.py"


def _load():
    spec = importlib.util.spec_from_file_location("connect_supervisor", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


supervisor = _load()


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def die(self) -> None:
        self.code = 1


def _harness(healthy: dict[str, bool] | None = None):
    module = supervisor
    health = {"prometheus": True, "loki": True, "tempo": True, "grafana": True}
    if healthy:
        health.update(healthy)
    launched: list[str] = []
    stopped: list[int] = []
    listeners: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    procs: list[FakeProc] = []
    clock = {"now": 1_000.0}

    def launch(spec):
        proc = FakeProc(5000 + len(procs))
        procs.append(proc)
        launched.append(spec.service)
        return proc

    def stop_pid(pid: int) -> None:
        stopped.append(pid)
        for port, pids in listeners.items():
            if pid in pids:
                listeners[port] = [item for item in pids if item != pid]
        for proc in procs:
            if proc.pid == pid:
                proc.die()

    box = module.Supervisor(
        launch=launch,
        healthy=lambda spec: health[spec.service],
        listeners=lambda port: list(listeners.get(port, [])),
        command=lambda pid: commands.get(pid, ""),
        stop_pid=stop_pid,
        now=lambda: clock["now"],
    )
    return box, health, launched, stopped, listeners, commands, procs, clock


def test_initial_tick_starts_each_forward_once() -> None:
    box, _health, launched, _stopped, _listeners, _commands, _procs, clock = _harness()
    box.tick()
    box.tick()
    clock["now"] += 10
    box.tick()
    assert launched == ["prometheus", "loki", "tempo", "grafana"]
    assert [slot.restarts for slot in box.slots] == [0, 0, 0, 0]
    assert [slot.status for slot in box.slots] == ["connected"] * 4


def test_exited_forward_restarts_only_itself_after_backoff() -> None:
    box, _health, launched, _stopped, _listeners, _commands, procs, clock = _harness()
    box.tick()
    procs[3].die()
    box.tick()
    assert launched == ["prometheus", "loki", "tempo", "grafana"]
    assert box.slots[3].status == "restarting"
    clock["now"] += supervisor.MIN_BACKOFF_SECONDS
    box.tick()
    assert launched == ["prometheus", "loki", "tempo", "grafana", "grafana"]
    assert box.slots[3].restarts == 1
    assert box.slots[0].restarts == 0


def test_backoff_is_bounded() -> None:
    delay = supervisor.MIN_BACKOFF_SECONDS
    for _ in range(10):
        delay = supervisor.next_backoff(delay)
    assert delay == supervisor.MAX_BACKOFF_SECONDS


def test_unhealthy_live_process_restarts_after_three_failures() -> None:
    box, health, launched, stopped, _listeners, _commands, procs, clock = _harness()
    box.tick()
    clock["now"] += supervisor.GRACE_SECONDS
    health["loki"] = False
    box.tick()
    box.tick()
    assert launched.count("loki") == 1
    box.tick()
    assert procs[1].pid in stopped
    assert box.slots[1].status == "restarting"
    clock["now"] += supervisor.MIN_BACKOFF_SECONDS
    health["loki"] = True
    box.tick()
    box.tick()
    assert box.slots[1].status == "connected"
    assert box.slots[1].restarts == 1
    assert box.slots[1].last_recovery_seconds == supervisor.MIN_BACKOFF_SECONDS


def test_foreign_listener_is_not_killed() -> None:
    box, _health, launched, stopped, listeners, commands, _procs, _clock = _harness()
    listeners[3000] = [42]
    commands[42] = "python -m http.server 3000"
    box.tick()
    assert 42 not in stopped
    assert "grafana" not in launched
    assert box.slots[3].status == "blocked"
    assert launched == ["prometheus", "loki", "tempo"]


def test_stale_owned_forward_is_replaced() -> None:
    box, _health, launched, stopped, listeners, commands, _procs, _clock = _harness()
    listeners[3000] = [77]
    commands[77] = "kubectl --context gke port-forward --address 127.0.0.1,::1 svc/grafana 3000:80"
    box.tick()
    assert stopped == [77]
    assert launched[-1] == "grafana"


def test_addresses_stay_on_loopback() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    every_interface = "0.0.0.0"  # noqa: S104
    assert every_interface not in text
    assert "shell=True" not in text
    assert "kubectl apply" not in text
    grafana = supervisor.FORWARDS[-1]
    assert grafana.service == "grafana"
    assert grafana.addresses == ("127.0.0.1", "::1")
    assert "http://localhost:3000/api/health" in grafana.health_urls
    for spec in supervisor.FORWARDS[:-1]:
        assert spec.addresses == ("127.0.0.1",)
    for spec in supervisor.FORWARDS:
        argv = supervisor.argv_for(spec)
        assert argv[0] == "kubectl"
        assert "--address" in argv
        assert "LoadBalancer" not in argv


def test_status_table_has_the_recording_fields() -> None:
    box, _health, _launched, _stopped, _listeners, _commands, _procs, clock = _harness()
    box.tick()
    clock["now"] += supervisor.GRACE_SECONDS
    box.tick()
    rendered = supervisor.format_status(box.snapshot())
    assert "service      port  status      restarts  last_success" in rendered
    assert "grafana      3000  connected   0" in rendered
    assert "prometheus   9090  connected   0" in rendered


def test_stop_marks_slots_and_signals_children() -> None:
    box, _health, _launched, stopped, _listeners, _commands, procs, _clock = _harness()
    box.tick()
    box.stop()
    assert [slot.status for slot in box.slots] == ["stopped"] * 4
    assert stopped == [proc.pid for proc in procs]

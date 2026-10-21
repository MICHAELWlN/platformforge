#!/usr/bin/env python3
"""Loopback port-forward supervisor for the PlatformForge recording session.

Watches four local forwards and restarts only the one that exited or failed
its HTTP check. It does not change Service types or any other cluster object.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

CONTEXT = "gke_platformforge-dev-mn2026_us-central1-a_platformforge-dev"
NAMESPACE = "platformforge-observability"
LOOPBACK = frozenset({"127.0.0.1", "::1"})
RUN_DIR = Path("/tmp/pf-demo-connect")  # noqa: S108
STATE_PATH = RUN_DIR / "state.json"
PID_PATH = RUN_DIR / "supervisor.pid"
LOCK_PATH = RUN_DIR / "supervisor.lock"
LOG_PATH = RUN_DIR / "supervisor.log"

MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
GRACE_SECONDS = 5.0
FAILS_BEFORE_RESTART = 3
TICK_SECONDS = 1.0
HEALTH_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ForwardSpec:
    service: str
    local_port: int
    kube_service: str
    remote_port: int
    addresses: tuple[str, ...]
    health_urls: tuple[str, ...]


FORWARDS: tuple[ForwardSpec, ...] = (
    ForwardSpec(
        "prometheus",
        9090,
        "prometheus-server",
        80,
        ("127.0.0.1",),
        ("http://127.0.0.1:9090/-/ready",),
    ),
    ForwardSpec(
        "loki",
        3100,
        "loki",
        3100,
        ("127.0.0.1",),
        ("http://127.0.0.1:3100/ready",),
    ),
    ForwardSpec(
        "tempo",
        3200,
        "tempo",
        3200,
        ("127.0.0.1",),
        ("http://127.0.0.1:3200/ready",),
    ),
    ForwardSpec(
        "grafana",
        3000,
        "grafana",
        80,
        ("127.0.0.1", "::1"),
        (
            "http://127.0.0.1:3000/api/health",
            "http://[::1]:3000/api/health",
            "http://localhost:3000/api/health",
        ),
    ),
)


def argv_for(spec: ForwardSpec) -> list[str]:
    """Build a kubectl port-forward argument list. Addresses are loopback only."""
    for address in spec.addresses:
        if address not in LOOPBACK:
            raise ValueError(f"refusing non-loopback address {address}")
    return [
        "kubectl",
        "--context",
        CONTEXT,
        "--namespace",
        NAMESPACE,
        "port-forward",
        "--address",
        ",".join(spec.addresses),
        f"svc/{spec.kube_service}",
        f"{spec.local_port}:{spec.remote_port}",
    ]


def next_backoff(current: float) -> float:
    return min(max(current, MIN_BACKOFF_SECONDS) * 2, MAX_BACKOFF_SECONDS)


def is_owned_forward(command: str, spec: ForwardSpec) -> bool:
    return (
        "port-forward" in command
        and f"svc/{spec.kube_service}" in command
        and f"{spec.local_port}:" in command
    )


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...


@dataclass
class Slot:
    spec: ForwardSpec
    proc: Process | None = None
    restarts: int = 0
    ever_started: bool = False
    last_success: float | None = None
    status: str = "starting"
    started_at: float | None = None
    failures: int = 0
    next_attempt: float = 0.0
    backoff: float = MIN_BACKOFF_SECONDS
    recovery_started: float | None = None
    last_recovery_seconds: float | None = None


@dataclass
class Supervisor:
    """One tick reconciles every forward. Tests supply the side-effect hooks."""

    launch: Launch
    healthy: Health
    listeners: Listeners
    command: CommandLookup
    stop_pid: StopPid
    now: Clock
    log: Log = field(default=lambda _message: None)
    slots: list[Slot] = field(default_factory=list)
    running: bool = True

    def __post_init__(self) -> None:
        if not self.slots:
            self.slots = [Slot(spec) for spec in FORWARDS]

    def tick(self) -> None:
        moment = self.now()
        for slot in self.slots:
            self._reconcile(slot, moment)

    def _reconcile(self, slot: Slot, moment: float) -> None:
        proc = slot.proc
        if proc is not None and proc.poll() is None:
            self._check_live(slot, moment)
            return
        if proc is not None:
            slot.proc = None
            slot.recovery_started = slot.recovery_started or moment
            slot.next_attempt = moment + slot.backoff
            slot.backoff = next_backoff(slot.backoff)
            slot.status = "restarting"
            delay = slot.next_attempt - moment
            self.log(f"{slot.spec.service} exited; next attempt in {delay:.0f}s")
            return
        if moment < slot.next_attempt:
            slot.status = "restarting"
            return
        if self._port_blocked(slot):
            return
        self._start(slot, moment)

    def _check_live(self, slot: Slot, moment: float) -> None:
        in_grace = slot.started_at is not None and moment - slot.started_at < GRACE_SECONDS
        if self.healthy(slot.spec):
            slot.failures = 0
            slot.backoff = MIN_BACKOFF_SECONDS
            slot.last_success = moment
            slot.status = "connected"
            if slot.recovery_started is not None:
                slot.last_recovery_seconds = moment - slot.recovery_started
                self.log(
                    f"{slot.spec.service} recovered in {slot.last_recovery_seconds:.1f}s "
                    f"after {slot.restarts} restarts"
                )
                slot.recovery_started = None
            return
        if in_grace:
            slot.status = "starting"
            return
        slot.failures += 1
        slot.status = "unhealthy"
        if slot.failures < FAILS_BEFORE_RESTART:
            return
        self.log(f"{slot.spec.service} failed {slot.failures} health checks; restarting")
        self._stop_slot(slot)
        slot.proc = None
        slot.recovery_started = slot.recovery_started or moment
        slot.next_attempt = moment + slot.backoff
        slot.backoff = next_backoff(slot.backoff)
        slot.failures = 0
        slot.status = "restarting"

    def _port_blocked(self, slot: Slot) -> bool:
        foreign = False
        for pid in self.listeners(slot.spec.local_port):
            if slot.proc is not None and pid == slot.proc.pid:
                continue
            command = self.command(pid)
            if is_owned_forward(command, slot.spec):
                self.log(f"stopping stale {slot.spec.service} port-forward pid {pid}")
                self.stop_pid(pid)
            else:
                foreign = True
        if foreign:
            slot.status = "blocked"
            self.log(f"{slot.spec.service} port {slot.spec.local_port} is held by another process")
            return True
        return bool(self.listeners(slot.spec.local_port))

    def _start(self, slot: Slot, moment: float) -> None:
        if slot.ever_started:
            slot.restarts += 1
            slot.recovery_started = slot.recovery_started or moment
        slot.ever_started = True
        slot.proc = self.launch(slot.spec)
        slot.started_at = moment
        slot.failures = 0
        slot.status = "starting"
        self.log(f"started {slot.spec.service} pid {slot.proc.pid} restarts {slot.restarts}")

    def _stop_slot(self, slot: Slot) -> None:
        proc = slot.proc
        if proc is None or proc.poll() is not None:
            return
        self.stop_pid(proc.pid)

    def stop(self) -> None:
        self.running = False
        for slot in self.slots:
            self._stop_slot(slot)
            slot.proc = None
            slot.status = "stopped"

    def snapshot(self, moment: float | None = None) -> dict[str, object]:
        stamp = self.now() if moment is None else moment
        return {
            "supervisor_pid": os.getpid(),
            "updated": _iso(stamp),
            "services": [
                {
                    "service": slot.spec.service,
                    "port": slot.spec.local_port,
                    "bind": list(slot.spec.addresses),
                    "status": slot.status,
                    "restarts": slot.restarts,
                    "last_success": _iso(slot.last_success) if slot.last_success else None,
                    "last_recovery_seconds": slot.last_recovery_seconds,
                    "pid": None if slot.proc is None else slot.proc.pid,
                }
                for slot in self.slots
            ],
        }


class Launch(Protocol):
    def __call__(self, spec: ForwardSpec) -> Process: ...


class Health(Protocol):
    def __call__(self, spec: ForwardSpec) -> bool: ...


class Listeners(Protocol):
    def __call__(self, port: int) -> list[int]: ...


class CommandLookup(Protocol):
    def __call__(self, pid: int) -> str: ...


class StopPid(Protocol):
    def __call__(self, pid: int) -> None: ...


class Clock(Protocol):
    def __call__(self) -> float: ...


class Log(Protocol):
    def __call__(self, message: str) -> None: ...


def format_status(state: dict[str, object]) -> str:
    rows = state.get("services")
    if not isinstance(rows, list):
        return "supervisor state is empty"
    lines = ["service      port  status      restarts  last_success"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        last = row.get("last_success") or "never"
        lines.append(
            f"{str(row.get('service', '')):<12} "
            f"{int(row.get('port', 0)):<5} "
            f"{str(row.get('status', '')):<11} "
            f"{int(row.get('restarts', 0)):<9} "
            f"{last}"
        )
    return "\n".join(lines)


def _iso(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def _loopback_http(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def http_ready(spec: ForwardSpec) -> bool:
    for url in spec.health_urls:
        if not _loopback_http(url):
            return False
        request = urllib.request.Request(url)  # noqa: S310
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=HEALTH_TIMEOUT_SECONDS
            ) as response:
                if response.status != 200:
                    return False
        except (OSError, urllib.error.URLError):
            return False
    return True


def listener_pids(port: int) -> list[int]:
    if port not in {spec.local_port for spec in FORWARDS}:
        raise ValueError(f"refusing to inspect port {port}")
    completed = subprocess.run(  # noqa: S603
        ["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [int(line) for line in completed.stdout.split() if line.strip()]


def process_command(pid: int) -> str:
    completed = subprocess.run(  # noqa: S603
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def stop_process(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def current_context() -> str:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl is not on PATH")
    completed = subprocess.run(  # noqa: S603
        [kubectl, "config", "current-context"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "kubectl config current-context failed")
    return completed.stdout.strip()


def require_context() -> None:
    current = current_context()
    print(f"CURRENT={current}")
    if current != CONTEXT:
        raise SystemExit(f"refusing context {current}")


class _PopenProcess:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()


def launch_forward(spec: ForwardSpec) -> Process:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl is not on PATH")
    argv = argv_for(spec)
    argv[0] = kubectl
    log_path = RUN_DIR / f"{spec.service}.log"
    log_file = log_path.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()
    return _PopenProcess(proc)


def _prepare_dir() -> None:
    RUN_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    command = process_command(pid)
    return "connect_supervisor.py" in command


def write_state(state: dict[str, object]) -> None:
    _prepare_dir()
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def read_state() -> dict[str, object] | None:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def run_loop() -> None:
    _prepare_dir()
    lock = LOCK_PATH.open("a")
    try:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("supervisor already running")
        return
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    supervisor = Supervisor(
        launch=launch_forward,
        healthy=http_ready,
        listeners=listener_pids,
        command=process_command,
        stop_pid=stop_process,
        now=time.time,
        log=_log,
    )

    def _shutdown(signum: int, _frame: object) -> None:
        _log(f"shutdown signal {signum}")
        supervisor.running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _log(f"supervisor {os.getpid()} started")
    while supervisor.running:
        supervisor.tick()
        write_state(supervisor.snapshot())
        slept = 0.0
        while supervisor.running and slept < TICK_SECONDS:
            time.sleep(0.2)
            slept += 0.2
    supervisor.stop()
    write_state(supervisor.snapshot())
    _log("supervisor stopped")
    PID_PATH.unlink(missing_ok=True)


def _log(message: str) -> None:
    line = f"{_iso(time.time())} {message}\n"
    _prepare_dir()
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _daemonize() -> None:
    read_fd, write_fd = os.pipe()
    if os.fork() > 0:
        os.close(write_fd)
        message = os.read(read_fd, 128).decode()
        os.close(read_fd)
        print(message, end="")
        raise SystemExit(0)
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os.close(write_fd)
        os._exit(0)
    os.chdir(RUN_DIR)
    with LOG_PATH.open("ab") as log_file:
        os.dup2(log_file.fileno(), 1)
        os.dup2(log_file.fileno(), 2)
    os.write(write_fd, f"supervisor started pid {os.getpid()}\n".encode())
    os.close(write_fd)
    sys.stdin.close()
    run_loop()
    os._exit(0)


def cmd_start() -> int:
    require_context()
    _prepare_dir()
    existing = _read_pid()
    if _pid_alive(existing):
        print(f"supervisor already running pid {existing}")
        return 0
    _daemonize()
    return 0


def cmd_status() -> int:
    pid = _read_pid()
    alive = _pid_alive(pid)
    state = read_state()
    if not alive:
        print("supervisor stopped")
        if state is not None:
            print(format_status(state))
        return 1
    if state is None:
        print(f"supervisor running pid {pid}")
        print("state not written yet")
        return 1
    print(f"supervisor running pid {pid}")
    print(format_status(state))
    failed = False
    services = state.get("services")
    if isinstance(services, list):
        by_name = {spec.service: spec for spec in FORWARDS}
        for row in services:
            if not isinstance(row, dict):
                continue
            spec = by_name.get(str(row.get("service")))
            if spec is None:
                continue
            live = "ok" if http_ready(spec) else "fail"
            if live == "fail":
                failed = True
            recovery = row.get("last_recovery_seconds")
            recovery_text = "" if recovery is None else f" recovery_s={recovery}"
            print(f"probe {spec.service} {live}{recovery_text}")
    return 1 if failed else 0


def cmd_stop() -> int:
    pid = _read_pid()
    if pid is not None and _pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.2)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    for spec in FORWARDS:
        for listener in listener_pids(spec.local_port):
            if is_owned_forward(process_command(listener), spec):
                stop_process(listener)
                print(f"stopped {spec.service} pid {listener}")
    PID_PATH.unlink(missing_ok=True)
    print("supervisor stopped")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"start", "status", "stop"}:
        print("usage: connect_supervisor.py start|status|stop", file=sys.stderr)
        return 2
    command = argv[1]
    if command == "start":
        return cmd_start()
    if command == "status":
        return cmd_status()
    return cmd_stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

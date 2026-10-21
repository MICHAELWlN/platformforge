"""Bounded canary reconciler. One decision per poll. Fail closed.

The process serves /metrics on a private address and queries only an allowlisted
Prometheus URL. It applies the reviewed stable or promote VirtualService.
It never selects the 90/10 file and never deletes a Deployment.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from platformforge.canary import (
    Analysis,
    AnalysisThresholds,
    CanaryAction,
    CanaryError,
    CanaryMetrics,
    RolloutState,
    VersionSample,
    analyze,
    counter_latency_query,
    counter_requests_query,
    counters_went_backwards,
    delta_payload,
    http_get,
    query_prometheus,
    require_prometheus_url,
    samples_from_prometheus,
)
from platformforge.canary_apply import (
    controller_context_allowed,
    controller_route,
)

# Pod-network bind. The Service is ClusterIP and the manifest sets no hostPort.
_POD_METRICS_HOST = "0.0.0.0"  # noqa: S104

Sleep = Callable[[float], None]
Clock = Callable[[], float]
Query = Callable[[str], dict]


@dataclass(frozen=True)
class ControllerConfig:
    """Limits for one running controller. Values outside these bounds are refused."""

    context: str
    namespace: str
    prometheus_url: str
    poll_interval_seconds: float = 15
    window_seconds: int = 60
    max_episode_seconds: float = 180
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8080
    service: str = "inventory-service"
    stable_version: str = "v0.1.0"
    canary_version: str = "v0.2.0"
    min_requests: int = 10
    max_error_ratio: float = 0.05
    max_p95_regression: float = 5.0
    min_p95_delta_ms: float = 200
    request_timeout_seconds: float = 5

    def __post_init__(self) -> None:
        controller_context_allowed(self.context, self.namespace)
        require_prometheus_url(self.prometheus_url)
        if not 5 <= self.poll_interval_seconds <= 60:
            raise CanaryError("poll interval must be from 5 to 60 seconds")
        if not 15 <= self.window_seconds <= 900:
            raise CanaryError("observation window must be from 15 to 900 seconds")
        if self.max_episode_seconds < self.window_seconds or self.max_episode_seconds > 900:
            raise CanaryError("max episode must cover the window and be at most 900 seconds")
        if self.metrics_host not in {"127.0.0.1", _POD_METRICS_HOST}:
            raise CanaryError("metrics host must be 127.0.0.1 or the pod address 0.0.0.0")
        if not 1024 <= self.metrics_port <= 65535:
            raise CanaryError("metrics port must be from 1024 to 65535")
        AnalysisThresholds(
            max_error_ratio=self.max_error_ratio,
            max_p95_regression=self.max_p95_regression,
            min_p95_delta_ms=self.min_p95_delta_ms,
            min_requests=self.min_requests,
            evaluation_seconds=self.window_seconds,
        )

    @property
    def thresholds(self) -> AnalysisThresholds:
        return AnalysisThresholds(
            max_error_ratio=self.max_error_ratio,
            max_p95_regression=self.max_p95_regression,
            min_p95_delta_ms=self.min_p95_delta_ms,
            min_requests=self.min_requests,
            evaluation_seconds=self.window_seconds,
        )

    @property
    def max_ticks(self) -> int:
        """Upper bound on polls in one episode. The loop also watches the clock."""
        return int(self.max_episode_seconds // self.poll_interval_seconds) + 1


@dataclass(frozen=True)
class Tick:
    state: RolloutState
    analysis: Analysis
    route_action: str | None
    episode_age: float


def parse_weights(body: dict) -> tuple[int, int]:
    """Read stable and canary weights. Any other shape is refused by the caller."""
    try:
        routes = body["spec"]["http"][0]["route"]
        found = {route["destination"]["subset"]: int(route["weight"]) for route in routes}
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("virtualservice shape is not the reviewed inventory route") from exc
    if set(found) != {"v0-1-0", "v0-2-0"}:
        raise CanaryError("virtualservice subsets are not the reviewed inventory pair")
    return found["v0-1-0"], found["v0-2-0"]


def decide_tick(
    *,
    weights: tuple[int, int] | None,
    analysis: Analysis | None,
    episode_age: float,
    config: ControllerConfig,
    ready: bool,
    timed_out: bool,
) -> Tick:
    """Choose the next route action without performing I/O."""
    if weights == (100, 0):
        return Tick(
            RolloutState.STABLE,
            _idle(CanaryAction.HOLD, "route is already stable"),
            None,
            episode_age,
        )
    if weights not in {(90, 10), (0, 100)}:
        reason = "route could not be read" if weights is None else "unreviewed weights"
        if weights is None and episode_age < config.max_episode_seconds:
            return Tick(
                RolloutState.ANALYZING,
                _idle(CanaryAction.HOLD, reason),
                None,
                episode_age,
            )
        return Tick(
            RolloutState.ROLLING_BACK,
            _idle(CanaryAction.ROLLBACK, reason),
            "rollback",
            episode_age,
        )
    if analysis is None:
        analysis = _compare(
            config,
            observation_seconds=int(min(episode_age, config.window_seconds)),
            ready=ready,
            timed_out=timed_out,
            stable=_absent(config.stable_version),
            canary=_absent(config.canary_version),
        )
    if analysis.action is CanaryAction.HOLD and episode_age >= config.max_episode_seconds:
        analysis = Analysis(
            CanaryAction.ROLLBACK,
            "episode exceeded while the decision was hold",
            analysis.stable,
            analysis.canary,
            analysis.error_ratio,
            analysis.p95_regression,
            analysis.request_ids,
            analysis.trace_ids,
        )
    if analysis.action is CanaryAction.ROLLBACK:
        return Tick(RolloutState.ROLLING_BACK, analysis, "rollback", episode_age)
    if analysis.action is CanaryAction.PROMOTE and weights == (90, 10):
        return Tick(RolloutState.PROMOTING, analysis, "promote", episode_age)
    if analysis.action is CanaryAction.PROMOTE:
        staying = Analysis(
            CanaryAction.HOLD,
            "promoted route remains",
            analysis.stable,
            analysis.canary,
            analysis.error_ratio,
            analysis.p95_regression,
        )
        return Tick(RolloutState.STABLE, staying, None, episode_age)
    return Tick(RolloutState.ANALYZING, analysis, None, episode_age)


def read_counters(config: ControllerConfig, query: Query) -> tuple[dict, dict] | None:
    """Read current counters once. A failure returns None and does not retry."""
    try:
        requests = query(counter_requests_query(config.service))
        latency = query(counter_latency_query(config.service))
    except (CanaryError, TimeoutError, urllib.error.URLError):
        return None
    if not isinstance(requests, dict) or not isinstance(latency, dict):
        return None
    return requests, latency


def analysis_from_delta(
    config: ControllerConfig,
    *,
    observation_seconds: int,
    ready: bool,
    current: tuple[dict, dict],
    baseline: tuple[dict, dict],
) -> Analysis:
    """Compare the counter increase since the episode baseline."""
    requests, latency = current
    base_requests, base_latency = baseline
    stable, canary = samples_from_prometheus(
        delta_payload(requests, base_requests),
        delta_payload(latency, base_latency),
        stable_version=config.stable_version,
        canary_version=config.canary_version,
    )
    return _compare(
        config,
        observation_seconds=observation_seconds,
        ready=ready,
        timed_out=False,
        stable=stable,
        canary=canary,
    )


def run_loop(
    config: ControllerConfig,
    *,
    metrics: CanaryMetrics,
    stop: threading.Event,
    read_weights: Callable[[], tuple[int, int] | None],
    read_ready: Callable[[], bool],
    apply_action: Callable[[str], None],
    query: Query,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> RolloutState:
    """Poll until stop is set. Each iteration sleeps once. Shutdown rolls back an open canary."""
    state = RolloutState.BASELINE
    episode_started: float | None = None
    ticks = 0
    last_weights: tuple[int, int] | None = None
    baseline: tuple[dict, dict] | None = None
    while not stop.is_set():
        now = clock()
        if episode_started is None:
            episode_started = now
        age = now - episode_started
        if ticks >= config.max_ticks and last_weights not in {None, (100, 0)}:
            age = config.max_episode_seconds
        weights = _read_or_none(read_weights)
        last_weights = weights
        ready = True
        if weights != (100, 0):
            ready = _ready_or_false(read_ready)
        observation = int(min(max(age, 0), config.window_seconds))
        analysis, timed_out = (None, False)
        if weights in {(90, 10), (0, 100)}:
            current = read_counters(config, query)
            if current is None:
                timed_out = True
            elif baseline is None:
                baseline = current
                analysis = _idle(CanaryAction.HOLD, "baseline recorded")
            elif counters_went_backwards(current[0], baseline[0]) or counters_went_backwards(
                current[1], baseline[1]
            ):
                baseline = current
                analysis = _idle(CanaryAction.HOLD, "counter reset, baseline refreshed")
            else:
                analysis = analysis_from_delta(
                    config,
                    observation_seconds=observation,
                    ready=ready,
                    current=current,
                    baseline=baseline,
                )
        tick = decide_tick(
            weights=weights,
            analysis=analysis,
            episode_age=age,
            config=config,
            ready=ready,
            timed_out=timed_out,
        )
        state = _apply_tick(tick, metrics, age, apply_action)
        ticks += 1
        audit(
            "decision",
            state=state.value,
            action=tick.analysis.action.value,
            reason=tick.analysis.reason,
            error_ratio=tick.analysis.error_ratio,
            p95_regression=tick.analysis.p95_regression,
            canary_total=tick.analysis.canary.total,
            canary_errors=tick.analysis.canary.errors,
            stable_p95_ms=tick.analysis.stable.p95_ms,
            canary_p95_ms=tick.analysis.canary.p95_ms,
            route_action=tick.route_action,
            episode_age=round(age, 3),
        )
        if weights == (100, 0):
            episode_started = None
            ticks = 0
            baseline = None
        if stop.is_set():
            break
        _wait(stop, config.poll_interval_seconds, sleep)
    if stop.is_set() and last_weights not in {None, (100, 0)}:
        state = _shutdown_rollback(metrics, apply_action)
    return state


def serve_metrics(
    metrics: CanaryMetrics, host: str, port: int
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Serve GET /metrics. The host must be loopback or the in-pod wildcard."""
    if host not in {"127.0.0.1", _POD_METRICS_HOST}:
        raise CanaryError("metrics host must be 127.0.0.1 or the pod address 0.0.0.0")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="canary-metrics", daemon=True)
    thread.start()
    return server, thread


def config_from_env() -> ControllerConfig:
    """Build a config from the process environment. Missing context fails closed."""
    return ControllerConfig(
        context=os.environ.get("PLATFORMFORGE_CANARY_CONTEXT", ""),
        namespace=os.environ.get("PLATFORMFORGE_CANARY_NAMESPACE", ""),
        prometheus_url=os.environ.get("PLATFORMFORGE_PROMETHEUS_URL", ""),
        poll_interval_seconds=float(os.environ.get("PLATFORMFORGE_CANARY_POLL_SECONDS", "15")),
        window_seconds=int(os.environ.get("PLATFORMFORGE_CANARY_WINDOW_SECONDS", "60")),
        max_episode_seconds=float(
            os.environ.get("PLATFORMFORGE_CANARY_MAX_EPISODE_SECONDS", "180")
        ),
        metrics_host=os.environ.get("PLATFORMFORGE_CANARY_METRICS_HOST", "127.0.0.1"),
        metrics_port=int(os.environ.get("PLATFORMFORGE_CANARY_METRICS_PORT", "8080")),
        min_requests=int(os.environ.get("PLATFORMFORGE_CANARY_MIN_REQUESTS", "10")),
        max_error_ratio=float(os.environ.get("PLATFORMFORGE_CANARY_MAX_ERROR_RATIO", "0.05")),
        max_p95_regression=float(os.environ.get("PLATFORMFORGE_CANARY_MAX_P95_REGRESSION", "5")),
        min_p95_delta_ms=float(os.environ.get("PLATFORMFORGE_CANARY_MIN_P95_DELTA_MS", "200")),
    )


def main() -> None:
    """Run in a pod. Kind is the default. GKE requires the explicit in-cluster gate."""
    config = config_from_env()
    controller_context_allowed(config.context, config.namespace)
    metrics = CanaryMetrics()
    server, _thread = serve_metrics(metrics, config.metrics_host, config.metrics_port)
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        from platformforge.canary_runtime import ClusterRuntime

        runtime = ClusterRuntime(config)
        run_loop(
            config,
            metrics=metrics,
            stop=stop,
            read_weights=runtime.read_weights,
            read_ready=runtime.read_ready,
            apply_action=runtime.apply_action,
            query=lambda promql: query_prometheus(
                config.prometheus_url,
                promql,
                timeout=config.request_timeout_seconds,
                fetch=http_get,
            ),
        )
    finally:
        server.shutdown()
        audit("shutdown", state=metrics.state.value)


def audit(event: str, **fields: object) -> None:
    """One JSON object per line. Values are numbers, short strings, or null."""
    print(json.dumps({"event": event, **fields}, sort_keys=True, default=_plain), flush=True)


def _apply_tick(
    tick: Tick,
    metrics: CanaryMetrics,
    age: float,
    apply_action: Callable[[str], None],
) -> RolloutState:
    nxt = tick.state
    if tick.route_action is not None:
        try:
            apply_action(tick.route_action)
        except (CanaryError, OSError) as exc:
            audit("route_failed", action=tick.route_action, error=type(exc).__name__)
            metrics.record(tick.analysis, age, RolloutState.FAILED)
            return RolloutState.FAILED
        nxt = RolloutState.STABLE
    metrics.record(tick.analysis, age, nxt)
    return nxt


def _shutdown_rollback(
    metrics: CanaryMetrics,
    apply_action: Callable[[str], None],
) -> RolloutState:
    try:
        apply_action("rollback")
    except (CanaryError, OSError) as exc:
        audit("shutdown_rollback_failed", error=type(exc).__name__)
        metrics.state = RolloutState.FAILED
        return RolloutState.FAILED
    audit("shutdown_rollback", file=controller_route("rollback"))
    metrics.state = RolloutState.STABLE
    return RolloutState.STABLE


def _compare(
    config: ControllerConfig,
    *,
    observation_seconds: int,
    ready: bool,
    timed_out: bool,
    stable: VersionSample,
    canary: VersionSample,
) -> Analysis:
    return analyze(
        stable=stable,
        canary=canary,
        thresholds=config.thresholds,
        observation_seconds=observation_seconds,
        ready=ready,
        timed_out=timed_out,
    )


def _idle(action: CanaryAction, reason: str) -> Analysis:
    return Analysis(action, reason, _absent("v0.1.0"), _absent("v0.2.0"), None, None)


def _absent(version: str) -> VersionSample:
    return VersionSample(version, 0.0, 0.0, None, False)


def _read_or_none(read_weights: Callable[[], tuple[int, int] | None]) -> tuple[int, int] | None:
    try:
        return read_weights()
    except (CanaryError, OSError):
        return None


def _ready_or_false(read_ready: Callable[[], bool]) -> bool:
    try:
        return read_ready()
    except (CanaryError, OSError):
        return False


def _wait(stop: threading.Event, seconds: float, sleep: Sleep) -> None:
    deadline = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < deadline:
        sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _plain(value: object) -> str:
    return str(value)


if __name__ == "__main__":
    main()

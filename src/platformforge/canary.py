"""Compare stable and canary Prometheus samples. This module does not call a cluster.

Decisions use the samples a caller collected. An empty sample is missing
telemetry, not a healthy zero. The rollout state machine never deletes a workload.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from platformforge.delivery import CanaryAction, DeliveryError

Fetch = Callable[[str, float], bytes]


class CanaryError(DeliveryError):
    """Raised when a canary sample or transition is not usable."""


class RolloutState(Enum):
    BASELINE = "baseline"
    CANARY_10 = "canary_10"
    ANALYZING = "analyzing"
    PROMOTING = "promoting"
    ROLLING_BACK = "rolling_back"
    STABLE = "stable"
    FAILED = "failed"


_TRANSITIONS: dict[tuple[RolloutState, str], RolloutState] = {
    (RolloutState.BASELINE, "start"): RolloutState.CANARY_10,
    (RolloutState.CANARY_10, "analyze"): RolloutState.ANALYZING,
    (RolloutState.ANALYZING, "hold"): RolloutState.ANALYZING,
    (RolloutState.ANALYZING, "promote"): RolloutState.PROMOTING,
    (RolloutState.ANALYZING, "rollback"): RolloutState.ROLLING_BACK,
    (RolloutState.PROMOTING, "confirm"): RolloutState.STABLE,
    (RolloutState.ROLLING_BACK, "confirm"): RolloutState.STABLE,
    (RolloutState.ROLLING_BACK, "fail"): RolloutState.FAILED,
    (RolloutState.STABLE, "rollback"): RolloutState.STABLE,
    (RolloutState.STABLE, "start"): RolloutState.CANARY_10,
    (RolloutState.FAILED, "rollback"): RolloutState.ROLLING_BACK,
}


@dataclass(frozen=True)
class AnalysisThresholds:
    """Limits for one comparison of stable and canary. Ratios are 0 to 1."""

    max_error_ratio: float = 0.05
    max_p95_regression: float = 0.50
    min_p95_delta_ms: float = 0
    min_requests: int = 50
    evaluation_seconds: int = 60

    def __post_init__(self) -> None:
        if not 0 <= self.max_error_ratio <= 1:
            raise CanaryError("max_error_ratio must be between 0 and 1")
        if self.max_p95_regression < 0:
            raise CanaryError("max_p95_regression cannot be negative")
        if self.min_p95_delta_ms < 0:
            raise CanaryError("min_p95_delta_ms cannot be negative")
        if self.min_requests < 1:
            raise CanaryError("min_requests must be at least 1")
        if not 15 <= self.evaluation_seconds <= 900:
            raise CanaryError("evaluation_seconds must be from 15 to 900")


@dataclass(frozen=True)
class VersionSample:
    version: str
    total: float
    errors: float
    p95_ms: float | None
    present: bool


@dataclass(frozen=True)
class Analysis:
    action: CanaryAction
    reason: str
    stable: VersionSample
    canary: VersionSample
    error_ratio: float | None
    p95_regression: float | None
    request_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()


@dataclass
class CanaryMetrics:
    """Counters the controller serves on its private /metrics port."""

    evaluations: int = 0
    promotions: int = 0
    rollbacks: int = 0
    duration_seconds: float = 0.0
    error_ratio: float | None = None
    latency_regression: float | None = None
    state: RolloutState = RolloutState.BASELINE
    decisions: dict[str, int] = field(
        default_factory=lambda: {"promote": 0, "hold": 0, "rollback": 0}
    )

    def record(self, analysis: Analysis, duration_seconds: float, state: RolloutState) -> None:
        self.evaluations += 1
        self.duration_seconds = duration_seconds
        self.state = state
        self.error_ratio = analysis.error_ratio
        self.latency_regression = analysis.p95_regression
        action = analysis.action.value
        self.decisions[action] = self.decisions.get(action, 0) + 1
        if analysis.action is CanaryAction.PROMOTE:
            self.promotions += 1
        elif analysis.action is CanaryAction.ROLLBACK:
            self.rollbacks += 1

    def render(self) -> str:
        state_value = list(RolloutState).index(self.state)
        lines = [
            "# HELP platformforge_canary_evaluations_total Canary evaluations completed.",
            "# TYPE platformforge_canary_evaluations_total counter",
            f"platformforge_canary_evaluations_total {self.evaluations}",
            "# HELP platformforge_canary_promotions_total Canary promotions decided.",
            "# TYPE platformforge_canary_promotions_total counter",
            f"platformforge_canary_promotions_total {self.promotions}",
            "# HELP platformforge_canary_rollbacks_total Canary rollbacks decided.",
            "# TYPE platformforge_canary_rollbacks_total counter",
            f"platformforge_canary_rollbacks_total {self.rollbacks}",
            "# HELP platformforge_canary_decisions_total Decisions by action.",
            "# TYPE platformforge_canary_decisions_total counter",
        ]
        for action in ("promote", "hold", "rollback"):
            count = self.decisions.get(action, 0)
            lines.append(f'platformforge_canary_decisions_total{{action="{action}"}} {count}')
        lines.extend(
            [
                "# HELP platformforge_canary_evaluation_duration_seconds Last evaluation duration.",
                "# TYPE platformforge_canary_evaluation_duration_seconds gauge",
                f"platformforge_canary_evaluation_duration_seconds {self.duration_seconds}",
                "# HELP platformforge_canary_error_ratio Last canary HTTP 5xx ratio.",
                "# TYPE platformforge_canary_error_ratio gauge",
                f"platformforge_canary_error_ratio {_gauge(self.error_ratio)}",
                "# HELP platformforge_canary_latency_regression Last canary p95 regression.",
                "# TYPE platformforge_canary_latency_regression gauge",
                f"platformforge_canary_latency_regression {_gauge(self.latency_regression)}",
                "# HELP platformforge_canary_state Current rollout state.",
                "# TYPE platformforge_canary_state gauge",
                f'platformforge_canary_state{{state="{self.state.value}"}} {state_value}',
                "",
            ]
        )
        return "\n".join(lines)


def transition(state: RolloutState, event: str) -> RolloutState:
    """Move the rollout. An unknown event is refused. Rollback from stable stays stable."""
    nxt = _TRANSITIONS.get((state, event))
    if nxt is None:
        raise CanaryError(f"refusing transition {state.value} via {event}")
    return nxt


def error_ratio(errors: float, total: float) -> float | None:
    """Return errors/total. Zero traffic has no rate."""
    if total <= 0 or errors < 0:
        return None
    return errors / total


def p95_regression(stable_ms: float | None, canary_ms: float | None) -> float | None:
    """Return (canary - stable) / stable. A zero stable latency has no ratio."""
    if stable_ms is None or canary_ms is None or stable_ms <= 0 or canary_ms < 0:
        return None
    return (canary_ms - stable_ms) / stable_ms


def histogram_quantile(quantile: float, buckets: list[tuple[float, float]]) -> float | None:
    """Estimate a quantile from cumulative histogram buckets (le, count)."""
    if not 0 < quantile < 1 or not buckets:
        return None
    ordered = sorted((le, max(0.0, count)) for le, count in buckets)
    monotonic: list[tuple[float, float]] = []
    running = 0.0
    for le, count in ordered:
        running = max(running, count)
        monotonic.append((le, running))
    total = monotonic[-1][1]
    if total <= 0:
        return None
    rank = quantile * total
    previous_le = 0.0
    previous_count = 0.0
    for le, count in monotonic:
        if count >= rank:
            if count == previous_count:
                return previous_le
            fraction = (rank - previous_count) / (count - previous_count)
            if le == float("inf"):
                return previous_le
            return previous_le + fraction * (le - previous_le)
        previous_le = le
        previous_count = count
    return None


def analyze(
    *,
    stable: VersionSample,
    canary: VersionSample,
    thresholds: AnalysisThresholds,
    observation_seconds: int,
    ready: bool,
    timed_out: bool = False,
    request_ids: tuple[str, ...] = (),
    trace_ids: tuple[str, ...] = (),
) -> Analysis:
    """Choose promote, hold, or rollback from one stable/canary comparison."""
    ratio = error_ratio(canary.errors, canary.total)
    regression = p95_regression(stable.p95_ms, canary.p95_ms)
    window_open = observation_seconds < thresholds.evaluation_seconds

    def finish(action: CanaryAction, reason: str) -> Analysis:
        return Analysis(
            action=action,
            reason=reason,
            stable=stable,
            canary=canary,
            error_ratio=ratio,
            p95_regression=regression,
            request_ids=request_ids,
            trace_ids=trace_ids,
        )

    if timed_out:
        if window_open:
            return finish(CanaryAction.HOLD, "prometheus timed out")
        return finish(CanaryAction.ROLLBACK, "prometheus timed out after the evaluation window")
    if not ready:
        return finish(CanaryAction.ROLLBACK, "canary readiness failed")
    if not canary.present or not stable.present:
        if window_open:
            return finish(CanaryAction.HOLD, "telemetry missing")
        return finish(CanaryAction.ROLLBACK, "telemetry missing")
    if canary.total < 0 or canary.errors < 0 or canary.errors > canary.total:
        return finish(CanaryAction.ROLLBACK, "invalid sample")
    if ratio is not None and ratio > thresholds.max_error_ratio:
        return finish(CanaryAction.ROLLBACK, "canary error rate above threshold")
    if canary.total < thresholds.min_requests:
        if window_open:
            return finish(CanaryAction.HOLD, "waiting for the minimum sample")
        return finish(CanaryAction.ROLLBACK, "insufficient sample after the evaluation window")
    if stable.p95_ms is None or canary.p95_ms is None or regression is None:
        if window_open:
            return finish(CanaryAction.HOLD, "latency telemetry missing")
        return finish(CanaryAction.ROLLBACK, "latency telemetry missing")
    if canary.p95_ms is None or stable.p95_ms is None:
        delta_ms = None
    else:
        delta_ms = canary.p95_ms - stable.p95_ms
    if (
        regression is not None
        and regression > thresholds.max_p95_regression
        and (delta_ms or 0) > thresholds.min_p95_delta_ms
    ):
        return finish(CanaryAction.ROLLBACK, "canary p95 latency regressed")
    return finish(CanaryAction.PROMOTE, "error rate and p95 are inside the thresholds")


def promql_window(seconds: int) -> str:
    """Return a bounded PromQL range. Callers cannot pass a free-form duration."""
    if not 15 <= seconds <= 900:
        raise CanaryError("query window must be from 15 to 900 seconds")
    return f"{seconds}s"


def requests_query(service: str, seconds: int) -> str:
    window = promql_window(seconds)
    _require_service(service)
    return (
        "sum by (destination_version, response_code) ("
        "increase(istio_requests_total{"
        'reporter="source",'
        f'destination_service_name="{service}"'
        f"}}[{window}]))"
    )


def latency_query(service: str, seconds: int) -> str:
    window = promql_window(seconds)
    _require_service(service)
    return (
        "sum by (le, destination_version) ("
        "increase(istio_request_duration_milliseconds_bucket{"
        'reporter="source",'
        f'destination_service_name="{service}"'
        f"}}[{window}]))"
    )


def counter_requests_query(service: str) -> str:
    """Current request counters. The controller subtracts its own baseline."""
    _require_service(service)
    return (
        "sum by (destination_version, response_code) ("
        "istio_requests_total{"
        'reporter="source",'
        f'destination_service_name="{service}"'
        "})"
    )


def counter_latency_query(service: str) -> str:
    """Current latency buckets. The controller subtracts its own baseline."""
    _require_service(service)
    return (
        "sum by (le, destination_version) ("
        "istio_request_duration_milliseconds_bucket{"
        'reporter="source",'
        f'destination_service_name="{service}"'
        "})"
    )


def counters_went_backwards(current: object, baseline: object) -> bool:
    """True when a series that existed at the baseline is smaller now."""
    current_values = _metric_values(current)
    for key, previous in _metric_values(baseline).items():
        latest = current_values.get(key)
        if latest is not None and latest + 1e-6 < previous:
            return True
    return False


def delta_payload(current: object, baseline: object) -> dict:
    """Return a Prometheus vector of current minus baseline.

    A series that appears after the baseline contributes its current value.
    A series that disappeared contributes nothing.
    """
    base = _metric_values(baseline)
    rows = _vector(current)
    result: list[dict] = []
    if rows is None:
        return {"status": "success", "data": {"resultType": "vector", "result": result}}
    for metric, count in rows:
        previous = base.get(_metric_key(metric))
        delta = count if previous is None else count - previous
        if delta < 0:
            delta = 0
        result.append({"metric": metric, "value": [0, repr(delta)]})
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def samples_from_prometheus(
    requests_payload: object,
    latency_payload: object,
    *,
    stable_version: str,
    canary_version: str,
) -> tuple[VersionSample, VersionSample]:
    """Build one sample per version. A version with no series is not present."""
    totals = _counts_by_version(requests_payload)
    latencies = _p95_by_version(latency_payload)
    return (
        _sample(stable_version, totals, latencies),
        _sample(canary_version, totals, latencies),
    )


_PROMETHEUS_HOSTS = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "prometheus-server.platformforge-observability.svc",
        "prometheus-server.platformforge-observability.svc.cluster.local",
    }
)


def require_prometheus_url(base_url: str) -> str:
    """Accept loopback and the in-cluster Prometheus Service. Refuse every other host."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query:
        raise CanaryError("prometheus URL must be http without userinfo or a query")
    if parsed.hostname not in _PROMETHEUS_HOSTS:
        raise CanaryError("prometheus URL host is not allowlisted")
    if parsed.path not in {"", "/"}:
        raise CanaryError("prometheus URL must not include a path")
    return base_url.rstrip("/")


def query_prometheus(base_url: str, query: str, *, timeout: float, fetch: Fetch) -> dict:
    """Run one instant query. A timeout or a non-success body is reported, not hidden."""
    base_url = require_prometheus_url(base_url)
    encoded = urllib.parse.urlencode({"query": query})
    try:
        body = fetch(f"{base_url.rstrip('/')}/api/v1/query?{encoded}", timeout)
    except TimeoutError as exc:
        raise CanaryError("prometheus timed out") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CanaryError("prometheus response was not JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise CanaryError("prometheus rejected the query")
    return payload


def http_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read(1_000_000)
    except TimeoutError as exc:
        raise CanaryError("prometheus timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise CanaryError("prometheus timed out") from exc
        raise CanaryError("prometheus query failed") from exc


def _sample(
    version: str,
    totals: dict[str, tuple[float, float]],
    latencies: dict[str, float | None],
) -> VersionSample:
    if version not in totals:
        return VersionSample(version, 0.0, 0.0, latencies.get(version), False)
    total, errors = totals[version]
    return VersionSample(version, total, errors, latencies.get(version), True)


def _counts_by_version(payload: object) -> dict[str, tuple[float, float]]:
    rows = _vector(payload)
    if rows is None:
        return {}
    totals: dict[str, list[float]] = {}
    for metric, count in rows:
        version = str(metric.get("destination_version", ""))
        if not version:
            continue
        bucket = totals.setdefault(version, [0.0, 0.0])
        bucket[0] += count
        code = str(metric.get("response_code", ""))
        if code.startswith("5"):
            bucket[1] += count
    return {version: (total, errors) for version, (total, errors) in totals.items()}


def _p95_by_version(payload: object) -> dict[str, float | None]:
    rows = _vector(payload)
    if rows is None:
        return {}
    grouped: dict[str, list[tuple[float, float]]] = {}
    for metric, count in rows:
        version = str(metric.get("destination_version", ""))
        le = _le(metric.get("le"))
        if not version or le is None:
            continue
        grouped.setdefault(version, []).append((le, count))
    return {version: histogram_quantile(0.95, buckets) for version, buckets in grouped.items()}


def _metric_key(metric: dict) -> tuple[tuple[str, str], ...]:
    pairs = ((str(key), str(value)) for key, value in metric.items() if key != "__name__")
    return tuple(sorted(pairs))


def _metric_values(payload: object) -> dict[tuple[tuple[str, str], ...], float]:
    rows = _vector(payload)
    if rows is None:
        return {}
    return {_metric_key(metric): count for metric, count in rows}


def _vector(payload: object) -> list[tuple[dict, float]] | None:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, list):
        return None
    rows: list[tuple[dict, float]] = []
    for row in result:
        if not isinstance(row, dict):
            return None
        metric = row.get("metric")
        value = row.get("value")
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            return None
        try:
            count = float(value[1])
        except (TypeError, ValueError):
            return None
        if count != count or count in {float("inf"), float("-inf")}:
            continue
        rows.append((metric, count))
    return rows


def _le(value: object) -> float | None:
    if value in {"+Inf", "Inf"}:
        return float("inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gauge(value: float | None) -> str:
    if value is None:
        return "NaN"
    return repr(value)


def _require_service(service: str) -> None:
    if not service or not all(char.isalnum() or char == "-" for char in service):
        raise CanaryError(f"service name {service!r} is not accepted")

"""Canary comparison, histogram quantiles, and rollout transitions. No cluster calls."""

from __future__ import annotations

import pytest

from platformforge.canary import (
    AnalysisThresholds,
    CanaryAction,
    CanaryError,
    CanaryMetrics,
    RolloutState,
    VersionSample,
    analyze,
    error_ratio,
    histogram_quantile,
    p95_regression,
    promql_window,
    query_prometheus,
    requests_query,
    require_prometheus_url,
    samples_from_prometheus,
    transition,
)
from platformforge.canary_apply import controller_route, require_experiment, reviewed_route
from platformforge.gke import GKE_CONTEXT

LIMITS = AnalysisThresholds(min_requests=50, evaluation_seconds=60, max_p95_regression=0.5)


def _sample(
    version: str, total: float, errors: float, p95: float | None, present: bool = True
) -> VersionSample:
    return VersionSample(version, total, errors, p95, present)


def _pair(**overrides: object) -> dict:
    base = dict(
        stable=_sample("v0.1.0", 100, 0, 20.0),
        canary=_sample("v0.2.0", 100, 0, 22.0),
        thresholds=LIMITS,
        observation_seconds=60,
        ready=True,
    )
    base.update(overrides)
    return base


def test_promote_when_error_rate_and_p95_are_inside_limits() -> None:
    decision = analyze(**_pair())
    assert decision.action is CanaryAction.PROMOTE
    assert decision.error_ratio == 0.0
    assert decision.p95_regression == pytest.approx(0.1)


def test_canary_5xx_rolls_back_without_dividing_by_zero() -> None:
    decision = analyze(**_pair(canary=_sample("v0.2.0", 10, 4, 20.0)))
    assert decision.action is CanaryAction.ROLLBACK
    assert decision.error_ratio == pytest.approx(0.4)
    assert error_ratio(1, 0) is None
    assert p95_regression(0.0, 10.0) is None


def test_missing_telemetry_holds_then_rolls_back_when_the_window_closes() -> None:
    missing = _sample("v0.2.0", 0, 0, None, present=False)
    early = analyze(**_pair(canary=missing, observation_seconds=10))
    late = analyze(**_pair(canary=missing, observation_seconds=60))
    assert early.action is CanaryAction.HOLD
    assert late.action is CanaryAction.ROLLBACK
    assert late.reason == "telemetry missing"


def test_thin_sample_holds_until_the_window_closes() -> None:
    thin = _sample("v0.2.0", 10, 0, 20.0)
    early = analyze(**_pair(canary=thin, observation_seconds=10))
    late = analyze(**_pair(canary=thin, observation_seconds=60))
    assert early.action is CanaryAction.HOLD
    assert late.reason == "insufficient sample after the evaluation window"


def test_prometheus_timeout_does_not_promote() -> None:
    early = analyze(**_pair(timed_out=True, observation_seconds=10))
    late = analyze(**_pair(timed_out=True, observation_seconds=60))
    assert early.action is CanaryAction.HOLD
    assert late.action is CanaryAction.ROLLBACK
    assert "timed out" in late.reason


def test_small_p95_gap_stays_inside_the_absolute_floor() -> None:
    limits = AnalysisThresholds(
        min_requests=10, evaluation_seconds=60, max_p95_regression=0.5, min_p95_delta_ms=50
    )
    decision = analyze(**_pair(thresholds=limits, canary=_sample("v0.2.0", 80, 0, 40.0)))
    assert decision.p95_regression == pytest.approx(1.0)
    assert decision.action is CanaryAction.PROMOTE


def test_p95_regression_rolls_back() -> None:
    decision = analyze(**_pair(canary=_sample("v0.2.0", 80, 0, 40.0)))
    assert decision.action is CanaryAction.ROLLBACK
    assert decision.reason == "canary p95 latency regressed"


def test_histogram_quantile_uses_bucket_counts() -> None:
    buckets = [(10.0, 10.0), (20.0, 50.0), (50.0, 100.0), (float("inf"), 100.0)]
    assert histogram_quantile(0.95, buckets) == pytest.approx(47.0)
    assert histogram_quantile(0.95, []) is None
    assert histogram_quantile(0.95, [(float("inf"), 0.0)]) is None


def test_non_finite_histogram_buckets_are_skipped() -> None:
    payload = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"destination_version": "v0.1.0", "le": "10"}, "value": [1, "NaN"]},
                {"metric": {"destination_version": "v0.1.0", "le": "20"}, "value": [1, "10"]},
                {"metric": {"destination_version": "v0.1.0", "le": "+Inf"}, "value": [1, "10"]},
            ]
        },
    }
    stable, _canary = samples_from_prometheus(
        {"status": "success", "data": {"result": []}},
        payload,
        stable_version="v0.1.0",
        canary_version="v0.2.0",
    )
    assert stable.p95_ms == pytest.approx(19.0)


def test_empty_prometheus_vector_is_missing() -> None:
    empty = {"status": "success", "data": {"result": []}}
    stable, canary = samples_from_prometheus(
        empty, empty, stable_version="v0.1.0", canary_version="v0.2.0"
    )
    assert stable.present is False
    assert canary.present is False


def test_prometheus_vector_splits_versions_and_5xx() -> None:
    requests = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"destination_version": "v0.2.0", "response_code": "200"},
                    "value": [1, "8"],
                },
                {
                    "metric": {"destination_version": "v0.2.0", "response_code": "500"},
                    "value": [1, "2"],
                },
                {
                    "metric": {"destination_version": "v0.1.0", "response_code": "200"},
                    "value": [1, "90"],
                },
            ]
        },
    }
    latency = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"destination_version": "v0.1.0", "le": "10"}, "value": [1, "90"]},
                {"metric": {"destination_version": "v0.1.0", "le": "+Inf"}, "value": [1, "90"]},
                {"metric": {"destination_version": "v0.2.0", "le": "10"}, "value": [1, "5"]},
                {"metric": {"destination_version": "v0.2.0", "le": "+Inf"}, "value": [1, "10"]},
            ]
        },
    }
    stable, canary = samples_from_prometheus(
        requests, latency, stable_version="v0.1.0", canary_version="v0.2.0"
    )
    assert (stable.total, stable.errors) == (90, 0)
    assert (canary.total, canary.errors) == (10, 2)
    assert canary.p95_ms is not None


def test_query_timeout_is_reported() -> None:
    def _timeout(_url: str, _seconds: float) -> bytes:
        raise TimeoutError

    with pytest.raises(CanaryError, match="timed out"):
        query_prometheus("http://127.0.0.1:9090", "up", timeout=1, fetch=_timeout)


def test_query_window_rejects_free_form_input() -> None:
    assert promql_window(60) == "60s"
    with pytest.raises(CanaryError):
        promql_window(5)
    query = requests_query("inventory-service", 60)
    assert "increase" in query
    assert "[60s]" in query
    with pytest.raises(CanaryError):
        requests_query("inventory;drop", 60)


def test_state_machine_and_idempotent_rollback() -> None:
    state = RolloutState.BASELINE
    state = transition(state, "start")
    state = transition(state, "analyze")
    state = transition(state, "rollback")
    assert state is RolloutState.ROLLING_BACK
    assert transition(state, "confirm") is RolloutState.STABLE
    assert transition(RolloutState.STABLE, "rollback") is RolloutState.STABLE
    with pytest.raises(CanaryError):
        transition(RolloutState.BASELINE, "promote")


def test_metrics_render_counts_and_state() -> None:
    metrics = CanaryMetrics()
    decision = analyze(**_pair(canary=_sample("v0.2.0", 10, 8, 20.0)))
    state = transition(RolloutState.BASELINE, "start")
    state = transition(state, "analyze")
    state = transition(state, "rollback")
    metrics.record(decision, 12.5, state)
    text = metrics.render()
    assert "platformforge_canary_rollbacks_total 1" in text
    assert "platformforge_canary_evaluations_total 1" in text
    assert 'platformforge_canary_decisions_total{action="rollback"} 1' in text
    assert "platformforge_canary_error_ratio 0.8" in text
    assert 'state="rolling_back"' in text
    assert "delete" not in text


def test_prometheus_url_allowlist() -> None:
    assert require_prometheus_url("http://127.0.0.1:9090").endswith(":9090")
    cluster = "http://prometheus-server.platformforge-observability.svc.cluster.local"
    assert require_prometheus_url(cluster) == cluster
    with pytest.raises(CanaryError):
        require_prometheus_url("http://prometheus.example.com")
    with pytest.raises(CanaryError):
        require_prometheus_url("https://127.0.0.1:9090")


def test_controller_route_never_selects_the_canary_split() -> None:
    assert controller_route("rollback") == "inventory-virtualservice-stable.yaml"
    assert controller_route("promote") == "inventory-virtualservice-promote.yaml"
    with pytest.raises(CanaryError):
        controller_route("canary")


def test_experiment_refuses_gke_and_unreviewed_weights() -> None:
    require_experiment("kind-platformforge", "platformforge-dev")
    with pytest.raises(CanaryError):
        require_experiment(GKE_CONTEXT, "platformforge-dev")
    with pytest.raises(CanaryError):
        require_experiment("kind-platformforge", "kube-system")
    assert reviewed_route(100, 0) == "inventory-virtualservice-stable.yaml"
    assert reviewed_route(90, 10) == "inventory-virtualservice-canary.yaml"
    assert reviewed_route(0, 100) == "inventory-virtualservice-promote.yaml"
    with pytest.raises(CanaryError):
        reviewed_route(50, 50)

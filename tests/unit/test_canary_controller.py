"""Controller decisions, shutdown, and the private metrics port. No cluster calls."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from platformforge.canary import (
    Analysis,
    CanaryAction,
    CanaryError,
    CanaryMetrics,
    RolloutState,
    VersionSample,
    analyze,
    counter_requests_query,
    delta_payload,
)
from platformforge.canary_controller import (
    ControllerConfig,
    decide_tick,
    parse_weights,
    run_loop,
    serve_metrics,
)
from platformforge.gke import GKE_CONTEXT

PROM = "http://127.0.0.1:9090"


def _config(**overrides: object) -> ControllerConfig:
    base = dict(
        context="kind-platformforge",
        namespace="platformforge-dev",
        prometheus_url=PROM,
        poll_interval_seconds=5,
        window_seconds=60,
        max_episode_seconds=90,
    )
    base.update(overrides)
    return ControllerConfig(**base)  # type: ignore[arg-type]


def _sample(version: str, total: float, errors: float, p95: float | None) -> VersionSample:
    return VersionSample(version, total, errors, p95, True)


def _analysis(action: CanaryAction, *, errors: float = 0, p95: float = 20) -> Analysis:
    return Analysis(
        action,
        "test",
        _sample("v0.1.0", 100, 0, 20),
        _sample("v0.2.0", 40, errors, p95),
        None if errors < 0 else errors / 40,
        None if p95 < 0 else (p95 - 20) / 20,
    )


def test_stable_route_does_not_change() -> None:
    tick = decide_tick(
        weights=(100, 0),
        analysis=None,
        episode_age=10,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action is None
    assert tick.state is RolloutState.STABLE
    assert tick.analysis.action is CanaryAction.HOLD


def test_error_ratio_selects_rollback() -> None:
    tick = decide_tick(
        weights=(90, 10),
        analysis=_analysis(CanaryAction.ROLLBACK, errors=20),
        episode_age=30,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action == "rollback"


def test_healthy_split_selects_promote() -> None:
    tick = decide_tick(
        weights=(90, 10),
        analysis=_analysis(CanaryAction.PROMOTE),
        episode_age=30,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action == "promote"


def test_latency_regression_selects_rollback() -> None:
    config = _config()
    decision = analyze(
        stable=_sample("v0.1.0", 100, 0, 20),
        canary=_sample("v0.2.0", 40, 0, 400),
        thresholds=config.thresholds,
        observation_seconds=60,
        ready=True,
    )
    tick = decide_tick(
        weights=(90, 10),
        analysis=decision,
        episode_age=40,
        config=config,
        ready=True,
        timed_out=False,
    )
    assert decision.reason == "canary p95 latency regressed"
    assert tick.route_action == "rollback"
    assert tick.analysis.p95_regression == pytest.approx(19)


def test_missing_telemetry_holds_then_fails_closed() -> None:
    early = decide_tick(
        weights=(90, 10),
        analysis=None,
        episode_age=10,
        config=_config(),
        ready=True,
        timed_out=True,
    )
    late = decide_tick(
        weights=(90, 10),
        analysis=None,
        episode_age=60,
        config=_config(),
        ready=True,
        timed_out=True,
    )
    assert early.route_action is None
    assert early.analysis.action is CanaryAction.HOLD
    assert late.route_action == "rollback"


def test_unreviewed_weights_roll_back() -> None:
    tick = decide_tick(
        weights=(50, 50),
        analysis=None,
        episode_age=1,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action == "rollback"
    assert tick.analysis.reason == "unreviewed weights"


def test_episode_hold_fails_closed() -> None:
    tick = decide_tick(
        weights=(90, 10),
        analysis=_analysis(CanaryAction.HOLD),
        episode_age=90,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action == "rollback"
    assert "episode exceeded" in tick.analysis.reason


def test_promoted_route_does_not_promote_again() -> None:
    tick = decide_tick(
        weights=(0, 100),
        analysis=_analysis(CanaryAction.PROMOTE),
        episode_age=20,
        config=_config(),
        ready=True,
        timed_out=False,
    )
    assert tick.route_action is None
    assert tick.analysis.action is CanaryAction.HOLD


def test_gke_context_is_refused() -> None:
    with pytest.raises(CanaryError):
        _config(context=GKE_CONTEXT)
    with pytest.raises(CanaryError):
        _config(prometheus_url="http://prometheus.example.com")
    with pytest.raises(CanaryError):
        _config(metrics_host="10.1.2.3")


def test_gke_context_requires_the_pod_token(monkeypatch, tmp_path) -> None:
    from platformforge import canary_apply

    monkeypatch.setenv("PLATFORMFORGE_CANARY_GKE", "1")
    monkeypatch.setenv("PLATFORMFORGE_CANARY_IN_CLUSTER", "1")
    with pytest.raises(CanaryError, match="service account token"):
        _config(context=GKE_CONTEXT)
    token = tmp_path / "token"
    token.write_text("present", encoding="utf-8")
    monkeypatch.setattr(canary_apply, "_SA_TOKEN", token)
    config = _config(context=GKE_CONTEXT)
    assert config.context == GKE_CONTEXT
    monkeypatch.delenv("PLATFORMFORGE_CANARY_GKE")
    with pytest.raises(CanaryError, match="not enabled"):
        _config(context=GKE_CONTEXT)


def test_parse_weights_rejects_other_shapes() -> None:
    body = {
        "spec": {
            "http": [
                {
                    "route": [
                        {"destination": {"subset": "v0-1-0"}, "weight": 90},
                        {"destination": {"subset": "v0-2-0"}, "weight": 10},
                    ]
                }
            ]
        }
    }
    assert parse_weights(body) == (90, 10)
    with pytest.raises(CanaryError):
        parse_weights({"spec": {}})


def test_delta_ignores_traffic_from_before_the_baseline() -> None:
    baseline = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"destination_version": "v0.2.0", "response_code": "500"},
                    "value": [1, "40"],
                }
            ]
        },
    }
    current = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"destination_version": "v0.2.0", "response_code": "500"},
                    "value": [2, "42"],
                },
                {
                    "metric": {"destination_version": "v0.2.0", "response_code": "200"},
                    "value": [2, "3"],
                },
            ]
        },
    }
    payload = delta_payload(current, baseline)
    counts = {
        row["metric"]["response_code"]: float(row["value"][1]) for row in payload["data"]["result"]
    }
    assert counts == {"500": 2.0, "200": 3.0}
    assert "[120s]" not in counter_requests_query("inventory-service")


def test_loop_rolls_back_once_and_a_second_stable_tick_does_not_apply() -> None:
    weights = {"value": (90, 10)}
    applied: list[str] = []
    stop = threading.Event()

    def apply_action(action: str) -> None:
        applied.append(action)
        if action == "rollback":
            weights["value"] = (100, 0)

    seen_stable = {"n": 0}

    def read_weights() -> tuple[int, int]:
        if weights["value"] == (100, 0):
            seen_stable["n"] += 1
        return weights["value"]

    def sleep(_seconds: float) -> None:
        if seen_stable["n"] >= 1:
            stop.set()

    calls = {"n": 0}

    def query(_promql: str) -> dict:
        calls["n"] += 1
        errors = "0" if calls["n"] <= 2 else "12"
        return {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"destination_version": "v0.2.0", "response_code": "500"},
                        "value": [1, errors],
                    },
                    {
                        "metric": {"destination_version": "v0.1.0", "response_code": "200"},
                        "value": [1, "80"],
                    },
                ]
            },
        }

    metrics = CanaryMetrics()
    state = run_loop(
        _config(),
        metrics=metrics,
        stop=stop,
        read_weights=read_weights,
        read_ready=lambda: True,
        apply_action=apply_action,
        query=query,
        sleep=sleep,
    )
    assert applied == ["rollback"]
    assert state is RolloutState.STABLE
    assert metrics.rollbacks == 1
    assert "delete" not in json.dumps(applied)


def test_shutdown_of_an_open_canary_applies_stable_once() -> None:
    applied: list[str] = []
    stop = threading.Event()

    def sleep(_seconds: float) -> None:
        stop.set()

    state = run_loop(
        _config(),
        metrics=CanaryMetrics(),
        stop=stop,
        read_weights=lambda: (90, 10),
        read_ready=lambda: True,
        apply_action=applied.append,
        query=lambda _query: (_ for _ in ()).throw(CanaryError("down")),
        sleep=sleep,
    )
    assert applied == ["rollback"]
    assert state is RolloutState.STABLE


def test_metrics_endpoint_is_loopback_and_reports_a_decision() -> None:
    metrics = CanaryMetrics()
    metrics.record(_analysis(CanaryAction.ROLLBACK, errors=10), 4.0, RolloutState.STABLE)
    server, _thread = serve_metrics(metrics, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as response:  # noqa: S310
            body = response.read().decode()
        assert "platformforge_canary_rollbacks_total 1" in body
        assert 'platformforge_canary_decisions_total{action="rollback"} 1' in body
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=2)  # noqa: S310
        assert raised.value.code == 404
    finally:
        server.shutdown()
    with pytest.raises(CanaryError):
        serve_metrics(metrics, "10.0.0.8", 8080)

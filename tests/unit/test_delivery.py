"""Canary decisions and delivery gates. No cluster and no registry calls."""

from __future__ import annotations

import pytest

from platformforge.delivery import (
    CANARY_WEIGHT,
    CanaryAction,
    CanaryObservation,
    CanaryThresholds,
    DeliveryError,
    ReleaseState,
    acquire_delivery_lock,
    canary_plan,
    commands_for,
    decide,
    promoted_virtualservice,
    refuse_apply,
    release_delivery_lock,
    require_deployable_image,
    require_manual_approval,
    require_private_manifest,
    require_target,
    rollback_plan,
    totals_from_prometheus,
)
from platformforge.gke import GKE_CONTEXT, GKE_PROJECT, GKE_REGISTRY

DIGEST = "sha256:" + ("ab" * 32)
OTHER = "sha256:" + ("cd" * 32)
IMAGE = f"{GKE_REGISTRY}/inventory-service@{DIGEST}"
PREVIOUS = f"{GKE_REGISTRY}/inventory-service@{OTHER}"
LIMITS = CanaryThresholds()


def _obs(**overrides: object) -> CanaryObservation:
    base = dict(
        total_requests=100,
        error_requests=0,
        success_requests=100,
        ready=True,
        telemetry_present=True,
        observation_seconds=120,
        authorization_ok=True,
        timed_out=False,
    )
    base.update(overrides)
    return CanaryObservation(**base)  # type: ignore[arg-type]


def _state(**overrides: object) -> ReleaseState:
    base = dict(
        stable_weight=90,
        canary_weight=10,
        previous_canary_image=PREVIOUS,
        candidate_image=IMAGE,
        failure_percent=0,
    )
    base.update(overrides)
    return ReleaseState(**base)  # type: ignore[arg-type]


def test_healthy_sample_promotes() -> None:
    decision = decide(_obs(), LIMITS)
    assert decision.action is CanaryAction.PROMOTE


def test_elevated_5xx_rolls_back_before_the_minimum_sample() -> None:
    decision = decide(_obs(total_requests=10, error_requests=4, success_requests=6), LIMITS)
    assert decision.action is CanaryAction.ROLLBACK
    assert "error rate" in decision.reason


def test_low_success_rate_rolls_back() -> None:
    decision = decide(_obs(total_requests=100, error_requests=0, success_requests=90), LIMITS)
    assert decision.action is CanaryAction.ROLLBACK
    assert "success rate" in decision.reason


def test_missing_telemetry_rolls_back() -> None:
    decision = decide(_obs(telemetry_present=False, total_requests=0, success_requests=0), LIMITS)
    assert decision.action is CanaryAction.ROLLBACK
    assert decision.reason == "telemetry missing"


def test_thin_sample_holds_then_rolls_back_when_the_window_closes() -> None:
    early = decide(_obs(total_requests=10, success_requests=10, observation_seconds=10), LIMITS)
    late = decide(_obs(total_requests=10, success_requests=10, observation_seconds=120), LIMITS)
    assert early.action is CanaryAction.HOLD
    assert late.action is CanaryAction.ROLLBACK
    assert "insufficient sample" in late.reason


def test_readiness_timeout_and_authorization_roll_back() -> None:
    assert decide(_obs(ready=False), LIMITS).action is CanaryAction.ROLLBACK
    assert decide(_obs(timed_out=True), LIMITS).action is CanaryAction.ROLLBACK
    assert decide(_obs(authorization_ok=False), LIMITS).action is CanaryAction.ROLLBACK


def test_empty_prometheus_result_is_missing_telemetry() -> None:
    total, success, errors, present = totals_from_prometheus(
        {"status": "success", "data": {"result": []}}
    )
    assert (total, success, errors, present) == (0, 0, 0, False)


def test_prometheus_vector_counts_2xx_and_5xx() -> None:
    payload = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"response_code": "200"}, "value": [1, "90"]},
                {"metric": {"response_code": "500"}, "value": [1, "10"]},
                {"metric": {"response_code": "404"}, "value": [1, "5"]},
            ]
        },
    }
    assert totals_from_prometheus(payload) == (105, 90, 10, True)


def test_thresholds_reject_impossible_ratios() -> None:
    with pytest.raises(DeliveryError):
        CanaryThresholds(max_error_ratio=1.5)


def test_image_must_be_an_amd64_digest_in_the_lab_registry() -> None:
    assert require_deployable_image(IMAGE, "linux/amd64") == IMAGE
    with pytest.raises(DeliveryError):
        require_deployable_image(f"{GKE_REGISTRY}/inventory-service:0.2.0", "linux/amd64")
    with pytest.raises(DeliveryError):
        require_deployable_image(f"{GKE_REGISTRY}/inventory-service:latest", "linux/amd64")
    with pytest.raises(DeliveryError):
        require_deployable_image(IMAGE, "linux/arm64")
    with pytest.raises(DeliveryError):
        require_deployable_image("example.com/inventory-service@" + DIGEST, "linux/amd64")


def test_target_rejects_kind_and_other_projects() -> None:
    require_target(context=GKE_CONTEXT, project=GKE_PROJECT, cluster="platformforge-dev")
    with pytest.raises(DeliveryError):
        require_target(
            context="kind-platformforge", project=GKE_PROJECT, cluster="platformforge-dev"
        )
    with pytest.raises(DeliveryError):
        require_target(context=GKE_CONTEXT, project="other-project", cluster="platformforge-dev")
    with pytest.raises(DeliveryError):
        require_target(context=GKE_CONTEXT, project=GKE_PROJECT, cluster="other")


def test_approval_allows_only_a_manual_dispatch_on_main() -> None:
    require_manual_approval(
        {
            "PLATFORMFORGE_DELIVERY_APPROVED": "1",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
        }
    )
    with pytest.raises(DeliveryError):
        require_manual_approval({"GITHUB_EVENT_NAME": "workflow_dispatch"})
    with pytest.raises(DeliveryError):
        require_manual_approval(
            {
                "PLATFORMFORGE_DELIVERY_APPROVED": "1",
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REF": "refs/heads/main",
            }
        )
    with pytest.raises(DeliveryError):
        require_manual_approval(
            {
                "PLATFORMFORGE_DELIVERY_APPROVED": "1",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_BASE_REF": "main",
            }
        )


def test_public_manifests_are_refused() -> None:
    with pytest.raises(DeliveryError):
        require_private_manifest("type: LoadBalancer")
    require_private_manifest(promoted_virtualservice())


def test_rollback_is_idempotent_and_does_not_delete() -> None:
    steps = rollback_plan(_state(failure_percent=100))
    assert [step.kind for step in steps] == ["route", "image", "failure-percent"]
    restored = ReleaseState(
        stable_weight=100,
        canary_weight=0,
        previous_canary_image=PREVIOUS,
        candidate_image=PREVIOUS,
        failure_percent=0,
    )
    assert rollback_plan(restored) == []
    assert rollback_plan(restored) == []
    commands = commands_for(steps)
    assert all("delete" not in part for command in commands for part in command)
    assert commands[1][-1] == f"inventory-service={PREVIOUS}"


def test_canary_weight_is_ten_percent() -> None:
    assert canary_plan()[0].detail == f"v0-1-0={100 - CANARY_WEIGHT} v0-2-0={CANARY_WEIGHT}"


def test_lock_blocks_a_second_delivery_until_release(tmp_path) -> None:
    path = tmp_path / "delivery.lock"
    acquire_delivery_lock(path, now=1000)
    with pytest.raises(DeliveryError):
        acquire_delivery_lock(path, now=1001)
    release_delivery_lock(path)
    release_delivery_lock(path)
    acquire_delivery_lock(path, now=2000)


def test_stale_lock_can_be_replaced(tmp_path) -> None:
    path = tmp_path / "delivery.lock"
    acquire_delivery_lock(path, now=1000)
    acquire_delivery_lock(path, now=1000 + 3600, stale_after=3600)


def test_refuse_apply_does_not_describe_a_successful_deploy() -> None:
    message = refuse_apply()
    assert "refused" in message
    assert "delete" not in message

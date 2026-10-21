"""Canary decisions and delivery gates. Nothing here contacts a cluster or a registry.

Live apply and image publish stay behind separate approvals. This module
plans those steps and refuses to run them.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from platformforge.gke import GKE_CONTEXT, GKE_PROJECT, GKE_REGISTRY, exposure_findings

GKE_CLUSTER = "platformforge-dev"
CANARY_WEIGHT = 10
STABLE_WEIGHT = 100
_IMAGE = re.compile(
    rf"^{re.escape(GKE_REGISTRY)}/(?P<name>[a-z0-9-]+)@(?P<digest>sha256:[a-f0-9]{{64}})$"
)


class DeliveryError(Exception):
    """Raised when a delivery gate refuses to continue."""


class CanaryAction(Enum):
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    HOLD = "hold"


@dataclass(frozen=True)
class CanaryThresholds:
    """Limits for one observation window. Ratios are fractions from 0 to 1."""

    max_error_ratio: float = 0.05
    min_success_ratio: float = 0.95
    min_requests: int = 50
    min_observation_seconds: int = 60

    def __post_init__(self) -> None:
        if not 0 <= self.max_error_ratio <= 1:
            raise DeliveryError("max_error_ratio must be between 0 and 1")
        if not 0 <= self.min_success_ratio <= 1:
            raise DeliveryError("min_success_ratio must be between 0 and 1")
        if self.min_requests < 1:
            raise DeliveryError("min_requests must be at least 1")
        if self.min_observation_seconds < 1:
            raise DeliveryError("min_observation_seconds must be at least 1")


@dataclass(frozen=True)
class CanaryObservation:
    total_requests: int
    error_requests: int
    success_requests: int
    ready: bool
    telemetry_present: bool
    observation_seconds: int
    authorization_ok: bool
    timed_out: bool = False


@dataclass(frozen=True)
class Decision:
    action: CanaryAction
    reason: str


@dataclass(frozen=True)
class ReleaseState:
    """Weights and the canary Deployment's current and previous images."""

    stable_weight: int
    canary_weight: int
    previous_canary_image: str
    candidate_image: str
    failure_percent: int

    def __post_init__(self) -> None:
        if self.stable_weight < 0 or self.canary_weight < 0:
            raise DeliveryError("weights cannot be negative")
        if self.stable_weight + self.canary_weight != 100:
            raise DeliveryError("stable and canary weights must sum to 100")
        if not 0 <= self.failure_percent <= 100:
            raise DeliveryError("failure_percent must be between 0 and 100")


@dataclass(frozen=True)
class PlanStep:
    """One idempotent recovery or promotion step. Never a delete."""

    kind: str
    detail: str


def require_deployable_image(reference: str, architecture: str) -> str:
    """Accept only a digest-pinned linux/amd64 image in the PlatformForge registry."""
    if ":" in reference.split("@", 1)[0] and "@sha256:" not in reference:
        raise DeliveryError(f"refusing mutable tag in {reference!r}")
    if reference.endswith(":latest") or ":latest@" in reference:
        raise DeliveryError("refusing the latest tag")
    match = _IMAGE.fullmatch(reference)
    if match is None:
        raise DeliveryError(f"image must be {GKE_REGISTRY}/<service>@sha256:<64 hex>")
    if architecture != "linux/amd64":
        raise DeliveryError(f"refusing architecture {architecture!r}; GKE nodes are linux/amd64")
    return reference


def require_target(*, context: str, project: str, cluster: str) -> None:
    """Refuse kind, the implicit context, and any project or cluster other than this lab."""
    if context.startswith("kind-"):
        raise DeliveryError(f"refusing kind context {context!r}")
    if context != GKE_CONTEXT:
        raise DeliveryError(f"refusing context {context!r}")
    if project != GKE_PROJECT:
        raise DeliveryError(f"refusing project {project!r}")
    if cluster != GKE_CLUSTER:
        raise DeliveryError(f"refusing cluster {cluster!r}")


def require_manual_approval(env: Mapping[str, str]) -> None:
    """Allow delivery only from an approved manual dispatch on main."""
    if env.get("PLATFORMFORGE_DELIVERY_APPROVED") != "1":
        raise DeliveryError("delivery is not approved")
    event = env.get("GITHUB_EVENT_NAME")
    if event is not None and event != "workflow_dispatch":
        raise DeliveryError(f"refusing GitHub event {event!r}")
    ref = env.get("GITHUB_REF")
    if ref is not None and ref != "refs/heads/main":
        raise DeliveryError(f"refusing ref {ref!r}")
    if env.get("GITHUB_BASE_REF"):
        raise DeliveryError("refusing a pull request")


def require_private_manifest(text: str) -> None:
    """Reject documents that would expose a Service or add an Ingress."""
    found = exposure_findings(text)
    if found:
        raise DeliveryError("refusing public exposure: " + ", ".join(found))


def acquire_delivery_lock(path: Path, *, now: float, stale_after: float = 3600) -> None:
    """Create a lock file. A fresh lock means another delivery is in progress."""
    if path.exists():
        recorded = float(path.read_text(encoding="utf-8").split()[0])
        if now - recorded < stale_after:
            raise DeliveryError(f"delivery lock is held: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{now}\n", encoding="utf-8")


def release_delivery_lock(path: Path) -> None:
    """Remove the lock if it exists. Repeating this is a no-op."""
    path.unlink(missing_ok=True)


def totals_from_prometheus(payload: object) -> tuple[int, int, int, bool]:
    """Sum Istio request series into total, HTTP 2xx, and HTTP 5xx.

    Returns telemetry_present as the fourth value. An empty or failed query is
    missing telemetry, which must not be treated as a healthy zero.
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return 0, 0, 0, False
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0, 0, 0, False
    result = data.get("result")
    if not isinstance(result, list) or not result:
        return 0, 0, 0, False
    total = success = errors = 0
    for row in result:
        if not isinstance(row, dict):
            return 0, 0, 0, False
        metric = row.get("metric")
        value = row.get("value")
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            return 0, 0, 0, False
        try:
            count = int(float(value[1]))
        except (TypeError, ValueError):
            return 0, 0, 0, False
        code = str(metric.get("response_code", ""))
        total += count
        if code.startswith("2"):
            success += count
        elif code.startswith("5"):
            errors += count
    return total, success, errors, True


def decide(observation: CanaryObservation, thresholds: CanaryThresholds) -> Decision:
    """Choose promote, rollback, or hold. Missing or thin telemetry never promotes."""
    if observation.timed_out:
        return Decision(CanaryAction.ROLLBACK, "deployment timed out")
    if not observation.authorization_ok:
        return Decision(CanaryAction.ROLLBACK, "authorization check failed")
    if not observation.ready:
        return Decision(CanaryAction.ROLLBACK, "readiness failed")
    if not observation.telemetry_present:
        return Decision(CanaryAction.ROLLBACK, "telemetry missing")
    if (
        observation.total_requests < 0
        or observation.error_requests < 0
        or observation.success_requests < 0
        or observation.error_requests > observation.total_requests
        or observation.success_requests > observation.total_requests
    ):
        return Decision(CanaryAction.ROLLBACK, "invalid sample")
    if observation.total_requests > 0:
        error_ratio = observation.error_requests / observation.total_requests
        success_ratio = observation.success_requests / observation.total_requests
        if error_ratio > thresholds.max_error_ratio:
            return Decision(CanaryAction.ROLLBACK, "error rate above threshold")
        if success_ratio < thresholds.min_success_ratio:
            return Decision(CanaryAction.ROLLBACK, "success rate below threshold")
    if observation.total_requests < thresholds.min_requests:
        if observation.observation_seconds < thresholds.min_observation_seconds:
            return Decision(CanaryAction.HOLD, "waiting for the minimum sample")
        return Decision(CanaryAction.ROLLBACK, "insufficient sample after the observation window")
    if observation.observation_seconds < thresholds.min_observation_seconds:
        return Decision(CanaryAction.HOLD, "observation window still open")
    return Decision(CanaryAction.PROMOTE, "error and success rates are inside the thresholds")


def rollback_plan(state: ReleaseState) -> list[PlanStep]:
    """Restore stable routing, the previous canary image, and a zero failure rate.

    Already-stable state produces an empty plan. The plan never deletes a workload.
    """
    steps: list[PlanStep] = []
    if (state.stable_weight, state.canary_weight) != (STABLE_WEIGHT, 0):
        steps.append(PlanStep("route", "v0-1-0=100 v0-2-0=0"))
    if state.candidate_image != state.previous_canary_image:
        steps.append(PlanStep("image", state.previous_canary_image))
    if state.failure_percent != 0:
        steps.append(PlanStep("failure-percent", "0"))
    return steps


def promote_plan() -> list[PlanStep]:
    """Shift inventory traffic onto the candidate subset and leave v0.1.0 in place."""
    return [PlanStep("route", "v0-1-0=0 v0-2-0=100")]


def canary_plan() -> list[PlanStep]:
    """Route the fixed 10 percent slice at the candidate subset."""
    return [PlanStep("route", f"v0-1-0={100 - CANARY_WEIGHT} v0-2-0={CANARY_WEIGHT}")]


def promoted_virtualservice() -> str:
    """100 percent of inventory traffic to the candidate subset. v0.1.0 stays deployed."""
    text = """apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: inventory-service
  namespace: platformforge-dev
spec:
  hosts:
    - inventory-service.platformforge-dev.svc.cluster.local
  exportTo: ["."]
  http:
    - name: inventory
      route:
        - destination:
            host: inventory-service.platformforge-dev.svc.cluster.local
            subset: v0-1-0
            port:
              number: 8080
          weight: 0
        - destination:
            host: inventory-service.platformforge-dev.svc.cluster.local
            subset: v0-2-0
            port:
              number: 8080
          weight: 100
"""
    require_private_manifest(text)
    return text


def commands_for(steps: list[PlanStep]) -> list[list[str]]:
    """Turn a plan into kubectl argv. Delete is not representable here."""
    stable = "deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml"
    canary = "deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml"
    healthy = "services/manifests/inventory-service-v0.2.0.yaml"
    commands: list[list[str]] = []
    for step in steps:
        if step.kind == "route" and step.detail == "v0-1-0=100 v0-2-0=0":
            commands.append(["kubectl", "apply", "-f", stable])
        elif step.kind == "route" and step.detail == "v0-1-0=90 v0-2-0=10":
            commands.append(["kubectl", "apply", "-f", canary])
        elif step.kind == "route" and step.detail == "v0-1-0=0 v0-2-0=100":
            commands.append(["kubectl", "apply", "-f", "-"])
        elif step.kind == "image":
            require_deployable_image(step.detail, "linux/amd64")
            commands.append(
                [
                    "kubectl",
                    "set",
                    "image",
                    "deployment/inventory-service-v0-2-0",
                    f"inventory-service={step.detail}",
                ]
            )
        elif step.kind == "failure-percent" and step.detail == "0":
            commands.append(["kubectl", "apply", "-f", healthy])
        else:
            raise DeliveryError(f"unknown plan step {step.kind}")
        if any(part == "delete" for command in commands for part in command):
            raise DeliveryError("delivery cannot delete workloads")
    return commands


def refuse_apply() -> str:
    """Live kubectl apply is a separate approval and is not available from this command."""
    return (
        "Live apply is refused. Promotion and rollback plans can be printed. "
        "Changing the GKE cluster requires a separate explicit approval."
    )


def observation_from_json(raw: str) -> CanaryObservation:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise DeliveryError("observation must be a JSON object")
    return CanaryObservation(
        total_requests=int(data["total_requests"]),
        error_requests=int(data["error_requests"]),
        success_requests=int(data["success_requests"]),
        ready=bool(data["ready"]),
        telemetry_present=bool(data["telemetry_present"]),
        observation_seconds=int(data["observation_seconds"]),
        authorization_ok=bool(data["authorization_ok"]),
        timed_out=bool(data.get("timed_out", False)),
    )


def main(argv: list[str] | None = None) -> int:
    """Print a decision or refuse apply. This command does not call kubectl."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m platformforge.delivery")
    sub = parser.add_subparsers(dest="command", required=True)
    decide_cmd = sub.add_parser("decide")
    decide_cmd.add_argument("observation")
    sub.add_parser("refuse-apply")
    gate = sub.add_parser("gate")
    gate.add_argument("--context", required=True)
    gate.add_argument("--project", required=True)
    gate.add_argument("--cluster", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "refuse-apply":
            print(refuse_apply(), file=sys.stderr)
            return 2
        if args.command == "gate":
            require_manual_approval(os.environ)
            require_target(context=args.context, project=args.project, cluster=args.cluster)
            print("delivery gate passed; apply is still refused")
            return 0
        decision = decide(
            observation_from_json(Path(args.observation).read_text(encoding="utf-8")),
            CanaryThresholds(),
        )
        print(f"{decision.action.value} {decision.reason}")
    except (DeliveryError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

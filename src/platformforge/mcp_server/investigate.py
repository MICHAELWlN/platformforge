"""Classify incident evidence. Hypotheses are never reported as verified causes."""

from __future__ import annotations

from typing import Any

from platformforge.mcp_server.policy import require_service_name, untrusted


def build_report(
    *,
    service: str,
    symptom: str,
    deployments: list[dict[str, Any]],
    pods: list[dict[str, Any]],
    events: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    prometheus: dict[str, Any] | None,
    loki: dict[str, Any] | None,
    tempo: dict[str, Any] | None,
    correlation: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Return observations, evidence, hypotheses, gaps, and read-only next steps."""
    name = require_service_name(service)
    verified: list[str] = []
    supporting: list[object] = []
    hypotheses: list[str] = []
    missing: list[str] = []
    restarts = 0
    not_ready = 0
    for row in deployments:
        desired = int(row.get("desired", 0) or 0)
        available = int(row.get("available", 0) or 0)
        verified.append(
            f"Deployment {row.get('name')} has {available} available of {desired} desired."
        )
        if available < desired:
            not_ready += 1
    for pod in pods:
        restarts += int(pod.get("restarts", 0) or 0)
        ready = int(pod.get("ready_containers", 0) or 0)
        total = int(pod.get("containers", 0) or 0)
        verified.append(
            f"Pod {pod.get('name')} phase {pod.get('phase')} ready {ready}/{total} "
            f"restarts {pod.get('restarts')} node {pod.get('node')}."
        )
    if not deployments and not pods:
        missing.append(f"No Deployment or Pod named for {name} was returned.")
    if not_ready:
        hypotheses.append(
            "Unverified hypothesis: unavailable replicas explain the symptom. "
            "Readiness alone does not identify the failing request path."
        )
    if restarts:
        hypotheses.append(
            "Unverified hypothesis: container restarts are consistent with a crash. "
            "The restart count does not prove it caused this symptom."
        )
    if symptom.strip():
        supporting.append(untrusted(symptom.strip()))
    supporting.extend(events[:10])
    supporting.extend(logs[:5])
    _telemetry(prometheus, "Prometheus", missing, supporting, hypotheses)
    _telemetry(loki, "Loki", missing, supporting, hypotheses)
    _telemetry(tempo, "Tempo", missing, supporting, hypotheses)
    if isinstance(correlation, dict):
        for fact in correlation.get("facts") or []:
            text = str(fact)
            if text.startswith("VirtualService"):
                verified.append(text)
            else:
                supporting.append(text)
        hypotheses.extend(str(item) for item in correlation.get("hypotheses") or [])
        missing.extend(str(item) for item in correlation.get("missing") or [])
    else:
        missing.append("Request and trace correlation was not run.")
    return {
        "service": name,
        "verified_root_cause": None,
        "verified": verified,
        "supporting_evidence": supporting,
        "possible_explanations": hypotheses,
        "missing": missing,
        "correlation": correlation,
        "next_diagnostics": [
            "Read one request_id from an untrusted log line and look up that id in Loki.",
            "Query istio_requests_total by response_code. An empty result is missing telemetry.",
            "Retrieve one trace by id. A missing trace is not success.",
            "Do not delete workloads or change VirtualService weights from this report.",
        ],
    }


def _telemetry(
    payload: dict[str, Any] | None,
    label: str,
    missing: list[str],
    supporting: list[object],
    hypotheses: list[str],
) -> None:
    if payload is None or payload.get("telemetry") == "missing":
        if isinstance(payload, dict) and payload.get("detail"):
            missing.append(f"{label}: {payload['detail']}")
        else:
            missing.append(
                f"{label} was not queried or returned no data. This is not a healthy zero."
            )
        return
    if payload.get("telemetry") == "invalid":
        missing.append(f"{label} rejected the bounded query.")
        return
    if not _has_samples(payload):
        missing.append(f"{label} returned an empty sample. This is not a healthy zero.")
        return
    supporting.append({"source": label, "summary": _summary(payload)})
    series = payload.get("series")
    if isinstance(series, list) and any(_has_server_error(item) for item in series):
        hypotheses.append(
            "Unverified hypothesis: HTTP 5xx samples are present. "
            "They are not a verified root cause."
        )


def _summary(payload: dict[str, Any]) -> str:
    if "series" in payload:
        series = payload.get("series")
        count = len(series) if isinstance(series, list) else 0
        return f"{count} series"
    if "lines" in payload:
        lines = payload.get("lines")
        count = len(lines) if isinstance(lines, list) else 0
        return f"{count} log lines"
    if "batches" in payload:
        return f"{payload.get('batches')} trace batches"
    return payload.get("telemetry", "")


def _has_samples(payload: dict[str, Any]) -> bool:
    series = payload.get("series")
    if isinstance(series, list) and series:
        return True
    lines = payload.get("lines")
    if isinstance(lines, list) and lines:
        return True
    batches = payload.get("batches")
    return isinstance(batches, int) and batches > 0


def _has_server_error(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    metric = item.get("metric")
    code = ""
    if isinstance(metric, dict):
        code = str(metric.get("response_code", ""))
    return code.startswith("5")

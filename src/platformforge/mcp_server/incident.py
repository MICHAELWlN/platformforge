"""Windowed incident metrics. NaN and empty samples stay unset, never zero."""

from __future__ import annotations

from typing import Any

from platformforge.canary import (
    CanaryError,
    counter_requests_query,
    histogram_quantile,
    promql_window,
    requests_query,
)
from platformforge.canary import latency_query as latency_buckets_query
from platformforge.mcp_server.policy import McpError, require_service_name
from platformforge.mcp_server.telemetry import Fetch, http_get, query_prometheus

_UNSET = frozenset({"NaN", "nan", "+Inf", "-Inf", "Inf"})


def query_incident_metrics(
    service: str,
    window_seconds: int,
    *,
    base_url: str | None = None,
    fetch: Fetch = http_get,
) -> dict[str, object]:
    """Compare cumulative counters with one bounded increase window."""
    name = require_service_name(service)
    window = _window(window_seconds)
    try:
        cumulative_query = counter_requests_query(name)
        window_query = requests_query(name, window)
        latency_query = latency_buckets_query(name, window)
    except CanaryError as exc:
        raise McpError(str(exc)) from exc
    duration = f"{promql_window(window)}"
    queries = {
        "cumulative_requests": cumulative_query,
        "windowed_requests": window_query,
        "latency_buckets": latency_query,
        "rollbacks": "platformforge_canary_rollbacks_total",
        "decisions": "platformforge_canary_decisions_total",
        "decisions_window": (
            f"sum by (action) (increase(platformforge_canary_decisions_total[{duration}]))"
        ),
        "state": "platformforge_canary_state",
        "error_ratio": "platformforge_canary_error_ratio",
        "latency_regression": "platformforge_canary_latency_regression",
    }
    fetched = {
        key: query_prometheus(query, base_url=base_url, fetch=fetch)
        for key, query in queries.items()
    }
    first = fetched["cumulative_requests"]
    if first.get("telemetry") == "missing":
        return {
            "service": name,
            "window_seconds": window,
            "telemetry": "missing",
            "detail": first.get("detail"),
            "verified_root_cause": None,
        }
    return {
        "service": name,
        "window_seconds": window,
        "telemetry": "present",
        "cumulative_counters": _ratios(fetched["cumulative_requests"], "cumulative_counter"),
        "windowed_increases": _ratios(fetched["windowed_requests"], "windowed_increase"),
        "rollbacks": _gauge(fetched["rollbacks"], "cumulative_counter"),
        "decisions_cumulative": _labeled(fetched["decisions"], "action", "cumulative_counter"),
        "decisions_window": _labeled(fetched["decisions_window"], "action", "windowed_increase"),
        "state": _state(fetched["state"]),
        "error_ratio": _gauge(fetched["error_ratio"], "gauge"),
        "latency_regression": _gauge(fetched["latency_regression"], "gauge"),
        "latency_p95": _latency(fetched["latency_buckets"]),
        "verified_root_cause": None,
    }


def _window(seconds: int) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError) as exc:
        raise McpError("query window must be from 15 to 900 seconds") from exc
    if not 15 <= value <= 900:
        raise McpError("query window must be from 15 to 900 seconds")
    return value


def _ratios(payload: dict[str, Any], kind: str) -> dict[str, object]:
    if payload.get("telemetry") != "present":
        return _absent(payload, kind)
    if payload.get("truncated"):
        return {
            "kind": kind,
            "sample": "insufficient",
            "note": "series were truncated; the ratio was not calculated and is not zero",
            "versions": [],
        }
    grouped: dict[str, dict[str, float]] = {}
    unset: set[str] = set()
    for row in _rows(payload):
        metric = row.get("metric")
        if not isinstance(metric, dict):
            continue
        version = str(metric.get("destination_version") or "")
        if not version:
            continue
        number, interpretation = _number(row)
        if interpretation == "unset":
            unset.add(version)
            continue
        if number is None:
            continue
        bucket = grouped.setdefault(version, {"total": 0.0, "errors": 0.0})
        bucket["total"] += number
        if str(metric.get("response_code", "")).startswith("5"):
            bucket["errors"] += number
    versions: list[dict[str, object]] = []
    for version in sorted(set(grouped) | unset):
        if version in unset and version not in grouped:
            versions.append(
                {
                    "destination_version": version,
                    "http_5xx_ratio": None,
                    "sample": "unset",
                    "note": "NaN is not zero",
                }
            )
            continue
        bucket = grouped.get(version, {"total": 0.0, "errors": 0.0})
        if bucket["total"] <= 0:
            versions.append(
                {
                    "destination_version": version,
                    "http_5xx_ratio": None,
                    "errors": bucket["errors"],
                    "total": bucket["total"],
                    "sample": "insufficient",
                    "note": "insufficient samples; the ratio is not zero",
                }
            )
            continue
        versions.append(
            {
                "destination_version": version,
                "http_5xx_ratio": bucket["errors"] / bucket["total"],
                "errors": bucket["errors"],
                "total": bucket["total"],
                "sample": "present",
            }
        )
    if not versions:
        return {
            "kind": kind,
            "sample": "missing",
            "note": "no series; this is not a healthy zero",
            "versions": [],
        }
    return {"kind": kind, "sample": "present", "versions": versions}


def _gauge(payload: dict[str, Any], kind: str) -> dict[str, object]:
    if payload.get("telemetry") != "present":
        return _absent(payload, kind)
    rows = _rows(payload)
    if not rows:
        return {
            "kind": kind,
            "sample": "missing",
            "value": None,
            "note": "no series; this is not a healthy zero",
        }
    number, interpretation = _number(rows[0])
    if interpretation == "unset":
        return {"kind": kind, "sample": "unset", "value": None, "note": "NaN is not zero"}
    return {"kind": kind, "sample": "present", "value": number}


def _labeled(payload: dict[str, Any], label: str, kind: str) -> dict[str, object]:
    if payload.get("telemetry") != "present":
        return _absent(payload, kind)
    rows = []
    for row in _rows(payload):
        metric = row.get("metric")
        if not isinstance(metric, dict):
            continue
        number, interpretation = _number(row)
        rows.append(
            {
                label: str(metric.get(label, "")),
                "value": number,
                "sample": interpretation if interpretation != "present" else "present",
                "note": "NaN is not zero" if interpretation == "unset" else "",
            }
        )
    if not rows:
        return {
            "kind": kind,
            "sample": "missing",
            "series": [],
            "note": "no series; this is not a healthy zero",
        }
    return {"kind": kind, "sample": "present", "series": rows}


def _state(payload: dict[str, Any]) -> dict[str, object]:
    gauge = _gauge(payload, "gauge")
    rows = _rows(payload)
    label = ""
    if rows:
        metric = rows[0].get("metric")
        if isinstance(metric, dict):
            label = str(metric.get("state", ""))
    gauge["state_label"] = label
    gauge["note"] = (
        "NaN is not zero"
        if gauge.get("sample") == "unset"
        else "gauge observation; not a VirtualService weight"
    )
    return gauge


def _latency(payload: dict[str, Any]) -> dict[str, object]:
    if payload.get("telemetry") != "present":
        return _absent(payload, "windowed_increase")
    if payload.get("truncated"):
        return {
            "kind": "windowed_increase",
            "sample": "insufficient",
            "note": "histogram series were truncated; p95 was not calculated and is not zero",
            "by_version": [],
        }
    grouped: dict[str, list[tuple[float, float]]] = {}
    unset: set[str] = set()
    for row in _rows(payload):
        metric = row.get("metric")
        if not isinstance(metric, dict):
            continue
        version = str(metric.get("destination_version") or "")
        le = _le(metric.get("le"))
        if not version or le is None:
            continue
        number, interpretation = _number(row)
        if interpretation == "unset":
            unset.add(version)
            continue
        if number is None:
            continue
        grouped.setdefault(version, []).append((le, number))
    by_version: list[dict[str, object]] = []
    for version in sorted(set(grouped) | unset):
        if version in unset and version not in grouped:
            by_version.append(
                {
                    "destination_version": version,
                    "p95_ms": None,
                    "sample": "unset",
                    "note": "NaN is not zero",
                }
            )
            continue
        estimate = histogram_quantile(0.95, grouped.get(version, []))
        if estimate is None:
            by_version.append(
                {
                    "destination_version": version,
                    "p95_ms": None,
                    "sample": "insufficient",
                    "note": "insufficient histogram samples; p95 is not zero",
                }
            )
            continue
        by_version.append({"destination_version": version, "p95_ms": estimate, "sample": "present"})
    if not by_version:
        return {
            "kind": "windowed_increase",
            "sample": "missing",
            "note": "no latency series; this is not a healthy zero",
            "by_version": [],
        }
    return {"kind": "windowed_increase", "sample": "present", "by_version": by_version}


def _absent(payload: dict[str, Any], kind: str) -> dict[str, object]:
    detail = str(payload.get("detail") or payload.get("error") or "telemetry is missing")
    return {
        "kind": kind,
        "sample": "missing" if payload.get("telemetry") != "invalid" else "invalid",
        "note": detail + "; this is not a healthy zero",
    }


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    series = payload.get("series")
    if not isinstance(series, list):
        return []
    return [row for row in series if isinstance(row, dict)]


def _number(row: dict[str, Any]) -> tuple[float | None, str]:
    value = row.get("value")
    raw = value[1] if isinstance(value, list) and len(value) >= 2 else None
    text = str(raw)
    if text in _UNSET:
        return None, "unset"
    try:
        return float(text), "present"
    except (TypeError, ValueError):
        return None, "missing"


def _le(value: object) -> float | None:
    text = str(value)
    if text == "+Inf":
        return float("inf")
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed

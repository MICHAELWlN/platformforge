"""Join logs, traces, and metrics that were actually returned. Never invent ids."""

from __future__ import annotations

import json
import re
from typing import Any

_REQUEST_ID = re.compile(r'"request_id"\s*:\s*"([A-Za-z0-9][A-Za-z0-9._-]{0,63})"')
_TRACE_ID = re.compile(r'"trace_id"\s*:\s*"([0-9a-fA-F]{32})"')
_STATUS_KEYS = frozenset({"http.status_code", "http.response.status_code"})
_SERVICE_KEY = "service.name"
_VERSION_KEY = "service.version"


def correlate_evidence(
    *,
    texts: list[str],
    tempo: dict[str, Any] | None,
    routing: dict[str, Any] | None,
    metrics: dict[str, Any] | None,
) -> dict[str, object]:
    """Classify facts, hypotheses, and gaps from evidence already in hand."""
    combined = "\n".join(texts)
    request_ids = _unique(_REQUEST_ID.findall(combined))
    trace_ids = _unique(item.lower() for item in _TRACE_ID.findall(combined))
    decisions = _rollback_lines(texts)
    spans = list(tempo.get("spans") or []) if isinstance(tempo, dict) else []
    facts: list[str] = []
    hypotheses: list[str] = []
    missing: list[str] = []
    if isinstance(routing, dict) and _weights_present(routing):
        facts.append(
            "VirtualService inventory-service observed weights "
            f"v0-1-0={routing['stable_weight']} v0-2-0={routing['canary_weight']}."
        )
    else:
        missing.append("VirtualService weights were not read. Current routing is unknown.")
    if request_ids:
        facts.append("Request ids present in the retrieved text: " + ", ".join(request_ids) + ".")
    else:
        missing.append("No request_id was present in the retrieved text.")
    if trace_ids:
        facts.append("Trace ids present in the retrieved text: " + ", ".join(trace_ids) + ".")
    else:
        missing.append("No trace_id was present in the retrieved text.")
    if not isinstance(tempo, dict) or tempo.get("telemetry") != "present":
        missing.append("Tempo was not queried or returned no trace. This is not a healthy zero.")
    else:
        _span_facts(spans, facts, missing)
    _metric_facts(metrics, facts, missing)
    supports: list[object] = []
    for decision in decisions:
        supports.append(decision)
        facts.append(
            "Controller log line, untrusted, records action=rollback "
            f"reason={decision['reason']!r} error_ratio={decision['error_ratio']} "
            f"canary_errors={decision['canary_errors']} canary_total={decision['canary_total']}."
        )
    if not decisions:
        missing.append("No parsed controller rollback decision was in the retrieved text.")
    window_has_errors = _window_has_5xx(metrics)
    if decisions and window_has_errors:
        hypotheses.append(
            "Unverified hypothesis: the windowed HTTP 5xx increase is the condition "
            "named by the controller log. The log is untrusted and is not a verified root cause."
        )
    elif window_has_errors:
        hypotheses.append(
            "Unverified hypothesis: HTTP 5xx samples are present. "
            "They are not a verified root cause."
        )
    return {
        "verified_root_cause": None,
        "request_ids": request_ids,
        "trace_ids": trace_ids,
        "facts": facts,
        "hypotheses": hypotheses,
        "missing": missing,
        "rollback_observations": supports,
        "spans": spans,
    }


def spans_from_tempo(payload: dict[str, Any]) -> list[dict[str, object]]:
    """Read HTTP status, service, and version only from attributes that exist."""
    batches = payload.get("batches")
    if not isinstance(batches, list):
        return []
    spans: list[dict[str, object]] = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        service, version = _resource_identity(batch.get("resource"))
        scopes = batch.get("scopeSpans")
        if not isinstance(scopes, list):
            scopes = batch.get("instrumentationLibrarySpans")
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            items = scope.get("spans")
            if not isinstance(items, list):
                continue
            for span in items:
                if isinstance(span, dict):
                    spans.append(_span(span, service, version))
    return spans[:20]


def _span(span: dict[str, Any], service: str | None, version: str | None) -> dict[str, object]:
    attributes = _attribute_map(span.get("attributes"))
    status = _first(attributes, _STATUS_KEYS)
    span_version = attributes.get(_VERSION_KEY) or version
    return {
        "name": str(span.get("name") or ""),
        "service": service,
        "service_sample": "present" if service else "missing",
        "version": span_version,
        "version_sample": "present" if span_version else "missing",
        "http_status": status,
        "http_status_sample": "present" if status else "missing",
    }


def _resource_identity(resource: object) -> tuple[str | None, str | None]:
    if not isinstance(resource, dict):
        return None, None
    attributes = _attribute_map(resource.get("attributes"))
    return attributes.get(_SERVICE_KEY), attributes.get(_VERSION_KEY)


def _attribute_map(raw: object) -> dict[str, str]:
    found: dict[str, str] = {}
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = _otlp_value(item.get("value"))
        if isinstance(key, str) and value is not None:
            found[key] = value
    return found


def _otlp_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue"):
        if key in value and value[key] is not None:
            return str(value[key])
    return None


def _first(attributes: dict[str, str], keys: frozenset[str]) -> str | None:
    for key in keys:
        if key in attributes:
            return attributes[key]
    return None


def _rollback_lines(texts: list[str]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for text in texts:
        for line in text.splitlines():
            if '"action"' not in line or "rollback" not in line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("action") != "rollback":
                continue
            found.append(
                {
                    "untrusted": True,
                    "action": "rollback",
                    "reason": payload.get("reason"),
                    "error_ratio": payload.get("error_ratio"),
                    "canary_errors": payload.get("canary_errors"),
                    "canary_total": payload.get("canary_total"),
                    "episode_age": payload.get("episode_age"),
                }
            )
            if len(found) == 5:
                return found
    return found


def _metric_facts(metrics: dict[str, Any] | None, facts: list[str], missing: list[str]) -> None:
    if not isinstance(metrics, dict) or metrics.get("telemetry") != "present":
        missing.append("Incident metrics were missing. This is not a healthy zero.")
        return
    for key in ("cumulative_counters", "windowed_increases"):
        block = metrics.get(key)
        if not isinstance(block, dict):
            missing.append(f"{key} was not returned.")
            continue
        if block.get("sample") != "present":
            missing.append(str(block.get("note") or f"{key} had no usable sample."))
            continue
        versions = block.get("versions")
        if not isinstance(versions, list):
            continue
        for row in versions:
            if not isinstance(row, dict):
                continue
            if row.get("sample") == "present":
                facts.append(
                    f"{block.get('kind')} {row.get('destination_version')} "
                    f"http_5xx_ratio={row.get('http_5xx_ratio')} "
                    f"errors={row.get('errors')} total={row.get('total')}."
                )
            else:
                missing.append(
                    f"{block.get('kind')} {row.get('destination_version')}: {row.get('note')}"
                )
    for key in ("error_ratio", "latency_regression"):
        block = metrics.get(key)
        if isinstance(block, dict) and block.get("sample") == "unset":
            missing.append(f"{key}: NaN is not zero.")
        elif isinstance(block, dict) and block.get("sample") == "missing":
            missing.append(f"{key}: {block.get('note')}")


def _window_has_5xx(metrics: dict[str, Any] | None) -> bool:
    if not isinstance(metrics, dict):
        return False
    block = metrics.get("windowed_increases")
    if not isinstance(block, dict):
        return False
    versions = block.get("versions")
    if not isinstance(versions, list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("sample") == "present"
        and float(row.get("errors") or 0) > 0
        for row in versions
    )


def _weights_present(routing: dict[str, Any]) -> bool:
    return isinstance(routing.get("stable_weight"), int) and isinstance(
        routing.get("canary_weight"), int
    )


def _unique(values: object) -> list[str]:
    seen: list[str] = []
    if not isinstance(values, list) and not hasattr(values, "__iter__"):
        return seen
    for item in values:  # type: ignore[union-attr]
        text = str(item)
        if text not in seen:
            seen.append(text)
    return seen[:20]


def _span_facts(spans: list[object], facts: list[str], missing: list[str]) -> None:
    if not spans:
        missing.append("Tempo returned no spans. HTTP status was not observed.")
        return
    saw_status = False
    for span in spans:
        if not isinstance(span, dict):
            continue
        if span.get("http_status_sample") == "present":
            saw_status = True
            facts.append(
                "Trace span "
                f"service={span.get('service')} version={span.get('version')} "
                f"http_status={span.get('http_status')}."
            )
        elif span.get("http_status_sample") == "missing":
            missing.append(f"Span {span.get('name') or '(unnamed)'} has no HTTP status attribute.")
        if span.get("version_sample") == "missing":
            missing.append(
                f"Span {span.get('name') or '(unnamed)'} has no service.version attribute."
            )
    if not saw_status:
        missing.append("No trace span included an HTTP status. That absence is not success.")

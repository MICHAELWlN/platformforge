"""Bounded loopback queries for Prometheus, Loki, and Tempo."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from platformforge.mcp_server.policy import (
    MAX_HTTP_BYTES,
    MAX_LOKI_LINES,
    MAX_SINCE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    McpError,
    require_logql,
    require_loopback,
    require_promql,
    require_trace_id,
    require_window,
    untrusted,
)

Fetch = Callable[[str, float], bytes]


def prometheus_url() -> str:
    return os.environ.get("PLATFORMFORGE_PROMETHEUS_URL", "").strip()


def loki_url() -> str:
    return os.environ.get("PLATFORMFORGE_LOKI_URL", "").strip()


def tempo_url() -> str:
    return os.environ.get("PLATFORMFORGE_TEMPO_URL", "").strip()


def http_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read(MAX_HTTP_BYTES + 1)
    except TimeoutError as exc:
        raise McpError("telemetry query timed out") from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise McpError("telemetry query returned 404") from exc
        raise McpError("telemetry query failed") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise McpError("telemetry query timed out") from exc
        raise McpError("telemetry query failed") from exc


def query_prometheus(
    query: str,
    *,
    base_url: str | None = None,
    fetch: Fetch = http_get,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    require_promql(query)
    base = _base(base_url if base_url is not None else prometheus_url(), "prometheus")
    if base is None:
        return _missing("prometheus")
    encoded = urllib.parse.urlencode({"query": query})
    payload = _json(_fetch(fetch, f"{base}/api/v1/query?{encoded}", timeout))
    if payload.get("status") != "success":
        return {
            "source": "prometheus",
            "error": "prometheus rejected the query",
            "telemetry": "invalid",
        }
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    rows = result if isinstance(result, list) else []
    return {
        "source": "prometheus",
        "telemetry": "present",
        "result_type": data.get("resultType") if isinstance(data, dict) else "",
        "series": rows[:20],
        "truncated": len(rows) > 20,
    }


def query_loki(
    query: str,
    *,
    start: int | None = None,
    end: int | None = None,
    now: float | None = None,
    base_url: str | None = None,
    fetch: Fetch = http_get,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    require_logql(query)
    moment = int(now if now is not None else time.time())
    window_end = moment if end is None else end
    window_start = window_end - MAX_SINCE_SECONDS if start is None else start
    require_window(window_start, window_end)
    base = _base(base_url if base_url is not None else loki_url(), "loki")
    if base is None:
        return _missing("loki")
    encoded = urllib.parse.urlencode(
        {
            "query": query,
            "limit": str(MAX_LOKI_LINES),
            "start": str(window_start * 1_000_000_000),
            "end": str(window_end * 1_000_000_000),
        }
    )
    payload = _json(_fetch(fetch, f"{base}/loki/api/v1/query_range?{encoded}", timeout))
    if payload.get("status") != "success":
        return {"source": "loki", "error": "loki rejected the query", "telemetry": "invalid"}
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    lines = _loki_lines(result if isinstance(result, list) else [])
    return {
        "source": "loki",
        "telemetry": "present",
        "lines": [untrusted(line) for line in lines[:MAX_LOKI_LINES]],
        "truncated": len(lines) > MAX_LOKI_LINES,
    }


def query_tempo(
    trace_id: str,
    *,
    base_url: str | None = None,
    fetch: Fetch = http_get,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    trace = require_trace_id(trace_id)
    base = _base(base_url if base_url is not None else tempo_url(), "tempo")
    if base is None:
        return _missing("tempo")
    try:
        payload = _json(_fetch(fetch, f"{base}/api/traces/{trace}", timeout))
    except McpError as exc:
        if "returned 404" in str(exc):
            return {
                "source": "tempo",
                "telemetry": "missing",
                "trace_id": trace,
                "batches": 0,
                "detail": "trace id was not found; this is not a healthy zero",
            }
        raise
    batches = payload.get("batches")
    count = len(batches) if isinstance(batches, list) else 0
    return {
        "source": "tempo",
        "telemetry": "present" if count else "missing",
        "trace_id": trace,
        "batches": count,
    }


def _fetch(fetch: Fetch, url: str, timeout: float) -> bytes:
    try:
        return fetch(url, timeout)
    except TimeoutError as exc:
        raise McpError("telemetry query timed out") from exc


def _base(url: str, source: str) -> str | None:
    if not url:
        return None
    try:
        return require_loopback(url).rstrip("/")
    except McpError:
        raise
    except Exception as exc:
        raise McpError(f"{source} URL is not usable") from exc


def _missing(source: str) -> dict[str, object]:
    return {
        "source": source,
        "telemetry": "missing",
        "detail": f"{source} URL is unset; this is not a healthy zero",
    }


def _json(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_HTTP_BYTES:
        raise McpError("telemetry response exceeded the size limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise McpError("telemetry response was not JSON") from exc
    if not isinstance(payload, dict):
        raise McpError("telemetry response was not an object")
    return payload


def _loki_lines(result: list[Any]) -> list[str]:
    lines: list[str] = []
    for stream in result:
        if not isinstance(stream, dict):
            continue
        values = stream.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, list) and len(value) >= 2:
                lines.append(str(value[1]))
    return lines

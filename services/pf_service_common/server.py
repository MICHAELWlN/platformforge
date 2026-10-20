"""Process entry point helper: configure logging and run uvicorn."""

from __future__ import annotations

import os

import uvicorn

from pf_service_common.logs import configure_logging


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def serve(service: str, app_factory: str) -> None:
    configure_logging(service, os.environ.get("LOG_LEVEL", "info").lower())
    uvicorn.run(
        app_factory,
        factory=True,
        host="0.0.0.0",  # noqa: S104 - must accept traffic from the pod network
        port=env_int("PORT", 8080, 1024, 65535),
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )

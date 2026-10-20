"""FastAPI application scaffolding shared by PlatformForge services."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pf_service_common.requestid import REQUEST_ID_HEADER, accept_or_generate, request_id_var
from pf_service_common.telemetry import configure_telemetry

access_log = logging.getLogger("pf.access")

PROBE_PATHS = frozenset({"/healthz", "/readyz"})
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode()


class RequestContextMiddleware:
    """Assign/propagate a request ID, echo it in the response, and log each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = None
        for key, value in scope.get("headers", []):
            if key == _REQUEST_ID_HEADER_BYTES:
                inbound = value.decode("latin-1")
                break
        request_id = accept_or_generate(inbound)
        token = request_id_var.set(request_id)
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("platformforge.request_id", request_id)
        status = 500
        start = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    *message.get("headers", []),
                    (_REQUEST_ID_HEADER_BYTES, request_id.encode()),
                ]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = scope.get("path", "")
            quiet = path in PROBE_PATHS and status < 400
            access_log.log(
                logging.DEBUG if quiet else logging.INFO,
                "request completed",
                extra={
                    "fields": {
                        "method": scope.get("method"),
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    }
                },
            )
            request_id_var.reset(token)


class VersionInfo(BaseModel):
    service: str
    version: str
    git_sha: str


Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_service_app(service: str, lifespan: Lifespan | None = None) -> FastAPI:
    """Create a FastAPI app with request context middleware and health endpoints.

    ``/readyz`` reports ready only while the app is running (after startup, before
    shutdown). It deliberately does not check downstream services, so an upstream outage
    degrades individual requests instead of removing every replica from the Service.
    """
    version = VersionInfo(
        service=service,
        version=os.environ.get("SERVICE_VERSION", "dev"),
        git_sha=os.environ.get("GIT_SHA", "unknown"),
    )

    @asynccontextmanager
    async def run(app: FastAPI) -> AsyncIterator[None]:
        if lifespan is None:
            app.state.ready = True
            yield
        else:
            async with lifespan(app):
                app.state.ready = True
                yield
        app.state.ready = False

    app = FastAPI(
        title=service,
        version=version.version,
        lifespan=run,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ready = False
    app.add_middleware(RequestContextMiddleware)
    # Added after the request-ID middleware, so the OpenTelemetry middleware runs first
    # and a span is already current when the request ID is attached to it.
    app.state.tracer_provider = configure_telemetry(app, service)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if app.state.ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not ready"}, status_code=503)

    @app.get("/version")
    async def get_version() -> VersionInfo:
        return version

    return app

"""orders-service HTTP API."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ValidationError

from orders_service import SERVICE_NAME
from orders_service.settings import Settings
from pf_service_common.app import create_service_app
from pf_service_common.requestid import REQUEST_ID_HEADER, current_request_id

log = logging.getLogger("orders")

ITEM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class InventoryResponse(BaseModel):
    """The subset of inventory-service's response that orders-service relies on."""

    item_id: str
    quantity: int


class OrderCheck(BaseModel):
    item_id: str
    orderable: bool
    quantity_available: int


def _upstream_error(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": error, "message": message, "request_id": current_request_id()},
    )


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=settings.inventory_url,
            timeout=settings.inventory_timeout_seconds,
            transport=transport,
            follow_redirects=False,
        ) as client:
            app.state.inventory = client
            log.info(
                "orders-service started",
                extra={
                    "fields": {
                        "inventory_url": settings.inventory_url,
                        "inventory_timeout_seconds": settings.inventory_timeout_seconds,
                    }
                },
            )
            yield

    app = create_service_app(SERVICE_NAME, lifespan=lifespan)

    @app.get("/orders/check/{item_id}")
    async def check_order(
        item_id: Annotated[str, Path(pattern=ITEM_ID_PATTERN)],
        request: Request,
    ) -> OrderCheck:
        client: httpx.AsyncClient = request.app.state.inventory
        upstream_path = f"/inventory/{item_id}"
        start = time.perf_counter()
        fields: dict[str, object] = {"upstream": settings.inventory_url, "path": upstream_path}
        try:
            response = await client.get(
                upstream_path, headers={REQUEST_ID_HEADER: current_request_id() or ""}
            )
        except httpx.RequestError as exc:
            fields["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
            fields["error"] = type(exc).__name__
            if isinstance(exc, httpx.TimeoutException):
                log.warning("inventory-service timed out", extra={"fields": fields})
                raise _upstream_error(
                    504, "inventory_timeout", "inventory-service did not respond in time"
                ) from exc
            if isinstance(exc, httpx.TransportError):
                log.warning("inventory-service unavailable", extra={"fields": fields})
                raise _upstream_error(
                    503, "inventory_unavailable", "inventory-service is unavailable"
                ) from exc
            log.warning("inventory-service request failed", extra={"fields": fields})
            raise _upstream_error(
                502, "inventory_bad_response", "inventory-service returned an invalid response"
            ) from exc

        fields["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        fields["status"] = response.status_code
        log.info("inventory-service responded", extra={"fields": fields})

        if response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={"error": "item_not_found", "request_id": current_request_id()},
            )
        if response.status_code != 200:
            raise _upstream_error(
                502, "inventory_bad_status", "inventory-service returned an unexpected status"
            )
        try:
            item = InventoryResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise _upstream_error(
                502, "inventory_bad_response", "inventory-service returned an invalid response"
            ) from exc
        if item.item_id != item_id:
            raise _upstream_error(
                502, "inventory_bad_response", "inventory-service returned a different item"
            )
        return OrderCheck(
            item_id=item_id, orderable=item.quantity > 0, quantity_available=item.quantity
        )

    return app

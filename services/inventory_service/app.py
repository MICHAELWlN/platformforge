"""inventory-service HTTP API."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel

from inventory_service import SERVICE_NAME
from pf_service_common.app import create_service_app
from pf_service_common.server import env_int

log = logging.getLogger("inventory")

ITEM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
FAILURE_PERCENT_ENV = "INVENTORY_FAILURE_PERCENT"
DELAY_MS_ENV = "INVENTORY_DELAY_MS"
MAX_DELAY_MS = 2000

# Static demo catalog. A real service would use a datastore.
CATALOG: dict[str, tuple[str, int]] = {
    "widget": ("Widget", 42),
    "gadget": ("Gadget", 0),
    "gizmo": ("Gizmo", 7),
}


class InventoryItem(BaseModel):
    item_id: str
    name: str
    quantity: int
    in_stock: bool


def create_app(failure_percent: int | None = None, delay_ms: int | None = None) -> FastAPI:
    """Build the app.

    ``failure_percent`` (or ``INVENTORY_FAILURE_PERCENT``, 0-100, default 0) makes that
    share of inventory lookups return HTTP 500. ``delay_ms`` (or ``INVENTORY_DELAY_MS``,
    0-2000, default 0) waits before a successful lookup. Health endpoints are never
    affected. Both exist for canary drills.
    """
    if failure_percent is None:
        failure_percent = env_int(FAILURE_PERCENT_ENV, 0, 0, 100)
    elif not 0 <= failure_percent <= 100:
        raise ValueError("failure_percent must be between 0 and 100")
    if delay_ms is None:
        delay_ms = env_int(DELAY_MS_ENV, 0, 0, MAX_DELAY_MS)
    elif not 0 <= delay_ms <= MAX_DELAY_MS:
        raise ValueError(f"delay_ms must be between 0 and {MAX_DELAY_MS}")
    app = create_service_app(SERVICE_NAME)
    if failure_percent:
        log.warning(
            "failure injection enabled", extra={"fields": {"failure_percent": failure_percent}}
        )
    if delay_ms:
        log.warning("delay injection enabled", extra={"fields": {"delay_ms": delay_ms}})

    @app.get("/inventory/{item_id}")
    async def get_inventory(
        item_id: Annotated[str, Path(pattern=ITEM_ID_PATTERN)],
    ) -> InventoryItem:
        # Not security-sensitive: only decides which drill requests fail.
        if failure_percent and random.uniform(0, 100) < failure_percent:  # noqa: S311
            log.warning("injected failure", extra={"fields": {"item_id": item_id}})
            raise HTTPException(status_code=500, detail={"error": "injected_failure"})
        entry = CATALOG.get(item_id)
        if entry is None:
            log.info("item not found", extra={"fields": {"item_id": item_id}})
            raise HTTPException(status_code=404, detail={"error": "item_not_found"})
        name, quantity = entry
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        return InventoryItem(item_id=item_id, name=name, quantity=quantity, in_stock=quantity > 0)

    return app

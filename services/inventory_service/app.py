"""inventory-service HTTP API."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel

from inventory_service import SERVICE_NAME
from pf_service_common.app import create_service_app

log = logging.getLogger("inventory")

ITEM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

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


def create_app() -> FastAPI:
    app = create_service_app(SERVICE_NAME)

    @app.get("/inventory/{item_id}")
    async def get_inventory(
        item_id: Annotated[str, Path(pattern=ITEM_ID_PATTERN)],
    ) -> InventoryItem:
        entry = CATALOG.get(item_id)
        if entry is None:
            log.info("item not found", extra={"fields": {"item_id": item_id}})
            raise HTTPException(status_code=404, detail={"error": "item_not_found"})
        name, quantity = entry
        return InventoryItem(item_id=item_id, name=name, quantity=quantity, in_stock=quantity > 0)

    return app

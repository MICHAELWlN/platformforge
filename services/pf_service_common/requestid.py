"""Request ID handling and propagation."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"

# Inbound IDs are attacker-controlled; restrict them so they are safe to log and forward.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_or_generate(value: str | None) -> str:
    """Return ``value`` if it is a well-formed request ID, otherwise a new one."""
    if value and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return new_request_id()


def current_request_id() -> str | None:
    return request_id_var.get()

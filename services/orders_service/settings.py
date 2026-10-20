"""orders-service configuration from environment variables."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_INVENTORY_URL = "http://inventory-service:8080"
DEFAULT_TIMEOUT_SECONDS = 2.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Settings:
    inventory_url: str = DEFAULT_INVENTORY_URL
    inventory_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        parts = urlsplit(self.inventory_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("INVENTORY_URL must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("INVENTORY_URL must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("INVENTORY_URL must not contain a query or fragment")
        timeout = self.inventory_timeout_seconds
        if not math.isfinite(timeout) or not (
            MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"INVENTORY_TIMEOUT_SECONDS must be between {MIN_TIMEOUT_SECONDS} "
                f"and {MAX_TIMEOUT_SECONDS}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        raw_timeout = env.get("INVENTORY_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("INVENTORY_TIMEOUT_SECONDS must be a number") from exc
        return cls(
            inventory_url=env.get("INVENTORY_URL", DEFAULT_INVENTORY_URL).rstrip("/"),
            inventory_timeout_seconds=timeout,
        )

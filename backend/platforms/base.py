from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PlatformStatus:
    id: str
    name: str
    enabled: bool
    configured: bool
    authorized: bool
    status: str
    capabilities: list[str]
    next_step: str
    redirect_uri: str
    last_sync_at: float | None = None


class PlatformConnector(Protocol):
    platform_id: str
    name: str
    capabilities: list[str]

    def status(self) -> PlatformStatus:
        """Return current connector readiness without making remote calls."""

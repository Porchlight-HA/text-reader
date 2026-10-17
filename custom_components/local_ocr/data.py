"""Runtime data for the Local OCR config entry."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

from .api import LocalOcrClient

if TYPE_CHECKING:
    from .coordinator import LocalOcrCoordinator


@dataclass
class LocalOcrRuntimeData:
    """Per-entry runtime state."""

    client: LocalOcrClient
    coordinator: LocalOcrCoordinator | None = None
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    last_duration_ms: int | None = None


type LocalOcrConfigEntry = ConfigEntry[LocalOcrRuntimeData]

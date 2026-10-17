"""Runtime data for the Text Reader config entry."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry

from .api import LocalOcrClient

if TYPE_CHECKING:
    from .coordinator import TextReaderCoordinator


@dataclass
class TextReaderRuntimeData:
    """Per-entry runtime state."""

    client: LocalOcrClient
    coordinator: TextReaderCoordinator | None = None
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    last_duration_ms: int | None = None


type TextReaderConfigEntry = ConfigEntry[TextReaderRuntimeData]

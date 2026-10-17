"""Runtime data for the Text Reader config entry."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .api import LocalOcrClient

if TYPE_CHECKING:
    from .coordinator import TextReaderCoordinator


@dataclass
class TextReaderRuntimeData:
    """Per-entry runtime state."""

    client: LocalOcrClient | None
    coordinator: TextReaderCoordinator | None = None
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=20))
    last_duration_ms: int | None = None
    # The platforms set up for this entry; unload uses exactly these (discovery can change the data).
    platforms: list[Platform] = field(default_factory=list)


type TextReaderConfigEntry = ConfigEntry[TextReaderRuntimeData]


@dataclass(frozen=True)
class Settings:
    """Backend settings resolved from options, then entry data, then defaults."""

    backend: str
    ai_entity: str | None
    auto_threshold: float


def settings(entry: ConfigEntry[Any]) -> Settings:
    """Resolve the effective settings for an entry."""
    data, options = entry.data, entry.options
    default_backend = "local" if data.get("url") else "ai_task"
    return Settings(
        backend=options.get("backend", data.get("backend", default_backend)),
        ai_entity=options.get("ai_entity", data.get("ai_entity")),
        auto_threshold=float(options.get("auto_threshold", 0.8)),
    )

"""Diagnostics for Local OCR (never includes image bytes or recognized text)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .data import LocalOcrConfigEntry

TO_REDACT = {"token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LocalOcrConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    status = runtime.coordinator.data if runtime.coordinator else None
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "managed": bool(entry.data.get("managed", False)),
        "info": asdict(status.info) if status and status.info else None,
        "health": asdict(status.health) if status and status.health else None,
        "recent": list(runtime.recent),
    }

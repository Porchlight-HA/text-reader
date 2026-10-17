"""Local OCR integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LocalOcrClient
from .data import LocalOcrConfigEntry, LocalOcrRuntimeData


async def async_setup_entry(hass: HomeAssistant, entry: LocalOcrConfigEntry) -> bool:
    """Set up Local OCR from a config entry."""
    client = LocalOcrClient(
        async_get_clientsession(hass),
        entry.data["url"],
        entry.data.get("token"),
        entry.options.get("timeout", 30),
    )
    entry.runtime_data = LocalOcrRuntimeData(client=client)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LocalOcrConfigEntry) -> bool:
    """Unload a config entry."""
    return True

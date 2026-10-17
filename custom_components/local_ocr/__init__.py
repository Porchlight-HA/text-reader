"""Local OCR integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LocalOcrClient
from .const import DOMAIN
from .coordinator import LocalOcrCoordinator
from .data import LocalOcrConfigEntry, LocalOcrRuntimeData

from .services import async_register_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Local OCR actions."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LocalOcrConfigEntry) -> bool:
    """Set up Local OCR from a config entry."""
    client = LocalOcrClient(
        async_get_clientsession(hass),
        entry.data["url"],
        entry.data.get("token"),
        entry.options.get("timeout", 30),
    )
    entry.runtime_data = LocalOcrRuntimeData(client=client)
    coordinator = LocalOcrCoordinator(hass, entry)
    entry.runtime_data.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LocalOcrConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = getattr(entry, "runtime_data", None)
    if unloaded and runtime is not None and runtime.coordinator is not None:
        runtime.coordinator.clear_issues()
    return unloaded

"""Local OCR integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LocalOcrClient
from .const import DOMAIN
from .data import LocalOcrConfigEntry, LocalOcrRuntimeData

from .services import async_register_services

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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LocalOcrConfigEntry) -> bool:
    """Unload a config entry."""
    return True

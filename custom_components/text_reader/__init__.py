"""Text Reader integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.typing import ConfigType

from .api import LocalOcrClient
from .app_installer import local_supported
from .attachments import cleanup_leftovers, tmp_dir
from .const import DOMAIN
from .coordinator import LOCAL_BACKENDS, TextReaderCoordinator
from .data import TextReaderConfigEntry, TextReaderRuntimeData, settings
from .services import async_register_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Text Reader actions."""
    async_register_services(hass)
    await hass.async_add_executor_job(cleanup_leftovers, tmp_dir(hass))
    return True


def _platforms(entry: TextReaderConfigEntry) -> list[Platform]:
    # The last-duration sensor is local-only: an AI-only entry has no App and gets none.
    if entry.data.get("url"):
        return PLATFORMS
    return [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: TextReaderConfigEntry) -> bool:
    """Set up Text Reader from a config entry."""
    client: LocalOcrClient | None = None
    if entry.data.get("url"):
        client = LocalOcrClient(
            async_get_clientsession(hass),
            entry.data["url"],
            entry.data.get("token"),
            entry.options.get("timeout", 30),
        )
    entry.runtime_data = TextReaderRuntimeData(client=client)
    coordinator = TextReaderCoordinator(hass, entry)
    entry.runtime_data.coordinator = coordinator
    # The hardware gate describes the Supervisor host only (Ruling R9).
    if settings(entry).backend in LOCAL_BACKENDS and is_hassio(hass):
        coordinator.local_unsupported = not (await local_supported(hass))[0]
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data.platforms = _platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, entry.runtime_data.platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TextReaderConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    # Exactly what setup forwarded: discovery may have added the App's url since.
    platforms = runtime.platforms if runtime is not None else _platforms(entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded and runtime is not None and runtime.coordinator is not None:
        runtime.coordinator.clear_issues()
    return unloaded

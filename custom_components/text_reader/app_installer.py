"""Install, start and find the Local OCR app through Supervisor.

Every Supervisor import is lazy: Core and Container installs have no Supervisor, and setup must still
load there. Nothing here logs tokens: discovery config is returned, never logged.
"""

from __future__ import annotations

import asyncio
import logging
import platform
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

_LOGGER = logging.getLogger(__name__)

APP_NAME = "Local OCR"
APP_SLUG = "local_ocr"
APP_REPO_URL = "https://github.com/Porchlight-HA/local-ocr"
DISCOVERY_SERVICE = "text_reader"

SUPPORTED_ARCHES = ("aarch64", "amd64")
MIN_MEM_KB = 1_900_000
MEMINFO_PATH = Path("/proc/meminfo")
POLL_INTERVAL = 5.0
_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}


class InstallFailed(Exception):
    """Supervisor couldn't add, install, start or hear from the app. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _mem_total_kb() -> int | None:
    """MemTotal in kB, or None when it can't be read (not Linux). Runs in an executor."""
    try:
        text = MEMINFO_PATH.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _arch(hass: HomeAssistant) -> str:
    """The system arch: Supervisor's when it has one, else the Python platform's."""
    if is_hassio(hass):
        try:
            from homeassistant.components.hassio import get_info

            arch = (get_info(hass) or {}).get("arch")
        except Exception:  # noqa: BLE001  # not ready yet: fall back to the platform
            arch = None
        if isinstance(arch, str) and arch:
            return arch
    machine = platform.machine()
    return _ARCH_ALIASES.get(machine.lower(), machine.lower())


async def local_supported(hass: HomeAssistant) -> tuple[bool, str]:
    """Can this system run the Local OCR app? Returns (ok, reason), reason "arch", "memory" or ""."""
    if _arch(hass) not in SUPPORTED_ARCHES:
        return False, "arch"
    mem = await hass.async_add_executor_job(_mem_total_kb)
    if mem is not None and mem < MIN_MEM_KB:
        return False, "memory"
    return True, ""


def _is_app(slug: str) -> bool:
    # A store repository prefixes its slugs with a hash ("1a2b3c4d_local_ocr"); a local build doesn't.
    return slug == APP_SLUG or slug.endswith(f"_{APP_SLUG}")


async def _find_app(client: Any) -> Any | None:
    """The app's store entry, preferring an installed one."""
    apps = [a for a in await client.store.addons_list() if _is_app(a.slug)]
    installed = [a for a in apps if a.installed]
    return (installed or apps or [None])[0]


async def installed_app(hass: HomeAssistant) -> str | None:
    """The slug of the installed app, or None (not installed, or Supervisor can't say)."""
    from aiohasupervisor import SupervisorError
    from homeassistant.components.hassio import get_supervisor_client

    try:
        app = await _find_app(get_supervisor_client(hass))
    except SupervisorError as err:
        _LOGGER.debug("Couldn't list Supervisor apps: %s", type(err).__name__)
        return None
    return app.slug if app is not None and app.installed else None


async def _start_if_stopped(hass: HomeAssistant, slug: str) -> None:
    from homeassistant.components.hassio import AddonManager, AddonState

    manager = AddonManager(hass, _LOGGER, APP_NAME, slug)
    info = await manager.async_get_addon_info()
    if info.state is not AddonState.RUNNING:
        await manager.async_start_addon()


async def install_and_start(hass: HomeAssistant) -> str:
    """Add the app repository, install and start the app; return its slug. Raises InstallFailed."""
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import StoreAddRepository
    from homeassistant.components.hassio import (
        AddonError,
        AddonManager,
        get_supervisor_client,
    )

    client = get_supervisor_client(hass)
    stage = "repository"
    try:
        app = await _find_app(client)
        if app is None:
            await client.store.add_repository(StoreAddRepository(repository=APP_REPO_URL))
            app = await _find_app(client)
        if app is None:
            stage = "not_found"
            raise InstallFailed(stage)
        if not app.installed:
            stage = "install"
            await AddonManager(hass, _LOGGER, APP_NAME, app.slug).async_install_addon()
        stage = "start"
        await _start_if_stopped(hass, app.slug)
    except (SupervisorError, AddonError) as err:
        _LOGGER.warning("Couldn't set up the Local OCR app (%s): %s", stage, type(err).__name__)
        raise InstallFailed(stage) from None
    except InstallFailed:
        _LOGGER.warning("Couldn't set up the Local OCR app (%s)", stage)
        raise
    return str(app.slug)


async def start_installed(hass: HomeAssistant) -> None:
    """Best effort: start the installed app if it is stopped. Never raises for Supervisor errors."""
    from aiohasupervisor import SupervisorError
    from homeassistant.components.hassio import AddonError

    slug = await installed_app(hass)
    if slug is None:
        return
    try:
        await _start_if_stopped(hass, slug)
    except (SupervisorError, AddonError) as err:
        _LOGGER.debug("Couldn't start the Local OCR app: %s", type(err).__name__)


async def wait_for_discovery(hass: HomeAssistant, timeout: float = 600) -> dict[str, Any]:
    """Poll Supervisor until the app announces itself; return its config plus the app slug.

    The config is {host, port, token}; "slug" is added from the announcing app. Raises
    InstallFailed("timeout").
    """
    from aiohasupervisor import SupervisorError
    from homeassistant.components.hassio import get_supervisor_client

    client = get_supervisor_client(hass)
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    for item in await client.discovery.list():
                        if item.service == DISCOVERY_SERVICE:
                            return {**item.config, "slug": item.addon}
                except SupervisorError as err:
                    _LOGGER.debug("Supervisor discovery list failed: %s", type(err).__name__)
                await asyncio.sleep(POLL_INTERVAL)
    except TimeoutError:
        raise InstallFailed("timeout") from None

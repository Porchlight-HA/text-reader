"""Diagnostics for Local OCR (never includes image bytes or recognized text)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .data import LocalOcrConfigEntry

TO_REDACT = {"token"}


def _strip_userinfo(url: Any) -> Any:
    """Drop user:password@ from a URL, keeping scheme://host:port/path."""
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LocalOcrConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    status = runtime.coordinator.data if runtime.coordinator else None
    entry_data = async_redact_data(entry.as_dict(), TO_REDACT)
    if "url" in entry_data.get("data", {}):
        entry_data["data"]["url"] = _strip_userinfo(entry_data["data"]["url"])
    return {
        "entry": entry_data,
        "managed": bool(entry.data.get("managed", False)),
        "info": asdict(status.info) if status and status.info else None,
        "health": asdict(status.health) if status and status.health else None,
        "recent": list(runtime.recent),
    }

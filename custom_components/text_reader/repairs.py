"""Repair flows for Text Reader."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)

# Supervisor reports the App started before its server listens (the model is not loaded at start, but s6
# and Python still take a few seconds). Poll again after this long instead of waiting for the next 60 s poll.
REFRESH_AFTER_START = 15


def _schedule_refresh(hass: HomeAssistant, entry: ConfigEntry) -> None:
    @callback
    def _refresh(_now: datetime) -> None:
        if entry.state is not ConfigEntryState.LOADED:
            return
        coordinator = entry.runtime_data.coordinator
        if coordinator is not None:
            hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(
        async_call_later(hass, REFRESH_AFTER_START, HassJob(_refresh, cancel_on_shutdown=True))
    )


class StartAppRepairFlow(ConfirmRepairFlow):
    """Start the stopped Local OCR app."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is None:
            return await super().async_step_confirm(None)
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        from homeassistant.components.hassio import AddonError, AddonManager

        slug = entry.data.get("slug") if entry else None
        if not slug:
            return self.async_abort(reason="cannot_start")
        manager = AddonManager(self.hass, _LOGGER, "Local OCR", slug)
        try:
            await manager.async_start_addon()
        except AddonError:
            return self.async_abort(reason="cannot_start")
        if entry is not None:
            _schedule_refresh(self.hass, entry)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow for a fixable issue."""
    if issue_id == "app_not_running":
        entry_id = (data or {}).get("entry_id")
        return StartAppRepairFlow(entry_id if isinstance(entry_id, str) else None)
    raise ValueError(f"unknown fixable issue {issue_id}")

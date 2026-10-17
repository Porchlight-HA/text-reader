"""Repair flows for Local OCR."""

from __future__ import annotations

import logging

from homeassistant.components.hassio import AddonError, AddonManager
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class StartAppRepairFlow(ConfirmRepairFlow):
    """Start the stopped Local OCR app."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        if user_input is None:
            return await super().async_step_confirm(None)
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        slug = entry.data.get("slug") if entry else None
        if not slug:
            return self.async_abort(reason="cannot_start")
        manager = AddonManager(self.hass, _LOGGER, "Local OCR", slug)
        try:
            await manager.async_start_addon()
        except AddonError:
            return self.async_abort(reason="cannot_start")
        if entry is not None and entry.runtime_data.coordinator is not None:
            await entry.runtime_data.coordinator.async_request_refresh()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow for a fixable issue."""
    if issue_id == "app_not_running":
        return StartAppRepairFlow((data or {}).get("entry_id"))
    raise ValueError(f"unknown fixable issue {issue_id}")

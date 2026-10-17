"""Repair flows for Text Reader."""

from __future__ import annotations

import logging
from datetime import datetime

import voluptuous as vol
from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.selector import BooleanSelector

from .ai_backend import eligible_entities
from .config_flow import _ai_entity_selector
from .data import settings

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


class AiEntityMissingFlow(RepairsFlow):
    """Pick another AI assistant after the one in use was removed."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str | bool] | None = None
    ) -> RepairsFlowResult:
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        eligible = eligible_entities(self.hass)
        if not eligible:
            return self.async_abort(reason="no_ai_entities")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("ai_entity") not in eligible:
                errors["base"] = "ai_entity_invalid"
            elif not user_input.get("privacy_ack"):
                errors["base"] = "privacy_ack_required"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, "ai_entity": user_input["ai_entity"]}
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required(
                        "ai_entity", default=(user_input or {}).get("ai_entity", eligible[0])
                    ): _ai_entity_selector(eligible),
                    vol.Required("privacy_ack", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )


class LocalUnsupportedFlow(RepairsFlow):
    """Offer to switch to the AI assistant when this device can't run the Local OCR app."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    def _entry(self) -> ConfigEntry | None:
        return (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if not settings(entry).ai_entity:
            return self.async_abort(reason="no_ai_assistant")
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                description_placeholders={"entity": settings(entry).ai_entity or ""},
            )
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, "backend": "ai_task"}
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow for a fixable issue."""
    if issue_id == "app_not_running":
        entry_id = (data or {}).get("entry_id")
        return StartAppRepairFlow(entry_id if isinstance(entry_id, str) else None)
    if issue_id == "ai_entity_missing":
        entry_id = (data or {}).get("entry_id")
        return AiEntityMissingFlow(entry_id if isinstance(entry_id, str) else None)
    if issue_id == "local_unsupported_here":
        entry_id = (data or {}).get("entry_id")
        return LocalUnsupportedFlow(entry_id if isinstance(entry_id, str) else None)
    raise ValueError(f"unknown fixable issue {issue_id}")

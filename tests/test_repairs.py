"""Tests for the Local OCR repair flow."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.components.hassio import AddonError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.local_ocr.const import DOMAIN
from custom_components.local_ocr.repairs import (
    REFRESH_AFTER_START,
    async_create_fix_flow,
)


def _entry(hass, **extra):
    e = MockConfigEntry(domain=DOMAIN, data={"url": "http://x:1", "managed": True, **extra})
    e.add_to_hass(hass)
    return e


async def _run(hass, entry):
    flow = await async_create_fix_flow(hass, "app_not_running", {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.issue_id = "app_not_running"
    flow.handler = DOMAIN
    first = await flow.async_step_init()
    assert first["type"] is FlowResultType.FORM
    return await flow.async_step_confirm({})


async def test_fix_flow_starts_addon_when_managed(hass):
    entry = _entry(hass, slug="abc_local_ocr")
    entry.runtime_data = type("R", (), {"coordinator": None})()
    with patch(
        "homeassistant.components.hassio.AddonManager"
    ) as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock()
        result = await _run(hass, entry)
    mgr_cls.assert_called_once()
    assert mgr_cls.call_args.args[3] == "abc_local_ocr"
    mgr_cls.return_value.async_start_addon.assert_awaited_once()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_fix_flow_aborts_on_addon_error(hass):
    entry = _entry(hass, slug="abc_local_ocr")
    with patch("homeassistant.components.hassio.AddonManager") as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock(side_effect=AddonError("x"))
        result = await _run(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_start"


async def test_fix_flow_aborts_when_entry_gone(hass):
    flow = await async_create_fix_flow(hass, "app_not_running", {"entry_id": "missing"})
    flow.hass = hass
    flow.issue_id = "app_not_running"
    flow.handler = DOMAIN
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_start"


async def test_fix_flow_aborts_without_slug(hass):
    result = await _run(hass, _entry(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_start"


async def test_fix_flow_polls_again_once_the_app_had_time_to_start(hass):
    entry = _entry(hass, slug="abc_local_ocr")
    entry.mock_state(hass, ConfigEntryState.LOADED)
    coordinator = type("C", (), {"async_request_refresh": AsyncMock()})()
    entry.runtime_data = type("R", (), {"coordinator": coordinator})()
    with patch("homeassistant.components.hassio.AddonManager") as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock()
        result = await _run(hass, entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Not straight away: the App's server isn't listening yet when Supervisor says it started.
    coordinator.async_request_refresh.assert_not_awaited()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=REFRESH_AFTER_START - 1))
    await hass.async_block_till_done()
    coordinator.async_request_refresh.assert_not_awaited()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=REFRESH_AFTER_START + 1))
    await hass.async_block_till_done()
    coordinator.async_request_refresh.assert_awaited_once()


async def test_delayed_refresh_skipped_after_unload(hass):
    entry = _entry(hass, slug="abc_local_ocr")
    entry.mock_state(hass, ConfigEntryState.LOADED)
    coordinator = type("C", (), {"async_request_refresh": AsyncMock()})()
    entry.runtime_data = type("R", (), {"coordinator": coordinator})()
    with patch("homeassistant.components.hassio.AddonManager") as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock()
        await _run(hass, entry)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=REFRESH_AFTER_START + 1))
    await hass.async_block_till_done()
    coordinator.async_request_refresh.assert_not_awaited()

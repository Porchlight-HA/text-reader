"""Tests for the Local OCR repair flow."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.hassio import AddonError
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.local_ocr.const import DOMAIN
from custom_components.local_ocr.repairs import async_create_fix_flow


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
        "custom_components.local_ocr.repairs.AddonManager"
    ) as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock()
        result = await _run(hass, entry)
    mgr_cls.assert_called_once()
    assert mgr_cls.call_args.args[3] == "abc_local_ocr"
    mgr_cls.return_value.async_start_addon.assert_awaited_once()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_fix_flow_aborts_on_addon_error(hass):
    entry = _entry(hass, slug="abc_local_ocr")
    with patch("custom_components.local_ocr.repairs.AddonManager") as mgr_cls:
        mgr_cls.return_value.async_start_addon = AsyncMock(side_effect=AddonError("x"))
        result = await _run(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_start"

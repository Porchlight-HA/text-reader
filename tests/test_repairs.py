"""Tests for the Text Reader repair flow."""

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

from custom_components.text_reader.const import DOMAIN
from custom_components.text_reader.repairs import (
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


# --- 0.3 flows ---------------------------------------------------------------

from custom_components.text_reader.repairs import AiEntityMissingFlow  # noqa: E402


def _flow(hass, issue_id, entry):
    flow = AiEntityMissingFlow(entry.entry_id) if issue_id == "ai_entity_missing" else None
    assert flow is not None
    flow.hass = hass
    flow.issue_id = issue_id
    flow.handler = DOMAIN
    return flow


async def test_ai_entity_missing_flow_updates_options_and_reloads(hass):
    entry = _entry(hass)
    entry.runtime_data = type("R", (), {"coordinator": None})()
    flow = _flow(hass, "ai_entity_missing", entry)
    with patch(
        "custom_components.text_reader.repairs.eligible_entities", return_value=["ai_task.new"]
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        first = await flow.async_step_init()
        assert first["type"] is FlowResultType.FORM
        no_ack = await flow.async_step_init({"ai_entity": "ai_task.new", "privacy_ack": False})
        assert no_ack["errors"] == {"base": "privacy_ack_required"}
        bad = await flow.async_step_init({"ai_entity": "ai_task.other", "privacy_ack": True})
        assert bad["errors"] == {"base": "ai_entity_invalid"}
        done = await flow.async_step_init({"ai_entity": "ai_task.new", "privacy_ack": True})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ai_entity"] == "ai_task.new"
    reload.assert_called_once_with(entry.entry_id)


async def test_ai_entity_missing_flow_aborts_without_candidates(hass):
    entry = _entry(hass)
    flow = _flow(hass, "ai_entity_missing", entry)
    with patch("custom_components.text_reader.repairs.eligible_entities", return_value=[]):
        result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ai_entities"


async def test_local_unsupported_without_ai_entity_just_explains(hass):
    entry = _entry(hass)
    flow = await async_create_fix_flow(hass, "local_unsupported_here", {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.issue_id = "local_unsupported_here"
    flow.handler = DOMAIN
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ai_assistant"
    assert "backend" not in entry.options

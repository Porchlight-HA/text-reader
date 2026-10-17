"""Tests for the status coordinator, repairs and entities."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.text_reader.const import DOMAIN

URL = "http://ocr.test:8738"
INFO = {"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"}
HEALTH = {"status": "ok", "model_state": "ready", "detail": None,
          "memory": {"oom_kills": 0, "last_oom": None}}
READ = {"text": "synthetic", "ms": 321, "blocks": [], "fields": {}}


async def _setup(hass, mock, *, managed=False, slug=None, info=INFO, health=HEALTH):
    mock.get(f"{URL}/v1/info", json=info)
    mock.get(f"{URL}/v1/health", json=health)
    mock.post(f"{URL}/v1/read", json=READ)
    data = {"url": URL, "token": "tok", "managed": managed}
    if slug:
        data["slug"] = slug
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass, n=1):
    for _ in range(n):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done()


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def test_three_failures_raise_app_not_running(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, managed=True, slug="abc_local_ocr")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass, 2)
    assert _issue(hass, "app_not_running") is None
    await _poll(hass, 1)
    issue = _issue(hass, "app_not_running")
    assert issue.is_fixable is True
    assert issue.data == {"entry_id": entry.entry_id}
    state = hass.states.get("binary_sensor.text_reader_status")
    assert state.state == "off"


async def test_recovery_clears_issue(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, managed=True, slug="s")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass, 3)
    assert _issue(hass, "app_not_running")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", json=HEALTH)
    await _poll(hass)
    assert _issue(hass, "app_not_running") is None
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"


async def test_app_outdated_issue(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, info={**INFO, "api": 0})
    issue = _issue(hass, "app_outdated")
    assert issue is not None and issue.is_fixable is False


async def test_model_failed_issue_has_detail(hass, aioclient_mock):
    await _setup(
        hass, aioclient_mock,
        health={"status": "ok", "model_state": "error", "detail": "bad onnx file",
                "memory": {"oom_kills": 0, "last_oom": None}},
    )
    issue = _issue(hass, "model_failed")
    assert issue.translation_placeholders == {"detail": "bad onnx file"}
    assert issue.is_fixable is False
    # The server still answers (status "ok"), so the connectivity sensor stays on; the repair carries the error.
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"


def _health(model_state="ready", detail=None, oom_kills=0, last_oom=None):
    return {"status": "ok", "model_state": model_state, "detail": detail,
            "memory": {"oom_kills": oom_kills, "last_oom": last_oom}}


async def _set_health(hass, mock, health):
    mock.clear_requests()
    mock.get(f"{URL}/v1/info", json=INFO)
    mock.get(f"{URL}/v1/health", json=health)
    await _poll(hass)


async def test_model_load_oom_shows_low_memory_instead_of_model_failed(hass, aioclient_mock):
    # What the server reports when loading the model runs out of memory.
    await _setup(hass, aioclient_mock, health=_health(
        "error", "out of memory: Failed to allocate memory for requested buffer"))
    assert _issue(hass, "low_memory") is not None
    assert _issue(hass, "model_failed") is None


async def test_recent_inference_oom_raises_low_memory(hass, aioclient_mock):
    recent = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    await _setup(hass, aioclient_mock, health=_health("ready", last_oom=recent))
    assert _issue(hass, "low_memory") is not None
    assert _issue(hass, "model_failed") is None


async def test_old_inference_oom_is_ignored(hass, aioclient_mock):
    old = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    await _setup(hass, aioclient_mock, health=_health("unloaded", last_oom=old))
    assert _issue(hass, "low_memory") is None


async def test_oom_kill_since_load_raises_low_memory_then_clears(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, health=_health("ready", oom_kills=4))
    assert _issue(hass, "low_memory") is None  # kills before the entry loaded don't count
    # The kernel killed the server; s6 restarted it, so the model is unloaded and there's no detail.
    await _set_health(hass, aioclient_mock, _health("unloaded", oom_kills=5))
    assert _issue(hass, "low_memory") is not None
    assert _issue(hass, "model_failed") is None
    await _set_health(hass, aioclient_mock, _health("ready", oom_kills=5))
    assert _issue(hass, "low_memory") is not None  # still within the hour
    later = dt_util.utcnow() + timedelta(hours=1, minutes=2)
    with patch("custom_components.text_reader.coordinator.dt_util.utcnow", return_value=later):
        await _set_health(hass, aioclient_mock, _health("ready", oom_kills=5))
    assert _issue(hass, "low_memory") is None


async def test_non_memory_load_error_is_model_failed_only(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, health=_health("error", "bad onnx file"))
    assert _issue(hass, "model_failed") is not None
    assert _issue(hass, "low_memory") is None


async def test_old_server_without_memory_object(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, health={"status": "ok", "model_state": "ready", "detail": None})
    assert _issue(hass, "low_memory") is None


async def test_unmanaged_issue_not_fixable(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, managed=False)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass, 3)
    issue = _issue(hass, "app_not_running")
    assert issue.is_fixable is False
    assert issue.translation_key == "app_not_running_unmanaged"


async def test_managed_without_slug_not_fixable(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, managed=True)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass, 3)
    assert _issue(hass, "app_not_running").is_fixable is False


async def test_401_triggers_reauth(hass, aioclient_mock):
    await _setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", status=401, json={"error": "unauthorized"})
    await _poll(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_401_never_raises_app_not_running(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, managed=True, slug="s")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", status=401, json={"error": "unauthorized"})
    await _poll(hass, 4)
    assert _issue(hass, "app_not_running") is None
    flows = [
        f for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"]["source"] == "reauth"
    ]
    assert len(flows) == 1


async def test_status_entity_on_off(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    reg = er.async_get(hass)
    ent = reg.async_get("binary_sensor.text_reader_status")
    assert ent.unique_id == f"{entry.entry_id}_status"
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"
    assert hass.states.get("binary_sensor.text_reader_status").attributes["device_class"] == "connectivity"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass)
    assert hass.states.get("binary_sensor.text_reader_status").state == "off"


async def test_last_duration_updates_after_action(hass, aioclient_mock, tmp_path):
    from io import BytesIO

    from PIL import Image

    entry = await _setup(hass, aioclient_mock)
    reg = er.async_get(hass)
    ent = reg.async_get("sensor.text_reader_last_duration")
    assert ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    reg.async_update_entity("sensor.text_reader_last_duration", disabled_by=None)
    with patch.object(hass.config, "is_allowed_path", return_value=True):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        p = tmp_path / "a.png"
        buf = BytesIO()
        Image.new("RGB", (50, 50), "white").save(buf, "PNG")
        p.write_bytes(buf.getvalue())
        await hass.services.async_call(
            DOMAIN, "read_image", {"file_path": str(p)}, blocking=True, return_response=True
        )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.text_reader_last_duration")
    assert state.state == "321"
    assert state.attributes["unit_of_measurement"] == "ms"


async def test_unload_clears_issues(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, info={**INFO, "api": 0})
    assert _issue(hass, "app_outdated")
    await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, "app_outdated") is None


async def test_read_401_starts_reauth(hass, aioclient_mock, tmp_path):
    from homeassistant.exceptions import HomeAssistantError

    await _setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=401, json={"error": "unauthorized"})
    p = tmp_path / "a.png"
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "PNG")
    p.write_bytes(buf.getvalue())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "read_image", {"file_path": str(p)}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "auth_failed"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


# --- backend-aware status and repairs (0.3) ---------------------------------

AI = "ai_task.helper"


def _ai_entity(hass, state="idle"):
    er.async_get(hass).async_get_or_create(
        "ai_task", "test", "helper", suggested_object_id="helper"
    )
    hass.states.async_set(AI, state)


async def _setup_entry(hass, mock, data, options=None):
    mock.get(f"{URL}/v1/info", json=INFO)
    mock.get(f"{URL}/v1/health", json=HEALTH)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader", data=data, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _remove_ai(hass):
    er.async_get(hass).async_remove(AI)
    hass.states.async_remove(AI)


async def test_ai_entity_removed_raises_missing_and_auto_keeps_local(hass, aioclient_mock):
    _ai_entity(hass)
    entry = await _setup_entry(
        hass, aioclient_mock, {"url": URL, "token": "t"},
        {"backend": "auto", "ai_entity": AI},
    )
    assert _issue(hass, "ai_entity_missing") is None
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"
    _remove_ai(hass)
    await _poll(hass)
    issue = _issue(hass, "ai_entity_missing")
    assert issue is not None and issue.is_fixable is True
    assert issue.translation_placeholders == {"entity": AI}
    assert issue.data == {"entry_id": entry.entry_id}
    # auto keeps polling the App, but the status is on only when every backend is healthy
    assert aioclient_mock.mock_calls[-1][1].path == "/v1/health"
    assert hass.states.get("binary_sensor.text_reader_status").state == "off"
    _ai_entity(hass)
    await _poll(hass)
    assert _issue(hass, "ai_entity_missing") is None
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"


async def test_ai_unavailable_three_polls(hass, aioclient_mock):
    _ai_entity(hass)
    await _setup_entry(
        hass, aioclient_mock, {"url": URL, "token": "t"},
        {"backend": "auto", "ai_entity": AI},
    )
    hass.states.async_set(AI, "unavailable")
    await _poll(hass, 2)
    assert _issue(hass, "ai_entity_unavailable") is None
    await _poll(hass)
    issue = _issue(hass, "ai_entity_unavailable")
    assert issue is not None and issue.is_fixable is False
    assert issue.translation_key == "ai_entity_unavailable"
    assert issue.translation_placeholders == {"entity": AI}
    hass.states.async_set(AI, "idle")
    await _poll(hass)
    assert _issue(hass, "ai_entity_unavailable") is None


async def test_ai_unavailable_ai_only_uses_its_own_copy_key(hass, aioclient_mock):
    _ai_entity(hass)
    await _setup_entry(hass, aioclient_mock, {"backend": "ai_task", "ai_entity": AI})
    hass.states.async_set(AI, "unavailable")
    await _poll(hass, 3)
    assert _issue(hass, "ai_entity_unavailable").translation_key == "ai_entity_unavailable_only"


async def test_ai_only_entry_has_no_local_polling(hass, aioclient_mock):
    _ai_entity(hass)
    entry = await _setup_entry(hass, aioclient_mock, {"backend": "ai_task", "ai_entity": AI})
    assert entry.runtime_data.client is None
    assert entry.runtime_data.coordinator is not None
    await _poll(hass, 2)
    assert aioclient_mock.call_count == 0
    assert hass.states.get("binary_sensor.text_reader_status").state == "on"
    assert hass.states.get("sensor.text_reader_last_duration") is None
    ent = er.async_get(hass)
    assert ent.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_duration") is None
    hass.states.async_set(AI, "unavailable")
    await _poll(hass)
    assert hass.states.get("binary_sensor.text_reader_status").state == "off"


async def test_local_issues_not_raised_for_ai_only(hass, aioclient_mock):
    _ai_entity(hass)
    await _setup_entry(hass, aioclient_mock, {"backend": "ai_task", "ai_entity": AI})
    for issue_id in ("app_not_running", "app_outdated", "model_failed", "low_memory"):
        assert _issue(hass, issue_id) is None


async def test_local_unsupported_issue_and_switch_fix(hass, aioclient_mock):
    from custom_components.text_reader.repairs import async_create_fix_flow

    _ai_entity(hass)
    with (
        patch("custom_components.text_reader.is_hassio", return_value=True),
        patch(
            "custom_components.text_reader.local_supported",
            return_value=(False, "arch"),
        ),
    ):
        entry = await _setup_entry(
            hass, aioclient_mock, {"url": URL, "token": "t"},
            {"backend": "local", "ai_entity": AI},
        )
    issue = _issue(hass, "local_unsupported_here")
    assert issue is not None and issue.is_fixable is True
    flow = await async_create_fix_flow(hass, "local_unsupported_here", issue.data)
    flow.hass = hass
    flow.issue_id = "local_unsupported_here"
    flow.handler = DOMAIN
    first = await flow.async_step_init()
    assert first["type"].value == "form"
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        done = await flow.async_step_confirm({})
    assert done["type"].value == "create_entry"
    assert entry.options["backend"] == "ai_task"
    reload.assert_called_once_with(entry.entry_id)


async def test_local_unsupported_not_checked_off_supervisor(hass, aioclient_mock):
    with (
        patch("custom_components.text_reader.is_hassio", return_value=False),
        patch(
            "custom_components.text_reader.local_supported", return_value=(False, "arch")
        ) as check,
    ):
        await _setup_entry(hass, aioclient_mock, {"url": URL, "token": "t"})
    check.assert_not_called()
    assert _issue(hass, "local_unsupported_here") is None


async def test_status_sensor_reflects_all_backends(hass, aioclient_mock):
    _ai_entity(hass)
    await _setup_entry(
        hass, aioclient_mock, {"url": URL, "token": "t"},
        {"backend": "auto", "ai_entity": AI},
    )
    sensor = "binary_sensor.text_reader_status"
    assert hass.states.get(sensor).state == "on"
    hass.states.async_set(AI, "unavailable")
    await _poll(hass)
    assert hass.states.get(sensor).state == "off"
    hass.states.async_set(AI, "idle")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass)
    assert hass.states.get(sensor).state == "off"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", json=HEALTH)
    await _poll(hass)
    assert hass.states.get(sensor).state == "on"


async def test_unload_clears_ai_issues(hass, aioclient_mock):
    _ai_entity(hass)
    entry = await _setup_entry(hass, aioclient_mock, {"backend": "ai_task", "ai_entity": AI})
    _remove_ai(hass)
    await _poll(hass)
    assert _issue(hass, "ai_entity_missing")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, "ai_entity_missing") is None

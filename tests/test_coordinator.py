"""Tests for the status coordinator, repairs and entities."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.local_ocr.const import DOMAIN

URL = "http://ocr.test:8738"
INFO = {"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"}
HEALTH = {"status": "ok", "model_state": "loaded"}
READ = {"text": "synthetic", "ms": 321, "blocks": [], "fields": {}}


async def _setup(hass, mock, *, managed=False, slug=None, info=INFO, health=HEALTH):
    mock.get(f"{URL}/v1/info", json=info)
    mock.get(f"{URL}/v1/health", json=health)
    mock.post(f"{URL}/v1/read", json=READ)
    data = {"url": URL, "token": "tok", "managed": managed}
    if slug:
        data["slug"] = slug
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR", data=data)
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
    state = hass.states.get("binary_sensor.local_ocr_status")
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
    assert hass.states.get("binary_sensor.local_ocr_status").state == "on"


async def test_app_outdated_issue(hass, aioclient_mock):
    await _setup(hass, aioclient_mock, info={**INFO, "api": 0})
    issue = _issue(hass, "app_outdated")
    assert issue is not None and issue.is_fixable is False


async def test_model_failed_issue_has_detail(hass, aioclient_mock):
    await _setup(
        hass, aioclient_mock,
        health={"status": "degraded", "model_state": "error", "detail": "bad onnx file"},
    )
    issue = _issue(hass, "model_failed")
    assert issue.translation_placeholders == {"detail": "bad onnx file"}
    assert issue.is_fixable is False
    assert hass.states.get("binary_sensor.local_ocr_status").state == "off"


async def test_low_memory_issue(hass, aioclient_mock):
    await _setup(
        hass, aioclient_mock,
        health={"status": "ok", "model_state": "loaded", "detail": "low memory"},
    )
    assert _issue(hass, "low_memory") is not None


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
    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", status=401, json={"error": "unauthorized"})
    await _poll(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_status_entity_on_off(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock)
    reg = er.async_get(hass)
    ent = reg.async_get("binary_sensor.local_ocr_status")
    assert ent.unique_id == f"{entry.entry_id}_status"
    assert hass.states.get("binary_sensor.local_ocr_status").state == "on"
    assert hass.states.get("binary_sensor.local_ocr_status").attributes["device_class"] == "connectivity"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError)
    await _poll(hass)
    assert hass.states.get("binary_sensor.local_ocr_status").state == "off"


async def test_last_duration_updates_after_action(hass, aioclient_mock, tmp_path):
    from io import BytesIO
    from PIL import Image

    entry = await _setup(hass, aioclient_mock)
    reg = er.async_get(hass)
    ent = reg.async_get("sensor.local_ocr_last_duration")
    assert ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    reg.async_update_entity("sensor.local_ocr_last_duration", disabled_by=None)
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
    state = hass.states.get("sensor.local_ocr_last_duration")
    assert state.state == "321"
    assert state.attributes["unit_of_measurement"] == "ms"


async def test_unload_clears_issues(hass, aioclient_mock):
    entry = await _setup(hass, aioclient_mock, info={**INFO, "api": 0})
    assert _issue(hass, "app_outdated")
    await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, "app_outdated") is None


async def test_read_401_starts_reauth(hass, aioclient_mock, tmp_path):
    import pytest
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
    assert err.value.translation_key == "app_unavailable"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)

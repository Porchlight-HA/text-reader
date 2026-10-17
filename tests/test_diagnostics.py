"""Tests for Text Reader diagnostics."""

import json
from io import BytesIO

from homeassistant.components import camera as _camera  # noqa: F401
from homeassistant.components import image as _image  # noqa: F401
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.text_reader.const import DOMAIN
from custom_components.text_reader.diagnostics import (
    async_get_config_entry_diagnostics,
)

URL = "http://ocr.test:8738"
SECRET_TEXT = "JORDAN SAMPLE"
RESULT = {
    "text": SECRET_TEXT,
    "ms": 12,
    "width": 200,
    "height": 100,
    "blocks": [{"text": SECRET_TEXT, "confidence": 0.9, "box": []}],
    "fields": {},
}


async def _setup(hass, aioclient_mock):
    aioclient_mock.get(
        f"{URL}/v1/info",
        json={"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"},
    )
    aioclient_mock.get(
        f"{URL}/v1/health", json={"status": "ok", "model_state": "ready"}
    )
    aioclient_mock.post(f"{URL}/v1/read", json=RESULT)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Text Reader",
        data={"url": URL, "token": "s3cret-token"},
        options={"preset": "text", "min_confidence": 0.5, "timeout": 30},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_diagnostics_redacts_token(hass, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "s3cret-token" not in json.dumps(diag)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    assert diag["managed"] is False
    assert diag["info"]["api"] == 1
    assert diag["health"]["model_state"] == "ready"
    assert diag["recent"] == []


async def test_diagnostics_never_contains_text(
    hass, aioclient_mock, tmp_path
) -> None:
    entry = await _setup(hass, aioclient_mock)
    buf = BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, "PNG")
    p = tmp_path / "a.png"
    p.write_bytes(buf.getvalue())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await hass.services.async_call(
        DOMAIN, "read_image", {"file_path": str(p)}, blocking=True,
        return_response=True,
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert len(diag["recent"]) == 1
    rec = diag["recent"][0]
    assert set(rec) == {"ms", "preset", "width", "height", "blocks", "error", "backend", "routed"}
    assert rec["blocks"] == 1
    assert rec["error"] is None
    assert (rec["width"], rec["height"]) == (200, 100)
    assert "JORDAN" not in json.dumps(diag)


async def test_failed_read_recorded_with_error_class(
    hass, aioclient_mock, tmp_path
) -> None:
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{URL}/v1/read", status=415,
        json={"error": "unsupported_image", "detail": "x"},
    )
    p = tmp_path / "a.png"
    p.write_bytes(b"not an image")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "read_image", {"file_path": str(p)}, blocking=True,
            return_response=True,
        )
    rec = (await async_get_config_entry_diagnostics(hass, entry))["recent"][0]
    assert rec["error"] == "ImageRejected"
    assert rec["width"] is None and rec["blocks"] == 0


async def test_diagnostics_strips_url_credentials(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        "http://ocr.test:8738/v1/info",
        json={"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"},
    )
    aioclient_mock.get(
        "http://ocr.test:8738/v1/health",
        json={"status": "ok", "model_state": "ready"},
    )
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
        data={"url": "http://user:hunter2pw@ocr.test:8738/base", "token": "t"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "hunter2pw" not in json.dumps(diag)
    assert diag["entry"]["data"]["url"] == "http://ocr.test:8738/base"


async def test_diagnostics_includes_backend_no_text(hass, aioclient_mock) -> None:
    from custom_components.text_reader.ai_backend import AI_PROMPT_VERSION

    aioclient_mock.get(
        f"{URL}/v1/info",
        json={"version": "0.3.0", "api": 1, "arch": "amd64", "engine": "rapidocr"},
    )
    aioclient_mock.get(
        f"{URL}/v1/health", json={"status": "ok", "model_state": "ready"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Text Reader",
        data={"url": URL, "token": "s3cret-token"},
        options={
            "backend": "auto",
            "ai_entity": "ai_task.test_assistant",
            "auto_threshold": 0.7,
            "privacy_ack": True,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.recent.append(
        {"ms": 5, "preset": "text", "width": 1, "height": 1, "blocks": 0,
         "error": None, "backend": "auto", "routed": "local→ai_task"}
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["backend"] == "auto"
    assert diag["ai_entity"] == "ai_task.test_assistant"
    assert diag["auto_threshold"] == 0.7
    assert diag["ai_prompt_version"] == AI_PROMPT_VERSION
    assert diag["recent"][0]["routed"] == "local→ai_task"
    assert "s3cret-token" not in json.dumps(diag)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"

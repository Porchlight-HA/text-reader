"""Tests for the local_ocr.read_image action."""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from homeassistant.components import camera as _camera  # noqa: F401
from homeassistant.components import image as _image  # noqa: F401
from homeassistant.setup import async_setup_component

from custom_components.local_ocr.api import Busy, LocalOcrClient
from custom_components.local_ocr.const import DOMAIN

URL = "http://ocr.test:8738"
RESULT = {"text": "synthetic", "ms": 12, "blocks": [], "fields": {}}


def _png(w=200, h=100) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def entry(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", json={"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"})
    aioclient_mock.get(f"{URL}/v1/health", json={"status": "ok", "model_state": "loaded"})
    aioclient_mock.post(f"{URL}/v1/read", json=RESULT)
    e = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
        data={"url": URL, "token": "tok"},
        options={"preset": "text", "min_confidence": 0.5, "timeout": 30},
    )
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.mock_calls.clear()  # drop the coordinator's setup polls
    return e


async def _call(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "read_image", data, blocking=True, return_response=True
    )


async def test_read_image_from_file_returns_response(hass, entry, aioclient_mock, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    assert await _call(hass, file_path=str(p)) == RESULT
    assert aioclient_mock.call_count == 1


async def test_two_sources_rejected(hass, entry):
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path="/x.png", camera_entity="camera.a")
    assert err.value.translation_key == "exactly_one_source"
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass)
    assert err.value.translation_key == "exactly_one_source"


async def test_not_loaded(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path="/x.png")
    assert err.value.translation_key == "not_loaded"


async def test_path_not_allowed(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = set()
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "path_not_allowed"


async def test_camera_no_image(hass, entry):
    with patch("homeassistant.components.camera.async_get_image",
               AsyncMock(side_effect=HomeAssistantError("nope"))):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, camera_entity="camera.porch")
    assert err.value.translation_key == "no_image"


async def test_image_entity_source(hass, entry, aioclient_mock):
    img = MagicMock(content=_png())
    with patch("homeassistant.components.image.async_get_image",
               AsyncMock(return_value=img)) as m:
        assert await _call(hass, image_entity="image.mailbox") == RESULT
    assert m.call_args.args[1] == "image.mailbox"


async def test_media_source_local(hass, entry, tmp_path):
    p = tmp_path / "m.png"
    p.write_bytes(_png())
    resolved = MagicMock(path=p, url="/x")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)):
        assert await _call(hass, media_content_id="media-source://media_source/local/m.png") == RESULT


async def test_server_415_maps_image_rejected(hass, entry, aioclient_mock, tmp_path):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=415,
                        json={"error": "unsupported_image", "message": "x"})
    p = tmp_path / "a.png"
    p.write_bytes(b"junk")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "image_rejected"
    assert err.value.translation_placeholders == {"code": "unsupported_image"}


async def test_busy_retries_then_succeeds(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    sleep = AsyncMock()
    read = AsyncMock(side_effect=[Busy(5), Busy(5), RESULT])
    with patch("custom_components.local_ocr.services.sleep", sleep), \
            patch.object(LocalOcrClient, "read", read):
        assert await _call(hass, file_path=str(p)) == RESULT
    assert [c.args[0] for c in sleep.call_args_list] == [5, 5]
    assert read.call_count == 3


async def test_busy_until_timeout_raises_timeout(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    clock = [0.0]

    async def fake_sleep(d):
        clock[0] += d

    read = AsyncMock(side_effect=Busy(5))
    with patch("custom_components.local_ocr.services.sleep", fake_sleep), \
            patch("custom_components.local_ocr.services.monotonic", lambda: clock[0]), \
            patch.object(LocalOcrClient, "read", read):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=str(p))
    assert err.value.translation_key == "timeout"
    assert err.value.translation_placeholders == {"timeout": "30"}
    assert read.call_count == 7


async def test_busy_budget_counts_request_time(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    clock = [0.0]

    async def slow_read(*a, **k):
        clock[0] += 20
        raise Busy(5)

    with patch("custom_components.local_ocr.services.sleep", AsyncMock()), \
            patch("custom_components.local_ocr.services.monotonic", lambda: clock[0]), \
            patch.object(LocalOcrClient, "read", slow_read):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=str(p))
    assert err.value.translation_key == "timeout"


async def test_busy_retry_after_clamped(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    sleep = AsyncMock()
    with patch("custom_components.local_ocr.services.sleep", sleep), \
            patch.object(LocalOcrClient, "read", AsyncMock(side_effect=[Busy(0), RESULT])):
        await _call(hass, file_path=str(p))
    assert sleep.call_args.args[0] == 0.5


async def test_media_source_remote_absolute(hass, entry, aioclient_mock):
    aioclient_mock.get("http://media.test/p.png", content=_png())
    resolved = MagicMock(path=None, url="http://media.test/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)):
        assert await _call(hass, media_content_id="media-source://x/p") == RESULT


async def test_media_source_remote_relative(hass, entry, aioclient_mock):
    aioclient_mock.get("http://ha.test:8123/api/p.png", content=_png())
    resolved = MagicMock(path=None, url="/api/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)), \
            patch("custom_components.local_ocr.services.get_url",
                  return_value="http://ha.test:8123"):
        assert await _call(hass, media_content_id="media-source://x/p") == RESULT


async def test_media_source_relative_no_url(hass, entry):
    from homeassistant.helpers.network import NoURLAvailableError

    resolved = MagicMock(path=None, url="/api/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)), \
            patch("custom_components.local_ocr.services.get_url",
                  side_effect=NoURLAvailableError):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, media_content_id="media-source://x/p")
    assert err.value.translation_key == "no_image"


@pytest.mark.parametrize("region", [
    {"x": -1, "y": 0, "w": 10, "h": 10},
    {"x": 0, "y": -5, "w": 10, "h": 10},
    {"x": 0, "y": 0, "w": 0, "h": 10},
    {"x": 0, "y": 0, "w": 10, "h": 0},
    {"x": 0.5, "y": 0, "w": 300, "h": 200},
    {"x": 10, "y": 10, "w": 0.5, "h": 200},
    {"x": 0, "y": 0, "w": 0.001, "h": 0.5},
])
async def test_invalid_region_rejected(hass, entry, aioclient_mock, tmp_path, region):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path=str(p), region=region)
    assert err.value.translation_key == "invalid_region"
    assert aioclient_mock.call_count == 0


async def test_unknown_preset_rejected(hass, entry):
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        await _call(hass, file_path="/x.png", preset="bogus")


async def test_region_fraction_converted_to_px(hass, entry, aioclient_mock, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png(200, 100))
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _call(hass, file_path=str(p), region={"x": 0.5, "y": 0.0, "w": 0.5, "h": 1})
    body = aioclient_mock.mock_calls[-1][2]
    assert _field(body, "region") == "100,0,100,100"


async def test_region_px_passthrough(hass, entry, aioclient_mock, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _call(hass, file_path=str(p), region={"x": 10, "y": 20, "w": 30, "h": 40})
    assert _field(aioclient_mock.mock_calls[-1][2], "region") == "10,20,30,40"


async def test_all_frames_passthrough(hass, entry, aioclient_mock, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _call(hass, file_path=str(p), all_frames=True, preset="envelope", min_confidence=0.7)
    body = aioclient_mock.mock_calls[-1][2]
    assert _field(body, "all_frames") == "true"
    assert _field(body, "preset") == "envelope"
    assert _field(body, "min_confidence") == "0.7"


def _field(form, name):
    for opts, _hdrs, value in form._fields:
        if opts.get("name") == name:
            return value
    raise AssertionError(name)

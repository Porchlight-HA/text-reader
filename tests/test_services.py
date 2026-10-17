"""Tests for the text_reader.read_image action."""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components import camera as _camera  # noqa: F401
from homeassistant.components import image as _image  # noqa: F401
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.text_reader.api import Busy, LocalOcrClient, Timeout
from custom_components.text_reader.const import DOMAIN

URL = "http://ocr.test:8738"
RESULT = {"text": "synthetic", "ms": 12, "blocks": [], "fields": {}}
LOCAL_RESULT = {**RESULT, "routed": "local", "confidence": None}


def _png(w=200, h=100) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def entry(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", json={"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr"})
    aioclient_mock.get(f"{URL}/v1/health", json={"status": "ok", "model_state": "ready", "detail": None,
                                                  "memory": {"oom_kills": 0, "last_oom": None}})
    aioclient_mock.post(f"{URL}/v1/read", json=RESULT)
    e = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
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
    assert await _call(hass, file_path=str(p)) == LOCAL_RESULT
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
        assert await _call(hass, image_entity="image.mailbox") == LOCAL_RESULT
    assert m.call_args.args[1] == "image.mailbox"


async def test_media_source_local(hass, entry, tmp_path):
    p = tmp_path / "m.png"
    p.write_bytes(_png())
    resolved = MagicMock(path=p, url="/x")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)):
        assert await _call(hass, media_content_id="media-source://media_source/local/m.png") == LOCAL_RESULT


async def test_server_415_maps_image_rejected(hass, entry, aioclient_mock, tmp_path):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=415,
                        json={"error": "unsupported_image", "message": "x"})
    p = tmp_path / "a.png"
    p.write_bytes(b"junk")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "unsupported_image"


@pytest.mark.parametrize(("status", "code", "key"), [
    (413, "too_large", "too_large"),
    (415, "unsupported_image", "unsupported_image"),
    (400, "bad_request", "bad_request"),
    (400, "unsupported_lang", "unsupported_lang"),
])
async def test_image_rejected_codes_are_plain_language(hass, entry, aioclient_mock, tmp_path, status, code, key):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=status, json={"error": code, "message": "x"})
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == key
    assert key in _exception_strings()


async def test_unknown_rejection_code_keeps_generic_message(hass, entry, aioclient_mock, tmp_path):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=400, json={"error": "something_new", "message": "x"})
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "image_rejected"
    assert err.value.translation_placeholders == {"code": "something_new"}


def _exception_strings():
    import json
    from pathlib import Path

    import custom_components.text_reader as pkg
    return json.loads((Path(pkg.__file__).parent / "strings.json").read_text())["exceptions"]


async def test_timeout_maps_to_timeout_message(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with patch.object(LocalOcrClient, "read", AsyncMock(side_effect=Timeout("TimeoutError"))):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=str(p))
    assert err.value.translation_key == "timeout"
    assert err.value.translation_placeholders == {"timeout": "30"}


async def test_all_frames_uses_longer_timeout(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    read = AsyncMock(return_value=dict(RESULT))
    with patch.object(LocalOcrClient, "read", read):
        await _call(hass, file_path=str(p))
        await _call(hass, file_path=str(p), all_frames=True)
    assert read.call_args_list[0].kwargs["timeout"] == 30
    assert read.call_args_list[1].kwargs["timeout"] == 180
    with patch.object(LocalOcrClient, "read", AsyncMock(side_effect=Timeout("x"))):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=str(p), all_frames=True)
    assert err.value.translation_placeholders == {"timeout": "180"}


async def test_server_error_is_translated_with_server_message(hass, entry, aioclient_mock, tmp_path):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/v1/read", status=500,
                        json={"error": "model_error", "message": "out of memory: x"})
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "server_error"
    assert err.value.translation_placeholders == {"message": "out of memory: x"}
    assert "server_error" in _exception_strings()


async def test_busy_retries_then_succeeds(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    sleep = AsyncMock()
    read = AsyncMock(side_effect=[Busy(5), Busy(5), dict(RESULT)])
    with patch("custom_components.text_reader.services.sleep", sleep), \
            patch.object(LocalOcrClient, "read", read):
        assert await _call(hass, file_path=str(p)) == LOCAL_RESULT
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
    with patch("custom_components.text_reader.services.sleep", fake_sleep), \
            patch("custom_components.text_reader.services.monotonic", lambda: clock[0]), \
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

    with patch("custom_components.text_reader.services.sleep", AsyncMock()), \
            patch("custom_components.text_reader.services.monotonic", lambda: clock[0]), \
            patch.object(LocalOcrClient, "read", slow_read):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=str(p))
    assert err.value.translation_key == "timeout"


async def test_busy_retry_after_clamped(hass, entry, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    sleep = AsyncMock()
    with patch("custom_components.text_reader.services.sleep", sleep), \
            patch.object(LocalOcrClient, "read", AsyncMock(side_effect=[Busy(0), dict(RESULT)])):
        await _call(hass, file_path=str(p))
    assert sleep.call_args.args[0] == 0.5


async def test_media_source_remote_absolute(hass, entry, aioclient_mock):
    aioclient_mock.get("http://media.test/p.png", content=_png())
    resolved = MagicMock(path=None, url="http://media.test/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)):
        assert await _call(hass, media_content_id="media-source://x/p") == LOCAL_RESULT


async def test_media_source_remote_relative(hass, entry, aioclient_mock):
    aioclient_mock.get("http://ha.test:8123/api/p.png", content=_png())
    resolved = MagicMock(path=None, url="/api/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)), \
            patch("custom_components.text_reader.services.get_url",
                  return_value="http://ha.test:8123"):
        assert await _call(hass, media_content_id="media-source://x/p") == LOCAL_RESULT


async def test_media_source_relative_no_url(hass, entry):
    from homeassistant.helpers.network import NoURLAvailableError

    resolved = MagicMock(path=None, url="/api/p.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)), \
            patch("custom_components.text_reader.services.get_url",
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


def _jpeg_with_orientation(w, h, orientation):
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.mark.parametrize(("orientation", "expected"), [
    (1, "100,0,100,100"),   # stored 200x100, shown 200x100
    (6, "50,0,50,200"),     # stored 200x100, shown rotated: 100x200
    (8, "50,0,50,200"),
    (3, "100,0,100,100"),   # 180 degrees: no swap
])
async def test_region_fraction_uses_exif_rotated_size(hass, entry, aioclient_mock, tmp_path, orientation, expected):
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg_with_orientation(200, 100, orientation))
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _call(hass, file_path=str(p), region={"x": 0.5, "y": 0.0, "w": 0.5, "h": 1})
    assert _field(aioclient_mock.mock_calls[-1][2], "region") == expected


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


async def test_missing_file_says_so(hass, entry, tmp_path):
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path=str(tmp_path / "nope.png"))
    assert err.value.translation_key == "file_not_found"
    assert err.value.translation_placeholders == {"path": str(tmp_path / "nope.png")}


async def test_directory_is_not_a_file(hass, entry, tmp_path):
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path=str(tmp_path))
    assert err.value.translation_key == "file_not_found"


async def test_oversized_file_not_read_or_sent(hass, entry, aioclient_mock, tmp_path):
    from custom_components.text_reader.services import MAX_IMAGE_BYTES

    p = tmp_path / "big.png"
    p.write_bytes(b"\0" * (MAX_IMAGE_BYTES + 1))
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "too_large"
    assert aioclient_mock.call_count == 0


async def test_file_at_the_cap_is_sent(hass, entry, aioclient_mock, tmp_path):
    from custom_components.text_reader.services import MAX_IMAGE_BYTES

    p = tmp_path / "edge.png"
    p.write_bytes(b"\0" * MAX_IMAGE_BYTES)
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    assert await _call(hass, file_path=str(p)) == LOCAL_RESULT


async def test_oversized_remote_media_rejected(hass, entry, aioclient_mock):
    from custom_components.text_reader.services import MAX_IMAGE_BYTES

    aioclient_mock.get("http://media.test/big.png", content=b"\0" * (MAX_IMAGE_BYTES + 1))
    resolved = MagicMock(path=None, url="http://media.test/big.png")
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=resolved)), pytest.raises(HomeAssistantError) as err:
        await _call(hass, media_content_id="media-source://x/big.png")
    assert err.value.translation_key == "too_large"


async def test_unexpected_client_error_is_translated(hass, entry, tmp_path):
    from custom_components.text_reader.api import LocalOcrError

    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with patch.object(LocalOcrClient, "read", AsyncMock(side_effect=LocalOcrError("boom"))), \
            pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=str(p))
    assert err.value.translation_key == "unexpected_error"
    assert err.value.translation_placeholders == {"error": "LocalOcrError"}


AI_ENTITY = "ai_task.test"
AI_RESULT = {
    "text": "from ai", "blocks": [], "fields": {}, "width": 200, "height": 100,
    "ms": 9, "engine": f"ai_task:{AI_ENTITY}", "model": None, "preset": "text",
    "confidence": None,
}


async def _file(hass, tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    return str(p)


@pytest.fixture
async def ai_entry(hass):
    e = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
        data={"backend": "ai_task", "ai_entity": AI_ENTITY, "privacy_ack": True},
        options={"preset": "text", "min_confidence": 0.5, "timeout": 30},
    )
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    return e


async def test_backend_field_overrides_option(hass, entry, tmp_path):
    path = await _file(hass, tmp_path)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "backend": "local", "ai_entity": AI_ENTITY}
    )
    ai = AsyncMock(return_value=dict(AI_RESULT))
    with patch("custom_components.text_reader.router.ai_read", ai), \
            patch.object(LocalOcrClient, "read", AsyncMock(return_value=dict(RESULT))) as local:
        result = await _call(hass, file_path=path, backend="ai_task")
        assert result["routed"] == "ai_task"
        local.assert_not_called()
        result = await _call(hass, file_path=path)  # option backend=local
        assert result["routed"] == "local"
    assert ai.call_count == 1
    with pytest.raises(vol.Invalid, match="backend"):
        await _call(hass, file_path=path, backend="cloud")


async def test_ai_only_entry_reads_without_app(hass, ai_entry, tmp_path):
    assert ai_entry.runtime_data.client is None
    path = await _file(hass, tmp_path)
    with patch("custom_components.text_reader.router.ai_read",
               AsyncMock(return_value=dict(AI_RESULT))):
        result = await _call(hass, file_path=path)
    assert result["routed"] == "ai_task"
    assert result["text"] == "from ai"
    assert ai_entry.runtime_data.recent[-1]["backend"] == "ai_task"


async def test_ai_unavailable_translated(hass, ai_entry, tmp_path):
    from custom_components.text_reader.ai_backend import AiUnavailable

    path = await _file(hass, tmp_path)
    with patch("custom_components.text_reader.router.ai_read",
               AsyncMock(side_effect=AiUnavailable("secret provider text"))):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=path)
    assert err.value.translation_key == "ai_unavailable"
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert "secret" not in str(err.value)
    assert "ai_unavailable" in _exception_strings()
    assert ai_entry.runtime_data.recent[-1]["error"] == "AiUnavailable"


async def test_recent_records_routed_no_text(hass, entry, tmp_path):
    path = await _file(hass, tmp_path)
    await _call(hass, file_path=path)
    rec = entry.runtime_data.recent[-1]
    assert rec["backend"] == "local"
    assert rec["routed"] == "local"
    assert "synthetic" not in repr(rec)
    assert "text" not in rec


async def test_duration_sensor_tracks_local_reads_only(hass, entry, tmp_path):
    path = await _file(hass, tmp_path)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "ai_entity": AI_ENTITY}
    )
    runtime = entry.runtime_data
    runtime.last_duration_ms = 999
    ai = AsyncMock(return_value=dict(AI_RESULT))
    with patch("custom_components.text_reader.router.ai_read", ai):
        await _call(hass, file_path=path, backend="ai_task")
        assert runtime.last_duration_ms == 999
        weak = {**RESULT, "text": "", "ms": 77}
        with patch.object(LocalOcrClient, "read", AsyncMock(return_value=weak)):
            result = await _call(hass, file_path=path, backend="auto")
        assert result["routed"] == "local→ai_task"
        assert runtime.last_duration_ms == 999
    with patch.object(LocalOcrClient, "read", AsyncMock(return_value=dict(RESULT))):
        await _call(hass, file_path=path, backend="local")
    assert runtime.last_duration_ms == 12


async def test_ai_only_entry_with_removed_entity_gives_translated_error(hass, tmp_path):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_get_or_create("ai_task", "test", "t", suggested_object_id="test")
    hass.states.async_set(AI_ENTITY, "idle")
    e = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
        data={"backend": "ai_task", "ai_entity": AI_ENTITY, "privacy_ack": True},
    )
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    er.async_get(hass).async_remove(AI_ENTITY)
    hass.states.async_remove(AI_ENTITY)
    path = await _file(hass, tmp_path)
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, file_path=path)
    assert err.value.translation_key == "ai_unavailable"
    assert str(err.value) == "The AI assistant for Text Reader isn't available"


def _real_ai_entity(hass):
    from types import SimpleNamespace

    from homeassistant.components.ai_task import AITaskEntityFeature
    from homeassistant.components.ai_task.const import DATA_COMPONENT

    feats = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    component = MagicMock()
    component.get_entity = lambda eid: SimpleNamespace(supported_features=feats) if eid == AI_ENTITY else None
    hass.data[DATA_COMPONENT] = component


async def test_unexpected_ai_error_is_translated_ai_unavailable(hass, ai_entry, tmp_path, caplog):
    _real_ai_entity(hass)
    path = await _file(hass, tmp_path)
    gen = AsyncMock(side_effect=RuntimeError("secret recognised words"))
    with patch("homeassistant.components.ai_task.async_generate_data", gen):
        with pytest.raises(HomeAssistantError) as err:
            await _call(hass, file_path=path)
    gen.assert_awaited_once()
    assert err.value.translation_key == "ai_unavailable"
    assert "secret" not in str(err.value) and "secret" not in caplog.text
    assert ai_entry.runtime_data.recent[-1]["error"] == "AiUnavailable"


async def test_local_backend_on_ai_only_entry_says_there_is_no_app(hass, ai_entry, tmp_path):
    path = await _file(hass, tmp_path)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, file_path=path, backend="local")
    assert err.value.translation_key == "no_local_app"
    assert str(err.value) == (
        "This Text Reader setup has no Local OCR app. Use backend: ai_task or auto, "
        "or add the app in Text Reader's options"
    )


def test_config_entry_field_is_named_for_text_reader():
    import json
    from pathlib import Path

    strings = json.loads(
        (Path(__file__).parents[1] / "custom_components/text_reader/strings.json").read_text()
    )
    field = strings["services"]["read_image"]["fields"]["config_entry_id"]
    assert field["name"] == "Text Reader entry"
    assert "Local OCR server" not in field["description"]

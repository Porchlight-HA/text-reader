"""Tests for the AI Task backend."""

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.ai_task import AITaskEntityFeature
from homeassistant.components.ai_task.const import DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError
from PIL import Image

from custom_components.text_reader import ai_backend
from custom_components.text_reader.ai_backend import (
    AI_PROMPT_VERSION,
    INSTRUCTIONS,
    AiUnavailable,
    ai_read,
    structure_for,
)
from custom_components.text_reader.api import ImageRejected
from custom_components.text_reader.attachments import TMP_DIRNAME

ENTITY = "ai_task.test"
FULL = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
PATCH = "custom_components.text_reader.ai_backend.async_generate_data"


def _png(size=(20, 10), frames=1) -> bytes:
    buf = io.BytesIO()
    imgs = [Image.new("RGB", size, (i * 10, 0, 0)) for i in range(frames)]
    imgs[0].save(buf, format="PNG" if frames == 1 else "GIF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _entity(hass, features=FULL):
    entity = SimpleNamespace(supported_features=features)
    component = MagicMock()
    component.get_entity = lambda eid: entity if eid == ENTITY else None
    hass.data[DATA_COMPONENT] = component


def _result(data):
    return SimpleNamespace(data=data)


async def test_ai_envelope_maps_fields(hass):
    _entity(hass)
    data = {
        "text": "JANE DOE\n1 MAIN ST",
        "addressee": ["JANE DOE", "1 MAIN ST"],
        "return_address": None,
        "markers": ["FORWARDING SERVICE REQUESTED"],
        "generic_addressee": False,
    }
    with patch(PATCH, AsyncMock(return_value=_result(data))) as gen:
        out = await ai_read(hass, ENTITY, _png(), preset="envelope")
    assert out["text"] == data["text"]
    assert out["fields"]["addressee"] == {
        "lines": ["JANE DOE", "1 MAIN ST"],
        "confidence": None,
    }
    assert out["fields"]["return_address"] is None
    assert out["fields"]["markers"] == ["FORWARDING SERVICE REQUESTED"]
    assert out["fields"]["generic_addressee"] is False
    assert (out["width"], out["height"]) == (20, 10)
    assert out["engine"] == f"ai_task:{ENTITY}"
    assert out["model"] is None and out["blocks"] == []
    assert out["preset"] == "envelope" and out["confidence"] is None
    assert isinstance(out["ms"], int)
    kwargs = gen.call_args.kwargs
    assert kwargs["entity_id"] == ENTITY
    assert kwargs["instructions"] == INSTRUCTIONS["envelope"]
    att = kwargs["attachments"][0]
    assert att["media_content_id"].startswith("media-source://text_reader/")
    assert att["media_content_type"] == "image/png"


async def test_ai_malformed_structure_normalised(hass):
    _entity(hass)
    bad = {"addressee": "x", "markers": 3, "generic_addressee": "yes"}
    with patch(PATCH, AsyncMock(return_value=_result(bad))):
        out = await ai_read(hass, ENTITY, _png(), preset="envelope")
    assert out["text"] == ""
    assert out["fields"] == {
        "addressee": None,
        "return_address": None,
        "markers": [],
        "generic_addressee": False,
    }
    junk = {"text": 5, "tracking": [{"carrier": "UPS"}, "x"], "ship_to": {"a": 1}}
    with patch(PATCH, AsyncMock(return_value=_result(junk))):
        out = await ai_read(hass, ENTITY, _png(), preset="label")
    assert out["fields"] == {"tracking": [], "ship_to": None}
    with patch(PATCH, AsyncMock(return_value=_result({"value": "12", "digits": 4}))):
        out = await ai_read(hass, ENTITY, _png(), preset="meter")
    assert out["fields"] == {"value": None, "digits": ""}
    with patch(PATCH, AsyncMock(return_value=_result(None))):
        out = await ai_read(hass, ENTITY, _png(), preset="text")
    assert out["text"] == "" and out["fields"] == {}


async def test_ai_refusal_text_only(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock(return_value=_result({"text": "I can't read that."}))):
        out = await ai_read(hass, ENTITY, _png(), preset="label")
    assert out["text"] == "I can't read that."
    assert out["fields"] == {"tracking": [], "ship_to": None}


async def test_ai_entity_missing_raises_unavailable(hass):
    _entity(hass)
    with pytest.raises(AiUnavailable):
        await ai_read(hass, "ai_task.nope", _png(), preset="text")
    del hass.data[DATA_COMPONENT]
    with pytest.raises(AiUnavailable):
        await ai_read(hass, ENTITY, _png(), preset="text")


async def test_ai_entity_unavailable_state(hass):
    _entity(hass)
    hass.states.async_set(ENTITY, "unavailable")
    with pytest.raises(AiUnavailable):
        await ai_read(hass, ENTITY, _png(), preset="text")


async def test_ai_entity_without_attachments_raises_unavailable(hass):
    _entity(hass, AITaskEntityFeature.GENERATE_DATA)
    with patch(PATCH, AsyncMock()) as gen, pytest.raises(AiUnavailable):
        await ai_read(hass, ENTITY, _png(), preset="text")
    gen.assert_not_called()


async def test_ai_homeassistant_error_raises_unavailable(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock(side_effect=HomeAssistantError("secret text"))):
        with pytest.raises(AiUnavailable) as err:
            await ai_read(hass, ENTITY, _png(), preset="text")
    assert "secret text" not in str(err.value)


async def test_ai_all_frames_one_call_per_frame(hass):
    _entity(hass)
    gen = AsyncMock(side_effect=[_result({"text": "a"}), _result({"text": "b"}), _result({"text": "c"})])
    with patch(PATCH, gen):
        out = await ai_read(hass, ENTITY, _png(frames=3), preset="text", all_frames=True)
    assert gen.await_count == 3
    assert [f["text"] for f in out["frames"]] == ["a", "b", "c"]
    assert out["text"] == "a\n\nb\n\nc"
    assert all(
        c.kwargs["attachments"][0]["media_content_type"] == "image/png"
        for c in gen.call_args_list
    )


async def test_ai_all_frames_too_many(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock()) as gen, patch.object(
        ai_backend, "_header", return_value=(1, 1, "image/gif", 51)
    ):
        with pytest.raises(ImageRejected) as err:
            await ai_read(hass, ENTITY, b"x", preset="text", all_frames=True)
    assert err.value.code == "too_large"
    gen.assert_not_called()


async def test_ai_bad_image_rejected(hass):
    _entity(hass)
    with pytest.raises(ImageRejected):
        await ai_read(hass, ENTITY, b"not an image", preset="text")


async def test_ai_staged_files_deleted_after_call(hass):
    _entity(hass)
    from pathlib import Path

    tmp = Path(hass.config.path(TMP_DIRNAME))
    seen: list[Path] = []

    async def fake(hass_, **kwargs):
        token = kwargs["attachments"][0]["media_content_id"].rsplit("/", 1)[1]
        seen.append(tmp / token)
        assert seen[-1].exists()
        raise HomeAssistantError("boom")

    with patch(PATCH, fake), pytest.raises(AiUnavailable):
        await ai_read(hass, ENTITY, _png(), preset="text")
    assert seen and not seen[0].exists()
    assert not list(tmp.iterdir())


async def test_ai_never_logs_text(hass, caplog):
    _entity(hass)
    caplog.set_level(logging.DEBUG)
    with patch(PATCH, AsyncMock(return_value=_result({"text": "SECRET-WORDS"}))):
        await ai_read(hass, ENTITY, _png(), preset="text")
    assert "SECRET-WORDS" not in caplog.text


def test_instructions_fixed_and_versioned():
    assert AI_PROMPT_VERSION == 1
    assert set(INSTRUCTIONS) == {"text", "envelope", "label", "meter"}
    assert INSTRUCTIONS["text"] == (
        "You are a transcription tool. Read the text in the attached image exactly as "
        "printed. Do not guess, correct, summarise or translate. If a field is not "
        "present or not legible, return null for it (or an empty list). Return only "
        "the requested fields: text."
    )
    assert INSTRUCTIONS["envelope"].endswith(
        "text, addressee, return_address, markers, generic_addressee."
    )


@pytest.mark.parametrize("preset", ["text", "envelope", "label", "meter"])
def test_structure_validates_like_service(preset):
    schema = structure_for(preset)
    sample = {
        "text": {"text": "t"},
        "envelope": {"text": "t", "addressee": ["a"], "generic_addressee": True},
        "label": {"text": "t", "tracking": [{"carrier": "UPS", "number": "1Z"}]},
        "meter": {"text": "t", "value": 12.5, "digits": "0012"},
    }[preset]
    assert schema(sample) == sample

"""Tests for the AI Task backend."""

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.ai_task import AITaskEntityFeature
from homeassistant.components.ai_task.const import DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError
from PIL import GifImagePlugin, Image

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
PATCH = "homeassistant.components.ai_task.async_generate_data"


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
        "indicia": [],
        "markers": [],
        "generic_addressee": False,
    }
    junk = {"text": 5, "tracking": [{"carrier": "UPS"}, "x"], "ship_to": {"a": 1}}
    with patch(PATCH, AsyncMock(return_value=_result(junk))):
        out = await ai_read(hass, ENTITY, _png(), preset="label")
    assert out["fields"] == {"tracking": [], "ship_to": None}
    with patch(PATCH, AsyncMock(return_value=_result({"value": "12", "digits": 4}))):
        out = await ai_read(hass, ENTITY, _png(), preset="meter")
    assert out["fields"] == {"value": None, "digits": "", "confidence": None}
    with patch(PATCH, AsyncMock(return_value=_result(None))):
        out = await ai_read(hass, ENTITY, _png(), preset="text")
    assert out["text"] == "" and out["fields"] is None


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


async def test_ai_all_frames_too_many(hass, monkeypatch):
    _entity(hass)
    monkeypatch.setattr(ai_backend, "MAX_FRAMES", 2)
    with patch(PATCH, AsyncMock()) as gen:
        with pytest.raises(ImageRejected) as err:
            await ai_read(hass, ENTITY, _png(frames=3), preset="text", all_frames=True)
    assert err.value.code == "too_large"
    gen.assert_not_called()


async def test_ai_bad_image_rejected(hass):
    _entity(hass)
    with pytest.raises(ImageRejected) as err:
        await ai_read(hass, ENTITY, b"not an image", preset="text")
    assert err.value.code == "unsupported_image"


async def test_ai_corrupt_frame_rejected(hass):
    _entity(hass)
    image = _png(frames=2)
    with patch(PATCH, AsyncMock()) as gen, patch.object(
        GifImagePlugin.GifImageFile, "seek", side_effect=OSError("truncated")
    ):
        with pytest.raises(ImageRejected) as err:
            await ai_read(hass, ENTITY, image, preset="text", all_frames=True)
    assert err.value.code == "unsupported_image"
    gen.assert_not_called()


async def test_ai_task_import_error_is_unavailable(hass):
    import sys

    _entity(hass)
    with patch.dict(sys.modules, {"homeassistant.components.ai_task": None}):
        with pytest.raises(AiUnavailable, match="ai_task_missing"):
            await ai_read(hass, ENTITY, _png(), preset="text")


async def test_ai_all_frames_has_no_top_level_fields(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock(return_value=_result({"text": "a"}))):
        out = await ai_read(hass, ENTITY, _png(frames=2), preset="text", all_frames=True)
    assert "fields" not in out and len(out["frames"]) == 2


def test_structure_required_and_nullable():
    import probatio

    def req(preset):
        return {str(k) for k in structure_for(preset).schema if isinstance(k, probatio.Required)}

    assert req("text") == {"text"}
    assert req("envelope") == {"text", "markers", "generic_addressee"}
    assert req("label") == {"text", "tracking"}
    assert req("meter") == {"text", "digits"}


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
        "envelope": {"text": "t", "addressee": ["a"], "markers": [], "generic_addressee": True},
        "label": {"text": "t", "tracking": [{"carrier": "UPS", "number": "1Z"}]},
        "meter": {"text": "t", "value": 12.5, "digits": "0012"},
    }[preset]
    assert schema(sample) == sample


# --- final review: one image normaliser for every AI path (I2) ------------------------------


def _capture(hass, sent: list, data=None):
    """A fake async_generate_data that records the staged bytes and mime of each call."""
    from pathlib import Path

    tmp = Path(hass.config.path(TMP_DIRNAME))

    async def fake(hass_, **kwargs):
        att = kwargs["attachments"][0]
        token = att["media_content_id"].rsplit("/", 1)[1]
        sent.append(((tmp / token).read_bytes(), att["media_content_type"]))
        return _result(data if data is not None else {"text": "t"})

    return fake


def _rotated_jpeg(size=(200, 100)) -> bytes:
    """Stored left half red, right half blue; EXIF orientation 6 (displayed rotated 90° CW)."""
    img = Image.new("RGB", size, "red")
    img.paste(Image.new("RGB", (size[0] // 2, size[1]), "blue"), (size[0] // 2, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


async def test_ai_exif_rotation_and_region_in_rotated_space(hass):
    _entity(hass)
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        out = await ai_read(hass, ENTITY, _rotated_jpeg(), preset="text", region=(0, 100, 100, 100))
    data, mime = sent[0]
    img = Image.open(io.BytesIO(data))
    assert mime == "image/png" and img.format == "PNG"
    assert img.size == (100, 100)
    r, g, b = img.convert("RGB").getpixel((50, 50))
    assert b > 200 and r < 60  # displayed bottom half is the stored right half: blue
    # Width and height are the rotated, uncropped size, as the local App reports.
    assert (out["width"], out["height"]) == (100, 200)


async def test_ai_exif_rotation_without_region_is_reencoded(hass):
    _entity(hass)
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        out = await ai_read(hass, ENTITY, _rotated_jpeg(), preset="text")
    img = Image.open(io.BytesIO(sent[0][0]))
    assert img.size == (100, 200)
    assert (out["width"], out["height"]) == (100, 200)


async def test_ai_mpo_is_sent_as_png(hass):
    _entity(hass)
    frames = [Image.new("RGB", (40, 30), "green"), Image.new("RGB", (40, 30), "white")]
    buf = io.BytesIO()
    frames[0].save(buf, "MPO", save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        await ai_read(hass, ENTITY, buf.getvalue(), preset="text")
    data, mime = sent[0]
    img = Image.open(io.BytesIO(data))
    assert mime == "image/png" and img.format == "PNG"
    assert getattr(img, "n_frames", 1) == 1


async def test_ai_animated_gif_sends_frame_zero_only(hass):
    _entity(hass)
    sent: list = []
    with patch(PATCH, _capture(hass, sent)) as _:
        out = await ai_read(hass, ENTITY, _png(frames=3), preset="text")
    assert len(sent) == 1
    data, mime = sent[0]
    img = Image.open(io.BytesIO(data))
    assert mime == "image/png" and getattr(img, "n_frames", 1) == 1
    assert img.convert("RGB").getpixel((0, 0))[0] < 5  # frame 0 is black; frames 1, 2 are red-ish
    assert (out["width"], out["height"]) == (20, 10)


async def test_ai_plain_image_is_reencoded_at_source_quality_without_metadata(hass):
    _entity(hass)
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), "white").save(buf, "JPEG")
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        await ai_read(hass, ENTITY, buf.getvalue(), preset="text")
    data, mime = sent[0]
    assert mime == "image/jpeg" and Image.open(io.BytesIO(data)).size == (30, 20)


def _gps_exif() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    exif.get_ifd(0x8825).update({1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)})
    return exif


def _icc() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _no_metadata(data: bytes) -> None:
    img = Image.open(io.BytesIO(data))
    assert len(img.getexif()) == 0 and not img.getexif().get_ifd(0x8825)
    assert b"Exif" not in data and b"SecretCam" not in data


async def test_ai_jpeg_pass_through_strips_exif_gps(hass):
    _entity(hass)
    buf = io.BytesIO()
    icc = _icc()
    Image.new("RGB", (30, 20), "white").save(buf, "JPEG", exif=_gps_exif(), icc_profile=icc)
    assert Image.open(io.BytesIO(buf.getvalue())).getexif().get_ifd(0x8825)  # fixture has GPS
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        await ai_read(hass, ENTITY, buf.getvalue(), preset="text")
    data, mime = sent[0]
    assert mime == "image/jpeg"
    _no_metadata(data)
    assert Image.open(io.BytesIO(data)).info.get("icc_profile") == icc


async def test_ai_png_and_webp_pass_through_strip_metadata(hass):
    from PIL import PngImagePlugin

    _entity(hass)
    png = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "SecretCam")
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta>SecretCam</x:xmpmeta>")
    Image.new("RGB", (30, 20), "white").save(png, "PNG", pnginfo=info, exif=_gps_exif())
    webp = io.BytesIO()
    Image.new("RGB", (30, 20), "white").save(
        webp, "WEBP", exif=_gps_exif(), xmp=b"<x:xmpmeta>SecretCam</x:xmpmeta>")
    assert b"XMP " in webp.getvalue()  # fixture has XMP
    for src, mime in ((png.getvalue(), "image/png"), (webp.getvalue(), "image/webp")):
        sent: list = []
        with patch(PATCH, _capture(hass, sent)):
            await ai_read(hass, ENTITY, src, preset="text")
        data, got = sent[0]
        assert got == mime
        _no_metadata(data)
        assert not Image.open(io.BytesIO(data)).info.get("Comment")
        assert b"xmpmeta" not in data and b"XMP" not in data and b"iTXt" not in data


async def test_ai_large_photo_reencoded_as_jpeg(hass):
    _entity(hass)
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        await ai_read(hass, ENTITY, _rotated_jpeg((2000, 1200)), preset="text")
    data, mime = sent[0]
    assert mime == "image/jpeg" and Image.open(io.BytesIO(data)).size == (1200, 2000)


async def test_ai_over_40_megapixels_rejected(hass, monkeypatch):
    _entity(hass)
    monkeypatch.setattr(ai_backend, "MAX_PIXELS", 199)
    with patch(PATCH, AsyncMock()) as gen, pytest.raises(ImageRejected) as err:
        await ai_read(hass, ENTITY, _png(), preset="text")  # 20 x 10 = 200 px
    assert err.value.code == "too_large"
    gen.assert_not_called()


async def test_ai_decompression_bomb_rejected(hass, monkeypatch):
    _entity(hass)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)  # Pillow refuses above twice this
    with patch(PATCH, AsyncMock()) as gen, pytest.raises(ImageRejected) as err:
        await ai_read(hass, ENTITY, _png(), preset="text")
    assert err.value.code == "too_large"
    gen.assert_not_called()


def test_forty_megapixel_limit_matches_the_app():
    assert ai_backend.MAX_PIXELS == 40_000_000


async def test_ai_frame_index_out_of_range_rejected(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock()) as gen, pytest.raises(ImageRejected) as err:
        await ai_read(hass, ENTITY, _png(frames=2), preset="text", index=5)
    assert err.value.code == "bad_request"
    gen.assert_not_called()


# --- final review: any AI failure is AiUnavailable (I3), with a time budget (I4) ----------


async def test_ai_unexpected_error_is_unavailable(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock(side_effect=RuntimeError("secret text"))):
        with pytest.raises(AiUnavailable) as err:
            await ai_read(hass, ENTITY, _png(), preset="text")
    assert str(err.value) == "RuntimeError"
    assert err.value.__cause__ is None and err.value.__suppress_context__


async def test_ai_staging_oserror_is_unavailable(hass):
    _entity(hass)
    with patch.object(
        __import__("custom_components.text_reader.attachments", fromlist=["_write"]),
        "_write", side_effect=OSError("disk full"),
    ), patch(PATCH, AsyncMock()) as gen, pytest.raises(AiUnavailable, match="OSError"):
        await ai_read(hass, ENTITY, _png(), preset="text")
    gen.assert_not_called()


async def test_ai_cancellation_propagates(hass):
    import asyncio

    _entity(hass)
    with patch(PATCH, AsyncMock(side_effect=asyncio.CancelledError)), pytest.raises(
        asyncio.CancelledError
    ):
        await ai_read(hass, ENTITY, _png(), preset="text")


async def test_ai_call_has_a_time_budget(hass, monkeypatch):
    import asyncio

    _entity(hass)
    monkeypatch.setattr(ai_backend, "AI_MIN_TIMEOUT", 0.01)

    async def slow(hass_, **kwargs):
        await asyncio.sleep(5)

    with patch(PATCH, slow), pytest.raises(AiUnavailable, match="timeout"):
        await ai_read(hass, ENTITY, _png(), preset="text", timeout=0.01)
    assert not list(Path_tmp(hass).iterdir())


def test_ai_budget_is_at_least_120_s():
    assert ai_backend.AI_MIN_TIMEOUT == 120
    assert ai_backend.ai_budget(30) == 120
    assert ai_backend.ai_budget(300) == 300


def Path_tmp(hass):
    from pathlib import Path

    return Path(hass.config.path(TMP_DIRNAME))


# --- final review: field shape parity with the local App (M1, M2) -------------------------


async def test_ai_field_shapes_match_local(hass):
    _entity(hass)
    env = {"text": "t", "addressee": [], "return_address": [], "markers": [], "generic_addressee": False}
    with patch(PATCH, AsyncMock(return_value=_result(env))):
        out = await ai_read(hass, ENTITY, _png(), preset="envelope")
    assert out["fields"] == {
        "addressee": None, "return_address": None, "indicia": [], "markers": [],
        "generic_addressee": False,
    }
    with patch(PATCH, AsyncMock(return_value=_result({"text": "t", "value": 12, "digits": "12"}))):
        out = await ai_read(hass, ENTITY, _png(), preset="meter")
    assert out["fields"] == {"value": 12.0, "digits": "12", "confidence": None}
    with patch(PATCH, AsyncMock(return_value=_result({"text": "t", "tracking": [], "ship_to": []}))):
        out = await ai_read(hass, ENTITY, _png(), preset="label")
    assert out["fields"] == {"tracking": [], "ship_to": None}
    with patch(PATCH, AsyncMock(return_value=_result({"text": "t"}))):
        out = await ai_read(hass, ENTITY, _png(), preset="text")
    assert out["fields"] is None


async def test_ai_all_frames_top_level_matches_local(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock(return_value=_result({"text": "a"}))):
        out = await ai_read(hass, ENTITY, _png(frames=2), preset="envelope", all_frames=True)
    assert set(out) == {"frames", "text"}
    for frame in out["frames"]:
        assert {"text", "blocks", "fields", "width", "height", "ms", "engine", "model", "preset",
                "confidence"} <= set(frame)


async def test_ai_transparency_flattened_onto_white(hass):
    _entity(hass)
    buf = io.BytesIO()
    Image.new("RGBA", (20, 10), (0, 0, 0, 0)).save(buf, "PNG")
    sent: list = []
    with patch(PATCH, _capture(hass, sent)):
        await ai_read(hass, ENTITY, buf.getvalue(), preset="text", region=(0, 0, 10, 10))
    img = Image.open(io.BytesIO(sent[0][0]))
    assert img.mode == "RGB" and img.getpixel((5, 5)) == (255, 255, 255)


async def test_ai_region_outside_the_image_rejected(hass):
    _entity(hass)
    with patch(PATCH, AsyncMock()) as gen, pytest.raises(ImageRejected) as err:
        await ai_read(hass, ENTITY, _png(), preset="text", region=(50, 0, 5, 5))
    assert err.value.code == "bad_request"
    gen.assert_not_called()

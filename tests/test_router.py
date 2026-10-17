"""Tests for the backend router."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from custom_components.text_reader import router
from custom_components.text_reader.ai_backend import AiUnavailable
from custom_components.text_reader.api import CannotConnect, ImageRejected, Timeout
from custom_components.text_reader.data import Settings, settings
from custom_components.text_reader.router import needs_fallback, route_read

AI_ENTITY = "ai_task.test"


def _img(size=(20, 10), frames=1) -> bytes:
    buf = io.BytesIO()
    imgs = [Image.new("RGB", size, (i * 40, 0, 0)) for i in range(frames)]
    imgs[0].save(buf, format="PNG" if frames == 1 else "GIF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _local(text="hello", conf=0.95, fields=None, preset="text"):
    return {
        "text": text,
        "blocks": [{"text": text, "confidence": conf, "box": [0, 0, 1, 1], "line": 0}] if text else [],
        "fields": fields if fields is not None else {},
        "width": 20,
        "height": 10,
        "ms": 5,
        "engine": "rapidocr",
        "model": "m",
        "preset": preset,
    }


def _ai(text="from ai", preset="text"):
    return {
        "text": text,
        "blocks": [],
        "fields": {},
        "width": 20,
        "height": 10,
        "ms": 9,
        "engine": f"ai_task:{AI_ENTITY}",
        "model": None,
        "preset": preset,
        "confidence": None,
    }


def _entry(client=None, *, url=True, options=None):
    data = {"url": "http://x"} if url else {}
    return SimpleNamespace(
        data=data, options=options or {}, runtime_data=SimpleNamespace(client=client)
    )


def _client(result=None, error=None):
    client = SimpleNamespace(read=AsyncMock())
    if error is not None:
        client.read.side_effect = error
    else:
        client.read.return_value = result
    return client


KW = {"preset": "text", "min_confidence": 0.5, "region": None, "all_frames": False, "timeout": 5.0}


async def _route(hass, entry, image=None, backend="auto", **over):
    return await route_read(hass, entry, image or _img(), backend=backend, **{**KW, **over})


# --- needs_fallback truth table -------------------------------------------------

ENV_OK = {"addressee": {"lines": ["A"], "confidence": 0.9}}
CASES = [
    # preset, result, expected
    ("text", _local(), False),
    ("text", _local(text=""), True),
    ("text", _local(conf=0.5), True),
    ("envelope", _local(fields=ENV_OK), False),
    ("envelope", _local(text=""), True),
    ("envelope", _local(fields={"addressee": None}), True),
    ("envelope", _local(fields={}), True),
    ("envelope", _local(fields=ENV_OK, conf=0.3), True),
    ("label", _local(fields={"tracking": [{"carrier": "ups", "number": "1Z"}]}), False),
    ("label", _local(text=""), True),
    ("label", _local(fields={"tracking": []}), True),
    ("label", _local(fields={}), True),
    ("label", _local(fields={"tracking": [{"carrier": "ups", "number": "1Z"}]}, conf=0.1), True),
    ("meter", _local(fields={"value": 12.5}), False),
    ("meter", _local(text=""), True),
    ("meter", _local(fields={"value": None}), True),
    ("meter", _local(fields={}), True),
    ("meter", _local(fields={"value": 12.5}, conf=0.79), True),
]


@pytest.mark.parametrize(("preset", "result", "expected"), CASES)
def test_needs_fallback(preset, result, expected):
    assert needs_fallback(preset, result, 0.8) is expected


def test_needs_fallback_threshold_is_mean_and_inclusive():
    result = _local()
    result["blocks"] = [{"confidence": 0.9}, {"confidence": 0.7}]
    assert needs_fallback("text", result, 0.8) is False  # mean 0.8
    assert needs_fallback("text", result, 0.81) is True


def test_needs_fallback_blank_text_and_no_blocks():
    result = _local()
    result["blocks"] = []
    assert needs_fallback("text", result, 0.8) is True
    assert needs_fallback("text", _local(text="   "), 0.8) is True


# --- settings -----------------------------------------------------------------


def test_settings_defaults_and_precedence():
    e = SimpleNamespace(data={"url": "u"}, options={})
    assert settings(e) == Settings("local", None, 0.8)
    e = SimpleNamespace(data={}, options={})
    assert settings(e).backend == "ai_task"
    e = SimpleNamespace(
        data={"url": "u", "backend": "local", "ai_entity": "a.b"},
        options={"backend": "auto", "auto_threshold": 0.6},
    )
    assert settings(e) == Settings("auto", "a.b", 0.6)


# --- routing --------------------------------------------------------------------


async def test_local_only_never_calls_ai(hass):
    client = _client(_local(text=""))
    with patch.object(router, "ai_read", new=AsyncMock()) as ai:
        result = await _route(hass, _entry(client), backend="local")
    ai.assert_not_called()
    assert result["routed"] == "local"
    assert result["confidence"] is None  # no blocks


async def test_local_passes_args_and_computes_confidence(hass):
    client = _client(_local(conf=0.9))
    await _route(hass, _entry(client), backend="local", region=(1, 2, 3, 4), preset="text")
    client.read.assert_awaited_once()
    assert client.read.await_args.kwargs["region"] == (1, 2, 3, 4)
    assert client.read.await_args.kwargs["timeout"] == 5.0


async def test_local_confidence_is_mean(hass):
    res = _local()
    res["blocks"] = [{"confidence": 0.9}, {"confidence": 0.7}]
    result = await _route(hass, _entry(_client(res)), backend="local")
    assert result["confidence"] == pytest.approx(0.8)


async def test_ai_only_never_calls_local(hass):
    client = _client(_local())
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(return_value=_ai())) as ai:
        result = await _route(hass, entry, backend="ai_task")
    client.read.assert_not_called()
    assert result["routed"] == "ai_task"
    assert result["confidence"] is None
    assert ai.await_args.args[1] == AI_ENTITY


async def test_ai_only_without_entity_raises(hass):
    with pytest.raises(AiUnavailable):
        await _route(hass, _entry(None, url=False), backend="ai_task")


async def test_ai_only_entry_without_client(hass):
    entry = _entry(None, url=False, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(return_value=_ai())):
        result = await _route(hass, entry, backend="auto")
    assert result["routed"] == "ai_task"


async def test_ai_region_is_cropped_before_staging(hass):
    entry = _entry(None, url=False, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(return_value=_ai())) as ai:
        await _route(hass, entry, image=_img((40, 30)), backend="ai_task", region=(5, 5, 10, 8))
    sent = Image.open(io.BytesIO(ai.await_args.args[2]))
    assert sent.size == (10, 8)


async def test_auto_strong_local_stays_local(hass):
    client = _client(_local())
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock()) as ai:
        result = await _route(hass, entry)
    ai.assert_not_called()
    assert result["routed"] == "local"
    assert result["confidence"] == pytest.approx(0.95)


async def test_auto_weak_local_goes_ai_routed_label(hass):
    client = _client(_local(conf=0.2))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(return_value=_ai())):
        result = await _route(hass, entry)
    assert result["routed"] == "local→ai_task"
    assert result["text"] == "from ai"
    assert result["confidence"] is None


async def test_auto_local_down_goes_ai(hass):
    for err in (CannotConnect("down"), Timeout("slow")):
        client = _client(error=err)
        entry = _entry(client, options={"ai_entity": AI_ENTITY})
        with patch.object(router, "ai_read", new=AsyncMock(return_value=_ai())):
            result = await _route(hass, entry)
        assert result["routed"] == "local→ai_task"


async def test_auto_local_down_and_ai_unavailable_raises_local_error(hass):
    client = _client(error=CannotConnect("down"))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with (
        patch.object(router, "ai_read", new=AsyncMock(side_effect=AiUnavailable("x"))),
        pytest.raises(CannotConnect) as exc,
    ):
        await _route(hass, entry)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


async def test_auto_image_rejected_no_fallback(hass):
    client = _client(error=ImageRejected("unsupported_image", "bad"))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with (
        patch.object(router, "ai_read", new=AsyncMock()) as ai,
        pytest.raises(ImageRejected),
    ):
        await _route(hass, entry)
    ai.assert_not_called()


async def test_auto_ai_unavailable_returns_local_result(hass):
    client = _client(_local(conf=0.2))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(side_effect=AiUnavailable("x"))):
        result = await _route(hass, entry)
    assert result["routed"] == "local"
    assert result["text"] == "hello"


async def test_auto_without_entity_returns_local_result(hass):
    client = _client(_local(conf=0.2))
    result = await _route(hass, _entry(client))
    assert result["routed"] == "local"


async def test_auto_threshold_option_is_used(hass):
    client = _client(_local(conf=0.7))
    entry = _entry(client, options={"ai_entity": AI_ENTITY, "auto_threshold": 0.6})
    with patch.object(router, "ai_read", new=AsyncMock()) as ai:
        result = await _route(hass, entry)
    ai.assert_not_called()
    assert result["routed"] == "local"


async def test_auto_all_frames_routes_weak_frames_only(hass):
    frames = [_local("one", 0.95), _local("two", 0.1), _local("three", 0.9)]
    client = _client({"frames": frames, "text": "one\n\ntwo\n\nthree", "width": 20, "height": 10})
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    ai_mock = AsyncMock(return_value=_ai("two fixed"))
    with patch.object(router, "ai_read", new=ai_mock):
        result = await _route(hass, entry, image=_img(frames=3), all_frames=True)
    assert client.read.await_args.kwargs["all_frames"] is True
    ai_mock.assert_awaited_once()
    assert ai_mock.await_args.kwargs.get("all_frames", False) is False
    sent = Image.open(io.BytesIO(ai_mock.await_args.args[2]))
    assert sent.format == "PNG"
    assert 30 <= sent.convert("RGB").getpixel((0, 0))[0] <= 50  # frame 1's colour, not 0 or 80
    assert result["confidence"] is None
    assert [f["text"] for f in result["frames"]] == ["one", "two fixed", "three"]
    assert [f["routed"] for f in result["frames"]] == ["local", "local→ai_task", "local"]
    assert result["routed"] == "local→ai_task"
    assert result["text"] == "one\n\ntwo fixed\n\nthree"
    assert result["frames"][0]["confidence"] == pytest.approx(0.95)
    assert result["frames"][1]["confidence"] is None


async def test_auto_all_frames_all_strong_stays_local(hass):
    frames = [_local("one"), _local("two")]
    client = _client({"frames": frames, "text": "one\n\ntwo"})
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock()) as ai:
        result = await _route(hass, entry, image=_img(frames=2), all_frames=True)
    ai.assert_not_called()
    assert result["routed"] == "local"


async def test_auto_all_frames_ai_unavailable_keeps_local_frame(hass):
    frames = [_local("one", 0.1), _local("two")]
    client = _client({"frames": frames, "text": "one\n\ntwo"})
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(side_effect=AiUnavailable("x"))):
        result = await _route(hass, entry, image=_img(frames=2), all_frames=True)
    assert result["routed"] == "local"
    assert result["frames"][0]["text"] == "one"


async def test_auto_all_frames_local_down_goes_ai_all_frames(hass):
    client = _client(error=CannotConnect("down"))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    ai_res = {"frames": [_ai("a"), _ai("b")], "text": "a\n\nb", "blocks": [], "confidence": None}
    with patch.object(router, "ai_read", new=AsyncMock(return_value=ai_res)) as ai:
        result = await _route(hass, entry, image=_img(frames=2), all_frames=True)
    assert ai.await_args.kwargs["all_frames"] is True
    assert result["routed"] == "local→ai_task"
    assert [f["routed"] for f in result["frames"]] == ["local→ai_task", "local→ai_task"]
    assert result["confidence"] is None


async def test_ai_error_not_chained_in_weak_fallback(hass):
    # AiUnavailable is swallowed in auto; nothing of it leaks into the result.
    client = _client(_local(conf=0.2))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    with patch.object(router, "ai_read", new=AsyncMock(side_effect=AiUnavailable("secret"))):
        result = await _route(hass, entry)
    assert "secret" not in repr(result)


async def test_ai_only_entry_sets_up_and_unloads(hass):
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.text_reader.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, title="Text Reader", data={"ai_entity": AI_ENTITY}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client is None
    assert entry.runtime_data.coordinator is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


def _same_in_region_gif() -> bytes:
    """Three frames identical inside (0, 0, 5, 5); they differ only outside it."""
    imgs = []
    for i in range(3):
        img = Image.new("RGB", (20, 10), (0, 0, 0))
        img.putpixel((15, 5), (i * 50 + 50, 0, 0))
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


async def test_ai_all_frames_region_identical_crops_one_call_per_frame(hass):
    entry = _entry(None, url=False, options={"ai_entity": AI_ENTITY})
    ai_mock = AsyncMock(side_effect=lambda *a, **k: _ai(f"t{ai_mock.await_count}"))
    with patch.object(router, "ai_read", new=ai_mock):
        result = await _route(
            hass, entry, image=_same_in_region_gif(), backend="ai_task",
            all_frames=True, region=(0, 0, 5, 5),
        )
    assert ai_mock.await_count == 3
    for call in ai_mock.await_args_list:
        sent = Image.open(io.BytesIO(call.args[2]))
        assert sent.format == "PNG" and sent.size == (5, 5)
        assert getattr(sent, "n_frames", 1) == 1
    assert len(result["frames"]) == 3
    assert result["routed"] == "ai_task"
    assert result["confidence"] is None


async def test_auto_weak_single_ai_image_rejected_keeps_local(hass):
    client = _client(_local(conf=0.2))
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    err = ImageRejected("unsupported_image", "x")
    with patch.object(router, "ai_read", new=AsyncMock(side_effect=err)):
        result = await _route(hass, entry)
    assert result["routed"] == "local"
    assert result["text"] == "hello"


async def test_auto_weak_frame_ai_image_rejected_keeps_local_frame(hass):
    frames = [_local("one", 0.1), _local("two")]
    client = _client({"frames": frames, "text": "one\n\ntwo"})
    entry = _entry(client, options={"ai_entity": AI_ENTITY})
    err = ImageRejected("unsupported_image", "x")
    with patch.object(router, "ai_read", new=AsyncMock(side_effect=err)):
        result = await _route(hass, entry, image=_img(frames=2), all_frames=True)
    assert result["routed"] == "local"
    assert result["frames"][0]["text"] == "one"
    assert result["confidence"] is not None


async def test_explicit_local_without_client_raises(hass):
    entry = _entry(None, url=False, options={"ai_entity": AI_ENTITY})
    with (
        patch.object(router, "ai_read", new=AsyncMock()) as ai,
        pytest.raises(CannotConnect),
    ):
        await _route(hass, entry, backend="local")
    ai.assert_not_called()


async def test_seam_region_all_frames_through_real_ai_backend(hass):
    from types import SimpleNamespace as NS
    from unittest.mock import MagicMock

    from homeassistant.components.ai_task import AITaskEntityFeature
    from homeassistant.components.ai_task.const import DATA_COMPONENT

    feats = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    component = MagicMock()
    component.get_entity = lambda eid: NS(supported_features=feats) if eid == AI_ENTITY else None
    hass.data[DATA_COMPONENT] = component
    entry = _entry(None, url=False, options={"ai_entity": AI_ENTITY})
    gen = AsyncMock(return_value=NS(data={"text": "ok"}))
    with patch("homeassistant.components.ai_task.async_generate_data", new=gen):
        result = await _route(
            hass, entry, image=_same_in_region_gif(), backend="ai_task",
            all_frames=True, region=(0, 0, 5, 5),
        )
    assert gen.await_count == 3
    assert [f["text"] for f in result["frames"]] == ["ok", "ok", "ok"]
    assert result["text"] == "ok\n\nok\n\nok"
    assert result["width"] == 5 and result["height"] == 5
    assert result["routed"] == "ai_task"

"""Tests for the private attachment media source."""

import asyncio
import stat
from pathlib import Path

import pytest
from homeassistant.components import media_source as ms
from homeassistant.components.media_source import MediaSourceItem, Unresolvable
from homeassistant.setup import async_setup_component

from custom_components.text_reader.attachments import TMP_DIRNAME, stage_image
from custom_components.text_reader.const import DOMAIN
from custom_components.text_reader.media_source import async_get_media_source

PREFIX = "media-source://text_reader/"


def _tmp(hass) -> Path:
    return Path(hass.config.path(TMP_DIRNAME))


def _item(hass, identifier: str) -> MediaSourceItem:
    return MediaSourceItem(hass, DOMAIN, identifier, None)


async def test_stage_writes_0600_and_deletes(hass):
    async with stage_image(hass, b"abc", "image/png") as mcid:
        assert mcid.startswith(PREFIX)
        token = mcid[len(PREFIX):]
        f = _tmp(hass) / token
        assert f.read_bytes() == b"abc"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert stat.S_IMODE(_tmp(hass).stat().st_mode) == 0o700
    assert not f.exists()
    assert token not in hass.data[DOMAIN]["staged"]


async def test_stage_deletes_on_exception(hass):
    with pytest.raises(RuntimeError):
        async with stage_image(hass, b"abc", "image/png") as mcid:
            f = _tmp(hass) / mcid[len(PREFIX):]
            assert f.exists()
            raise RuntimeError("boom")
    assert not f.exists()
    assert not hass.data[DOMAIN]["staged"]


async def test_stage_deletes_on_cancel(hass):
    entered = asyncio.Event()
    paths: list[Path] = []

    async def worker():
        async with stage_image(hass, b"abc", "image/png") as mcid:
            paths.append(_tmp(hass) / mcid[len(PREFIX):])
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(worker())
    await entered.wait()
    assert paths[0].exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].exists()
    assert not hass.data[DOMAIN]["staged"]


async def test_resolve_live_token_returns_path(hass):
    source = await async_get_media_source(hass)
    async with stage_image(hass, b"abc", "image/jpeg") as mcid:
        token = mcid[len(PREFIX):]
        media = await source.async_resolve_media(_item(hass, token))
        assert media.url == ""
        assert media.mime_type == "image/jpeg"
        assert media.path == _tmp(hass) / token
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, token))


async def test_resolve_unknown_token_unresolvable(hass):
    source = await async_get_media_source(hass)
    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item(hass, "nope"))


async def test_browse_is_empty_directory(hass):
    source = await async_get_media_source(hass)
    async with stage_image(hass, b"abc", "image/png") as mcid:
        result = await source.async_browse_media(_item(hass, ""))
        token = mcid[len(PREFIX):]
        assert result.title == "Text Reader"
        assert result.can_expand is True
        assert result.can_play is False
        assert result.children == []
        assert result.thumbnail is None
        assert token not in repr(result.as_dict())


async def test_discovered_via_media_source_api(hass):
    assert await async_setup_component(hass, "media_source", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    async with stage_image(hass, b"abc", "image/png") as mcid:
        media = await ms.async_resolve_media(hass, mcid, None)
        assert media.path == _tmp(hass) / mcid[len(PREFIX):]
        assert media.mime_type == "image/png"
    with pytest.raises(Unresolvable):
        await ms.async_resolve_media(hass, PREFIX + "unknown", None)


async def test_setup_cleans_leftovers(hass):
    d = _tmp(hass)
    d.mkdir(mode=0o700, exist_ok=True)
    (d / "leftover").write_bytes(b"x")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert not (d / "leftover").exists()


async def test_cancel_during_write_still_deletes_the_file(hass):
    # The executor thread keeps writing after the task is cancelled: the file must still go.
    import threading
    from unittest.mock import patch

    from custom_components.text_reader import attachments

    started, release, written = threading.Event(), threading.Event(), threading.Event()
    real_write = attachments._write

    def slow_write(directory, path, data):
        started.set()
        release.wait(5)
        real_write(directory, path, data)
        assert path.exists()  # the file really is created after the cancel
        written.set()

    async def stage():
        async with stage_image(hass, b"abc", "image/png"):
            pass  # pragma: no cover - cancelled before the body

    with patch.object(attachments, "_write", slow_write):
        task = hass.async_create_task(stage())
        await hass.async_add_executor_job(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await hass.async_add_executor_job(written.wait, 5)
        for _ in range(5):  # let the write's done-callbacks run on the loop
            await asyncio.sleep(0)
        await hass.async_block_till_done()
    assert not list(_tmp(hass).iterdir())
    assert not hass.data[DOMAIN]["staged"]

"""Short-lived private image files for AI Task attachments."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

TMP_DIRNAME = ".text_reader_tmp"


def tmp_dir(hass: HomeAssistant) -> Path:
    """Return the private temp directory."""
    return Path(hass.config.path(TMP_DIRNAME))


def staged_registry(hass: HomeAssistant) -> dict[str, tuple[Path, str]]:
    """Return the live token registry, creating it on first use."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    registry: dict[str, tuple[Path, str]] = domain_data.setdefault("staged", {})
    return registry


def _write(directory: Path, path: Path, data: bytes) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def _after_cancelled_write(done: asyncio.Future[None], path: Path) -> None:
    if not done.cancelled():
        done.exception()  # retrieved, so asyncio doesn't log it (it could name the path)
    _remove(path)


def cleanup_leftovers(directory: Path) -> None:
    """Delete files left behind by a crash (runs in the executor)."""
    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)


def stage_image(
    hass: HomeAssistant, data: bytes, mime: str
) -> AbstractAsyncContextManager[str]:
    """Stage image bytes privately; yield a media-source content id."""
    return _stage(hass, data, mime)


@asynccontextmanager
async def _stage(hass: HomeAssistant, data: bytes, mime: str) -> AsyncIterator[str]:
    token = secrets.token_urlsafe(16)
    directory = tmp_dir(hass)
    path = directory / token
    registry = staged_registry(hass)
    try:
        write = hass.async_add_executor_job(_write, directory, path, data)
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # The thread keeps writing after a cancel; the finally below can run before it creates
            # the file. Delete it again once the write is over (success or not).
            write.add_done_callback(lambda done: _after_cancelled_write(done, path))
            raise
        registry[token] = (path, mime)
        yield f"media-source://{DOMAIN}/{token}"
    finally:
        registry.pop(token, None)
        # No await here, so cancellation cannot skip the delete.
        _remove(path)

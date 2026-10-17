"""Route a read to the local App, the AI Task entity, or local-then-AI (auto)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .ai_backend import AiUnavailable, Region, ai_read
from .api import CannotConnect, ImageRejected, Timeout
from .data import TextReaderConfigEntry, settings

ROUTED_LOCAL = "local"
ROUTED_AI = "ai_task"
ROUTED_BOTH = "local→ai_task"



class NoLocalApp(Exception):
    """`backend: local` was asked of an entry that has no Local OCR app."""


def _mean_confidence(blocks: Any) -> float | None:
    values = [
        b["confidence"]
        for b in blocks or []
        if isinstance(b, dict) and isinstance(b.get("confidence"), (int, float))
    ]
    return sum(values) / len(values) if values else None


def needs_fallback(preset: str, result: dict[str, Any], threshold: float) -> bool:
    """True when a local result is too weak to trust (see the auto triggers)."""
    text = result.get("text")
    blocks = result.get("blocks") or []
    if not isinstance(text, str) or not text.strip() or not blocks:
        return True
    fields = result.get("fields") or {}
    if preset == "envelope" and fields.get("addressee") is None:
        return True
    if preset == "label" and not fields.get("tracking"):
        return True
    if preset == "meter" and fields.get("value") is None:
        return True
    confidence = _mean_confidence(blocks)
    return confidence is not None and confidence < threshold


def _label_local(result: dict[str, Any]) -> None:
    result["routed"] = ROUTED_LOCAL
    result["confidence"] = _mean_confidence(result.get("blocks"))


def _label_local_result(result: dict[str, Any]) -> dict[str, Any]:
    frames = result.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            _label_local(frame)
        all_blocks = [b for f in frames for b in f.get("blocks") or []]
        result["routed"] = ROUTED_LOCAL
        result["confidence"] = _mean_confidence(all_blocks)
    else:
        _label_local(result)
    return result


def _label_ai(result: dict[str, Any], routed: str) -> dict[str, Any]:
    result["routed"] = routed
    result["confidence"] = None
    for frame in result.get("frames") or []:
        frame["routed"] = routed
        frame["confidence"] = None
    return result


async def _ai(
    hass: HomeAssistant,
    entity_id: str,
    image: bytes,
    *,
    preset: str,
    region: Region | None,
    timeout: float,
    index: int | None = None,
    all_frames: bool = False,
) -> dict[str, Any]:
    """AI read. ai_read owns every image step (frame, EXIF rotation, crop, re-encode, limits)."""
    return await ai_read(
        hass,
        entity_id,
        image,
        preset=preset,
        all_frames=all_frames,
        region=region,
        index=index,
        timeout=timeout,
    )


async def route_read(
    hass: HomeAssistant,
    entry: TextReaderConfigEntry,
    image: bytes,
    *,
    backend: str,
    preset: str,
    min_confidence: float,
    region: Region | None,
    all_frames: bool,
    timeout: float,
) -> dict[str, Any]:
    """Read an image with the requested backend; always sets `routed` and `confidence`."""
    cfg = settings(entry)
    client = entry.runtime_data.client

    if backend == "local" and client is None:
        raise NoLocalApp
    if backend == "ai_task" or client is None:
        if not cfg.ai_entity:
            raise AiUnavailable("no_entity")
        result = await _ai(
            hass, cfg.ai_entity, image, preset=preset, region=region, timeout=timeout,
            all_frames=all_frames,
        )
        return _label_ai(result, ROUTED_AI)

    local_error: CannotConnect | Timeout | None = None
    local: dict[str, Any] | None = None
    try:
        local = await client.read(
            image,
            preset=preset,
            min_confidence=min_confidence,
            region=region,
            all_frames=all_frames,
            timeout=timeout,
        )
    except (CannotConnect, Timeout) as err:
        if backend != "auto":
            raise
        local_error = err

    if local is None:
        # auto and the local App is unreachable: AI is the only option.
        assert local_error is not None
        if not cfg.ai_entity:
            raise local_error
        ai_result: dict[str, Any] | None
        try:
            ai_result = await _ai(
                hass, cfg.ai_entity, image, preset=preset, region=region, timeout=timeout,
                all_frames=all_frames,
            )
        except AiUnavailable:
            ai_result = None
        if ai_result is None:
            raise local_error from None
        return _label_ai(ai_result, ROUTED_BOTH)

    _label_local_result(local)
    if backend != "auto" or not cfg.ai_entity:
        return local
    if isinstance(local.get("frames"), list):
        return await _route_frames(
            hass, cfg.ai_entity, cfg.auto_threshold, image, local, preset, region, timeout
        )
    if not needs_fallback(preset, local, cfg.auto_threshold):
        return local
    try:
        result = await _ai(
            hass, cfg.ai_entity, image, preset=preset, region=region, timeout=timeout
        )
    except (AiUnavailable, ImageRejected):
        return local
    return _label_ai(result, ROUTED_BOTH)


async def _route_frames(
    hass: HomeAssistant,
    entity_id: str,
    threshold: float,
    image: bytes,
    local: dict[str, Any],
    preset: str,
    region: Region | None,
    timeout: float,
) -> dict[str, Any]:
    """Re-read only the weak frames with the AI entity."""
    frames: list[dict[str, Any]] = local["frames"]
    fell_back = False
    for index, frame in enumerate(frames):
        if not needs_fallback(preset, frame, threshold):
            continue
        try:
            replacement = await _ai(
                hass, entity_id, image, preset=preset, region=region, timeout=timeout,
                index=index,
            )
        except (AiUnavailable, ImageRejected):
            continue
        frames[index] = _label_ai(replacement, ROUTED_BOTH)
        fell_back = True
    if fell_back:
        local["routed"] = ROUTED_BOTH
        local["text"] = "\n\n".join(f.get("text", "") for f in frames)
        local["confidence"] = None  # some frames were served by AI
    return local

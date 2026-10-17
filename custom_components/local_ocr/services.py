"""The local_ocr.read_image action."""

from __future__ import annotations

from asyncio import sleep
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import get_url

from .api import (
    Busy,
    CannotConnect,
    ImageRejected,
    LocalOcrError,
    ServerError,
    Timeout,
    Unauthorized,
)
from .const import DOMAIN, SIGNAL_DURATION_UPDATED
from .data import LocalOcrConfigEntry

SERVICE_READ_IMAGE = "read_image"
PRESETS = ["text", "envelope", "meter", "label"]
SOURCES = ("image_entity", "camera_entity", "file_path", "media_content_id")
# Reading every frame of an animation takes about one single read per frame (2.5-4 s each on a Pi 4).
ALL_FRAMES_TIMEOUT = 180
# Server rejection codes with their own plain-language message; other codes use image_rejected.
REJECTION_KEYS = ("too_large", "unsupported_image", "bad_request", "unsupported_lang")

REGION_SCHEMA = vol.Schema(
    {
        vol.Required("x"): vol.Coerce(float),
        vol.Required("y"): vol.Coerce(float),
        vol.Required("w"): vol.Coerce(float),
        vol.Required("h"): vol.Coerce(float),
    }
)

READ_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("image_entity"): cv.entity_id,
        vol.Optional("camera_entity"): cv.entity_id,
        vol.Optional("file_path"): cv.string,
        vol.Optional("media_content_id"): cv.string,
        vol.Optional("preset"): vol.In(PRESETS),
        vol.Optional("region"): REGION_SCHEMA,
        vol.Optional("min_confidence"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=1)
        ),
        vol.Optional("all_frames"): cv.boolean,
    }
)


def _get_entry(hass: HomeAssistant, entry_id: str | None) -> LocalOcrConfigEntry:
    loaded = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if hasattr(e, "runtime_data")
    ]
    if entry_id is not None:
        for e in loaded:
            if e.entry_id == entry_id:
                return e
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    if len(loaded) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    return loaded[0]


def _no_image(entity_id: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="no_image",
        translation_placeholders={"entity_id": entity_id},
    )


async def _load_image(hass: HomeAssistant, call_data: dict[str, Any]) -> bytes:
    if (entity_id := call_data.get("image_entity")) is not None:
        from homeassistant.components import image

        try:
            return (await image.async_get_image(hass, entity_id)).content
        except HomeAssistantError as err:
            raise _no_image(entity_id) from err

    if (entity_id := call_data.get("camera_entity")) is not None:
        from homeassistant.components import camera

        try:
            return (await camera.async_get_image(hass, entity_id)).content
        except HomeAssistantError as err:
            raise _no_image(entity_id) from err

    if (path := call_data.get("file_path")) is not None:
        if not hass.config.is_allowed_path(path):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
                translation_placeholders={"path": path},
            )
        try:
            return await hass.async_add_executor_job(Path(path).read_bytes)
        except OSError as err:
            raise _no_image(path) from err

    media_id = call_data["media_content_id"]
    from homeassistant.components import media_source

    try:
        resolved = await media_source.async_resolve_media(hass, media_id, None)
    except (HomeAssistantError, ValueError) as err:
        raise _no_image(media_id) from err
    if (local := getattr(resolved, "path", None)) is not None:
        try:
            return await hass.async_add_executor_job(Path(local).read_bytes)
        except OSError as err:
            raise _no_image(media_id) from err
    url = resolved.url
    try:
        if url.startswith("/"):
            url = f"{get_url(hass)}{url}"
        async with async_get_clientsession(hass).get(url) as resp:
            resp.raise_for_status()
            return await resp.read()
    except Exception as err:  # noqa: BLE001 - never echo the URL
        raise _no_image(media_id) from err


def _image_size(data: bytes) -> tuple[int, int]:
    """The image's size as the server sees it: after EXIF rotation (orientations 5-8 swap w and h)."""
    from PIL import Image

    with Image.open(BytesIO(data)) as img:
        width, height = img.size
        try:
            orientation = int(img.getexif().get(0x0112, 1))
        except (TypeError, ValueError, OSError, SyntaxError):
            orientation = 1
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def _invalid_region() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="invalid_region"
    )


async def _region_px(
    hass: HomeAssistant, region: dict[str, float], image: bytes
) -> tuple[int, int, int, int]:
    x, y, w, h = (region[k] for k in ("x", "y", "w", "h"))
    values = (x, y, w, h)
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise _invalid_region()
    if max(values) <= 1:
        try:
            width, height = await hass.async_add_executor_job(_image_size, image)
        except Exception as err:  # noqa: BLE001 - PIL raises many types
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unsupported_image"
            ) from err
        px = (round(x * width), round(y * height), round(w * width), round(h * height))
    else:
        if any(0 < v < 1 for v in values):
            raise _invalid_region()
        px = (round(x), round(y), round(w), round(h))
    if px[2] < 1 or px[3] < 1:
        raise _invalid_region()
    return px


async def _read_image(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    data = call.data
    if sum(1 for s in SOURCES if data.get(s) is not None) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="exactly_one_source"
        )
    entry = _get_entry(hass, data.get("config_entry_id"))
    options = entry.options
    preset = data.get("preset") or options.get("preset", "text")
    min_conf = data.get("min_confidence", options.get("min_confidence", 0.5))
    timeout = float(options.get("timeout", 30))
    if data.get("all_frames", False):
        timeout = max(timeout, ALL_FRAMES_TIMEOUT)

    started = monotonic()
    image = await _load_image(hass, data)
    region = (
        await _region_px(hass, data["region"], image) if "region" in data else None
    )

    try:
        result = await _read_with_retry(
            hass, entry, image, preset, min_conf, region, timeout,
            data.get("all_frames", False),
        )
    except Exception as err:
        # Translated errors are raised "from None"; the client error is the context.
        root = err.__context__ if isinstance(err.__context__, LocalOcrError) else err
        _record(entry, None, preset, None, root)
        raise
    _record(entry, result, preset, monotonic() - started, None)
    return result


def _record(
    entry: LocalOcrConfigEntry,
    result: dict[str, Any] | None,
    preset: str,
    elapsed: float | None,
    err: Exception | None,
) -> None:
    """Append a privacy-safe request record (never any text)."""
    ms = result.get("ms") if isinstance(result, dict) else None
    if not isinstance(ms, (int, float)):
        ms = round(elapsed * 1000) if elapsed is not None else None
    blocks = result.get("blocks") if isinstance(result, dict) else None
    entry.runtime_data.recent.append(
        {
            "ms": ms,
            "preset": preset,
            "width": result.get("width") if isinstance(result, dict) else None,
            "height": result.get("height") if isinstance(result, dict) else None,
            "blocks": len(blocks) if isinstance(blocks, list) else 0,
            "error": type(err).__name__ if err is not None else None,
        }
    )


async def _read_with_retry(
    hass: HomeAssistant,
    entry: LocalOcrConfigEntry,
    image: bytes,
    preset: str,
    min_conf: float,
    region: tuple[int, int, int, int] | None,
    timeout: float,
    all_frames: bool,
) -> ServiceResponse:
    client = entry.runtime_data.client
    start = monotonic()
    timed_out = HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="timeout",
        translation_placeholders={"timeout": f"{timeout:g}"},
    )
    while True:
        try:
            result = await client.read(
                image,
                preset=preset,
                min_confidence=min_conf,
                region=region,
                all_frames=all_frames,
                timeout=timeout,
            )
            ms = result.get("ms") if isinstance(result, dict) else None
            if not isinstance(ms, (int, float)):
                ms = (monotonic() - start) * 1000
            entry.runtime_data.last_duration_ms = round(ms)
            async_dispatcher_send(hass, SIGNAL_DURATION_UPDATED)
            return result
        except Busy as busy:
            delay = max(busy.retry_after, 0.5)
            if monotonic() - start + delay > timeout:
                raise timed_out from None
            await sleep(delay)
        except Timeout:
            raise timed_out from None
        except ImageRejected as err:
            if err.code in REJECTION_KEYS:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=err.code
                ) from None
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="image_rejected",
                translation_placeholders={"code": err.code},
            ) from None
        except Unauthorized:
            entry.async_start_reauth(hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from None
        except CannotConnect:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="app_unavailable"
            ) from None
        except ServerError as err:
            # The server's own error text (never recognised text).
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="server_error",
                translation_placeholders={"message": str(err)},
            ) from None
        except LocalOcrError as err:
            raise HomeAssistantError(str(err)) from None


def async_register_services(hass: HomeAssistant) -> None:
    """Register the Local OCR actions."""

    async def handle(call: ServiceCall) -> ServiceResponse:
        return await _read_image(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_IMAGE,
        handle,
        schema=READ_IMAGE_SCHEMA,  # type: ignore[arg-type]  # HA shims voluptuous to probatio
        supports_response=SupportsResponse.ONLY,
    )

"""The local_ocr.read_image action."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
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
from homeassistant.helpers.network import get_url

from .api import (
    Busy,
    CannotConnect,
    ImageRejected,
    LocalOcrError,
    Unauthorized,
)
from .const import DOMAIN
from .data import LocalOcrConfigEntry

SERVICE_READ_IMAGE = "read_image"
SOURCES = ("image_entity", "camera_entity", "file_path", "media_content_id")

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
        vol.Optional("preset"): cv.string,
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
    if url.startswith("/"):
        url = f"{get_url(hass)}{url}"
    try:
        async with async_get_clientsession(hass).get(url) as resp:
            resp.raise_for_status()
            return await resp.read()
    except Exception as err:  # noqa: BLE001 - never echo the URL
        raise _no_image(media_id) from err


def _image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(BytesIO(data)) as img:
        return img.size


async def _region_px(
    hass: HomeAssistant, region: dict[str, float], image: bytes
) -> tuple[int, int, int, int]:
    x, y, w, h = (region[k] for k in ("x", "y", "w", "h"))
    if max(x, y, w, h) <= 1:
        try:
            width, height = await hass.async_add_executor_job(_image_size, image)
        except Exception as err:  # noqa: BLE001 - PIL raises many types
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="image_rejected",
                translation_placeholders={"code": "unsupported_image"},
            ) from err
        return (round(x * width), round(y * height), round(w * width), round(h * height))
    return (round(x), round(y), round(w), round(h))


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
    timeout = options.get("timeout", 30)

    image = await _load_image(hass, data)
    region = (
        await _region_px(hass, data["region"], image) if "region" in data else None
    )

    client = entry.runtime_data.client
    waited = 0.0
    while True:
        try:
            return await client.read(
                image,
                preset=preset,
                min_confidence=min_conf,
                region=region,
                all_frames=data.get("all_frames", False),
            )
        except Busy as busy:
            if waited + busy.retry_after > timeout:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="timeout",
                    translation_placeholders={"timeout": f"{timeout:g}"},
                ) from None
            waited += busy.retry_after
            await asyncio.sleep(busy.retry_after)
        except ImageRejected as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="image_rejected",
                translation_placeholders={"code": err.code},
            ) from None
        except (CannotConnect, Unauthorized):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="app_unavailable"
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
        schema=READ_IMAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

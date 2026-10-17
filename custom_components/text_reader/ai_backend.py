"""Read text with a Home Assistant AI Task entity (fixed instructions, no user prompt)."""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import probatio
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from PIL import Image

from .api import ImageRejected
from .attachments import stage_image

_LOGGER = logging.getLogger(__name__)

AI_PROMPT_VERSION = 1
MAX_FRAMES = 50

_INSTRUCTION_TEMPLATE = (
    "You are a transcription tool. Read the text in the attached image exactly as "
    "printed. Do not guess, correct, summarise or translate. If a field is not "
    "present or not legible, return null for it (or an empty list). Return only the "
    "requested fields: {fields}."
)

_FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "envelope": ("text", "addressee", "return_address", "markers", "generic_addressee"),
    "label": ("text", "tracking", "ship_to"),
    "meter": ("text", "value", "digits"),
}

INSTRUCTIONS: dict[str, str] = {
    preset: _INSTRUCTION_TEMPLATE.format(fields=", ".join(names))
    for preset, names in _FIELD_NAMES.items()
}

_STRING: dict[str, Any] = {"text": {}}
_STRING_LIST: dict[str, Any] = {"text": {"multiple": True}}
_TRACKING: dict[str, Any] = {
    "object": {
        "multiple": True,
        "fields": {
            "carrier": {"selector": _STRING, "required": True},
            "number": {"selector": _STRING, "required": True},
        },
    }
}

# Same shape the ai_task.generate_data service accepts for `structure`.
_STRUCTURE_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "text": {
        "text": {"selector": _STRING, "required": True, "description": "All text, top to bottom"},
    },
    "envelope": {
        "text": {"selector": _STRING, "required": True, "description": "All text, top to bottom"},
        "addressee": {"selector": _STRING_LIST, "description": "Recipient address lines, top to bottom"},
        "return_address": {"selector": _STRING_LIST, "description": "Sender address lines, top to bottom"},
        "markers": {
            "selector": _STRING_LIST,
            "required": True,
            "description": "Bulk-mail markers such as PRSRT STD, ECRWSS",
        },
        "generic_addressee": {
            "selector": {"boolean": {}},
            "required": True,
            "description": "True if addressed to Current Resident, Occupant or similar",
        },
    },
    "label": {
        "text": {"selector": _STRING, "required": True, "description": "All text, top to bottom"},
        "tracking": {
            "selector": _TRACKING,
            "required": True,
            "description": "Tracking numbers with carrier ups/usps/fedex/dhl",
        },
        "ship_to": {"selector": _STRING_LIST, "description": "Ship-to address lines, top to bottom"},
    },
    "meter": {
        "text": {"selector": _STRING, "required": True, "description": "All text, top to bottom"},
        "value": {
            "selector": {"number": {"mode": "box", "step": "any"}},
            "description": "The meter reading as a number",
        },
        "digits": {"selector": _STRING, "required": True, "description": "The digits shown, as printed"},
    },
}


class AiUnavailable(Exception):
    """The AI Task entity can't be used (missing, unavailable, unsupported, or it failed)."""


def structure_for(preset: str) -> probatio.Schema:
    """Build the probatio schema exactly as ai_task's generate_data service does."""
    spec = _STRUCTURE_SPECS[preset]
    fields: dict[Any, Any] = {}
    for name, item in spec.items():
        marker = probatio.Required if item.get("required") else probatio.Optional
        fields[marker(name, description=item.get("description"))] = selector.selector(
            item["selector"]
        )
    return probatio.Schema(fields, extra=probatio.PREVENT_EXTRA)


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, str)]


def _lines(value: Any) -> dict[str, Any] | None:
    lines = _str_list(value)
    return None if lines is None else {"lines": lines, "confidence": None}


def _tracking(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if (
            isinstance(item, dict)
            and isinstance(item.get("carrier"), str)
            and isinstance(item.get("number"), str)
        ):
            out.append({"carrier": item["carrier"], "number": item["number"]})
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def normalise(preset: str, data: Any) -> tuple[str, dict[str, Any]]:
    """Map the model's data to (text, contract fields); never raises."""
    if not isinstance(data, dict):
        data = {}
    text = data.get("text")
    text = text if isinstance(text, str) else ""
    fields: dict[str, Any] = {}
    if preset == "envelope":
        markers = _str_list(data.get("markers"))
        generic = data.get("generic_addressee")
        fields = {
            "addressee": _lines(data.get("addressee")),
            "return_address": _lines(data.get("return_address")),
            "markers": markers if markers is not None else [],
            "generic_addressee": generic if isinstance(generic, bool) else False,
        }
    elif preset == "label":
        fields = {
            "tracking": _tracking(data.get("tracking")),
            "ship_to": _lines(data.get("ship_to")),
        }
    elif preset == "meter":
        digits = data.get("digits")
        fields = {
            "value": _number(data.get("value")),
            "digits": digits if isinstance(digits, str) else "",
        }
    return text, fields


def _entity_problem(hass: HomeAssistant, entity_id: str) -> str | None:
    # Lazy: ai_task pulls in conversation, which core/venv installs may lack.
    from homeassistant.components.ai_task import AITaskEntityFeature
    from homeassistant.components.ai_task.const import DATA_COMPONENT

    component = hass.data.get(DATA_COMPONENT)
    entity = component.get_entity(entity_id) if component is not None else None
    if entity is None:
        return "entity_not_found"
    state = hass.states.get(entity_id)
    if state is not None and state.state == STATE_UNAVAILABLE:
        return "entity_unavailable"
    needed = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    if needed & entity.supported_features != needed:
        return "entity_lacks_features"
    return None


def _header(image: bytes) -> tuple[int, int, str, int]:
    """(width, height, mime, n_frames) from the Pillow header."""
    try:
        with Image.open(io.BytesIO(image)) as img:
            fmt = img.format or "PNG"
            return (
                img.width,
                img.height,
                Image.MIME.get(fmt, "image/png"),
                getattr(img, "n_frames", 1),
            )
    except Exception as err:
        raise ImageRejected("unsupported_image", "could not decode image") from err


def _frames_png(image: bytes) -> list[bytes]:
    out: list[bytes] = []
    try:
        with Image.open(io.BytesIO(image)) as img:
            for index in range(getattr(img, "n_frames", 1)):
                img.seek(index)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                out.append(buf.getvalue())
    except Exception as err:
        raise ImageRejected("unsupported_image", "could not decode frame") from err
    return out


async def _call(
    hass: HomeAssistant, entity_id: str, data: bytes, mime: str, preset: str
) -> Any:
    from homeassistant.components.ai_task import async_generate_data

    async with stage_image(hass, data, mime) as media_content_id:
        try:
            result = await async_generate_data(
                hass,
                task_name=f"Text Reader {preset}",
                entity_id=entity_id,
                instructions=INSTRUCTIONS[preset],
                structure=structure_for(preset),
                attachments=[
                    {"media_content_id": media_content_id, "media_content_type": mime}
                ],
            )
        except HomeAssistantError as err:
            raise AiUnavailable(type(err).__name__) from err
    return result.data


async def _one(
    hass: HomeAssistant,
    entity_id: str,
    data: bytes,
    mime: str,
    preset: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    start = time.monotonic()
    raw = await _call(hass, entity_id, data, mime, preset)
    text, fields = normalise(preset, raw)
    ms = int((time.monotonic() - start) * 1000)
    _LOGGER.debug("ai_task read entity=%s preset=%s ms=%d", entity_id, preset, ms)
    return {
        "text": text,
        "blocks": [],
        "fields": fields,
        "width": width,
        "height": height,
        "ms": ms,
        "engine": f"ai_task:{entity_id}",
        "model": None,
        "preset": preset,
        "confidence": None,
    }


async def ai_read(
    hass: HomeAssistant,
    entity_id: str,
    image: bytes,
    *,
    preset: str,
    all_frames: bool = False,
) -> dict[str, Any]:
    """Read an image with the AI Task entity; returns a contract-shaped dict."""
    start = time.monotonic()
    try:
        problem = _entity_problem(hass, entity_id)
    except ImportError as err:
        raise AiUnavailable("ai_task_missing") from err
    if problem is not None:
        raise AiUnavailable(problem)

    width, height, mime, n_frames = await hass.async_add_executor_job(_header, image)
    if not all_frames:
        single = await _one(hass, entity_id, image, mime, preset, width, height)
        single["ms"] = int((time.monotonic() - start) * 1000)
        return single

    if n_frames > MAX_FRAMES:
        raise ImageRejected("too_large", "too many frames")
    pngs = await hass.async_add_executor_job(_frames_png, image)
    frames = [
        await _one(hass, entity_id, png, "image/png", preset, width, height)
        for png in pngs
    ]
    return {
        "frames": frames,
        "text": "\n\n".join(f["text"] for f in frames),
        "blocks": [],
        "width": width,
        "height": height,
        "ms": int((time.monotonic() - start) * 1000),
        "engine": f"ai_task:{entity_id}",
        "model": None,
        "preset": preset,
        "confidence": None,
    }

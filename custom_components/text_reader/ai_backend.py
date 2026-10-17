"""Read text with a Home Assistant AI Task entity (fixed instructions, no user prompt)."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

import probatio
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from PIL import Image, ImageOps

from .api import ImageRejected
from .attachments import stage_image

_LOGGER = logging.getLogger(__name__)

AI_PROMPT_VERSION = 1
MAX_FRAMES = 50
# The local App's limit: decoding more than this needs too much memory on a Pi.
MAX_PIXELS = 40_000_000
# Above this, a photo is re-encoded as JPEG (quality 90) rather than a much larger PNG.
PHOTO_PIXELS = 2_000_000
# Formats every assistant accepts as-is (when single-frame, unrotated and uncropped).
_PASSTHROUGH = {"JPEG", "PNG", "WEBP"}
_PHOTO_FORMATS = {"JPEG", "MPO"}
# Each AI call (one per frame) gets at least this long, whatever the read timeout option says.
AI_MIN_TIMEOUT = 120

type Region = tuple[int, int, int, int]

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


def _lines_or_none(value: Any) -> dict[str, Any] | None:
    """Address lines as the App returns them; an empty list is "not found" (null), like local."""
    lines = _str_list(value)
    return {"lines": lines, "confidence": None} if lines else None


def normalise(preset: str, data: Any) -> tuple[str, dict[str, Any] | None]:
    """Map the model's data to (text, contract fields) shaped like the local App's; never raises."""
    if not isinstance(data, dict):
        data = {}
    text = data.get("text")
    text = text if isinstance(text, str) else ""
    fields: dict[str, Any] | None = None
    if preset == "envelope":
        markers = _str_list(data.get("markers"))
        generic = data.get("generic_addressee")
        fields = {
            "addressee": _lines_or_none(data.get("addressee")),
            "return_address": _lines_or_none(data.get("return_address")),
            "indicia": [],  # the assistant isn't asked for indicia: always an empty list
            "markers": markers if markers is not None else [],
            "generic_addressee": generic if isinstance(generic, bool) else False,
        }
    elif preset == "label":
        fields = {
            "tracking": _tracking(data.get("tracking")),
            "ship_to": _lines_or_none(data.get("ship_to")),
        }
    elif preset == "meter":
        digits = data.get("digits")
        fields = {
            "value": _number(data.get("value")),
            "digits": digits if isinstance(digits, str) else "",
            "confidence": None,
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


def eligible_entities(hass: HomeAssistant) -> list[str]:
    """AI Task entities that can read an image right now (data with attachments, available)."""
    try:
        return sorted(
            entity_id
            for entity_id in hass.states.async_entity_ids("ai_task")
            if _entity_problem(hass, entity_id) is None
        )
    except ImportError:
        return []


@dataclass(frozen=True)
class _Frame:
    """One frame, ready to send: bytes, mime, and its size after EXIF rotation (uncropped)."""

    data: bytes
    mime: str
    width: int
    height: int


def _orientation(img: Image.Image) -> int:
    try:
        return int(img.getexif().get(0x0112, 1))
    except (TypeError, ValueError, OSError, SyntaxError):
        return 1


def _to_rgb(img: Image.Image) -> Image.Image:
    """RGB, with any transparency flattened onto white (as the App does)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img if img.mode == "RGB" else img.convert("RGB")


def _strip_metadata(img: Image.Image, fmt: str) -> bytes:
    """Re-encode a pass-through image without EXIF (which can carry GPS), text, XMP or comments.

    JPEG is re-encoded at the source quality (`quality="keep"`) without metadata: not
    byte-identical, and progressive becomes baseline. PNG and WEBP are saved lossless. The ICC
    colour profile (not personal data) is carried over.
    """
    icc = img.info.get("icc_profile")
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(
            buf, format="JPEG", quality="keep", exif=b"", comment=b"", icc_profile=icc
        )
    elif fmt == "WEBP":
        img.save(buf, format="WEBP", lossless=True, exif=b"", xmp=b"", icc_profile=icc)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


def _encode_frame(
    img: Image.Image, source: bytes, region: Region | None, single: bool
) -> _Frame:
    orientation = _orientation(img)
    rotate = 2 <= orientation <= 8
    w, h = img.size
    width, height = (h, w) if orientation in (5, 6, 7, 8) else (w, h)
    fmt = img.format or ""
    if single and not rotate and region is None and fmt in _PASSTHROUGH:
        return _Frame(_strip_metadata(img, fmt), Image.MIME[fmt], width, height)
    frame = ImageOps.exif_transpose(img) if rotate else img
    if region is not None:
        x, y, rw, rh = region
        if x >= width or y >= height:
            raise ImageRejected("bad_request", "region starts outside the image")
        frame = frame.crop((x, y, min(width, x + rw), min(height, y + rh)))
    frame = _to_rgb(frame)
    buf = io.BytesIO()
    if fmt in _PHOTO_FORMATS and frame.width * frame.height > PHOTO_PIXELS:
        frame.save(buf, format="JPEG", quality=90)
        return _Frame(buf.getvalue(), "image/jpeg", width, height)
    frame.save(buf, format="PNG")
    return _Frame(buf.getvalue(), "image/png", width, height)


def prepare_frames(
    image: bytes, *, region: Region | None, index: int | None, all_frames: bool
) -> list[_Frame]:
    """The ONE image normaliser for every AI path (runs in the executor).

    Opens the image, picks the frame(s) (frame `index`, default 0, unless all_frames), applies the
    EXIF rotation, crops the region in rotated coordinates, and re-encodes unless the source is
    already a single-frame JPEG/PNG/WEBP with no rotation and no crop (then only its metadata is
    stripped). Raises ImageRejected.
    """
    try:
        with Image.open(io.BytesIO(image)) as img:
            return _frames_of(img, image, region=region, index=index, all_frames=all_frames)
    except ImageRejected:
        raise
    except Image.DecompressionBombError:
        raise ImageRejected("too_large", "too many pixels") from None
    except Exception:  # noqa: BLE001  # Pillow raises many types for a bad file
        raise ImageRejected("unsupported_image", "could not decode image") from None


def _frames_of(
    img: Image.Image, source: bytes, *, region: Region | None, index: int | None, all_frames: bool
) -> list[_Frame]:
    if img.width * img.height > MAX_PIXELS:
        raise ImageRejected("too_large", "too many pixels")
    count = getattr(img, "n_frames", 1)
    if all_frames:
        if count > MAX_FRAMES:
            raise ImageRejected("too_large", "too many frames")
        indices = list(range(count))
    else:
        indices = [index or 0]
        if indices[0] >= count:
            raise ImageRejected("bad_request", "frame out of range")
    out: list[_Frame] = []
    for i in indices:
        img.seek(i)
        out.append(_encode_frame(img, source, region, count == 1))
    return out


def ai_budget(timeout: float) -> float:
    """Seconds allowed for one AI call: the read timeout, but never under AI_MIN_TIMEOUT."""
    return max(float(timeout), float(AI_MIN_TIMEOUT))


async def _call(
    hass: HomeAssistant, entity_id: str, data: bytes, mime: str, preset: str, budget: float
) -> Any:
    from homeassistant.components.ai_task import async_generate_data

    # Any failure is AiUnavailable with only the error class: never the message, which can hold
    # recognised text. CancelledError is not an Exception, so cancellation still propagates.
    try:
        async with asyncio.timeout(budget):
            async with stage_image(hass, data, mime) as media_content_id:
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
                return result.data
    except TimeoutError:
        raise AiUnavailable("timeout") from None
    except Exception as err:  # noqa: BLE001
        raise AiUnavailable(type(err).__name__) from None


async def _one(
    hass: HomeAssistant, entity_id: str, frame: _Frame, preset: str, budget: float
) -> dict[str, Any]:
    start = time.monotonic()
    raw = await _call(hass, entity_id, frame.data, frame.mime, preset, budget)
    text, fields = normalise(preset, raw)
    ms = int((time.monotonic() - start) * 1000)
    _LOGGER.debug("ai_task read entity=%s preset=%s ms=%d", entity_id, preset, ms)
    return {
        "text": text,
        "blocks": [],
        "fields": fields,
        "width": frame.width,
        "height": frame.height,
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
    region: Region | None = None,
    index: int | None = None,
    timeout: float = AI_MIN_TIMEOUT,
) -> dict[str, Any]:
    """Read an image with the AI Task entity; returns a contract-shaped dict.

    Width and height are the frame's size after EXIF rotation and before any crop, as the App
    reports. With all_frames the result is {frames, text}, like the App's.
    """
    start = time.monotonic()
    try:
        problem = _entity_problem(hass, entity_id)
    except ImportError as err:
        raise AiUnavailable("ai_task_missing") from err
    if problem is not None:
        raise AiUnavailable(problem)

    frames = await hass.async_add_executor_job(
        lambda: prepare_frames(image, region=region, index=index, all_frames=all_frames)
    )
    budget = ai_budget(timeout)
    if not all_frames:
        single = await _one(hass, entity_id, frames[0], preset, budget)
        single["ms"] = int((time.monotonic() - start) * 1000)
        return single
    results = [await _one(hass, entity_id, frame, preset, budget) for frame in frames]
    return {"frames": results, "text": "\n\n".join(r["text"] for r in results)}

"""Tests for the Local OCR API client."""

import ast
import re
from pathlib import Path

import aiohttp
import pytest
from unittest.mock import patch
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.local_ocr.api import (
    Busy,
    CannotConnect,
    ImageRejected,
    Info,
    LocalOcrClient,
    ServerError,
    Unauthorized,
)

URL = "http://ocr.test:8738"


def _client(hass, token="tok"):
    return LocalOcrClient(async_get_clientsession(hass), URL, token, 5)


async def test_health(hass, aioclient_mock):
    aioclient_mock.get(
        f"{URL}/v1/health",
        json={"status": "ok", "model_state": "ready", "detail": None},
    )
    health = await _client(hass).health()
    assert (health.status, health.model_state, health.detail) == ("ok", "ready", None)


async def test_info_ignores_unknown_keys(hass, aioclient_mock):
    aioclient_mock.get(
        f"{URL}/v1/info",
        json={
            "version": "0.1.0",
            "api": 1,
            "arch": "amd64",
            "engine": "rapidocr",
            "models": [],
            "presets": ["text", "mail"],
            "langs": ["en"],
            "limits": {"max_bytes": 10485760, "max_side": 4000},
            "future_key": 1,
        },
    )
    info = await _client(hass).info()
    assert isinstance(info, Info)
    assert info.api == 1
    assert info.presets == ["text", "mail"]
    assert info.limits["max_side"] == 4000


async def test_read_ok_sends_form_and_auth(hass, aioclient_mock):
    aioclient_mock.post(f"{URL}/v1/read", json={"text": "hi", "blocks": []})
    result = await _client(hass).read(
        b"img", preset="mail", min_confidence=0.7, region=(1, 2, 3, 4), all_frames=True
    )
    assert result["text"] == "hi"
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer tok"
    fields = {
        opts["name"]: value for opts, _hdrs, value in data._fields
    }
    assert fields["image"] == b"img"
    assert fields["preset"] == "mail"
    assert fields["min_confidence"] == "0.7"
    assert fields["region"] == "1,2,3,4"
    assert fields["all_frames"] == "true"


async def test_no_auth_header_without_token(hass, aioclient_mock):
    aioclient_mock.get(
        f"{URL}/v1/info",
        json={"version": "1", "api": 1, "arch": "a", "engine": "e",
              "presets": [], "limits": {}},
    )
    await LocalOcrClient(async_get_clientsession(hass), URL, None).info()
    assert "Authorization" not in (aioclient_mock.mock_calls[0][3] or {})


async def test_401(hass, aioclient_mock):
    aioclient_mock.post(
        f"{URL}/v1/read", status=401, json={"error": "unauthorized", "message": "x"}
    )
    with pytest.raises(Unauthorized):
        await _client(hass).read(b"x")


@pytest.mark.parametrize(
    ("status", "code"),
    [(413, "too_large"), (415, "unsupported_image"), (400, "bad_request")],
)
async def test_image_rejected(hass, aioclient_mock, status, code):
    aioclient_mock.post(
        f"{URL}/v1/read", status=status, json={"error": code, "message": "m"}
    )
    with pytest.raises(ImageRejected) as err:
        await _client(hass).read(b"x")
    assert err.value.code == code


async def test_busy(hass, aioclient_mock):
    aioclient_mock.post(
        f"{URL}/v1/read",
        status=503,
        json={"error": "busy", "message": "m"},
        headers={"Retry-After": "5"},
    )
    with pytest.raises(Busy) as err:
        await _client(hass).read(b"x")
    assert err.value.retry_after == 5.0


async def test_server_error_keeps_message(hass, aioclient_mock):
    aioclient_mock.post(
        f"{URL}/v1/read",
        status=500,
        json={"error": "model_error", "message": "out of memory: boom"},
    )
    with pytest.raises(ServerError, match="out of memory"):
        await _client(hass).read(b"x")


async def test_connection_error(hass, aioclient_mock):
    aioclient_mock.post(f"{URL}/v1/read", exc=aiohttp.ClientError())
    with pytest.raises(CannotConnect):
        await _client(hass).read(b"x")


async def test_timeout(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/health", exc=TimeoutError())
    with pytest.raises(CannotConnect):
        await _client(hass).health()


async def test_info_missing_required_keys(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", json={"version": "1"})
    with pytest.raises(ServerError, match="invalid info"):
        await _client(hass).info()


async def test_health_missing_keys(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/health", json={})
    with pytest.raises(ServerError, match="invalid health"):
        await _client(hass).health()


@pytest.mark.parametrize(
    ("header", "expected"),
    [("nan", 5.0), ("inf", 5.0), ("-inf", 0.0), ("-3", 0.0), ("abc", 5.0), ("7", 7.0)],
)
async def test_busy_retry_after_sanitised(hass, aioclient_mock, header, expected):
    aioclient_mock.post(
        f"{URL}/v1/read",
        status=503,
        json={"error": "busy", "message": "m"},
        headers={"Retry-After": header},
    )
    with pytest.raises(Busy) as err:
        await _client(hass).read(b"x")
    assert err.value.retry_after == expected


async def test_payload_error_is_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", exc=aiohttp.ClientPayloadError("cut"))
    with pytest.raises(CannotConnect):
        await _client(hass).info()


async def test_invalid_header_never_leaks_token(hass, aioclient_mock):
    secret = "sekrit\ntoken"
    aioclient_mock.get(f"{URL}/v1/info", exc=ValueError(f"bad header {secret}"))
    with pytest.raises(Unauthorized) as err:
        await LocalOcrClient(async_get_clientsession(hass), URL, secret).info()
    assert "sekrit" not in str(err.value)
    assert "sekrit" not in repr(err.value)


async def test_malformed_url_is_cannot_connect(hass, aioclient_mock):
    # The mock skips aiohttp's URL validation, so raise what aiohttp raises.
    aioclient_mock.get(
        re.compile(r".*bad host.*"), exc=aiohttp.InvalidURL("http://bad host:8738")
    )
    client = LocalOcrClient(async_get_clientsession(hass), "http://bad host:8738", "t")
    with pytest.raises(CannotConnect):
        await client.info()


async def test_body_read_payload_error_is_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", json={"version": "1"})
    with patch.object(
        AiohttpClientMockResponse,
        "json",
        side_effect=aiohttp.ClientPayloadError("truncated"),
    ):
        with pytest.raises(CannotConnect):
            await _client(hass).info()


def test_api_module_has_no_ha_imports():
    src = Path(__file__).parent.parent / "custom_components/local_ocr/api.py"
    tree = ast.parse(src.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert "homeassistant" not in mods
    allowed = {"aiohttp", "asyncio", "dataclasses", "typing", "__future__"}
    assert mods <= allowed, mods - allowed

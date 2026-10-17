"""Async client for the Local OCR server. Imports only aiohttp and stdlib."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields
from typing import Any

import aiohttp


class LocalOcrError(Exception):
    """Base error."""


class CannotConnect(LocalOcrError):
    """Server unreachable or timed out."""


class Unauthorized(LocalOcrError):
    """Token missing or wrong."""


class ImageRejected(LocalOcrError):
    """Server refused the image or request."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Busy(LocalOcrError):
    """Server queue is full."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"busy, retry after {retry_after}s")
        self.retry_after = retry_after


class ServerError(LocalOcrError):
    """Server-side failure (model_error etc.); message is kept."""


@dataclass
class Health:
    status: str
    model_state: str
    detail: str | None = None


@dataclass
class Info:
    version: str
    api: int
    arch: str
    engine: str
    presets: list[str] = field(default_factory=list)
    limits: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Info:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class LocalOcrClient:
    """Client for /v1/health, /v1/info and /v1/read."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str | None,
        timeout: float = 30,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        headers = self._headers() if auth else {}
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                headers=headers,
                timeout=self._timeout,
                **kwargs,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                retry = resp.headers.get("Retry-After")
        except ValueError as err:
            # aiohttp rejects invalid header values; never echo the token.
            raise Unauthorized("invalid token format") from None
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

        if status < 400:
            if not isinstance(body, dict):
                raise ServerError("invalid response from server")
            return body

        code = body.get("error", "") if isinstance(body, dict) else ""
        message = body.get("message", "") if isinstance(body, dict) else ""
        if status == 401:
            raise Unauthorized(message or "unauthorized")
        if status in (400, 413, 415):
            raise ImageRejected(code or "bad_request", message)
        if status == 503:
            try:
                delay = float(retry) if retry is not None else 5.0
            except ValueError:
                delay = 5.0
            if delay != delay or delay in (float("inf"), float("-inf")):  # nan/inf
                delay = 5.0
            raise Busy(max(delay, 0.0))
        raise ServerError(message or code or f"HTTP {status}")

    async def health(self) -> Health:
        data = await self._request("GET", "/v1/health", auth=False)
        try:
            return Health(
                status=data["status"],
                model_state=data["model_state"],
                detail=data.get("detail"),
            )
        except KeyError as err:
            raise ServerError("invalid health response") from err

    async def info(self) -> Info:
        data = await self._request("GET", "/v1/info")
        try:
            return Info.from_dict(data)
        except (TypeError, KeyError) as err:
            raise ServerError("invalid info response") from err

    async def read(
        self,
        image: bytes,
        *,
        preset: str = "text",
        min_confidence: float = 0.5,
        region: tuple[int, int, int, int] | None = None,
        all_frames: bool = False,
    ) -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field(
            "image", image, filename="image", content_type="application/octet-stream"
        )
        form.add_field("preset", preset)
        form.add_field("min_confidence", str(min_confidence))
        if region is not None:
            form.add_field("region", ",".join(str(int(v)) for v in region))
        form.add_field("all_frames", "true" if all_frames else "false")
        return await self._request("POST", "/v1/read", data=form)

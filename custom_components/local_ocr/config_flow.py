"""Config flow for Local OCR."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    CannotConnect,
    LocalOcrClient,
    LocalOcrError,
    ServerError,
    Unauthorized,
)
from .const import DEFAULT_PORT, DOMAIN, MIN_API, SAMPLE_PATH

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL = f"http://localhost:{DEFAULT_PORT}"
DEFAULT_OPTIONS = {"preset": "text", "min_confidence": 0.5, "timeout": 30}
PRESETS = ["text", "envelope", "meter", "label"]


def _clean_token(value: Any) -> str | None:
    """Strip whitespace; empty means no token."""
    return (value or "").strip() or None


def _host_url(host: str, port: int | None) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port or DEFAULT_PORT}"


class _CheckFailed(Exception):
    """A test-step check failed; carries the error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _async_check(
    hass: HomeAssistant, url: str, token: str | None, *, read_sample: bool = True
) -> dict[str, str]:
    """Run the connection checks; return description placeholders.

    Raises _CheckFailed(code). Never logs tokens, image bytes or recognised text.
    """
    client = LocalOcrClient(async_get_clientsession(hass), url, token)
    try:
        info = await client.info()
        if info.api < MIN_API:
            raise _CheckFailed("app_outdated")
        if not read_sample:
            return {}
        health = await client.health()
        if health.model_state == "error":
            raise _CheckFailed("model_failed")
        sample = await hass.async_add_executor_job(SAMPLE_PATH.read_bytes)
        result = await client.read(sample, preset="envelope")
    except CannotConnect as err:
        raise _CheckFailed("cannot_connect") from err
    except Unauthorized as err:
        raise _CheckFailed("invalid_auth") from err
    except ServerError as err:
        raise _CheckFailed("model_failed") from err
    except LocalOcrError as err:
        _LOGGER.warning("Local OCR test failed: %s", type(err).__name__)
        raise _CheckFailed("unknown") from err
    except _CheckFailed:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Unexpected error testing Local OCR: %s", type(err).__name__)
        raise _CheckFailed("unknown") from err

    fields = result.get("fields") or {}
    lines = (fields.get("addressee") or {}).get("lines") or []
    markers = fields.get("markers") or []
    return {
        "lines": str(len(result.get("blocks") or [])),
        "seconds": f"{(result.get('ms') or 0) / 1000:.1f}",
        "preview": " / ".join(lines[:3]) or "none",
        "markers": ", ".join(markers) or "none",
    }


class LocalOcrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Local OCR."""

    VERSION = 1

    def __init__(self) -> None:
        self._url: str = DEFAULT_URL
        self._token: str | None = None
        self._managed = False
        self._slug: str | None = None
        self._needs_token = False
        self._placeholders: dict[str, str] | None = None

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> LocalOcrOptionsFlow:
        return LocalOcrOptionsFlow()

    # --- discovery -------------------------------------------------------

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        cfg = discovery_info.config
        self._url = _host_url(cfg["host"], cfg.get("port"))
        self._token = _clean_token(cfg.get("token"))
        self._managed = True
        self._slug = discovery_info.slug
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(
            updates={"url": self._url, "token": self._token, "slug": self._slug}
        )
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="hassio_confirm")
        return await self.async_step_test()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        self._url = _host_url(str(discovery_info.ip_address), discovery_info.port)
        self._managed = False
        self._needs_token = discovery_info.properties.get("auth") == "1"
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={"url": self._url})
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if self._needs_token:
                self._token = _clean_token(user_input.get("token"))
            return await self.async_step_test()
        schema = vol.Schema({vol.Required("token"): str}) if self._needs_token else None
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema or vol.Schema({}),  # type: ignore[arg-type]  # HA shims voluptuous to probatio
            description_placeholders={"url": self._url},
        )

    # --- manual ----------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._url = user_input["url"].strip().rstrip("/")
            self._token = _clean_token(user_input.get("token"))
            self._managed = False
            return await self.async_step_test()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required("url", default=DEFAULT_URL): str,
                    vol.Optional("token"): str,
                }
            ),
        )

    # --- test ------------------------------------------------------------

    async def async_step_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._placeholders is not None:
            return self.async_create_entry(
                title="Local OCR",
                data={
                    "url": self._url,
                    "token": self._token,
                    "managed": self._managed,
                    **({"slug": self._slug} if self._slug else {}),
                },
                options=dict(DEFAULT_OPTIONS),
            )
        self._placeholders = None
        try:
            self._placeholders = await _async_check(self.hass, self._url, self._token)
        except _CheckFailed as err:
            errors["base"] = err.code
        return self.async_show_form(
            step_id="test",
            errors=errors,
            description_placeholders=self._placeholders or {
                "lines": "0", "seconds": "0.0", "preview": "none", "markers": "none"
            },
        )

    # --- reauth / reconfigure -------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = _clean_token(user_input.get("token"))
            try:
                await _async_check(self.hass, entry.data["url"], token, read_sample=False)
            except _CheckFailed as err:
                errors["base"] = err.code
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={"token": token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("token"): str}),  # type: ignore[arg-type]  # HA shims voluptuous to probatio
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input["url"].strip().rstrip("/")
            try:
                await _async_check(self.hass, url, entry.data.get("token"))
            except _CheckFailed as err:
                errors["base"] = err.code
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={"url": url}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {vol.Required("url", default=entry.data["url"]): str}
            ),
            errors=errors,
        )


class LocalOcrOptionsFlow(OptionsFlow):
    """Options: default preset, confidence, timeout."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = {**DEFAULT_OPTIONS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required("preset", default=opts["preset"]): SelectSelector(
                        SelectSelectorConfig(options=PRESETS, translation_key="preset")
                    ),
                    vol.Required(
                        "min_confidence", default=opts["min_confidence"]
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=1, step=0.05)
                    ),
                    vol.Required("timeout", default=opts["timeout"]): NumberSelector(
                        NumberSelectorConfig(min=5, max=300, step=1, unit_of_measurement="s")
                    ),
                }
            ),
        )

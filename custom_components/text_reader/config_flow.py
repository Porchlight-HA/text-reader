"""Config flow for Text Reader: one question, then install, pick and test."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    EntityWithDeviceFilterSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import app_installer
from .ai_backend import AiUnavailable, ai_read, eligible_entities
from .api import (
    CannotConnect,
    LocalOcrClient,
    LocalOcrError,
    ServerError,
    Timeout,
    Unauthorized,
)
from .app_installer import InstallFailed, local_supported
from .const import DEFAULT_PORT, DOMAIN, MIN_API, SAMPLE_PATH
from .data import settings

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL = f"http://localhost:{DEFAULT_PORT}"
DEFAULT_THRESHOLD = 0.8
DEFAULT_OPTIONS: dict[str, Any] = {"preset": "text", "min_confidence": 0.5, "timeout": 30}
PRESETS = ["text", "envelope", "meter", "label"]
BACKENDS = ["local", "ai_task", "auto"]
LOCAL_BACKENDS = ("local", "auto")
AI_BACKENDS = ("ai_task", "auto")
NOT_USED = "not used"
# InstallFailed reasons that get their own error on the manual page. A failure to add the
# repository or install needs none: the page itself says what to do.
_INSTALL_ERRORS = {
    "timeout": "app_not_found",
    "not_started": "app_failed_to_start",
    "not_installed": "app_not_installed_yet",
}

MY_ADD_REPOSITORY = (
    "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/"
    f"?repository_url={app_installer.APP_REPO_URL}"
)
DOCKER_RUN = (
    "docker run -d --name local-ocr --restart unless-stopped -p 8738:8738 \\\n"
    "  -e LOCAL_OCR_TOKEN=<choose-a-long-random-string> local-ocr"
)
# The selector filter needs the feature by name; the flow also passes the checked list of entities.
_ATTACHMENTS_FEATURE = "ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS"


def _clean_token(value: Any) -> str | None:
    """Strip whitespace; empty means no token."""
    return (value or "").strip() or None


def _host_url(host: str, port: int | None) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port or DEFAULT_PORT}"


def _summary(lines: int, ms: Any, addressee: list[Any]) -> str:
    seconds = f"{(ms or 0) / 1000:.1f}"
    preview = " / ".join(str(line) for line in addressee[:3]) or "none"
    return f"Read {lines} lines in {seconds} s. Addressee: {preview}"


def _addressee_lines(result: dict[str, Any]) -> list[Any]:
    addressee = (result.get("fields") or {}).get("addressee") or {}
    lines = addressee.get("lines") if isinstance(addressee, dict) else None
    return lines if isinstance(lines, list) else []


class _CheckFailed(Exception):
    """A test-step check failed; carries the error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _async_check(
    hass: HomeAssistant, url: str, token: str | None, *, read_sample: bool = True
) -> str:
    """Run the Local OCR connection checks; return the sample summary ("" without the sample).

    Raises _CheckFailed(code). Never logs tokens, image bytes or recognised text.
    """
    client = LocalOcrClient(async_get_clientsession(hass), url, token)
    try:
        info = await client.info()
        if info.api < MIN_API:
            raise _CheckFailed("app_outdated")
        if not read_sample:
            return ""
        health = await client.health()
        if health.model_state == "error":
            raise _CheckFailed("model_failed")
        sample = await hass.async_add_executor_job(SAMPLE_PATH.read_bytes)
        result = await client.read(sample, preset="envelope")
    except CannotConnect as err:
        raise _CheckFailed("cannot_connect") from err
    except Timeout as err:
        raise _CheckFailed("timeout") from err
    except Unauthorized as err:
        raise _CheckFailed("invalid_auth") from err
    except ServerError as err:
        if err.code == "model_error" or str(err).lower().startswith("out of memory"):
            raise _CheckFailed("model_failed") from err
        if err.code == "internal_error":
            raise _CheckFailed("unknown") from err
        # A 404, an HTML page or JSON without our fields: something else answers at this address.
        raise _CheckFailed("not_local_ocr") from err
    except LocalOcrError as err:
        _LOGGER.warning("Local OCR test failed: %s", type(err).__name__)
        raise _CheckFailed("unknown") from err
    except _CheckFailed:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Unexpected error testing Local OCR: %s", type(err).__name__)
        raise _CheckFailed("unknown") from err

    blocks = [b for b in result.get("blocks") or [] if isinstance(b, dict)]
    # Count visual lines (blocks sharing the server's `line` index), not blocks.
    line_ids = {b["line"] for b in blocks if isinstance(b.get("line"), int)}
    lines = len(line_ids) if line_ids else len(blocks)
    return _summary(lines, result.get("ms"), _addressee_lines(result))


async def _async_check_ai(hass: HomeAssistant, entity_id: str) -> str:
    """Read the sample with the AI entity; return the summary. Raises _CheckFailed(code)."""
    sample = await hass.async_add_executor_job(SAMPLE_PATH.read_bytes)
    try:
        result = await ai_read(hass, entity_id, sample, preset="envelope")
    except AiUnavailable as err:
        _LOGGER.warning("AI assistant test failed: %s", err)
        raise _CheckFailed("ai_unavailable") from None
    except Exception as err:  # noqa: BLE001  # never log the message: it could hold recognised text
        _LOGGER.warning("AI assistant test failed: %s", type(err).__name__)
        raise _CheckFailed("ai_failed") from None
    text = result.get("text") or ""
    lines = sum(1 for line in str(text).splitlines() if line.strip())
    return _summary(lines, result.get("ms"), _addressee_lines(result))


def _ai_entity_selector(include: list[str] | None = None) -> EntitySelector:
    config = EntitySelectorConfig(
        filter=EntityWithDeviceFilterSelectorConfig(
            domain="ai_task", supported_features=[_ATTACHMENTS_FEATURE]
        )
    )
    if include is not None:
        config["include_entities"] = include
    try:
        return EntitySelector(config)
    except Exception:  # noqa: BLE001
        # Resolving the feature name imports ai_task, which can fail where its requirements are
        # missing: fall back to the domain filter (the setup step also passes include_entities).
        config["filter"] = EntityWithDeviceFilterSelectorConfig(domain="ai_task")
        return EntitySelector(config)


class TextReaderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Text Reader."""

    VERSION = 1

    def __init__(self) -> None:
        self._backend = "local"
        self._ai_entity: str | None = None
        self._url: str = DEFAULT_URL
        self._token: str | None = None
        self._managed = False
        self._slug: str | None = None
        self._needs_token = False
        self._placeholders: dict[str, str] | None = None
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._manual_error: str | None = None

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> TextReaderOptionsFlow:
        return TextReaderOptionsFlow()

    # --- discovery -------------------------------------------------------

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        cfg = discovery_info.config
        self._url = _host_url(cfg["host"], cfg.get("port"))
        self._token = _clean_token(cfg.get("token"))
        self._managed = True
        self._slug = discovery_info.slug
        await self.async_set_unique_id(DOMAIN)
        # "managed" too: discovery onto an AI-only entry turns it into an App entry, whose token comes
        # from discovery (reauth_managed) and which zeroconf must not repoint.
        updates = {"url": self._url, "token": self._token, "slug": self._slug, "managed": True}
        self._abort_stale_reauth(updates)
        self._abort_if_unique_id_configured(updates=updates)
        return await self.async_step_hassio_confirm()

    def _abort_stale_reauth(self, updates: dict[str, Any]) -> None:
        """Discovery is about to update the entry (new token or URL): any open reauth flow is now moot."""
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
        if entry is None or all(entry.data.get(k) == v for k, v in updates.items()):
            return
        for flow in self.hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

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
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, DOMAIN)
        if entry is not None and entry.data.get("managed"):
            # The App's entry follows Supervisor discovery; a standalone server seen on the LAN
            # (or the App's own port, if published) must not repoint it.
            return self.async_abort(reason="already_configured")
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

    # --- the one question --------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        # Only gate where Supervisor runs the app; Container/Core users may run it on another machine.
        supported, reason = (
            await local_supported(self.hass) if is_hassio(self.hass) else (True, "")
        )
        if not supported:
            _LOGGER.debug("Local OCR app not offered here: %s", reason)
            return self.async_show_menu(step_id="user_ai_only", menu_options=["ai"])
        return self.async_show_menu(step_id="user", menu_options=["local", "ai", "both"])

    async def async_step_user_ai_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The menu shown where the app can't run (its own step, so its text can say why)."""
        return await self.async_step_user(user_input)

    async def _async_begin(self, backend: str) -> None:
        self._backend = backend
        # Claim the unique id now, so the App's own discovery (which fires when the App we install
        # starts) doesn't open a second flow next to this one.
        await self.async_set_unique_id(DOMAIN, raise_on_progress=False)

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self._async_begin("local")
        return await self._async_step_local_app()

    async def async_step_ai(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self._async_begin("ai_task")
        return await self.async_step_ai_entity()

    async def async_step_both(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self._async_begin("auto")
        # The AI pick comes first: it's quick, and it can abort before anything is installed.
        return await self.async_step_ai_entity()

    # --- AI ----------------------------------------------------------------

    async def async_step_ai_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        eligible = eligible_entities(self.hass)
        if not eligible:
            return self.async_abort(reason="no_ai_entities")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("privacy_ack"):
                errors["base"] = "privacy_ack_required"
            else:
                self._ai_entity = user_input["ai_entity"]
                if self._backend == "auto":
                    return await self._async_step_local_app()
                return await self.async_step_test()
        return self.async_show_form(
            step_id="ai_entity",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required(
                        "ai_entity",
                        default=(user_input or {}).get("ai_entity", eligible[0]),
                    ): _ai_entity_selector(eligible),
                    vol.Required("privacy_ack", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    # --- the Local OCR app -------------------------------------------------

    async def _async_step_local_app(self) -> ConfigFlowResult:
        if not is_hassio(self.hass):
            return await self.async_step_local_container()
        slug = await app_installer.installed_app(self.hass)
        if slug is not None:
            self._slug = slug
            if await self._app_too_old(slug):
                self._manual_error = "app_too_old"
                return await self.async_step_install_app_manual()
            return await self.async_step_wait_for_discovery()
        return await self.async_step_install_app()

    async def _app_too_old(self, slug: str) -> bool:
        return app_installer.app_too_old(await app_installer.installed_version(self.hass, slug))

    async def _async_install(self) -> dict[str, Any]:
        self._slug = await app_installer.install_and_start(self.hass)
        return await app_installer.wait_for_discovery(self.hass, slug=self._slug)

    async def _async_wait(self) -> dict[str, Any]:
        slug = await app_installer.start_installed(self.hass)
        if slug is None:
            # Nothing would ever announce itself: don't spin for ten minutes.
            raise InstallFailed("not_installed")
        return await app_installer.wait_for_discovery(self.hass, slug=slug)

    async def _async_progress(
        self, step_id: str, work: Coroutine[Any, Any, dict[str, Any]]
    ) -> ConfigFlowResult:
        """Run `work` behind a progress step; on success continue to the sample test."""
        if self._task is None:
            self._task = self.hass.async_create_task(work, eager_start=False)
        else:
            work.close()
        if not self._task.done():
            return self.async_show_progress(
                step_id=step_id, progress_action=step_id, progress_task=self._task
            )
        task, self._task = self._task, None
        # Every failure must end the spinner: HA's background configure only suppresses UnknownFlow.
        try:
            cfg = task.result()
        except InstallFailed as err:
            self._manual_error = _INSTALL_ERRORS.get(err.reason)
            return self.async_show_progress_done(next_step_id="install_app_manual")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Setting up the Local OCR app failed: %s", type(err).__name__)
            self._manual_error = "unknown"
            return self.async_show_progress_done(next_step_id="install_app_manual")
        host, port = cfg.get("host"), cfg.get("port")
        if not isinstance(host, str) or not host or not (
            port is None or (isinstance(port, int) and 0 < port < 65536)
        ):
            _LOGGER.warning("The Local OCR app announced itself without a usable address")
            self._manual_error = "app_not_found"
            return self.async_show_progress_done(next_step_id="install_app_manual")
        self._url = _host_url(host, port)
        self._token = _clean_token(cfg.get("token"))
        self._managed = True
        self._slug = cfg.get("slug") or self._slug
        return self.async_show_progress_done(next_step_id="test")

    async def async_step_install_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_progress("install_app", self._async_install())

    async def async_step_wait_for_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_progress("wait_for_discovery", self._async_wait())

    async def async_step_install_app_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._manual_error = None
            # Check before the spinner: with no app, or one too old to announce itself as
            # text_reader, the wait could only time out after ten minutes.
            slug = await app_installer.installed_app(self.hass)
            if slug is None:
                self._manual_error = "app_not_installed_yet"
            elif await self._app_too_old(slug):
                self._manual_error = "app_too_old"
            else:
                self._slug = slug
                return await self.async_step_wait_for_discovery()
        return self.async_show_form(
            step_id="install_app_manual",
            description_placeholders={"repo_link": MY_ADD_REPOSITORY},
            errors={"base": self._manual_error} if self._manual_error else {},
        )

    async def async_step_local_container(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._url = user_input["url"].strip().rstrip("/")
            self._token = _clean_token(user_input.get("token"))
            self._managed = False
            return await self.async_step_test()
        return self.async_show_form(
            step_id="local_container",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required("url", default=self._url): str,
                    vol.Optional("token"): str,
                }
            ),
            description_placeholders={
                "docker_run": DOCKER_RUN,
                "docs_url": app_installer.APP_REPO_URL,
            },
        )

    # --- test ------------------------------------------------------------

    async def _async_run_tests(self) -> dict[str, str]:
        local_result = ai_result = NOT_USED
        if self._backend in LOCAL_BACKENDS:
            local_result = await _async_check(self.hass, self._url, self._token)
        if self._backend in AI_BACKENDS and self._ai_entity:
            ai_result = await _async_check_ai(self.hass, self._ai_entity)
        return {"local_result": local_result, "ai_result": ai_result}

    def _entry_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"backend": self._backend}
        if self._backend in LOCAL_BACKENDS:
            data.update(url=self._url, token=self._token, managed=self._managed)
            if self._slug:
                data["slug"] = self._slug
        if self._backend in AI_BACKENDS:
            data["ai_entity"] = self._ai_entity
        return data

    async def async_step_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None and self._placeholders is not None:
            return self.async_create_entry(
                title="Text Reader",
                data=self._entry_data(),
                options={
                    "backend": self._backend,
                    # Only when this backend uses it: after "change", a local-only choice drops it.
                    "ai_entity": self._ai_entity if self._backend in AI_BACKENDS else None,
                    "auto_threshold": DEFAULT_THRESHOLD,
                    **DEFAULT_OPTIONS,
                },
            )
        self._placeholders = None
        try:
            self._placeholders = await self._async_run_tests()
        except _CheckFailed as err:
            # A neutral form that claims no result. Submit retries the test, or goes back to the
            # choice (so a failing assistant doesn't trap a working local setup, or the reverse).
            return self.async_show_form(
                step_id="test_failed",
                data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                    {
                        vol.Required("next", default="retry"): SelectSelector(
                            SelectSelectorConfig(
                                options=["retry", "change"], translation_key="test_failed_next"
                            )
                        )
                    }
                ),
                errors={"base": err.code},
            )
        return self.async_show_form(
            step_id="test", description_placeholders=self._placeholders
        )

    async def async_step_test_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (user_input or {}).get("next") == "change":
            # Back to the one question. What was found or entered for the local App (address,
            # token, slug) is kept, so choosing it again doesn't start from scratch.
            self._placeholders = None
            return await self.async_step_user()
        return await self.async_step_test()

    # --- reauth / reconfigure -------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        if entry_data.get("managed"):
            return await self.async_step_reauth_managed()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_managed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """App entries: the token lives in the App's /data, so the user can't type it.

        Restarting the App re-sends discovery with its current token, which updates the entry (and aborts
        this flow). Submit re-checks with whatever token the entry has now.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _async_check(
                    self.hass, entry.data["url"], entry.data.get("token"), read_sample=False
                )
            except _CheckFailed as err:
                errors["base"] = err.code
            else:
                return self.async_update_reload_and_abort(entry, data_updates={})
        return self.async_show_form(step_id="reauth_managed", errors=errors)

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
        if not entry.data.get("url"):
            return self.async_abort(reason="no_local_app")
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


class TextReaderOptionsFlow(OptionsFlowWithReload):
    """Options: backend, AI assistant, auto threshold, preset, confidence, timeout.

    Saving reloads the entry so changes take effect.
    """

    async def _validate(self, user_input: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Return (options to save, error code or None)."""
        entry = self.config_entry
        current = settings(entry)
        backend = user_input["backend"]
        ai_entity = user_input.get("ai_entity") or None
        options = {k: v for k, v in user_input.items() if k != "privacy_ack"}
        options["ai_entity"] = ai_entity
        if backend in LOCAL_BACKENDS and not entry.data.get("url"):
            # On Supervisor, the App's discovery adds it to this entry once it runs.
            if not is_hassio(self.hass):
                return options, "local_not_set_up"
            if not (await local_supported(self.hass))[0]:
                return options, "local_unsupported_options"
            return options, "local_not_set_up_app"
        if backend in AI_BACKENDS and ai_entity is None:
            return options, "ai_entity_required"
        # Whatever the backend: a per-call backend or a repair can send images to any stored
        # entity, so every entity is stored only with an ack. The stored one was acked when it was
        # set, and is not re-checked, so a briefly unavailable assistant doesn't block other options.
        if ai_entity is not None and ai_entity != current.ai_entity:
            if ai_entity not in eligible_entities(self.hass):
                return options, "ai_entity_invalid"
            if not user_input.get("privacy_ack"):
                return options, "privacy_ack_required"
        return options, None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options, error = await self._validate(user_input)
            if error is None:
                return self.async_create_entry(data=options)
            errors["base"] = error
        current = settings(self.config_entry)
        opts = {
            **DEFAULT_OPTIONS,
            "auto_threshold": DEFAULT_THRESHOLD,
            **self.config_entry.options,
            "backend": current.backend,
            **(user_input or {}),
        }
        ai_entity = (user_input or {}).get("ai_entity", current.ai_entity)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(  # type: ignore[arg-type]  # HA shims voluptuous to probatio
                {
                    vol.Required("backend", default=opts["backend"]): SelectSelector(
                        SelectSelectorConfig(options=BACKENDS, translation_key="backend")
                    ),
                    vol.Optional(
                        "ai_entity", description={"suggested_value": ai_entity}
                    ): _ai_entity_selector(),
                    vol.Optional("privacy_ack", default=False): BooleanSelector(),
                    vol.Required(
                        "auto_threshold", default=opts["auto_threshold"]
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0.5, max=0.99, step=0.01, mode=NumberSelectorMode.BOX
                        )
                    ),
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
            errors=errors,
        )

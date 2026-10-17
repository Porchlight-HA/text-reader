"""Tests for the Text Reader config flow."""

from ipaddress import ip_address
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.text_reader.app_installer import InstallFailed
from custom_components.text_reader.const import DOMAIN

CF = "custom_components.text_reader.config_flow"

URL = "http://localhost:8738"
TOKEN = "s3cret-token"

INFO = {"version": "0.1.0", "api": 1, "arch": "amd64", "engine": "rapidocr",
        "presets": ["text", "envelope"], "limits": {}}
HEALTH = {"status": "ok", "model_state": "ready", "detail": None,
          "memory": {"oom_kills": 0, "last_oom": None}}
READ = {
    "text": "x", "ms": 412,
    "blocks": [{"text": "a", "line": 1}, {"text": "b", "line": 1}, {"text": "c", "line": 2},
               {"text": "d", "line": 3}],
    "fields": {
        "addressee": {"lines": ["POSTAL CUSTOMER", "89 PLACEHOLDER LN",
                                "DEMOCITY MN 55999", "extra"], "confidence": 0.9},
        "markers": ["PRSRT STD", "PERMIT NO", "ECRWSS"],
    },
}


def mock_server(mock, url=URL, *, info=INFO, health=HEALTH, read=READ, read_status=200):
    mock.get(f"{url}/v1/info", json=info)
    mock.get(f"{url}/v1/health", json=health)
    mock.post(f"{url}/v1/read", json=read, status=read_status)


LOCAL_RESULT = "Read 3 lines in 0.4 s. Addressee: POSTAL CUSTOMER / 89 PLACEHOLDER LN / DEMOCITY MN 55999"
OPTIONS = {"backend": "local", "ai_entity": None, "auto_threshold": 0.8,
           "preset": "text", "min_confidence": 0.5, "timeout": 30}


@pytest.fixture(autouse=True)
def _host(request):
    """By default: a Core/Container install that can run the app. Tests override as needed."""
    with patch(f"{CF}.local_supported", AsyncMock(return_value=(True, ""))), \
            patch(f"{CF}.is_hassio", return_value=False):
        yield


async def container_flow(hass, user_input):
    """User flow → "On this device" without Supervisor → the container form, submitted."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"})
    assert result["step_id"] == "local_container"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


def hassio_info(host="local-ocr", token=TOKEN):
    return HassioServiceInfo(
        config={"host": host, "port": 8738, "token": token},
        name="Local OCR", slug="local_ocr", uuid="abc")


def zc_info(auth="0"):
    return ZeroconfServiceInfo(
        ip_address=ip_address("192.168.1.5"), ip_addresses=[ip_address("192.168.1.5")],
        port=8738, hostname="ocr.local.", type="_local-ocr._tcp.local.",
        name="Local OCR._local-ocr._tcp.local.",
        properties={"auth": auth, "api": "1"})


async def test_hassio_discovery_still_creates_local_entry(hass, aioclient_mock):
    mock_server(aioclient_mock, "http://local-ocr:8738")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=hassio_info())
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "test"
    ph = result["description_placeholders"]
    # 4 blocks on 3 distinct lines
    assert ph == {"local_result": LOCAL_RESULT, "ai_result": "not used"}
    assert TOKEN not in str(ph)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Text Reader"
    assert result["data"] == {"backend": "local", "url": "http://local-ocr:8738", "token": TOKEN,
                              "managed": True, "slug": "local_ocr"}
    assert result["options"] == OPTIONS
    assert result["result"].unique_id == DOMAIN


async def test_hassio_rediscovery_updates_token_and_url(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": "http://old:8738", "token": "old", "managed": True})
    entry.add_to_hass(hass)
    with patch("custom_components.text_reader.async_setup_entry", return_value=True) as setup:
        await hass.config_entries.async_setup(entry.entry_id)
        assert setup.call_count == 1
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
            data=hassio_info(host="new-host", token="new"))
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://new-host:8738"
    assert entry.data["token"] == "new"
    assert entry.data["slug"] == "local_ocr"
    assert setup.call_count == 2  # entry reloaded


async def test_zeroconf_with_auth_asks_token(hass, aioclient_mock):
    mock_server(aioclient_mock, "http://192.168.1.5:8738")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("1"))
    assert result["step_id"] == "zeroconf_confirm"
    assert "token" in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": f"  {TOKEN} "})
    assert result["step_id"] == "test"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {"backend": "local", "url": "http://192.168.1.5:8738", "token": TOKEN,
                              "managed": False}


async def test_zeroconf_without_auth_confirms(hass, aioclient_mock):
    mock_server(aioclient_mock, "http://192.168.1.5:8738")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
    assert result["step_id"] == "zeroconf_confirm"
    assert "token" not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "test"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["token"] is None


async def test_container_path_manual_url(hass, aioclient_mock):
    mock_server(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"})
    assert result["step_id"] == "local_container"
    assert "docker run" in result["description_placeholders"]["docker_run"]
    defaults = {k.schema: k.default() for k in result["data_schema"].schema if hasattr(k, "default") and callable(k.default)}
    assert defaults["url"] == "http://localhost:8738"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL + "/", "token": "   "})
    assert result["step_id"] == "test"
    assert result["description_placeholders"] == {"local_result": LOCAL_RESULT, "ai_result": "not used"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"backend": "local", "url": URL, "token": None, "managed": False}
    assert result["options"] == OPTIONS
    assert result["result"].unique_id == DOMAIN


async def test_test_step_no_addressee_no_markers(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"ms": 1500, "blocks": [], "fields": {"markers": []}})
    result = await container_flow(hass, {"url": URL})
    assert result["description_placeholders"] == {
        "local_result": "Read 0 lines in 1.5 s. Addressee: none", "ai_result": "not used"}


async def test_test_step_cannot_connect_then_retry_succeeds(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", exc=aiohttp.ClientConnectionError())
    result = await container_flow(hass, {"url": URL})
    # A failed test shows a neutral form that claims no result, plus the error.
    assert result["step_id"] == "test_failed"
    assert result["errors"] == {"base": "cannot_connect"}
    assert not result.get("description_placeholders")
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert not result.get("errors")
    assert result["step_id"] == "test"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_test_step_invalid_auth(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", status=401, json={"error": "unauthorized"})
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_test_step_outdated_api(hass, aioclient_mock):
    mock_server(aioclient_mock, info={**INFO, "api": 0})
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "app_outdated"}


async def test_test_step_model_failed(hass, aioclient_mock):
    mock_server(aioclient_mock, health={"status": "degraded", "model_state": "error"})
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_model_failed_on_read(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "model_error", "message": "boom"}, read_status=500)
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_out_of_memory_is_model_failed(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "internal_error", "message": "out of memory: x"}, read_status=500)
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_wrong_server_is_not_local_ocr(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", status=404, text="<html>Not Found</html>")
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "not_local_ocr"}


async def test_test_step_html_200_is_not_local_ocr(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", status=200, text="<html>hello</html>")
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "not_local_ocr"}


async def test_test_step_timeout(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", exc=TimeoutError())
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "timeout"}


def test_strings_have_test_failed_and_new_errors():
    import json
    from pathlib import Path

    import custom_components.text_reader as pkg
    root = Path(pkg.__file__).parent
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations" / "en.json").read_text())
    cfg = strings["config"]
    assert "{" not in cfg["step"]["test_failed"]["description"]
    assert {"not_local_ocr", "timeout"} <= set(cfg["error"])
    assert "reauth_managed" in cfg["step"] and "data" not in cfg["step"]["reauth_managed"]


async def test_test_step_unknown(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "bad_request"}, read_status=400)
    result = await container_flow(hass, {"url": URL})
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure_changes_url(hass, aioclient_mock):
    new = "http://other:8738"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": TOKEN, "managed": False})
    entry.add_to_hass(hass)
    mock_server(aioclient_mock, new)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": new})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {"url": new, "token": TOKEN, "managed": False}


async def test_reconfigure_error_shows_form(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": None, "managed": False})
    entry.add_to_hass(hass)
    aioclient_mock.get("http://dead:8738/v1/info", exc=aiohttp.ClientConnectionError())
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://dead:8738"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_updates_token(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": "old", "managed": False})
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json={})
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": " new "})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "new"


async def test_reauth_managed_has_no_token_field(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": "old", "managed": True, "slug": "local_ocr"})
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", status=401, json={})
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_managed"
    assert not result["data_schema"] or not result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "invalid_auth"}  # app not restarted yet: same token
    # The restarted app re-sends discovery, which updates the token; Submit re-checks with it.
    hass.config_entries.async_update_entry(entry, data={**entry.data, "token": "new"})
    aioclient_mock.clear_requests()
    mock_server(aioclient_mock)
    with patch("custom_components.text_reader.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer new"


async def test_hassio_discovery_aborts_open_reauth(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": "http://local-ocr:8738", "token": "old", "managed": True,
                                  "slug": "local_ocr"})
    entry.add_to_hass(hass)
    aioclient_mock.get("http://local-ocr:8738/v1/info", status=401, json={})
    await entry.start_reauth_flow(hass)

    def reauth_flows():
        return [f for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                if f["context"]["source"] == "reauth"]

    assert len(reauth_flows()) == 1
    with patch("custom_components.text_reader.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
            data=hassio_info(token="fresh"))
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "fresh"
    assert reauth_flows() == []


async def test_hassio_discovery_same_token_keeps_reauth(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": "http://local-ocr:8738", "token": TOKEN, "managed": True,
                                  "slug": "local_ocr"})
    entry.add_to_hass(hass)
    aioclient_mock.get("http://local-ocr:8738/v1/info", status=401, json={})
    await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=hassio_info())
    assert result["reason"] == "already_configured"
    assert any(f["context"]["source"] == "reauth"
               for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN))


async def test_reauth_cannot_connect(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": "old", "managed": False})
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", exc=aiohttp.ClientConnectionError())
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "x"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_saves(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": None, "managed": False},
                            options={"preset": "text", "min_confidence": 0.5, "timeout": 30})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"preset": "envelope", "min_confidence": 0.7, "timeout": 45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"backend": "local", "ai_entity": None, "auto_threshold": 0.8,
                             "preset": "envelope", "min_confidence": 0.7, "timeout": 45}


async def test_single_instance_abort(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"url": URL}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_setup_entry(hass, aioclient_mock):
    mock_server(aioclient_mock, URL)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": TOKEN, "managed": False},
                            options={"timeout": 12})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.runtime_data.client is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_change_reloads_entry(hass, aioclient_mock):
    mock_server(aioclient_mock, URL)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": URL, "token": TOKEN, "managed": False},
                            options={"preset": "text", "min_confidence": 0.5, "timeout": 30})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    old_client = entry.runtime_data.client
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"preset": "text", "min_confidence": 0.5, "timeout": 75})
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.runtime_data.client is not old_client
    assert entry.runtime_data.client._timeout.total == 75


async def test_zeroconf_never_repoints_a_managed_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": "http://local-ocr:8738", "token": TOKEN, "managed": True,
                                  "slug": "local_ocr"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["url"] == "http://local-ocr:8738"


async def test_zeroconf_updates_a_standalone_entry_url(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"url": "http://192.168.1.4:8738", "token": None, "managed": False})
    entry.add_to_hass(hass)
    with patch("custom_components.text_reader.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://192.168.1.5:8738"


def test_every_form_field_has_a_description():
    import json
    from pathlib import Path

    strings = json.loads((Path(__file__).parents[1] / "custom_components/text_reader/strings.json").read_text())
    steps = [*strings["config"]["step"].values(), *strings["options"]["step"].values()]
    for step in steps:
        assert set(step.get("data", {})) == set(step.get("data_description", {})), step.get("title")


# --- 0.3: one question, install-for-me, AI picker, steering ------------------------------

AI = "ai_task.vision"
APP_SLUG = "abc123_local_ocr"
APP_URL = "http://abc123-local-ocr:8738"
AI_READ = {
    "text": "POSTAL CUSTOMER\n89 PLACEHOLDER LN\n\nDEMOCITY MN 55999", "ms": 2100, "blocks": [],
    "fields": {"addressee": {"lines": ["POSTAL CUSTOMER", "89 PLACEHOLDER LN"]}},
    "engine": f"ai_task:{AI}", "model": None, "preset": "envelope", "confidence": None,
}
AI_RESULT = "Read 3 lines in 2.1 s. Addressee: POSTAL CUSTOMER / 89 PLACEHOLDER LN"
MENU = ["local", "ai", "both"]


def ai_entities(hass, *entity_ids):
    """Register AI Task entities that support attachments (as the real ai_task component would)."""
    from homeassistant.components.ai_task import AITaskEntityFeature
    from homeassistant.components.ai_task.const import DATA_COMPONENT

    full = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    entity = SimpleNamespace(supported_features=full)
    component = MagicMock()
    component.get_entity = lambda eid: entity if eid in entity_ids else None
    hass.data[DATA_COMPONENT] = component
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, "unknown")


OUR_REPO = SimpleNamespace(slug="abc123", source="https://github.com/Porchlight-HA/local-ocr",
                           url="https://github.com/Porchlight-HA/local-ocr")


def app(installed=False):
    return SimpleNamespace(slug=APP_SLUG, installed=installed, repository="abc123")


class FakeSupervisor:
    """get_supervisor_client(hass) and AddonManager, backed by an in-memory store."""

    def __init__(self, *, in_store=False, installed=False, running=False, repo_error=False,
                 announce=True, starts=True):
        from aiohasupervisor import SupervisorError

        self.apps = [app(installed=installed)] if in_store or installed else []
        self.repos = [OUR_REPO] if in_store or installed else []
        self.running = running
        self.client = MagicMock()
        self.client.store.addons_list = AsyncMock(side_effect=lambda: list(self.apps))
        self.client.store.repositories_list = AsyncMock(side_effect=lambda: list(self.repos))

        async def add_repository(options):
            if repo_error:
                raise SupervisorError("repository is private")
            self.repos = [OUR_REPO]
            self.apps = [app()]

        self.client.store.add_repository = AsyncMock(side_effect=add_repository)
        discovered = [SimpleNamespace(service="mqtt", addon="core_mosquitto", config={}),
                      SimpleNamespace(service="text_reader", addon=APP_SLUG,
                                      config={"host": "abc123-local-ocr", "port": 8738, "token": TOKEN})]
        self.client.discovery.list = AsyncMock(return_value=discovered if announce else [])
        self.manager = MagicMock()
        self.manager.async_install_addon = AsyncMock()

        async def start():
            self.running = starts

        self.manager.async_start_addon = AsyncMock(side_effect=start)

        async def info():
            from homeassistant.components.hassio import AddonState
            return SimpleNamespace(state=AddonState.RUNNING if self.running else AddonState.NOT_RUNNING)

        self.manager.async_get_addon_info = AsyncMock(side_effect=info)
        self.manager_cls = MagicMock(return_value=self.manager)

    def __enter__(self):
        self._patches = [
            patch("homeassistant.components.hassio.get_supervisor_client", return_value=self.client),
            patch("homeassistant.components.hassio.AddonManager", self.manager_cls),
            patch(f"{CF}.is_hassio", return_value=True),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


async def pick(hass, choice):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": choice})


async def finish_progress(hass, result, step_id, action):
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == step_id and result["progress_action"] == action
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_menu_shows_three_choices(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert list(result["menu_options"]) == MENU


@pytest.mark.parametrize("reason", ["arch", "memory"])
async def test_menu_ai_only_when_local_unsupported(hass, reason):
    with patch(f"{CF}.local_supported", AsyncMock(return_value=(False, reason))), \
            patch(f"{CF}.is_hassio", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user_ai_only"
    assert list(result["menu_options"]) == ["ai"]


async def test_menu_not_gated_without_supervisor(hass):
    """Container/Core users may run the app on another machine: always offer all three (R9)."""
    with patch(f"{CF}.local_supported", AsyncMock(return_value=(False, "memory"))) as check:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    assert list(result["menu_options"]) == MENU
    check.assert_not_awaited()


async def test_local_installs_app_then_discovers(hass, aioclient_mock):
    from aiohasupervisor.models import StoreAddRepository

    mock_server(aioclient_mock, APP_URL)
    with FakeSupervisor() as sup:
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
    sup.client.store.add_repository.assert_awaited_once_with(
        StoreAddRepository(repository="https://github.com/Porchlight-HA/local-ocr"))
    assert sup.manager_cls.call_args.args[2:] == ("Local OCR", APP_SLUG)
    sup.manager.async_install_addon.assert_awaited_once()
    sup.manager.async_start_addon.assert_awaited_once()
    assert result["step_id"] == "test"
    assert result["description_placeholders"] == {"local_result": LOCAL_RESULT, "ai_result": "not used"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"backend": "local", "url": APP_URL, "token": TOKEN,
                              "managed": True, "slug": APP_SLUG}
    assert result["options"] == OPTIONS


async def test_install_failed_falls_back_to_manual(hass, aioclient_mock):
    mock_server(aioclient_mock, APP_URL)
    with FakeSupervisor(repo_error=True) as sup:
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "install_app_manual"
        assert result["description_placeholders"]["repo_link"] == (
            "https://my.home-assistant.io/redirect/supervisor_add_addon_repository/"
            "?repository_url=https://github.com/Porchlight-HA/local-ocr")
        assert not result["errors"]
        sup.manager.async_install_addon.assert_not_awaited()
        # The user adds the repository and installs the app by hand, then selects Submit.
        sup.repos, sup.apps = [OUR_REPO], [app(installed=True)]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await finish_progress(hass, result, "wait_for_discovery", "wait_for_discovery")
    sup.manager.async_start_addon.assert_awaited_once()  # installed but not started: start it
    assert result["step_id"] == "test"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["slug"] == APP_SLUG and result["data"]["managed"] is True


async def test_manual_wait_times_out_shows_error(hass):
    with FakeSupervisor(repo_error=True), \
            patch(f"{CF}.app_installer.wait_for_discovery",
                  AsyncMock(side_effect=InstallFailed("timeout"))):
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await finish_progress(hass, result, "wait_for_discovery", "wait_for_discovery")
    assert result["step_id"] == "install_app_manual"
    assert result["errors"] == {"base": "app_not_found"}


async def test_install_discovery_timeout_goes_to_manual(hass):
    with FakeSupervisor(), patch(f"{CF}.app_installer.wait_for_discovery",
                                 AsyncMock(side_effect=InstallFailed("timeout"))):
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
    assert result["step_id"] == "install_app_manual"
    assert result["errors"] == {"base": "app_not_found"}


async def test_install_unexpected_error_ends_spinner(hass):
    with FakeSupervisor(), patch(f"{CF}.app_installer.install_and_start",
                                 AsyncMock(side_effect=RuntimeError("boom"))):
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
    assert result["step_id"] == "install_app_manual"
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.parametrize("cfg", [
    {"port": 8738, "token": TOKEN, "slug": APP_SLUG},
    {"host": "", "port": 8738},
    {"host": "abc123-local-ocr", "port": "nope"},
])
async def test_discovery_without_usable_address_ends_spinner(hass, cfg):
    with FakeSupervisor(installed=True, running=True), \
            patch(f"{CF}.app_installer.wait_for_discovery", AsyncMock(return_value=cfg)):
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "wait_for_discovery", "wait_for_discovery")
    assert result["step_id"] == "install_app_manual"
    assert result["errors"] == {"base": "app_not_found"}


async def test_app_that_does_not_start_fails_fast(hass):
    with FakeSupervisor(announce=False, starts=False) as sup:
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "install_app", "install_app")
    sup.manager.async_start_addon.assert_awaited_once()
    assert result["step_id"] == "install_app_manual"
    assert result["errors"] == {"base": "app_failed_to_start"}


async def test_installed_app_that_does_not_start_fails_fast(hass):
    with FakeSupervisor(installed=True, announce=False, starts=False):
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "wait_for_discovery", "wait_for_discovery")
    assert result["errors"] == {"base": "app_failed_to_start"}


async def test_hassio_discovery_onto_ai_only_entry_marks_managed(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"backend": "ai_task", "ai_entity": AI})
    entry.add_to_hass(hass)
    with patch("custom_components.text_reader.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=hassio_info())
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["managed"] is True
    assert entry.data["url"] == "http://local-ocr:8738" and entry.data["slug"] == "local_ocr"
    # ...so zeroconf can no longer repoint it.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://local-ocr:8738"


async def test_app_already_installed_skips_install(hass, aioclient_mock):
    mock_server(aioclient_mock, APP_URL)
    with FakeSupervisor(installed=True, running=True) as sup:
        result = await pick(hass, "local")
        result = await finish_progress(hass, result, "wait_for_discovery", "wait_for_discovery")
    sup.client.store.add_repository.assert_not_awaited()
    sup.manager.async_install_addon.assert_not_awaited()
    sup.manager.async_start_addon.assert_not_awaited()
    assert result["step_id"] == "test"


async def test_ai_step_requires_privacy_ack(hass):
    ai_entities(hass, AI)
    result = await pick(hass, "ai")
    assert result["step_id"] == "ai_entity"
    assert {k.schema for k in result["data_schema"].schema} == {"ai_entity", "privacy_ack"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"ai_entity": AI, "privacy_ack": False})
    assert result["step_id"] == "ai_entity"
    assert result["errors"] == {"base": "privacy_ack_required"}
    with patch(f"{CF}.ai_read", AsyncMock(return_value=AI_READ)) as read:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"ai_entity": AI, "privacy_ack": True})
    assert read.await_args.args[1] == AI and read.await_args.kwargs == {"preset": "envelope"}
    assert result["step_id"] == "test"
    assert result["description_placeholders"] == {"local_result": "not used", "ai_result": AI_RESULT}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"backend": "ai_task", "ai_entity": AI}
    assert result["options"] == {**OPTIONS, "backend": "ai_task", "ai_entity": AI}


async def test_ai_step_only_offers_eligible_entities(hass):
    ai_entities(hass, AI)
    hass.states.async_set("ai_task.text_only", "unknown")  # no attachments support
    result = await pick(hass, "ai")
    selector = next(v for k, v in result["data_schema"].schema.items() if k.schema == "ai_entity")
    assert selector.config["include_entities"] == [AI]


@pytest.mark.parametrize("choice", ["ai", "both"])
async def test_ai_step_no_entities_aborts(hass, choice):
    with FakeSupervisor() as sup:
        result = await pick(hass, choice)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ai_entities"
    sup.client.store.add_repository.assert_not_awaited()  # Both checks before installing anything


async def test_both_creates_auto_entry_and_tests_both(hass, aioclient_mock):
    ai_entities(hass, AI)
    mock_server(aioclient_mock)
    result = await pick(hass, "both")
    assert result["step_id"] == "ai_entity"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ai_entity": AI})
    assert result["errors"] == {"base": "privacy_ack_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"ai_entity": AI, "privacy_ack": True})
    assert result["step_id"] == "local_container"
    with patch(f"{CF}.ai_read", AsyncMock(return_value=AI_READ)) as read:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    read.assert_awaited_once()
    assert result["step_id"] == "test"
    assert result["description_placeholders"] == {"local_result": LOCAL_RESULT, "ai_result": AI_RESULT}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {"backend": "auto", "url": URL, "token": None, "managed": False,
                              "ai_entity": AI}
    assert result["options"] == {**OPTIONS, "backend": "auto", "ai_entity": AI}


@pytest.mark.parametrize(("error", "code"), [
    ("unavailable", "ai_unavailable"),
    ("boom", "ai_failed"),
])
async def test_ai_test_failure_shows_neutral_form(hass, error, code, caplog):
    from custom_components.text_reader.ai_backend import AiUnavailable

    ai_entities(hass, AI)
    exc = AiUnavailable("entity_unavailable") if error == "unavailable" else RuntimeError("secret text")
    result = await pick(hass, "ai")
    with patch(f"{CF}.ai_read", AsyncMock(side_effect=exc)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"ai_entity": AI, "privacy_ack": True})
    assert result["step_id"] == "test_failed"
    assert result["errors"] == {"base": code}
    assert not result.get("description_placeholders")
    assert "secret text" not in caplog.text
    with patch(f"{CF}.ai_read", AsyncMock(return_value=AI_READ)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "test"


async def test_hassio_discovery_during_setup_is_absorbed(hass):
    with FakeSupervisor(), patch(f"{CF}.app_installer.wait_for_discovery",
                                 AsyncMock(side_effect=InstallFailed("timeout"))):
        result = await pick(hass, "local")
        await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=hassio_info())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"


async def test_reconfigure_ai_only_entry_aborts(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"backend": "ai_task", "ai_entity": AI})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_local_app"


def _local_entry(hass, **options):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"backend": "local", "url": URL, "token": None, "managed": False},
                            options={**OPTIONS, **options})
    entry.add_to_hass(hass)
    return entry


async def test_options_change_backend_requires_ack_for_ai(hass):
    ai_entities(hass, AI)
    entry = _local_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {k.schema for k in result["data_schema"].schema}
    assert {"backend", "ai_entity", "privacy_ack", "auto_threshold", "preset", "min_confidence",
            "timeout"} == keys
    base = {"preset": "text", "min_confidence": 0.5, "timeout": 30, "auto_threshold": 0.85}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "backend": "auto", "ai_entity": AI})
    assert result["errors"] == {"base": "privacy_ack_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "backend": "auto", "ai_entity": AI, "privacy_ack": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**base, "backend": "auto", "ai_entity": AI}
    # Same entity, still used: no new ack needed for other changes.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "backend": "ai_task", "ai_entity": AI, "timeout": 60})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["backend"] == "ai_task"


async def test_options_new_entity_needs_ack(hass):
    ai_entities(hass, AI, "ai_task.cloud")
    entry = _local_entry(hass, backend="auto", ai_entity=AI)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"backend": "auto", "ai_entity": "ai_task.cloud"})
    assert result["errors"] == {"base": "privacy_ack_required"}


async def test_options_rejects_ineligible_entity(hass):
    ai_entities(hass, AI)
    hass.states.async_set("ai_task.text_only", "unknown")
    entry = _local_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"backend": "auto", "ai_entity": "ai_task.text_only", "privacy_ack": True})
    assert result["errors"] == {"base": "ai_entity_invalid"}
    assert entry.options["backend"] == "local"


async def test_options_ai_backend_needs_entity(hass):
    entry = _local_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"backend": "ai_task", "privacy_ack": True})
    assert result["errors"] == {"base": "ai_entity_required"}


async def test_options_local_backend_needs_the_app(hass):
    ai_entities(hass, AI)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Text Reader",
                            data={"backend": "ai_task", "ai_entity": AI},
                            options={**OPTIONS, "backend": "ai_task", "ai_entity": AI})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"backend": "auto", "ai_entity": AI})
    assert result["errors"] == {"base": "local_not_set_up"}


async def test_options_ai_entity_cleared_for_local(hass):
    entry = _local_entry(hass, backend="auto", ai_entity=AI)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"backend": "local"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ai_entity"] is None and entry.options["backend"] == "local"


def test_strings_cover_new_steps():
    import json
    from pathlib import Path

    strings = json.loads((Path(__file__).parents[1] / "custom_components/text_reader/strings.json").read_text())
    cfg = strings["config"]
    assert cfg["step"]["user"]["menu_options"] == {
        "local": "On this device (private) — uses the free Local OCR app",
        "ai": "With an AI assistant you already set up — images are sent to that assistant",
        "both": "Both (recommended) — read on this device, ask the assistant only when it's unsure",
    }
    assert cfg["step"]["user_ai_only"]["description"] == (
        "This device can't run the Local OCR app (needs a 64-bit system with 2 GB+ memory). "
        "Text Reader can use an AI assistant instead.")
    assert cfg["step"]["ai_entity"]["data"]["privacy_ack"] == (
        "I understand images are sent to this assistant, and cloud assistants can see them.")
    assert cfg["abort"]["no_ai_entities"] == (
        "No AI assistant that can read images is set up. Add one (for example Ollama with a "
        "vision model), then try again.")
    assert {"{local_result}", "{ai_result}"} <= {w for w in cfg["step"]["test"]["description"].split()}
    assert {"install_app", "wait_for_discovery"} <= set(cfg["progress"])
    for code in ("privacy_ack_required", "ai_unavailable", "ai_failed", "app_not_found"):
        assert code in cfg["error"]
    assert cfg["step"]["install_app_manual"]["description"].startswith(
        "Text Reader couldn't finish setting up the Local OCR app automatically.")
    assert "{repo_link}" in cfg["step"]["install_app_manual"]["description"]
    assert cfg["error"]["app_failed_to_start"] == (
        "The Local OCR app didn't start. Open it in Settings > Apps to see its log, then try again.")
    assert strings["options"]["error"]["ai_entity_invalid"] == (
        "That assistant can't read images. Pick another one.")
    for code in ("privacy_ack_required", "ai_entity_required", "local_not_set_up"):
        assert code in strings["options"]["error"]


async def test_ai_selector_falls_back_without_feature_filter(hass):
    from custom_components.text_reader import config_flow

    with patch(f"{CF}._ATTACHMENTS_FEATURE", "ai_task.NoSuchEnum.SUPPORT_ATTACHMENTS"):
        selector = config_flow._ai_entity_selector()
    assert selector.config["filter"] == [{"domain": ["ai_task"]}]
    selector = config_flow._ai_entity_selector(["ai_task.x"])
    from homeassistant.components.ai_task import AITaskEntityFeature

    assert selector.config["filter"][0]["supported_features"] == [
        AITaskEntityFeature.SUPPORT_ATTACHMENTS]

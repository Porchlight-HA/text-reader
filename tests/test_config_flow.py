"""Tests for the Local OCR config flow."""

from ipaddress import ip_address
from unittest.mock import patch

import aiohttp
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.local_ocr.const import DOMAIN

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


async def test_hassio_discovery_creates_entry_after_test(hass, aioclient_mock):
    mock_server(aioclient_mock, "http://local-ocr:8738")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=hassio_info())
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "test"
    ph = result["description_placeholders"]
    assert ph == {"lines": "3", "seconds": "0.4",  # 4 blocks on 3 distinct lines
                  "preview": "POSTAL CUSTOMER / 89 PLACEHOLDER LN / DEMOCITY MN 55999",
                  "markers": "PRSRT STD, PERMIT NO, ECRWSS"}
    assert TOKEN not in str(ph)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Local OCR"
    assert result["data"] == {"url": "http://local-ocr:8738", "token": TOKEN, "managed": True, "slug": "local_ocr"}
    assert result["options"] == {"preset": "text", "min_confidence": 0.5, "timeout": 30}
    assert result["result"].unique_id == DOMAIN


async def test_hassio_rediscovery_updates_token_and_url(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": "http://old:8738", "token": "old", "managed": True})
    entry.add_to_hass(hass)
    with patch("custom_components.local_ocr.async_setup_entry", return_value=True) as setup:
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
    assert result["data"] == {"url": "http://192.168.1.5:8738", "token": TOKEN, "managed": False}


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


async def test_user_flow_manual_url(hass, aioclient_mock):
    mock_server(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    defaults = {k.schema: k.default() for k in result["data_schema"].schema if hasattr(k, "default") and callable(k.default)}
    assert defaults["url"] == "http://localhost:8738"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": "   "})
    assert result["step_id"] == "test"
    assert result["description_placeholders"]["markers"] == "PRSRT STD, PERMIT NO, ECRWSS"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {"url": URL, "token": None, "managed": False}


async def test_test_step_no_addressee_no_markers(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"ms": 1500, "blocks": [], "fields": {"markers": []}})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["description_placeholders"] == {
        "lines": "0", "seconds": "1.5", "preview": "none", "markers": "none"}


async def test_test_step_cannot_connect_then_retry_succeeds(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_test_step_outdated_api(hass, aioclient_mock):
    mock_server(aioclient_mock, info={**INFO, "api": 0})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "app_outdated"}


async def test_test_step_model_failed(hass, aioclient_mock):
    mock_server(aioclient_mock, health={"status": "degraded", "model_state": "error"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_model_failed_on_read(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "model_error", "message": "boom"}, read_status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_out_of_memory_is_model_failed(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "internal_error", "message": "out of memory: x"}, read_status=500)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "model_failed"}


async def test_test_step_wrong_server_is_not_local_ocr(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", status=404, text="<html>Not Found</html>")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "not_local_ocr"}


async def test_test_step_html_200_is_not_local_ocr(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", status=200, text="<html>hello</html>")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "not_local_ocr"}


async def test_test_step_timeout(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/v1/info", exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "timeout"}


def test_strings_have_test_failed_and_new_errors():
    import json
    from pathlib import Path

    import custom_components.local_ocr as pkg
    root = Path(pkg.__file__).parent
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations" / "en.json").read_text())
    cfg = strings["config"]
    assert "{" not in cfg["step"]["test_failed"]["description"]
    assert {"not_local_ocr", "timeout"} <= set(cfg["error"])
    assert "reauth_managed" in cfg["step"] and "data" not in cfg["step"]["reauth_managed"]


async def test_test_step_unknown(hass, aioclient_mock):
    mock_server(aioclient_mock, read={"error": "bad_request"}, read_status=400)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure_changes_url(hass, aioclient_mock):
    new = "http://other:8738"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": URL, "token": None, "managed": False})
    entry.add_to_hass(hass)
    aioclient_mock.get("http://dead:8738/v1/info", exc=aiohttp.ClientConnectionError())
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://dead:8738"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_updates_token(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    with patch("custom_components.local_ocr.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer new"


async def test_hassio_discovery_aborts_open_reauth(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": "http://local-ocr:8738", "token": "old", "managed": True,
                                  "slug": "local_ocr"})
    entry.add_to_hass(hass)
    aioclient_mock.get("http://local-ocr:8738/v1/info", status=401, json={})
    await entry.start_reauth_flow(hass)

    def reauth_flows():
        return [f for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                if f["context"]["source"] == "reauth"]

    assert len(reauth_flows()) == 1
    with patch("custom_components.local_ocr.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
            data=hassio_info(token="fresh"))
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "fresh"
    assert reauth_flows() == []


async def test_hassio_discovery_same_token_keeps_reauth(hass, aioclient_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": URL, "token": "old", "managed": False})
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{URL}/v1/info", exc=aiohttp.ClientConnectionError())
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "x"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_saves(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": URL, "token": None, "managed": False},
                            options={"preset": "text", "min_confidence": 0.5, "timeout": 30})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"preset": "envelope", "min_confidence": 0.7, "timeout": 45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"preset": "envelope", "min_confidence": 0.7, "timeout": 45}


async def test_single_instance_abort(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={"url": URL}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_setup_entry(hass, aioclient_mock):
    mock_server(aioclient_mock, URL)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
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
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": "http://local-ocr:8738", "token": TOKEN, "managed": True,
                                  "slug": "local_ocr"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["url"] == "http://local-ocr:8738"


async def test_zeroconf_updates_a_standalone_entry_url(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Local OCR",
                            data={"url": "http://192.168.1.4:8738", "token": None, "managed": False})
    entry.add_to_hass(hass)
    with patch("custom_components.local_ocr.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=zc_info("0"))
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://192.168.1.5:8738"


def test_every_form_field_has_a_description():
    import json
    from pathlib import Path

    strings = json.loads((Path(__file__).parents[1] / "custom_components/local_ocr/strings.json").read_text())
    steps = [*strings["config"]["step"].values(), *strings["options"]["step"].values()]
    for step in steps:
        assert set(step.get("data", {})) == set(step.get("data_description", {})), step.get("title")

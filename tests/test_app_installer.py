"""Tests for installing and finding the Local OCR app through Supervisor."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohasupervisor import SupervisorError
from aiohasupervisor.models import StoreAddRepository
from homeassistant.components.hassio import AddonError, AddonState

from custom_components.text_reader import app_installer
from custom_components.text_reader.app_installer import (
    APP_REPO_URL,
    InstallFailed,
    install_and_start,
    installed_app,
    local_supported,
    start_installed,
    wait_for_discovery,
)

AI = "custom_components.text_reader.app_installer"
SLUG = "abc123_local_ocr"


REPO = SimpleNamespace(slug="abc123", source=APP_REPO_URL + ".git", url=APP_REPO_URL + "/")
OTHER = SimpleNamespace(slug="evil999", source="https://github.com/someone/local-ocr", url="")


def _app(slug=SLUG, installed=False, repository="abc123"):
    return SimpleNamespace(slug=slug, installed=installed, repository=repository)


@pytest.fixture
def sup():
    """A fake Supervisor client plus AddonManager, patched where the installer imports them."""
    client = MagicMock()
    client.store.addons_list = AsyncMock(return_value=[])
    client.store.add_repository = AsyncMock()
    client.store.repositories_list = AsyncMock(return_value=[REPO, OTHER])
    client.discovery.list = AsyncMock(return_value=[])
    manager = MagicMock()
    manager.async_install_addon = AsyncMock()
    manager.async_start_addon = AsyncMock()
    manager.async_get_addon_info = AsyncMock(
        return_value=SimpleNamespace(state=AddonState.NOT_RUNNING))
    manager_cls = MagicMock(return_value=manager)
    with patch("homeassistant.components.hassio.get_supervisor_client", return_value=client), \
            patch("homeassistant.components.hassio.AddonManager", manager_cls):
        yield SimpleNamespace(client=client, manager=manager, manager_cls=manager_cls)


def _meminfo(tmp_path, kb):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal:       {kb} kB\nMemFree:        1000 kB\n")
    return path


@pytest.mark.parametrize(("machine", "kb", "expected"), [
    ("x86_64", 8_000_000, (True, "")),
    ("aarch64", 1_900_000, (True, "")),
    ("aarch64", 1_700_000, (True, "")),  # a 2 GB Pi after the GPU reservation (R8)
    ("aarch64", 1_699_999, (False, "memory")),
    ("arm64", 3_800_000, (True, "")),
    ("aarch64", 949_000, (False, "memory")),
    ("armv7l", 8_000_000, (False, "arch")),
    ("i686", 8_000_000, (False, "arch")),
])
async def test_local_supported_platform(hass, tmp_path, machine, kb, expected):
    with patch(f"{AI}.MEMINFO_PATH", _meminfo(tmp_path, kb)), \
            patch(f"{AI}.platform.machine", return_value=machine):
        assert await local_supported(hass) == expected


async def test_local_supported_prefers_supervisor_arch(hass, tmp_path):
    with patch(f"{AI}.MEMINFO_PATH", _meminfo(tmp_path, 4_000_000)), \
            patch(f"{AI}.is_hassio", return_value=True), \
            patch(f"{AI}.platform.machine", return_value="aarch64"), \
            patch("homeassistant.components.hassio.get_info", return_value={"arch": "armv7"}):
        assert await local_supported(hass) == (False, "arch")


async def test_local_supported_supervisor_not_ready_falls_back(hass, tmp_path):
    with patch(f"{AI}.MEMINFO_PATH", _meminfo(tmp_path, 4_000_000)), \
            patch(f"{AI}.is_hassio", return_value=True), \
            patch(f"{AI}.platform.machine", return_value="x86_64"), \
            patch("homeassistant.components.hassio.get_info", side_effect=RuntimeError):
        assert await local_supported(hass) == (True, "")


async def test_local_supported_unreadable_meminfo_is_not_a_veto(hass, tmp_path):
    with patch(f"{AI}.MEMINFO_PATH", tmp_path / "missing"), \
            patch(f"{AI}.platform.machine", return_value="x86_64"):
        assert await local_supported(hass) == (True, "")
    (tmp_path / "odd").write_text("MemFree: 1 kB\n")
    with patch(f"{AI}.MEMINFO_PATH", tmp_path / "odd"), \
            patch(f"{AI}.platform.machine", return_value="x86_64"):
        assert await local_supported(hass) == (True, "")


async def test_install_adds_repo_installs_and_starts(hass, sup):
    sup.client.store.repositories_list.side_effect = [[OTHER], [OTHER, REPO]]
    sup.client.store.addons_list.return_value = [
        _app("core_mosquitto", installed=True, repository="core"), _app()]
    assert await install_and_start(hass) == SLUG
    sup.client.store.add_repository.assert_awaited_once_with(StoreAddRepository(repository=APP_REPO_URL))
    assert sup.manager_cls.call_args.args[2:] == ("Local OCR", SLUG)
    sup.manager.async_install_addon.assert_awaited_once()
    sup.manager.async_start_addon.assert_awaited_once()


async def test_install_skips_repo_and_install_when_present(hass, sup):
    sup.client.store.addons_list.return_value = [_app(installed=True)]
    sup.manager.async_get_addon_info.return_value = SimpleNamespace(state=AddonState.RUNNING)
    assert await install_and_start(hass) == SLUG
    sup.client.store.add_repository.assert_not_awaited()
    sup.manager.async_install_addon.assert_not_awaited()
    sup.manager.async_start_addon.assert_not_awaited()


async def test_install_repository_error(hass, sup, caplog):
    sup.client.store.add_repository.side_effect = SupervisorError("private repo")
    with pytest.raises(InstallFailed) as err:
        await install_and_start(hass)
    assert err.value.reason == "repository"
    assert "repository" in caplog.text


async def test_install_app_missing_after_repo(hass, sup):
    sup.client.store.repositories_list.side_effect = [[], [REPO]]
    with pytest.raises(InstallFailed) as err:
        await install_and_start(hass)
    assert err.value.reason == "not_found"


async def test_install_addon_error(hass, sup):
    sup.client.store.addons_list.return_value = [_app()]
    sup.manager.async_install_addon.side_effect = AddonError("no space")
    with pytest.raises(InstallFailed) as err:
        await install_and_start(hass)
    assert err.value.reason == "install"


async def test_install_start_error(hass, sup):
    sup.client.store.addons_list.return_value = [_app(installed=True)]
    sup.manager.async_start_addon.side_effect = AddonError("boom")
    with pytest.raises(InstallFailed) as err:
        await install_and_start(hass)
    assert err.value.reason == "start"


async def test_installed_app(hass, sup):
    sup.client.store.addons_list.return_value = [_app()]
    assert await installed_app(hass) is None
    sup.client.store.addons_list.return_value = [_app(), _app("def456_local_ocr", installed=True,
                                                                   repository="def456")]
    assert await installed_app(hass) is None  # installed, but not from our repository
    sup.client.store.addons_list.return_value = [_app(), _app("abc123_local_ocr", installed=True)]
    assert await installed_app(hass) == SLUG
    sup.client.store.addons_list.side_effect = SupervisorError("down")
    assert await installed_app(hass) is None


async def test_start_installed_is_best_effort(hass, sup):
    assert await start_installed(hass) is None  # nothing installed: nothing to do
    sup.manager.async_start_addon.assert_not_awaited()
    sup.client.store.addons_list.return_value = [_app(installed=True)]
    assert await start_installed(hass) == SLUG
    sup.manager.async_start_addon.assert_awaited_once()
    sup.manager.async_start_addon.side_effect = AddonError("boom")
    await start_installed(hass)  # swallowed


async def test_wait_for_discovery_returns_config(hass, sup):
    ours = SimpleNamespace(service="text_reader", addon=SLUG,
                           config={"host": "abc123-local-ocr", "port": 8738, "token": "t"})
    sup.client.discovery.list.side_effect = [
        SupervisorError("busy"),
        [SimpleNamespace(service="mqtt", addon="core_mosquitto", config={})],
        [ours],
    ]
    with patch(f"{AI}.POLL_INTERVAL", 0):
        cfg = await wait_for_discovery(hass, timeout=5)
    assert cfg == {"host": "abc123-local-ocr", "port": 8738, "token": "t", "slug": SLUG}


async def test_wait_for_discovery_times_out(hass, sup):
    with patch(f"{AI}.POLL_INTERVAL", 0.01), pytest.raises(InstallFailed) as err:
        await wait_for_discovery(hass, timeout=0.05)
    assert err.value.reason == "timeout"


def test_app_slug_matching():
    assert not app_installer._is_app("local_ocr")  # a local build is not our repository's app
    assert app_installer._is_app("1a2b3c4d_local_ocr")
    assert not app_installer._is_app("local_ocr_extra")


async def test_find_app_ignores_lookalike_repositories(hass, sup):
    sup.client.store.repositories_list.return_value = [OTHER]
    sup.client.store.addons_list.return_value = [_app("evil999_local_ocr", installed=True,
                                                      repository="evil999")]
    assert await installed_app(hass) is None
    sup.client.store.repositories_list.return_value = [OTHER, REPO]
    assert await installed_app(hass) is None  # the installed one is still the look-alike
    sup.client.store.addons_list.return_value.append(_app(installed=True))
    assert await installed_app(hass) == SLUG


async def test_wait_for_discovery_fails_fast_when_app_stops(hass, sup):
    with patch(f"{AI}.POLL_INTERVAL", 0), pytest.raises(InstallFailed) as err:
        await wait_for_discovery(hass, timeout=5, slug=SLUG)
    assert err.value.reason == "not_started"


async def test_wait_for_discovery_tolerates_unknown_state(hass, sup):
    ours = SimpleNamespace(service="text_reader", addon=SLUG, config={"host": "h"})
    sup.client.discovery.list.side_effect = [[], [], [ours]]
    sup.manager.async_get_addon_info.side_effect = [
        AddonError("busy"), SimpleNamespace(state=AddonState.RUNNING)]
    with patch(f"{AI}.POLL_INTERVAL", 0):
        assert await wait_for_discovery(hass, timeout=5, slug=SLUG) == {"host": "h", "slug": SLUG}

"""Status coordinator for Local OCR."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import CannotConnect, Health, Info, LocalOcrError, Unauthorized
from .const import DOMAIN, MIN_API

if TYPE_CHECKING:
    from .data import LocalOcrConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)
INFO_EVERY = 10
FAILURES_FOR_ISSUE = 3
OOM_RECENT = timedelta(hours=1)
ISSUE_IDS = ("app_not_running", "app_outdated", "model_failed", "low_memory")


@dataclass
class Status:
    """Latest known state of the server."""

    health: Health | None
    info: Info | None
    consecutive_failures: int


class LocalOcrCoordinator(DataUpdateCoordinator[Status]):
    """Poll the Local OCR server's health."""

    config_entry: LocalOcrConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LocalOcrConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._polls = 0
        self._info: Info | None = None
        self._failures = 0
        self._auth_failed = False
        self._oom_kills: int | None = None  # last count seen; the first poll sets the baseline
        self._oom_kill_seen: datetime | None = None  # when a poll last saw the count rise

    async def _async_update_data(self) -> Status:
        client = self.config_entry.runtime_data.client
        health: Health | None = None
        try:
            if self._info is None or self._polls % INFO_EVERY == 0:
                self._info = await client.info()
            health = await client.health()
            self._failures = 0
            self._auth_failed = False
        except Unauthorized:
            # The app answered; the token is wrong. Not a connection failure.
            self._failures = 0
            if not self._auth_failed:
                self._auth_failed = True
                self.config_entry.async_start_reauth(self.hass)
        except (CannotConnect, LocalOcrError):
            self._failures += 1
        self._polls += 1
        status = Status(health, self._info, self._failures)
        self._sync_issues(status)
        return status

    def _memory_low(self, health: Health | None) -> bool:
        """True when the server ran out of memory recently.

        Causes: a model load failed for lack of memory ("out of memory: ..." detail); an inference ran out of
        memory within the last hour (last_oom); or the kernel OOM-killed the server (cgroup oom_kills rose since
        the entry loaded) within the last hour.
        """
        if health is None:
            return False
        now = dt_util.utcnow()
        if health.oom_kills is not None:
            if self._oom_kills is not None and health.oom_kills > self._oom_kills:
                self._oom_kill_seen = now
            self._oom_kills = health.oom_kills
        if self._oom_kill_seen is not None and now - self._oom_kill_seen <= OOM_RECENT:
            return True
        if (health.detail or "").lower().startswith("out of memory"):
            return True
        if health.last_oom:
            last = dt_util.parse_datetime(health.last_oom)
            if last is not None and last.tzinfo is not None and now - last <= OOM_RECENT:
                return True
        return False

    def _sync_issues(self, status: Status) -> None:
        entry = self.config_entry
        health, info = status.health, status.info
        active: dict[str, tuple[bool, str, dict[str, str]]] = {}
        if status.consecutive_failures >= FAILURES_FOR_ISSUE and not self._auth_failed:
            fixable = bool(entry.data.get("managed") and entry.data.get("slug"))
            key = "app_not_running" if fixable else "app_not_running_unmanaged"
            active["app_not_running"] = (fixable, key, {})
        if info is not None and info.api < MIN_API:
            active["app_outdated"] = (False, "app_outdated", {})
        if self._memory_low(health):
            # A memory cause replaces model_failed: the fix is memory, not a restart.
            active["low_memory"] = (False, "low_memory", {})
        elif health is not None and health.model_state == "error":
            active["model_failed"] = (
                False,
                "model_failed",
                {"detail": health.detail or "unknown"},
            )
        for issue_id in ISSUE_IDS:
            if issue_id in active:
                fixable, key, placeholders = active[issue_id]
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=fixable,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=key,
                    translation_placeholders=placeholders,
                    data={"entry_id": entry.entry_id},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def clear_issues(self) -> None:
        """Remove every issue this entry raised."""
        for issue_id in ISSUE_IDS:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

"""Status coordinator for Text Reader."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import CannotConnect, Health, Info, LocalOcrError, Unauthorized
from .const import DOMAIN, MIN_API
from .data import settings

if TYPE_CHECKING:
    from .data import TextReaderConfigEntry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=60)
INFO_EVERY = 10
FAILURES_FOR_ISSUE = 3
OOM_RECENT = timedelta(hours=1)
ISSUE_IDS = (
    "app_not_running",
    "app_outdated",
    "model_failed",
    "low_memory",
    "ai_entity_missing",
    "ai_entity_unavailable",
    "local_unsupported_here",
)
LOCAL_BACKENDS = ("local", "auto")
AI_BACKENDS = ("ai_task", "auto")


@dataclass
class Status:
    """Latest known state of the server."""

    health: Health | None
    info: Info | None
    consecutive_failures: int
    uses_local: bool = True
    # None when the entry doesn't use an AI assistant; else "ok", "missing" or "unavailable".
    ai_state: str | None = None

    @property
    def healthy(self) -> bool:
        """True when every backend the entry uses is healthy."""
        local_ok = not self.uses_local or (
            self.health is not None and self.health.status == "ok"
        )
        return local_ok and self.ai_state in (None, "ok")


class TextReaderCoordinator(DataUpdateCoordinator[Status]):
    """Poll the Local OCR server's health and check the AI assistant."""

    config_entry: TextReaderConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TextReaderConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.local_unsupported = False  # set at setup (Supervisor installs only)
        self._ai_unavailable = 0
        self._polls = 0
        self._info: Info | None = None
        self._failures = 0
        self._auth_failed = False
        self._oom_kills: int | None = None  # last count seen; the first poll sets the baseline
        self._oom_kill_seen: datetime | None = None  # when a poll last saw the count rise

    @property
    def uses_local(self) -> bool:
        return (
            self.config_entry.runtime_data.client is not None
            and settings(self.config_entry).backend in LOCAL_BACKENDS
        )

    @property
    def ai_entity(self) -> str | None:
        """The AI assistant entity in use, or None when the backend doesn't use one."""
        cfg = settings(self.config_entry)
        return cfg.ai_entity if cfg.backend in AI_BACKENDS else None

    def _ai_state(self, entity_id: str | None) -> str | None:
        if entity_id is None:
            self._ai_unavailable = 0
            return None
        state = self.hass.states.get(entity_id)
        if er.async_get(self.hass).async_get(entity_id) is None and state is None:
            self._ai_unavailable = 0
            return "missing"
        if state is not None and state.state == STATE_UNAVAILABLE:
            self._ai_unavailable += 1
            return "unavailable"
        self._ai_unavailable = 0
        return "ok"

    async def _async_update_data(self) -> Status:
        ai_state = self._ai_state(self.ai_entity)
        if not self.uses_local:
            status = Status(None, None, 0, False, ai_state)
            self._sync_issues(status)
            return status
        client = self.config_entry.runtime_data.client
        assert client is not None
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
        status = Status(health, self._info, self._failures, True, ai_state)
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
        active: dict[str, tuple[bool, str, dict[str, str]]] = {}
        if self.local_unsupported:
            active["local_unsupported_here"] = (True, "local_unsupported_here", {})
        entity = self.ai_entity
        if entity is not None and status.ai_state == "missing":
            active["ai_entity_missing"] = (True, "ai_entity_missing", {"entity": entity})
        elif entity is not None and self._ai_unavailable >= FAILURES_FOR_ISSUE:
            key = "ai_entity_unavailable" if status.uses_local else "ai_entity_unavailable_only"
            active["ai_entity_unavailable"] = (False, key, {"entity": entity})
        if status.uses_local:
            self._local_issues(status, active)
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

    def _local_issues(
        self, status: Status, active: dict[str, tuple[bool, str, dict[str, str]]]
    ) -> None:
        entry = self.config_entry
        health, info = status.health, status.info
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

    def clear_issues(self) -> None:
        """Remove every issue this entry raised."""
        for issue_id in ISSUE_IDS:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

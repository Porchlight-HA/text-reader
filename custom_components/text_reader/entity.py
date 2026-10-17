"""Base entity for Text Reader."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Status, TextReaderCoordinator
from .data import TextReaderConfigEntry


class TextReaderEntity(CoordinatorEntity[TextReaderCoordinator]):
    """Entity bound to the single Text Reader service device."""

    _attr_has_entity_name = True

    def __init__(self, entry: TextReaderConfigEntry, key: str) -> None:
        coordinator = entry.runtime_data.coordinator
        assert coordinator is not None
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        status: Status = self.coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name="Text Reader",
            sw_version=status.info.version if status and status.info else None,
        )

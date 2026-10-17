"""Base entity for Local OCR."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LocalOcrCoordinator, Status
from .data import LocalOcrConfigEntry


class LocalOcrEntity(CoordinatorEntity[LocalOcrCoordinator]):
    """Entity bound to the single Local OCR service device."""

    _attr_has_entity_name = True

    def __init__(self, entry: LocalOcrConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        status: Status = self.coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name="Local OCR",
            sw_version=status.info.version if status and status.info else None,
        )

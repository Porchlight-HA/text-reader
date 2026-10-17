"""Last-duration sensor for Local OCR."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SIGNAL_DURATION_UPDATED
from .data import LocalOcrConfigEntry
from .entity import LocalOcrEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: LocalOcrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LocalOcrLastDuration(entry)])


class LocalOcrLastDuration(LocalOcrEntity, SensorEntity):
    """How long the last read took."""

    _attr_translation_key = "last_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: LocalOcrConfigEntry) -> None:
        super().__init__(entry, "last_duration")
        self._entry = entry

    @property
    def native_value(self) -> int | None:
        return self._entry.runtime_data.last_duration_ms

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DURATION_UPDATED, self._on_duration)
        )

    @callback
    def _on_duration(self) -> None:
        self.async_write_ha_state()

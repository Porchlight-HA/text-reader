"""Status binary sensor for Text Reader."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .data import TextReaderConfigEntry
from .entity import TextReaderEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: TextReaderConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([TextReaderStatusSensor(entry)])


class TextReaderStatusSensor(TextReaderEntity, BinarySensorEntity):
    """On when every backend in use is healthy: the App reports ok, the AI assistant is available."""

    _attr_translation_key = "status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: TextReaderConfigEntry) -> None:
        super().__init__(entry, "status")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.healthy

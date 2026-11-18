"""Binary sensors for Geist JSON digital inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.exceptions import HomeAssistantError

from .const import API_TYPE_JSON, CONF_API_TYPE
from .entity import IntegrationBlueprintEntity, json_device_info
from .types import JsonDevice, JsonParsedData, JsonSensor, is_digital_measurement

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import BlueprintDataUpdateCoordinator
    from .data import IntegrationBlueprintConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up digital inputs reported by the JSON API."""
    if entry.data.get(CONF_API_TYPE) != API_TYPE_JSON:
        return
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    if not isinstance(data, JsonParsedData):
        message = "The JSON API returned unexpected digital input data"
        raise HomeAssistantError(message)
    entities: list[GeistDigitalInput] = []
    for device in data.devices:
        for sensor in device.sensors:
            if not is_digital_measurement(sensor):
                continue
            entity = GeistDigitalInput(coordinator, device, sensor)
            entities.append(entity)
    async_add_entities(entities)


class GeistDigitalInput(IntegrationBlueprintEntity, BinarySensorEntity):
    """A CCAT digital input."""

    _attr_name = "Digital"
    _attr_icon = "mdi:dip-switch"

    def __init__(
        self,
        coordinator: BlueprintDataUpdateCoordinator,
        device: JsonDevice,
        sensor: JsonSensor,
    ) -> None:
        """Initialize a digital input."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._sensor_key = sensor.key
        self._attr_unique_id = f"{device.id}_{sensor.key}"
        self._attr_device_info = json_device_info(device)

    @property
    def is_on(self) -> bool | None:
        """Return the input state, or unknown for unrecognized values."""
        data = self.coordinator.data
        if isinstance(data, JsonParsedData):
            for device in data.devices:
                if device.id == self._device_id:
                    for sensor in device.sensors:
                        if sensor.key == self._sensor_key:
                            if sensor.value in (1, "1"):
                                return True
                            if sensor.value in (0, "0"):
                                return False
                            return None
        return None

"""Binary sensors for Geist JSON digital inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from .const import API_TYPE_JSON, CONF_API_TYPE, DOMAIN
from .entity import IntegrationBlueprintEntity, json_entity_id
from .types import JsonDevice, JsonParsedData, JsonSensor, is_digital_measurement

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData, HomeAssistant
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
    registry = er.async_get(hass)
    entities: list[GeistDigitalInput] = []
    for device in data.devices:
        for sensor in device.sensors:
            if not is_digital_measurement(sensor):
                continue
            entity = GeistDigitalInput(coordinator, device, sensor)
            unique_id = f"{device.id}_{sensor.key}"
            existing_id = registry.async_get_entity_id(
                "binary_sensor", DOMAIN, unique_id
            )
            if existing_id:
                existing = registry.async_get(existing_id)
                old_defaults = (
                    json_entity_id("binary_sensor", unique_id),
                    json_entity_id("binary_sensor", f"{unique_id}_digital"),
                )
                if (
                    existing
                    and existing.config_entry_id == entry.entry_id
                    and any(
                        existing_id == old_default
                        or (
                            existing_id.startswith(f"{old_default}_")
                            and existing_id.removeprefix(f"{old_default}_").isdecimal()
                        )
                        for old_default in old_defaults
                    )
                ):
                    new_id = registry.async_generate_entity_id(
                        "binary_sensor", entity.entity_id.removeprefix("binary_sensor.")
                    )
                    registry.async_update_entity(existing_id, new_entity_id=new_id)
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
        self.entity_id = json_entity_id("binary_sensor", f"{device.id}_digital")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Geist",
            model=device.model or None,
        )
        if device.serial_number:
            self._attr_device_info["serial_number"] = device.serial_number

    async def async_added_to_hass(self) -> None:
        """Remove the legacy sensor only after this binary sensor has a state."""
        await super().async_added_to_hass()
        listening = True

        @callback
        def stop_listening() -> None:
            nonlocal listening
            if listening:
                listening = False
                unsubscribe()

        @callback
        def remove_legacy(event: Event[EventStateChangedData]) -> None:
            if event.data["new_state"] is None:
                return
            stop_listening()
            entry_id = self.coordinator.config_entry.entry_id
            if (
                not self.registry_entry
                or self.registry_entry.config_entry_id != entry_id
            ):
                return
            registry = er.async_get(self.hass)
            legacy_id = registry.async_get_entity_id(
                "sensor", DOMAIN, self.registry_entry.unique_id
            )
            legacy = registry.async_get(legacy_id) if legacy_id else None
            if legacy and legacy.config_entry_id == entry_id:
                registry.async_remove(legacy.entity_id)

        unsubscribe = async_track_state_change_event(
            self.hass, [self.entity_id], remove_legacy
        )
        self.async_on_remove(stop_listening)

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

"""Sensor platform for ha_geist.

Creates one sensor per `<field>` element under each `<device>` in the
parsed XML. Each Home Assistant device is created from the `<device>`
element using its `name` attribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .entity import IntegrationBlueprintEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import BlueprintDataUpdateCoordinator
    from .data import IntegrationBlueprintConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from parsed XML data."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    devices = data.get("devices") or []

    entities: list[FieldSensor] = []
    for dev in devices:
        device_id = dev.get("id")
        device_name = dev.get("name")
        fields = dev.get("fields") or {}
        # Each field value may be a dict with `value` and `niceName` keys
        for key, value in fields.items():
            if isinstance(value, dict):
                nice = value.get("niceName") or key
            else:
                nice = key
        entities.append(
            FieldSensor(
                coordinator=coordinator,
                device_id=device_id or "",
                device_name=device_name or "",
                field_key=key,
                nice_name=nice,
            )
        )

    if entities:
        async_add_entities(entities)


class FieldSensor(IntegrationBlueprintEntity, SensorEntity):
    """Sensor representing a single `<field>` of a device."""

    def __init__(
        self,
        coordinator: BlueprintDataUpdateCoordinator,
        device_id: str,
        device_name: str,
        field_key: str,
        nice_name: str,
    ) -> None:
        """Initialize the field sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._field_key = field_key
        self._attr_name = nice_name
        # Make unique id by combining entry id, device id and field key
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{device_id}_{field_key}"
        )
        # DeviceInfo for grouping sensors under a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name or None,
        )

    @property
    def native_value(self) -> str | None:
        """Return the latest value for this field from coordinator data."""
        data = self.coordinator.data or {}
        devices = data.get("devices") or []
        for dev in devices:
            if dev.get("id") == self._device_id:
                fields = dev.get("fields") or {}
                entry = fields.get(self._field_key)
                if isinstance(entry, dict):
                    return entry.get("value")
                return entry
        return None

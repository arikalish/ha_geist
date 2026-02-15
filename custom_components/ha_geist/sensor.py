"""Sensor platform for ha_geist.

Creates one sensor per `<field>` element under each `<device>` in the
parsed XML. Each Home Assistant device is created from the `<device>`
element using its `name` attribute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
import re
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .entity import IntegrationBlueprintEntity
from .types import Device, Field

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
    devices: list[Device] = data.get("devices") or []

    entities: list[FieldSensor] = []
    for dev in devices:
        device_id = dev.id
        device_name = dev.name
        manufacturer = dev.manufacturer if hasattr(dev, "manufacturer") else None
        model = dev.type if hasattr(dev, "type") else None
        fields = dev.fields or {}
        for key, field_obj in fields.items():
            if isinstance(field_obj, Field):
                nice = field_obj.niceName or "Unknown"
            else:
                nice = key or "Unknown"

            entities.append(
                FieldSensor(
                    coordinator=coordinator,
                    device_id=device_id or "",
                    device_name=device_name or "",
                    manufacturer=manufacturer,
                    model=model,
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
        manufacturer: str | None,
        model: str | None,
        field_key: str,
        nice_name: str,
    ) -> None:
        """Initialize the field sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._field_key = field_key
        self._attr_name = nice_name
        # Make unique id per entity using the device `id` and field key
        self._attr_unique_id = f"{device_id}_{field_key}"
        # DeviceInfo for grouping sensors under a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name or None,
            manufacturer=manufacturer,
            model=model,
        )

        # Unit of measurement
        self._attr_native_unit_of_measurement = self.unit_for_field_key(field_key)
        self._attr_icon = self.icon_for_field_key(field_key)
        # Set entity_id to a slugified version of the device id and field key
        safe_key = re.sub(r"[^a-z0-9_]", "_", (field_key or "").lower())
        safe_device = re.sub(r"[^a-z0-9_]", "_", (device_id or "").lower())
        self.entity_id = f"{DOMAIN}.{safe_device}_{safe_key}"

    @property
    def native_value(self) -> str | None:
        """Return the latest value for this field from coordinator data."""
        data = self.coordinator.data or {}
        devices: list[Device] = data.get("devices") or []
        for dev in devices:
            if dev.id == self._device_id:
                entry = dev.fields.get(self._field_key)
                if isinstance(entry, Field):
                    return entry.value
                return entry
        return None

    @staticmethod
    def icon_for_field_key(key: str) -> str | None:  # noqa: PLR0911
        """Return an icon based on the field key."""
        if key == "TOTAL":
            return "mdi:power-plug"
        if key.startswith("CT"):
            return "mdi:power-socket-us"
        if key in {"TempC", "TempF"}:
            return "mdi:thermometer"
        if key in {"DewPtC", "DewPtF"}:
            return "mdi:thermometer-water"
        if key == "Humidity":
            return "mdi:water-percent"
        if key.startswith("Volts"):
            return "mdi:sine-wave"
        if key.startswith("Amps"):
            return "mdi:current-ac"
        if key.startswith("Pwr-Factor"):
            return "mdi:percent-circle"
        return None

    @staticmethod
    def unit_for_field_key(key: str) -> str | None:  # noqa: PLR0911
        """
        Return unit of measurement for a given field key.

        Examples:
        - "TOTAL" and keys starting with "CT" -> "A"
        - Temperature keys -> "°C" or "°F"
        - Voltage -> "V"
        - Power -> "W" or "VA"
        - Percentages -> "%"

        """
        if key == "TOTAL" or key.startswith("CT"):
            return "A"
        if key in {"TempC", "DewPtC"}:
            return "°C"
        if key in {"TempF", "DewPtF"}:
            return "°F"
        if key == "Humidity":
            return "%"
        if key.startswith("Volts"):
            return "V"
        if key.startswith("Amps"):
            return "A"
        if key.startswith("RealPower"):
            return "W"
        if key.startswith("ApPower"):
            return "VA"
        if key.startswith("Pwr-Factor"):
            return "%"
        return None

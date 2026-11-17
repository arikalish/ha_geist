"""Sensor platform for ha_geist.

Creates one sensor per `<field>` element under each `<device>` in the
parsed XML. Each Home Assistant device is created from the `<device>`
element using its `name` attribute.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from .const import API_TYPE_JSON, CONF_API_TYPE, DOMAIN
from .entity import IntegrationBlueprintEntity
from .types import Device, Field, JsonDevice, JsonParsedData, JsonSensor

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import BlueprintDataUpdateCoordinator
    from .data import IntegrationBlueprintConfigEntry


_JSON_SENSOR_FIELDS = {
    "temperature": "TempC",
    "temp": "TempC",
    "dewpoint": "DewPtC",
    "humidity": "Humidity",
    "relativehumidity": "Humidity",
    "voltage": "Volt",
    "volts": "Volt",
    "current": "Amps",
    "amps": "Amps",
    "powerfactor": "Pwr-Factor",
    "sound": "Sound",
    "airflow": "Airflow",
    "light": "Light",
    "analog": "IO",
    "realpower": "RealPower",
    "power": "RealPower",
    "apparentpower": "ApPower",
}

_FIELD_DEVICE_CLASSES = {
    "TempC": SensorDeviceClass.TEMPERATURE,
    "DewPtC": SensorDeviceClass.TEMPERATURE,
    "Humidity": SensorDeviceClass.HUMIDITY,
    "Volt": SensorDeviceClass.VOLTAGE,
    "Amps": SensorDeviceClass.CURRENT,
    "Pwr-Factor": SensorDeviceClass.POWER_FACTOR,
    "RealPower": SensorDeviceClass.POWER,
    "ApPower": SensorDeviceClass.APPARENT_POWER,
}


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from parsed XML data."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    if entry.data.get(CONF_API_TYPE) == API_TYPE_JSON:
        if not isinstance(data, JsonParsedData):
            message = "The JSON API returned unexpected data"
            raise HomeAssistantError(message)
        async_add_entities(
            [
                JsonSensorEntity(coordinator, device, sensor)
                for device in data.devices
                for sensor in device.sensors
            ]
        )
        return

    devices: list[Device] = data.get("devices", []) if data else []

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
                    serial_number=dev.serial_number,
                    sw_version=dev.sw_version,
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
        serial_number: str | None,
        sw_version: str | None,
        field_key: str,
        nice_name: str,
    ) -> None:
        """Initialize the field sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._field_key = field_key
        self._attr_name = "Relative Humidity" if field_key == "Humidity" else nice_name
        # Make unique id per entity using the device `id` and field key
        self._attr_unique_id = f"{device_id}_{field_key}"
        # DeviceInfo for grouping sensors under a Home Assistant device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name or None,
            manufacturer=manufacturer,
            model=model,
            serial_number=serial_number,
            sw_version=sw_version,
        )

        # Unit of measurement
        self._attr_native_unit_of_measurement = self.unit_for_field_key(field_key)
        self._attr_icon = self.icon_for_field_key(field_key)
        # Set entity_id to a slugified version of the device id and field key
        safe_key = re.sub(r"[^a-z0-9_]", "_", (field_key or "").lower())
        safe_device = re.sub(r"[^a-z0-9_]", "_", (device_id or "").lower())
        self.entity_id = f"sensor.{safe_device}_{safe_key}"

    @property
    def native_value(self) -> float | str | None:
        """Return the latest value for this field from coordinator data."""
        data = self.coordinator.data or {}
        devices: list[Device] = data.get("devices") or []
        for dev in devices:
            if dev.id == self._device_id:
                entry = dev.fields.get(self._field_key)
                value = entry.value if isinstance(entry, Field) else entry
                if value is None:
                    return None
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    return (
                        None
                        if self._attr_native_unit_of_measurement is not None
                        else value
                    )
                return numeric_value if math.isfinite(numeric_value) else None
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
        if key.startswith("Volt"):
            return "mdi:sine-wave"
        if key.startswith("Amps"):
            return "mdi:current-ac"
        if key.startswith("Pwr-Factor"):
            return "mdi:percent-circle"
        if key == "Sound":
            return "mdi:volume-high"
        if key == "Airflow":
            return "mdi:weather-dust"
        if key.startswith("IO"):
            return "mdi:chart-line-variant"
        if key == "Light":
            return "mdi:lightbulb-on-50"
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
        if key == "TOTAL" or key.startswith(("CT", "Amps")):
            return UnitOfElectricCurrent.AMPERE
        if key in {"TempC", "DewPtC"}:
            return UnitOfTemperature.CELSIUS
        if key in {"TempF", "DewPtF"}:
            return UnitOfTemperature.FAHRENHEIT
        if key == "Humidity" or key.startswith("Pwr-Factor"):
            return PERCENTAGE
        if key.startswith("Volt"):
            return UnitOfElectricPotential.VOLT
        if key.startswith("RealPower"):
            return UnitOfPower.WATT
        if key.startswith("ApPower"):
            return UnitOfApparentPower.VOLT_AMPERE
        return None


class JsonSensorEntity(IntegrationBlueprintEntity, SensorEntity):
    """Sensor representing a measurement returned by the JSON API."""

    def __init__(
        self,
        coordinator: BlueprintDataUpdateCoordinator,
        device: JsonDevice,
        sensor: JsonSensor,
    ) -> None:
        """Initialize the JSON sensor."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._sensor_key = sensor.key
        self._attr_name = sensor.name
        self._attr_unique_id = f"{device.id}_{sensor.key}"
        self._attr_native_unit_of_measurement = {
            "C": UnitOfTemperature.CELSIUS,
            "F": UnitOfTemperature.FAHRENHEIT,
        }.get(sensor.unit or "", sensor.unit or None)
        field_key = None
        for value in (sensor.measurement_type, sensor.measurement_name):
            if value:
                field_key = _JSON_SENSOR_FIELDS.get(
                    re.sub(r"[^a-z0-9]", "", value.lower())
                )
                if field_key:
                    break
        if field_key:
            if field_key == "Humidity":
                self._attr_name = "Relative Humidity"
            self._attr_icon = FieldSensor.icon_for_field_key(field_key)
            device_class = _FIELD_DEVICE_CLASSES.get(field_key)
            if (
                field_key == "Humidity"
                and self._attr_native_unit_of_measurement is None
            ):
                self._attr_native_unit_of_measurement = PERCENTAGE
            if (
                device_class
                and self._attr_native_unit_of_measurement
                in DEVICE_CLASS_UNITS[device_class]
            ):
                self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Geist",
            model=device.model or None,
        )
        if device.serial_number:
            self._attr_device_info["serial_number"] = device.serial_number

    @property
    def native_value(self) -> float | None:
        """Return the latest numeric value from coordinator data."""
        data = self.coordinator.data
        if not isinstance(data, JsonParsedData):
            return None
        for device in data.devices:
            if device.id == self._device_id:
                for sensor in device.sensors:
                    if sensor.key == self._sensor_key:
                        try:
                            value = float(sensor.value)
                        except (TypeError, ValueError):
                            return None
                        return value if math.isfinite(value) else None
        return None

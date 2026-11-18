"""Expose measurements and read-only alarm states from XML and JSON devices."""

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
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import API_TYPE_JSON, CONF_API_TYPE, DOMAIN
from .coordinator import BlueprintDataUpdateCoordinator
from .entity import IntegrationBlueprintEntity, json_entity_id
from .types import (
    Alarm,
    Device,
    Field,
    JsonDevice,
    JsonParsedData,
    JsonSensor,
    current_alarms,
    find_alarm,
    is_digital_measurement,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import IntegrationBlueprintConfigEntry


_JSON_SENSOR_FIELDS = {
    "temperature": "TempC",
    "temp": "TempC",
    "dewpoint": "DewPtC",
    "humidity": "Humidity",
    "relativehumidity": "Humidity",
    "voltage": "Volts",
    "volts": "Volts",
    "current": "Amps",
    "amps": "Amps",
    "powerfactor": "Pwr-Factor",
    "sound": "Sound",
    "soundlevel": "Sound",
    "airflow": "Airflow",
    "light": "Light",
    "lightlevel": "Light",
    "analog": "IO",
    "realpower": "RealPower",
    "power": "RealPower",
    "apparentpower": "ApPower",
}

_JSON_FIELD_NAMES = {
    "TempC": "Temperature",
    "DewPtC": "Dew Point",
    "Humidity": "Relative Humidity",
    "Volts": "Volts",
    "Amps": "Amps",
    "Pwr-Factor": "Power Factor",
    "Sound": "Sound Level",
    "Airflow": "Air Flow",
    "Light": "Light Level",
    "RealPower": "Real Power",
    "ApPower": "Apparent Power",
}

_FIELD_DEVICE_CLASSES = {
    "TempC": SensorDeviceClass.TEMPERATURE,
    "DewPtC": SensorDeviceClass.TEMPERATURE,
    "Humidity": SensorDeviceClass.HUMIDITY,
    "Volts": SensorDeviceClass.VOLTAGE,
    "Amps": SensorDeviceClass.CURRENT,
    "Pwr-Factor": SensorDeviceClass.POWER_FACTOR,
    "RealPower": SensorDeviceClass.POWER,
    "ApPower": SensorDeviceClass.APPARENT_POWER,
}

_TEMPERATURE_UNITS = {
    "C": UnitOfTemperature.CELSIUS,
    "F": UnitOfTemperature.FAHRENHEIT,
    "°C": UnitOfTemperature.CELSIUS,
    "°F": UnitOfTemperature.FAHRENHEIT,
}


def _json_field_key(sensor: JsonSensor) -> str | None:
    """Find the equivalent XML field for a JSON measurement."""
    for value in (sensor.measurement_type, sensor.measurement_name):
        if value:
            field_key = _JSON_SENSOR_FIELDS.get(re.sub(r"[^a-z0-9]", "", value.lower()))
            if field_key:
                return field_key
    return None


def _json_temperature_unit(sensor: JsonSensor) -> UnitOfTemperature | None:
    """Recognize the temperature units reported by the device."""
    return (
        _TEMPERATURE_UNITS.get(sensor.unit.strip().upper())
        if isinstance(sensor.unit, str)
        else None
    )


def _json_sensor_name(sensor: JsonSensor, field_key: str | None) -> str:
    """Use XML-style names for known generic measurements, retaining custom labels."""
    name = (
        re.sub(r"\s*\([CF]\)$", "", sensor.name, flags=re.IGNORECASE)
        if field_key in {"TempC", "DewPtC"}
        else sensor.name
    )
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    if field_key == "Humidity" or (
        field_key and _JSON_SENSOR_FIELDS.get(normalized) == field_key
    ):
        return _JSON_FIELD_NAMES.get(field_key, name)
    return sensor.name


async def async_setup_entry(
    hass: HomeAssistant,
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
        _setup_json_sensors(hass, entry, data, async_add_entities)
        _setup_alarm_sensors(hass, entry, async_add_entities)
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
    _setup_alarm_sensors(hass, entry, async_add_entities)


def _setup_json_sensors(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
    data: JsonParsedData,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create JSON sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[JsonSensorEntity] = []
    for device in data.devices:
        for sensor in device.sensors:
            if is_digital_measurement(sensor):
                continue
            field_key = _json_field_key(sensor)
            source_unit = _json_temperature_unit(sensor)
            is_temperature = field_key in {"TempC", "DewPtC"} and source_unit
            units = (
                (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)
                if is_temperature
                else (None,)
            )
            new_entities = [
                JsonSensorEntity(coordinator, device, sensor, unit) for unit in units
            ]
            entities.extend(new_entities)
    async_add_entities(entities)


def _setup_alarm_sensors(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Reconcile alarm sensors with the device's configured triggers."""
    coordinator = entry.runtime_data.coordinator
    known_ids: set[str] = set()

    @callback
    def add_new_alarms() -> None:
        alarms = current_alarms(coordinator.data)
        if alarms is None:
            return
        current_ids = {alarm.id for alarm in alarms}
        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_alarm_"
        for existing in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                existing.domain == "sensor"
                and existing.platform == DOMAIN
                and existing.unique_id.startswith(prefix)
                and existing.unique_id.removeprefix(prefix) not in current_ids
            ):
                registry.async_remove(existing.entity_id)
        known_ids.intersection_update(current_ids)
        new_alarms = [alarm for alarm in alarms if alarm.id not in known_ids]
        known_ids.update(alarm.id for alarm in new_alarms)
        if new_alarms:
            async_add_entities(
                [
                    AlarmSensor(coordinator, entry.entry_id, alarm)
                    for alarm in new_alarms
                ]
            )

    add_new_alarms()
    entry.async_on_unload(coordinator.async_add_listener(add_new_alarms))


class AlarmSensor(CoordinatorEntity[BlueprintDataUpdateCoordinator], SensorEntity):
    """Expose the device's reported state for a configured alarm."""

    _attr_icon = "mdi:bell-alert"

    def __init__(
        self, coordinator: BlueprintDataUpdateCoordinator, entry_id: str, alarm: Alarm
    ) -> None:
        """Initialize an alarm sensor."""
        super().__init__(coordinator)
        self._alarm_id = alarm.id
        self._attr_unique_id = f"{entry_id}_alarm_{alarm.id}"
        self._attr_name = alarm.name
        self.entity_id = f"sensor.{slugify(alarm.id)}_alarm"
        if alarm.device_id:
            self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, alarm.device_id)})

    def _alarm(self) -> Alarm | None:
        """Find the latest data for this alarm."""
        return find_alarm(self.coordinator.data, self._alarm_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the name when an alarm slot is reconfigured."""
        alarm = self._alarm()
        if alarm and alarm.name != self._attr_name:
            self._attr_name = alarm.name
            if self.registry_entry:
                self.registry_entry = er.async_get(self.hass).async_update_entity(
                    self.entity_id, original_name=alarm.name
                )
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return whether the alarm is present in the latest successful update."""
        return super().available and self._alarm() is not None

    @property
    def native_value(self) -> str | None:
        """Return the exact state reported by the device."""
        alarm = self._alarm()
        return alarm.state if alarm else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return the alarm's reported configuration."""
        alarm = self._alarm()
        return alarm.attributes if alarm else None


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
        temperature_unit: UnitOfTemperature | None = None,
    ) -> None:
        """Initialize the JSON sensor."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._sensor_key = sensor.key
        self._temperature_unit = temperature_unit
        field_key = _json_field_key(sensor)
        name = _json_sensor_name(sensor, field_key)
        self._attr_name = name
        self._attr_unique_id = f"{device.id}_{sensor.key}"
        self._attr_native_unit_of_measurement = (
            temperature_unit or _json_temperature_unit(sensor) or sensor.unit or None
        )
        suffix = field_key
        if temperature_unit:
            if field_key not in {"TempC", "DewPtC"}:
                msg = (
                    "A temperature unit requires a temperature or dewpoint measurement"
                )
                raise ValueError(msg)
            suffix = f"{field_key[:-1]}{'C' if temperature_unit == UnitOfTemperature.CELSIUS else 'F'}"
            self._attr_unique_id = f"{self._attr_unique_id}_{suffix.lower()}"
            name = re.sub(r"\s*\([CF]\)$", "", name, flags=re.IGNORECASE)
            self._attr_name = f"{name} ({'C' if temperature_unit == UnitOfTemperature.CELSIUS else 'F'})"
            self._attr_suggested_unit_of_measurement = temperature_unit
        elif field_key in {"TempC", "DewPtC"} or (
            not suffix and sensor.key.startswith("entity_")
        ):
            suffix = sensor.measurement_type or sensor.measurement_name
        measurement_type = (
            "analog"
            if sensor.key.startswith("analog_")
            else suffix
            or sensor.measurement_type
            or sensor.measurement_name
            or "measurement"
        )
        self.entity_id = json_entity_id("sensor", f"{device.id}_{measurement_type}")
        if field_key:
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
                        if not math.isfinite(value):
                            return None
                        if self._temperature_unit:
                            source_unit = _json_temperature_unit(sensor)
                            if not source_unit:
                                return None
                            if source_unit != self._temperature_unit:
                                value = round(
                                    TemperatureConverter.convert(
                                        value, source_unit, self._temperature_unit
                                    ),
                                    2,
                                )
                        return value if math.isfinite(value) else None
        return None

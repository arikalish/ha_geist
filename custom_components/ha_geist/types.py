"""
Types for ha_geist parsed XML data.

Defines TypedDict and dataclass representations for fields and devices
extracted from the `data.xml` responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict


class ParsedData(TypedDict):
    devices: list[Device]
    raw: str
    alarms: list[Alarm] | None


@dataclass
class Alarm:
    """A device-reported alarm and its current state."""

    id: str
    name: str
    state: str
    device_id: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    trigger_id: str | None = None
    latching: bool = False


def unique_alarm_ids(alarms: list[Alarm]) -> list[Alarm]:
    """Reject duplicate normalized identities."""
    used: set[str] = set()
    result: list[Alarm] = []
    for alarm in alarms:
        if alarm.id in used:
            msg = f"Duplicate alarm identity {alarm.id}"
            raise ValueError(msg)
        used.add(alarm.id)
        result.append(alarm)
    return result


@dataclass
class Field:
    key: str
    value: str | None
    niceName: str | None


@dataclass
class JsonSensor:
    """Sensor value from a JSON API device."""

    key: str
    name: str
    value: Any
    unit: str | None
    measurement_type: str | None = None
    measurement_name: str | None = None


def is_digital_measurement(sensor: JsonSensor) -> bool:
    """Return whether a JSON measurement is a CCAT digital input."""
    return (sensor.measurement_type or "").lower() == "ccatdigital"


@dataclass
class JsonRelay:
    """Relay state from a JSON API device."""

    index: str
    name: str
    state: str
    on_label: str
    off_label: str
    mode: str | None = None


@dataclass
class JsonDevice:
    """Device and entities returned by the JSON API."""

    id: str
    name: str
    model: str
    sensors: list[JsonSensor] = field(default_factory=list)
    relays: list[JsonRelay] = field(default_factory=list)
    serial_number: str | None = None
    via_device_id: str | None = None


@dataclass
class JsonParsedData:
    """Normalized data from the JSON API."""

    devices: list[JsonDevice]
    raw: dict[str, Any]
    alarms: list[Alarm] | None = None


def current_alarms(data: object) -> list[Alarm] | None:
    """Return current alarm data, distinguishing missing data from an empty list."""
    if isinstance(data, JsonParsedData):
        return data.alarms
    if isinstance(data, dict):
        return data.get("alarms")
    return None


def find_alarm(data: object, alarm_id: str) -> Alarm | None:
    """Find an alarm in the latest coordinator data."""
    alarms = current_alarms(data)
    if alarms is not None:
        return next((alarm for alarm in alarms if alarm.id == alarm_id), None)
    return None


@dataclass
class Device:
    id: str
    name: str
    type: str | None = None
    available: str | None = None
    index: str | None = None
    manufacturer: str | None = None
    sw_version: str | None = None
    serial_number: str | None = None
    via_device_id: str | None = None
    fields: dict[str, Field] = field(default_factory=dict)

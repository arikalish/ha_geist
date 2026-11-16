"""Types for ha_geist parsed XML data.

Defines TypedDict and dataclass representations for fields and devices
extracted from the `data.xml` responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict


class ParsedData(TypedDict):
    devices: list[Device]
    raw: str


@dataclass
class Field:
    key: str
    value: Optional[str]
    niceName: Optional[str]


@dataclass
class JsonSensor:
    """Sensor value from a JSON API device."""

    key: str
    name: str
    value: Any
    unit: Optional[str]


@dataclass
class JsonRelay:
    """Relay state from a JSON API device."""

    index: str
    name: str
    state: str
    on_label: str
    off_label: str


@dataclass
class JsonDevice:
    """Device and entities returned by the JSON API."""

    id: str
    name: str
    model: str
    sensors: list[JsonSensor] = field(default_factory=list)
    relays: list[JsonRelay] = field(default_factory=list)


@dataclass
class JsonParsedData:
    """Normalized data from the JSON API."""

    devices: list[JsonDevice]
    raw: dict[str, Any]


@dataclass
class Device:
    id: str
    name: str
    type: Optional[str] = None
    available: Optional[str] = None
    index: Optional[str] = None
    manufacturer: Optional[str] = None
    sw_version: Optional[str] = None
    serial_number: Optional[str] = None
    fields: dict[str, Field] = field(default_factory=dict)

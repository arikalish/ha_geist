"""Types for ha_geist parsed XML data.

Defines TypedDict and dataclass representations for fields and devices
extracted from the `data.xml` responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TypedDict


class ParsedData(TypedDict):
    devices: list[Device]
    raw: str


@dataclass
class Field:
    key: str
    value: Optional[str]
    niceName: Optional[str]


@dataclass
class Device:
    id: str
    name: str
    type: Optional[str] = None
    available: Optional[str] = None
    index: Optional[str] = None
    fields: dict[str, Field] = field(default_factory=dict)

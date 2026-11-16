"""Custom types for ha_geist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import IntegrationBlueprintApiClient
    from .api_json import GeistJsonApiClient
    from .coordinator import BlueprintDataUpdateCoordinator


type IntegrationBlueprintConfigEntry = ConfigEntry[IntegrationBlueprintData]


@dataclass
class IntegrationBlueprintData:
    """Data for the Blueprint integration."""

    client: IntegrationBlueprintApiClient | GeistJsonApiClient
    coordinator: BlueprintDataUpdateCoordinator
    integration: Integration

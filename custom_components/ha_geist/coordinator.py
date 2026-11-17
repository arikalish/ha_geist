"""DataUpdateCoordinator for ha_geist."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_json import GeistJsonApiClient
from .const import DOMAIN, LOGGER
from .exceptions import (
    IntegrationBlueprintApiClientAuthenticationError,
    IntegrationBlueprintApiClientError,
)
from .types import JsonParsedData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IntegrationBlueprintConfigEntry
    from .types import ParsedData


def _update_xml_firmware(
    hass: HomeAssistant, config_entry_id: str, data: ParsedData
) -> None:
    """Keep host firmware current and remove stale firmware from attached devices."""
    registry = dr.async_get(hass)
    for device in data["devices"]:
        if device.index is None or not device.id:
            continue
        entry = registry.async_get_device(identifiers={(DOMAIN, device.id)})
        if not entry:
            continue
        if device.index == "0":
            if device.sw_version and entry.sw_version != device.sw_version:
                registry.async_update_device(entry.id, sw_version=device.sw_version)
        elif entry.sw_version is not None and entry.config_entries == {config_entry_id}:
            registry.async_update_device(entry.id, sw_version=None)


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class BlueprintDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: IntegrationBlueprintConfigEntry

    async def _async_update_data(self) -> ParsedData | JsonParsedData:
        """Update data via library."""
        try:
            data = await self.config_entry.runtime_data.client.async_get_data()
        except IntegrationBlueprintApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except IntegrationBlueprintApiClientError as exception:
            raise UpdateFailed(exception) from exception

        if isinstance(data, JsonParsedData):
            client = self.config_entry.runtime_data.client
            if isinstance(client, GeistJsonApiClient):
                try:
                    data.alarms = await client.async_get_alarms(data.devices)
                except IntegrationBlueprintApiClientError as exception:
                    LOGGER.warning("Unable to retrieve device alarms: %s", exception)
            registry = dr.async_get(self.hass)
            for device in data.devices:
                if not device.serial_number:
                    continue
                entry = registry.async_get_device(identifiers={(DOMAIN, device.id)})
                if entry and entry.serial_number != device.serial_number:
                    registry.async_update_device(
                        entry.id, serial_number=device.serial_number
                    )
        else:
            _update_xml_firmware(self.hass, self.config_entry.entry_id, data)
        return data

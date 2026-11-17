"""
Custom integration to integrate ha_geist with Home Assistant.

For more details about this integration, please refer to
https://github.com/arikalish/ha_geist
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .api_json import GeistJsonApiClient
from .api_xml import IntegrationBlueprintApiClient
from .const import API_TYPE_JSON, API_TYPE_XML, CONF_API_TYPE, DOMAIN, LOGGER
from .coordinator import BlueprintDataUpdateCoordinator
from .data import IntegrationBlueprintData
from .entity import json_entity_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import IntegrationBlueprintConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_migrate_entry(
    hass: HomeAssistant, entry: IntegrationBlueprintConfigEntry
) -> bool:
    """Assign device-specific entity IDs to existing JSON entities once."""
    if entry.version != 1:
        return False
    if entry.data.get(CONF_API_TYPE) == API_TYPE_JSON:
        registry = er.async_get(hass)
        alarm_prefix = f"{entry.entry_id}_alarm_"
        for existing in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                existing.platform != DOMAIN
                or existing.domain not in {"sensor", "binary_sensor", "switch"}
                or existing.unique_id.startswith(alarm_prefix)
            ):
                continue
            desired_id = json_entity_id(existing.domain, existing.unique_id)
            suffix = existing.entity_id.removeprefix(f"{desired_id}_")
            if existing.entity_id == desired_id or (
                suffix != existing.entity_id and suffix.isdecimal()
            ):
                continue
            entity_id = registry.async_generate_entity_id(
                existing.domain, existing.unique_id
            )
            registry.async_update_entity(existing.entity_id, new_entity_id=entity_id)
    hass.config_entries.async_update_entry(entry, version=2)
    return True


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    coordinator = BlueprintDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        update_interval=timedelta(seconds=30),
    )
    api_type = entry.data.get(CONF_API_TYPE, API_TYPE_XML)
    client_type = (
        GeistJsonApiClient
        if api_type == API_TYPE_JSON
        else IntegrationBlueprintApiClient
    )
    entry.runtime_data = IntegrationBlueprintData(
        client=client_type(
            username=entry.data.get(CONF_USERNAME),
            password=entry.data.get(CONF_PASSWORD),
            session=async_get_clientsession(hass),
            base_url=entry.data.get(CONF_URL),
        ),
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
    )

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)

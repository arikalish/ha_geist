"""Switch platform for relays exposed by the Geist JSON API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api_json import GeistJsonApiClient
from .const import API_TYPE_JSON, CONF_API_TYPE, DOMAIN
from .coordinator import BlueprintDataUpdateCoordinator
from .types import JsonDevice, JsonParsedData, JsonRelay

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import IntegrationBlueprintConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up relay switches from the JSON API."""
    if entry.data.get(CONF_API_TYPE) != API_TYPE_JSON:
        return
    data = entry.runtime_data.coordinator.data
    client = entry.runtime_data.client
    if not isinstance(data, JsonParsedData) or not isinstance(
        client, GeistJsonApiClient
    ):
        message = "The JSON API returned unexpected relay data"
        raise HomeAssistantError(message)
    if not client.can_control:
        return
    async_add_entities(
        [
            GeistRelaySwitch(entry.runtime_data.coordinator, device, relay)
            for device in data.devices
            for relay in device.relays
        ]
    )


class GeistRelaySwitch(CoordinatorEntity[BlueprintDataUpdateCoordinator], SwitchEntity):
    """Control one relay from a Geist JSON API device."""

    _attr_icon = "mdi:electric-switch"

    def __init__(
        self,
        coordinator: BlueprintDataUpdateCoordinator,
        device: JsonDevice,
        relay: JsonRelay,
    ) -> None:
        """Initialize the relay switch."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._relay_index = relay.index
        self._attr_name = relay.name
        self._attr_unique_id = f"{device.id}_relay_{relay.index}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Geist",
            model=device.model or None,
        )
        if device.serial_number:
            self._attr_device_info["serial_number"] = device.serial_number

    @property
    def is_on(self) -> bool:
        """Return the latest relay state."""
        return self._relay_state(self._relay()) is True

    @property
    def available(self) -> bool:
        """Return whether the relay supports manual control."""
        relay = self._relay()
        return (
            super().available
            and relay is not None
            and (relay.mode or "").lower() != "alarm"
            and self._relay_state(relay) is not None
        )

    def _relay(self) -> JsonRelay | None:
        """Return the latest data for this relay."""
        data = self.coordinator.data
        if not isinstance(data, JsonParsedData):
            return None
        for device in data.devices:
            if device.id == self._device_id:
                for relay in device.relays:
                    if relay.index == self._relay_index:
                        return relay
        return None

    @staticmethod
    def _relay_state(relay: JsonRelay | None) -> bool | None:
        """Return the known state of a relay."""
        if relay is not None:
            state = relay.state.lower()
            if state in {"on", "on2off", "latchedon", "rebootoff"}:
                return True
            if state in {"off", "off2on", "latchedoff", "rebooton"}:
                return False
        return None

    async def async_turn_on(self, **kwargs: object) -> None:  # noqa: ARG002
        """Energize the relay."""
        await self._set_state("on")

    async def async_turn_off(self, **kwargs: object) -> None:  # noqa: ARG002
        """De-energize the relay."""
        await self._set_state("off")

    async def _set_state(self, state: str) -> None:
        """Send a relay command and refresh the coordinator."""
        relay = self._relay()
        if relay and (relay.mode or "").lower() == "alarm":
            message = "Cannot control a relay in alarm mode"
            raise HomeAssistantError(message)
        client = self.coordinator.config_entry.runtime_data.client
        if not isinstance(client, GeistJsonApiClient):
            message = "The configured device does not support relay control"
            raise TypeError(message)
        await client.async_set_relay_state(self._device_id, self._relay_index, state)
        await self.coordinator.async_request_refresh()

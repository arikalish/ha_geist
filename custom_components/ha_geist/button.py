"""Buttons for acknowledging Geist JSON alarms."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .api_json import GeistJsonApiClient
from .const import API_TYPE_JSON, CONF_API_TYPE, DOMAIN
from .coordinator import BlueprintDataUpdateCoordinator
from .types import Alarm, JsonParsedData, find_alarm

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import IntegrationBlueprintConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationBlueprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up acknowledgement buttons for configured alarms."""
    if entry.data.get(CONF_API_TYPE) != API_TYPE_JSON:
        return
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    if not isinstance(coordinator.data, JsonParsedData) or not isinstance(
        client, GeistJsonApiClient
    ):
        msg = "The JSON API returned unexpected alarm data"
        raise HomeAssistantError(msg)
    if not client.can_control:
        return
    known_ids: set[str] = set()

    @callback
    def update_buttons() -> None:
        data = coordinator.data
        if not isinstance(data, JsonParsedData) or data.alarms is None:
            return
        alarms = [alarm for alarm in data.alarms if alarm.trigger_id]
        current_ids = {alarm.id for alarm in alarms}
        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_alarm_ack_"
        for existing in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                existing.domain == "button"
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
                    GeistAlarmAcknowledgeButton(coordinator, entry.entry_id, alarm)
                    for alarm in new_alarms
                ]
            )

    update_buttons()
    entry.async_on_unload(coordinator.async_add_listener(update_buttons))


class GeistAlarmAcknowledgeButton(
    CoordinatorEntity[BlueprintDataUpdateCoordinator], ButtonEntity
):
    """Acknowledge a single alarm trigger."""

    _attr_icon = "mdi:bell-check"

    def __init__(
        self, coordinator: BlueprintDataUpdateCoordinator, entry_id: str, alarm: Alarm
    ) -> None:
        """Initialize the acknowledgement button."""
        super().__init__(coordinator)
        self._alarm_id = alarm.id
        self._attr_unique_id = f"{entry_id}_alarm_ack_{alarm.id}"
        self._attr_name = f"Acknowledge {alarm.name}"
        self.entity_id = f"button.{slugify(alarm.id)}_acknowledge"
        if alarm.device_id:
            self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, alarm.device_id)})

    @property
    def available(self) -> bool:
        """Allow acknowledgement only for an active alarm."""
        alarm = find_alarm(self.coordinator.data, self._alarm_id)
        client = self.coordinator.config_entry.runtime_data.client
        return (
            super().available
            and isinstance(client, GeistJsonApiClient)
            and client.can_control
            and alarm is not None
            and bool(alarm.trigger_id)
            and alarm.state.lower() in {"tripped", "latched"}
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Keep the label current when the alarm threshold changes."""
        alarm = find_alarm(self.coordinator.data, self._alarm_id)
        if alarm and (name := f"Acknowledge {alarm.name}") != self._attr_name:
            self._attr_name = name
            if self.registry_entry:
                self.registry_entry = er.async_get(self.hass).async_update_entity(
                    self.entity_id, original_name=name
                )
        super()._handle_coordinator_update()

    async def async_press(self) -> None:
        """Acknowledge the alarm and refresh its reported state."""
        alarm = find_alarm(self.coordinator.data, self._alarm_id)
        client = self.coordinator.config_entry.runtime_data.client
        if (
            not self.available
            or alarm is None
            or not alarm.trigger_id
            or not isinstance(client, GeistJsonApiClient)
        ):
            msg = "This alarm cannot be acknowledged"
            raise HomeAssistantError(msg)
        await client.async_acknowledge_alarm(alarm.trigger_id)
        await self.coordinator.async_request_refresh()

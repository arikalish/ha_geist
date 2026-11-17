"""Tests for the Geist XML and JSON API clients."""

# unittest assertions are used to keep these async tests independent of pytest fixtures.
# ruff: noqa: PT009, PT027

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.ha_geist import async_migrate_entry
from custom_components.ha_geist.api_json import GeistJsonApiClient, _parse_devices
from custom_components.ha_geist.api_xml import IntegrationBlueprintApiClient
from custom_components.ha_geist.binary_sensor import (
    GeistDigitalInput,
)
from custom_components.ha_geist.binary_sensor import (
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.ha_geist.button import GeistAlarmAcknowledgeButton
from custom_components.ha_geist.button import async_setup_entry as async_setup_buttons
from custom_components.ha_geist.const import API_TYPE_JSON, CONF_API_TYPE
from custom_components.ha_geist.coordinator import BlueprintDataUpdateCoordinator
from custom_components.ha_geist.exceptions import (
    IntegrationBlueprintApiClientAuthenticationError,
    IntegrationBlueprintApiClientError,
)
from custom_components.ha_geist.sensor import (
    AlarmSensor,
    FieldSensor,
    JsonSensorEntity,
    _setup_alarm_sensors,
)
from custom_components.ha_geist.sensor import (
    async_setup_entry as async_setup_sensors,
)
from custom_components.ha_geist.switch import GeistRelaySwitch
from custom_components.ha_geist.types import Alarm, Device

EXPECTED_READ_ONLY_REQUESTS = 3


class FakeResponse:
    """Minimal aiohttp response for client tests."""

    def __init__(
        self,
        *,
        status: int = 200,
        json_data: dict[str, Any] | None = None,
        text_data: str = "",
    ) -> None:
        """Create a fake response."""
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    def raise_for_status(self) -> None:
        """Raise for an unsuccessful status."""
        if self.status >= HTTPStatus.BAD_REQUEST:
            message = f"unexpected HTTP status: {self.status}"
            raise RuntimeError(message)

    async def json(self, **kwargs: Any) -> dict[str, Any]:
        """Return the configured JSON response."""
        if kwargs.get("content_type") not in (None, "application/json"):
            message = "unexpected content type"
            raise ValueError(message)
        if self._json_data is None:
            message = "missing JSON"
            raise json.JSONDecodeError(message, "", 0)
        return self._json_data

    async def text(self) -> str:
        """Return the configured text response."""
        return self._text_data


class FakeRequest:
    """Request object supporting both aiohttp's await and context-manager APIs."""

    def __init__(self, response: FakeResponse) -> None:
        """Create a request wrapper."""
        self.response = response

    def __await__(self) -> Any:
        """Allow the request to be awaited like aiohttp's request wrapper."""

        async def resolve() -> FakeResponse:
            return self.response

        return resolve().__await__()

    async def __aenter__(self) -> FakeResponse:
        """Enter the async response context."""
        return self.response

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit the async response context."""
        return


class FakeSession:
    """Record requests and return a configured response sequence."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        """Create a session with queued responses."""
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> FakeRequest:
        """Record a request and return the next response."""
        self.requests.append(kwargs)
        return FakeRequest(next(self.responses))


def api_response(data: Any = None, *, ret_code: int = 0) -> FakeResponse:
    """Create an API response."""
    return FakeResponse(json_data={"retCode": ret_code, "retMsg": "", "data": data})


def login_response(
    session_key: str = "sample-session-id", *, control: str = "true"
) -> FakeResponse:
    """Create a successful login response."""
    return api_response({"token": session_key, "control": control, "admin": "false"})


def system_response(serial: str = "controller-serial") -> FakeResponse:
    """Create public controller metadata."""
    return api_response({"model": "BB-REL-THA4", "serialNumber": serial})


class TestApiClients(unittest.TestCase):
    """Validate the XML and JSON API clients."""

    def test_json_entities_use_device_specific_entity_ids(self) -> None:
        """Identical measurements and relays on different devices have distinct IDs."""
        data = _parse_devices(
            {
                device_id: {
                    "entity": {
                        "0": {
                            "measurement": {
                                "0": {"type": "temperature", "value": "20"},
                                "1": {"type": "ccatDigital", "value": "1"},
                            }
                        }
                    },
                    "analog": {"0": {"label": "Analog 1", "value": "1"}},
                    "relay": {"0": {"label": "Relay 1", "state": "off"}},
                }
                for device_id in ("device-1", "device-2")
            }
        )
        coordinator = SimpleNamespace(
            data=data,
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
        )
        entities = [
            entity
            for device in data.devices
            for entity in (
                JsonSensorEntity(coordinator, device, device.sensors[0]),
                GeistDigitalInput(coordinator, device, device.sensors[1]),
                JsonSensorEntity(coordinator, device, device.sensors[2]),
                GeistRelaySwitch(coordinator, device, device.relays[0]),
            )
        ]
        self.assertEqual(
            [entity.entity_id for entity in entities],
            [
                "sensor.device_1_temperature",
                "binary_sensor.device_1_digital",
                "sensor.device_1_analog",
                "switch.device_1_relay_0",
                "sensor.device_2_temperature",
                "binary_sensor.device_2_digital",
                "sensor.device_2_analog",
                "switch.device_2_relay_0",
            ],
        )

    def test_json_entity_id_migration_preserves_registry_settings(self) -> None:
        """Rename existing entries once, retaining unique IDs and custom settings."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    version=1,
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_new_entities=False,
                    pref_disable_polling=False,
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                registry = er.async_get(hass)
                first = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device-1_analog_0",
                    config_entry=entry,
                    suggested_object_id="analog_1",
                )
                registry.async_update_entity(first.entity_id, name="Custom analog")
                second = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device-2_analog_0",
                    config_entry=entry,
                    suggested_object_id="analog_1",
                )
                digital = registry.async_get_or_create(
                    "binary_sensor",
                    "ha_geist",
                    "device-1_entity_0_1",
                    config_entry=entry,
                    disabled_by=er.RegistryEntryDisabler.USER,
                )
                relay = registry.async_get_or_create(
                    "switch",
                    "ha_geist",
                    "device-1_relay_0",
                    config_entry=entry,
                )
                alarm = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "entry_alarm_device_1_analog_0_high_0",
                    config_entry=entry,
                )
                conflict = registry.async_get_or_create(
                    "sensor",
                    "other",
                    "other-unique",
                    suggested_object_id="device_1_analog_0",
                )
                self.assertTrue(await async_migrate_entry(hass, entry))  # type: ignore[arg-type]
                migrated_id = registry.async_get_entity_id(
                    "sensor", "ha_geist", "device-1_analog_0"
                )
                self.assertEqual(migrated_id, "sensor.device_1_analog_0_2")
                migrated = registry.async_get(migrated_id)
                assert migrated is not None
                self.assertEqual(migrated.name, "Custom analog")
                self.assertEqual(
                    registry.async_get_entity_id(
                        "sensor", "ha_geist", second.unique_id
                    ),
                    "sensor.device_2_analog_0",
                )
                digital_id = registry.async_get_entity_id(
                    "binary_sensor", "ha_geist", digital.unique_id
                )
                self.assertEqual(digital_id, "binary_sensor.device_1_entity_0_1")
                self.assertEqual(
                    registry.async_get(digital_id).disabled_by,
                    er.RegistryEntryDisabler.USER,
                )
                self.assertEqual(
                    registry.async_get_entity_id("switch", "ha_geist", relay.unique_id),
                    "switch.device_1_relay_0",
                )
                self.assertIsNotNone(registry.async_get(alarm.entity_id))
                self.assertIsNotNone(registry.async_get(conflict.entity_id))
                hass.config_entries.async_update_entry.assert_called_with(
                    entry, version=2
                )
                self.assertTrue(await async_migrate_entry(hass, entry))  # type: ignore[arg-type]
                self.assertEqual(
                    registry.async_get_entity_id(
                        "sensor", "ha_geist", "device-1_analog_0"
                    ),
                    migrated_id,
                )
                await hass.async_block_till_done()
                data = _parse_devices(
                    {"device-2": {"analog": {"0": {"label": "Analog 1", "value": "3"}}}}
                )
                coordinator = SimpleNamespace(
                    data=data,
                    config_entry=entry,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                )
                sensor = JsonSensorEntity(
                    coordinator,
                    data.devices[0],
                    data.devices[0].sensors[0],  # type: ignore[arg-type]
                )
                sensor._attr_device_info = None  # noqa: SLF001
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry
                await platform.async_add_entities([sensor])
                state = hass.states.get("sensor.device_2_analog_0")
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.state, "3.0")
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_xml_entry_migration_keeps_entity_ids(self) -> None:
        """The version upgrade does not rename XML entities."""
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=MagicMock())
        )
        entry = SimpleNamespace(version=1, data={}, entry_id="xml")
        with patch("custom_components.ha_geist.er.async_get") as get_registry:
            self.assertTrue(asyncio.run(async_migrate_entry(hass, entry)))  # type: ignore[arg-type]
            get_registry.assert_not_called()
        hass.config_entries.async_update_entry.assert_called_once_with(entry, version=2)

    def test_xml_api_parses_example_data(self) -> None:
        """Legacy data.xml parsing still exposes device fields."""
        xml = (Path(__file__).parents[1] / "examples" / "data-rsm.xml").read_text()
        session = FakeSession([FakeResponse(text_data=xml)])
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=session,  # type: ignore[arg-type]
            base_url="http://geist.local",
        )

        result = asyncio.run(client.async_get_data())

        self.assertEqual(result["devices"][0].fields["TempC"].value, "25.00")
        self.assertEqual(result["devices"][0].manufacturer, "Geist")
        self.assertEqual(result["devices"][0].sw_version, "3.16.3")
        self.assertIsNone(result["devices"][1].sw_version)
        self.assertEqual(result["devices"][0].serial_number, "01D9BCD512000084")
        self.assertEqual(result["devices"][1].serial_number, "EA00000132301A28")
        self.assertEqual(session.requests[0]["url"], "http://geist.local/data.xml")

        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            data=result,
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            entry_id="entry",
            data={},
            runtime_data=SimpleNamespace(coordinator=coordinator),
            async_on_unload=MagicMock(),
        )
        add_entities = MagicMock()
        with (
            patch("custom_components.ha_geist.sensor.er.async_get"),
            patch(
                "custom_components.ha_geist.sensor.er.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            asyncio.run(async_setup_sensors(None, entry, add_entities))  # type: ignore[arg-type]
        entities = add_entities.call_args.args[0]
        versions = {
            entity.device_info["name"]: entity.device_info["sw_version"]
            for entity in entities
        }
        self.assertEqual(versions["Entertainment Center RSM"], "3.16.3")
        self.assertIsNone(versions["Temp Sensor"])

    def test_xml_debug_log_includes_host_and_friendly_name(self) -> None:
        """Log the configured connection host and XML server name."""
        xml = '<server host="Office Monitor"><devices /></server>'
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
            base_url="http://url-user:url-password@geist.local/data.xml",
        )
        with self.assertLogs("custom_components.ha_geist", level="DEBUG") as logs:
            asyncio.run(client.async_get_data())
        self.assertEqual(
            logs.records[0].getMessage(),
            "Parsed data.xml: server_host=geist.local, server_name=Office Monitor, devices=0",
        )
        self.assertNotIn("url-password", "\n".join(logs.output))

    def test_xml_alarms_are_read_from_device_data(self) -> None:
        """Only configured XML alarms are exposed."""
        xml = (Path(__file__).parents[1] / "examples" / "data-em.xml").read_text()
        xml = xml.replace(
            'field="TOTAL" em-trip="0" em-warn="0" enabled="0" status="clear"',
            'field="TOTAL" em-trip="2" em-warn="0" enabled="2" status="clear"',
        )
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
            base_url="http://geist.local",
        )
        data = asyncio.run(client.async_get_data())
        alarms = data["alarms"]
        self.assertIsNotNone(alarms)
        assert alarms is not None
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].name, "Amps Total Alarm (warning 0, alarm 2)")
        self.assertEqual(alarms[0].state, "clear")
        self.assertEqual(alarms[0].device_id, "EM4000198500585D")
        self.assertEqual(alarms[0].attributes["enabled"], "2")
        self.assertEqual(alarms[0].id, "em4000198500585d_total_0")

    def test_xml_alarms_with_same_field_keep_separate_ids(self) -> None:
        """RSM alarm numbers distinguish triggers for the same measurement."""
        xml = """<server><devices>
            <device id="host" index="0" name="Host">
              <field key="TempF" value="70" niceName="Temperature (F)"/>
            </device>
            </devices><alarms>
              <alarm alarm-num="0" device-id="host" field="TempF" limtype="High"
                     limit="50" status="Tripped" email="private"/>
              <alarm alarm-num="1" device-id="host" field="TempF" limtype="Low"
                     limit="0" status="Untripped"/>
              <alarm alarm-num="2" device-id="host" field="TempF" limtype="High"
                     limit="60" status="Untripped"/>
            </alarms></server>"""
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
            base_url="http://geist.local",
        )
        alarms = asyncio.run(client.async_get_data())["alarms"]
        assert alarms is not None
        self.assertEqual(
            [alarm.id for alarm in alarms],
            ["host_tempf_high_0", "host_tempf_low_1", "host_tempf_high_2"],
        )
        self.assertEqual(
            [alarm.state for alarm in alarms], ["tripped", "clear", "clear"]
        )
        self.assertEqual(alarms[0].name, "Temperature (F) High Alarm (limit 50)")
        self.assertEqual(alarms[2].name, "Temperature (F) High Alarm (limit 60)")
        self.assertEqual(alarms[0].attributes["reported_status"], "Tripped")
        self.assertNotIn("email", alarms[0].attributes)

    def test_xml_alarm_identity_tracks_field_and_limit_not_slot(self) -> None:
        """A different target or slot creates a distinct alarm identity."""

        def parse(field: str, limit_type: str, slot: str) -> Alarm:
            xml = f"""<server><devices>
                <device id="host-1" index="0" name="Host">
                  <field key="{field}" value="1" niceName="{field}"/>
                </device>
                </devices><alarms>
                  <alarm alarm-num="{slot}" device-id="host-1" field="{field}"
                         limtype="{limit_type}" status="Untripped"/>
                </alarms></server>"""
            client = IntegrationBlueprintApiClient(
                username=None,
                password=None,
                session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
                base_url="http://geist.local",
            )
            alarms = asyncio.run(client.async_get_data())["alarms"]
            assert alarms is not None
            return alarms[0]

        temperature = parse("TempC", "Low", "3")
        light = parse("Light", "Low", "3")
        moved = parse("Light", "Low", "8")
        self.assertEqual(temperature.id, "host_1_tempc_low_3")
        self.assertEqual(light.id, "host_1_light_low_3")
        self.assertEqual(moved.id, "host_1_light_low_8")

    def test_xml_invalid_alarm_does_not_return_incomplete_alarm_list(self) -> None:
        """Malformed alarm entries cannot be mistaken for removed alarms."""
        xml = """<server><devices><device id="host" /></devices>
            <alarms><alarm alarm-num="1" device-id="host" status="clear" /></alarms>
            </server>"""
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
            base_url="http://geist.local",
        )
        with self.assertRaises(IntegrationBlueprintApiClientError):
            asyncio.run(client.async_get_data())

    def test_xml_duplicate_alarm_identity_rejects_poll(self) -> None:
        """Duplicate IDs cannot cause a configured alarm to be silently removed."""
        xml = """<server><devices><device id="host" /></devices><alarms>
            <alarm alarm-num="1" device-id="host" field="Light"
                   limtype="Low" status="clear"/>
            <alarm alarm-num="1" device-id="host" field="Light"
                   limtype="Low" status="clear"/>
            </alarms></server>"""
        client = IntegrationBlueprintApiClient(
            username=None,
            password=None,
            session=FakeSession([FakeResponse(text_data=xml)]),  # type: ignore[arg-type]
            base_url="http://geist.local",
        )
        with self.assertRaises(IntegrationBlueprintApiClientError):
            asyncio.run(client.async_get_data())

    def test_json_alarm_states_and_target_are_preserved(self) -> None:
        """JSON triggers retain exact states and identify their target device."""
        device = {
            "device-1": {
                "type": "BB-REL-THA4",
                "analog": {"0": {"label": "Analog 1", "value": "0"}},
            }
        }
        trigger = {
            "0": {
                "state": "latched",
                "path": "device-1/analog/0",
                "type": "high",
                "severity": "alarm",
                "threshold": "50",
                "latching": True,
            },
            "1": {
                "state": "clear",
                "path": "device-1/analog/0",
                "type": "high",
            },
        }
        session = FakeSession(
            [
                login_response(),
                api_response(device),
                system_response(),
                api_response({"trigger": trigger}),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            data = await client.async_get_data()
            alarms = await client.async_get_alarms(data.devices)
            self.assertEqual(len(alarms), 2)
            self.assertEqual(alarms[0].name, "Analog 1 High Alarm (limit 50)")
            self.assertEqual(alarms[0].id, "device_1_analog_0_high_0")
            self.assertEqual(alarms[0].state, "latched")
            self.assertEqual(alarms[0].device_id, "device-1")
            self.assertEqual(alarms[0].attributes["threshold"], "50")
            self.assertTrue(alarms[0].attributes["latching"])
            self.assertTrue(alarms[0].latching)
            self.assertEqual(alarms[0].trigger_id, "0")
            self.assertEqual(alarms[1].id, "device_1_analog_0_high_1")
            self.assertEqual(alarms[1].name, "Analog 1 High Alarm")

        asyncio.run(run())
        self.assertEqual(session.requests[3]["url"], "http://geist.local/api/alarm")
        self.assertEqual(session.requests[3]["json"]["cmd"], "get")

    def test_json_alarm_acknowledgement_uses_trigger_and_retries_session(self) -> None:
        """Acknowledge the specified trigger with the normal token retry."""
        session = FakeSession(
            [
                login_response(),
                api_response({"device": {"type": "other"}}),
                system_response(),
                api_response(ret_code=1002),
                login_response("renewed-session"),
                api_response(),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,
            "http://geist.local",  # type: ignore[arg-type]
        )

        async def run() -> None:
            await client.async_get_data()
            await client.async_acknowledge_alarm("3/4")

        asyncio.run(run())
        self.assertEqual(
            session.requests[3]["url"], "http://geist.local/api/alarm/trigger/3%2F4"
        )
        self.assertEqual(session.requests[3]["json"]["cmd"], "ack")
        self.assertEqual(
            session.requests[5]["json"], {"token": "renewed-session", "cmd": "ack"}
        )

    def test_json_alarm_acknowledgement_requires_permission(self) -> None:
        """Read-only accounts cannot send acknowledgement commands."""
        session = FakeSession(
            [
                login_response(control="false"),
                api_response({"device": {"type": "other"}}),
                system_response(),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,
            "http://geist.local",  # type: ignore[arg-type]
        )

        async def run() -> None:
            await client.async_get_data()
            with self.assertRaises(IntegrationBlueprintApiClientAuthenticationError):
                await client.async_acknowledge_alarm("3")
            with self.assertRaises(ValueError):
                await client.async_acknowledge_alarm("")

        asyncio.run(run())
        self.assertEqual(len(session.requests), EXPECTED_READ_ONLY_REQUESTS)

    def test_invalid_json_alarm_is_not_treated_as_removed(self) -> None:
        """A malformed trigger makes the alarm read fail instead of appearing absent."""
        for trigger in ({"path": "device/analog/0"}, None):
            with self.subTest(trigger=trigger):
                session = FakeSession(
                    [login_response(), api_response({"trigger": {"3": trigger}})]
                )
                client = GeistJsonApiClient(
                    "test-user",
                    "not-a-real-password",
                    session,
                    "http://geist.local",  # type: ignore[arg-type]
                )
                with self.assertRaises(IntegrationBlueprintApiClientError):
                    asyncio.run(client.async_get_alarms([]))

    def test_alarm_sensors_track_new_removed_and_unavailable_alarms(self) -> None:
        """New triggers appear dynamically; missing alarm data is unavailable."""
        data = _parse_devices({"device-1": {"type": "BB-REL-THA4"}})
        data.alarms = []
        coordinator = SimpleNamespace(
            data=data,
            last_update_success=True,
            async_add_listener=MagicMock(return_value=MagicMock()),
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
        )
        entry = SimpleNamespace(
            entry_id="entry",
            runtime_data=SimpleNamespace(coordinator=coordinator),
            async_on_unload=MagicMock(),
        )
        add_entities = MagicMock()
        with (
            patch("custom_components.ha_geist.sensor.er.async_get"),
            patch(
                "custom_components.ha_geist.sensor.er.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            _setup_alarm_sensors(None, entry, add_entities)  # type: ignore[arg-type]
        add_entities.assert_not_called()
        listener = coordinator.async_add_listener.call_args.args[0]
        data.alarms = [
            Alarm(
                "0", "Analog 1 High Alarm", "tripped", "device-1", {"severity": "alarm"}
            )
        ]
        with (
            patch("custom_components.ha_geist.sensor.er.async_get"),
            patch(
                "custom_components.ha_geist.sensor.er.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            listener()
        sensor = add_entities.call_args.args[0][0]
        self.assertIsInstance(sensor, AlarmSensor)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, "tripped")
        self.assertEqual(sensor.extra_state_attributes, {"severity": "alarm"})
        self.assertEqual(sensor.unique_id, "entry_alarm_0")
        self.assertEqual(sensor.device_info["identifiers"], {("ha_geist", "device-1")})
        data.alarms[0].state = "latched"
        with (
            patch("custom_components.ha_geist.sensor.er.async_get"),
            patch(
                "custom_components.ha_geist.sensor.er.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            listener()
        self.assertEqual(add_entities.call_count, 1)
        self.assertEqual(sensor.native_value, "latched")
        data.alarms[0].state = "clear"
        self.assertEqual(sensor.native_value, "clear")
        data.alarms = None
        self.assertFalse(sensor.available)
        data.alarms = []
        self.assertFalse(sensor.available)

    def test_alarm_fetch_failure_does_not_stop_json_polling(self) -> None:
        """Alarm API failures are logged while measurement updates continue."""
        data = _parse_devices({"device-1": {"type": "other"}})
        client = MagicMock(spec=GeistJsonApiClient)
        client.async_get_data = AsyncMock(return_value=data)
        client.async_get_alarms = AsyncMock(
            side_effect=IntegrationBlueprintApiClientError("alarm endpoint failed")
        )
        coordinator = SimpleNamespace(
            hass=object(),
            config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
        )
        registry = SimpleNamespace(async_get_device=MagicMock())
        with (
            patch(
                "custom_components.ha_geist.coordinator.dr.async_get",
                return_value=registry,
            ),
            self.assertLogs("custom_components.ha_geist", level="WARNING"),
        ):
            result = asyncio.run(
                BlueprintDataUpdateCoordinator._async_update_data(coordinator)  # type: ignore[arg-type]
            )
        self.assertIs(result, data)
        self.assertIsNone(data.alarms)

    def test_alarm_acknowledgement_button_lifecycle(self) -> None:
        """Buttons track all triggers and acknowledge only active alarms."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                data = _parse_devices({"device": {"type": "other"}})
                data.alarms = None
                client = MagicMock(spec=GeistJsonApiClient)
                client.can_control = True
                client.async_acknowledge_alarm = AsyncMock()
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_polling=False,
                    pref_disable_new_entities=False,
                    async_on_unload=MagicMock(),
                )
                coordinator = SimpleNamespace(
                    data=data,
                    last_update_success=True,
                    config_entry=entry,
                    async_add_listener=MagicMock(return_value=lambda: None),
                    async_request_refresh=AsyncMock(),
                )
                entry.runtime_data = SimpleNamespace(
                    coordinator=coordinator, client=client
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="button",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry
                tasks: list[asyncio.Task[None]] = []

                def add_entities(entities: list[GeistAlarmAcknowledgeButton]) -> None:
                    tasks.append(
                        hass.async_create_task(platform.async_add_entities(entities))
                    )

                await async_setup_buttons(hass, entry, add_entities)  # type: ignore[arg-type]
                listener = coordinator.async_add_listener.call_args.args[0]
                active = Alarm(
                    "device_analog_0_low_3",
                    "Analog Low Alarm (limit 65)",
                    "tripped",
                    None,
                    trigger_id="3",
                    latching=True,
                )
                data.alarms = [
                    active,
                    Alarm("other", "Other Alarm", "tripped", None, trigger_id="2"),
                ]
                listener()
                await asyncio.gather(*tasks)
                button = platform.entities["button.device_analog_0_low_3_acknowledge"]
                other_button = platform.entities["button.other_acknowledge"]
                self.assertEqual(len(platform.entities), 2)
                self.assertTrue(button.available)
                await button.async_press()
                client.async_acknowledge_alarm.assert_awaited_once_with("3")
                coordinator.async_request_refresh.assert_awaited_once()

                self.assertTrue(other_button.available)
                await other_button.async_press()
                client.async_acknowledge_alarm.assert_awaited_with("2")
                self.assertEqual(coordinator.async_request_refresh.await_count, 2)

                active.state = "latched"
                self.assertTrue(button.available)
                await button.async_press()
                self.assertEqual(client.async_acknowledge_alarm.await_count, 3)

                client.async_acknowledge_alarm.side_effect = (
                    IntegrationBlueprintApiClientError("acknowledgement rejected")
                )
                with self.assertRaises(IntegrationBlueprintApiClientError):
                    await button.async_press()
                self.assertEqual(coordinator.async_request_refresh.await_count, 3)
                client.async_acknowledge_alarm.reset_mock(side_effect=True)

                active.state = "clear"
                self.assertFalse(button.available)
                with self.assertRaises(HomeAssistantError):
                    await button.async_press()
                client.async_acknowledge_alarm.assert_not_awaited()

                other_alarm = data.alarms[1]
                other_alarm.state = "clear"
                self.assertFalse(other_button.available)
                with self.assertRaises(HomeAssistantError):
                    await other_button.async_press()
                client.async_acknowledge_alarm.assert_not_awaited()

                data.alarms = None
                listener()
                registry = er.async_get(hass)
                self.assertIsNotNone(registry.async_get(button.entity_id))
                data.alarms = [active]
                active.latching = False
                listener()
                await hass.async_block_till_done()
                self.assertIsNotNone(registry.async_get(button.entity_id))
                self.assertIn(button.entity_id, platform.entities)
                self.assertIsNone(registry.async_get(other_button.entity_id))
                data.alarms = []
                listener()
                await hass.async_block_till_done()
                self.assertIsNone(registry.async_get(button.entity_id))
                self.assertNotIn(button.entity_id, platform.entities)
                data.alarms = [active]
                active.state = "latched"
                listener()
                await asyncio.gather(*tasks)
                self.assertIn(button.entity_id, platform.entities)
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_alarm_acknowledgement_requires_control_and_attaches_to_device(
        self,
    ) -> None:
        """Only control accounts receive buttons, grouped with the target device."""
        data = _parse_devices({"device-1": {"type": "other"}})
        alarm = Alarm(
            "device_1_analog_0_low_3",
            "Analog Low Alarm (limit 65)",
            "tripped",
            "device-1",
            trigger_id="3",
            latching=True,
        )
        data.alarms = [alarm]
        client = MagicMock(spec=GeistJsonApiClient)
        client.can_control = False
        coordinator = SimpleNamespace(
            data=data,
            last_update_success=True,
            config_entry=SimpleNamespace(
                entry_id="entry",
                runtime_data=SimpleNamespace(client=client),
            ),
        )
        entry = SimpleNamespace(
            data={CONF_API_TYPE: API_TYPE_JSON},
            runtime_data=SimpleNamespace(coordinator=coordinator, client=client),
        )
        add_entities = MagicMock()
        asyncio.run(async_setup_buttons(None, entry, add_entities))  # type: ignore[arg-type]
        add_entities.assert_not_called()
        button = GeistAlarmAcknowledgeButton(coordinator, "entry", alarm)  # type: ignore[arg-type]
        self.assertEqual(button.name, "Acknowledge Analog Low Alarm (limit 65)")
        self.assertEqual(button.device_info["identifiers"], {("ha_geist", "device-1")})
        self.assertFalse(button.available)

    def test_xml_alarm_registry_reconciles_additions_and_removals(self) -> None:
        """Only absent XML alarms are removed, and they can be added again."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                config_entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    pref_disable_polling=False,
                    pref_disable_new_entities=False,
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = config_entry
                registry = er.async_get(hass)
                old = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "entry_alarm_old_slot",
                    config_entry=config_entry,
                )
                unrelated = registry.async_get_or_create(
                    "sensor", "ha_geist", "unrelated_sensor", config_entry=config_entry
                )
                data = {"alarms": []}
                coordinator = SimpleNamespace(
                    data=data,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                    config_entry=config_entry,
                )
                entry = SimpleNamespace(
                    entry_id="entry",
                    runtime_data=SimpleNamespace(coordinator=coordinator),
                    async_on_unload=MagicMock(),
                )
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = config_entry
                tasks: list[asyncio.Task[None]] = []

                def add_entities(entities: list[AlarmSensor]) -> None:
                    tasks.append(
                        hass.async_create_task(platform.async_add_entities(entities))
                    )

                _setup_alarm_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                listener = coordinator.async_add_listener.call_args.args[0]
                self.assertIsNone(registry.async_get(old.entity_id))
                self.assertIsNotNone(registry.async_get(unrelated.entity_id))

                alarm = Alarm("host_light_low_3", "Light Alarm", "clear", None, {})
                data["alarms"] = [alarm]
                listener()
                await asyncio.gather(*tasks)
                await hass.async_block_till_done()
                entity_id = "sensor.host_light_low_3_alarm"
                self.assertEqual(hass.states.get(entity_id).state, "clear")
                self.assertIsNotNone(registry.async_get(entity_id))

                data["alarms"] = None
                listener()
                self.assertIsNotNone(registry.async_get(entity_id))

                data["alarms"] = []
                listener()
                await hass.async_block_till_done()
                self.assertIsNone(registry.async_get(entity_id))
                self.assertIsNone(hass.states.get(entity_id))
                self.assertIsNotNone(registry.async_get(unrelated.entity_id))

                data["alarms"] = [alarm]
                listener()
                await asyncio.gather(*tasks)
                await hass.async_block_till_done()
                self.assertEqual(hass.states.get(entity_id).state, "clear")
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_json_legacy_alarm_cleanup_waits_for_alarm_data(self) -> None:
        """Stale JSON alarm entries are removed only after a successful read."""
        data = _parse_devices({"device": {"type": "other"}})
        coordinator = SimpleNamespace(
            data=data,
            async_add_listener=MagicMock(return_value=lambda: None),
        )
        entry = SimpleNamespace(
            entry_id="entry",
            runtime_data=SimpleNamespace(coordinator=coordinator),
            async_on_unload=MagicMock(),
        )
        registry = SimpleNamespace(async_remove=MagicMock())
        entries = [
            SimpleNamespace(
                domain="sensor",
                platform="ha_geist",
                unique_id="entry_alarm_0",
                entity_id="sensor.old",
            ),
            SimpleNamespace(
                domain="sensor",
                platform="ha_geist",
                unique_id="entry_alarm_device_analog_0_high_0",
                entity_id="sensor.new",
            ),
            SimpleNamespace(
                domain="sensor",
                platform="other",
                unique_id="entry_alarm_1",
                entity_id="sensor.unrelated",
            ),
        ]
        with (
            patch(
                "custom_components.ha_geist.sensor.er.async_get", return_value=registry
            ),
            patch(
                "custom_components.ha_geist.sensor.er.async_entries_for_config_entry",
                return_value=entries,
            ),
        ):
            _setup_alarm_sensors(None, entry, MagicMock())  # type: ignore[arg-type]
            listener = coordinator.async_add_listener.call_args.args[0]
            registry.async_remove.assert_not_called()
            data.alarms = [
                Alarm("device_analog_0_high_0", "Analog Alarm", "clear", None)
            ]
            listener()
            entries.pop(0)
            listener()
            registry.async_remove.assert_called_once_with("sensor.old")

    def test_json_alarm_registry_reconciles_additions_and_removals(self) -> None:
        """Remove only absent alarms after successful reads and recreate them."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    pref_disable_polling=False,
                    pref_disable_new_entities=False,
                    async_on_unload=MagicMock(),
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                registry = er.async_get(hass)
                stale = registry.async_get_or_create(
                    "sensor", "ha_geist", "entry_alarm_old", config_entry=entry
                )
                unrelated = registry.async_get_or_create(
                    "sensor", "ha_geist", "unrelated_sensor", config_entry=entry
                )
                button = registry.async_get_or_create(
                    "button", "ha_geist", "entry_alarm_ack_old", config_entry=entry
                )
                data = _parse_devices({"device": {"type": "other"}})
                data.alarms = None
                coordinator = SimpleNamespace(
                    data=data,
                    last_update_success=True,
                    config_entry=entry,
                    async_add_listener=MagicMock(return_value=lambda: None),
                )
                entry.runtime_data = SimpleNamespace(coordinator=coordinator)
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry
                tasks: list[asyncio.Task[None]] = []

                def add_entities(entities: list[AlarmSensor]) -> None:
                    tasks.append(
                        hass.async_create_task(platform.async_add_entities(entities))
                    )

                _setup_alarm_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                listener = coordinator.async_add_listener.call_args.args[0]
                self.assertIsNotNone(registry.async_get(stale.entity_id))
                first = Alarm("device_temp_high_0", "High alarm", "tripped", None)
                second = Alarm("device_temp_low_1", "Low alarm", "clear", None)
                data.alarms = [first, second]
                listener()
                await asyncio.gather(*tasks)
                await hass.async_block_till_done()
                self.assertIsNone(registry.async_get(stale.entity_id))
                first_id = "sensor.device_temp_high_0_alarm"
                second_id = "sensor.device_temp_low_1_alarm"
                self.assertIsNotNone(hass.states.get(first_id))
                self.assertIsNotNone(hass.states.get(second_id))
                data.alarms = None
                listener()
                self.assertIsNotNone(registry.async_get(first_id))
                data.alarms = [second]
                listener()
                await hass.async_block_till_done()
                self.assertIsNone(registry.async_get(first_id))
                self.assertIsNone(hass.states.get(first_id))
                self.assertIsNotNone(registry.async_get(second_id))
                self.assertIsNotNone(registry.async_get(unrelated.entity_id))
                self.assertIsNotNone(registry.async_get(button.entity_id))
                data.alarms = [first, second]
                listener()
                await asyncio.gather(*tasks)
                await hass.async_block_till_done()
                self.assertEqual(hass.states.get(first_id).state, "tripped")
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_new_alarm_publishes_a_home_assistant_state(self) -> None:
        """A trigger discovered after setup is registered with a live state."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                data = _parse_devices({"device": {"type": "other"}})
                data.alarms = []
                coordinator = SimpleNamespace(
                    data=data,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                    config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
                )
                entry = SimpleNamespace(
                    entry_id="entry",
                    runtime_data=SimpleNamespace(coordinator=coordinator),
                    async_on_unload=MagicMock(),
                )
                tasks: list[asyncio.Task[None]] = []

                def add_entities(entities: list[AlarmSensor]) -> None:
                    tasks.append(
                        hass.async_create_task(platform.async_add_entities(entities))
                    )

                _setup_alarm_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                data.alarms = [
                    Alarm("0", "Temperature Low Alarm (limit 10)", "tripped", None, {})
                ]
                coordinator.async_add_listener.call_args.args[0]()
                await asyncio.gather(*tasks)
                sensor = platform.entities["sensor.0_alarm"]
                state = hass.states.get(sensor.entity_id)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.state, "tripped")
                self.assertEqual(
                    state.attributes["friendly_name"],
                    "Temperature Low Alarm (limit 10)",
                )

                data.alarms = [
                    Alarm("0", "Temperature Low Alarm (limit 20)", "clear", None, {})
                ]
                sensor._handle_coordinator_update()  # noqa: SLF001
                await hass.async_block_till_done()
                state = hass.states.get(sensor.entity_id)
                assert state is not None
                self.assertEqual(state.state, "clear")
                self.assertEqual(
                    state.attributes["friendly_name"],
                    "Temperature Low Alarm (limit 20)",
                )
                registry = er.async_get(hass)
                registry_entry = registry.async_get(sensor.entity_id)
                assert registry_entry is not None
                self.assertEqual(
                    registry_entry.original_name, "Temperature Low Alarm (limit 20)"
                )

                registry.async_update_entity(sensor.entity_id, name="Custom alarm")
                await hass.async_block_till_done()
                data.alarms = [
                    Alarm("0", "Temperature Low Alarm (limit 30)", "tripped", None, {})
                ]
                sensor._handle_coordinator_update()  # noqa: SLF001
                await hass.async_block_till_done()
                state = hass.states.get(sensor.entity_id)
                assert state is not None
                self.assertEqual(state.attributes["friendly_name"], "Custom alarm")
                registry_entry = registry.async_get(sensor.entity_id)
                assert registry_entry is not None
                self.assertEqual(
                    registry_entry.original_name, "Temperature Low Alarm (limit 30)"
                )
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_json_digital_input_reports_binary_state(self) -> None:
        """CCAT digital values map to on/off and invalid values remain unknown."""
        data = _parse_devices(
            {
                "digital-device": {
                    "type": "ccat",
                    "entity": {
                        "0": {
                            "measurement": {"0": {"type": "ccatDigital", "value": "1"}}
                        }
                    },
                }
            }
        )
        coordinator = SimpleNamespace(
            data=data,
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
        )
        measurement = data.devices[0].sensors[0]
        entity = GeistDigitalInput(
            coordinator,
            data.devices[0],
            measurement,  # type: ignore[arg-type]
        )
        self.assertEqual(entity.name, "Digital")
        self.assertEqual(entity.icon, "mdi:dip-switch")
        self.assertEqual(entity.unique_id, "digital-device_entity_0_0")
        self.assertTrue(entity.is_on)
        measurement.value = 0
        self.assertFalse(entity.is_on)
        measurement.value = "0"
        self.assertFalse(entity.is_on)
        measurement.value = "2"
        self.assertIsNone(entity.is_on)
        measurement.value = {}
        self.assertIsNone(entity.is_on)
        measurement.value = None
        self.assertIsNone(entity.is_on)

    def test_digital_setup_only_creates_binary_input(self) -> None:
        """Digital inputs are not also created as numeric sensors."""
        data = _parse_devices(
            {
                "digital-device": {
                    "entity": {
                        "0": {
                            "measurement": {
                                "0": {"type": "ccatDigital", "value": "1"},
                                "1": {"type": "temperature", "value": "20"},
                            }
                        }
                    }
                }
            }
        )
        coordinator = SimpleNamespace(
            data=data,
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            entry_id="entry",
            data={CONF_API_TYPE: API_TYPE_JSON},
            runtime_data=SimpleNamespace(coordinator=coordinator),
            async_on_unload=MagicMock(),
        )

        async def run() -> None:
            binary_entities = MagicMock()
            with patch(
                "custom_components.ha_geist.binary_sensor.er.async_get",
                return_value=SimpleNamespace(
                    async_get_entity_id=MagicMock(return_value=None)
                ),
            ):
                await async_setup_binary_sensors(None, entry, binary_entities)  # type: ignore[arg-type]
            binary = binary_entities.call_args.args[0]
            self.assertEqual(len(binary), 1)
            self.assertIsInstance(binary[0], GeistDigitalInput)

            add_entities = MagicMock()
            with patch(
                "custom_components.ha_geist.sensor.er.async_get",
                return_value=SimpleNamespace(
                    async_get_entity_id=MagicMock(return_value=None)
                ),
            ):
                await async_setup_sensors(object(), entry, add_entities)  # type: ignore[arg-type]
            sensors = add_entities.call_args.args[0]
            self.assertEqual(len(sensors), 1)
            self.assertEqual(sensors[0].name, "Temperature")

        asyncio.run(run())

    def test_json_temperature_units_and_registry_migration(self) -> None:
        """Create both units, migrate old sensors, and use current source units."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                hass.config.units = US_CUSTOMARY_SYSTEM
                await er.async_load(hass)
                await dr.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_new_entities=False,
                    pref_disable_polling=False,
                    async_on_unload=MagicMock(),
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                registry = er.async_get(hass)
                old = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device_entity_0_0",
                    config_entry=entry,
                    suggested_object_id="device_entity_0_0",
                )
                registry.async_update_entity(old.entity_id, name="Custom temperature")
                registry.async_update_entity_options(
                    old.entity_id,
                    "sensor.private",
                    {"suggested_unit_of_measurement": "°F"},
                )
                registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device_entity_0_0_tempf",
                    config_entry=entry,
                    suggested_object_id="device_entity_0_0_tempf",
                )
                humidity = registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device_entity_0_2",
                    config_entry=entry,
                    suggested_object_id="device_entity_0_2_humidity",
                )
                registry.async_get_or_create(
                    "sensor",
                    "ha_geist",
                    "device_entity_0_1",
                    config_entry=entry,
                    suggested_object_id="custom_dewpoint",
                )
                data = _parse_devices(
                    {
                        "device": {
                            "entity": {
                                "0": {
                                    "measurement": {
                                        "0": {
                                            "type": "temperature",
                                            "value": "20",
                                            "units": "C",
                                        },
                                        "1": {
                                            "type": "dewpoint",
                                            "value": "32",
                                            "units": "F",
                                        },
                                        "2": {"type": "humidity", "value": "50"},
                                    }
                                }
                            }
                        }
                    }
                )
                coordinator = SimpleNamespace(
                    data=data,
                    config_entry=entry,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                )
                entry.runtime_data = SimpleNamespace(coordinator=coordinator)
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry
                tasks: list[asyncio.Task[None]] = []

                def add_entities(entities: list[JsonSensorEntity]) -> None:
                    tasks.append(
                        hass.async_create_task(platform.async_add_entities(entities))
                    )

                await async_setup_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                await asyncio.gather(*tasks)
                await hass.async_block_till_done()
                self.assertEqual(len(platform.entities), 5)
                temp_c = platform.entities["sensor.device_tempc"]
                temp_f = platform.entities["sensor.device_tempf"]
                dew_c = platform.entities["sensor.device_dewptc"]
                dew_f = platform.entities["sensor.custom_dewpoint"]
                self.assertEqual(
                    [temp_c.name, temp_f.name], ["Temperature (C)", "Temperature (F)"]
                )
                self.assertEqual(
                    [dew_c.name, dew_f.name], ["Dew Point (C)", "Dew Point (F)"]
                )
                for entity, expected, unit in (
                    (temp_c, 20, "°C"),
                    (temp_f, 68, "°F"),
                    (dew_c, 0, "°C"),
                    (dew_f, 32, "°F"),
                ):
                    state = hass.states.get(entity.entity_id)
                    assert state is not None
                    self.assertAlmostEqual(float(state.state), expected)
                    self.assertEqual(state.attributes["unit_of_measurement"], unit)
                migrated = registry.async_get(temp_c.entity_id)
                assert migrated is not None
                self.assertEqual(migrated.unique_id, "device_entity_0_0_tempc")
                self.assertEqual(migrated.name, "Custom temperature")
                self.assertEqual(dew_f.unique_id, "device_entity_0_1_dewptf")
                self.assertEqual(
                    registry.async_get_entity_id(
                        "sensor", "ha_geist", humidity.unique_id
                    ),
                    "sensor.device_humidity",
                )
                data.devices[0].sensors[0].value = "20.123"
                self.assertEqual(temp_c.native_value, 20.123)
                self.assertEqual(temp_f.native_value, 68.22)
                data.devices[0].sensors[1].value = "31"
                self.assertEqual(dew_f.native_value, 31)
                self.assertEqual(dew_c.native_value, -0.56)
                data.devices[0].sensors[0].unit = "F"
                data.devices[0].sensors[0].value = "32"
                self.assertAlmostEqual(temp_c.native_value, 0)
                self.assertAlmostEqual(temp_f.native_value, 32)
                data.devices[0].sensors[0].value = "nan"
                self.assertIsNone(temp_c.native_value)
                data.devices[0].sensors[0].value = "20"
                data.devices[0].sensors[0].unit = None
                self.assertIsNone(temp_f.native_value)
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_digital_migration_removes_only_matching_legacy_entry(self) -> None:
        """Wait for a state and never remove another config entry's sensor."""
        data = _parse_devices(
            {
                "device": {
                    "entity": {
                        "0": {
                            "measurement": {"0": {"type": "ccatDigital", "value": "1"}}
                        }
                    }
                }
            }
        )
        coordinator = SimpleNamespace(
            data=data,
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
        )
        registry = SimpleNamespace(
            async_get_entity_id=MagicMock(return_value="sensor.legacy"),
            async_get=MagicMock(),
            async_remove=MagicMock(),
        )

        async def run(config_entry_id: str, binary_entry_id: str = "entry") -> None:
            entity = GeistDigitalInput(
                coordinator,
                data.devices[0],
                data.devices[0].sensors[0],  # type: ignore[arg-type]
            )
            entity.hass = object()  # type: ignore[assignment]
            entity.entity_id = "binary_sensor.digital"
            entity.registry_entry = SimpleNamespace(
                config_entry_id=binary_entry_id, unique_id=entity.unique_id
            )
            entity.async_on_remove = MagicMock()
            registry.async_get.return_value = SimpleNamespace(
                entity_id="sensor.legacy", config_entry_id=config_entry_id
            )
            unsubscribe = MagicMock()
            with (
                patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
                patch(
                    "custom_components.ha_geist.binary_sensor.async_track_state_change_event",
                    return_value=unsubscribe,
                ) as track,
                patch(
                    "custom_components.ha_geist.binary_sensor.er.async_get",
                    return_value=registry,
                ),
            ):
                await entity.async_added_to_hass()
                registry.async_remove.assert_not_called()
                listener = track.call_args.args[2]
                listener(SimpleNamespace(data={"new_state": None}))
                registry.async_remove.assert_not_called()
                unsubscribe.assert_not_called()
                listener(SimpleNamespace(data={"new_state": object()}))
                unsubscribe.assert_called_once()
                entity.async_on_remove.call_args.args[0]()
                unsubscribe.assert_called_once()

        asyncio.run(run("other-entry"))
        registry.async_remove.assert_not_called()
        asyncio.run(run("entry", "other-entry"))
        registry.async_remove.assert_not_called()
        asyncio.run(run("entry"))
        registry.async_remove.assert_called_once_with("sensor.legacy")
        registry.async_get_entity_id.assert_called_with(
            "sensor", "ha_geist", "device_entity_0_0"
        )

    def test_multiple_json_measurements_keep_ids_on_reload(self) -> None:
        """Duplicate measurement types retain their IDs and values after reordering."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                await dr.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_new_entities=False,
                    pref_disable_polling=False,
                    async_on_unload=MagicMock(),
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                data = _parse_devices(
                    {
                        "device": {
                            "entity": {
                                "0": {
                                    "measurement": {
                                        "0": {
                                            "type": "temperature",
                                            "value": "20",
                                            "units": "C",
                                        }
                                    }
                                },
                                "1": {
                                    "measurement": {
                                        "0": {
                                            "type": "temperature",
                                            "value": "30",
                                            "units": "C",
                                        }
                                    }
                                },
                            },
                            "analog": {
                                "0": {"value": "3"},
                                "2": {"value": "7"},
                            },
                        }
                    }
                )
                coordinator = SimpleNamespace(
                    data=data,
                    config_entry=entry,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                )
                entry.runtime_data = SimpleNamespace(coordinator=coordinator)
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry

                async def add_measurements() -> None:
                    tasks: list[asyncio.Task[None]] = []

                    def add_entities(entities: list[JsonSensorEntity]) -> None:
                        tasks.append(
                            hass.async_create_task(
                                platform.async_add_entities(entities)
                            )
                        )

                    await async_setup_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                    await asyncio.gather(*tasks)
                    await hass.async_block_till_done()

                async def check() -> None:
                    self.assertEqual(
                        set(platform.entities),
                        {
                            "sensor.device_tempc",
                            "sensor.device_tempc_2",
                            "sensor.device_tempf",
                            "sensor.device_tempf_2",
                            "sensor.device_analog",
                            "sensor.device_analog_2",
                        },
                    )
                    for entity_id, value in (
                        ("sensor.device_tempc", 20),
                        ("sensor.device_tempc_2", 30),
                        ("sensor.device_tempf", 68),
                        ("sensor.device_tempf_2", 86),
                        ("sensor.device_analog", 3),
                        ("sensor.device_analog_2", 7),
                    ):
                        state = hass.states.get(entity_id)
                        assert state is not None
                        self.assertAlmostEqual(float(state.state), value)

                await add_measurements()
                await check()
                await platform.async_reset()
                data.devices[0].sensors.reverse()
                await add_measurements()
                await check()
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_digital_entity_id_migration(self) -> None:
        """Rename existing default IDs without discarding registry settings."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_new_entities=False,
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                registry = er.async_get(hass)
                old = registry.async_get_or_create(
                    "binary_sensor",
                    "ha_geist",
                    "device_entity_0_0",
                    config_entry=entry,
                    suggested_object_id="device_entity_0_0",
                )
                registry.async_update_entity(old.entity_id, name="Custom input")
                second = registry.async_get_or_create(
                    "binary_sensor",
                    "ha_geist",
                    "device_entity_1_0",
                    config_entry=entry,
                    suggested_object_id="device_entity_1_0_digital",
                )
                registry.async_get_or_create(
                    "binary_sensor",
                    "other",
                    "other_input",
                    suggested_object_id="device_digital",
                )
                custom = registry.async_get_or_create(
                    "binary_sensor",
                    "ha_geist",
                    "device_entity_2_0",
                    config_entry=entry,
                    suggested_object_id="custom_input",
                )
                data = _parse_devices(
                    {
                        "device": {
                            "entity": {
                                "0": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "1"}
                                    }
                                },
                                "1": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "0"}
                                    }
                                },
                                "2": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "1"}
                                    }
                                },
                            }
                        }
                    }
                )
                coordinator = SimpleNamespace(data=data, config_entry=entry)
                entry.runtime_data = SimpleNamespace(coordinator=coordinator)
                add_entities = MagicMock()
                await async_setup_binary_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                entity_id = registry.async_get_entity_id(
                    "binary_sensor", "ha_geist", old.unique_id
                )
                self.assertEqual(entity_id, "binary_sensor.device_digital_2")
                migrated = registry.async_get(entity_id)
                assert migrated is not None
                self.assertEqual(migrated.name, "Custom input")
                self.assertEqual(
                    registry.async_get_entity_id(
                        "binary_sensor", "ha_geist", second.unique_id
                    ),
                    "binary_sensor.device_digital_3",
                )
                self.assertEqual(
                    registry.async_get_entity_id(
                        "binary_sensor", "ha_geist", custom.unique_id
                    ),
                    "binary_sensor.custom_input",
                )
                entities = add_entities.call_args.args[0]
                self.assertEqual(
                    [entity.entity_id for entity in entities],
                    [
                        "binary_sensor.device_digital",
                        "binary_sensor.device_digital",
                        "binary_sensor.device_digital",
                    ],
                )
                self.assertEqual(
                    [entity.is_on for entity in entities], [True, False, True]
                )
                data.devices[0].sensors.reverse()
                self.assertEqual(
                    [entity.is_on for entity in entities], [True, False, True]
                )
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_digital_migration_runs_after_home_assistant_publishes_state(self) -> None:
        """The real entity lifecycle publishes the replacement and removes the old ID."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    pref_disable_polling=False,
                    pref_disable_new_entities=False,
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                registry = er.async_get(hass)
                legacy = registry.async_get_or_create(
                    "sensor", "ha_geist", "device_entity_0_0", config_entry=entry
                )
                data = _parse_devices(
                    {
                        "device": {
                            "entity": {
                                "0": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "1"}
                                    }
                                }
                            }
                        }
                    }
                )
                coordinator = SimpleNamespace(
                    data=data,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                    config_entry=entry,
                )
                entity = GeistDigitalInput(
                    coordinator,
                    data.devices[0],
                    data.devices[0].sensors[0],  # type: ignore[arg-type]
                )
                entity._attr_device_info = None  # noqa: SLF001
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="binary_sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry
                await platform.async_add_entities([entity])
                await hass.async_block_till_done()
                state = hass.states.get(entity.entity_id)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.state, "on")
                self.assertIsNone(registry.async_get(legacy.entity_id))
                await platform.async_reset()
                await hass.async_block_till_done()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_multiple_digital_inputs_keep_distinct_ids_on_reload(self) -> None:
        """Allocate concise IDs and preserve them when the API order changes."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as config_dir:
                hass = HomeAssistant(config_dir)
                await er.async_load(hass)
                await dr.async_load(hass)
                entry = SimpleNamespace(
                    entry_id="entry",
                    domain="ha_geist",
                    data={CONF_API_TYPE: API_TYPE_JSON},
                    pref_disable_new_entities=False,
                    pref_disable_polling=False,
                )
                hass.config_entries = MagicMock()
                hass.config_entries.async_get_entry.return_value = entry
                data = _parse_devices(
                    {
                        "device": {
                            "entity": {
                                "0": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "1"}
                                    }
                                },
                                "1": {
                                    "measurement": {
                                        "0": {"type": "ccatDigital", "value": "0"}
                                    }
                                },
                            }
                        }
                    }
                )
                coordinator = SimpleNamespace(
                    data=data,
                    config_entry=entry,
                    last_update_success=True,
                    async_add_listener=MagicMock(return_value=lambda: None),
                )
                entry.runtime_data = SimpleNamespace(coordinator=coordinator)
                platform = EntityPlatform(
                    hass=hass,
                    logger=logging.getLogger(__name__),
                    domain="binary_sensor",
                    platform_name="ha_geist",
                    platform=None,
                    scan_interval=timedelta(seconds=30),
                    entity_namespace=None,
                )
                platform.config_entry = entry

                async def add_inputs() -> None:
                    tasks: list[asyncio.Task[None]] = []

                    def add_entities(entities: list[GeistDigitalInput]) -> None:
                        tasks.append(
                            hass.async_create_task(
                                platform.async_add_entities(entities)
                            )
                        )

                    await async_setup_binary_sensors(hass, entry, add_entities)  # type: ignore[arg-type]
                    await asyncio.gather(*tasks)
                    await hass.async_block_till_done()

                await add_inputs()
                self.assertEqual(
                    set(platform.entities),
                    {"binary_sensor.device_digital", "binary_sensor.device_digital_2"},
                )
                self.assertEqual(
                    hass.states.get("binary_sensor.device_digital").state, "on"
                )
                self.assertEqual(
                    hass.states.get("binary_sensor.device_digital_2").state, "off"
                )
                await platform.async_reset()
                data.devices[0].sensors.reverse()
                await add_inputs()
                self.assertEqual(
                    hass.states.get("binary_sensor.device_digital").state, "on"
                )
                self.assertEqual(
                    hass.states.get("binary_sensor.device_digital_2").state, "off"
                )
                await platform.async_reset()
                await hass.async_stop(force=True)

        asyncio.run(run())

    def test_json_api_authenticates_and_parses_measurements(self) -> None:
        """The JSON API client logs in and maps measurements and relays."""
        payload = {
            "device-1": {
                "type": "BB-REL-THA4",
                "label": "Basement",
                "entity": {
                    "0": {
                        "name": "Climate",
                        "measurement": {
                            "0": {
                                "type": "temperature",
                                "value": "72.5",
                                "units": "F",
                            }
                        },
                    }
                },
                "analog": {"0": {"name": "Analog 1", "value": "5.0", "units": "V"}},
                "relay": {
                    "0": {
                        "name": "Relay 1",
                        "state": "off",
                        "onLabel": "On",
                        "offLabel": "Off",
                    }
                },
            },
            "device-2": {
                "type": "ccat",
                "entity": {
                    "0": {
                        "measurement": {
                            "0": {"type": "temperature", "value": "20", "units": "C"}
                        }
                    }
                },
            },
            "device-3": {"type": "other"},
        }
        session = FakeSession(
            [login_response(), api_response(payload), system_response()]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        result = asyncio.run(client.async_get_data())

        self.assertEqual(result.devices[0].sensors[0].name, "Temperature")
        self.assertEqual(result.devices[0].sensors[0].value, "72.5")
        self.assertEqual(result.devices[0].relays[0].state, "off")
        self.assertEqual(result.devices[0].serial_number, "controller-serial")
        self.assertEqual(result.devices[1].serial_number, "device-2")
        self.assertIsNone(result.devices[2].serial_number)
        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            data=result,
        )
        sensor = JsonSensorEntity(
            coordinator,
            result.devices[0],
            result.devices[0].sensors[0],  # type: ignore[arg-type]
        )
        switch = GeistRelaySwitch(
            coordinator,
            result.devices[0],
            result.devices[0].relays[0],  # type: ignore[arg-type]
        )
        sensor_info = sensor.device_info
        switch_info = switch.device_info
        assert sensor_info is not None
        assert switch_info is not None
        self.assertEqual(sensor_info.get("serial_number"), "controller-serial")
        self.assertEqual(switch_info.get("serial_number"), "controller-serial")
        ccat_sensor = JsonSensorEntity(
            coordinator,
            result.devices[1],
            result.devices[1].sensors[0],  # type: ignore[arg-type]
        )
        ccat_info = ccat_sensor.device_info
        assert ccat_info is not None
        self.assertEqual(ccat_info.get("serial_number"), "device-2")
        self.assertEqual(
            session.requests[0]["url"], "http://geist.local/api/auth/test-user"
        )
        self.assertEqual(session.requests[1]["json"]["token"], "sample-session-id")
        self.assertEqual(session.requests[2]["method"], "GET")
        self.assertEqual(session.requests[2]["url"], "http://geist.local/api/sys")

    def test_json_update_debug_logs_exclude_credentials_and_payloads(self) -> None:
        """Device and alarm reads log counts without exposing request data."""
        payload = {
            "device-1": {
                "type": "BB-REL-THA4",
                "entity": {
                    "0": {"measurement": {"0": {"type": "temperature", "value": "23"}}}
                },
                "analog": {"0": {"value": "5"}},
                "relay": {"0": {"state": "off"}},
            },
            "device-2": {"type": "other"},
        }
        alarm_data = {
            "trigger": {
                "0": {"path": "device-1/analog/0", "state": "clear", "type": "high"}
            }
        }
        session = FakeSession(
            [
                login_response("private-token"),
                api_response(payload),
                api_response(
                    {
                        "model": "BB-REL-THA4",
                        "serialNumber": "controller-serial",
                        "label": "Office Monitor",
                    }
                ),
                api_response(alarm_data),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "private-password",
            session,  # type: ignore[arg-type]
            "http://url-user:url-password@geist.local",
        )

        async def run() -> None:
            data = await client.async_get_data()
            await client.async_get_alarms(data.devices)

        with self.assertLogs("custom_components.ha_geist", level="DEBUG") as logs:
            asyncio.run(run())
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                "Parsed JSON device data: server_host=geist.local, server_name=Office Monitor, devices=2, "
                "measurements=2, relays=1",
                "Parsed JSON alarm data: server_host=geist.local, server_name=Office Monitor, alarms=1",
            ],
        )
        for secret in ("private-token", "private-password", "url-user", "url-password"):
            self.assertNotIn(secret, "\n".join(logs.output))

    def test_json_debug_log_handles_missing_server_label(self) -> None:
        """Missing or unavailable metadata does not prevent update logging."""
        for system in (system_response(), api_response(ret_code=1)):
            with self.subTest(system=system._json_data):  # noqa: SLF001
                client = GeistJsonApiClient(
                    "user",
                    "password",
                    FakeSession([login_response(), api_response({}), system]),  # type: ignore[arg-type]
                    "http://geist.local",
                )
                with self.assertLogs(
                    "custom_components.ha_geist", level="DEBUG"
                ) as logs:
                    asyncio.run(client.async_get_data())
                messages = [record.getMessage() for record in logs.records]
                self.assertIn(
                    "Parsed JSON device data: server_host=geist.local, server_name=None, "
                    "devices=0, measurements=0, relays=0",
                    messages,
                )

    def test_json_sensor_types_and_icons(self) -> None:
        """Measurement and analog metadata produce matching HA icons and types."""
        result = _parse_devices(
            {
                "device-1": {
                    "entity": {
                        "0": {
                            "name": "Climate",
                            "measurement": {
                                "0": {
                                    "type": "temperature",
                                    "value": "72",
                                    "units": "F",
                                },
                                "1": {"type": "dew_point", "value": "46", "units": "F"},
                                "2": {
                                    "type": "unknown",
                                    "name": "Humidity",
                                    "value": "30",
                                },
                                "3": {"type": "other", "value": "5"},
                            },
                        }
                    },
                    "analog": {"0": {"name": "Analog 1", "value": "100"}},
                }
            }
        )
        device = result.devices[0]
        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            data=result,
        )
        entities = [
            JsonSensorEntity(coordinator, device, sensor)  # type: ignore[arg-type]
            for sensor in device.sensors
        ]
        self.assertEqual(entities[0].name, "Temperature")
        self.assertEqual(entities[1].name, "Dew Point")
        self.assertEqual(entities[2].name, "Relative Humidity")
        self.assertEqual(entities[2].entity_id, "sensor.device_1_humidity")
        self.assertEqual(entities[2].unique_id, "device-1_entity_0_2")
        self.assertEqual(entities[2].native_value, 30.0)
        self.assertEqual(entities[4].name, "Analog 1")
        for entity, icon, device_class, unit in zip(
            entities,
            (
                "mdi:thermometer",
                "mdi:thermometer-water",
                "mdi:water-percent",
                None,
                "mdi:chart-line-variant",
            ),
            (
                SensorDeviceClass.TEMPERATURE,
                SensorDeviceClass.TEMPERATURE,
                SensorDeviceClass.HUMIDITY,
                None,
                None,
            ),
            ("°F", "°F", "%", None, None),
            strict=True,
        ):
            with self.subTest(sensor=entity.name):
                entity.hass = SimpleNamespace(  # type: ignore[assignment]
                    config=SimpleNamespace(units=US_CUSTOMARY_SYSTEM)
                )
                self.assertEqual(entity.icon, icon)
                self.assertEqual(entity.device_class, device_class)
                self.assertEqual(entity.native_unit_of_measurement, unit)
                self.assertIsNotNone(entity.state)

    def test_json_generic_names_match_xml_fields(self) -> None:
        """Generic JSON measurements use the names and suffixes of XML fields."""
        measurements = {
            "0": {"type": "relativeHumidity"},
            "1": {"type": "dewpoint", "units": "C"},
            "2": {"type": "light"},
            "3": {"type": "airflow"},
            "4": {"type": "sound"},
            "5": {"type": "voltage"},
            "6": {"type": "current"},
            "7": {"type": "light", "name": "Custom light"},
        }
        data = _parse_devices(
            {"device": {"entity": {"0": {"measurement": measurements}}}}
        )
        device = data.devices[0]
        coordinator = SimpleNamespace(
            data=data, config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist")
        )
        entities = [
            JsonSensorEntity(coordinator, device, sensor)  # type: ignore[arg-type]
            for sensor in device.sensors
        ]
        self.assertEqual(
            [entity.name for entity in entities],
            [
                "Relative Humidity",
                "Dew Point",
                "Light Level",
                "Air Flow",
                "Sound Level",
                "Volts",
                "Amps",
                "Custom light",
            ],
        )
        self.assertEqual(
            [entity.entity_id for entity in entities],
            [
                "sensor.device_humidity",
                "sensor.device_dewpoint",
                "sensor.device_light",
                "sensor.device_airflow",
                "sensor.device_sound",
                "sensor.device_volts",
                "sensor.device_amps",
                "sensor.device_light",
            ],
        )

    def test_xml_humidity_entity_uses_relative_humidity_name(self) -> None:
        """XML humidity uses the same name and percentage unit as JSON."""
        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            data={"devices": []},
        )
        entity = FieldSensor(
            coordinator=coordinator,  # type: ignore[arg-type]
            device_id="device-1",
            device_name="Climate",
            manufacturer=None,
            model=None,
            serial_number=None,
            sw_version=None,
            field_key="Humidity",
            nice_name="Humidity",
        )

        self.assertEqual(entity.name, "Relative Humidity")
        self.assertEqual(entity.native_unit_of_measurement, "%")

    def test_json_unknown_unit_does_not_assign_incompatible_device_class(self) -> None:
        """An unrecognized unit cannot result in an invalid HA sensor class."""
        result = _parse_devices(
            {
                "device-1": {
                    "entity": {
                        "0": {
                            "measurement": {
                                "0": {
                                    "type": "temperature",
                                    "value": "20",
                                    "units": "unknown",
                                }
                            }
                        }
                    }
                }
            }
        )
        device = result.devices[0]
        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(entry_id="entry", domain="ha_geist"),
            data=result,
        )
        entity = JsonSensorEntity(
            coordinator,
            device,
            device.sensors[0],  # type: ignore[arg-type]
        )

        self.assertEqual(entity.icon, "mdi:thermometer")
        self.assertIsNone(entity.device_class)
        self.assertEqual(entity.native_unit_of_measurement, "unknown")

    def test_json_relay_command_uses_control_payload(self) -> None:
        """Relay control is sent only through the explicit on/off API command."""
        session = FakeSession(
            [
                login_response(),
                api_response({}),
                system_response(),
                api_response({}),
                api_response({}),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            await client.async_get_data()
            await client.async_set_relay_state("device-1", "0", "on")
            await client.async_set_relay_state("device-1", "0", "off")

        asyncio.run(run())

        request = session.requests[3]
        self.assertEqual(request["url"], "http://geist.local/api/dev/device-1/relay/0")
        self.assertEqual(
            request["json"],
            {
                "token": "sample-session-id",
                "cmd": "control",
                "data": {"action": "on"},
            },
        )
        self.assertEqual(session.requests[4]["json"]["data"], {"action": "off"})

    def test_alarm_mode_relay_cannot_be_controlled(self) -> None:
        """Alarm-mode relays are unavailable and reject direct switch commands."""
        data = _parse_devices(
            {"device-1": {"relay": {"0": {"state": "on", "mode": "alarm"}}}}
        )
        client = MagicMock(spec=GeistJsonApiClient)
        client.async_set_relay_state = AsyncMock()
        coordinator = SimpleNamespace(
            config_entry=SimpleNamespace(
                entry_id="entry",
                domain="ha_geist",
                runtime_data=SimpleNamespace(client=client),
            ),
            data=data,
            last_update_success=True,
            async_request_refresh=AsyncMock(),
        )
        switch = GeistRelaySwitch(
            coordinator,
            data.devices[0],
            data.devices[0].relays[0],  # type: ignore[arg-type]
        )

        async def run() -> None:
            self.assertFalse(switch.available)
            self.assertTrue(switch.is_on)
            with self.assertRaises(HomeAssistantError):
                await switch.async_turn_off()
            with self.assertRaises(HomeAssistantError):
                await switch.async_turn_on()
            client.async_set_relay_state.assert_not_awaited()

            coordinator.data = _parse_devices(
                {"device-1": {"relay": {"0": {"state": "off", "mode": "manual"}}}}
            )
            self.assertTrue(switch.available)
            await switch.async_turn_on()
            client.async_set_relay_state.assert_awaited_once_with("device-1", "0", "on")
            coordinator.async_request_refresh.assert_awaited_once()

            coordinator.data = _parse_devices(
                {"device-1": {"relay": {"0": {"state": "off"}}}}
            )
            self.assertTrue(switch.available)

        asyncio.run(run())

    def test_json_read_only_account_cannot_send_relay_command(self) -> None:
        """A read-only account fails before any relay control request is sent."""
        session = FakeSession(
            [login_response(control="false"), api_response({}), system_response()]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            await client.async_get_data()
            with self.assertRaises(IntegrationBlueprintApiClientAuthenticationError):
                await client.async_set_relay_state("device-1", "0", "on")

        asyncio.run(run())

        self.assertEqual(len(session.requests), EXPECTED_READ_ONLY_REQUESTS)

    def test_json_client_reauthenticates_once_when_session_expires(self) -> None:
        """An expired API token is refreshed and the original read retried."""
        session = FakeSession(
            [
                login_response("old-session"),
                api_response(ret_code=1002),
                login_response("new-session"),
                api_response({}),
                system_response(),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        asyncio.run(client.async_get_data())

        self.assertEqual(session.requests[1]["json"]["token"], "old-session")
        self.assertEqual(session.requests[3]["json"]["token"], "new-session")

    def test_json_client_reuses_token_between_updates(self) -> None:
        """Polling reuses a valid token instead of logging in on every refresh."""
        session = FakeSession(
            [login_response(), api_response({}), system_response(), api_response({})]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            await client.async_get_data()
            await client.async_get_data()

        asyncio.run(run())

        self.assertEqual(len(session.requests), 4)
        self.assertEqual(session.requests[3]["json"]["token"], "sample-session-id")

    def test_json_controller_uses_id_when_serial_is_unset(self) -> None:
        """A uniquely matched controller uses its ID when its serial is unset."""
        for serial in ("unset", ""):
            with self.subTest(serial=serial):
                session = FakeSession(
                    [
                        login_response(),
                        api_response({"device-1": {"type": "BB-REL-THA4"}}),
                        system_response(serial),
                    ]
                )
                client = GeistJsonApiClient(
                    "test-user",
                    "not-a-real-password",
                    session,  # type: ignore[arg-type]
                    "http://geist.local",
                )
                result = asyncio.run(client.async_get_data())
                self.assertEqual(result.devices[0].serial_number, "device-1")

    def test_json_system_serial_requires_unique_model(self) -> None:
        """Do not assign a system serial when multiple devices match the model."""
        devices = {
            "device-1": {"type": "BB-REL-THA4"},
            "device-2": {"type": "BB-REL-THA4"},
        }
        session = FakeSession(
            [login_response(), api_response(devices), system_response()]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        with self.assertLogs("custom_components.ha_geist", level="WARNING"):
            result = asyncio.run(client.async_get_data())
        self.assertTrue(all(device.serial_number is None for device in result.devices))

    def test_json_system_metadata_failure_is_retried(self) -> None:
        """Unavailable optional metadata does not stop device polling."""
        payload = {"device-1": {"type": "BB-REL-THA4"}}
        session = FakeSession(
            [
                login_response(),
                api_response(payload),
                FakeResponse(status=HTTPStatus.FORBIDDEN),
                api_response(payload),
                system_response(),
            ]
        )
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            with self.assertLogs("custom_components.ha_geist", level="WARNING"):
                first = await client.async_get_data()
            second = await client.async_get_data()
            self.assertIsNone(first.devices[0].serial_number)
            self.assertEqual(second.devices[0].serial_number, "controller-serial")

        asyncio.run(run())

    def test_json_recovered_serial_updates_registered_device(self) -> None:
        """A serial recovered after setup updates the existing HA device."""
        device_data = _parse_devices({"device-1": {"type": "BB-REL-THA4"}})
        device_data.devices[0].serial_number = "controller-serial"
        registry = SimpleNamespace(
            async_get_device=MagicMock(
                return_value=SimpleNamespace(id="registry-id", serial_number=None)
            ),
            async_update_device=MagicMock(),
        )
        coordinator = SimpleNamespace(
            hass=object(),
            config_entry=SimpleNamespace(
                runtime_data=SimpleNamespace(
                    client=SimpleNamespace(
                        async_get_data=AsyncMock(return_value=device_data)
                    )
                )
            ),
        )
        with patch(
            "custom_components.ha_geist.coordinator.dr.async_get", return_value=registry
        ):
            result = asyncio.run(
                BlueprintDataUpdateCoordinator._async_update_data(coordinator)  # type: ignore[arg-type]
            )

        self.assertIs(result, device_data)
        registry.async_get_device.assert_called_once_with(
            identifiers={("ha_geist", "device-1")}
        )
        registry.async_update_device.assert_called_once_with(
            "registry-id", serial_number="controller-serial"
        )

    def test_xml_firmware_updates_host_and_clears_owned_external_device(self) -> None:
        """Only the host owns XML firmware; shared devices remain untouched."""
        devices = [
            Device(id="host", name="Host", index="0", sw_version="3.16.3"),
            Device(id="external", name="External", index="1"),
            Device(id="shared", name="Shared", index="2"),
            Device(id="unknown", name="Unknown"),
        ]
        entries = {
            "host": SimpleNamespace(id="host-registry", sw_version="old"),
            "external": SimpleNamespace(
                id="external-registry", sw_version="old", config_entries={"entry-id"}
            ),
            "shared": SimpleNamespace(
                id="shared-registry",
                sw_version="old",
                config_entries={"entry-id", "other-entry"},
            ),
            "unknown": SimpleNamespace(
                id="unknown-registry",
                sw_version="old",
                config_entries={"entry-id"},
            ),
        }
        registry = SimpleNamespace(
            async_get_device=MagicMock(
                side_effect=lambda identifiers: entries.get(next(iter(identifiers))[1])
            ),
            async_update_device=MagicMock(),
        )
        data = {"devices": devices, "raw": ""}
        coordinator = SimpleNamespace(
            hass=object(),
            config_entry=SimpleNamespace(
                entry_id="entry-id",
                runtime_data=SimpleNamespace(
                    client=SimpleNamespace(async_get_data=AsyncMock(return_value=data))
                ),
            ),
        )
        with patch(
            "custom_components.ha_geist.coordinator.dr.async_get", return_value=registry
        ):
            result = asyncio.run(
                BlueprintDataUpdateCoordinator._async_update_data(coordinator)  # type: ignore[arg-type]
            )

        self.assertIs(result, data)
        self.assertEqual(registry.async_update_device.call_count, 2)
        registry.async_update_device.assert_any_call(
            "host-registry", sw_version="3.16.3"
        )
        registry.async_update_device.assert_any_call(
            "external-registry", sw_version=None
        )

    def test_json_client_rejects_invalid_login(self) -> None:
        """Invalid passwords surface as authentication errors."""
        session = FakeSession([api_response({}, ret_code=1004)])
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            with self.assertRaises(IntegrationBlueprintApiClientAuthenticationError):
                await client.async_get_data()

        asyncio.run(run())

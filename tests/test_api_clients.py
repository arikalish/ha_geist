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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.ha_geist.api import (
    IntegrationBlueprintApiClient,
    IntegrationBlueprintApiClientAuthenticationError,
    IntegrationBlueprintApiClientError,
)
from custom_components.ha_geist.api_json import GeistJsonApiClient, _parse_devices
from custom_components.ha_geist.binary_sensor import (
    GeistDigitalInput,
)
from custom_components.ha_geist.binary_sensor import (
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.ha_geist.const import API_TYPE_JSON, CONF_API_TYPE
from custom_components.ha_geist.coordinator import BlueprintDataUpdateCoordinator
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
            self.assertEqual(alarms[1].id, "device_1_analog_0_high_1")
            self.assertEqual(alarms[1].name, "Analog 1 High Alarm")

        asyncio.run(run())
        self.assertEqual(session.requests[3]["url"], "http://geist.local/api/alarm")
        self.assertEqual(session.requests[3]["json"]["cmd"], "get")

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
        listener()
        sensor = add_entities.call_args.args[0][0]
        self.assertIsInstance(sensor, AlarmSensor)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, "tripped")
        self.assertEqual(sensor.extra_state_attributes, {"severity": "alarm"})
        self.assertEqual(sensor.unique_id, "entry_alarm_0")
        self.assertEqual(sensor.device_info["identifiers"], {("ha_geist", "device-1")})
        data.alarms[0].state = "latched"
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
        """Only legacy JSON alarm entries are removed after a successful read."""
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
            data.alarms = []
            listener()
            listener()
            registry.async_remove.assert_called_once_with("sensor.old")

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
            await async_setup_binary_sensors(None, entry, binary_entities)  # type: ignore[arg-type]
            binary = binary_entities.call_args.args[0]
            self.assertEqual(len(binary), 1)
            self.assertIsInstance(binary[0], GeistDigitalInput)

            add_entities = MagicMock()
            await async_setup_sensors(object(), entry, add_entities)  # type: ignore[arg-type]
            sensors = add_entities.call_args.args[0]
            self.assertEqual(len(sensors), 1)
            self.assertEqual(sensors[0].name, "Temperature")

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

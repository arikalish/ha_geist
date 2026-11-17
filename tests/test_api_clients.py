"""Tests for the Geist XML and JSON API clients."""

# unittest assertions are used to keep these async tests independent of pytest fixtures.
# ruff: noqa: PT009, PT027

from __future__ import annotations

import asyncio
import json
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.ha_geist.api import (
    IntegrationBlueprintApiClient,
    IntegrationBlueprintApiClientAuthenticationError,
)
from custom_components.ha_geist.api_json import GeistJsonApiClient, _parse_devices
from custom_components.ha_geist.coordinator import BlueprintDataUpdateCoordinator
from custom_components.ha_geist.sensor import (
    FieldSensor,
    JsonSensorEntity,
)
from custom_components.ha_geist.sensor import (
    async_setup_entry as async_setup_sensors,
)
from custom_components.ha_geist.switch import GeistRelaySwitch
from custom_components.ha_geist.types import Device

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
        )
        entry = SimpleNamespace(
            data={}, runtime_data=SimpleNamespace(coordinator=coordinator)
        )
        add_entities = MagicMock()
        asyncio.run(async_setup_sensors(None, entry, add_entities))  # type: ignore[arg-type]
        entities = add_entities.call_args.args[0]
        versions = {
            entity.device_info["name"]: entity.device_info["sw_version"]
            for entity in entities
        }
        self.assertEqual(versions["Entertainment Center RSM"], "3.16.3")
        self.assertIsNone(versions["Temp Sensor"])

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
            coordinator, result.devices[0], result.devices[0].sensors[0]  # type: ignore[arg-type]
        )
        switch = GeistRelaySwitch(
            coordinator, result.devices[0], result.devices[0].relays[0]  # type: ignore[arg-type]
        )
        sensor_info = sensor.device_info
        switch_info = switch.device_info
        assert sensor_info is not None
        assert switch_info is not None
        self.assertEqual(sensor_info.get("serial_number"), "controller-serial")
        self.assertEqual(switch_info.get("serial_number"), "controller-serial")
        ccat_sensor = JsonSensorEntity(
            coordinator, result.devices[1], result.devices[1].sensors[0]  # type: ignore[arg-type]
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
            coordinator, device, device.sensors[0]  # type: ignore[arg-type]
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
            coordinator, data.devices[0], data.devices[0].relays[0]  # type: ignore[arg-type]
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
                    client=SimpleNamespace(async_get_data=AsyncMock(return_value=device_data))
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

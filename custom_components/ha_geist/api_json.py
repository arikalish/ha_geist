"""Client for Geist devices that expose the token-authenticated JSON API."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any, NoReturn
from urllib.parse import quote

import aiohttp
import async_timeout
from homeassistant.util import slugify

from .api import (
    IntegrationBlueprintApiClientAuthenticationError,
    IntegrationBlueprintApiClientCommunicationError,
    IntegrationBlueprintApiClientError,
)
from .const import LOGGER
from .types import (
    Alarm,
    JsonDevice,
    JsonParsedData,
    JsonRelay,
    JsonSensor,
    unique_alarm_ids,
)

_SUCCESS = 0
_SESSION_EXPIRED = 1002
_AUTH_ERROR_CODES = {1001, 1002, 1003, 1004}


def _raise_error(
    error_type: type[Exception],
    message: str,
    cause: BaseException | None = None,
) -> NoReturn:
    """Raise a client exception while preserving an optional cause."""
    error = error_type(message)
    if cause is not None:
        raise error from cause
    raise error


def _items(value: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Return mapping entries from an API object or indexed list."""
    if isinstance(value, Mapping):
        return [
            (str(key), item) for key, item in value.items() if isinstance(item, Mapping)
        ]
    if isinstance(value, list):
        return [
            (str(index), item)
            for index, item in enumerate(value)
            if isinstance(item, Mapping)
        ]
    return []


def _parse_devices(payload: Any) -> JsonParsedData:
    """Convert the /api/dev response into integration entities' data."""
    if not isinstance(payload, Mapping):
        _raise_error(
            IntegrationBlueprintApiClientError,
            "The device API returned an invalid device list",
        )

    device_payload = payload.get("dev", payload)
    if not isinstance(device_payload, Mapping):
        _raise_error(
            IntegrationBlueprintApiClientError,
            "The device API returned an invalid device list",
        )

    devices: list[JsonDevice] = []
    for device_id, source in _items(device_payload):
        sensors: list[JsonSensor] = []
        for entity_index, entity in _items(source.get("entity")):
            for measurement_index, measurement in _items(entity.get("measurement")):
                measurement_type = str(measurement.get("type") or measurement_index)
                name = str(
                    measurement.get("name")
                    or measurement_type.replace("_", " ").title()
                )
                sensors.append(
                    JsonSensor(
                        key=f"entity_{entity_index}_{measurement_index}",
                        name=name,
                        value=measurement.get("value"),
                        unit=measurement.get("units"),
                        measurement_type=str(measurement["type"])
                        if measurement.get("type") is not None
                        else None,
                        measurement_name=str(measurement["name"])
                        if measurement.get("name") is not None
                        else None,
                    )
                )

        for analog_index, analog in _items(source.get("analog")):
            sensors.append(
                JsonSensor(
                    key=f"analog_{analog_index}",
                    name=str(
                        analog.get("label")
                        or analog.get("name")
                        or f"Analog {analog_index}"
                    ),
                    value=analog.get("value"),
                    unit=analog.get("units"),
                    measurement_type="analog",
                )
            )

        relays = [
            JsonRelay(
                index=relay_index,
                name=str(
                    relay.get("label") or relay.get("name") or f"Relay {relay_index}"
                ),
                state=str(relay.get("state") or "unknown"),
                on_label=str(relay.get("onLabel") or "On"),
                off_label=str(relay.get("offLabel") or "Off"),
                mode=str(relay["mode"]) if relay.get("mode") is not None else None,
            )
            for relay_index, relay in _items(source.get("relay"))
        ]
        devices.append(
            JsonDevice(
                id=device_id,
                name=str(source.get("label") or source.get("name") or device_id),
                model=str(source.get("type") or ""),
                sensors=sensors,
                relays=relays,
                serial_number=(
                    device_id
                    if str(source.get("type") or "").lower() == "ccat"
                    else None
                ),
            )
        )

    return JsonParsedData(devices=devices, raw=dict(payload))


class GeistJsonApiClient:
    """Authenticate and retrieve data from Geist's JSON API."""

    def __init__(
        self,
        username: str | None,
        password: str | None,
        session: aiohttp.ClientSession,
        base_url: str,
    ) -> None:
        """Initialize the client."""
        self._username = username
        self._password = password
        self._session = session
        self._base_url = base_url.rstrip("/")
        if self._base_url.lower().endswith("/data.xml"):
            self._base_url = self._base_url[: -len("/data.xml")]
        self._token: str | None = None
        self._can_control = False
        self._system_info: Mapping[str, Any] | None = None

    async def async_get_data(self) -> JsonParsedData:
        """Fetch current device data, refreshing an expired token once."""
        if self._token is None:
            await self._async_login()
        response = await self._async_call("/api/dev", "get")
        data = response.get("data")
        if not isinstance(data, Mapping):
            _raise_error(
                IntegrationBlueprintApiClientError,
                "The device API returned an invalid device list",
            )
        parsed = _parse_devices(data)
        system_info_loaded = False
        if self._system_info is None:
            try:
                system_response = await self._async_send("/api/sys", None)
                self._check_response(system_response)
                system_data = system_response.get("data")
                if not isinstance(system_data, Mapping):
                    _raise_error(
                        IntegrationBlueprintApiClientError,
                        "The device returned invalid system metadata",
                    )
                self._system_info = system_data
                system_info_loaded = True
            except IntegrationBlueprintApiClientError as exception:
                LOGGER.warning(
                    "Unable to retrieve device system metadata: %s", exception
                )

        if self._system_info is not None:
            model = self._system_info.get("model")
            serial = self._system_info.get("serialNumber")
            if isinstance(model, str) and model:
                matches = [device for device in parsed.devices if device.model == model]
                if len(matches) == 1:
                    if isinstance(serial, str) and serial.strip().lower() not in {
                        "",
                        "unset",
                    }:
                        matches[0].serial_number = serial.strip()
                    elif serial is None or isinstance(serial, str):
                        matches[0].serial_number = matches[0].id
                elif system_info_loaded:
                    LOGGER.warning(
                        "Cannot match controller metadata to exactly one device with "
                        "model %s",
                        model,
                    )
        return parsed

    @property
    def can_control(self) -> bool:
        """Return whether the authenticated user may control relays."""
        return self._can_control

    async def async_get_alarms(self, devices: list[JsonDevice]) -> list[Alarm]:
        """Read configured alarm triggers and their states."""
        response = await self._async_call("/api/alarm", "get")
        data = response.get("data")
        if not isinstance(data, Mapping) or not isinstance(
            data.get("trigger"), (Mapping, list)
        ):
            _raise_error(
                IntegrationBlueprintApiClientError,
                "The device returned an invalid alarm list",
            )
        devices_by_id = {device.id: device for device in devices}
        alarms: list[Alarm] = []
        for alarm_id, trigger in _items(data["trigger"]):
            state = trigger.get("state")
            path = trigger.get("path")
            if (
                not isinstance(state, str)
                or not state
                or not isinstance(path, str)
                or not path.strip("/")
            ):
                LOGGER.warning("Skipping JSON alarm %s without state or path", alarm_id)
                continue
            parts = path.strip("/").split("/")
            device = devices_by_id.get(parts[0])
            sensor_key = None
            match parts:
                case [_, "analog", index]:
                    sensor_key = f"analog_{index}"
                case [_, "entity", index, "measurement", measurement]:
                    sensor_key = f"entity_{index}_{measurement}"
            sensor_name = None
            if device and sensor_key:
                sensor_name = next(
                    (
                        sensor.name
                        for sensor in device.sensors
                        if sensor.key == sensor_key
                    ),
                    None,
                )
                if sensor_name and sensor_name.lower() == "humidity":
                    sensor_name = "Relative Humidity"
            alarm_type = trigger.get("type")
            name_parts = [
                sensor_name or (device.name if device else str(alarm_id)),
                alarm_type.title()
                if isinstance(alarm_type, str) and alarm_type in {"high", "low"}
                else "",
                "Alarm",
            ]
            name = " ".join(part for part in name_parts if part)
            threshold = trigger.get("threshold")
            if (
                isinstance(threshold, (str, int, float))
                and not isinstance(threshold, bool)
                and threshold != ""
            ):
                name = f"{name} (limit {threshold})"
            alarms.append(
                Alarm(
                    id=slugify(
                        "_".join(
                            part
                            for part in (path, alarm_type, alarm_id)
                            if isinstance(part, str) and part
                        )
                    ),
                    name=name,
                    state=state,
                    device_id=device.id if device else None,
                    attributes={
                        key: trigger[key]
                        for key in (
                            "severity",
                            "type",
                            "path",
                            "threshold",
                            "latching",
                            "tripDelay",
                            "clearDelay",
                        )
                        if key in trigger
                    },
                )
            )
        try:
            return unique_alarm_ids(alarms)
        except ValueError as exception:
            _raise_error(
                IntegrationBlueprintApiClientError,
                "The device returned duplicate alarm identities",
                exception,
            )

    async def async_set_relay_state(
        self, device_id: str, relay_index: str, state: str
    ) -> None:
        """Set a relay to on or off."""
        if state not in {"on", "off"}:
            _raise_error(ValueError, "Relay state must be 'on' or 'off'")
        if not self._can_control:
            _raise_error(
                IntegrationBlueprintApiClientAuthenticationError,
                "The configured user does not have relay control permission",
            )
        path = (
            f"/api/dev/{quote(device_id, safe='')}/relay/{quote(relay_index, safe='')}"
        )
        await self._async_call(path, "control", {"action": state})

    async def _async_login(self) -> None:
        """Acquire a token using the device's login endpoint."""
        if not self._username or not self._password:
            _raise_error(
                IntegrationBlueprintApiClientAuthenticationError,
                "A username and password are required for the JSON API",
            )

        response = await self._async_send(
            f"/api/auth/{quote(self._username, safe='')}",
            {
                "token": "",
                "cmd": "login",
                "data": {"password": self._password},
            },
        )
        self._check_response(response)
        data = response.get("data")
        if not isinstance(data, Mapping):
            _raise_error(
                IntegrationBlueprintApiClientAuthenticationError,
                "The device did not return an authentication token",
            )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            _raise_error(
                IntegrationBlueprintApiClientAuthenticationError,
                "The device did not return an authentication token",
            )
        self._token = token
        self._can_control = _is_enabled(data.get("control")) or _is_enabled(
            data.get("admin")
        )

    async def _async_call(
        self,
        path: str,
        command: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated API call, retrying once after session expiry."""
        if self._token is None:
            await self._async_login()
        response = await self._async_send(
            path,
            {
                "token": self._token,
                "cmd": command,
                **({"data": data} if data is not None else {}),
            },
        )
        if _ret_code(response) == _SESSION_EXPIRED:
            await self._async_login()
            response = await self._async_send(
                path,
                {
                    "token": self._token,
                    "cmd": command,
                    **({"data": data} if data is not None else {}),
                },
            )
        self._check_response(response)
        return response

    async def _async_send(
        self, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Send a JSON command or retrieve public system metadata."""
        try:
            async with (
                async_timeout.timeout(15),
                self._session.request(
                    method="POST" if payload is not None else "GET",
                    url=f"{self._base_url}{path}",
                    **({"json": payload} if payload is not None else {}),
                ) as response,
            ):
                if response.status in (401, 403):
                    _raise_error(
                        IntegrationBlueprintApiClientAuthenticationError,
                        "Invalid credentials",
                    )
                response.raise_for_status()
                try:
                    result = await response.json(content_type=None)
                except (aiohttp.ClientError, json.JSONDecodeError) as exception:
                    _raise_error(
                        IntegrationBlueprintApiClientCommunicationError,
                        "The device returned an invalid JSON response",
                        exception,
                    )
        except TimeoutError as exception:
            _raise_error(
                IntegrationBlueprintApiClientCommunicationError,
                "Timeout communicating with the device",
                exception,
            )
        except (aiohttp.ClientError, socket.gaierror) as exception:
            _raise_error(
                IntegrationBlueprintApiClientCommunicationError,
                f"Error communicating with the device: {exception}",
                exception,
            )

        if not isinstance(result, dict):
            _raise_error(
                IntegrationBlueprintApiClientCommunicationError,
                "The device returned an invalid JSON response",
            )
        return result

    @staticmethod
    def _check_response(response: dict[str, Any]) -> None:
        """Raise an explicit error when the API reports a failed command."""
        code = _ret_code(response)
        if code == _SUCCESS:
            return
        if code in _AUTH_ERROR_CODES:
            _raise_error(
                IntegrationBlueprintApiClientAuthenticationError,
                "The device rejected the authentication or permission request",
            )
        _raise_error(
            IntegrationBlueprintApiClientError,
            f"Device API error ({code}): {response.get('retMsg') or 'unknown error'}",
        )


def _ret_code(response: Mapping[str, Any]) -> int:
    """Read the API result code, rejecting malformed responses explicitly."""
    try:
        return int(response.get("retCode", -1))
    except (TypeError, ValueError) as exception:
        _raise_error(
            IntegrationBlueprintApiClientError,
            "The device returned an invalid result code",
            exception,
        )


def _is_enabled(value: Any) -> bool:
    """Interpret the boolean values returned by the device API."""
    return value is True or (isinstance(value, str) and value.lower() == "true")

"""Tests for the Geist XML and JSON API clients."""

# unittest assertions are used to keep these async tests independent of pytest fixtures.
# ruff: noqa: PT009, PT027

from __future__ import annotations

import asyncio
import json
import unittest
from http import HTTPStatus
from pathlib import Path
from typing import Any

from custom_components.ha_geist.api import (
    IntegrationBlueprintApiClient,
    IntegrationBlueprintApiClientAuthenticationError,
)
from custom_components.ha_geist.api_json import GeistJsonApiClient

EXPECTED_READ_ONLY_REQUESTS = 2


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
        self.assertEqual(result["devices"][0].serial_number, "01D9BCD512000084")
        self.assertEqual(result["devices"][1].serial_number, "EA00000132301A28")
        self.assertEqual(session.requests[0]["url"], "http://geist.local/data.xml")

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
            }
        }
        session = FakeSession([login_response(), api_response(payload)])
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        result = asyncio.run(client.async_get_data())

        self.assertEqual(result.devices[0].sensors[0].name, "Climate Temperature")
        self.assertEqual(result.devices[0].sensors[0].value, "72.5")
        self.assertEqual(result.devices[0].relays[0].state, "off")
        self.assertEqual(
            session.requests[0]["url"], "http://geist.local/api/auth/test-user"
        )
        self.assertEqual(session.requests[1]["json"]["token"], "sample-session-id")

    def test_json_relay_command_uses_control_payload(self) -> None:
        """Relay control is sent only through the explicit on/off API command."""
        session = FakeSession([login_response(), api_response({}), api_response({})])
        client = GeistJsonApiClient(
            "test-user",
            "not-a-real-password",
            session,  # type: ignore[arg-type]
            "http://geist.local",
        )

        async def run() -> None:
            await client.async_get_data()
            await client.async_set_relay_state("device-1", "0", "on")

        asyncio.run(run())

        request = session.requests[2]
        self.assertEqual(request["url"], "http://geist.local/api/dev/device-1/relay/0")
        self.assertEqual(
            request["json"],
            {
                "token": "sample-session-id",
                "cmd": "control",
                "data": {"action": {"value": "on"}},
            },
        )

    def test_json_read_only_account_cannot_send_relay_command(self) -> None:
        """A read-only account fails before any relay control request is sent."""
        session = FakeSession([login_response(control="false"), api_response({})])
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
        session = FakeSession([login_response(), api_response({}), api_response({})])
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

        self.assertEqual(len(session.requests), 3)
        self.assertEqual(session.requests[2]["json"]["token"], "sample-session-id")

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

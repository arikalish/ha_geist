"""HTTP API client for ha_geist that fetches and parses data.xml."""

from __future__ import annotations

import socket
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp
import async_timeout

from .const import LOGGER
from .types import Device, Field, ParsedData


class IntegrationBlueprintApiClientError(Exception):
    """Exception to indicate a general API error."""


class IntegrationBlueprintApiClientCommunicationError(
    IntegrationBlueprintApiClientError,
):
    """Exception to indicate a communication error."""


class IntegrationBlueprintApiClientAuthenticationError(
    IntegrationBlueprintApiClientError,
):
    """Exception to indicate an authentication error."""


def _verify_response_or_raise(response: aiohttp.ClientResponse) -> None:
    """Verify that the response is valid."""
    if response.status in (401, 403):
        msg = "Invalid credentials"
        raise IntegrationBlueprintApiClientAuthenticationError(
            msg,
        )
    response.raise_for_status()


class IntegrationBlueprintApiClient:
    """API client that fetches a `data.xml` file using HTTP Basic Auth.

    The client will return a parsed representation of the XML as a
    Python dict: {"devices": [...], "raw": "<xml>...</xml>"}
    """

    def __init__(
        self,
        username: str | None,
        password: str | None,
        session: aiohttp.ClientSession,
        base_url: str | None = None,
    ) -> None:
        """Initialize the client.

        `base_url` may point to a host or a full path to `data.xml`. If a
        host is provided the client will request `{base_url.rstrip('/')}/data.xml`.
        """
        self._username = username
        self._password = password
        self._session = session
        self._base_url = base_url

    async def async_get_data(self) -> Any:
        """Download and parse `data.xml` using HTTP Basic Auth."""
        # Determine URL to fetch
        if self._base_url:
            if self._base_url.lower().endswith(".xml"):
                url = self._base_url
            else:
                url = f"{self._base_url.rstrip('/')}/data.xml"
        else:
            # Fallback to a sensible default if no URL provided
            url = "http://example.com/data.xml"

        try:
            async with async_timeout.timeout(15):
                # Only include auth if both username and password were provided
                auth = None
                if self._username and self._password:
                    auth = aiohttp.BasicAuth(self._username, self._password)

                response = await self._session.request(
                    method="GET",
                    url=url,
                    auth=auth,
                )

                # If the response indicates auth problems, raise immediately
                if response.status in (401, 403):
                    _verify_response_or_raise(response)

                # Helper: parse XML into ParsedData
                def _parse_xml(text_to_parse: str) -> ParsedData:
                    root = ET.fromstring(text_to_parse)
                    server_company_local = root.attrib.get("company")
                    server_sw_version_local = root.attrib.get(
                        "version"
                    ) or root.attrib.get("product-version")
                    parsed_local: ParsedData = {"devices": [], "raw": text_to_parse}
                    devices_container = root.find("devices")
                    if devices_container is None:
                        return parsed_local

                    devices_list: list[Device] = []
                    for device_elem in devices_container.findall("device"):
                        d = Device(
                            id=device_elem.attrib.get("id", ""),
                            name=device_elem.attrib.get("name", ""),
                            type=device_elem.attrib.get("type"),
                            available=device_elem.attrib.get("available"),
                            index=device_elem.attrib.get("index"),
                            manufacturer=server_company_local,
                            sw_version=(
                                server_sw_version_local
                                if device_elem.attrib.get("index") == "0"
                                else None
                            ),
                            serial_number=device_elem.attrib.get("id"),
                        )

                        for child in device_elem:
                            if child.tag.lower() == "field":
                                key = child.attrib.get("key")
                                value = child.attrib.get("value")
                                nice = child.attrib.get("niceName")
                                if key:
                                    d.fields[key] = Field(
                                        key=key, value=value, niceName=nice
                                    )
                            else:
                                setattr(d, child.tag, child.text)

                        devices_list.append(d)

                    parsed_local["devices"] = devices_list
                    return parsed_local

                _verify_response_or_raise(response)
                text = await response.text()
                parsed = _parse_xml(text)
                LOGGER.debug(
                    "Parsed data.xml: server_host=%s, devices=%d",
                    ET.fromstring(text).attrib.get("host"),
                    len(parsed.get("devices", [])),
                )
                return parsed

        except IntegrationBlueprintApiClientAuthenticationError:
            raise
        except TimeoutError as exception:
            msg = f"Timeout error fetching information - {exception}"
            raise IntegrationBlueprintApiClientCommunicationError(
                msg,
            ) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            msg = f"Error fetching information - {exception}"
            raise IntegrationBlueprintApiClientCommunicationError(
                msg,
            ) from exception
        except Exception as exception:  # pylint: disable=broad-except
            msg = f"Something really wrong happened! - {exception}"
            raise IntegrationBlueprintApiClientError(
                msg,
            ) from exception

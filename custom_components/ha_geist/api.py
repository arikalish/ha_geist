"""HTTP API client for ha_geist that fetches and parses data.xml."""

from __future__ import annotations

import socket
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp
import async_timeout

from .const import LOGGER


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
        username: str,
        password: str,
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
                response = await self._session.request(
                    method="GET",
                    url=url,
                    auth=aiohttp.BasicAuth(self._username, self._password),
                )
                _verify_response_or_raise(response)
                text = await response.text()

                # Parse XML into a simple Python structure
                parsed = {"devices": [], "raw": text}
                try:
                    root = ET.fromstring(text)
                    # Look for devices container
                    devices_container = root.find("devices")
                    if devices_container is not None:
                        for device_elem in devices_container.findall("device"):
                            # Start with device attributes
                            dev = dict(device_elem.attrib)
                            # Use the device's own `name` attribute as provided in XML

                            # Parse child elements (e.g., <field .../>)
                            for child in device_elem:
                                if child.tag.lower() == "field":
                                    key = child.attrib.get("key")
                                    value = child.attrib.get("value")
                                    nice = child.attrib.get("niceName")
                                    if key:
                                        # Store both the raw value and the nicename
                                        dev.setdefault("fields", {})[key] = {
                                            "value": value,
                                            "niceName": nice,
                                        }
                                else:
                                    dev[child.tag] = child.text

                            parsed["devices"].append(dev)
                    else:
                        # Generic mapping for top-level elements when no devices container
                        for child in root:
                            parsed.setdefault(child.tag, child.text)
                except ET.ParseError:
                    # Keep raw text if parsing fails
                    pass

                # Log summary information after initial parse
                try:
                    LOGGER.debug(
                        "Parsed data.xml: server_host=%s, devices=%d",
                        server_host,
                        len(parsed.get("devices", [])),
                    )
                except Exception:
                    # Avoid failing parsing/logging for unexpected logging errors
                    pass

                return parsed

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

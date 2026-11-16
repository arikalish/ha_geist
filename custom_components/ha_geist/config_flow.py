"""Adds config flow for Blueprint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from slugify import slugify

from .api import (
    IntegrationBlueprintApiClient,
    IntegrationBlueprintApiClientAuthenticationError,
    IntegrationBlueprintApiClientCommunicationError,
    IntegrationBlueprintApiClientError,
)
from .api_json import GeistJsonApiClient
from .const import API_TYPE_JSON, API_TYPE_XML, CONF_API_TYPE, DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo


class BlueprintFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Blueprint."""

    VERSION = 1

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Handle a device discovered from its DHCP MAC address."""
        self._discovered_url = f"http://{discovery_info.ip}"
        await self.async_set_unique_id(slugify(self._discovered_url))
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": str(discovery_info.ip)}
        return await self.async_step_user()

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            try:
                api_type = await self._test_credentials(
                    username=user_input.get(CONF_USERNAME),
                    password=user_input.get(CONF_PASSWORD),
                    url=user_input[CONF_URL],
                )
            except IntegrationBlueprintApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                _errors["base"] = "auth"
            except IntegrationBlueprintApiClientCommunicationError as exception:
                LOGGER.error(exception)
                _errors["base"] = "connection"
            except IntegrationBlueprintApiClientError as exception:
                LOGGER.exception(exception)
                _errors["base"] = "unknown"
            else:
                # Use URL for unique id
                unique_source = user_input[CONF_URL]
                await self.async_set_unique_id(slugify(unique_source))
                self._abort_if_unique_id_configured()
                title = user_input[CONF_URL]
                return self.async_create_entry(
                    title=title,
                    data={**user_input, CONF_API_TYPE: api_type},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME,
                        default=(user_input or {}).get(CONF_USERNAME, vol.UNDEFINED),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                    vol.Required(
                        CONF_URL,
                        default=(user_input or {}).get(
                            CONF_URL,
                            getattr(self, "_discovered_url", "http://example.com"),
                        ),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.URL,
                        ),
                    ),
                    vol.Optional(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                        ),
                    ),
                },
            ),
            errors=_errors,
        )

    async def _test_credentials(
        self, username: str | None, password: str | None, url: str
    ) -> str:
        """Detect the device API and validate its credentials."""
        xml_error: IntegrationBlueprintApiClientError | None = None
        xml_client = IntegrationBlueprintApiClient(
            username=username,
            password=password,
            session=async_get_clientsession(self.hass),
            base_url=url,
        )
        try:
            xml_data = await xml_client.async_get_data()
        except IntegrationBlueprintApiClientError as exception:
            xml_error = exception
        else:
            if xml_data["devices"]:
                return API_TYPE_XML
            message = "The XML endpoint did not contain device data"
            xml_error = IntegrationBlueprintApiClientError(message)

        json_client = GeistJsonApiClient(
            username=username,
            password=password,
            session=async_get_clientsession(self.hass),
            base_url=url,
        )
        try:
            await json_client.async_get_data()
        except IntegrationBlueprintApiClientCommunicationError as exception:
            if xml_error and "invalid credentials" in str(xml_error).lower():
                raise IntegrationBlueprintApiClientAuthenticationError(
                    "Invalid credentials"
                ) from exception
            raise
        return API_TYPE_JSON

"""Adds config flow for Blueprint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_URL, CONF_USERNAME
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

    VERSION = 2

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
        return await self._async_connection_form("user", user_input)

    async def async_step_reconfigure(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Allow an existing connection to be updated."""
        return await self._async_connection_form("reconfigure", user_input)

    async def _async_connection_form(
        self,
        step_id: str,
        user_input: dict | None,
    ) -> config_entries.ConfigFlowResult:
        """Show and validate connection settings for setup or reconfiguration."""
        entry = self._get_reconfigure_entry() if step_id == "reconfigure" else None
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                key: value
                for key, value in user_input.items()
                if key not in (CONF_USERNAME, CONF_PASSWORD) or value
            }
            try:
                api_type = await self._test_credentials(
                    username=user_input.get(CONF_USERNAME),
                    password=user_input.get(CONF_PASSWORD),
                    url=user_input[CONF_URL],
                )
            except IntegrationBlueprintApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                errors["base"] = "auth"
            except IntegrationBlueprintApiClientCommunicationError as exception:
                LOGGER.error(exception)
                errors["base"] = "connection"
            except IntegrationBlueprintApiClientError as exception:
                LOGGER.exception(exception)
                errors["base"] = "unknown"
            else:
                url = user_input[CONF_URL]
                title = user_input.get(CONF_NAME, "").strip() or url
                connection_data = {
                    key: value for key, value in user_input.items() if key != CONF_NAME
                }
                if entry is None:
                    await self.async_set_unique_id(slugify(url))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=title,
                        data={**connection_data, CONF_API_TYPE: api_type},
                    )

                unique_id = entry.unique_id
                if url != entry.data[CONF_URL]:
                    unique_id = slugify(url)
                    existing = await self.async_set_unique_id(unique_id)
                    if existing and existing.entry_id != entry.entry_id:
                        errors["base"] = "already_configured"
                if not errors:
                    data = {
                        key: value
                        for key, value in entry.data.items()
                        if key not in (CONF_USERNAME, CONF_PASSWORD)
                    }
                    data.update(connection_data)
                    data[CONF_API_TYPE] = api_type
                    return self.async_update_reload_and_abort(
                        entry,
                        data=data,
                        unique_id=unique_id,
                        title=title,
                    )

        suggested = (
            user_input
            if user_input is not None
            else {
                **entry.data,
                CONF_NAME: (entry.title if entry.title != entry.data[CONF_URL] else ""),
            }
            if entry is not None
            else {CONF_URL: getattr(self, "_discovered_url", "http://example.com")}
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_URL): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.URL,
                            ),
                        ),
                        vol.Optional(CONF_NAME): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.TEXT,
                            ),
                        ),
                        vol.Optional(CONF_USERNAME): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.TEXT,
                            ),
                        ),
                        vol.Optional(CONF_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.PASSWORD,
                            ),
                        ),
                    },
                ),
                suggested,
            ),
            errors=errors,
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

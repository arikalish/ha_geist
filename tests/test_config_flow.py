"""Tests for Geist connection setup and reconfiguration."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import voluptuous as vol
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_geist.api import (
    IntegrationBlueprintApiClientAuthenticationError,
)
from custom_components.ha_geist.config_flow import BlueprintFlowHandler
from custom_components.ha_geist.const import API_TYPE_JSON, API_TYPE_XML, CONF_API_TYPE


class TestConfigFlow(unittest.IsolatedAsyncioTestCase):
    """Verify setup and updating an existing config entry."""

    def setUp(self) -> None:
        """Create a flow and config-entry manager."""
        self.flow = BlueprintFlowHandler()
        self.flow.flow_id = "test-flow"
        self.flow.context = {"source": "reconfigure"}
        self.manager = SimpleNamespace(
            async_update_entry=MagicMock(return_value=True),
            async_schedule_reload=MagicMock(),
        )
        self.flow.hass = SimpleNamespace(  # type: ignore[assignment]
            config_entries=self.manager
        )
        self.entry = SimpleNamespace(
            entry_id="existing-entry",
            unique_id="http-old-local",
            title="http://old.local",
            data={
                CONF_URL: "http://old.local",
                CONF_USERNAME: "old-user",
                CONF_PASSWORD: "old-password",
                CONF_API_TYPE: API_TYPE_XML,
                "other": "preserved",
            },
        )

    async def test_setup_form_order_and_optional_credentials(self) -> None:
        """URL is first, followed by an optional name and credentials."""
        self.flow.context = {"source": "user"}
        result = await self.flow.async_step_user()

        self.assertEqual(result.get("type"), FlowResultType.FORM)
        schema = result.get("data_schema")
        assert schema is not None
        markers = list(schema.schema)
        self.assertEqual(
            [str(marker) for marker in markers], ["url", "name", "username", "password"]
        )
        self.assertIsInstance(markers[0], vol.Required)
        self.assertIsInstance(markers[1], vol.Optional)
        self.assertIsInstance(markers[2], vol.Optional)
        self.assertIsInstance(markers[3], vol.Optional)
        self.assertEqual(
            schema({CONF_URL: "http://host.local"}),
            {CONF_URL: "http://host.local"},
        )

    async def test_setup_uses_name_as_entry_title(self) -> None:
        """A supplied name is shown as the config entry title."""
        self.flow.context = {"source": "user"}
        with (
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
            patch.object(self.flow, "async_set_unique_id", new_callable=AsyncMock),
            patch.object(self.flow, "_abort_if_unique_id_configured"),
        ):
            result = await self.flow.async_step_user(
                {CONF_URL: "http://host.local", CONF_NAME: " Living Room "}
            )

        self.assertEqual(result.get("title"), "Living Room")
        self.assertEqual(
            result.get("data"),
            {CONF_URL: "http://host.local", CONF_API_TYPE: API_TYPE_XML},
        )

    async def test_setup_without_name_uses_url_as_title(self) -> None:
        """The URL remains the default entry title for unnamed devices."""
        self.flow.context = {"source": "user"}
        with (
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
            patch.object(self.flow, "async_set_unique_id", new_callable=AsyncMock),
            patch.object(self.flow, "_abort_if_unique_id_configured"),
        ):
            result = await self.flow.async_step_user({CONF_URL: "http://host.local"})

        self.assertEqual(result.get("title"), "http://host.local")

    async def test_reconfigure_prefills_and_updates_connection(self) -> None:
        """Changes update the existing entry and reload it without dropping data."""
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_JSON,
            ) as validate,
            patch.object(
                self.flow,
                "async_set_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            form = await self.flow.async_step_reconfigure()
            schema = form.get("data_schema")
            assert schema is not None
            markers = list(schema.schema)
            self.assertEqual(
                [marker.description["suggested_value"] for marker in markers],
                ["http://old.local", "", "old-user", "old-password"],
            )
            result = await self.flow.async_step_reconfigure(
                {
                    CONF_URL: "http://new.local",
                    CONF_NAME: "Basement",
                    CONF_USERNAME: "new-user",
                    CONF_PASSWORD: "new-password",
                }
            )

        self.assertEqual(result.get("reason"), "reconfigure_successful")
        validate.assert_awaited_once_with(
            url="http://new.local", username="new-user", password="new-password"
        )
        self.manager.async_update_entry.assert_called_once_with(
            entry=self.entry,
            unique_id="http-new-local",
            title="Basement",
            data={
                CONF_URL: "http://new.local",
                CONF_USERNAME: "new-user",
                CONF_PASSWORD: "new-password",
                CONF_API_TYPE: API_TYPE_JSON,
                "other": "preserved",
            },
            options=ANY,
        )
        self.manager.async_schedule_reload.assert_called_once_with("existing-entry")

    async def test_reconfigure_preserves_a_manually_renamed_entry(self) -> None:
        """A manually renamed entry is suggested and can be preserved."""
        self.entry.title = "Existing custom name"
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
        ):
            form = await self.flow.async_step_reconfigure()
            schema = form.get("data_schema")
            assert schema is not None
            markers = list(schema.schema)
            self.assertEqual(
                markers[1].description["suggested_value"], "Existing custom name"
            )
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://old.local", CONF_NAME: "Existing custom name"}
            )

        self.assertEqual(result.get("reason"), "reconfigure_successful")
        self.assertEqual(
            self.manager.async_update_entry.call_args.kwargs["title"],
            "Existing custom name",
        )

    async def test_reconfigure_can_clear_name(self) -> None:
        """Clearing the optional name restores the URL as the title."""
        self.entry.title = "Existing custom name"
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
        ):
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://old.local"}
            )

        self.assertEqual(result.get("reason"), "reconfigure_successful")
        self.assertEqual(
            self.manager.async_update_entry.call_args.kwargs["title"],
            "http://old.local",
        )

    async def test_reconfigure_updates_default_title_with_url(self) -> None:
        """An unnamed entry's title follows its updated URL."""
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
            patch.object(
                self.flow,
                "async_set_unique_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://new.local"}
            )

        self.assertEqual(result.get("reason"), "reconfigure_successful")
        self.assertEqual(
            self.manager.async_update_entry.call_args.kwargs["title"],
            "http://new.local",
        )

    async def test_reconfigure_can_clear_optional_credentials(self) -> None:
        """Omitted or blank credentials are removed from the entry."""
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ) as validate,
        ):
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://old.local", CONF_USERNAME: "", CONF_PASSWORD: ""}
            )

        self.assertEqual(result.get("reason"), "reconfigure_successful")
        validate.assert_awaited_once_with(
            url="http://old.local", username=None, password=None
        )
        data = self.manager.async_update_entry.call_args.kwargs["data"]
        self.assertNotIn(CONF_USERNAME, data)
        self.assertNotIn(CONF_PASSWORD, data)
        self.assertEqual(data["other"], "preserved")

    async def test_invalid_credentials_do_not_update_entry(self) -> None:
        """Authentication errors are shown without modifying the entry."""
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                side_effect=IntegrationBlueprintApiClientAuthenticationError(
                    "bad login"
                ),
            ),
        ):
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://old.local", CONF_USERNAME: "bad-user"}
            )

        self.assertEqual(result.get("errors"), {"base": "auth"})
        self.manager.async_update_entry.assert_not_called()
        self.manager.async_schedule_reload.assert_not_called()

    async def test_reconfigure_rejects_another_entry_url(self) -> None:
        """A URL already assigned to another entry cannot be used."""
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=self.entry),
            patch.object(
                self.flow,
                "_test_credentials",
                new_callable=AsyncMock,
                return_value=API_TYPE_XML,
            ),
            patch.object(
                self.flow,
                "async_set_unique_id",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(entry_id="other-entry"),
            ),
        ):
            result = await self.flow.async_step_reconfigure(
                {CONF_URL: "http://other.local"}
            )

        self.assertEqual(result.get("errors"), {"base": "already_configured"})
        self.manager.async_update_entry.assert_not_called()

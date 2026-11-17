# Geist Home Assistant integration

This custom integration supports Geist devices that expose either the legacy
XML endpoint or the newer token-authenticated JSON API.

## Setup

Add **HA Geist** from **Settings → Devices & services → Add integration**.
Enter the device URL (for example, `http://192.168.1.50`) and an account
supported by the device. The integration detects the API type while validating
the connection. Devices with known Geist MAC address prefixes can be offered
through DHCP discovery; manual host entry remains available.

Use an account with control permissions if you want to operate relays. The
integration keeps the API token in memory and stores the configured username
and password in the Home Assistant config entry.

## Entities

The legacy XML API creates sensors from each device's `<field>` elements. The
JSON API creates sensors from device measurements and analog inputs, and a
switch for each relay. Relay switches send explicit on/off commands and refresh
the device state afterward. Data is polled every 30 seconds. Debug logs summarize
JSON device, measurement, relay and alarm counts without logging credentials.

Configured alarms are exposed as read-only sensors. Their names include reported
thresholds, and their entity IDs include the target, comparison type, and alarm
slot (or channel on older XML devices).
XML and JSON alarm sensors are added and removed as device configuration changes.
For JSON devices, a control-enabled account also exposes an acknowledgement
button for each alarm. It is available while the alarm is tripped or
latched and does not resolve the underlying fault.
Changing an alarm identity creates a new entity ID; update automations that
refer to the old one. Digital inputs from the JSON API are binary sensors.
JSON measurement and digital input entity IDs use the device ID and measurement
type, with numeric suffixes for duplicates. Known measurement names follow the
XML naming style. Temperature and dewpoint sensors provide both Celsius and
Fahrenheit when the source unit is known; converted values are rounded to two
decimal places. Relay IDs retain their relay key.
Existing default IDs are renamed on upgrade; review automations or dashboards
referring to the previous IDs.

SNMP discovery is not currently used. Discovery does not scan network ranges.

## Development

Run `scripts/develop` to start the local Home Assistant instance. Run
`ruff check .` and `pytest` to lint and test the integration.

## Repository notes

This repository also includes scaffolding used for local development and
testing.

## Repository layout

The integration code lives in `custom_components/ha_geist`; development and
Home Assistant configuration files are included for local testing.

## What?

This repository contains multiple files, here is a overview:

File | Purpose | Documentation
-- | -- | --
`.devcontainer.json` | Used for development/testing with Visual Studio Code. | [Documentation](https://code.visualstudio.com/docs/remote/containers)
`.github/ISSUE_TEMPLATE/*.yml` | Templates for the issue tracker | [Documentation](https://help.github.com/en/github/building-a-strong-community/configuring-issue-templates-for-your-repository)
`custom_components/ha_geist/*` | Integration files, this is where everything happens. | [Documentation](https://developers.home-assistant.io/docs/creating_component_index)
`CONTRIBUTING.md` | Guidelines on how to contribute. | [Documentation](https://help.github.com/en/github/building-a-strong-community/setting-guidelines-for-repository-contributors)
`LICENSE` | The license file for the project. | [Documentation](https://help.github.com/en/github/creating-cloning-and-archiving-repositories/licensing-a-repository)
`README.md` | The file you are reading now, should contain info about the integration, installation and configuration instructions. | [Documentation](https://help.github.com/en/github/writing-on-github/basic-writing-and-formatting-syntax)
`requirements.txt` | Python packages used for development/lint/testing this integration. | [Documentation](https://pip.pypa.io/en/stable/user_guide/#requirements-files)

## How?

1. Create a new repository in GitHub, using this repository as a template by clicking the "Use this template" button in the GitHub UI.
1. Open your new repository in Visual Studio Code devcontainer (Preferably with the "`Dev Containers: Clone Repository in Named Container Volume...`" option).
1. Rename all instances of the `ha_geist` to `custom_components/<your_integration_domain>` (e.g. `custom_components/awesome_integration`).
1. Rename all instances of the `HA Geist` to `<Your Integration Name>` (e.g. `Awesome Integration`).
1. Run the `scripts/develop` to start HA and test out your new integration.

## Next steps

These are some next steps you may want to look into:
- Add tests to your integration, [`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) can help you get started.
- Add brand images (logo/icon) to https://github.com/home-assistant/brands.
- Create your first release.
- Share your integration on the [Home Assistant Forum](https://community.home-assistant.io/).
- Submit your integration to [HACS](https://hacs.xyz/docs/publish/start).

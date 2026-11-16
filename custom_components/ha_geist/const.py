"""Constants for ha_geist."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "ha_geist"
ATTRIBUTION = "Data provided by Geist device"
CONF_API_TYPE = "api_type"
API_TYPE_XML = "xml"
API_TYPE_JSON = "json"

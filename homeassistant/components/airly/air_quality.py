"""Support for the Airly air_quality service."""
from __future__ import annotations

from typing import Any

from homeassistant.components.air_quality import (
    ATTR_AQI,
    ATTR_PM_2_5,
    ATTR_PM_10,
    AirQualityEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AirlyDataUpdateCoordinator
from .const import (
    ATTR_API_ADVICE,
    ATTR_API_CAQI,
    ATTR_API_CAQI_DESCRIPTION,
    ATTR_API_CAQI_LEVEL,
    ATTR_API_PM10,
    ATTR_API_PM10_LIMIT,
    ATTR_API_PM10_PERCENT,
    ATTR_API_PM25,
    ATTR_API_PM25_LIMIT,
    ATTR_API_PM25_PERCENT,
    ATTRIBUTION,
    DEFAULT_NAME,
    DOMAIN,
    LABEL_ADVICE,
    MANUFACTURER,
)

LABEL_AQI_DESCRIPTION = f"{ATTR_AQI}_description"
LABEL_AQI_LEVEL = f"{ATTR_AQI}_level"
LABEL_PM_2_5_LIMIT = f"{ATTR_PM_2_5}_limit"
LABEL_PM_2_5_PERCENT = f"{ATTR_PM_2_5}_percent_of_limit"
LABEL_PM_10_LIMIT = f"{ATTR_PM_10}_limit"
LABEL_PM_10_PERCENT = f"{ATTR_PM_10}_percent_of_limit"

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Airly air_quality entity based on a config entry."""
    name = entry.data[CONF_NAME]

    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([AirlyAirQuality(coordinator, name)], False)


class AirlyAirQuality(CoordinatorEntity, AirQualityEntity):
    """Define an Airly air quality."""

    coordinator: AirlyDataUpdateCoordinator

    def __init__(self, coordinator: AirlyDataUpdateCoordinator, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_icon = "mdi:blur"
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.latitude}-{coordinator.longitude}"

    @property
    def air_quality_index(self) -> float | None:
        """Return the air quality index."""
        return round_state(self.coordinator.data[ATTR_API_CAQI])

    @property
    def particulate_matter_2_5(self) -> float | None:
        """Return the particulate matter 2.5 level."""
        return round_state(self.coordinator.data.get(ATTR_API_PM25))

    @property
    def particulate_matter_10(self) -> float | None:
        """Return the particulate matter 10 level."""
        return round_state(self.coordinator.data.get(ATTR_API_PM10))

    @property
    def attribution(self) -> str:
        """Return the attribution."""
        return ATTRIBUTION

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return {
            "identifiers": {
                (
                    DOMAIN,
                    f"{self.coordinator.latitude}-{self.coordinator.longitude}",
                )
            },
            "name": DEFAULT_NAME,
            "manufacturer": MANUFACTURER,
            "entry_type": "service",
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs = {
            LABEL_AQI_DESCRIPTION: self.coordinator.data[ATTR_API_CAQI_DESCRIPTION],
            LABEL_ADVICE: self.coordinator.data[ATTR_API_ADVICE],
            LABEL_AQI_LEVEL: self.coordinator.data[ATTR_API_CAQI_LEVEL],
        }
        if ATTR_API_PM25 in self.coordinator.data:
            attrs[LABEL_PM_2_5_LIMIT] = self.coordinator.data[ATTR_API_PM25_LIMIT]
            attrs[LABEL_PM_2_5_PERCENT] = round(
                self.coordinator.data[ATTR_API_PM25_PERCENT]
            )
        if ATTR_API_PM10 in self.coordinator.data:
            attrs[LABEL_PM_10_LIMIT] = self.coordinator.data[ATTR_API_PM10_LIMIT]
            attrs[LABEL_PM_10_PERCENT] = round(
                self.coordinator.data[ATTR_API_PM10_PERCENT]
            )
        return attrs


def round_state(state: float | None) -> float | None:
    """Round state."""
    return round(state) if state else state

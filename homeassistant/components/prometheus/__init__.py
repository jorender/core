"""Support for Prometheus metrics export."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import astuple, dataclass
import logging
import string
from typing import Any, cast

from aiohttp import web
import prometheus_client
from prometheus_client.metrics import MetricWrapperBase
import voluptuous as vol

from homeassistant import core as hacore
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    HVACAction,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
)
from homeassistant.components.fan import (
    ATTR_DIRECTION,
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
)
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.humidifier import ATTR_AVAILABLE_MODES, ATTR_HUMIDITY
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_MODE,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    CONTENT_TYPE_TEXT_PLAIN,
    EVENT_STATE_CHANGED,
    PERCENTAGE,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_ON,
    STATE_OPEN,
    STATE_OPENING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State
from homeassistant.helpers import (
    config_validation as cv,
    entityfilter,
    state as state_helper,
)
from homeassistant.helpers.entity_registry import (
    EVENT_ENTITY_REGISTRY_UPDATED,
    EventEntityRegistryUpdatedData,
)
from homeassistant.helpers.entity_values import EntityValues
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.dt import as_timestamp
from homeassistant.util.unit_conversion import TemperatureConverter

_LOGGER = logging.getLogger(__name__)

API_ENDPOINT = "/api/prometheus"
IGNORED_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


DOMAIN = "prometheus"
CONF_FILTER = "filter"
CONF_REQUIRES_AUTH = "requires_auth"
CONF_PROM_NAMESPACE = "namespace"
CONF_COMPONENT_CONFIG = "component_config"
CONF_COMPONENT_CONFIG_GLOB = "component_config_glob"
CONF_COMPONENT_CONFIG_DOMAIN = "component_config_domain"
CONF_DEFAULT_METRIC = "default_metric"
CONF_OVERRIDE_METRIC = "override_metric"
COMPONENT_CONFIG_SCHEMA_ENTRY = vol.Schema(
    {vol.Optional(CONF_OVERRIDE_METRIC): cv.string}
)
ALLOWED_METRIC_CHARS = set(string.ascii_letters + string.digits + "_:")

DEFAULT_NAMESPACE = "homeassistant"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            {
                vol.Optional(CONF_FILTER, default={}): entityfilter.FILTER_SCHEMA,
                vol.Optional(CONF_PROM_NAMESPACE, default=DEFAULT_NAMESPACE): cv.string,
                vol.Optional(CONF_REQUIRES_AUTH, default=True): cv.boolean,
                vol.Optional(CONF_DEFAULT_METRIC): cv.string,
                vol.Optional(CONF_OVERRIDE_METRIC): cv.string,
                vol.Optional(CONF_COMPONENT_CONFIG, default={}): vol.Schema(
                    {cv.entity_id: COMPONENT_CONFIG_SCHEMA_ENTRY}
                ),
                vol.Optional(CONF_COMPONENT_CONFIG_GLOB, default={}): vol.Schema(
                    {cv.string: COMPONENT_CONFIG_SCHEMA_ENTRY}
                ),
                vol.Optional(CONF_COMPONENT_CONFIG_DOMAIN, default={}): vol.Schema(
                    {cv.string: COMPONENT_CONFIG_SCHEMA_ENTRY}
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Activate Prometheus component."""
    hass.http.register_view(PrometheusView(config[DOMAIN][CONF_REQUIRES_AUTH]))

    conf: dict[str, Any] = config[DOMAIN]
    entity_filter: entityfilter.EntityFilter = conf[CONF_FILTER]
    namespace: str = conf[CONF_PROM_NAMESPACE]
    climate_units = hass.config.units.temperature_unit
    override_metric: str | None = conf.get(CONF_OVERRIDE_METRIC)
    default_metric: str | None = conf.get(CONF_DEFAULT_METRIC)
    component_config = EntityValues(
        conf[CONF_COMPONENT_CONFIG],
        conf[CONF_COMPONENT_CONFIG_DOMAIN],
        conf[CONF_COMPONENT_CONFIG_GLOB],
    )

    metrics = PrometheusMetrics(
        entity_filter,
        namespace,
        climate_units,
        component_config,
        override_metric,
        default_metric,
    )

    hass.bus.listen(EVENT_STATE_CHANGED, metrics.handle_state_changed_event)
    hass.bus.listen(
        EVENT_ENTITY_REGISTRY_UPDATED,
        metrics.handle_entity_registry_updated,
    )

    for state in hass.states.all():
        if entity_filter(state.entity_id):
            metrics.handle_state(state)

    return True


@dataclass(frozen=True, slots=True)
class MetricNameWithLabelValues:
    """Class to represent a metric with its label values.

    The prometheus client library doesn't easily allow us to get back the
    information we put into it. Specifically, it is very expensive to query
    which label values have been set for metrics.

    This class is used to hold a bit of data we need to efficiently remove
    labelsets from metrics.
    """

    metric_name: str
    label_values: tuple[str, ...]


class PrometheusMetrics:
    """Model all of the metrics which should be exposed to Prometheus."""

    def __init__(
        self,
        entity_filter: entityfilter.EntityFilter,
        namespace: str,
        climate_units: UnitOfTemperature,
        component_config: EntityValues,
        override_metric: str | None,
        default_metric: str | None,
    ) -> None:
        """Initialize Prometheus Metrics."""
        self._component_config = component_config
        self._override_metric = override_metric
        self._default_metric = default_metric
        self._filter = entity_filter
        self._sensor_metric_handlers: list[
            Callable[[State, str | None], str | None]
        ] = [
            self._sensor_override_component_metric,
            self._sensor_override_metric,
            self._sensor_timestamp_metric,
            self._sensor_attribute_metric,
            self._sensor_default_metric,
            self._sensor_fallback_metric,
        ]

        if namespace:
            self.metrics_prefix = f"{namespace}_"
        else:
            self.metrics_prefix = ""
        self._metrics: dict[str, MetricWrapperBase] = {}
        self._metrics_by_entity_id: dict[str, set[MetricNameWithLabelValues]] = (
            defaultdict(set)
        )
        self._climate_units = climate_units

    def handle_state_changed_event(self, event: Event[EventStateChangedData]) -> None:
        """Handle new messages from the bus."""
        if (state := event.data.get("new_state")) is None:
            return

        if not self._filter(state.entity_id):
            _LOGGER.debug("Filtered out entity %s", state.entity_id)
            return

        if (
            old_state := event.data.get("old_state")
        ) is not None and old_state.attributes.get(
            ATTR_FRIENDLY_NAME
        ) != state.attributes.get(ATTR_FRIENDLY_NAME):
            self._remove_labelsets(old_state.entity_id)

        self.handle_state(state)

    def handle_state(self, state: State) -> None:
        """Add/update a state in Prometheus."""
        entity_id = state.entity_id
        _LOGGER.debug("Handling state update for %s", entity_id)

        labels = self._labels(state)

        self._metric(
            "state_change",
            prometheus_client.Counter,
            "The number of state changes",
            labels,
        ).inc()

        self._metric(
            "entity_available",
            prometheus_client.Gauge,
            "Entity is available (not in the unavailable or unknown state)",
            labels,
        ).set(float(state.state not in IGNORED_STATES))

        self._metric(
            "last_updated_time_seconds",
            prometheus_client.Gauge,
            "The last_updated timestamp",
            labels,
        ).set(state.last_updated.timestamp())

        if state.state in IGNORED_STATES:
            self._remove_labelsets(
                entity_id,
                {"state_change", "entity_available", "last_updated_time_seconds"},
            )
        else:
            domain, _ = hacore.split_entity_id(entity_id)
            handler = f"_handle_{domain}"
            if hasattr(self, handler) and state.state:
                getattr(self, handler)(state)

    def handle_entity_registry_updated(
        self, event: Event[EventEntityRegistryUpdatedData]
    ) -> None:
        """Listen for deleted, disabled or renamed entities and remove them from the Prometheus Registry."""
        if event.data["action"] in (None, "create"):
            return

        entity_id = event.data.get("entity_id")
        _LOGGER.debug("Handling entity update for %s", entity_id)

        metrics_entity_id: str | None = None

        if event.data["action"] == "remove":
            metrics_entity_id = entity_id
        elif event.data["action"] == "update":
            changes = event.data["changes"]

            if "entity_id" in changes:
                metrics_entity_id = changes["entity_id"]
            elif "disabled_by" in changes:
                metrics_entity_id = entity_id

        if metrics_entity_id:
            self._remove_labelsets(metrics_entity_id)

    def _remove_labelsets(
        self,
        entity_id: str,
        ignored_metric_names: set[str] | None = None,
    ) -> None:
        """Remove labelsets matching the given entity id from all non-ignored metrics."""
        if ignored_metric_names is None:
            ignored_metric_names = set()
        metric_set = self._metrics_by_entity_id[entity_id]
        removed_metrics = set()
        for metric in metric_set:
            metric_name, label_values = astuple(metric)
            if metric_name in ignored_metric_names:
                continue

            _LOGGER.debug(
                "Removing labelset %s from %s for entity_id: %s",
                label_values,
                metric_name,
                entity_id,
            )
            removed_metrics.add(metric)
            self._metrics[metric_name].remove(*label_values)
        metric_set -= removed_metrics
        if not metric_set:
            del self._metrics_by_entity_id[entity_id]

    def _handle_attributes(self, state: State) -> None:
        for key, value in state.attributes.items():
            try:
                value = float(value)
            except (ValueError, TypeError):
                continue

            self._metric(
                f"{state.domain}_attr_{key.lower()}",
                prometheus_client.Gauge,
                f"{key} attribute of {state.domain} entity",
                self._labels(state),
            ).set(value)

    def _metric[_MetricBaseT: MetricWrapperBase](
        self,
        metric_name: str,
        factory: type[_MetricBaseT],
        documentation: str,
        labels: dict[str, str],
    ) -> _MetricBaseT:
        try:
            metric = cast(_MetricBaseT, self._metrics[metric_name])
        except KeyError:
            full_metric_name = self._sanitize_metric_name(
                f"{self.metrics_prefix}{metric_name}"
            )
            self._metrics[metric_name] = factory(
                full_metric_name,
                documentation,
                labels.keys(),
                registry=prometheus_client.REGISTRY,
            )
            metric = cast(_MetricBaseT, self._metrics[metric_name])
        self._metrics_by_entity_id[labels["entity"]].add(
            MetricNameWithLabelValues(metric_name, tuple(labels.values()))
        )
        return metric.labels(**labels)

    @staticmethod
    def _sanitize_metric_name(metric: str) -> str:
        return "".join(
            [c if c in ALLOWED_METRIC_CHARS else f"u{hex(ord(c))}" for c in metric]
        )

    @staticmethod
    def state_as_number(state: State) -> float | None:
        """Return state as a float, or None if state cannot be converted."""
        try:
            if state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.TIMESTAMP:
                value = as_timestamp(state.state)
            else:
                value = state_helper.state_as_number(state)
        except ValueError:
            _LOGGER.debug("Could not convert %s to float", state)
            value = None
        return value

    @staticmethod
    def _labels(
        state: State,
        extra_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if extra_labels is None:
            extra_labels = {}
        labels = {
            "entity": state.entity_id,
            "domain": state.domain,
            "friendly_name": state.attributes.get(ATTR_FRIENDLY_NAME),
        }
        if not labels.keys().isdisjoint(extra_labels.keys()):
            conflicting_keys = labels.keys() & extra_labels.keys()
            raise ValueError(
                f"extra_labels contains conflicting keys: {conflicting_keys}"
            )
        return labels | extra_labels

    def _battery(self, state: State) -> None:
        if (battery_level := state.attributes.get(ATTR_BATTERY_LEVEL)) is None:
            return

        try:
            value = float(battery_level)
        except ValueError:
            return

        self._metric(
            "battery_level_percent",
            prometheus_client.Gauge,
            "Battery level as a percentage of its capacity",
            self._labels(state),
        ).set(value)

    def _handle_binary_sensor(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "binary_sensor_state",
            prometheus_client.Gauge,
            "State of the binary sensor (0/1)",
            self._labels(state),
        ).set(value)

    def _handle_input_boolean(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "input_boolean_state",
            prometheus_client.Gauge,
            "State of the input boolean (0/1)",
            self._labels(state),
        ).set(value)

    def _numeric_handler(self, state: State, domain: str, title: str) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        if unit := self._unit_string(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)):
            metric = self._metric(
                f"{domain}_state_{unit}",
                prometheus_client.Gauge,
                f"State of the {title} measured in {unit}",
                self._labels(state),
            )
        else:
            metric = self._metric(
                f"{domain}_state",
                prometheus_client.Gauge,
                f"State of the {title}",
                self._labels(state),
            )

        if (
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            == UnitOfTemperature.FAHRENHEIT
        ):
            value = TemperatureConverter.convert(
                value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )

        metric.set(value)

    def _handle_input_number(self, state: State) -> None:
        self._numeric_handler(state, "input_number", "input number")

    def _handle_number(self, state: State) -> None:
        self._numeric_handler(state, "number", "number")

    def _handle_device_tracker(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "device_tracker_state",
            prometheus_client.Gauge,
            "State of the device tracker (0/1)",
            self._labels(state),
        ).set(value)

    def _handle_person(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "person_state",
            prometheus_client.Gauge,
            "State of the person (0/1)",
            self._labels(state),
        ).set(value)

    def _handle_cover(self, state: State) -> None:
        cover_states = [STATE_CLOSED, STATE_CLOSING, STATE_OPEN, STATE_OPENING]
        for cover_state in cover_states:
            metric = self._metric(
                "cover_state",
                prometheus_client.Gauge,
                "State of the cover (0/1)",
                self._labels(state, {"state": cover_state}),
            )
            metric.set(float(cover_state == state.state))

        position = state.attributes.get(ATTR_CURRENT_POSITION)
        if position is not None:
            self._metric(
                "cover_position",
                prometheus_client.Gauge,
                "Position of the cover (0-100)",
                self._labels(state),
            ).set(float(position))

        tilt_position = state.attributes.get(ATTR_CURRENT_TILT_POSITION)
        if tilt_position is not None:
            self._metric(
                "cover_tilt_position",
                prometheus_client.Gauge,
                "Tilt Position of the cover (0-100)",
                self._labels(state),
            ).set(float(tilt_position))

    def _handle_light(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        brightness = state.attributes.get(ATTR_BRIGHTNESS)
        if state.state == STATE_ON and brightness is not None:
            value = float(brightness) / 255.0
        value = value * 100

        self._metric(
            "light_brightness_percent",
            prometheus_client.Gauge,
            "Light brightness percentage (0..100)",
            self._labels(state),
        ).set(value)

    def _handle_lock(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "lock_state",
            prometheus_client.Gauge,
            "State of the lock (0/1)",
            self._labels(state),
        ).set(value)

    def _handle_climate_temp(
        self, state: State, attr: str, metric_name: str, metric_description: str
    ) -> None:
        if (temp := state.attributes.get(attr)) is None:
            return

        if self._climate_units == UnitOfTemperature.FAHRENHEIT:
            temp = TemperatureConverter.convert(
                temp, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        self._metric(
            metric_name,
            prometheus_client.Gauge,
            metric_description,
            self._labels(state),
        ).set(temp)

    def _handle_climate(self, state: State) -> None:
        self._handle_climate_temp(
            state,
            ATTR_TEMPERATURE,
            "climate_target_temperature_celsius",
            "Target temperature in degrees Celsius",
        )
        self._handle_climate_temp(
            state,
            ATTR_TARGET_TEMP_HIGH,
            "climate_target_temperature_high_celsius",
            "Target high temperature in degrees Celsius",
        )
        self._handle_climate_temp(
            state,
            ATTR_TARGET_TEMP_LOW,
            "climate_target_temperature_low_celsius",
            "Target low temperature in degrees Celsius",
        )
        self._handle_climate_temp(
            state,
            ATTR_CURRENT_TEMPERATURE,
            "climate_current_temperature_celsius",
            "Current temperature in degrees Celsius",
        )

        if current_action := state.attributes.get(ATTR_HVAC_ACTION):
            for action in HVACAction:
                self._metric(
                    "climate_action",
                    prometheus_client.Gauge,
                    "HVAC action",
                    self._labels(state, {"action": action.value}),
                ).set(float(action == current_action))

        current_mode = state.state
        available_modes = state.attributes.get(ATTR_HVAC_MODES)
        if current_mode and available_modes:
            for mode in available_modes:
                self._metric(
                    "climate_mode",
                    prometheus_client.Gauge,
                    "HVAC mode",
                    self._labels(state, {"mode": mode}),
                ).set(float(mode == current_mode))

        preset_mode = state.attributes.get(ATTR_PRESET_MODE)
        available_preset_modes = state.attributes.get(ATTR_PRESET_MODES)
        if preset_mode and available_preset_modes:
            for mode in available_preset_modes:
                self._metric(
                    "climate_preset_mode",
                    prometheus_client.Gauge,
                    "Preset mode enum",
                    self._labels(state, {"mode": mode}),
                ).set(float(mode == preset_mode))

        fan_mode = state.attributes.get(ATTR_FAN_MODE)
        available_fan_modes = state.attributes.get(ATTR_FAN_MODES)
        if fan_mode and available_fan_modes:
            for mode in available_fan_modes:
                self._metric(
                    "climate_fan_mode",
                    prometheus_client.Gauge,
                    "Fan mode enum",
                    self._labels(state, {"mode": mode}),
                ).set(float(mode == fan_mode))

    def _handle_humidifier(self, state: State) -> None:
        humidifier_target_humidity_percent = state.attributes.get(ATTR_HUMIDITY)
        if humidifier_target_humidity_percent:
            self._metric(
                "humidifier_target_humidity_percent",
                prometheus_client.Gauge,
                "Target Relative Humidity",
                self._labels(state),
            ).set(humidifier_target_humidity_percent)

        if (value := self.state_as_number(state)) is not None:
            self._metric(
                "humidifier_state",
                prometheus_client.Gauge,
                "State of the humidifier (0/1)",
                self._labels(state),
            ).set(value)

        current_mode = state.attributes.get(ATTR_MODE)
        available_modes = state.attributes.get(ATTR_AVAILABLE_MODES)
        if current_mode and available_modes:
            for mode in available_modes:
                self._metric(
                    "humidifier_mode",
                    prometheus_client.Gauge,
                    "Humidifier Mode",
                    self._labels(state, {"mode": mode}),
                ).set(float(mode == current_mode))

    def _handle_sensor(self, state: State) -> None:
        unit = self._unit_string(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

        for metric_handler in self._sensor_metric_handlers:
            metric = metric_handler(state, unit)
            if metric is not None:
                break

        if metric is not None and (value := self.state_as_number(state)) is not None:
            documentation = "State of the sensor"
            if unit:
                documentation = f"Sensor data measured in {unit}"

            if (
                state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                == UnitOfTemperature.FAHRENHEIT
            ):
                value = TemperatureConverter.convert(
                    value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
                )
            self._metric(
                metric,
                prometheus_client.Gauge,
                documentation,
                self._labels(state),
            ).set(value)

        self._battery(state)

    def _sensor_default_metric(self, state: State, unit: str | None) -> str | None:
        """Get default metric."""
        return self._default_metric

    @staticmethod
    def _sensor_attribute_metric(state: State, unit: str | None) -> str | None:
        """Get metric based on device class attribute."""
        metric = state.attributes.get(ATTR_DEVICE_CLASS)
        if metric is not None:
            return f"sensor_{metric}_{unit}"
        return None

    @staticmethod
    def _sensor_timestamp_metric(state: State, unit: str | None) -> str | None:
        """Get metric for timestamp sensors, which have no unit of measurement attribute."""
        metric = state.attributes.get(ATTR_DEVICE_CLASS)
        if metric == SensorDeviceClass.TIMESTAMP:
            return f"sensor_{metric}_seconds"
        return None

    def _sensor_override_metric(self, state: State, unit: str | None) -> str | None:
        """Get metric from override in configuration."""
        if self._override_metric:
            return self._override_metric
        return None

    def _sensor_override_component_metric(
        self, state: State, unit: str | None
    ) -> str | None:
        """Get metric from override in component configuration."""
        return self._component_config.get(state.entity_id).get(CONF_OVERRIDE_METRIC)

    @staticmethod
    def _sensor_fallback_metric(state: State, unit: str | None) -> str | None:
        """Get metric from fallback logic for compatibility."""
        if unit not in (None, ""):
            return f"sensor_unit_{unit}"
        return "sensor_state"

    @staticmethod
    def _unit_string(unit: str | None) -> str | None:
        """Get a formatted string of the unit."""
        if unit is None:
            return None

        units = {
            UnitOfTemperature.CELSIUS: "celsius",
            UnitOfTemperature.FAHRENHEIT: "celsius",  # F should go into C metric
            PERCENTAGE: "percent",
        }
        default = unit.replace("/", "_per_")
        default = default.lower()
        return units.get(unit, default)

    def _handle_switch(self, state: State) -> None:
        if (value := self.state_as_number(state)) is not None:
            self._metric(
                "switch_state",
                prometheus_client.Gauge,
                "State of the switch (0/1)",
                self._labels(state),
            ).set(value)

        self._handle_attributes(state)

    def _handle_fan(self, state: State) -> None:
        if (value := self.state_as_number(state)) is not None:
            self._metric(
                "fan_state",
                prometheus_client.Gauge,
                "State of the fan (0/1)",
                self._labels(state),
            ).set(value)

        fan_speed_percent = state.attributes.get(ATTR_PERCENTAGE)
        if fan_speed_percent is not None:
            self._metric(
                "fan_speed_percent",
                prometheus_client.Gauge,
                "Fan speed percent (0-100)",
                self._labels(state),
            ).set(float(fan_speed_percent))

        fan_is_oscillating = state.attributes.get(ATTR_OSCILLATING)
        if fan_is_oscillating is not None:
            self._metric(
                "fan_is_oscillating",
                prometheus_client.Gauge,
                "Whether the fan is oscillating (0/1)",
                self._labels(state),
            ).set(float(fan_is_oscillating))

        fan_preset_mode = state.attributes.get(ATTR_PRESET_MODE)
        available_modes = state.attributes.get(ATTR_PRESET_MODES)
        if fan_preset_mode and available_modes:
            for mode in available_modes:
                self._metric(
                    "fan_preset_mode",
                    prometheus_client.Gauge,
                    "Fan preset mode enum",
                    self._labels(state, {"mode": mode}),
                ).set(float(mode == fan_preset_mode))

        fan_direction = state.attributes.get(ATTR_DIRECTION)
        if fan_direction in {DIRECTION_FORWARD, DIRECTION_REVERSE}:
            self._metric(
                "fan_direction_reversed",
                prometheus_client.Gauge,
                "Fan direction reversed (bool)",
                self._labels(state),
            ).set(float(fan_direction == DIRECTION_REVERSE))

    def _handle_zwave(self, state: State) -> None:
        self._battery(state)

    def _handle_automation(self, state: State) -> None:
        self._metric(
            "automation_triggered_count",
            prometheus_client.Counter,
            "Count of times an automation has been triggered",
            self._labels(state),
        ).inc()

    def _handle_counter(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "counter_value",
            prometheus_client.Gauge,
            "Value of counter entities",
            self._labels(state),
        ).set(value)

    def _handle_update(self, state: State) -> None:
        if (value := self.state_as_number(state)) is None:
            return

        self._metric(
            "update_state",
            prometheus_client.Gauge,
            "Update state, indicating if an update is available (0/1)",
            self._labels(state),
        ).set(value)

    def _handle_alarm_control_panel(self, state: State) -> None:
        current_state = state.state

        if current_state:
            for alarm_state in AlarmControlPanelState:
                self._metric(
                    "alarm_control_panel_state",
                    prometheus_client.Gauge,
                    "State of the alarm control panel (0/1)",
                    self._labels(state, {"state": alarm_state.value}),
                ).set(float(alarm_state.value == current_state))


class PrometheusView(HomeAssistantView):
    """Handle Prometheus requests."""

    url = API_ENDPOINT
    name = "api:prometheus"

    def __init__(self, requires_auth: bool) -> None:
        """Initialize Prometheus view."""
        self.requires_auth = requires_auth

    async def get(self, request: web.Request) -> web.Response:
        """Handle request for Prometheus metrics."""
        _LOGGER.debug("Received Prometheus metrics request")

        hass = request.app[KEY_HASS]
        body = await hass.async_add_executor_job(
            prometheus_client.generate_latest, prometheus_client.REGISTRY
        )
        return web.Response(
            body=body,
            content_type=CONTENT_TYPE_TEXT_PLAIN,
        )

"""Decision binary sensors, for automations and dashboards."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SCENARIO_EXPECTED, SMOKE_PM25
from .coordinator import OffgridConfigEntry, PlannerData, air_window
from .entity import OffgridEntity


@dataclass(frozen=True, kw_only=True)
class PlannerBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[PlannerData], bool | None]


def _planning(d: PlannerData):
    return d.plan.scenarios[d.plan.planning_scenario]


def _smoke(d: PlannerData) -> bool | None:
    """PM2.5 forecast to reach the smoke threshold within 24 h. Unknown (not off) without a current forecast."""
    pm = [p.pm2_5 for p in air_window(d) if p.pm2_5 is not None]
    return max(pm) >= SMOKE_PM25 if pm else None


BINARY_SENSORS = (
    PlannerBinaryDescription(key="tilt_recommended",
                             value=lambda d: d.plan.scenarios[SCENARIO_EXPECTED].tilt_recommended),
    PlannerBinaryDescription(key="shed_needed", value=lambda d: bool(_planning(d).shed_steps)),
    PlannerBinaryDescription(key="generator_needed", value=lambda d: _planning(d).generator_needed),
    PlannerBinaryDescription(key="smoke_forecast", device_class=BinarySensorDeviceClass.SMOKE, value=_smoke),
)


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(PlannerBinarySensor(entry.runtime_data, desc) for desc in BINARY_SENSORS)


class PlannerBinarySensor(OffgridEntity, BinarySensorEntity):
    entity_description: PlannerBinaryDescription

    def __init__(self, coordinator, description: PlannerBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data
        return None if d is None else self.entity_description.value(d)

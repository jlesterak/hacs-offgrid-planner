"""Decision binary sensors, for automations and dashboards."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SCENARIO_EXPECTED
from .coordinator import OffgridConfigEntry, PlannerData
from .entity import OffgridEntity


@dataclass(frozen=True, kw_only=True)
class PlannerBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[PlannerData], bool]


def _planning(d: PlannerData):
    return d.plan.scenarios[d.plan.planning_scenario]


BINARY_SENSORS = (
    PlannerBinaryDescription(key="tilt_recommended",
                             value=lambda d: d.plan.scenarios[SCENARIO_EXPECTED].tilt_recommended),
    PlannerBinaryDescription(key="shed_needed", value=lambda d: bool(_planning(d).shed_steps)),
    PlannerBinaryDescription(key="generator_needed", value=lambda d: _planning(d).generator_needed),
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

"""Runtime-adjustable planner settings (restored across restarts)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntityDescription, NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NUMBER_DEFAULTS, NUMBER_RESERVE, NUMBER_SITE_HORIZON
from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .entity import OffgridEntity


@dataclass(frozen=True, kw_only=True)
class PlannerNumberDescription(NumberEntityDescription):
    pass


NUMBERS = (
    PlannerNumberDescription(key=NUMBER_RESERVE, native_min_value=5, native_max_value=80, native_step=1,
                             native_unit_of_measurement=PERCENTAGE, mode=NumberMode.BOX,
                             entity_category=EntityCategory.CONFIG),
    # 0.5 = open desert horizon, 1 = typical camp, 1.5+ = trees or canyon walls.
    PlannerNumberDescription(key=NUMBER_SITE_HORIZON, native_min_value=0, native_max_value=2, native_step=0.05,
                             mode=NumberMode.SLIDER, entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(PlannerNumber(entry.runtime_data, desc) for desc in NUMBERS)


class PlannerNumber(OffgridEntity, RestoreNumber):
    entity_description: PlannerNumberDescription

    def __init__(self, coordinator: OffgridCoordinator, description: PlannerNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        value = last.native_value if last and last.native_value is not None else NUMBER_DEFAULTS[self.key]
        self.coordinator.numbers[self.key] = float(value)

    @property
    def key(self) -> str:
        return self.entity_description.key

    @property
    def available(self) -> bool:
        return True  # settings stay editable even when planning fails

    @property
    def native_value(self) -> float:
        return self.coordinator.numbers[self.key]

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_number(self.key, value)
        self.async_write_ha_state()

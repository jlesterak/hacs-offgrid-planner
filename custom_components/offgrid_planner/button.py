"""Start and finish learning a load; set the baseline from last night."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .entity import OffgridEntity


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LearnStartButton(coordinator), LearnFinishButton(coordinator),
                        BaselineFromLastNightButton(coordinator)])


class _LearnButton(OffgridEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return True


class LearnStartButton(_LearnButton):
    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "learn_start")

    async def async_press(self) -> None:
        self.coordinator.async_learn_start()


class LearnFinishButton(_LearnButton):
    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "learn_finish")

    async def async_press(self) -> None:
        await self.coordinator.async_learn_finish()


class BaselineFromLastNightButton(OffgridEntity, ButtonEntity):
    """Moves last night's model gap into the always-on baseline (only while a night check exists)."""

    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "baseline_from_last_night")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.suggested_baseline_w() is not None

    async def async_press(self) -> None:
        await self.coordinator.async_baseline_from_last_night()

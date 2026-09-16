"""Choose which shed-list load to learn, and how."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .core.learn import Mode
from .entity import OffgridEntity


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LearnLoadSelect(coordinator), LearnModeSelect(coordinator)])


class _LearnSelect(OffgridEntity, SelectEntity):
    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_shed_listener(self.async_write_ha_state))


class LearnLoadSelect(_LearnSelect):
    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "learn_load")

    @property
    def options(self) -> list[str]:
        return [i["summary"] for i in self.coordinator.shed_items] or ["(shed list is empty)"]

    @property
    def current_option(self) -> str | None:
        item = self.coordinator.shed_item(self.coordinator.learn_uid)
        return item["summary"] if item else None

    async def async_select_option(self, option: str) -> None:
        item = next((i for i in self.coordinator.shed_items if i["summary"] == option), None)
        self.coordinator.learn_uid = item["uid"] if item else None
        self.async_write_ha_state()


class LearnModeSelect(_LearnSelect):
    _attr_options = [m.value for m in Mode]

    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "learn_mode")

    @property
    def current_option(self) -> str:
        return self.coordinator.learn_mode.value

    async def async_select_option(self, option: str) -> None:
        self.coordinator.learn_mode = Mode(option)
        self.async_write_ha_state()

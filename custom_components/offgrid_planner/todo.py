"""Loads and shed steps as editable to-do lists. Shed steps: top is applied first, drag to reprioritise."""
from __future__ import annotations

import uuid

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .entity import OffgridEntity


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([PlannerListEntity(coordinator, "loads", "load_items"),
                        PlannerListEntity(coordinator, "shed_order", "step_items")])


class PlannerListEntity(OffgridEntity, TodoListEntity):
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: OffgridCoordinator, key: str, attr: str) -> None:
        super().__init__(coordinator, key)
        self._list_attr = attr

    @property
    def _items(self) -> list[dict]:
        return getattr(self.coordinator, self._list_attr)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_shed_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True  # editable even when planning fails (e.g. SOC sensor offline)

    @property
    def todo_items(self) -> list[TodoItem]:
        return [TodoItem(uid=i["uid"], summary=i["summary"], description=i.get("description"),
                         status=TodoItemStatus(i.get("status", "needs_action")))
                for i in self._items]

    def _index(self, uid: str) -> int:
        for n, item in enumerate(self._items):
            if item["uid"] == uid:
                return n
        raise ServiceValidationError(f"No shed list item {uid}")

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self._items.append({
            "uid": uuid.uuid4().hex, "summary": item.summary or "", "description": item.description,
            "status": str(item.status or TodoItemStatus.NEEDS_ACTION)})
        await self.coordinator.async_save_lists()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        stored = self._items[self._index(item.uid)]
        stored.update(summary=item.summary or stored["summary"], description=item.description,
                      status=str(item.status or TodoItemStatus.NEEDS_ACTION))
        await self.coordinator.async_save_lists()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        items = self._items
        items[:] = [i for i in items if i["uid"] not in set(uids)]
        await self.coordinator.async_save_lists()

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        items = self._items
        moved = items.pop(self._index(uid))
        position = 0 if previous_uid is None else self._index(previous_uid) + 1
        items.insert(position, moved)
        await self.coordinator.async_save_lists()

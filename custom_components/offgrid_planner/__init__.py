"""Off-Grid Planner: forecast-driven tilt, load-shedding and generator advice."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import OffgridConfigEntry, OffgridCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry) -> bool:
    coordinator = OffgridCoordinator(hass, entry)
    await coordinator.async_load_cache()
    entry.runtime_data = coordinator
    # Numbers restore their values first, then trigger the first plan.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if unsub_meter := coordinator.async_start_meter():
        entry.async_on_unload(unsub_meter)
    entry.async_on_unload(coordinator.async_watch_soc())
    await coordinator.async_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OffgridConfigEntry) -> bool:
    entry.runtime_data.async_learn_cancel()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: OffgridConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

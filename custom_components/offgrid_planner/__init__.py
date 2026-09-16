"""Off-Grid Planner: forecast-driven tilt, load-shedding and generator advice."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import OffgridConfigEntry, OffgridCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry) -> bool:
    coordinator = OffgridCoordinator(hass, entry)
    await coordinator.async_load_cache()
    entry.runtime_data = coordinator
    # Numbers restore their values first, then trigger the first plan.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OffgridConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: OffgridConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

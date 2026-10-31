"""Off-Grid Planner: forecast-driven tilt, load-shedding and generator advice."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .core.destination import MAX_DAYS

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.TODO,
             Platform.WEATHER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_PLAN_DESTINATION = "plan_destination"
SERVICE_CLEAR_DESTINATION = "clear_destination"
PLAN_DESTINATION_SCHEMA = vol.Schema({
    vol.Required("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
    vol.Required("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
    vol.Required("start_date"): cv.date,
    vol.Optional("days", default=7): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS)),
    vol.Optional("soc"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
    vol.Optional("name"): cv.string,
    vol.Optional("timezone"): cv.time_zone,
})


def _coordinator(hass: HomeAssistant) -> OffgridCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Off-Grid Planner is not set up.")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Services: plan a planned destination (another site and dates) and clear it."""

    async def plan_destination(call: ServiceCall) -> dict[str, Any]:
        coordinator = _coordinator(hass)
        data = dict(call.data)
        if "soc" not in data:
            try:
                data["soc"] = coordinator.current_soc()
            except UpdateFailed as err:
                raise ServiceValidationError(f"{err}; pass soc to plan without it.") from err
        result = await coordinator.async_plan_destination(data)
        if "error" in result:
            raise HomeAssistantError(result["error"])
        return result

    async def clear_destination(call: ServiceCall) -> None:
        await _coordinator(hass).async_clear_destination()

    hass.services.async_register(DOMAIN, SERVICE_PLAN_DESTINATION, plan_destination,
                                 schema=PLAN_DESTINATION_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_DESTINATION, clear_destination)
    return True


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

"""Config and options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BASELINE_W,
    CONF_CAPACITY_WH,
    CONF_ENSEMBLE,
    CONF_FRIDGE_W,
    CONF_GEN_CHARGE_W,
    CONF_GEN_END,
    CONF_GEN_START,
    CONF_GENERATOR,
    CONF_HEAT_W_PER_DEGC,
    CONF_JACK_WH,
    CONF_LOADS_TODO,
    CONF_RATED_W,
    CONF_SOC_ENTITY,
    CONF_SYSTEM_FACTOR,
    CONF_TILT_AZIMUTH,
    CONF_TILT_DEG,
    CONF_TILT_END,
    CONF_TILT_START,
    DEFAULTS,
    DOMAIN,
)


def _num(lo: float, hi: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    cfg = selector.NumberSelectorConfig(min=lo, max=hi, step=step, mode=selector.NumberSelectorMode.BOX)
    if unit:
        cfg["unit_of_measurement"] = unit
    return selector.NumberSelector(cfg)


def _schema(current: dict[str, Any]) -> vol.Schema:
    def d(key):
        return current.get(key, DEFAULTS.get(key))

    todo = {vol.Optional(CONF_LOADS_TODO, description={"suggested_value": current.get(CONF_LOADS_TODO)}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="todo"))}
    return vol.Schema({
        vol.Required(CONF_SOC_ENTITY, default=current.get(CONF_SOC_ENTITY, vol.UNDEFINED)):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="battery")),
        **todo,
        vol.Required(CONF_CAPACITY_WH, default=d(CONF_CAPACITY_WH)): _num(100, 200000, 10, "Wh"),
        vol.Required(CONF_RATED_W, default=d(CONF_RATED_W)): _num(10, 100000, 10, "W"),
        vol.Required(CONF_SYSTEM_FACTOR, default=d(CONF_SYSTEM_FACTOR)): _num(0.1, 1.2, 0.01),
        vol.Required(CONF_BASELINE_W, default=d(CONF_BASELINE_W)): _num(0, 5000, 1, "W"),
        vol.Required(CONF_FRIDGE_W, default=d(CONF_FRIDGE_W)): _num(0, 1000, 1, "W"),
        vol.Required(CONF_HEAT_W_PER_DEGC, default=d(CONF_HEAT_W_PER_DEGC)): _num(0, 100, 0.1, "W/°C"),
        vol.Required(CONF_TILT_DEG, default=d(CONF_TILT_DEG)): _num(0, 45, 0.5, "°"),
        vol.Required(CONF_TILT_AZIMUTH, default=d(CONF_TILT_AZIMUTH)): _num(0, 359, 1, "°"),
        vol.Required(CONF_TILT_START, default=d(CONF_TILT_START)): _num(0, 24, 0.5, "h"),
        vol.Required(CONF_TILT_END, default=d(CONF_TILT_END)): _num(0, 24, 0.5, "h"),
        vol.Required(CONF_JACK_WH, default=d(CONF_JACK_WH)): _num(0, 1000, 1, "Wh"),
        vol.Required(CONF_GENERATOR, default=d(CONF_GENERATOR)): selector.BooleanSelector(),
        vol.Required(CONF_GEN_CHARGE_W, default=d(CONF_GEN_CHARGE_W)): _num(50, 20000, 10, "W"),
        vol.Required(CONF_GEN_START, default=d(CONF_GEN_START)): _num(0, 24, 0.5, "h"),
        vol.Required(CONF_GEN_END, default=d(CONF_GEN_END)): _num(0, 24, 0.5, "h"),
        vol.Required(CONF_ENSEMBLE, default=d(CONF_ENSEMBLE)): selector.BooleanSelector(),
    })


class OffgridPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Off-Grid Planner", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OffgridPlannerOptionsFlow:
        return OffgridPlannerOptionsFlow()


class OffgridPlannerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))

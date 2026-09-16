"""Constants for the Off-Grid Planner integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "offgrid_planner"

PLAN_INTERVAL = timedelta(minutes=15)
WEATHER_MAX_AGE = timedelta(hours=1)
WEATHER_STALE_AFTER = timedelta(hours=12)
MOVE_REFETCH_KM = 5.0
FETCH_TIMEOUT_S = 30
STORAGE_VERSION = 1

CONF_SOC_ENTITY = "soc_entity"
CONF_LOADS_TODO = "loads_todo"
CONF_CAPACITY_WH = "capacity_wh"
CONF_RATED_W = "rated_w"
CONF_SYSTEM_FACTOR = "system_factor"
CONF_BASELINE_W = "baseline_w"
CONF_FRIDGE_W = "fridge_w"
CONF_HEAT_W_PER_DEGC = "heat_w_per_degc"
CONF_TILT_DEG = "tilt_deg"
CONF_TILT_AZIMUTH = "tilt_azimuth"
CONF_TILT_START = "tilt_start_hour"
CONF_TILT_END = "tilt_end_hour"
CONF_JACK_WH = "jack_wh_per_day"
CONF_GENERATOR = "generator"
CONF_GEN_CHARGE_W = "generator_charge_w"
CONF_GEN_START = "generator_start_hour"
CONF_GEN_END = "generator_end_hour"
CONF_ENSEMBLE = "use_ensemble"

DEFAULTS = {
    CONF_CAPACITY_WH: 6240,
    CONF_RATED_W: 1580,
    CONF_SYSTEM_FACTOR: 0.68,
    CONF_BASELINE_W: 43,
    CONF_FRIDGE_W: 27,
    CONF_HEAT_W_PER_DEGC: 1.0,
    CONF_TILT_DEG: 10,
    CONF_TILT_AZIMUTH: 180,
    CONF_TILT_START: 9,
    CONF_TILT_END: 17,
    CONF_JACK_WH: 10,
    CONF_GENERATOR: True,
    CONF_GEN_CHARGE_W: 650,
    CONF_GEN_START: 8,
    CONF_GEN_END: 20,
    CONF_ENSEMBLE: True,
}

# Runtime-adjustable numbers (restored across restarts).
NUMBER_RESERVE = "reserve_soc"
NUMBER_SITE_HORIZON = "site_horizon"
NUMBER_DEFAULTS = {NUMBER_RESERVE: 20.0, NUMBER_SITE_HORIZON: 1.0}

SCENARIO_EXPECTED = "expected"
SCENARIO_BAD = "bad_week"

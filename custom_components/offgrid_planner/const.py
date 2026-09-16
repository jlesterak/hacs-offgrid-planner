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
CONF_BATTERY_POWER = "battery_power_entity"  # W, + = charging
CONF_PV_POWER = "pv_power_entity"  # W, optional: subtracted when learning loads by day
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
    CONF_HEAT_W_PER_DEGC: 1.3,  # furnace blower, fitted on 2026-09-14/16 nights
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

SHED_STORAGE_KEY = "shed_list"
LEARN_SAMPLE_INTERVAL = timedelta(seconds=1)
METER_SAMPLE_INTERVAL = timedelta(seconds=5)

# Seeded once when the integration is added; edit freely in the to-do lists.
DEFAULT_LOADS = (
    ("Inverter", "35 W idle, 24/7, supply (estimate)"),
    ("Starlink + router", "60 W, 24/7, needs inverter (estimate)"),
    ("NAS", "40 W, 24/7, needs inverter (estimate)"),
    ("Espresso machine", "1200 W, 0.3 h/day in 7-10, needs inverter (estimate)"),
    ("Small dishwasher", "900 W, 1 h/day in 12-15, needs inverter (estimate)"),
    ("Ice maker", "120 W, 6 h/day in 10-18, needs inverter (estimate)"),
)
DEFAULT_STEPS = (
    ("Espresso machine → off", "Espresso machine: off"),
    ("Small dishwasher → off", "Small dishwasher: off"),
    ("Ice maker → off", "Ice maker: off"),
    ("NAS → evenings only", "NAS: 18-22"),
    ("Internet → 8-22", "Starlink + router: 8-22"),
    ("Inverter → 8-22", "Inverter: 8-22"),
    ("NAS → off", "NAS: off"),
    ("Internet → weekday work hours", "Starlink + router: 8-17 weekdays, off weekends"),
    ("Inverter → 8-17", "Inverter: 8-17"),
    ("Inverter → off", "Inverter: off"),
)
# The 0.1/0.2 single shed list seed, used to detect an unedited list worth replacing on upgrade.
LEGACY_SHED_LIST = (
    ("Espresso machine", "1200 W, 0.3 h/day, 7-10 (estimate)"),
    ("Small dishwasher", "900 W, 1 h/day, 12-15 (estimate)"),
    ("Ice maker", "120 W, 6 h/day, 10-18 (estimate)"),
    ("NAS", "40 W, 4 h/day, 18-22 (estimate)"),
    ("Internet: weekday evenings and nights", "60 W, 15 h/day, 17-8, weekdays (estimate)"),
    ("Internet: weekends", "60 W, 24 h/day, weekends (estimate)"),
    ("Internet: weekday work hours", "60 W, 9 h/day, 8-17, weekdays, essential (estimate)"),
)

SCENARIO_EXPECTED = "expected"
SCENARIO_BAD = "bad_week"

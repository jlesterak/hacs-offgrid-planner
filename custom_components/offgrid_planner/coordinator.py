"""Coordinator: keeps a cached forecast, reads SOC and the shed list, and replans."""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    FETCH_TIMEOUT_S,
    MOVE_REFETCH_KM,
    NUMBER_DEFAULTS,
    NUMBER_RESERVE,
    NUMBER_SITE_HORIZON,
    PLAN_INTERVAL,
    SCENARIO_BAD,
    SCENARIO_EXPECTED,
    STORAGE_VERSION,
    WEATHER_MAX_AGE,
    WEATHER_STALE_AFTER,
)
from .core.battery import BatteryConfig, GeneratorConfig
from .core.loads import BaseLoad, Load, LoadModel, parse_load
from .core.planner import Plan, PlannerConfig, make_plan
from .core.pv import ArrayConfig, TiltPlan
from .core.weather import (
    ENSEMBLE_URL,
    FORECAST_URL,
    ensemble_params,
    forecast_params,
    parse_ensemble,
    parse_forecast,
    pessimistic,
)

_LOGGER = logging.getLogger(__name__)

type OffgridConfigEntry = ConfigEntry[OffgridCoordinator]


@dataclass
class PlannerData:
    plan: Plan
    soc: float
    weather_fetched: datetime | None
    weather_stale: bool
    loads: list[Load]


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


class OffgridCoordinator(DataUpdateCoordinator[PlannerData]):
    config_entry: OffgridConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OffgridConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=PLAN_INTERVAL)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._cache: dict[str, Any] = {}
        self.numbers: dict[str, float] = dict(NUMBER_DEFAULTS)

    def opt(self, key: str) -> Any:
        return self.config_entry.options.get(key, self.config_entry.data.get(key, DEFAULTS.get(key)))

    async def async_load_cache(self) -> None:
        self._cache = await self._store.async_load() or {}

    async def async_set_number(self, key: str, value: float) -> None:
        self.numbers[key] = value
        await self.async_request_refresh()

    # --- weather ---------------------------------------------------------------

    async def _fetch_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            resp = await session.get(url, params=params)
            resp.raise_for_status()
            return await resp.json()

    async def _maybe_refresh_weather(self, lat: float, lon: float) -> None:
        now = dt_util.utcnow()
        fetched = self._cache.get("fetched")
        moved = ("lat" not in self._cache
                 or _km(self._cache["lat"], self._cache["lon"], lat, lon) > MOVE_REFETCH_KM)
        if fetched and not moved and now - dt_util.parse_datetime(fetched) < WEATHER_MAX_AGE:
            return
        try:
            forecast = await self._fetch_json(FORECAST_URL, forecast_params(lat, lon))
            ensemble = None
            if self.opt(CONF_ENSEMBLE):
                try:
                    ensemble = await self._fetch_json(ENSEMBLE_URL, ensemble_params(lat, lon))
                except (TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("Ensemble forecast unavailable: %s", err)
        except (TimeoutError, aiohttp.ClientError) as err:
            # Offline is normal off-grid: keep planning from the cached forecast.
            _LOGGER.debug("Forecast fetch failed, using cache: %s", err)
            return
        self._cache = {"fetched": now.isoformat(), "lat": round(lat, 2), "lon": round(lon, 2),
                       "forecast": forecast, "ensemble": ensemble}
        await self._store.async_save(self._cache)

    # --- inputs ----------------------------------------------------------------

    def _soc(self) -> float:
        entity_id = self.opt(CONF_SOC_ENTITY)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise UpdateFailed(f"{entity_id} is unavailable")
        try:
            return max(0.0, min(100.0, float(state.state)))
        except ValueError as err:
            raise UpdateFailed(f"{entity_id} is not numeric: {state.state}") from err

    async def _loads(self) -> list[Load]:
        entity_id = self.opt(CONF_LOADS_TODO)
        if not entity_id or self.hass.states.get(entity_id) is None:
            return []
        resp = await self.hass.services.async_call(
            "todo", "get_items", {"entity_id": entity_id, "status": ["needs_action", "completed"]},
            blocking=True, return_response=True)
        items = (resp or {}).get(entity_id, {}).get("items", [])
        loads = []
        for item in items:
            load = parse_load(item.get("summary", ""), item.get("description"),
                              item.get("status") == "completed")
            if load is None:
                _LOGGER.debug("Shed list item without wattage ignored: %s", item.get("summary"))
            else:
                loads.append(load)
        return loads

    # --- plan ------------------------------------------------------------------

    async def _async_update_data(self) -> PlannerData:
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        tz = str(self.hass.config.time_zone)
        await self._maybe_refresh_weather(lat, lon)
        if not self._cache.get("forecast"):
            raise UpdateFailed("No forecast yet (never online since setup)")
        soc = self._soc()
        loads = await self._loads()
        now = dt_util.utcnow()

        scenarios = {SCENARIO_EXPECTED: parse_forecast(self._cache["forecast"])}
        if self._cache.get("ensemble"):
            members = parse_ensemble(self._cache["ensemble"])
            scenarios[SCENARIO_BAD] = pessimistic(scenarios[SCENARIO_EXPECTED], members, 0.1, start=now, hours=168)

        array = ArrayConfig(rated_w=float(self.opt(CONF_RATED_W)), k=float(self.opt(CONF_SYSTEM_FACTOR)),
                            site_horizon=self.numbers[NUMBER_SITE_HORIZON])
        load_model = LoadModel(
            BaseLoad(baseline_w=float(self.opt(CONF_BASELINE_W)), fridge_w=float(self.opt(CONF_FRIDGE_W)),
                     heat_w_per_degc=float(self.opt(CONF_HEAT_W_PER_DEGC))),
            tuple(loads), tz=tz)
        generator = (GeneratorConfig(charge_w=float(self.opt(CONF_GEN_CHARGE_W)),
                                     allowed_start_hour=float(self.opt(CONF_GEN_START)),
                                     allowed_end_hour=float(self.opt(CONF_GEN_END)), tz=tz)
                     if self.opt(CONF_GENERATOR) else None)
        cfg = PlannerConfig(
            reserve_soc=self.numbers[NUMBER_RESERVE],
            jack_wh_per_day=float(self.opt(CONF_JACK_WH)),
            tilt=TiltPlan(tilt_deg=float(self.opt(CONF_TILT_DEG)), panel_az=float(self.opt(CONF_TILT_AZIMUTH)),
                          start_hour=float(self.opt(CONF_TILT_START)),
                          end_hour=float(self.opt(CONF_TILT_END)), tz=tz))
        battery = BatteryConfig(capacity_wh=float(self.opt(CONF_CAPACITY_WH)))

        # The simulation is CPU-bound (a few hundred ms on a Pi at most): keep it off the event loop.
        plan = await self.hass.async_add_executor_job(
            make_plan, scenarios, now, soc, lat, lon, array, battery, load_model, generator, cfg,
            SCENARIO_BAD if SCENARIO_BAD in scenarios else SCENARIO_EXPECTED)

        fetched = dt_util.parse_datetime(self._cache["fetched"])
        return PlannerData(plan=plan, soc=soc, weather_fetched=fetched,
                           weather_stale=now - fetched > WEATHER_STALE_AFTER, loads=loads)

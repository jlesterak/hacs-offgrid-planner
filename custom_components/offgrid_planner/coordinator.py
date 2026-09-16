"""Coordinator: keeps a cached forecast, reads SOC and the shed list, and replans."""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASELINE_W,
    CONF_BATTERY_POWER,
    CONF_CAPACITY_WH,
    CONF_ENSEMBLE,
    CONF_FRIDGE_W,
    CONF_GEN_CHARGE_W,
    CONF_GEN_END,
    CONF_GEN_START,
    CONF_GENERATOR,
    CONF_HEAT_W_PER_DEGC,
    CONF_JACK_WH,
    CONF_PV_POWER,
    CONF_RATED_W,
    CONF_SOC_ENTITY,
    CONF_SYSTEM_FACTOR,
    CONF_TILT_AZIMUTH,
    CONF_TILT_DEG,
    CONF_TILT_END,
    CONF_TILT_START,
    DEFAULT_LOADS,
    DEFAULT_STEPS,
    DEFAULTS,
    DOMAIN,
    FETCH_TIMEOUT_S,
    LEARN_SAMPLE_INTERVAL,
    LEGACY_SHED_LIST,
    METER_SAMPLE_INTERVAL,
    MOVE_REFETCH_KM,
    NUMBER_DEFAULTS,
    NUMBER_RESERVE,
    NUMBER_SITE_HORIZON,
    PLAN_INTERVAL,
    SCENARIO_BAD,
    SCENARIO_EXPECTED,
    SHED_STORAGE_KEY,
    STORAGE_VERSION,
    WEATHER_MAX_AGE,
    WEATHER_STALE_AFTER,
)
from .core.battery import BatteryConfig, GeneratorConfig
from .core.learn import Learner, Mode, Phase, apply_to_description
from .core.loadcheck import EnergyMeter, LoadCheck, hour_key, last_night
from .core.loads import BaseLoad, Load, LoadModel, ShedStep, parse_load, parse_step
from .core.planner import DaySummary, Plan, PlannerConfig, daily_summary, make_plan
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
    daily: dict[str, list[DaySummary]]
    load_check: LoadCheck | None
    problems: list[str]


def _item(summary: str, description: str) -> dict[str, Any]:
    return {"uid": uuid.uuid4().hex, "summary": summary, "description": description, "status": "needs_action"}


def _is_legacy_seed(items: list[dict[str, Any]]) -> bool:
    return [(i.get("summary"), i.get("description"), i.get("status")) for i in items] == [
        (name, desc, "needs_action") for name, desc in LEGACY_SHED_LIST]


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
        self._shed_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.{SHED_STORAGE_KEY}")
        # To-do style items {"uid", "summary", "description", "status"}: loads, and shed steps (top = first).
        self.load_items: list[dict[str, Any]] = []
        self.step_items: list[dict[str, Any]] = []
        self.learner: Learner | None = None
        self.learn_uid: str | None = None
        self.learn_mode: Mode = Mode.STEP
        self.learn_last_w: float | None = None
        self._learn_unsub: CALLBACK_TYPE | None = None
        self._shed_listeners: list[Callable[[], None]] = []
        self.meter = EnergyMeter()
        self._meter_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.meter")

    def opt(self, key: str) -> Any:
        return self.config_entry.options.get(key, self.config_entry.data.get(key, DEFAULTS.get(key)))

    async def async_load_cache(self) -> None:
        self._cache = await self._store.async_load() or {}
        meter = await self._meter_store.async_load()
        if meter:
            self.meter.hours = meter.get("hours", {})
        stored = await self._shed_store.async_load()
        if stored is not None and "loads" in stored:
            self.load_items, self.step_items = stored["loads"], stored.get("steps", [])
            return
        if stored is None or _is_legacy_seed(stored.get("items", [])):
            self.load_items = [_item(name, desc) for name, desc in DEFAULT_LOADS]
            self.step_items = [_item(name, desc) for name, desc in DEFAULT_STEPS]
        else:
            # 0.1/0.2 edited single list: every item becomes a load, and a "→ off" step in the same order.
            self.load_items = stored.get("items", [])
            self.step_items = [_item(f"{i['summary']} → off", f"{i['summary']}: off") for i in self.load_items]
        await self._shed_store.async_save(self._lists())

    # --- shed list ---------------------------------------------------------------

    @callback
    def async_add_shed_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._shed_listeners.append(update)
        return lambda: self._shed_listeners.remove(update)

    @callback
    def _notify_ui(self) -> None:
        """Update the shed list and learning entities only (not every planner sensor)."""
        for update in list(self._shed_listeners):
            update()

    def _lists(self) -> dict[str, Any]:
        return {"loads": self.load_items, "steps": self.step_items}

    async def async_save_lists(self) -> None:
        await self._shed_store.async_save(self._lists())
        self._notify_ui()
        await self.async_refresh()  # edits are rare: replan now rather than after the debounce

    def load_item(self, uid: str | None) -> dict[str, Any] | None:
        return next((i for i in self.load_items if i["uid"] == uid), None)

    @callback
    def async_watch_soc(self) -> CALLBACK_TYPE:
        """Replan as soon as the SOC sensor recovers, e.g. a BLE shunt that reports a few seconds after startup."""

        @callback
        def _changed(event) -> None:
            new = event.data.get("new_state")
            if (not self.last_update_success and new is not None
                    and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)):
                self.hass.async_create_task(self.async_request_refresh())

        return async_track_state_change_event(self.hass, [self.opt(CONF_SOC_ENTITY)], _changed)

    # --- measured load (for the model check) -----------------------------------------

    @callback
    def async_start_meter(self) -> CALLBACK_TYPE | None:
        if not self.opt(CONF_BATTERY_POWER):
            return None
        return async_track_time_interval(self.hass, self._async_meter_sample, METER_SAMPLE_INTERVAL)

    async def _async_meter_sample(self, now: datetime) -> None:
        state = self.hass.states.get(self.opt(CONF_BATTERY_POWER))
        try:
            discharge_w = -float(state.state) if state else None
        except ValueError:
            discharge_w = None
        self.meter.add(now, discharge_w)
        # Batch writes: the SD card doesn't need a save every few seconds.
        self._meter_store.async_delay_save(lambda: {"hours": self.meter.hours}, 600)

    # --- learning ------------------------------------------------------------------

    def _load_power_w(self) -> float | None:
        """Instant load power from the battery (and PV) sensors: load = PV − battery charge power."""
        def num(entity_id):
            state = self.hass.states.get(entity_id) if entity_id else None
            try:
                return float(state.state) if state else None
            except ValueError:
                return None

        battery = num(self.opt(CONF_BATTERY_POWER))
        if battery is None:
            return None
        pv = num(self.opt(CONF_PV_POWER)) if self.opt(CONF_PV_POWER) else 0.0
        return None if pv is None else pv - battery

    @callback
    def async_learn_start(self) -> None:
        self.async_learn_cancel()
        if not self.opt(CONF_BATTERY_POWER):
            self.learner = Learner(self.learn_mode)
            self.learner.phase, self.learner.message = Phase.FAILED, "Set a battery power sensor in the options."
        elif self.load_item(self.learn_uid) is None:
            self.learner = Learner(self.learn_mode)
            self.learner.phase, self.learner.message = Phase.FAILED, "Choose which load to learn first."
        else:
            self.learner = Learner(self.learn_mode)
            self._learn_unsub = async_track_time_interval(self.hass, self._async_learn_sample, LEARN_SAMPLE_INTERVAL)
        self._notify_ui()

    @callback
    def async_learn_cancel(self) -> None:
        if self._learn_unsub:
            self._learn_unsub()
            self._learn_unsub = None

    async def _async_learn_sample(self, now: datetime) -> None:
        learner = self.learner
        if learner is None:
            return
        load_w = self._load_power_w()
        if load_w is not None:
            self.learn_last_w = load_w
            learner.feed(now, load_w)
        await self._async_learn_after_step()

    async def async_learn_finish(self) -> None:
        if self.learner is None:
            return
        self.learner.finish()
        await self._async_learn_after_step()

    async def _async_learn_after_step(self) -> None:
        learner = self.learner
        if learner.phase in (Phase.DONE, Phase.FAILED):
            self.async_learn_cancel()
            item = self.load_item(self.learn_uid)
            if learner.phase == Phase.DONE and item is not None and learner.result.watts > 0:
                item["description"] = apply_to_description(item.get("description"), learner.result.watts)
                await self.async_save_lists()
        self._notify_ui()

    async def async_set_number(self, key: str, value: float) -> None:
        self.numbers[key] = value
        await self.async_refresh()

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

    def _loads(self) -> tuple[list[Load], list[ShedStep], list[str]]:
        """Parse both lists. Unparseable items are reported as problems, not silently dropped."""
        loads, steps, problems = [], [], []
        for item in self.load_items:
            load = parse_load(item.get("summary", ""), item.get("description"), item.get("status") == "completed")
            if load is None:
                problems.append(f"Load '{item.get('summary')}': no wattage (e.g. '40 W, 24/7')")
            else:
                loads.append(load)
        for item in self.step_items:
            step = parse_step(item.get("summary", ""), item.get("description"), item.get("status") == "completed")
            if step is None:
                problems.append(f"Shed step '{item.get('summary')}': use 'Load name: schedule' (e.g. 'NAS: 18-22')")
            else:
                steps.append(step)
        return loads, steps, problems

    # --- plan ------------------------------------------------------------------

    async def _async_update_data(self) -> PlannerData:
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        tz = str(self.hass.config.time_zone)
        await self._maybe_refresh_weather(lat, lon)
        if not self._cache.get("forecast"):
            raise UpdateFailed("No forecast yet (never online since setup)")
        soc = self._soc()
        loads, steps, problems = self._loads()
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
            tuple(loads), tuple(steps), tz=tz)
        problems += load_model.problems()
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

        daily = {name: daily_summary(sc, tz) for name, sc in plan.scenarios.items()}

        self.meter.prune(now)
        past = [p for p in scenarios[SCENARIO_EXPECTED] if p.start < now]
        modelled = {hour_key(p.start): w for p, w in zip(past, load_model.series(past), strict=True)}
        check = last_night(now, lat, lon, self.meter, modelled)

        fetched = dt_util.parse_datetime(self._cache["fetched"])
        return PlannerData(plan=plan, soc=soc, weather_fetched=fetched,
                           weather_stale=now - fetched > WEATHER_STALE_AFTER, loads=loads,
                           daily=daily, load_check=check, problems=problems)

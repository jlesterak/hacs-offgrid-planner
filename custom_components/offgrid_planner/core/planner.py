"""Turn forecasts into decisions: tilt the trailer, shed loads, or plan generator runs.

For each weather scenario (e.g. "expected" and "bad week"):
  1. Tilt: compare daytime-tilted vs flat PV over the next days, net of the jack's energy.
  2. Shed: the smallest number of shed steps, in order, that keeps SOC above the reserve.
  3. Generator: if applying every step is not enough, simulate generator runs.
The overall status comes from the planning scenario (the pessimistic one when available).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import IntEnum

from .battery import BatteryConfig, GeneratorConfig, SimResult, simulate
from .loads import LoadModel
from .pv import ArrayConfig, TiltPlan, pv_series
from .weather import WeatherPeriod


class Status(IntEnum):
    OK = 0
    TILT = 1
    SHED = 2
    GENERATOR = 3


@dataclass(frozen=True)
class PlannerConfig:
    reserve_soc: float = 20.0
    horizon_hours: int = 168
    tilt_window_hours: int = 72
    tilt_min_gain_wh_per_day: float = 300.0
    jack_wh_per_day: float = 10.0  # up in the morning, down at night; placeholder until measured
    tilt: TiltPlan = field(default_factory=lambda: TiltPlan(tilt_deg=10.0))


@dataclass
class ScenarioPlan:
    name: str
    pv_today_wh: float  # rest of today, from now
    pv_today_full_wh: float  # whole local day, including hours already past
    pv_tomorrow_wh: float
    pv_horizon_wh: float
    load_horizon_wh: float
    tilt_gain_wh_per_day: float
    tilt_recommended: bool
    no_action: SimResult
    shed_level: int | None  # steps applied; None: shedding alone is not enough
    shed_steps: list[str]  # labels of the applied steps (all active steps when the generator is needed)
    shed_saving_wh_per_day: float
    step_effects: list[dict]  # per applied step: saved_wh_per_day, also_cuts
    with_plan: SimResult  # the chosen tilt/shed/generator plan
    generator_needed: bool
    status: Status
    # Hourly series for the horizon (same length as the SOC lists), for daily summaries and charts.
    starts: list[dt.datetime] = field(default_factory=list)
    pv_w: list[float] = field(default_factory=list)
    load_w: list[float] = field(default_factory=list)  # with the chosen shedding
    load_no_action_w: list[float] = field(default_factory=list)


@dataclass
class DaySummary:
    day: dt.date
    pv_wh: float
    load_wh: float  # with the chosen shedding
    load_no_action_wh: float
    min_soc: float  # with the plan
    min_soc_no_action: float
    end_soc: float
    generator_hours: float = 0.0
    max_soc: float = -1.0  # with the plan


@dataclass
class Plan:
    created: dt.datetime
    status: Status
    planning_scenario: str
    scenarios: dict[str, ScenarioPlan]


def _window(periods: list[WeatherPeriod], now: dt.datetime, hours: int) -> list[WeatherPeriod]:
    end = now + dt.timedelta(hours=hours)
    return [p for p in periods if p.start + dt.timedelta(hours=p.hours) > now and p.start < end]


def _day_energy(periods, series, zone_day) -> float:
    return sum(w * p.hours for p, w in zip(periods, series, strict=True) if zone_day(p))


def _generator_plan(periods, soc_now, pv, load, battery, reserve, generator) -> SimResult:
    """Lowest generator start SOC that keeps the battery above the reserve.

    Quiet hours mean waiting for the reserve can be too late (the night drains it before the
    generator may run), so start earlier in the day when needed. Falls back to the best effort.
    """
    if generator is None:
        return simulate(periods, soc_now, pv, load, battery, reserve)
    best = None
    start = max(generator.start_soc, reserve + 1)
    while start <= 95:
        gen = replace(generator, start_soc=start, stop_soc=max(generator.stop_soc, min(100, start + 15)))
        res = simulate(periods, soc_now, pv, load, battery, reserve, gen)
        if best is None or res.min_soc > best.min_soc:
            best = res
        if res.min_soc >= reserve:
            return res
        start += 5
    return best


def plan_scenario(name: str, periods: list[WeatherPeriod], now: dt.datetime, soc_now: float,
                  lat: float, lon: float, array: ArrayConfig, battery: BatteryConfig, loads: LoadModel,
                  generator: GeneratorConfig | None, cfg: PlannerConfig) -> ScenarioPlan:
    all_periods = periods
    periods = _window(periods, now, cfg.horizon_hours)
    reserve = cfg.reserve_soc
    flat = pv_series(periods, lat, lon, array)
    tilted = pv_series(periods, lat, lon, array, cfg.tilt) if cfg.tilt.tilt_deg > 0 else flat
    base_load = loads.series(periods)

    zone = cfg.tilt._zone
    today = now.astimezone(zone).date()

    def on(day):
        return lambda p: (p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone).date() == day

    # Tilt gain over the tilt window, per day, net of jack energy.
    tw = [i for i, p in enumerate(periods) if p.start < now + dt.timedelta(hours=cfg.tilt_window_hours)]
    days = max(1, cfg.tilt.days_tilted([periods[i] for i in tw]))
    gain = sum((tilted[i] - flat[i]) * periods[i].hours for i in tw) / days - cfg.jack_wh_per_day
    tilt_ok = cfg.tilt.tilt_deg > 0 and gain >= cfg.tilt_min_gain_wh_per_day
    pv = tilted if tilt_ok else flat

    no_action = simulate(periods, soc_now, flat, base_load, battery, reserve)
    steps = loads.active_steps()

    shed_level, chosen = None, None
    for level in range(len(steps) + 1):
        res = simulate(periods, soc_now, pv, loads.series(periods, level) if level else base_load,
                       battery, reserve)
        if res.min_soc >= reserve:
            shed_level, chosen = level, res
            break

    generator_needed = shed_level is None
    applied = len(steps) if generator_needed else shed_level
    if generator_needed:
        plan_load = loads.series(periods, applied)
        chosen = _generator_plan(periods, soc_now, pv, plan_load, battery, reserve, generator)
    else:
        plan_load = loads.series(periods, applied) if applied else base_load
    horizon_days = max(sum(p.hours for p in periods) / 24, 1 / 24)
    saving = (sum(w * p.hours for p, w in zip(periods, base_load, strict=True))
              - sum(w * p.hours for p, w in zip(periods, plan_load, strict=True))) / horizon_days
    effects = [{"step": e["step"], "saved_wh_per_day": round(e["saved_wh"] / horizon_days),
                "also_cuts": e["also_cuts"]} for e in loads.step_effects(periods, applied)]

    if generator_needed:
        status = Status.GENERATOR
    elif shed_level:
        status = Status.SHED
    elif tilt_ok and no_action.min_soc < 100 and (
            no_action.min_soc < reserve + 20 or no_action.curtailed_wh < gain):
        status = Status.TILT
    else:
        status = Status.OK

    today_periods = [p for p in all_periods if on(today)(p)]
    today_pv = pv_series(today_periods, lat, lon, array, cfg.tilt if tilt_ok else None)

    return ScenarioPlan(
        name=name,
        pv_today_wh=_day_energy(periods, pv, on(today)),
        pv_today_full_wh=sum(w * p.hours for p, w in zip(today_periods, today_pv, strict=True)),
        pv_tomorrow_wh=_day_energy(periods, pv, on(today + dt.timedelta(days=1))),
        pv_horizon_wh=sum(w * p.hours for p, w in zip(periods, pv, strict=True)),
        load_horizon_wh=sum(w * p.hours for p, w in zip(periods, base_load, strict=True)),
        tilt_gain_wh_per_day=gain,
        tilt_recommended=tilt_ok,
        no_action=no_action,
        shed_level=shed_level,
        shed_steps=[st.label for st in steps[:applied]],
        shed_saving_wh_per_day=saving,
        step_effects=effects,
        with_plan=chosen,
        generator_needed=generator_needed,
        status=status,
        starts=[p.start for p in periods],
        pv_w=pv,
        load_w=plan_load,
        load_no_action_w=base_load,
    )


def daily_summary(sc: ScenarioPlan, tz: str) -> list[DaySummary]:
    """Per local day: energy in and out, lowest, highest and end-of-day SOC, with and without the plan."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    days: dict[dt.date, DaySummary] = {}
    gen_on = sc.generator_needed
    for i, start in enumerate(sc.starts):
        day = (start + dt.timedelta(minutes=30)).astimezone(zone).date()
        d = days.get(day)
        if d is None:
            d = days[day] = DaySummary(day, 0.0, 0.0, 0.0, 101.0, 101.0, 0.0)
        d.pv_wh += sc.pv_w[i]
        d.load_wh += sc.load_w[i]
        d.load_no_action_wh += sc.load_no_action_w[i]
        d.min_soc = min(d.min_soc, sc.with_plan.soc[i])
        d.max_soc = max(d.max_soc, sc.with_plan.soc[i])
        d.min_soc_no_action = min(d.min_soc_no_action, sc.no_action.soc[i])
        d.end_soc = sc.with_plan.soc[i]
        if gen_on and sc.with_plan.generator_on and sc.with_plan.generator_on[i]:
            d.generator_hours += 1.0
    return list(days.values())


def make_plan(scenarios: dict[str, list[WeatherPeriod]], now: dt.datetime, soc_now: float,
              lat: float, lon: float, array: ArrayConfig, battery: BatteryConfig, loads: LoadModel,
              generator: GeneratorConfig | None, cfg: PlannerConfig,
              planning_scenario: str | None = None) -> Plan:
    plans = {name: plan_scenario(name, periods, now, soc_now, lat, lon, array, battery, loads,
                                 generator, cfg)
             for name, periods in scenarios.items() if periods}
    if not plans:
        raise ValueError("no weather scenarios")
    key = planning_scenario if planning_scenario in plans else max(plans, key=lambda n: plans[n].status)
    return Plan(created=now, status=plans[key].status, planning_scenario=key, scenarios=plans)

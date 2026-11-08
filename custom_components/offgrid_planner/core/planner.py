"""Turn forecasts into decisions: tilt the trailer, shed loads, or plan generator runs.

For each weather scenario (e.g. "expected" and "bad week"):
  1. Tilt: compare daytime-tilted vs flat PV over the next days, net of the jack's energy.
  2. Shed: a shed level (how many steps, in order) per local day, as late and as small as possible, that
     keeps SOC above the reserve. Shedding before the battery's last full charge is wasted (curtailed), so a
     bad spell later in the week does not make today shed. Entities report today's level.
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
    # True: shed_level/status/advice describe today (live plan). False: the highest day (a planned trip).
    report_today: bool = True


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
    shed_level: int | None  # steps applied today (or on the worst day, see report_today); None: generator needed
    step_total: int  # active shed steps available
    shed_steps: list[str]  # labels of those steps (all active steps when the generator is needed)
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
    day_levels: list[tuple[dt.date, int]] = field(default_factory=list)  # shed level per local day
    next_shed: dict | None = None  # first later day that needs more shedding: {date, level, steps}


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
    shed_level: int = 0


@dataclass
class Plan:
    created: dt.datetime
    status: Status
    planning_scenario: str
    scenarios: dict[str, ScenarioPlan]


FULL_SOC = 99.5  # a period ending at or above this counts as a full charge


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
    n = len(steps)

    def local_day(p):
        return (p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone).date()

    plan_days = sorted({local_day(p) for p in periods})
    day_i = [plan_days.index(local_day(p)) for p in periods]
    by_level: dict[int, list[float]] = {0: base_load}

    def level_series(level: int) -> list[float]:
        if level not in by_level:
            by_level[level] = loads.series(periods, level)
        return by_level[level]

    def load_for(levels: list[int]) -> list[float]:
        return [level_series(levels[d])[i] for i, d in enumerate(day_i)]

    def run(levels: list[int]) -> SimResult:
        return simulate(periods, soc_now, pv, load_for(levels), battery, reserve)

    levels = [0] * len(plan_days)
    chosen = run(levels)
    generator_needed = False
    while chosen.min_soc < reserve:
        breach = next((i for i, x in enumerate(chosen.soc) if x < reserve), 0)
        full = max((i for i, x in enumerate(chosen.soc[:breach]) if x >= FULL_SOC), default=-1)
        first = day_i[full + 1] if full + 1 < len(day_i) else 0
        raise_day = next((d for d in range(day_i[breach], first - 1, -1) if levels[d] < n), None)
        if raise_day is None:
            generator_needed = True
            break
        levels[raise_day] += 1
        chosen = run(levels)
    if not generator_needed:
        # Raising late first can leave a pre-dawn breach day at a high level that the evening before covers.
        for d in range(len(plan_days) - 1, -1, -1):
            while levels[d] > 0:
                trial = levels[:d] + [levels[d] - 1] + levels[d + 1:]
                res = run(trial)
                if res.min_soc < reserve:
                    break
                levels, chosen = trial, res

    if generator_needed:
        levels = [n] * len(plan_days)
        plan_load = load_for(levels)
        chosen = _generator_plan(periods, soc_now, pv, plan_load, battery, reserve, generator)
    else:
        plan_load = load_for(levels)
    today_i = plan_days.index(today) if today in plan_days else 0
    applied = 0 if not levels else levels[today_i] if cfg.report_today else max(levels, default=0)
    shed_level = None if generator_needed else applied
    next_shed = None
    if cfg.report_today and not generator_needed:
        later = next((d for d in range(today_i + 1, len(plan_days)) if levels[d] > applied), None)
        if later is not None:
            next_shed = {"date": plan_days[later], "level": levels[later],
                         "steps": [st.label for st in steps[:levels[later]]]}
    horizon_days = max(sum(p.hours for p in periods) / 24, 1 / 24)
    saving = (sum(w * p.hours for p, w in zip(periods, base_load, strict=True))
              - sum(w * p.hours for p, w in zip(periods, plan_load, strict=True))) / horizon_days
    effects = [{"step": e["step"], "saved_wh_per_day": round(e["saved_wh"] / horizon_days),
                "also_cuts": e["also_cuts"]} for e in loads.step_effects(periods, applied)]

    if generator_needed:
        status = Status.GENERATOR
    elif applied:
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
        step_total=len(steps),
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
        day_levels=list(zip(plan_days, levels, strict=True)),
        next_shed=next_shed,
    )


def generator_runs(sc: ScenarioPlan) -> list[dict]:
    """The recommended generator runs, in order: when, how long, energy into the battery and fuel."""
    res = sc.with_plan
    if not sc.generator_needed or not res.generator_hours:
        return []
    per_h = {"wh": res.generator_wh / res.generator_hours, "fuel_l": res.fuel_l / res.generator_hours}
    runs: list[dict] = []
    for start, on in zip(sc.starts, res.generator_on, strict=False):  # periods are hourly
        if not on:
            continue
        end = start + dt.timedelta(hours=1)
        if runs and runs[-1]["end"] == start:
            runs[-1]["end"] = end
            runs[-1]["hours"] += 1.0
        else:
            runs.append({"start": start, "end": end, "hours": 1.0})
    return [{**r, "kwh": round(r["hours"] * per_h["wh"] / 1000, 2), "fuel_l": round(r["hours"] * per_h["fuel_l"], 2)}
            for r in runs]


def generator_runs_iso(sc: ScenarioPlan) -> list[dict]:
    """generator_runs with ISO timestamps, for entity attributes and service responses."""
    return [{**r, "start": r["start"].isoformat(), "end": r["end"].isoformat()} for r in generator_runs(sc)]


def daily_summary(sc: ScenarioPlan, tz: str) -> list[DaySummary]:
    """Per local day: energy in and out, lowest, highest and end-of-day SOC, with and without the plan."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    days: dict[dt.date, DaySummary] = {}
    gen_on = sc.generator_needed
    level_of = dict(sc.day_levels)
    for i, start in enumerate(sc.starts):
        day = (start + dt.timedelta(minutes=30)).astimezone(zone).date()
        d = days.get(day)
        if d is None:
            d = days[day] = DaySummary(day, 0.0, 0.0, 0.0, 101.0, 101.0, 0.0, shed_level=level_of.get(day, 0))
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


def week_rows(daily: dict[str, list[DaySummary]], planning: str) -> list[dict]:
    """One row per local day for dashboards: expected vs planning-scenario solar, load and SOC."""
    expected = {x.day: x for x in daily.get("expected", [])}
    plan = {x.day: x for x in daily.get(planning, [])}
    rows = []
    for day in sorted(set(expected) | set(plan)):
        e, p = expected.get(day), plan.get(day) or expected.get(day)
        rows.append({
            "date": day.isoformat(),
            "pv_kwh": round(e.pv_wh / 1000, 2) if e else None,
            "pv_bad_kwh": round(p.pv_wh / 1000, 2),
            "load_kwh": round(p.load_no_action_wh / 1000, 2),
            "load_planned_kwh": round(p.load_wh / 1000, 2),
            "min_soc": round(p.min_soc),
            "min_soc_no_action": round(p.min_soc_no_action),
            "max_soc": round(p.max_soc),
            "end_soc": round(p.end_soc),
            "generator_h": p.generator_hours,
            "shed_level": p.shed_level,
        })
    return rows


def _steps_text(sc: ScenarioPlan) -> str:
    parts = []
    for e in sc.step_effects:
        also = f" (also cuts {', '.join(e['also_cuts'])})" if e["also_cuts"] else ""
        parts.append(f"{e['step']}{also}")
    return "; ".join(parts)


def _steps_range(n: int) -> str:
    return f"step{'s 1–' + str(n) if n > 1 else ' 1'}"


def _later(sc: ScenarioPlan) -> str:
    ns = sc.next_shed
    if not ns:
        return ""
    return f" From {ns['date']:%a %m-%d}: shed {_steps_range(ns['level'])} ({'; '.join(ns['steps'])})."


def advice(sc: ScenarioPlan) -> str:
    if sc.status == Status.GENERATOR:
        r = sc.with_plan
        when = r.generator_first_start.isoformat() if r.generator_first_start else "soon"
        return (f"Generator needed: first run {when}, about {r.generator_hours:.0f} h and "
                f"{r.fuel_l:.1f} L over the next week, even with every shed step applied.")
    if sc.status == Status.SHED:
        n = len(sc.shed_steps)
        return (f"Apply shed {_steps_range(n)}: {_steps_text(sc)}. "
                f"Saves ~{sc.shed_saving_wh_per_day:.0f} Wh/day and keeps the battery above the reserve."
                + _later(sc))
    if sc.status == Status.TILT:
        return f"Tilt the trailer by day: ~{sc.tilt_gain_wh_per_day:.0f} Wh/day more solar." + _later(sc)
    return ("No shedding needed today." + _later(sc)) if sc.next_shed else "No action needed."


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


def model_day_pv(models: dict[str, list[WeatherPeriod]], expected: list[WeatherPeriod], now: dt.datetime,
                 lat: float, lon: float, array: ArrayConfig, tz: str) -> dict[str, dict[str, float]]:
    """Flat-array PV per forecast model: {"today": rest of today Wh, "tomorrow": Wh}, plus "Main" (expected).

    Temperatures come from the main forecast. A model only gets a day it covers for every daylight hour
    (HRRR stops at 48 h), so a short model never looks like a dark day.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    temps = {p.start: p.temp_c for p in expected}

    def local_day(p):
        return (p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone).date()

    wanted = {"today": today, "tomorrow": today + dt.timedelta(days=1)}
    daylight = {name: {p.start for p in expected if local_day(p) == day and p.ghi > 0
                       and (name != "today" or p.start + dt.timedelta(hours=p.hours) > now)}
                for name, day in wanted.items()}
    out: dict[str, dict[str, float]] = {}
    for model, periods in {"Main": expected, **models}.items():
        periods = [replace(p, temp_c=temps.get(p.start, p.temp_c)) for p in periods] if model != "Main" else periods
        days = {}
        for name, day in wanted.items():
            sel = [p for p in periods if local_day(p) == day
                   and (name != "today" or p.start + dt.timedelta(hours=p.hours) > now)]
            if not daylight[name] or not daylight[name] <= {p.start for p in sel}:
                continue
            days[name] = round(sum(w * p.hours for p, w in zip(sel, pv_series(sel, lat, lon, array), strict=True)))
        if days:
            out[model] = days
    return out

import datetime as dt
import math

import pytest
from core.battery import BatteryConfig, GeneratorConfig, simulate
from core.loads import BaseLoad, LoadModel, parse_load, parse_step
from core.planner import PlannerConfig, Status, make_plan
from core.pv import ArrayConfig, TiltPlan, low_sun_factor, pv_series
from core.solar import erbs_split, position
from core.weather import WeatherPeriod, parse_ensemble, parse_forecast, percentile_member, pessimistic

UTC = dt.UTC
TZ = "America/Phoenix"
YUMA = (32.69, -114.63)
COLO = (40.5, -105.5)


def week(start: dt.datetime, lat: float, lon: float, sky: float = 1.0, temp: float = 12.0, days: int = 7):
    """Hourly clear-sky (Haurwitz) weather scaled by `sky` (1 = clear)."""
    out = []
    for i in range(24 * days):
        t = start + dt.timedelta(hours=i)
        elev, _ = position(t + dt.timedelta(minutes=30), lat, lon)
        cz = math.sin(math.radians(elev))
        ghi = 1098 * cz * math.exp(-0.057 / cz) * sky if cz > 0.01 else 0.0
        out.append(WeatherPeriod(start=t, hours=1.0, ghi=ghi, temp_c=temp))
    return out


# --- solar -----------------------------------------------------------------

def test_solar_noon_june_colorado():
    elev, az = position(dt.datetime(2026, 6, 21, 19, 2, tzinfo=UTC), *COLO)
    assert elev == pytest.approx(72.9, abs=0.3)
    assert az == pytest.approx(180, abs=5)


def test_erbs_split_conserves_ghi():
    beam, diffuse = erbs_split(600, 45, dt.datetime(2026, 6, 1, tzinfo=UTC))
    assert beam + diffuse == pytest.approx(600)
    assert 0 < diffuse < beam


# --- pv --------------------------------------------------------------------

def test_low_sun_factor_site_horizon():
    typical = ArrayConfig()
    open_site = ArrayConfig(site_horizon=0.5)
    assert low_sun_factor(12, typical) == pytest.approx(0.51)
    assert low_sun_factor(12, open_site) == pytest.approx(0.755)
    assert low_sun_factor(1, typical) == 0.0
    assert low_sun_factor(50, typical) == 1.0


def test_tilt_helps_in_december_and_only_by_day():
    start = dt.datetime(2026, 12, 20, 7, tzinfo=UTC)
    wx = week(start, *COLO, days=1)
    arr = ArrayConfig()
    flat = sum(pv_series(wx, *COLO, arr))
    tilt = TiltPlan(tilt_deg=10, start_hour=0, end_hour=24, tz="America/Denver")
    tilted = sum(pv_series(wx, *COLO, arr, tilt))
    assert 1.2 < tilted / flat < 1.5
    north = TiltPlan(tilt_deg=10, panel_az=0, start_hour=0, end_hour=24, tz="America/Denver")
    assert sum(pv_series(wx, *COLO, arr, north)) < flat
    night_only = TiltPlan(tilt_deg=10, start_hour=20, end_hour=23, tz="America/Denver")
    assert sum(pv_series(wx, *COLO, arr, night_only)) == pytest.approx(flat)


def test_clear_december_day_matches_analysis():
    # analysis/: typical Colorado camp, clear Dec 21, flat ≈ 1.9–2.1 kWh at the PV input.
    wx = week(dt.datetime(2026, 12, 21, 7, tzinfo=UTC), *COLO, days=1)
    wh = sum(pv_series(wx, *COLO, ArrayConfig(charger_efficiency=1.0)))
    assert 1500 < wh < 2600


# --- loads -----------------------------------------------------------------

def test_parse_load_variants():
    ld = parse_load("Espresso", "1200 W, 0.3 h/day in 7-10, weekdays, needs inverter")
    assert (ld.watts, ld.hours_per_day, ld.needs_inverter) == (1200, 0.3, True)
    assert ld.schedule.describe() == "7-10 weekdays"
    star = parse_load("Starlink", "60W 8:00-17:00 weekdays DC")
    assert star.schedule.describe() == "8-17 weekdays" and not star.needs_inverter
    pi = parse_load("Pi", "6 W essential dc")
    assert pi.essential and pi.schedule.describe() == "24/7"
    inv = parse_load("Inverter", "35 W idle, 24/7, supply (estimate)")
    assert inv.supply and not inv.needs_inverter
    assert parse_load("Mystery", "sometimes") is None
    assert not parse_load("NAS", "40 W, 18-22", completed=True).in_use
    assert parse_load("Espresso", "1180 W measured, 0.3 h/day in 7-10").battery_side


def test_parse_step_forms():
    st = parse_step("NAS → evenings", "NAS: 18-22")
    assert st.load_name == "NAS" and st.schedule.describe() == "18-22"
    assert parse_step("Inverter -> off", None).schedule.describe() == "off"
    assert parse_step("Starlink → work hours", "Starlink: 8:30-17 weekdays, off weekends").schedule.describe() == \
        "8:30-17 weekdays, off weekends"
    assert parse_step("Just a note", None) is None
    assert not parse_step("NAS → off", "NAS: off", completed=True).enabled


def test_schedule_wraps_midnight_and_days():
    sch = parse_step("x", "Heater: 22-2 weekdays").schedule
    mon_23 = dt.datetime(2026, 9, 14, 23, tzinfo=UTC)
    sat_23 = dt.datetime(2026, 9, 19, 23, tzinfo=UTC)
    assert sch.active(mon_23) and not sch.active(sat_23)


def _trailer(**kw):
    loads = (
        parse_load("Inverter", "35 W idle, 24/7, supply"),
        parse_load("NAS", "40 W, 24/7"),
        parse_load("Starlink", "60 W, 24/7"),
        parse_load("Espresso", "1200 W, 0.5 h/day in 7-9"),
        parse_load("Router", "10 W, 24/7, DC, essential"),
    )
    steps = tuple(parse_step(lbl, desc) for lbl, desc in (
        ("Espresso → off", "Espresso: off"),
        ("NAS → evenings", "NAS: 18-22"),
        ("Inverter → 8-22", "Inverter: 8-22"),
        ("Router → off", "Router: off"),  # essential: never applied
        ("NAS → off", "NAS: off"),
        ("Inverter → off", "Inverter: off"),
    ))
    return LoadModel(BaseLoad(baseline_w=0, fridge_w=0, heat_w_per_degc=0), loads, steps, tz="UTC",
                     inverter_efficiency=1.0, **kw)


def test_steps_apply_in_order_and_skip_essential():
    lm = _trailer()
    assert [st.label for st in lm.active_steps()] == [
        "Espresso → off", "NAS → evenings", "Inverter → 8-22", "NAS → off", "Inverter → off"]
    day = [WeatherPeriod(dt.datetime(2026, 9, 14, h, tzinfo=UTC), 1.0, 0, 10.0) for h in range(24)]
    wh = [sum(lm.series(day, level)) for level in range(6)]
    # level 0: inverter 35×24 + NAS 40×24 + Starlink 60×24 + espresso 600 + router 240
    assert wh[0] == pytest.approx(840 + 960 + 1440 + 600 + 240)
    assert wh[1] == pytest.approx(wh[0] - 600)
    assert wh[2] == pytest.approx(wh[1] - 40 * 20)  # NAS only 18-22
    # Inverter 8-22 also stops Starlink 22-08 (10 h) and saves its own idle 10 h; NAS already within 18-22.
    assert wh[3] == pytest.approx(wh[2] - 35 * 10 - 60 * 10)
    assert wh[5] == pytest.approx(240)  # only the DC essential router is left


def test_most_restrictive_step_wins_regardless_of_order():
    lm = _trailer()
    reordered = LoadModel(lm.base, lm.loads, (parse_step("NAS → off", "NAS: off"),
                                              parse_step("NAS → evenings", "NAS: 18-22")), tz="UTC")
    evening = dt.datetime(2026, 9, 14, 19, tzinfo=UTC)
    assert "NAS" not in reordered.power_w(evening, 10.0, level=2)[1]


def test_step_effects_report_dependent_loads():
    lm = _trailer()
    day = [WeatherPeriod(dt.datetime(2026, 9, 14, h, tzinfo=UTC), 1.0, 0, 10.0) for h in range(24)]
    effects = lm.step_effects(day, 3)
    assert effects[2]["step"] == "Inverter → 8-22"
    assert effects[2]["also_cuts"] == ["Starlink"]
    assert effects[2]["saved_wh"] == pytest.approx(35 * 10 + 60 * 10)


def test_problems_flag_unknown_load():
    lm = LoadModel(BaseLoad(), (parse_load("NAS", "40 W"),), (parse_step("Ghost → off", "Ghost: off"),))
    assert lm.problems() == ["Shed step 'Ghost → off': no load named 'Ghost'"]
    assert lm.active_steps() == []


# --- battery ---------------------------------------------------------------

def _flat_periods(n, start=dt.datetime(2026, 9, 14, 0, tzinfo=UTC)):
    return [WeatherPeriod(start + dt.timedelta(hours=i), 1.0, 0.0, 10.0) for i in range(n)]


def test_simulate_discharge_and_reserve():
    periods = _flat_periods(10)
    res = simulate(periods, 50, [0] * 10, [312] * 10, BatteryConfig(), reserve_soc=20)
    assert res.soc[-1] == pytest.approx(0.0)  # 3120 Wh from a 6240 Wh bank at 50%
    assert res.first_below == periods[6].start  # 50% - 5%/h: ends below 20% after 7 h
    assert res.unmet_wh == pytest.approx(0, abs=1e-6)


def test_simulate_curtails_when_full():
    periods = _flat_periods(3)
    res = simulate(periods, 99, [1000] * 3, [0] * 3, BatteryConfig(charge_efficiency=1.0), 20)
    assert res.soc[-1] == pytest.approx(100)
    assert res.curtailed_wh == pytest.approx(3000 - 62.4)


def test_generator_respects_quiet_hours_and_stop():
    periods = _flat_periods(48)
    gen = GeneratorConfig(charge_w=1000, start_soc=25, stop_soc=40, tz="UTC",
                          allowed_start_hour=8, allowed_end_hour=20)
    res = simulate(periods, 26, [0] * 48, [100] * 48, BatteryConfig(charge_efficiency=1.0), 20, gen)
    assert res.generator_first_start.hour >= 8
    assert 0 < res.generator_hours < 30
    assert res.fuel_l > 0


# --- weather parsing ---------------------------------------------------------

def test_parse_forecast_shifts_to_period_start():
    data = {"hourly": {"time": ["2026-09-16T13:00"], "shortwave_radiation": [400],
                       "direct_radiation": [300], "diffuse_radiation": [100], "temperature_2m": [20]}}
    (p,) = parse_forecast(data)
    assert p.start == dt.datetime(2026, 9, 16, 12, tzinfo=UTC) and p.beam_h == 300


def test_ensemble_percentile_member_picks_whole_member():
    data = {"hourly": {"time": ["2026-09-16T13:00", "2026-09-16T14:00"],
                       "shortwave_radiation": [500, 500],
                       "shortwave_radiation_member01": [100, 100],
                       "shortwave_radiation_member02": [300, 300],
                       "temperature_2m": [10, 10]}}
    members = parse_ensemble(data)
    assert len(members) == 3
    assert percentile_member(members, 0.0)[0].ghi == 100
    assert percentile_member(members, 0.5)[0].ghi == 300


def test_pessimistic_scales_main_forecast_by_ensemble_spread():
    start = dt.datetime(2026, 9, 16, 0, tzinfo=UTC)
    main = [WeatherPeriod(start + dt.timedelta(hours=h), 1.0, 500.0, 20.0, 400.0, 100.0) for h in range(48)]

    def member(day1, day2):
        return [WeatherPeriod(start + dt.timedelta(hours=h), 1.0, day1 if h < 24 else day2, 20.0) for h in range(48)]

    # Ensemble is much brighter overall than the main model; only its relative spread should matter.
    members = {"a": member(900, 900), "b": member(450, 900), "c": member(900, 300), "d": member(900, 900),
               "e": member(900, 900)}
    bad = pessimistic(main, members, q=0.0)
    assert bad[0].ghi == pytest.approx(500)  # darkest member "c" is normal on day 1
    assert bad[30].ghi == pytest.approx(500 * 300 / 900) and bad[30].beam_h == pytest.approx(400 / 3)
    assert all(b.ghi <= m.ghi for b, m in zip(bad, main, strict=True))


# --- planner ---------------------------------------------------------------

def _loads():
    loads = (
        parse_load("Espresso machine", "1200 W, 0.3 h/day in 7-10"),
        parse_load("Dishwasher", "900 W, 1 h/day in 12-15"),
        parse_load("Ice maker", "120 W, 6 h/day in 10-18"),
        parse_load("NAS", "40 W, 18-22"),
        parse_load("Starlink + router", "60 W, 8-17 weekdays, DC"),
    )
    steps = tuple(parse_step(f"{ld.name} → off", f"{ld.name}: off") for ld in loads)
    return LoadModel(BaseLoad(heat_w_per_degc=0), loads, steps, tz=TZ)


def _plan(sky, soc, month=9, loc=YUMA, temp=15.0):
    now = dt.datetime(2026, month, 14, 15, tzinfo=UTC)
    cfg = PlannerConfig(reserve_soc=20, tilt=TiltPlan(tilt_deg=10, tz=TZ))
    wx = week(now - dt.timedelta(hours=now.hour), *loc, sky=sky, temp=temp, days=8)
    return make_plan({"expected": wx}, now, soc, *loc, ArrayConfig(), BatteryConfig(), _loads(),
                     GeneratorConfig(tz=TZ), cfg)


def test_plan_sunny_september_is_ok():
    plan = _plan(sky=1.0, soc=80)
    sc = plan.scenarios["expected"]
    assert plan.status == Status.OK
    assert sc.pv_tomorrow_wh > 4000


def test_plan_gloomy_week_sheds_in_priority_order():
    plan = _plan(sky=0.35, soc=70)
    sc = plan.scenarios["expected"]
    assert plan.status in (Status.SHED, Status.GENERATOR)
    if plan.status == Status.SHED:
        assert sc.shed_steps[0] == "Espresso machine → off"
        assert sc.with_plan.min_soc >= 20


def test_plan_dark_week_needs_generator():
    plan = _plan(sky=0.05, soc=40, month=12)
    sc = plan.scenarios["expected"]
    assert plan.status == Status.GENERATOR
    assert sc.generator_needed and sc.with_plan.generator_hours > 0
    assert sc.with_plan.min_soc >= 20  # starts early enough despite quiet hours
    assert sc.with_plan.generator_first_start is not None
    assert sc.no_action.first_below is not None


def test_plan_december_recommends_tilt():
    plan = _plan(sky=1.0, soc=60, month=12)
    assert plan.scenarios["expected"].tilt_recommended
    assert plan.scenarios["expected"].tilt_gain_wh_per_day > 300


def test_planning_scenario_defaults_to_worst():
    now = dt.datetime(2026, 12, 14, 15, tzinfo=UTC)
    good = week(now - dt.timedelta(hours=15), *YUMA, sky=1.0, days=8)
    bad = week(now - dt.timedelta(hours=15), *YUMA, sky=0.05, days=8)
    plan = make_plan({"expected": good, "bad": bad}, now, 40, *YUMA, ArrayConfig(), BatteryConfig(),
                     _loads(), GeneratorConfig(tz=TZ), PlannerConfig(tilt=TiltPlan(tilt_deg=10, tz=TZ)))
    assert plan.planning_scenario == "bad"
    assert plan.status == Status.GENERATOR


ESPRESSO = "1549 W measured, 0.1 h/day in {} weekdays, needs inverter"


def test_problem_when_duty_window_starts_before_the_inverter():
    lm = LoadModel(BaseLoad(), (parse_load("Inverter", "35 W idle, 8-21, supply"),
                                parse_load("Espresso machine", ESPRESSO.format("7-10")),
                                parse_load("Starlink", "60 W, 24/7, needs inverter"),
                                parse_load("Fan", "16 W, 4 h/day in 6-21, DC")), tz="America/Denver")
    problems = lm.problems()
    assert len(problems) == 1  # always-on AC loads and DC loads are fine
    assert problems[0].startswith("Load 'Espresso machine': 33% of its window (7-10 weekdays)")
    fixed = LoadModel(BaseLoad(), (parse_load("Inverter", "35 W idle, 8-21, supply"),
                                   parse_load("Espresso machine", ESPRESSO.format("8-10"))),
                      tz="America/Denver")
    assert fixed.problems() == []

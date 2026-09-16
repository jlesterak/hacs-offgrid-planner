import datetime as dt
import math

import pytest
from core.battery import BatteryConfig, GeneratorConfig, simulate
from core.loads import BaseLoad, Load, LoadModel, parse_load
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
    ld = parse_load("Espresso", "1200 W, 0.3 h/day, 7-10, weekdays")
    assert (ld.watts, ld.hours_per_day, ld.window, ld.days, ld.ac) == (1200, 0.3, (7, 10), "weekdays", True)
    star = parse_load("Starlink", "60W 9h 8:00-17:00 weekdays DC")
    assert star.window == (8, 17) and not star.ac and star.hours_per_day == 9
    fridge = parse_load("Pi", "6 W essential dc")
    assert fridge.essential and fridge.hours_per_day == 24
    assert parse_load("Mystery", "sometimes") is None
    assert not parse_load("NAS", "40 W, 4 h, 18-22", completed=True).in_use


def test_load_window_wraps_midnight_and_weekdays():
    ld = Load("Heater", 100, hours_per_day=4, window=(22, 2), days="weekdays")
    mon_23 = dt.datetime(2026, 9, 14, 23, tzinfo=UTC)
    sat_23 = dt.datetime(2026, 9, 19, 23, tzinfo=UTC)
    assert ld.active(mon_23) and not ld.active(sat_23)
    assert ld.mean_w(mon_23, 1.0) == pytest.approx(100)  # 4 h in a 4 h window


def test_shed_order_follows_priority():
    wx = week(dt.datetime(2026, 9, 14, 7, tzinfo=UTC), *YUMA, days=1)
    loads = LoadModel(BaseLoad(heat_w_per_degc=0), (
        Load("Espresso", 1200, 0.5, (7, 9)),
        Load("Router", 10, essential=True, ac=False),
        Load("NAS", 40, 4, (18, 22)),
    ), tz=TZ, inverter_efficiency=1.0)
    assert [ld.name for ld in loads.sheddable()] == ["Espresso", "NAS"]
    full, shed1 = sum(loads.series(wx)), sum(loads.series(wx, 1))
    assert full - shed1 == pytest.approx(600)
    assert loads.daily_wh(loads.loads[2]) == pytest.approx(160)


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
    return LoadModel(BaseLoad(heat_w_per_degc=0), (
        Load("Espresso machine", 1200, 0.3, (7, 10)),
        Load("Dishwasher", 900, 1, (12, 15)),
        Load("Ice maker", 120, 6, (10, 18)),
        Load("NAS", 40, 4, (18, 22)),
        Load("Starlink + router", 60, 9, (8, 17), days="weekdays", ac=False),
    ), tz=TZ)


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
        assert sc.shed_loads[0] == "Espresso machine"
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

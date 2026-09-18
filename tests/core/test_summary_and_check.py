import datetime as dt

import pytest
from core.battery import BatteryConfig, GeneratorConfig
from core.loadcheck import EnergyMeter, LoadCheck, fitted_baseline_w, hour_key, last_night
from core.loads import BaseLoad, LoadModel, parse_load, parse_step
from core.planner import PlannerConfig, daily_summary, make_plan
from core.pv import ArrayConfig, TiltPlan
from core.solar import position
from core.weather import WeatherPeriod

UTC = dt.UTC
YUMA = (32.69, -114.63)
TZ = "America/Phoenix"


def _clear(start, days, sky=1.0):
    import math
    out = []
    for i in range(24 * days):
        t = start + dt.timedelta(hours=i)
        e, _ = position(t + dt.timedelta(minutes=30), *YUMA)
        cz = math.sin(math.radians(e))
        out.append(WeatherPeriod(t, 1.0, 1098 * cz * math.exp(-0.057 / cz) * sky if cz > 0.01 else 0.0, 15.0))
    return out


def test_daily_summary_matches_hourly_series():
    now = dt.datetime(2026, 12, 14, 15, tzinfo=UTC)
    wx = _clear(now - dt.timedelta(hours=15), 8, sky=0.05)
    loads = LoadModel(BaseLoad(heat_w_per_degc=0), (parse_load("Espresso", "1200 W, 0.3 h/day in 7-10"),),
                      (parse_step("Espresso → off", "Espresso: off"),), tz=TZ)
    plan = make_plan({"bad": wx}, now, 35, *YUMA, ArrayConfig(), BatteryConfig(), loads, GeneratorConfig(tz=TZ),
                     PlannerConfig(tilt=TiltPlan(tilt_deg=10, tz=TZ)))
    sc = plan.scenarios["bad"]
    days = daily_summary(sc, TZ)
    assert len(days) == 8  # today (partial) + 7
    assert sum(d.pv_wh for d in days) == pytest.approx(sc.pv_horizon_wh)
    assert min(d.min_soc for d in days) == pytest.approx(sc.with_plan.min_soc)
    assert max(d.max_soc for d in days) == pytest.approx(max(sc.with_plan.soc))
    assert all(d.min_soc <= d.end_soc <= d.max_soc for d in days)
    assert sum(d.generator_hours for d in days) == pytest.approx(sc.with_plan.generator_hours)
    assert days[-1].end_soc == pytest.approx(sc.with_plan.soc[-1])
    assert all(d.load_wh <= d.load_no_action_wh + 1e-6 for d in days)
    assert sc.pv_today_full_wh >= sc.pv_today_wh


def test_energy_meter_integrates_and_splits_hours():
    m = EnergyMeter()
    t0 = dt.datetime(2026, 9, 16, 4, 59, 30, tzinfo=UTC)
    for s in range(0, 61):
        m.add(t0 + dt.timedelta(seconds=s), 72.0)
    assert m.hours[hour_key(t0)][0] == pytest.approx(72 * 30 / 3600)
    assert m.hours[hour_key(t0 + dt.timedelta(minutes=1))][1] == pytest.approx(30)
    m.add(t0 + dt.timedelta(minutes=10), 72.0)  # 9-minute gap: not integrated
    assert sum(v[1] for v in m.hours.values()) == pytest.approx(60)


def test_last_night_compares_measured_with_model():
    lat, lon = YUMA
    # Local night in Phoenix (UTC-7) ≈ 02:00–13:00 UTC in September.
    now = dt.datetime(2026, 9, 16, 16, 30, tzinfo=UTC)
    m = EnergyMeter()
    modelled = {}
    for h in range(0, 16):
        start = dt.datetime(2026, 9, 16, h, tzinfo=UTC)
        m.hours[hour_key(start)] = [90.0, 3600.0]  # measured 90 Wh/h
        modelled[hour_key(start)] = 75.0
    check = last_night(now, lat, lon, m, modelled)
    assert check is not None and check.hours >= 8
    assert check.ratio == pytest.approx(1.2)
    # Poor coverage → no check.
    for v in m.hours.values():
        v[1] = 1000.0
    assert last_night(now, lat, lon, m, modelled) is None


def test_fitted_baseline_moves_the_nightly_gap_into_the_baseline():
    t = dt.datetime(2026, 9, 17, 4, tzinfo=UTC)
    check = LoadCheck(measured_wh=1000.0, modelled_wh=800.0, hours=10, night_start=t, night_end=t)
    assert fitted_baseline_w(check, 43.0) == pytest.approx(63.0)
    low = LoadCheck(measured_wh=100.0, modelled_wh=800.0, hours=10, night_start=t, night_end=t)
    assert fitted_baseline_w(low, 43.0) == 0.0  # never negative


def test_always_on_lists_base_parts_and_essential_loads():
    t = dt.datetime(2026, 12, 14, 0, tzinfo=UTC)
    periods = [WeatherPeriod(t, 1.0, 0.0, 5.0), WeatherPeriod(t + dt.timedelta(hours=1), 1.0, 0.0, 15.0)]
    loads = LoadModel(BaseLoad(baseline_w=10, fridge_w=27, heat_w_per_degc=2),
                      (parse_load("Heat tape", "33 W, 24/7, DC, essential"), parse_load("NAS", "40 W, 24/7")), tz=TZ)
    rows = {r["name"]: r for r in loads.always_on(periods)}
    assert rows["Baseline (everything not listed)"]["watts"] == 10
    assert rows["Fridge (average)"]["watts"] == 27
    assert rows["Furnace blower (next 24 h average)"]["watts"] == 10  # (2 × 10 °C + 0) / 2 h
    assert rows["Heat tape"] == {"name": "Heat tape", "watts": 33, "when": "24/7"}
    assert "NAS" not in rows

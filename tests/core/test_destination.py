"""Planned destination: forecast vs past years, leap days, and which past year is 'expected' vs 'bad week'."""
import datetime as dt

from core import destination as dest
from core.weather import WeatherPeriod

TODAY = dt.date(2026, 10, 31)


def test_near_dates_use_the_forecast_and_far_dates_use_past_years():
    assert dest.source_for(TODAY, 7, TODAY) == "forecast"
    assert dest.source_for(TODAY + dt.timedelta(days=8), 7, TODAY) == "forecast"  # last day is today + 15
    assert dest.source_for(TODAY + dt.timedelta(days=9), 7, TODAY) == "climatology"
    assert dest.source_for(dt.date(2028, 7, 8), 14, TODAY) == "climatology"


def test_climatology_asks_three_past_years_on_the_same_dates():
    reqs = dest.requests("climatology", 59.92, -149.66, dt.date(2028, 7, 8), 14)
    assert list(reqs) == ["2027", "2026", "2025"]
    url, params = reqs["2026"]
    assert url == dest.ARCHIVE_URL
    assert (params["start_date"], params["end_date"]) == ("2026-07-07", "2026-07-22")  # a spare day each side
    url, params = dest.requests("forecast", 59.92, -149.66, TODAY, 3)["forecast"]
    assert url != dest.ARCHIVE_URL and params["start_date"] == "2026-10-30"


def test_leap_day_maps_to_28_february():
    assert dest.same_day(dt.date(2028, 2, 29), 2027) == dt.date(2027, 2, 28)


def _year(day: dt.date, ghi: float) -> dict:
    t0 = dt.datetime(day.year, day.month, day.day)
    return {"hourly": {"time": [(t0 + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48)],
                       "shortwave_radiation": [ghi] * 48, "temperature_2m": [10.0] * 48}}


def test_middle_year_is_expected_dimmest_is_bad_week_and_both_land_on_trip_dates():
    start = dt.date(2028, 7, 8)
    first, _ = dest.fetch_window(start, 1)
    raw = {str(y): _year(dest.same_day(first, y), ghi) for y, ghi in ((2027, 300.0), (2026, 100.0), (2025, 200.0))}
    sc = dest.scenarios("climatology", raw, start, 1)
    assert sc["expected"][0].ghi == 200.0 and sc["bad_week"][0].ghi == 100.0
    assert (sc["expected"][0].start + dt.timedelta(hours=1)).date() == first  # Open-Meteo labels hour-ending


def test_no_usable_data_gives_no_scenarios():
    assert dest.scenarios("forecast", {}, TODAY, 3) == {}
    assert dest.scenarios("climatology", {}, dt.date(2028, 7, 8), 3) == {}
    assert dest.climate_scenarios({"2027": [WeatherPeriod(dt.datetime(2027, 7, 8, tzinfo=dt.UTC), 1.0, 5.0, 10.0)]}
                                  ).keys() == {"expected"}

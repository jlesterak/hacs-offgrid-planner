"""Planned-destination weather: the forecast when the dates are near, otherwise the same dates in past years."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

from .weather import FORECAST_URL, HOURLY_VARS, WeatherPeriod, parse_forecast, round_coord

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_AHEAD_DAYS = 15  # Open-Meteo serves 16 days including today
CLIMATE_YEARS = 3
MAX_DAYS = 31


def fetch_window(start: dt.date, days: int) -> tuple[dt.date, dt.date]:
    """One spare day each side (inclusive), so local days in any time zone are covered by GMT data."""
    return start - dt.timedelta(days=1), start + dt.timedelta(days=days)


def source_for(start: dt.date, days: int, today: dt.date) -> str:
    first, last = fetch_window(start, days)
    near = today - dt.timedelta(days=1) <= first and last <= today + dt.timedelta(days=FORECAST_AHEAD_DAYS)
    return "forecast" if near else "climatology"


def same_day(day: dt.date, year: int) -> dt.date:
    try:
        return day.replace(year=year)
    except ValueError:  # 29 February
        return day.replace(year=year, day=28)


def requests(source: str, lat: float, lon: float, start: dt.date, days: int) -> dict[str, tuple[str, dict]]:
    """{label: (url, params)}: the forecast, or one archive request per past year (label = the year)."""
    first, last = fetch_window(start, days)

    def params(a: dt.date, b: dt.date) -> dict[str, str]:
        return {"latitude": str(round_coord(lat)), "longitude": str(round_coord(lon)),
                "hourly": ",".join(HOURLY_VARS), "start_date": a.isoformat(), "end_date": b.isoformat(),
                "timezone": "GMT"}

    if source == "forecast":
        return {"forecast": (FORECAST_URL, params(first, last))}
    out = {}
    for year in range(start.year - 1, start.year - 1 - CLIMATE_YEARS, -1):
        a = same_day(first, year)
        out[str(year)] = (ARCHIVE_URL, params(a, a + (last - first)))
    return out


def climate_scenarios(years: dict[str, list[WeatherPeriod]]) -> dict[str, list[WeatherPeriod]]:
    """The middle year (by total sunshine) as expected and the dimmest as the bad week."""
    if not years:
        return {}
    ranked = sorted(years.values(), key=lambda ps: sum(p.ghi * p.hours for p in ps))
    out = {"expected": ranked[len(ranked) // 2]}
    if len(ranked) > 1:
        out["bad_week"] = ranked[0]
    return out


def scenarios(source: str, raw: dict[str, dict], start: dt.date, days: int) -> dict[str, list[WeatherPeriod]]:
    """Weather scenarios on the destination's dates from the raw responses. Empty if there is no usable data."""
    if source == "forecast":
        periods = parse_forecast(raw["forecast"]) if "forecast" in raw else []
        return {"expected": periods} if periods else {}
    first, _ = fetch_window(start, days)
    years = {}
    for label, data in raw.items():
        shift = first - same_day(first, int(label))  # whole days: moves the past year onto the trip dates
        periods = [replace(p, start=p.start + shift) for p in parse_forecast(data)]
        if periods:
            years[label] = periods
    return climate_scenarios(years)

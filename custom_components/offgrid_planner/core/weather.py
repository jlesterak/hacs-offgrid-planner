"""Weather periods and Open-Meteo request/response handling (no network code here)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
HOURLY_VARS = ("shortwave_radiation", "direct_radiation", "diffuse_radiation", "temperature_2m")
ENSEMBLE_VARS = ("shortwave_radiation", "temperature_2m")
ENSEMBLE_MODEL = "gfs025"


@dataclass(frozen=True)
class WeatherPeriod:
    """One period of weather. Radiation is the mean over [start, start + hours)."""

    start: dt.datetime  # aware, UTC
    hours: float
    ghi: float
    temp_c: float
    beam_h: float | None = None  # direct radiation on the horizontal; None = decompose GHI
    diffuse: float | None = None


def round_coord(x: float, places: int = 2) -> float:
    """~1 km: enough for weather, and avoids sending an exact campsite."""
    return round(x, places)


def forecast_params(lat: float, lon: float, days: int = 7) -> dict[str, str]:
    return {
        "latitude": str(round_coord(lat)),
        "longitude": str(round_coord(lon)),
        "hourly": ",".join(HOURLY_VARS),
        "forecast_days": str(days),
        "past_days": "1",
        "timezone": "GMT",
    }


def ensemble_params(lat: float, lon: float, days: int = 7) -> dict[str, str]:
    return {
        "latitude": str(round_coord(lat)),
        "longitude": str(round_coord(lon)),
        "hourly": ",".join(ENSEMBLE_VARS),
        "models": ENSEMBLE_MODEL,
        "forecast_days": str(days),
        "timezone": "GMT",
    }


def _period_start(time_str: str) -> dt.datetime:
    # Open-Meteo radiation values are the mean over the hour *ending* at `time`.
    end = dt.datetime.fromisoformat(time_str).replace(tzinfo=dt.UTC)
    return end - dt.timedelta(hours=1)


def _num(v, default=0.0) -> float:
    return default if v is None else float(v)


def parse_forecast(data: dict) -> list[WeatherPeriod]:
    h = data["hourly"]
    out = []
    for i, t in enumerate(h["time"]):
        if h["shortwave_radiation"][i] is None:
            continue
        out.append(WeatherPeriod(
            start=_period_start(t), hours=1.0,
            ghi=_num(h["shortwave_radiation"][i]),
            temp_c=_num(h["temperature_2m"][i], 10.0),
            beam_h=_num(h["direct_radiation"][i]) if h.get("direct_radiation") else None,
            diffuse=_num(h["diffuse_radiation"][i]) if h.get("diffuse_radiation") else None,
        ))
    return out


def parse_ensemble(data: dict) -> dict[str, list[WeatherPeriod]]:
    """Return {member: periods}. Member 0 is the control run."""
    h = data["hourly"]
    members: dict[str, list[WeatherPeriod]] = {}
    for key in h:
        if not key.startswith("shortwave_radiation"):
            continue
        suffix = key[len("shortwave_radiation"):]
        temps = h.get("temperature_2m" + suffix) or h.get("temperature_2m")
        periods = []
        for i, t in enumerate(h["time"]):
            if h[key][i] is None:
                continue
            periods.append(WeatherPeriod(start=_period_start(t), hours=1.0, ghi=_num(h[key][i]),
                                         temp_c=_num(temps[i] if temps else None, 10.0)))
        members[suffix.lstrip("_") or "member00"] = periods
    return members


def percentile_member(members: dict[str, list[WeatherPeriod]], q: float,
                     start: dt.datetime | None = None, hours: float | None = None) -> list[WeatherPeriod]:
    """The member whose total GHI over the window ranks at quantile q (0 = darkest).

    Picking a whole member keeps day-to-day weather consistent, unlike per-hour percentiles.
    """
    def total(periods):
        return sum(p.ghi * p.hours for p in periods
                   if (start is None or p.start >= start)
                   and (hours is None or start is None or p.start < start + dt.timedelta(hours=hours)))

    ranked = sorted(members.values(), key=total)
    if not ranked:
        return []
    return ranked[min(len(ranked) - 1, max(0, round(q * (len(ranked) - 1))))]

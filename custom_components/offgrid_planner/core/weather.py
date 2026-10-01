"""Weather periods, air quality and Open-Meteo request/response handling (no network code here)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
HOURLY_VARS = ("shortwave_radiation", "direct_radiation", "diffuse_radiation", "temperature_2m")
# Radiation only: the bad week scales the main forecast's radiation and keeps its temperatures.
ENSEMBLE_VARS = ("shortwave_radiation",)
# Pooled "grand ensemble": 31 GFS + 51 ECMWF IFS + 51 ECMWF AIFS members. Response keys carry a
# per-model suffix (gfs025 comes back as ncep_gefs025).
ENSEMBLE_MODELS = ("gfs025", "ecmwf_ifs025", "ecmwf_aifs025")
# Deterministic models to compare over the next 2 days. A model outside its domain (HRRR/NBM outside
# the US, HRDPS in the desert southwest) is simply missing from the response.
COMPARE_MODELS = ("ncep_hrrr_conus", "ncep_nbm_conus", "gem_hrdps_continental", "ecmwf_ifs", "ukmo_seamless",
                  "gem_seamless")
MODEL_NAMES = {
    "ncep_hrrr_conus": "HRRR", "ncep_nbm_conus": "NBM", "gem_hrdps_continental": "HRDPS",
    "ecmwf_ifs": "ECMWF IFS", "ukmo_seamless": "UKMO", "gem_seamless": "GEM",
    "ncep_gefs025": "GEFS", "ecmwf_ifs025_ensemble": "ECMWF IFS ens", "ecmwf_aifs025_ensemble": "ECMWF AIFS ens",
}
AIR_VARS = ("aerosol_optical_depth", "pm2_5", "dust", "us_aqi")


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
        "models": ",".join(ENSEMBLE_MODELS),
        "forecast_days": str(days),
        "timezone": "GMT",
    }


def compare_params(lat: float, lon: float, days: int = 3) -> dict[str, str]:
    return {
        "latitude": str(round_coord(lat)),
        "longitude": str(round_coord(lon)),
        "hourly": "shortwave_radiation",
        "models": ",".join(COMPARE_MODELS),
        "forecast_days": str(days),
        "timezone": "GMT",
    }


def air_params(lat: float, lon: float, days: int = 3) -> dict[str, str]:
    return {
        "latitude": str(round_coord(lat)),
        "longitude": str(round_coord(lon)),
        "hourly": ",".join(AIR_VARS),
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


def _split_member_key(suffix: str) -> tuple[str, str]:
    """'member03_ncep_gefs025' → ('ncep_gefs025', 'member03'); 'ncep_gefs025' → (…, 'member00').

    Single-model responses (and caches from 0.5) have no model suffix: the model is ''.
    """
    if suffix.startswith("member"):
        member, _, model = suffix.partition("_")
        return model, member
    return suffix, "member00"


def member_model(key: str) -> str:
    return key.partition("/")[0] if "/" in key else ""


def parse_ensemble(data: dict) -> dict[str, list[WeatherPeriod]]:
    """Return {"model/memberNN": periods} ("memberNN" for single-model data). member00 is the control run."""
    h = data["hourly"]
    members: dict[str, list[WeatherPeriod]] = {}
    for key in h:
        if not key.startswith("shortwave_radiation"):
            continue
        suffix = key[len("shortwave_radiation"):].lstrip("_")
        model, member = _split_member_key(suffix)
        temps = h.get(key.replace("shortwave_radiation", "temperature_2m", 1)) or h.get("temperature_2m")
        periods = []
        for i, t in enumerate(h["time"]):
            if h[key][i] is None:
                continue
            periods.append(WeatherPeriod(start=_period_start(t), hours=1.0, ghi=_num(h[key][i]),
                                         temp_c=_num(temps[i] if temps else None, 10.0)))
        if periods:
            members[f"{model}/{member}" if model else member] = periods
    return members


def parse_models(data: dict) -> dict[str, list[WeatherPeriod]]:
    """Multi-model forecast → {model: periods}. Temperatures are not requested (taken from the main forecast)."""
    h = data["hourly"]
    out: dict[str, list[WeatherPeriod]] = {}
    for key, values in h.items():
        if not key.startswith("shortwave_radiation_"):
            continue
        periods = [WeatherPeriod(start=_period_start(t), hours=1.0, ghi=float(v), temp_c=10.0)
                   for t, v in zip(h["time"], values, strict=True) if v is not None]
        if periods:
            out[key[len("shortwave_radiation_"):]] = periods
    return out


@dataclass(frozen=True)
class AirPeriod:
    """Air quality for the hour starting at `start` (CAMS via Open-Meteo)."""

    start: dt.datetime  # aware, UTC
    aod: float | None  # aerosol optical depth at 550 nm, unitless
    pm2_5: float | None  # µg/m³
    dust: float | None  # µg/m³
    us_aqi: float | None


def parse_air(data: dict) -> list[AirPeriod]:
    h = data["hourly"]

    def col(name, i):
        values = h.get(name)
        return None if values is None or values[i] is None else float(values[i])

    out = []
    for i, t in enumerate(h["time"]):
        p = AirPeriod(start=dt.datetime.fromisoformat(t).replace(tzinfo=dt.UTC), aod=col("aerosol_optical_depth", i),
                      pm2_5=col("pm2_5", i), dust=col("dust", i), us_aqi=col("us_aqi", i))
        if any(v is not None for v in (p.aod, p.pm2_5, p.dust, p.us_aqi)):
            out.append(p)
    return out


def _window_total(periods: list[WeatherPeriod], start: dt.datetime | None, hours: float | None) -> float:
    return sum(p.ghi * p.hours for p in periods
               if (start is None or p.start >= start)
               and (hours is None or start is None or p.start < start + dt.timedelta(hours=hours)))


def percentile_member(members: dict[str, list[WeatherPeriod]], q: float,
                     start: dt.datetime | None = None, hours: float | None = None) -> list[WeatherPeriod]:
    """The member whose total GHI over the window ranks at quantile q (0 = darkest).

    Picking a whole member keeps day-to-day weather consistent, unlike per-hour percentiles.
    """
    ranked = sorted(members.values(), key=lambda m: _window_total(m, start, hours))
    return _at_quantile(ranked, q) if ranked else []


def _by_model(members: dict[str, list[WeatherPeriod]]) -> dict[str, dict[str, list[WeatherPeriod]]]:
    groups: dict[str, dict[str, list[WeatherPeriod]]] = {}
    for key, periods in members.items():
        groups.setdefault(member_model(key), {})[key] = periods
    return groups


def _pool(members: dict[str, list[WeatherPeriod]], start: dt.datetime | None, hours: float | None):
    """[(score, member, its model's median member)], where score = member total ÷ its model's median total.

    Normalizing per model removes each model's brightness bias, so the pooled ranking reflects
    spread, not which model runs sunnier.
    """
    pool = []
    for group in _by_model(members).values():
        median = percentile_member(group, 0.5, start, hours)
        median_total = _window_total(median, start, hours)
        if median_total <= 0:
            continue
        pool.extend((_window_total(m, start, hours) / median_total, m, median) for m in group.values())
    pool.sort(key=lambda x: x[0])
    return pool


def _at_quantile(seq: list, q: float):
    return seq[min(len(seq) - 1, max(0, round(q * (len(seq) - 1))))]


def ensemble_summary(members: dict[str, list[WeatherPeriod]], q: float, start: dt.datetime | None = None,
                     hours: float | None = None) -> dict[str, dict[str, float | int]]:
    """Per model: member count and its q-member's window total ÷ its median, to see whether models agree."""
    out = {}
    for model, group in _by_model(members).items():
        pool = _pool(group, start, hours)
        if pool:
            out[MODEL_NAMES.get(model, model or "ensemble")] = {
                "members": len(group), "low_vs_median": round(_at_quantile(pool, q)[0], 2)}
    return out


def pessimistic(expected: list[WeatherPeriod], members: dict[str, list[WeatherPeriod]], q: float = 0.1,
                start: dt.datetime | None = None, hours: float | None = None) -> list[WeatherPeriod]:
    """Scale the main forecast by the ensemble's spread instead of using ensemble radiation directly.

    Ensemble models (e.g. GFS) can be biased brighter or darker than the best local model. So rank all
    members of all models by their window total relative to their own model's median, take the one at
    quantile q, and per UTC day multiply the main forecast by (that member's day total ÷ its model's
    median member's day total), capped at 1.
    """
    pool = _pool(members, start, hours)
    if not pool:
        return expected
    _, member, median = _at_quantile(pool, q)

    def by_day(periods):
        out: dict[dt.date, float] = {}
        for p in periods:
            out[p.start.date()] = out.get(p.start.date(), 0.0) + p.ghi * p.hours
        return out

    low, mid = by_day(member), by_day(median)
    ratio = {d: min(1.0, low[d] / mid[d]) if mid.get(d) else 1.0 for d in low}
    result = []
    for p in expected:
        r = ratio.get(p.start.date(), 1.0)
        result.append(replace(p, ghi=p.ghi * r,
                              beam_h=None if p.beam_h is None else p.beam_h * r,
                              diffuse=None if p.diffuse is None else p.diffuse * r))
    return result

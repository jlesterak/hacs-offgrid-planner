"""PV output model calibrated for a flat (or slightly tilted) roof array."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .solar import erbs_split, poa, position
from .weather import WeatherPeriod

# (lower elevation bound °, factor): measured ÷ modelled output for a flat roof array at mountain
# camps, July–September 2026 (typical site). Below the first bound the factor is 0.
DEFAULT_LOW_SUN = ((2.0, 0.25), (5.0, 0.39), (10.0, 0.51), (15.0, 0.63), (20.0, 0.69),
                   (25.0, 0.89), (30.0, 0.95), (35.0, 1.0))


@dataclass(frozen=True)
class ArrayConfig:
    rated_w: float = 1580.0
    k: float = 0.68  # system factor at high sun (fitted at the charge controller PV input)
    charger_efficiency: float = 0.95  # PV input → battery
    gamma: float = -0.0035  # power temperature coefficient per °C
    c_cell: float = 0.035  # cell temperature rise, °C per W/m²
    albedo: float = 0.2
    low_sun: tuple[tuple[float, float], ...] = DEFAULT_LOW_SUN
    # Site horizon factor s: effective low-sun factor = 1 - s × (1 - f). 1 = typical camp,
    # < 1 open horizon (desert), > 1 trees or canyon walls. Learned per site.
    site_horizon: float = 1.0


@dataclass(frozen=True)
class TiltPlan:
    """Tilt the trailer during local daytime hours and level it for the night."""

    tilt_deg: float = 0.0
    panel_az: float = 180.0
    start_hour: float = 9.0
    end_hour: float = 17.0
    tz: str = "UTC"
    _zone: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_zone", ZoneInfo(self.tz))

    def tilt_at(self, t_utc: dt.datetime) -> float:
        if self.tilt_deg <= 0:
            return 0.0
        local = t_utc.astimezone(self._zone)
        h = local.hour + local.minute / 60
        return self.tilt_deg if self.start_hour <= h < self.end_hour else 0.0

    def days_tilted(self, periods: list[WeatherPeriod]) -> int:
        return len({p.start.astimezone(self._zone).date() for p in periods if self.tilt_at(p.start) > 0})


def low_sun_factor(elev: float, cfg: ArrayConfig) -> float:
    f = 0.0
    for bound, value in cfg.low_sun:
        if elev >= bound:
            f = value
    return max(0.0, min(1.0, 1 - cfg.site_horizon * (1 - f)))


def pv_power_w(p: WeatherPeriod, lat: float, lon: float, cfg: ArrayConfig,
               tilt_deg: float = 0.0, panel_az: float = 180.0) -> float:
    """Mean power into the battery over the period (W), before curtailment."""
    mid = p.start + dt.timedelta(hours=p.hours / 2)
    elev, az = position(mid, lat, lon)
    if elev <= 0 or p.ghi <= 0:
        return 0.0
    if p.beam_h is None or p.diffuse is None:
        beam_h, diffuse = erbs_split(p.ghi, elev, mid)
    else:
        beam_h, diffuse = p.beam_h, p.diffuse
    g = poa(beam_h, diffuse, elev, az, tilt_deg, panel_az, cfg.albedo)
    t_cell = p.temp_c + cfg.c_cell * g
    dc = cfg.k * low_sun_factor(elev, cfg) * g / 1000 * cfg.rated_w * (1 + cfg.gamma * (t_cell - 25))
    return max(0.0, dc * cfg.charger_efficiency)


def pv_series(periods: list[WeatherPeriod], lat: float, lon: float, cfg: ArrayConfig,
              tilt: TiltPlan | None = None) -> list[float]:
    return [pv_power_w(p, lat, lon, cfg,
                       tilt.tilt_at(p.start) if tilt else 0.0,
                       tilt.panel_az if tilt else 180.0) for p in periods]

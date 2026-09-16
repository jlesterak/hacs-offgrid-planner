"""Battery state-of-charge simulation with an optional generator."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .weather import WeatherPeriod


@dataclass(frozen=True)
class BatteryConfig:
    capacity_wh: float = 6240.0  # 24 V 260 Ah LiFePO4
    charge_efficiency: float = 0.97  # energy into the cells per Wh delivered to the battery


@dataclass(frozen=True)
class GeneratorConfig:
    """Charging from a generator through the inverter-charger."""

    charge_w: float = 650.0  # into the battery (~25 A × 26 V)
    start_soc: float = 25.0  # start when SOC falls below this (%)
    stop_soc: float = 60.0
    allowed_start_hour: float = 8.0  # quiet hours: local time window when it may run
    allowed_end_hour: float = 20.0
    # Fuel at the generator's load. Honda EU2200i: 3.6 L tank, 8.1 h at 1/4 load, 3.2 h at 1800 W
    # (Honda specs; verify). Linear between those points.
    rated_w: float = 1800.0
    fuel_l_per_h_quarter: float = 3.6 / 8.1
    fuel_l_per_h_rated: float = 3.6 / 3.2
    ac_overhead_w: float = 100.0  # charger losses + AC loads carried while it runs
    tz: str = "UTC"
    _zone: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_zone", ZoneInfo(self.tz))

    def allowed(self, t_utc: dt.datetime) -> bool:
        local = t_utc.astimezone(self._zone)
        h = local.hour + local.minute / 60
        return self.allowed_start_hour <= h < self.allowed_end_hour

    def fuel_l_per_h(self) -> float:
        load = (self.charge_w / 0.9 + self.ac_overhead_w) / self.rated_w
        q, r = self.fuel_l_per_h_quarter, self.fuel_l_per_h_rated
        return q + (r - q) * max(0.0, min(1.0, (load - 0.25) / 0.75))


@dataclass
class SimResult:
    soc: list[float]  # SOC at the end of each period (%)
    min_soc: float
    min_soc_at: dt.datetime | None
    first_below: dt.datetime | None  # start of the first period that ends below the reserve
    empty_at: dt.datetime | None
    curtailed_wh: float
    unmet_wh: float
    generator_hours: float
    generator_first_start: dt.datetime | None
    fuel_l: float


def simulate(periods: list[WeatherPeriod], soc_start: float, pv_w: list[float], load_w: list[float],
             battery: BatteryConfig, reserve_soc: float,
             generator: GeneratorConfig | None = None) -> SimResult:
    energy = battery.capacity_wh * soc_start / 100
    cap = battery.capacity_wh
    soc: list[float] = []
    min_soc, min_at, first_below, empty_at = soc_start, None, None, None
    curtailed = unmet = gen_h = 0.0
    gen_on, gen_first = False, None
    for p, pv, load in zip(periods, pv_w, load_w, strict=True):
        pct = energy / cap * 100
        if generator is not None:
            if gen_on and (pct >= generator.stop_soc or not generator.allowed(p.start)):
                gen_on = False
            elif not gen_on and pct < generator.start_soc and generator.allowed(p.start):
                gen_on = True
                gen_first = gen_first or p.start
        gen = generator.charge_w if gen_on else 0.0
        if gen_on:
            gen_h += p.hours
        net_wh = (pv + gen - load) * p.hours
        if net_wh > 0:
            stored = net_wh * battery.charge_efficiency
            room = cap - energy
            if stored > room:
                curtailed += (stored - room) / battery.charge_efficiency
                stored = room
            energy += stored
        else:
            energy += net_wh
            if energy < 0:
                unmet += -energy
                empty_at = empty_at or p.start
                energy = 0.0
        pct = energy / cap * 100
        soc.append(pct)
        if pct < min_soc:
            min_soc, min_at = pct, p.start + dt.timedelta(hours=p.hours)
        if first_below is None and pct < reserve_soc:
            first_below = p.start
    fuel = gen_h * generator.fuel_l_per_h() if generator else 0.0
    return SimResult(soc, min_soc, min_at, first_below, empty_at, curtailed, unmet, gen_h, gen_first, fuel)

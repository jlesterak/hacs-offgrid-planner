"""Load model: an essential base (baseline, fridge, heating) plus a prioritised list of loads.

Loads can come from a Home Assistant to-do list: the item order is the shed priority
(top = shed first), the summary is the name, and the description holds the numbers, e.g.
    "1200 W, 0.3 h/day, 7-10, weekdays"      espresso machine, mornings on weekdays
    "60 W, 15 h/day, 8-17, weekdays, DC"     Starlink, only while the internet schedule allows
    "35 W, 24 h/day, essential"              never shed
    "1150 W measured, 0.3 h/day, 7-10"       learned at the battery: inverter losses already included
A completed (checked) item is a load that is not in use at all.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .weather import WeatherPeriod

DAYS = ("daily", "weekdays", "weekends")


@dataclass(frozen=True)
class Load:
    name: str
    watts: float  # while running, at the appliance
    hours_per_day: float = 24.0
    window: tuple[float, float] | None = None  # local [start, end) hours; wraps past midnight
    days: str = "daily"
    ac: bool = True  # AC loads go through the inverter
    essential: bool = False
    in_use: bool = True

    def window_hours(self) -> float:
        if self.window is None:
            return 24.0
        a, b = self.window
        return (b - a) % 24 or 24.0

    def active(self, local: dt.datetime) -> bool:
        if not self.in_use:
            return False
        if self.days == "weekdays" and local.weekday() >= 5:
            return False
        if self.days == "weekends" and local.weekday() < 5:
            return False
        if self.window is None:
            return True
        h = local.hour + local.minute / 60
        a, b = self.window
        return a <= h < b if a < b else (h >= a or h < b)

    def mean_w(self, local: dt.datetime, inverter_efficiency: float) -> float:
        """Average battery-side power when active (duty spread over the window)."""
        if not self.active(local):
            return 0.0
        duty = min(1.0, self.hours_per_day / self.window_hours())
        w = self.watts * duty
        return w / inverter_efficiency if self.ac else w


@dataclass(frozen=True)
class BaseLoad:
    """Loads that always run, measured at the battery (shunt)."""

    baseline_w: float = 43.0  # night baseline, 2026-09-14/16
    fridge_w: float = 27.0  # average (compressor duty)
    # Furnace blower: average W per °C of outdoor temperature below the balance point. Fitted from
    # furnace runs on the 2026-09-14/16 nights (mild, ~10–12 °C): ~1.2–1.4. Refit in winter.
    heat_w_per_degc: float = 1.3
    heat_balance_c: float = 15.0

    def mean_w(self, temp_c: float) -> float:
        return self.baseline_w + self.fridge_w + self.heat_w_per_degc * max(0.0, self.heat_balance_c - temp_c)


@dataclass(frozen=True)
class LoadModel:
    base: BaseLoad
    loads: tuple[Load, ...]
    tz: str = "UTC"
    inverter_efficiency: float = 0.9

    def sheddable(self) -> list[Load]:
        return [ld for ld in self.loads if not ld.essential and ld.in_use]

    def series(self, periods: list[WeatherPeriod], shed_level: int = 0) -> list[float]:
        zone = ZoneInfo(self.tz)
        shed = {ld.name for ld in self.sheddable()[:shed_level]}
        running = [ld for ld in self.loads if ld.name not in shed]
        out = []
        for p in periods:
            local = (p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone)
            out.append(self.base.mean_w(p.temp_c)
                       + sum(ld.mean_w(local, self.inverter_efficiency) for ld in running))
        return out

    def daily_wh(self, load: Load) -> float:
        """Average Wh/day at the battery, over a week."""
        per_week = load.hours_per_day * {"daily": 7, "weekdays": 5, "weekends": 2}[load.days]
        w = load.watts / self.inverter_efficiency if load.ac else load.watts
        return w * per_week / 7 if load.in_use else 0.0


_W = re.compile(r"(\d+(?:\.\d+)?)\s*w\b", re.I)
_H = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?(?:\s*/\s*day)?\b", re.I)
_WIN = re.compile(r"\b(\d{1,2}(?:[:.]\d{2})?)\s*-\s*(\d{1,2}(?:[:.]\d{2})?)\b")


def _hour(s: str) -> float:
    if ":" in s or "." in s:
        h, m = re.split(r"[:.]", s)
        return int(h) + int(m) / 60
    return float(s)


def parse_load(summary: str, description: str | None, completed: bool = False) -> Load | None:
    """Parse one to-do item. Returns None when no wattage is given."""
    text = description or ""
    w = _W.search(text)
    if not w:
        return None
    h = _H.search(_W.sub("", text))
    win = _WIN.search(text)
    low = text.lower()
    days = next((d for d in DAYS if d in low), "daily")
    return Load(
        name=summary.strip(),
        watts=float(w.group(1)),
        hours_per_day=float(h.group(1)) if h else 24.0,
        window=(_hour(win.group(1)), _hour(win.group(2))) if win else None,
        days=days,
        ac=not re.search(r"\b(dc|measured)\b", low),
        essential=bool(re.search(r"\b(essential|never shed|keep)\b", low)),
        in_use=not completed,
    )

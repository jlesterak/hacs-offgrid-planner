"""Check the load model against measured battery discharge at night.

At night PV is zero, so battery discharge measured by a shunt is exactly the load. Comparing it with
the model for the same hours shows whether the base load and shed-list estimates are realistic.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .solar import position

HOUR = dt.timedelta(hours=1)
NIGHT_ELEVATION = -3.0  # sun below this at mid-hour: no PV
MIN_COVERAGE = 0.9
MIN_NIGHT_HOURS = 4
MAX_GAP_S = 60.0  # longer sensor gaps are not integrated


def hour_key(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0).isoformat()


@dataclass
class EnergyMeter:
    """Integrates battery discharge power into hourly buckets: {hour_iso: [discharge_wh, covered_s]}."""

    hours: dict[str, list[float]] = field(default_factory=dict)
    last_t: dt.datetime | None = None
    last_discharge_w: float | None = None
    keep_hours: int = 48

    def add(self, t: dt.datetime, discharge_w: float | None) -> None:
        if self.last_t is not None and self.last_discharge_w is not None:
            gap = (t - self.last_t).total_seconds()
            if 0 < gap <= MAX_GAP_S:
                self._accumulate(self.last_t, t, self.last_discharge_w)
        self.last_t, self.last_discharge_w = t, discharge_w

    def _accumulate(self, a: dt.datetime, b: dt.datetime, w: float) -> None:
        # Split across hour boundaries.
        while a < b:
            hour_end = a.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0) + HOUR
            seg_end = min(b, hour_end)
            secs = (seg_end - a).total_seconds()
            bucket = self.hours.setdefault(hour_key(a), [0.0, 0.0])
            bucket[0] += w * secs / 3600
            bucket[1] += secs
            a = seg_end

    def prune(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(hours=self.keep_hours)
        self.hours = {k: v for k, v in self.hours.items() if dt.datetime.fromisoformat(k) >= cutoff}


@dataclass
class LoadCheck:
    measured_wh: float
    modelled_wh: float
    hours: int
    night_start: dt.datetime
    night_end: dt.datetime

    @property
    def ratio(self) -> float:
        return self.measured_wh / self.modelled_wh if self.modelled_wh > 0 else float("nan")


def last_night(now: dt.datetime, lat: float, lon: float, meter: EnergyMeter,
               modelled_w: dict[str, float]) -> LoadCheck | None:
    """Compare the most recent run of complete, well-covered night hours within the last 36 h."""
    end = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    run: list[dt.datetime] = []
    t = end - HOUR
    while t >= end - dt.timedelta(hours=36):
        elev, _ = position(t + HOUR / 2, lat, lon)
        key = hour_key(t)
        covered = meter.hours.get(key, [0.0, 0.0])[1] >= MIN_COVERAGE * 3600
        is_night_hour = elev < NIGHT_ELEVATION and covered and key in modelled_w
        if is_night_hour:
            run.append(t)
        elif run and elev >= NIGHT_ELEVATION:
            break  # reached the previous day: stop at the most recent night
        t -= HOUR
    if len(run) < MIN_NIGHT_HOURS:
        return None
    measured = sum(meter.hours[hour_key(h)][0] * 3600 / meter.hours[hour_key(h)][1] for h in run)
    modelled = sum(modelled_w[hour_key(h)] for h in run)
    return LoadCheck(round(measured, 1), round(modelled, 1), len(run), min(run), max(run) + HOUR)


def fitted_baseline_w(check: LoadCheck, baseline_w: float) -> float:
    """The baseline that would have made last night's model match the battery: the gap per hour moves into it.

    Anything the model got wrong that night (a listed load left off, a colder night than the heat fit) lands in
    the baseline too, so it is only as good as the night was typical.
    """
    return round(max(0.0, baseline_w + (check.measured_wh - check.modelled_wh) / check.hours), 1)

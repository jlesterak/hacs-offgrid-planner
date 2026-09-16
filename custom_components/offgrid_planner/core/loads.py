"""Load model: an always-on base, a list of loads with normal schedules, and ordered shed steps.

Loads (one to-do item each; the description holds the numbers):
    "40 W, 24/7, needs inverter"                  NAS, always on, AC
    "1200 W, 0.3 h/day in 7-10, needs inverter"   espresso: 0.3 h spread over 07–10
    "60 W, 8-17 weekdays, 10-22 weekends"          different hours on weekends
    "35 W idle, 24/7, supply"                      the inverter itself: other loads that
                                                   "need inverter" only run while it is on
    "6 W, 24/7, DC, essential"                     never shed
    "1180 W measured, …"                           learned at the battery: losses already included
A checked-off load is not in use at all.

Shed steps (ordered, top first; one to-do item each). Each step cuts one load down to a schedule:
    "NAS: 18-22"      "Starlink + router: 8-17 weekdays"      "Inverter: off"
Steps apply cumulatively from the top; for the same load the most restrictive schedule wins
(a load runs only when its normal schedule and every applied step allow it). Cutting the supply
(inverter) also stops every load that needs it outside the remaining hours.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .weather import WeatherPeriod

DAY_WORDS = {"daily": "daily", "every day": "daily", "weekdays": "weekdays", "weekday": "weekdays",
             "weekends": "weekends", "weekend": "weekends"}


def _day_matches(days: str, local: dt.datetime) -> bool:
    if days == "weekdays":
        return local.weekday() < 5
    if days == "weekends":
        return local.weekday() >= 5
    return True


@dataclass(frozen=True)
class Rule:
    days: str = "daily"
    window: tuple[float, float] | None = None  # local [start, end) hours, may wrap midnight; None = all day
    off: bool = False

    def hours(self) -> float:
        if self.off:
            return 0.0
        if self.window is None:
            return 24.0
        a, b = self.window
        return (b - a) % 24 or 24.0

    def covers(self, h: float) -> bool:
        if self.window is None:
            return True
        a, b = self.window
        return a <= h < b if a < b else (h >= a or h < b)


@dataclass(frozen=True)
class Schedule:
    """Empty rules = always on. Otherwise on only when a matching on-rule covers the time and no
    matching off-rule exists."""

    rules: tuple[Rule, ...] = ()

    @classmethod
    def always(cls) -> Schedule:
        return cls(())

    @classmethod
    def never(cls) -> Schedule:
        return cls((Rule(off=True),))

    def active(self, local: dt.datetime) -> bool:
        if not self.rules:
            return True
        matching = [r for r in self.rules if _day_matches(r.days, local)]
        if any(r.off for r in matching):
            return False
        h = local.hour + local.minute / 60
        return any(r.covers(h) for r in matching)

    def hours_on(self, local: dt.datetime) -> float:
        """Scheduled hours on this local day (for spreading an hours-per-day duty over the window)."""
        if not self.rules:
            return 24.0
        matching = [r for r in self.rules if _day_matches(r.days, local)]
        if any(r.off for r in matching):
            return 0.0
        return min(24.0, sum(r.hours() for r in matching))

    def describe(self) -> str:
        if not self.rules:
            return "24/7"
        parts = []
        for r in self.rules:
            if r.off:
                parts.append("off" if r.days == "daily" else f"off {r.days}")
            elif r.window is None:
                parts.append("24/7" if r.days == "daily" else r.days)
            else:
                win = f"{_fmt(r.window[0])}-{_fmt(r.window[1])}"
                parts.append(win if r.days == "daily" else f"{win} {r.days}")
        return ", ".join(parts)


def _fmt(h: float) -> str:
    return f"{int(h)}" if h == int(h) else f"{int(h)}:{round((h - int(h)) * 60):02d}"


@dataclass(frozen=True)
class Load:
    name: str
    watts: float  # while running; at the appliance unless battery_side
    hours_per_day: float | None = None  # duty within the scheduled hours; None = runs whenever scheduled
    schedule: Schedule = field(default_factory=Schedule.always)
    needs_inverter: bool = True
    supply: bool = False  # this is the inverter: watts = its own idle draw while on
    essential: bool = False
    in_use: bool = True
    battery_side: bool = False  # learned at the battery: inverter losses already included


@dataclass(frozen=True)
class ShedStep:
    label: str
    load_name: str
    schedule: Schedule
    enabled: bool = True


@dataclass(frozen=True)
class BaseLoad:
    """Loads that always run and aren't on the list, measured at the battery (shunt)."""

    baseline_w: float = 43.0  # night baseline, inverter off, 2026-09-14/16
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
    loads: tuple[Load, ...] = ()
    steps: tuple[ShedStep, ...] = ()
    tz: str = "UTC"
    inverter_efficiency: float = 0.9

    def _load(self, name: str) -> Load | None:
        return next((ld for ld in self.loads if ld.name.casefold() == name.casefold()), None)

    def supply(self) -> Load | None:
        return next((ld for ld in self.loads if ld.supply), None)

    def problems(self) -> list[str]:
        out = []
        for st in self.steps:
            if st.enabled and self._load(st.load_name) is None:
                out.append(f"Shed step '{st.label}': no load named '{st.load_name}'")
        if sum(1 for ld in self.loads if ld.supply) > 1:
            out.append("More than one load is marked 'supply'; only the first is used")
        return out

    def active_steps(self) -> list[ShedStep]:
        """Steps the planner may apply, in order: enabled, pointing at an in-use, non-essential load."""
        out = []
        for st in self.steps:
            ld = self._load(st.load_name)
            if st.enabled and ld is not None and ld.in_use and not ld.essential:
                out.append(st)
        return out

    def _limits(self, level: int) -> dict[str, list[Schedule]]:
        limits: dict[str, list[Schedule]] = {}
        for st in self.active_steps()[:level]:
            limits.setdefault(st.load_name.casefold(), []).append(st.schedule)
        return limits

    def _on(self, ld: Load, local: dt.datetime, limits: dict[str, list[Schedule]], supply_on: bool) -> bool:
        if not ld.in_use or not ld.schedule.active(local):
            return False
        if any(not s.active(local) for s in limits.get(ld.name.casefold(), ())):
            return False
        return supply_on or not ld.needs_inverter or ld.supply

    def power_w(self, local: dt.datetime, temp_c: float, level: int = 0,
                limits: dict[str, list[Schedule]] | None = None) -> tuple[float, set[str]]:
        """Battery-side power at a local time with the first `level` steps applied, and the loads running."""
        limits = self._limits(level) if limits is None else limits
        supply = self.supply()
        supply_on = supply is None or self._on(supply, local, limits, True)
        total, running = self.base.mean_w(temp_c), set()
        for ld in self.loads:
            if not self._on(ld, local, limits, supply_on):
                continue
            running.add(ld.name)
            if ld.supply:
                total += ld.watts
                continue
            w = ld.watts
            if ld.hours_per_day is not None:
                sched_h = ld.schedule.hours_on(local)
                w *= min(1.0, ld.hours_per_day / sched_h) if sched_h > 0 else 0.0
            if ld.needs_inverter and not ld.battery_side:
                w /= self.inverter_efficiency
            total += w
        return total, running

    def series(self, periods: list[WeatherPeriod], shed_level: int = 0) -> list[float]:
        zone = ZoneInfo(self.tz)
        limits = self._limits(shed_level)
        return [self.power_w((p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone), p.temp_c,
                             limits=limits)[0] for p in periods]

    def step_effects(self, periods: list[WeatherPeriod], level: int) -> list[dict]:
        """For each of the first `level` steps: energy saved over the periods and other loads it also cuts."""
        zone = ZoneInfo(self.tz)
        steps = self.active_steps()[:level]
        locals_ = [((p.start + dt.timedelta(hours=p.hours / 2)).astimezone(zone), p) for p in periods]
        prev = [self.power_w(t, p.temp_c, limits={}) for t, p in locals_]
        out = []
        for i, st in enumerate(steps, start=1):
            limits = self._limits(i)
            cur = [self.power_w(t, p.temp_c, limits=limits) for t, p in locals_]
            saved = sum((a[0] - b[0]) * p.hours for a, b, (_, p) in zip(prev, cur, locals_, strict=True))
            target = self._load(st.load_name)
            also = sorted({name for a, b in zip(prev, cur, strict=True) for name in a[1] - b[1]} - {target.name})
            out.append({"step": st.label, "saved_wh": saved, "also_cuts": also})
            prev = cur
        return out


# --- parsing to-do items ------------------------------------------------------------

_W = re.compile(r"(\d+(?:\.\d+)?)\s*w\b", re.I)
_H = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*(?:/\s*day|per\s+day)?\b", re.I)
_WIN = re.compile(r"(?<![\d.])(\d{1,2}(?::\d{2})?)\s*-\s*(\d{1,2}(?::\d{2})?)(?![\d.])")
_ARROW = re.compile(r"\s*(?:→|->|=>|:)\s*")


def _hour(s: str) -> float:
    if ":" in s:
        h, m = s.split(":")
        return int(h) + int(m) / 60
    return float(s)


def parse_schedule(text: str) -> Schedule:
    """Parse schedule phrases such as '24/7', 'off', '8-17 weekdays, 10-22 weekends', 'weekends'.

    Legacy form '8-17, weekdays' (day word in its own segment) applies the days to the window.
    Segments without schedule information (watts, tags) are ignored.
    """
    rules: list[Rule] = []
    bare_days: list[str] = []
    for seg in re.split(r"[,;]", text):
        low = seg.strip().lower()
        if not low:
            continue
        days = next((v for k, v in DAY_WORDS.items() if re.search(rf"\b{k}\b", low)), None)
        win = _WIN.search(_H.sub("", _W.sub("", low)))
        if re.search(r"24/7|\balways\b|\ball day\b", low):
            rules.append(Rule(days or "daily"))
        elif re.search(r"\boff\b|\bnever\b", low):
            rules.append(Rule(days or "daily", off=True))
        elif win:
            rules.append(Rule(days or "daily", (_hour(win.group(1)), _hour(win.group(2)))))
        elif days:
            bare_days.append(days)
    if bare_days:
        windowed_daily = [r for r in rules if r.days == "daily" and r.window is not None and not r.off]
        if windowed_daily and len(bare_days) == 1:
            rules = [Rule(bare_days[0], r.window) if r in windowed_daily else r for r in rules]
        else:
            rules += [Rule(d) for d in bare_days]
    if len(rules) == 1 and rules[0] == Rule():
        return Schedule.always()
    return Schedule(tuple(rules))


def parse_load(summary: str, description: str | None, completed: bool = False) -> Load | None:
    """Parse one Loads item. Returns None when no wattage is given."""
    text = description or ""
    w = _W.search(text)
    if not w:
        return None
    low = text.lower()
    h = _H.search(_W.sub("", text))
    is_supply = bool(re.search(r"\bsupply\b", low))
    needs = not is_supply and not re.search(r"\bdc\b", low)
    return Load(
        name=summary.strip(),
        watts=float(w.group(1)),
        hours_per_day=float(h.group(1)) if h and float(h.group(1)) < 24 else None,
        schedule=parse_schedule(re.sub(r"\(.*?\)", "", text)),
        needs_inverter=needs,
        supply=is_supply,
        essential=bool(re.search(r"\b(essential|never shed|keep)\b", low)),
        in_use=not completed,
        battery_side=bool(re.search(r"\bmeasured\b", low)),
    )


def parse_step(summary: str, description: str | None, completed: bool = False) -> ShedStep | None:
    """Parse one Shed steps item: description 'Load name: schedule', or summary 'Load name → schedule'."""
    for text in (description, summary):
        if not text:
            continue
        parts = _ARROW.split(text.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return ShedStep(label=summary.strip(), load_name=parts[0].strip(),
                            schedule=parse_schedule(re.sub(r"\(.*?\)", "", parts[1])), enabled=not completed)
    return None

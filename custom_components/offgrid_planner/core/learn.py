"""Learn a load's power by watching battery-side power while the user switches it on.

Load power is derived from the battery (and optionally PV) sensors: load = PV − battery charge power.

Phases:
  baseline  → collect samples with the load off (at least BASELINE_S seconds and BASELINE_N samples)
  waiting   → wait for a sustained rise above the baseline (user switches the load on)
  measuring → collect samples while it runs
  done      → step mode: load dropped back to baseline; average mode or finish(): stopped by the user
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from enum import StrEnum

BASELINE_S = 20.0
BASELINE_N = 8
MIN_STEP_W = 15.0
CONFIRM_N = 3  # consecutive samples above/below threshold
WAIT_TIMEOUT_S = 300.0
STEP_TIMEOUT_S = 900.0
AVERAGE_TIMEOUT_S = 4 * 3600.0


class Phase(StrEnum):
    IDLE = "idle"
    BASELINE = "baseline"
    WAITING = "waiting_for_load"
    MEASURING = "measuring"
    DONE = "done"
    FAILED = "failed"


class Mode(StrEnum):
    STEP = "step"  # switch on, then off again: median power while on
    AVERAGE = "average"  # cycling loads: mean power until finished (include full cycles)


@dataclass
class LearnResult:
    watts: float
    baseline_w: float
    spread_w: float  # stdev while on: high = noisy (another load cycled, clouds)
    samples: int
    seconds: float

    @property
    def confident(self) -> bool:
        return self.samples >= 5 and self.spread_w <= max(20.0, 0.25 * self.watts)


@dataclass
class Learner:
    mode: Mode = Mode.STEP
    phase: Phase = Phase.BASELINE
    started: dt.datetime | None = None
    baseline: list[float] = field(default_factory=list)
    on: list[float] = field(default_factory=list)
    on_since: dt.datetime | None = None
    last_t: dt.datetime | None = None
    _above: int = 0
    _below: int = 0
    result: LearnResult | None = None
    message: str = "Keep the load off while the baseline is measured."

    @property
    def baseline_w(self) -> float | None:
        return statistics.median(self.baseline) if self.baseline else None

    def _threshold(self) -> float:
        spread = statistics.pstdev(self.baseline) if len(self.baseline) > 1 else 0.0
        return max(MIN_STEP_W, 4 * spread)

    def feed(self, t: dt.datetime, load_w: float) -> Phase:
        self.started = self.started or t
        self.last_t = t
        elapsed = (t - self.started).total_seconds()
        if self.phase == Phase.BASELINE:
            self.baseline.append(load_w)
            if elapsed >= BASELINE_S and len(self.baseline) >= BASELINE_N:
                self.phase = Phase.WAITING
                self.message = f"Baseline {self.baseline_w:.0f} W. Switch the load on now."
        elif self.phase == Phase.WAITING:
            if load_w - self.baseline_w > self._threshold():
                self._above += 1
                self.on.append(load_w)
                if self._above >= CONFIRM_N:
                    self.phase = Phase.MEASURING
                    self.on_since = t
                    self.message = ("Measuring. Switch it off when done." if self.mode == Mode.STEP
                                    else "Measuring. Let it run through full cycles, then press Finish.")
            else:
                self._above = 0
                self.on.clear()
                if elapsed > BASELINE_S + WAIT_TIMEOUT_S:
                    self._fail("No load switched on within 5 minutes.")
        elif self.phase == Phase.MEASURING:
            on_s = (t - self.on_since).total_seconds()
            if self.mode == Mode.STEP and load_w - self.baseline_w < self._threshold() / 2:
                self._below += 1
                if self._below >= CONFIRM_N:
                    self._complete()
                    return self.phase
            else:
                self._below = 0
                self.on.append(load_w)
            if on_s > (STEP_TIMEOUT_S if self.mode == Mode.STEP else AVERAGE_TIMEOUT_S):
                self._complete()
        return self.phase

    def finish(self) -> Phase:
        if self.phase in (Phase.MEASURING, Phase.WAITING) and len(self.on) >= CONFIRM_N:
            self._complete()
        elif self.phase not in (Phase.DONE, Phase.FAILED):
            self._fail("Stopped before the load was detected.")
        return self.phase

    def _complete(self) -> None:
        values = self.on
        level = statistics.median(values) if self.mode == Mode.STEP else statistics.fmean(values)
        watts = max(0.0, level - self.baseline_w)
        seconds = (self.last_t - self.on_since).total_seconds() if self.on_since else 0.0
        self.result = LearnResult(round(watts, 1), round(self.baseline_w, 1),
                                  round(statistics.pstdev(values), 1) if len(values) > 1 else 0.0,
                                  len(values), seconds)
        self.phase = Phase.DONE
        self.message = (f"Learned {watts:.0f} W." if self.result.confident
                        else f"Learned {watts:.0f} W, but readings were noisy; consider repeating.")

    def _fail(self, why: str) -> None:
        self.phase = Phase.FAILED
        self.message = why


_W = re.compile(r"(\d+(?:\.\d+)?)\s*w\b(\s*measured)?", re.I)


def apply_to_description(description: str | None, watts: float) -> str:
    """Put the learned (battery-side) watts into a shed-list description, keeping everything else."""
    text = (description or "").strip()
    new = f"{watts:.0f} W measured"
    if _W.search(text):
        text = _W.sub(new, text, count=1)
    else:
        text = f"{new}, {text}" if text else new
    text = re.sub(r"\s*\(estimate\)", "", text)
    return text

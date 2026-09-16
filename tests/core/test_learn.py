import datetime as dt

import pytest
from core.learn import BASELINE_S, Learner, Mode, Phase, apply_to_description
from core.loads import parse_load

T0 = dt.datetime(2026, 9, 16, 3, 0, tzinfo=dt.UTC)


def run(learner, values, start_s=0):
    for n, w in enumerate(values):
        learner.feed(T0 + dt.timedelta(seconds=start_s + n), w)
    return start_s + len(values)


def test_step_learns_switch_on_off():
    lr = Learner(Mode.STEP)
    t = run(lr, [45, 47, 44, 46] * 6)  # 24 s baseline around 45 W
    assert lr.phase == Phase.WAITING
    t = run(lr, [1240, 1250, 1245, 1260, 1238] * 4, t)
    assert lr.phase == Phase.MEASURING
    run(lr, [46, 45, 44], t)
    assert lr.phase == Phase.DONE
    assert lr.result.watts == pytest.approx(1200, abs=10)
    assert lr.result.confident


def test_average_mode_includes_duty_cycle_until_finish():
    lr = Learner(Mode.AVERAGE)
    t = run(lr, [40] * int(BASELINE_S + 2))
    cycle = [160] * 30 + [45] * 30  # compressor on half the time
    t = run(lr, cycle * 10, t)
    assert lr.phase == Phase.MEASURING  # does not stop when the compressor cycles off
    assert lr.finish() == Phase.DONE
    assert lr.result.watts == pytest.approx(62, abs=6)


def test_no_load_detected_fails_on_finish():
    lr = Learner()
    run(lr, [40] * 30)
    assert lr.finish() == Phase.FAILED


def test_noisy_measurement_not_confident():
    lr = Learner()
    t = run(lr, [40] * 25)
    t = run(lr, [400, 900, 300, 1000, 350, 950, 420], t)
    lr.finish()
    assert lr.phase == Phase.DONE and not lr.result.confident


@pytest.mark.parametrize(("before", "after"), [
    ("1200 W, 0.3 h/day, 7-10 (estimate)", "1187 W measured, 0.3 h/day, 7-10"),
    ("900 W measured, 1 h", "1187 W measured, 1 h"),
    ("2 h/day, 18-22", "1187 W measured, 2 h/day, 18-22"),
    (None, "1187 W measured"),
])
def test_apply_to_description(before, after):
    assert apply_to_description(before, 1187.4) == after


def test_measured_watts_are_battery_side():
    assert parse_load("Espresso", "1187 W measured, 0.3 h/day").ac is False

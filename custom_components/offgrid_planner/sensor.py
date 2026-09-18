"""Planner sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SCENARIO_EXPECTED
from .coordinator import OffgridConfigEntry, PlannerData
from .core.learn import Phase
from .core.planner import ScenarioPlan, Status
from .entity import OffgridEntity

STATUS_OPTIONS = [s.name.lower() for s in Status]


def _expected(d: PlannerData) -> ScenarioPlan:
    return d.plan.scenarios[SCENARIO_EXPECTED]


def _planning(d: PlannerData) -> ScenarioPlan:
    return d.plan.scenarios[d.plan.planning_scenario]


def _week(d: PlannerData) -> list[dict[str, Any]]:
    """One row per local day for dashboards: expected vs bad-week solar, load and SOC."""
    expected = {x.day: x for x in d.daily.get(SCENARIO_EXPECTED, [])}
    planning = {x.day: x for x in d.daily.get(d.plan.planning_scenario, [])}
    rows = []
    for day in sorted(set(expected) | set(planning)):
        e, p = expected.get(day), planning.get(day) or expected.get(day)
        rows.append({
            "date": day.isoformat(),
            "pv_kwh": round(e.pv_wh / 1000, 2) if e else None,
            "pv_bad_kwh": round(p.pv_wh / 1000, 2),
            "load_kwh": round(p.load_no_action_wh / 1000, 2),
            "load_planned_kwh": round(p.load_wh / 1000, 2),
            "min_soc": round(p.min_soc),
            "min_soc_no_action": round(p.min_soc_no_action),
            "max_soc": round(p.max_soc),
            "end_soc": round(p.end_soc),
            "generator_h": p.generator_hours,
        })
    return rows


def _steps_text(sc: ScenarioPlan) -> str:
    parts = []
    for e in sc.step_effects:
        also = f" (also cuts {', '.join(e['also_cuts'])})" if e["also_cuts"] else ""
        parts.append(f"{e['step']}{also}")
    return "; ".join(parts)


def _advice(d: PlannerData) -> str:
    sc = _planning(d)
    if sc.status == Status.GENERATOR:
        r = sc.with_plan
        when = r.generator_first_start.isoformat() if r.generator_first_start else "soon"
        return (f"Generator needed: first run {when}, about {r.generator_hours:.0f} h and "
                f"{r.fuel_l:.1f} L over the next week, even with every shed step applied.")
    if sc.status == Status.SHED:
        n = len(sc.shed_steps)
        return (f"Apply shed step{'s 1–' + str(n) if n > 1 else ' 1'}: {_steps_text(sc)}. "
                f"Saves ~{sc.shed_saving_wh_per_day:.0f} Wh/day and keeps the battery above the reserve.")
    if sc.status == Status.TILT:
        return f"Tilt the trailer by day: ~{sc.tilt_gain_wh_per_day:.0f} Wh/day more solar."
    return "No action needed."


@dataclass(frozen=True, kw_only=True)
class PlannerSensorDescription(SensorEntityDescription):
    value: Callable[[PlannerData], Any]
    attrs: Callable[[PlannerData], dict[str, Any]] | None = None


SENSORS: tuple[PlannerSensorDescription, ...] = (
    PlannerSensorDescription(
        key="status", device_class=SensorDeviceClass.ENUM, options=STATUS_OPTIONS,
        value=lambda d: d.plan.status.name.lower(),
        attrs=lambda d: {"advice": _advice(d), "week": _week(d), "planning_scenario": d.plan.planning_scenario,
                         "shed_steps": _planning(d).shed_steps,
                         "step_effects": _planning(d).step_effects,
                         "problems": d.problems,
                         "tilt_recommended": _expected(d).tilt_recommended,
                         "weather_stale": d.weather_stale}),
    PlannerSensorDescription(
        key="min_soc_expected", native_unit_of_measurement=PERCENTAGE, device_class=SensorDeviceClass.BATTERY,
        suggested_display_precision=0,
        value=lambda d: round(_expected(d).with_plan.min_soc, 1),
        attrs=lambda d: {"min_soc_at": _expected(d).with_plan.min_soc_at,
                         "no_action_min_soc": round(_expected(d).no_action.min_soc, 1)}),
    PlannerSensorDescription(
        key="min_soc_bad_week", native_unit_of_measurement=PERCENTAGE, device_class=SensorDeviceClass.BATTERY,
        suggested_display_precision=0,
        value=lambda d: round(_planning(d).with_plan.min_soc, 1),
        attrs=lambda d: {"scenario": d.plan.planning_scenario,
                         "no_action_min_soc": round(_planning(d).no_action.min_soc, 1)}),
    PlannerSensorDescription(
        key="reserve_reached", device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _planning(d).no_action.first_below),
    PlannerSensorDescription(
        key="pv_today", native_unit_of_measurement=UnitOfEnergy.WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=0, value=lambda d: round(_expected(d).pv_today_wh)),
    PlannerSensorDescription(
        key="pv_today_full", native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY, suggested_display_precision=0,
        value=lambda d: round(_expected(d).pv_today_full_wh)),
    PlannerSensorDescription(
        key="load_model_check", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
        value=lambda d: round(d.load_check.ratio, 2) if d.load_check else None,
        attrs=lambda d: ({"measured_wh": d.load_check.measured_wh, "modelled_wh": d.load_check.modelled_wh,
                          "hours": d.load_check.hours, "night_start": d.load_check.night_start,
                          "night_end": d.load_check.night_end} if d.load_check else
                         {"note": "Needs a battery power sensor and one night of data."})),
    PlannerSensorDescription(
        key="pv_tomorrow", native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY, suggested_display_precision=0,
        value=lambda d: round(_expected(d).pv_tomorrow_wh)),
    PlannerSensorDescription(
        key="tilt_gain", native_unit_of_measurement=UnitOfEnergy.WATT_HOUR, suggested_display_precision=0,
        value=lambda d: round(_expected(d).tilt_gain_wh_per_day)),
    PlannerSensorDescription(
        key="shed_saving", native_unit_of_measurement=UnitOfEnergy.WATT_HOUR, suggested_display_precision=0,
        value=lambda d: round(_planning(d).shed_saving_wh_per_day)),
    PlannerSensorDescription(
        key="generator_hours", native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
        value=lambda d: _planning(d).with_plan.generator_hours if _planning(d).generator_needed else 0.0),
    PlannerSensorDescription(
        key="generator_first_run", device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _planning(d).with_plan.generator_first_start if _planning(d).generator_needed else None),
    PlannerSensorDescription(
        key="generator_fuel", native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.VOLUME, suggested_display_precision=1,
        value=lambda d: round(_planning(d).with_plan.fuel_l, 2) if _planning(d).generator_needed else 0.0),
    PlannerSensorDescription(
        key="weather_updated", device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.weather_fetched),
)


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([*(PlannerSensor(coordinator, desc) for desc in SENSORS), LearningSensor(coordinator)])


class LearningSensor(OffgridEntity, SensorEntity):
    """Progress and instructions while learning a load."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [p.value for p in Phase]
    # Changes every second while learning; don't write it to the recorder.
    _unrecorded_attributes = frozenset({"load_power_now_w"})

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "learning")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_shed_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        learner = self.coordinator.learner
        return learner.phase.value if learner else Phase.IDLE.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        learner = c.learner
        item = c.load_item(c.learn_uid)
        attrs: dict[str, Any] = {"load": item["summary"] if item else None, "mode": c.learn_mode.value,
                                 "load_power_now_w": None if c.learn_last_w is None else round(c.learn_last_w)}
        if learner:
            attrs["message"] = learner.message
            attrs["baseline_w"] = None if learner.baseline_w is None else round(learner.baseline_w)
            if learner.result:
                attrs.update(result_w=learner.result.watts, spread_w=learner.result.spread_w,
                             confident=learner.result.confident, samples=learner.result.samples)
        else:
            attrs["message"] = "Pick a load, press Start, then switch the load on when told."
        return attrs


class PlannerSensor(OffgridEntity, SensorEntity):
    entity_description: PlannerSensorDescription
    # The hourly SOC trajectory is for charts only; keep it out of the recorder (SD card).
    _unrecorded_attributes = frozenset({"soc_forecast", "week"})

    def __init__(self, coordinator, description: PlannerSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        d = self.coordinator.data
        if d is None:
            return None
        attrs = self.entity_description.attrs(d) if self.entity_description.attrs else {}
        if self.entity_description.key.startswith("min_soc"):
            sc = _expected(d) if self.entity_description.key == "min_soc_expected" else _planning(d)
            attrs["soc_forecast"] = [round(x, 1) for x in sc.with_plan.soc]
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in attrs.items()}

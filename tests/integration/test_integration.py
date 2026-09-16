"""Integration tests against a real Home Assistant core (pytest-homeassistant-custom-component)."""
from __future__ import annotations

import datetime as dt
import math

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.offgrid_planner.const import (
    CONF_BATTERY_POWER,
    CONF_SOC_ENTITY,
    DEFAULT_SHED_LIST,
    DOMAIN,
    NUMBER_RESERVE,
)
from custom_components.offgrid_planner.core.solar import position
from custom_components.offgrid_planner.core.weather import ENSEMBLE_URL, FORECAST_URL

LAT, LON = 32.69, -114.63  # Yuma
SHED = "todo.off_grid_planner_shed_order"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _hourly(sky: float, members: int = 0) -> dict:
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - dt.timedelta(days=1)
    times, ghi, temp = [], [], []
    for i in range(24 * 9):
        end = start + dt.timedelta(hours=i + 1)
        elev, _ = position(end - dt.timedelta(minutes=30), LAT, LON)
        cz = math.sin(math.radians(elev))
        times.append(end.strftime("%Y-%m-%dT%H:%M"))
        ghi.append(round(1098 * cz * math.exp(-0.057 / cz) * sky, 1) if cz > 0.01 else 0.0)
        temp.append(15.0)
    hourly = {"time": times, "shortwave_radiation": ghi, "temperature_2m": temp}
    if members:
        for m in range(1, members + 1):
            hourly[f"shortwave_radiation_member{m:02d}"] = [g * m / members for g in ghi]
    else:
        hourly["direct_radiation"] = [g * 0.85 for g in ghi]
        hourly["diffuse_radiation"] = [g * 0.15 for g in ghi]
    return {"hourly": hourly}


@pytest.fixture
async def env(hass: HomeAssistant):
    hass.config.latitude, hass.config.longitude = LAT, LON
    await hass.config.async_set_time_zone("America/Phoenix")
    hass.states.async_set("sensor.battery_soc", "60", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set("sensor.battery_power", "-45", {"device_class": "power", "unit_of_measurement": "W"})


async def _setup(hass, aioclient_mock, sky=1.0, members=10):
    aioclient_mock.get(FORECAST_URL, json=_hourly(sky))
    aioclient_mock.get(ENSEMBLE_URL, json=_hourly(sky, members=members))
    entry = MockConfigEntry(domain=DOMAIN, title="Off-Grid Planner",
                            data={CONF_SOC_ENTITY: "sensor.battery_soc", CONF_BATTERY_POWER: "sensor.battery_power"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _items(hass):
    resp = await hass.services.async_call("todo", "get_items", {"entity_id": SHED}, blocking=True,
                                          return_response=True)
    return resp[SHED]["items"]


async def test_config_flow_creates_entry(hass: HomeAssistant, env) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOC_ENTITY: "sensor.battery_soc", CONF_BATTERY_POWER: "sensor.battery_power"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["capacity_wh"] == 6240


async def test_setup_creates_entities_and_plans(hass: HomeAssistant, env, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    status = hass.states.get("sensor.off_grid_planner_status")
    assert status is not None and status.state in ("ok", "tilt", "shed", "generator")
    assert status.attributes["planning_scenario"] == "bad_week"
    assert float(hass.states.get("sensor.off_grid_planner_solar_tomorrow").state) > 3000
    assert hass.states.get("number.off_grid_planner_reserve_soc").state == "20.0"
    assert hass.states.get("sensor.off_grid_planner_learning").state == "idle"
    for _method, url, *_ in aioclient_mock.mock_calls:
        assert url.query["latitude"] == "32.69" and url.query["longitude"] == "-114.63"


async def test_shed_list_seeded_and_editable(hass: HomeAssistant, env, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    items = await _items(hass)
    assert [i["summary"] for i in items] == [name for name, _ in DEFAULT_SHED_LIST]
    assert hass.states.get(SHED).state == str(len(DEFAULT_SHED_LIST))

    await hass.services.async_call("todo", "add_item", {"entity_id": SHED, "item": "Hair dryer",
                                                        "description": "1500 W, 0.1 h/day, 7-8"}, blocking=True)
    await hass.services.async_call("todo", "update_item", {"entity_id": SHED, "item": "NAS", "status": "completed"},
                                   blocking=True)
    await hass.services.async_call("todo", "update_item", {"entity_id": SHED, "item": "Ice maker",
                                                           "description": "150 W, 8 h/day, 9-18"}, blocking=True)
    await hass.services.async_call("todo", "remove_item", {"entity_id": SHED, "item": "Internet: weekends"},
                                   blocking=True)
    items = await _items(hass)
    hair = next(i for i in items if i["summary"] == "Hair dryer")
    # Drag the hair dryer to the top (shed first): the UI calls the entity's move method.
    shed_entity = hass.data["entity_components"]["todo"].get_entity(SHED)
    await shed_entity.async_move_todo_item(hair["uid"], None)
    await hass.async_block_till_done()

    items = await _items(hass)
    assert items[0]["summary"] == "Hair dryer"
    assert "Internet: weekends" not in [i["summary"] for i in items]
    assert next(i for i in items if i["summary"] == "NAS")["status"] == "completed"
    assert next(i for i in items if i["summary"] == "Ice maker")["description"] == "150 W, 8 h/day, 9-18"

    # The planner sees the new order, and the list survives a reload.
    loads = entry.runtime_data.data.loads
    assert loads[0].name == "Hair dryer" and not next(ld for ld in loads if ld.name == "NAS").in_use
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await _items(hass))[0]["summary"] == "Hair dryer"


async def test_dark_week_generator_uses_shed_order(hass: HomeAssistant, env, aioclient_mock) -> None:
    hass.states.async_set("sensor.battery_soc", "30", {"device_class": "battery"})
    await _setup(hass, aioclient_mock, sky=0.05)
    status = hass.states.get("sensor.off_grid_planner_status")
    assert status.state == "generator"
    assert status.attributes["shed_loads"][:2] == ["Espresso machine", "Small dishwasher"]
    assert "Internet: weekday work hours" not in status.attributes["shed_loads"]  # essential
    assert float(hass.states.get("sensor.off_grid_planner_generator_hours_next_7_days").state) > 0


async def test_learn_load_updates_shed_item(hass: HomeAssistant, env, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.off_grid_planner_load_to_learn", "option": "Espresso machine"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.off_grid_planner_start_learning"},
                                   blocking=True)

    async def tick(battery_w: float, seconds: int):
        for _ in range(seconds):
            hass.states.async_set("sensor.battery_power", str(battery_w))
            freezer.tick(dt.timedelta(seconds=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    await tick(-45, 25)
    assert hass.states.get("sensor.off_grid_planner_learning").state == "waiting_for_load"
    await tick(-1225, 20)  # espresso machine on (battery discharging 1225 W)
    assert hass.states.get("sensor.off_grid_planner_learning").state == "measuring"
    await tick(-46, 4)  # off again
    learning = hass.states.get("sensor.off_grid_planner_learning")
    assert learning.state == "done"
    assert learning.attributes["result_w"] == pytest.approx(1180, abs=5)
    espresso = next(i for i in await _items(hass) if i["summary"] == "Espresso machine")
    assert espresso["description"] == "1180 W measured, 0.3 h/day, 7-10"
    assert next(ld for ld in entry.runtime_data._loads() if ld.name == "Espresso machine").watts == 1180
    # Sampling stopped.
    assert entry.runtime_data._learn_unsub is None


async def test_learn_without_power_sensor_fails_cleanly(hass: HomeAssistant, env, aioclient_mock) -> None:
    aioclient_mock.get(FORECAST_URL, json=_hourly(1.0))
    aioclient_mock.get(ENSEMBLE_URL, json=_hourly(1.0, members=5))
    entry = MockConfigEntry(domain=DOMAIN, title="Off-Grid Planner", data={CONF_SOC_ENTITY: "sensor.battery_soc"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": "button.off_grid_planner_start_learning"},
                                   blocking=True)
    learning = hass.states.get("sensor.off_grid_planner_learning")
    assert learning.state == "failed" and "battery power" in learning.attributes["message"]


async def test_offline_keeps_planning_from_cache(hass: HomeAssistant, env, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    coordinator = entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, exc=aiohttp.ClientError("no internet"))
    aioclient_mock.get(ENSEMBLE_URL, exc=aiohttp.ClientError("no internet"))
    coordinator._cache["fetched"] = (dt_util.utcnow() - dt.timedelta(hours=2)).isoformat()
    async_fire_time_changed(hass, dt_util.utcnow() + dt.timedelta(minutes=16))
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert hass.states.get("sensor.off_grid_planner_status").state != "unavailable"


async def test_soc_unavailable_keeps_list_and_settings_usable(hass: HomeAssistant, env, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    hass.states.async_set("sensor.battery_soc", "unavailable")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.off_grid_planner_status").state == "unavailable"
    assert hass.states.get("number.off_grid_planner_reserve_soc").state == "20.0"
    assert hass.states.get(SHED).state != "unavailable"


async def test_reserve_number_replans(hass: HomeAssistant, env, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.off_grid_planner_reserve_soc", "value": 35}, blocking=True)
    await hass.async_block_till_done()
    assert entry.runtime_data.numbers[NUMBER_RESERVE] == 35

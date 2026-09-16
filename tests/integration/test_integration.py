"""Integration tests against a real Home Assistant core (pytest-homeassistant-custom-component)."""
from __future__ import annotations

import datetime as dt
import math

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.offgrid_planner.const import (
    CONF_LOADS_TODO,
    CONF_SOC_ENTITY,
    DOMAIN,
    NUMBER_RESERVE,
)
from custom_components.offgrid_planner.core.solar import position
from custom_components.offgrid_planner.core.weather import ENSEMBLE_URL, FORECAST_URL

LAT, LON = 32.69, -114.63  # Yuma


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _hourly(sky: float, members: int = 0) -> dict:
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - dt.timedelta(days=1)
    times, ghi, temp = [], [], []
    for i in range(24 * 8):
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
async def setup_env(hass: HomeAssistant):
    hass.config.latitude, hass.config.longitude = LAT, LON
    await hass.config.async_set_time_zone("America/Phoenix")
    hass.states.async_set("sensor.battery_soc", "60", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set("todo.shed_order", "3")
    items = [
        {"summary": "Espresso machine", "description": "1200 W, 0.3 h/day, 7-10", "status": "needs_action"},
        {"summary": "Dishwasher", "description": "900 W, 1 h, 12-15", "status": "needs_action"},
        {"summary": "NAS", "description": "40 W, 4 h, 18-22", "status": "completed"},
        {"summary": "Notes", "description": "no wattage here", "status": "needs_action"},
    ]

    async def get_items(call: ServiceCall):
        return {"todo.shed_order": {"items": items}}

    hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
    return items


async def _setup(hass, aioclient_mock, sky=1.0, members=10):
    aioclient_mock.get(FORECAST_URL, json=_hourly(sky))
    aioclient_mock.get(ENSEMBLE_URL, json=_hourly(sky, members=members))
    entry = MockConfigEntry(domain=DOMAIN, title="Off-Grid Planner",
                            data={CONF_SOC_ENTITY: "sensor.battery_soc", CONF_LOADS_TODO: "todo.shed_order"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_creates_entry(hass: HomeAssistant, setup_env) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOC_ENTITY: "sensor.battery_soc"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOC_ENTITY] == "sensor.battery_soc"
    assert result["data"]["capacity_wh"] == 6240


async def test_setup_creates_entities_and_plans(hass: HomeAssistant, setup_env, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    status = hass.states.get("sensor.off_grid_planner_status")
    assert status is not None and status.state in ("ok", "tilt", "shed", "generator")
    assert status.attributes["planning_scenario"] in ("expected", "bad_week")
    assert "advice" in status.attributes
    assert float(hass.states.get("sensor.off_grid_planner_solar_tomorrow").state) > 3000
    assert hass.states.get("number.off_grid_planner_reserve_soc").state == "20.0"
    assert hass.states.get("binary_sensor.off_grid_planner_generator_needed") is not None
    assert hass.states.get("sensor.off_grid_planner_forecast_downloaded").state not in ("unknown", "unavailable")
    # Coordinates are rounded before leaving the house.
    for _method, url, *_ in aioclient_mock.mock_calls:
        assert url.query["latitude"] == "32.69" and url.query["longitude"] == "-114.63"


async def test_dark_week_sheds_in_list_order_then_generator(hass: HomeAssistant, setup_env,
                                                            aioclient_mock) -> None:
    hass.states.async_set("sensor.battery_soc", "30", {"device_class": "battery"})
    await _setup(hass, aioclient_mock, sky=0.05)
    status = hass.states.get("sensor.off_grid_planner_status")
    assert status.state == "generator"
    # Completed item (NAS) and the item without wattage are not on the shed list.
    assert status.attributes["shed_loads"] == ["Espresso machine", "Dishwasher"]
    assert hass.states.get("binary_sensor.off_grid_planner_generator_needed").state == "on"
    assert float(hass.states.get("sensor.off_grid_planner_generator_hours_next_7_days").state) > 0


async def test_offline_keeps_planning_from_cache(hass: HomeAssistant, setup_env, aioclient_mock) -> None:
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


async def test_soc_unavailable_makes_entities_unavailable(hass: HomeAssistant, setup_env,
                                                         aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    hass.states.async_set("sensor.battery_soc", "unavailable")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.off_grid_planner_status").state == "unavailable"
    assert hass.states.get("number.off_grid_planner_reserve_soc").state == "20.0"


async def test_reserve_number_replans(hass: HomeAssistant, setup_env, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.off_grid_planner_reserve_soc", "value": 35},
                                   blocking=True)
    await hass.async_block_till_done()
    assert entry.runtime_data.numbers[NUMBER_RESERVE] == 35
    assert hass.states.get("number.off_grid_planner_reserve_soc").state == "35.0"

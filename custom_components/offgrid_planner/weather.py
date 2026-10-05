"""Weather entity served from the planner's cached Open-Meteo forecast.

Off-grid the internet is often down. HA's own weather integrations go unavailable then; this one keeps
showing the last forecast it fetched, with `forecast_fetched` and `stale` attributes so cards and
announcements can say how old it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.weather import Forecast, WeatherEntity, WeatherEntityFeature
from homeassistant.const import UnitOfPrecipitationDepth, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import WEATHER_MAX_AGE
from .coordinator import OffgridConfigEntry, OffgridCoordinator
from .core.weather import Conditions, current_conditions, daily_conditions, wmo_condition
from .entity import OffgridEntity

HOURLY_FORECAST_HOURS = 72
# Two missed hourly fetches: the forecast is from before the internet went down.
STALE_AFTER = 2 * WEATHER_MAX_AGE


async def async_setup_entry(hass: HomeAssistant, entry: OffgridConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([OffgridWeather(entry.runtime_data)])


class OffgridWeather(OffgridEntity, WeatherEntity):
    _attr_attribution = "Weather data by Open-Meteo.com (cached)"
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_dew_point_unit = UnitOfTemperature.CELSIUS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_supported_features = WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY

    def __init__(self, coordinator: OffgridCoordinator) -> None:
        super().__init__(coordinator, "forecast")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Advance "now" every hour even when nothing else changes (offline, or the plan keeps failing).
        self.async_on_remove(async_track_time_change(self.hass, self._tick, minute=0, second=30))

    @callback
    def _tick(self, _now: datetime) -> None:
        self._refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh()

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(None))

    def _now(self) -> Conditions | None:
        _, hours = self.coordinator.conditions()
        return current_conditions(hours, dt_util.utcnow())

    @property
    def available(self) -> bool:
        # Deliberately not the coordinator's success flag: a failed plan (SOC sensor down) still has a forecast.
        return self._now() is not None

    @property
    def condition(self) -> str | None:
        c = self._now()
        return c.condition if c else None

    @property
    def native_temperature(self) -> float | None:
        return (c := self._now()) and c.temp_c

    @property
    def native_dew_point(self) -> float | None:
        return (c := self._now()) and c.dew_point_c

    @property
    def humidity(self) -> float | None:
        return (c := self._now()) and c.humidity

    @property
    def cloud_coverage(self) -> int | None:
        c = self._now()
        return None if c is None or c.cloud_cover is None else round(c.cloud_cover)

    @property
    def native_wind_speed(self) -> float | None:
        return (c := self._now()) and c.wind_kmh

    @property
    def native_wind_gust_speed(self) -> float | None:
        return (c := self._now()) and c.gust_kmh

    @property
    def wind_bearing(self) -> float | None:
        return (c := self._now()) and c.wind_dir

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fetched, _ = self.coordinator.conditions()
        return {"forecast_fetched": fetched.isoformat() if fetched else None,
                "stale": fetched is None or dt_util.utcnow() - fetched > STALE_AFTER}

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        _, hours = self.coordinator.conditions()
        now = dt_util.utcnow()
        start = now.replace(minute=0, second=0, microsecond=0)
        return [_hour(c) for c in hours if start <= c.time < start + timedelta(hours=HOURLY_FORECAST_HOURS)] or None

    async def async_forecast_daily(self) -> list[Forecast] | None:
        _, hours = self.coordinator.conditions()
        tz = dt_util.get_default_time_zone()
        days = daily_conditions(hours, tz, from_date=dt_util.now(tz).date())
        return [Forecast(
            datetime=d.start.isoformat(), condition=wmo_condition(d.code), native_temperature=d.temp_max_c,
            native_templow=d.temp_min_c, native_dew_point=d.dew_point_max_c, cloud_coverage=d.cloud_cover,
            native_precipitation=d.precip_mm, precipitation_probability=d.precip_prob,
            native_wind_speed=d.wind_kmh, native_wind_gust_speed=d.gust_kmh) for d in days] or None


def _hour(c: Conditions) -> Forecast:
    return Forecast(
        datetime=c.time.isoformat(), condition=c.condition, native_temperature=c.temp_c,
        native_dew_point=c.dew_point_c, humidity=c.humidity, cloud_coverage=c.cloud_cover,
        native_precipitation=c.precip_mm, precipitation_probability=c.precip_prob, native_wind_speed=c.wind_kmh,
        native_wind_gust_speed=c.gust_kmh, wind_bearing=c.wind_dir, is_daytime=c.is_day)

# Off-Grid Planner for Home Assistant

Forecast-driven advice for off-grid and mobile solar setups (trailers, vans, cabins):

- **Tilt**: when tilting the rig by day is worth it (flat roof arrays lose a lot at low sun).
- **Shed loads**: which loads to switch off, in *your* priority order, to stay above a reserve for the week.
- **Generator**: when you'll need it, for about how long, and how much fuel, respecting quiet hours.

Existing planners (Predbat, EMHASS, …) optimise grid tariffs and assume the grid is there as a fallback.
This one assumes it isn't.

## How it works

- Weather: Open-Meteo hourly forecast plus an ensemble for a *bad week* (the darkest 10%), for Home
  Assistant's home location (rounded to ~1 km), so it follows you when your home location follows GPS.
  Fetched hourly when online and cached, so planning continues offline.
- PV: plane-of-array irradiance for flat or tilted panels, a measured low-sun factor for flat roof arrays,
  a per-site horizon factor (open desert < 1 < trees/canyon), temperature derating.
- Loads: an always-on base (baseline, fridge, heating vs outdoor temperature) plus the **Shed order** to-do
  list the integration creates (seeded with examples). Add, edit, delete, check off and drag to reprioritise
  in the To-do panel; the plan updates immediately. Item description format:
  `1200 W, 0.3 h/day, 7-10, weekdays` · add `essential` to never shed · `DC` for loads that bypass the
  inverter · check an item off when the load isn't in use.
- **Learn a load**: pick it in *Load to learn*, press *Start learning*, keep it off for ~20 s, switch it on
  when *Learning* says so, then off again (or press *Finish learning* in *Average over cycles* mode for
  cycling loads such as an ice maker). The measured battery-side watts replace the estimate in the item.
  Needs a fast battery power sensor (a shunt at ~1 Hz). By day, add a solar power sensor or learn with
  steady sun; a full battery hides load changes.
- Battery: 7-day hourly SOC simulation with curtailment when full, then a search for the smallest shed level
  and, if that isn't enough, the earliest generator start that keeps SOC above the reserve.

## Entities

Status (`ok` / `tilt` / `shed` / `generator`, with an `advice` attribute), lowest SOC next 7 days
(expected and bad week, with an hourly `soc_forecast` attribute that isn't recorded), reserve reached
without action, solar today/tomorrow, tilt gain, shed saving, generator hours / first run / fuel,
binary sensors for tilt/shed/generator, and adjustable **Reserve SOC** and **Site horizon factor**.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff uv
# Core tests only (no Home Assistant needed):
.venv/bin/pytest tests/core
# Everything, against a real Home Assistant core (Python 3.14):
UV_CACHE_DIR=.tools/uv-cache UV_PYTHON_INSTALL_DIR=.tools/python .venv/bin/uv venv --python 3.14 .tools/ha-venv
.venv/bin/uv pip install --python .tools/ha-venv/bin/python pytest-homeassistant-custom-component
.tools/ha-venv/bin/python -m pytest
```

Status: early (0.1.0), calibrated on one 1.58 kW flat-roof trailer array in Colorado.

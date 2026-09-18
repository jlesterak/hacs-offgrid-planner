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
- Loads: an always-on base (baseline, fridge, heating vs outdoor temperature) plus two editable to-do lists
  the integration creates (seeded with examples); the plan updates immediately on any edit.
  - **Loads**: what runs and when. `40 W, 24/7, needs inverter` · `1200 W, 0.3 h/day in 7-10` ·
    `60 W, 8-17 weekdays, 10-22 weekends` · `35 W idle, 24/7, supply` (the inverter: loads that need it only run
    while it is on) · `DC` = doesn't need the inverter · `essential` = never shed · check an item off when unused.
  - **Shed steps**: ordered cutbacks, top first, drag to reorder. Each step cuts one load to a schedule:
    `NAS: 18-22`, `Starlink + router: 8-17 weekdays, off weekends`, `Inverter: off`. Steps apply cumulatively;
    the most restrictive schedule for a load wins, and cutting the inverter also stops the AC loads that need
    it (the advice lists what else each step cuts and how much it saves).
  - Items the planner can't parse are listed in the Status `problems` attribute.
- **Learn a load**: pick it in *Load to learn*, press *Start learning*, keep it off for ~20 s, switch it on
  when *Learning* says so, then off again (or press *Finish learning* in *Average over cycles* mode for
  cycling loads such as an ice maker). The measured battery-side watts replace the estimate in the Loads item.
  Needs a fast battery power sensor (a shunt at ~1 Hz). By day, add a solar power sensor or learn with
  steady sun; a full battery hides load changes.
- Battery: 7-day hourly SOC simulation with curtailment when full, then a search for the smallest shed level
  and, if that isn't enough, the earliest generator start that keeps SOC above the reserve.

## Entities

Status (`ok` / `tilt` / `shed` / `generator`, with an `advice` attribute), lowest SOC next 7 days
(expected and bad week, with an hourly `soc_forecast` attribute that isn't recorded), reserve reached
without action, solar today/tomorrow, tilt gain, shed saving, generator hours / first run / fuel,
binary sensors for tilt/shed/generator, adjustable **Reserve SOC** and **Site horizon factor**, a `week`
attribute on Status (per day: expected and bad-week solar, load, lowest/highest/end SOC, generator hours; not recorded)
for dashboards, and **Load model check**: last night's measured battery discharge ÷ the modelled load for the
same hours (PV is zero at night, so the shunt measures the load exactly). Well above 1 means loads are missing
or underestimated; well below 1 means estimates are too high. Its attributes list the `always_on` draw
(baseline, fridge, furnace averaged over the next 24 h, essential loads) and `baseline_fit_w`, the baseline that
would have matched last night; the **Set baseline from last night** button saves it (available once a night has
been measured). Pick a typical night: anything else the model got wrong that night lands in the baseline too.

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

## License

The Unlicense: public domain. See [LICENSE](LICENSE).

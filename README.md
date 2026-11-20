# F1 2026 simulator (local)

A local lap-time and race simulator for the 2026 Formula 1 season: pick a calendar track, one of the 11
cars and a driver, edit the setup, and get the optimal racing line, a lap time, full telemetry, comparisons,
race simulations and a 3D replay.

**Read this first.** Real 2026 car data (downforce, drag, mass distribution, spring rates, power-unit maps)
is secret. This simulator reverse-calibrates its cars from public timing data, and every parameter says
where its value comes from. Use it for **trends and comparisons, not for exact real lap times or race
results.**

Build status is tracked in [PROGRESS.md](PROGRESS.md). The interface contract between backend, UI and 3D
scene is in [docs/API.md](docs/API.md).

## Install

Python 3.10+ (developed on 3.13).

```bash
python -m venv .venv                                  # or: uv venv .venv
.venv/bin/pip install -r requirements.txt             # runtime: numpy, scipy (+ numba, optional)
.venv/bin/pip install numba                           # optional: ~0.05 s per lap instead of seconds
.venv/bin/pip install -r requirements-fetch.txt       # only for the fetch scripts: fastf1, requests
```

The simulator runs fully offline from `data/` and `web/assets/`. `fastf1` and `requests` are never imported
at runtime.

## Run

```bash
.venv/bin/python run.py                     # starts http://127.0.0.1:8765 and opens the browser
.venv/bin/python run.py --port 9000 --no-browser
```

Data and assets (already cached in this folder; re-run after each new race):

```bash
.venv/bin/python scripts/fetch_data.py                 # all public data, skips rounds already cached
.venv/bin/python scripts/fetch_data.py --rounds 21 22  # only these rounds
.venv/bin/python scripts/fetch_data.py --source openf1 # auto | openf1 | fastf1
.venv/bin/python scripts/fetch_data.py --force         # refetch everything
.venv/bin/python scripts/fetch_assets.py               # CC0 textures and HDRIs (--res 1k|2k, --force)
.venv/bin/python scripts/calibrate_presets.py          # team presets + fit report + validation table (in progress)
.venv/bin/python scripts/alignment_report.py           # telemetry alignment RMS for every track
.venv/bin/python -c "from f1sim import racing_line; racing_line.build_all(force=True)"  # rebuild lines
```

Tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Architecture

```
scripts/fetch_data.py ──> data/ (geojson, Jolpica, OpenF1/FastF1 telemetry, standings)
                              │
f1sim/tracks.py ──────────> track: 3 m centerline, curvature, width, elevation, zones
f1sim/racing_line.py ─────> optimal line (cached in data/cache/)
f1sim/car/ ───────────────> parameter schema + physics tables per car
f1sim/solver.py ──────────> lap time + telemetry
f1sim/calibration/ ───────> team presets fitted to public timing (data/presets/)
f1sim/race/ ──────────────> race pace, strategy, overtaking, race loop, Monte Carlo
f1sim/optimise.py ────────> setup optimiser
f1sim/server.py + run.py ─> JSON API (stdlib http.server) + static web/
web/ ─────────────────────> UI, hand-drawn canvas charts, Three.js 3D scene
```

- **Data pipeline** (`scripts/fetch_data.py`). Fetches once and caches under `data/`: centerlines,
  track widths, the calendar, standings, and per-round qualifying, results, pit stops, laps, sectors,
  speed traps, tyre stints, weather and car telemetry with X/Y/Z position for each team's best quali lap
  (`data/public/2026/<NN>/`). If the live fetch fails it falls back to a labelled stale snapshot.
- **Tracks** (`f1sim/tracks.py`). Projects lon/lat to metres, fits a periodic spline, resamples every
  3 m scaled to the official length, and computes heading, curvature and normals analytically. Public
  telemetry X/Y is aligned to the centerline (FFT cross-correlation over every start offset, both driving
  directions and both handedness, then ICP) to fix the start/finish line and direction, and to fit an
  elevation profile from telemetry Z. Width comes from the TUM mean, then `data/track_overrides.json`, then
  a street or permanent default. Corners, kerbs, run-off and pit entry/exit are generated.
- **Car model** (`f1sim/car/`). About 75 parameters in 8 groups (`params.py`), each with a default, range,
  unit, tooltip and provenance tag. Modules for active aero (Z/X modes) and a ride-height-sensitive floor
  that stalls (`aero.py`), load-sensitive tyres with thermal state, wear and a cliff (`tyres.py`), roll
  stiffness and load transfer (`suspension.py`), brake bias and disc temperature (`brakes.py`), and the
  ICE + 350 kW MGU-K with the 2026 deployment taper and override mode (`powertrain.py`). `vehicle.py`
  turns a car and driver into speed-indexed limit tables for the solver.
- **Lap solver** (`f1sim/solver.py`). Quasi-steady point mass: corner speed limits, then forward and
  backward passes with the friction ellipse, power limit, drag, gradient and rev limit (numba, pure-Python
  fallback). An energy bisection spends the per-lap budget (quali may drain the battery; race is net zero)
  with super-clipping, lift-and-coast and rear-only regen. Tyre and brake thermal models feed back once.
- **Racing line** (`f1sim/racing_line.py`). Curvature-plus-length least squares inside the track edges
  (kerbs give extra room at a grip penalty), with a golden-section search over the weighting to minimise
  lap time. Cached per track.
- **Race simulator** (`f1sim/race/`; the race loop and Monte Carlo are still being built, see PROGRESS.md). A lap-time surface from the solver (fuel, tyre age, compound), tyre
  strategy, traffic with dirty air and override, overtaking probability from pace delta, track difficulty
  and racecraft, pit stops, safety cars, DNFs and Monte Carlo runs. Public race parameters (pit loss,
  stint lengths) come from `f1sim/calibration/race_targets.py` → `data/race_params.json`.
- **Setup optimiser** (`f1sim/optimise.py`). Tunes chosen parameters on their step grid for a quali or
  race objective and reports per-parameter sensitivities.
- **Calibration** (`f1sim/calibration/`, `scripts/calibrate_presets.py`). A global fit (grip, floor
  downforce, drag) shared by all teams, then a per-team `least_squares` fit regularised toward the
  baseline, against median qualifying gap to pole, speed traps and corner minimum speeds. Outputs
  `data/presets/<team>.json`, `_global.json`, `_report.json` and `_validation.json`. Constructor points
  are only used as a sanity check on ordering, never as a pace target.
- **Server** (`f1sim/server.py`, `run.py`). Stdlib `http.server` with JSON endpoints for tracks, teams,
  drivers, parameter schema, presets, liveries, simulate, compare, race, optimise, calibrate and setups;
  long jobs run in the background (`f1sim/jobs.py`). Setup suitability flags are in `f1sim/suitability.py`.
- **UI and 3D** (`web/`; still being built and not yet tested end to end, see PROGRESS.md). Static HTML and ES modules, no build step. Charts are drawn on canvas by hand.
  The 3D scene uses Three.js vendored in `web/vendor/three/`: a procedural car with team colours (or your
  own GLB), track ribbon with kerbs and run-off, HDRI lighting, several cameras and post-processing. If an
  asset is missing the scene falls back to plain materials and shows a note.

## What is accurate and what is approximate

Every parameter shows one of three provenance tags in the UI:

| Tag | Meaning | Count (of 75 car parameters) |
|---|---|---|
| `regulation` | FIA 2026 rule value: 768 kg minimum mass, 350 kW MGU-K, deployment taper 290 → 355 km/h, 8.5 MJ harvest cap, 4 MJ battery window, override mode | 10 |
| `public-derived` | Fitted from public timing or telemetry (calibrated presets, elevation, start line) | 7 |
| `estimate` | Engineering guess; editable | 58 |

Fairly reliable:
- Track length (scaled to official, 0.0% error), shape, start/finish line and driving direction (aligned
  to telemetry), elevation where telemetry Z exists.
- Relative effects of setup changes (wing, ride height, bias, compounds) as trends.
- 2026 energy rules (deployment taper, harvest cap, override mode).

Approximate:
- Absolute lap times. Before calibration the baseline car is 5–15% off real poles (see PROGRESS.md §4);
  see the Validation section for current numbers.
- Driver ratings: subjective estimates, tagged `estimate`.
- Tyre and brake temperatures, wear rates and the thermal balance between axles.
- Kerbs, run-off and pit entry/exit are generated, not surveyed. Width is a single mean value per track.
- The point-mass solver has no transient vehicle dynamics; tight chicanes on tracks with a sparse
  centerline are smoothed and may run slightly fast (flagged in each track's notes).
- Wet weather is not modelled. Wet sessions (e.g. Interlagos 2026 quali) must be filtered out of targets.

## Data sources and licences

| Source | Used for | Licence / terms |
|---|---|---|
| [bacinger/f1-circuits](https://github.com/bacinger/f1-circuits) | Centerline geometry and official length | See the repository's licence |
| [TUMFTM/racetrack-database](https://github.com/TUMFTM/racetrack-database) | **Mean track widths only** (17 tracks). Its coordinates are never used or mixed with bacinger's | LGPL-3.0 |
| [Jolpica F1 API](https://github.com/jolpica/jolpica-f1) | Calendar, standings, qualifying, results, pit stops | Public API; see the project's terms |
| [OpenF1](https://openf1.org) | Laps, sectors, speed traps, tyres, pit-lane times, weather, car telemetry and position | Free, unofficial public API; see its terms |
| [FastF1](https://github.com/theOehrly/Fast-F1) (optional) | Same data as OpenF1 when an F1 account is configured | MIT (library); data from F1 live timing |
| [Poly Haven](https://polyhaven.com/license) | Textures and HDRIs in `web/assets/` (manifest with URL, licence and hash) | CC0 |
| [three.js](https://threejs.org) | 3D rendering, vendored in `web/vendor/three/` | MIT |

This project is not affiliated with Formula 1, the FIA or any team. F1 data is used for personal,
non-commercial simulation.

**OpenF1 instead of FastF1.** Since 2026, FastF1's upstream (F1 live timing) needs an F1 account login and
loads no laps without one. `fetch_data.py` therefore uses the public OpenF1 API by default. FastF1 is used
when `F1_USERNAME` and `F1_PASSWORD` are set in the environment, or with `--source fastf1`. Both paths write
the same files (`tel_quali.json`, `tel_quali.npz`, `tel_race.json`). Credentials are only read from the
environment; never put them in a committed file.

**No logos or trademarks.** No sponsor logos, team badges or real livery artwork are included or
downloaded. Liveries (`data/liveries/<team>.json`) are colour schemes only. There is a decal slot
(`web/assets/liveries/`) and an optional GLB car path (`web/assets/cars/`) for your own files, kept local.

## Differences from the brief

From the live data at fetch time (after round 20):

- **Calendar.** The live 2026 calendar has 23 rounds and includes a **Sepang round** (round 16, listed as
  the "Bahrain Grand Prix in Malaysia"), which is not in the brief's default list. **Bahrain and Jeddah are
  not on it.** Madring (Madrid) is round 14. Rounds 21–23 (Las Vegas, Qatar, Abu Dhabi) had not run yet,
  so they have no telemetry; re-run `fetch_data.py` after each race.
- **Entry list.** The live entries differ from the brief. The rosters are written to `data/teams.json` and
  `data/drivers.json` (others are kept as reserves). PROGRESS.md §3.1 records Red Bull as Verstappen +
  Lawson and Racing Bulls as Lindblad + Tsunoda; the current `data/teams.json` lists Red Bull as
  Verstappen + Hadjar (Lawson reserve) and Racing Bulls as Lindblad + Lawson (Tsunoda reserve), following
  the latest results. Haas lists Bearman + Rafael Câmara (Câmara raced round 20; Ocon and Hirakawa are
  reserves). Cadillac is Pérez + Bottas.
- **Standings after round 20:** Mercedes, Ferrari, Red Bull, McLaren, Racing Bulls, Alpine, Haas, Audi,
  Williams, Aston Martin, Cadillac.

## Validation

### Model vs real pole and race time

Generated by `scripts/calibrate_presets.py` (global model, default driver, real session weather) from cached public data. `*` = track used in the global fit. Wet sessions (rain fraction above 0.2, for example Interlagos) are skipped; no per-track fudge is applied.

| Track | Real pole (s) | Sim (s) | Error % |
|---|---|---|---|
| albert_park | 78.518 | 80.930 | +3.07 |
| baku * | 102.526 | 95.342 | -7.01 |
| catalunya | 74.679 | 74.239 | -0.59 |
| cota | 94.035 | 94.972 | +1.00 |
| hungaroring | 77.207 | 77.211 | +0.01 |
| madring | 91.824 | 96.606 | +5.21 |
| marina_bay | 91.373 | 91.866 | +0.54 |
| mexico * | 76.514 | 72.916 | -4.70 |
| miami | 87.798 | 88.054 | +0.29 |
| monaco * | 72.051 | 72.288 | +0.33 |
| montreal | 72.578 | 71.787 | -1.09 |
| monza * | 81.786 | 82.577 | +0.97 |
| red_bull_ring | 66.113 | 66.567 | +0.69 |
| sepang | 95.130 | 92.786 | -2.46 |
| shanghai | 92.064 | 92.180 | +0.13 |
| silverstone * | 88.111 | 88.725 | +0.70 |
| spa | 104.361 | 104.321 | -0.04 |
| suzuka * | 88.778 | 88.929 | +0.17 |
| zandvoort | 71.163 | 72.497 | +1.87 |

Global factors: `mu_scale` 1.0422, `cla_floor` 3.5 (at its upper bound), `cda_body` 0.6899.
Known misses: Baku (-7 %) and Mexico City (-4.7 %) are simulated too fast, Madring (+5.2 %) and Albert Park (+3.1 %) too slow. These come from track geometry / racing-line limits that three global factors cannot absorb. Sim top speeds (about 300-330 km/h) are still below the real 320-350 km/h.

### Team fit vs standings

| Team | Target gap to pole % | Fitted gap % | Residual % | Standing | Pace rank |
|---|---|---|---|---|---|
| mercedes | 0.00 | 0.01 | +0.01 | 1 | 1 |
| ferrari | 0.23 | 0.26 | +0.02 | 2 | 2 |
| mclaren | 0.41 | 0.47 | +0.05 | 4 | 3 |
| red_bull | 0.40 | 0.54 | +0.14 | 3 | 4 |
| racing_bulls | 1.28 | 1.21 | -0.07 | 5 | 5 |
| alpine | 1.30 | 1.24 | -0.05 | 6 | 6 |
| audi | 1.41 | 1.52 | +0.11 | 8 | 7 |
| haas | 1.97 | 2.00 | +0.03 | 7 | 8 |
| williams | 2.52 | 2.34 | -0.18 | 9 | 9 |
| cadillac | 3.68 | 3.55 | -0.13 | 11 | 10 |
| aston_martin | 4.24 | 4.06 | -0.18 | 10 | 11 |

- dry sessions used: 8; wet sessions skipped (rain fraction > 0.2)
- calibrated with the default driver rating (no driver nudge); tyre_wear_scale is not fitted
- Spearman correlation of fitted pace order vs constructor standings: 0.97

### Telemetry alignment (`data/alignment_report.json`)

Generated by `scripts/alignment_report.py`: the fastest public quali lap's X/Y aligned to the centerline.
RMS is the mean nearest-centerline distance over all telemetry samples. It includes the racing line's
lateral offset from the centerline (up to half the track width), so a few metres is expected.

- 20 of 23 tracks aligned; median RMS **4.3 m**, range 2.7 m (Monza) to 14.5 m (Sepang).
- **Flagged (RMS > 10 m): Sepang, 14.5 m** (p95 35.9 m, max 57 m). The bacinger centerline and the
  telemetry differ in places; the start line and direction are still taken from the fit, but the geometry
  there needs checking against the current Sepang layout.
- Next worst: Marina Bay 6.8 m (digitised opposite to the driving direction; reversed automatically),
  Silverstone 5.8 m, Albert Park 5.6 m.
- Las Vegas, Lusail and Yas Marina: no telemetry yet (start line as digitised, flat).

| Rd | Track | RMS m | p95 m | Direction |
|---|---|---|---|---|
| 1 | albert_park | 5.63 | 11.1 | as digitised |
| 2 | shanghai | 4.23 | 9.7 | as digitised |
| 3 | suzuka | 2.95 | 5.5 | as digitised |
| 4 | miami | 5.19 | 9.5 | as digitised |
| 5 | montreal | 3.79 | 7.4 | as digitised |
| 6 | monaco | 2.88 | 5.9 | as digitised |
| 7 | catalunya | 5.33 | 11.2 | as digitised |
| 8 | red_bull_ring | 3.26 | 6.2 | as digitised |
| 9 | silverstone | 5.75 | 10.2 | as digitised |
| 10 | spa | 3.97 | 8.5 | as digitised |
| 11 | hungaroring | 4.41 | 7.9 | as digitised |
| 12 | zandvoort | 4.26 | 9.1 | as digitised |
| 13 | monza | 2.68 | 4.8 | as digitised |
| 14 | madring | 3.74 | 6.8 | as digitised |
| 15 | baku | 5.32 | 9.9 | as digitised |
| 16 | sepang | **14.52** | 35.9 | as digitised |
| 17 | marina_bay | 6.82 | 15.3 | reversed |
| 18 | cota | 4.35 | 8.6 | as digitised |
| 19 | mexico | 5.33 | 10.2 | as digitised |
| 20 | interlagos | 3.53 | 6.5 | as digitised |
| 21–23 | las_vegas, lusail, yas_marina | – | – | no telemetry |

### Drivers

`tests/test_drivers.py` runs the best and worst rated current drivers (Verstappen, Stroll) in the same
baseline car. Measured deltas (2026-11-20, uncalibrated car): Monza 0.32 s, **Silverstone 0.52 s**,
**Monaco 0.68 s**. Silverstone and Monaco are outside the brief's 0.1–0.5 s band, so the test fails there
(the numbers move with the car model). The driver pace ratings are estimates and need a narrower spread or
a track-dependent effect.

## Known limitations

- Calibration is the foundation of every absolute number; until presets exist all teams share the
  baseline car.
- Car-model acceptance checks have failed on rear wing vs corner speed, floor stall and compound order
  (PROGRESS.md §4); run the tests for the current state. The tests are kept strict.
- Tyre heat balance between axles is off; brake temperatures peak low (about 400–500 °C).
- Point-mass solver: no transient yaw, no combined slip beyond the friction ellipse, no kerb strikes.
- Track width is a single mean value; kerbs and run-off are generated.
- Wet weather is a stub (`wet_skill` is not used).

## What to improve next

1. Finish and validate the global and team calibration; publish the validation tables above.
2. Fix the failing car-model trends (front/rear aero split, floor stall, tyre warm-up window).
3. Better tyre thermal model (multi-node, per axle) and brake heat input.
4. Real kerb and width maps, and a better Sepang centerline.
5. Elevation for tracks without telemetry (Las Vegas, Lusail, Yas Marina) once they run.
6. Wet weather and safety-car strategy.
7. Full vehicle dynamics (transient model) for chicanes and kerbs.
8. Driver teammate nudge from qualifying head-to-heads (`data/driver_ratings.json`).

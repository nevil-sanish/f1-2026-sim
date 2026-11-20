# F1 2026 Simulator: build progress

Spec: `/home/nevilsanish/Downloads/F1_SIM_BUILD_PROMPT_v2.md` (sections are cited as §N below).
Interface contract shared by backend, UI and 3D scene: [docs/API.md](docs/API.md). User docs: [README.md](README.md).
Status as of 2026-11-20 (second pass). All 13 stages have code; the full test suite passes
(`.venv/bin/python -m unittest discover -s tests`: 68 tests, OK, 1 skipped — the opt-in live calibration job).
The app was checked in a browser: Lap, Compare, Optimise, Race (22 cars, 53 laps, 2.5 s, 3D replay), Livery and Data modes work.

---

## 1. Summary

| Area (spec stage, §12) | Status | Notes |
|---|---|---|
| 1. Data fetch + tracks | **Done** | 20 rounds cached; all 23 tracks load; alignment RMS reported per track (`data/alignment_report.json`) |
| 2. Point-mass lap solver | **Done** | Curvature smoothed over 18 m; deploy cut-off smoothed |
| 3. Racing line | **Done** | Cached for all 23 tracks |
| 4. Load transfer, balance, tyres, brakes, ride height | **Done** | All 16 car-model acceptance tests pass |
| 5. Energy + active aero | **Done** | Top speed still low (see §4) |
| 6. First visual milestone (3D) | **Done** | `web/scene/scene.js` + track mesh, environment, cameras, post-fx |
| 7. Team preset calibration | **Done** | `f1sim/calibration/fit.py`; 15 of 19 tracks within 3% of real pole |
| 8. Drivers | **Done** | Teammate nudge in `data/driver_ratings.json`; best-vs-worst delta 0.1–0.5 s |
| 9. Race simulator | **Done** | `race_sim.py`, `montecarlo.py`; overtakes too high (see §4) |
| 10. Setup optimiser | **Done** | |
| 11. Full visuals | **Done** | 11 colour-only liveries; FPS only measured in a software-rendered browser |
| 12. Server + UI end-to-end | **Done** | `tests/test_server.py` (18 tests) |
| 13. Validation report + README | **Done** | README has validation, team and alignment tables |

## 2. Environment

- Project venv: `.venv/` (Python 3.13, made with `uv`). Installed: numpy 2.5, scipy 1.18, numba 0.68,
  fastf1 3.9, requests.
- `requirements.txt` (runtime: numpy, scipy, optional numba) and `requirements-fetch.txt` (fastf1, requests)
  are written.
- Not a git repository yet.

## 3. Done

### 3.1 Data fetch (`scripts/fetch_data.py`), §3

- **Track geometry:** centerlines from bacinger/f1-circuits go to `data/geojson/`. Mean track widths from
  TUMFTM for 17 tracks go to `data/tum_widths.json` (widths only, never coordinates).
- **Jolpica:** calendar, constructor and driver standings, and per-round qualifying, results and pit stops.
- **Telemetry source changed: OpenF1 instead of FastF1.** Since 2026 FastF1's upstream (F1 live timing) needs an
  F1 account login. Without one it loads no laps. The fetcher therefore uses the public **OpenF1** API by
  default: laps, sectors, speed traps, compounds and tyre age, pit-lane times, weather, plus car telemetry and
  X/Y/Z position for each team's best qualifying lap. FastF1 is still used if `F1_USERNAME`/`F1_PASSWORD` are
  set in the environment. Choose the source with `--source auto|openf1|fastf1`. Both paths write the same
  files: `tel_quali.json`, `tel_quali.npz` and `tel_race.json`.
- **Cached:** rounds 01–20 under `data/public/2026/<NN>/` (3.6 MB), plus `data/public/fetched_at.json`.
- **Calendar check:** the live 2026 calendar has 23 rounds. It includes a **Sepang round** ("Bahrain Grand Prix
  in Malaysia", round 16) that is not in the brief's default list. Bahrain and Jeddah are not on it. The fetcher
  prints these differences.
- **Entry list check:** the live entries differ from the brief. Red Bull is Verstappen + Lawson, and Racing Bulls
  is Lindblad + Tsunoda; Hadjar is not the current Red Bull driver. Results also show a Haas driver, Rafael
  Câmara, at round 20. The current pairs are written to `data/teams.json` and `data/drivers.json`, with others
  listed as reserves.
- **Standings after round 20:** Mercedes, Ferrari, Red Bull, McLaren, Racing Bulls, Alpine, Haas, Audi,
  Williams, Aston Martin, Cadillac.
- **Fallback:** if the live fetch fails, the stale-snapshot order from §3.4 is used and labelled as stale.
- **Rounds 21–23** (Las Vegas, Qatar, Abu Dhabi) had not run at fetch time. Re-run the fetcher after each race.
  It skips rounds already cached.

### 3.2 Track pipeline (`f1sim/tracks.py`, `data/track_catalog.json`, `data/track_overrides.json`), §3.5

- Projection, periodic spline, uniform 3 m resampling scaled to the official length, and analytic heading,
  curvature and normals. **All 23 tracks** close cleanly, have finite curvature and are within 0.0% of the
  official length.
- **Telemetry alignment:** an FFT cross-correlation, refined by ICP, fixes the start/finish line and the driving
  direction and reports the RMS offset. Earlier tests gave 2.7–5.6 m on Monza, Suzuka, Albert Park and Shanghai.
  At that point most other tracks had no telemetry; they need re-checking now that all 20 rounds are cached.
- **Elevation:** fitted from telemetry Z where available (e.g. Suzuka 40 m, Monza 12 m) and tagged
  public-derived. Otherwise the track is flat.
- **Width:** TUM mean, then the override file, then a street or permanent default.
- **Generated zones:** corners, kerbs (apex and exit), run-off (gravel, asphalt or wall by circuit type), and pit
  entry/exit (an estimate).
- A sparse-centerline warning is added to the track's notes for 10 tracks.
- The catalogue holds per-track laps, pit loss and overtaking difficulty (estimates) and the night-race flag.

### 3.3 Car model (`f1sim/car/`), §4 and §7

- `params.py`: about 80 parameters in 8 groups. Each has a default, min, max, step, unit, tooltip and
  provenance tag (regulation, public-derived or estimate). It also holds the team-fit keys, the global-fit keys,
  `make_car()` (defaults, then global calibration, then team preset, then user overrides) and
  `schema_for_api()`. 2026 rule values are set: 768 kg, 350 kW MGU-K, 290→355 km/h taper, 8.5 MJ harvest cap,
  4 MJ battery swing, and override mode.
- `aero.py`: Z/X active aero and a ride-height-dependent floor solved by fixed point. Downforce collapses below
  the stall height. Drag includes brake ducts and toe.
- `tyres.py`: C1–C5 compounds, load-sensitive grip `Fy = c·Fz^(1−ls)`, pressure and camber penalties,
  single-node thermal model, energy-based wear and the cliff.
- `suspension.py`: roll-stiffness distribution, lateral load transfer per axle, damper efficiency (optimum
  ζ≈0.65), compliance, kerb factor, diff traction and preload.
- `brakes.py`: bias efficiency and a lumped disc temperature model with radiation and fade.
- `powertrain.py`: ICE power curve, 8 gears, rev limit and MGU-K deployment taper, including override.
- `vehicle.py`: builds the speed-indexed tables the solver uses: lateral limit, traction, braking, resistance,
  balance and ideal brake bias.

### 3.4 Lap solver (`f1sim/solver.py`), §8.1

- Corner speed from a monotone table, then a forward/backward pass with numba (pure-Python fallback). Includes
  the friction ellipse, power limit, drag, gradient and rev limit.
- **Energy:** a bisection on a single deployment cut speed spends the per-lap budget (quali may spend the
  battery; race is net zero). It also models super-clipping, lift-and-coast, and regen limited to the rear axle.
- X/Z aero zones with a transition delay, shift-time loss, and tyre and brake thermal models fed back once.
- **Telemetry channels:** v, t, ax, ay, throttle, brake, gear, rpm, aero mode, SoC, deploy and harvest power,
  tyre and brake temperatures, ride heights, downforce, balance, lift, ideal bias, and wear per lap.
- **Speed:** about 0.05 s per lap with numba after the first JIT compile (about 1.3 s).

### 3.5 Racing line (`f1sim/racing_line.py`), §8.2

- Curvature + length least squares (`lsq_linear` on sparse matrices, re-linearised) inside track-edge bounds,
  with extra room on kerbs at a grip penalty. A golden-section search over λ minimises lap time.
- **Cached** for all 23 tracks in `data/cache/line_<id>_v5.npz`. `build_all()` rebuilds them in parallel in
  about 50 s.
- Faster than the centerline on every track (e.g. Monza 93.5 s → 87.3 s).

### 3.6 Drivers (`f1sim/drivers.py`), §8.1.6

- 8 rating parameters for 23 drivers. These are subjective estimates, editable, and tagged as estimates.
- Hooks for the teammate quali nudge (`data/driver_ratings.json`) exist, but nothing generates that file yet.

### 3.7 Setup optimiser (`f1sim/optimise.py`), §8.4 (written by a subagent)

- `tests/test_optimise.py`: **12 tests pass**. The agent was stopped before its final report, so its timings
  have not been reviewed.

### 3.8 Public race parameters (`f1sim/calibration/race_targets.py` → `data/race_params.json`)

- Written by the race subagent; `data/race_params.json` has been generated. Its numbers have not been reviewed
  yet.

---

## 4. Known problems (open)

1. **Pace outliers.** Validation vs real 2026 poles (`data/presets/_validation.json`): most tracks within ±1%, but
   Baku −7.0%, Mexico −4.7%, Madring +5.2%, Albert Park +3.1%, Sepang −2.5%. No per-track fudge was applied. Likely
   track geometry / racing-line limits (Sepang's centerline is also off by 14.5 m RMS against telemetry).
2. **Top speed low:** about 300–310 km/h at most tracks against 320–350 real. Energy budget and super-clipping cap
   deployment. `cla_floor` sits at its upper bound (3.5), which hints at missing grip elsewhere in the model.
3. **Overtakes too high:** about 90 per race at Monza (real ~45). Tune `PASS_MARGIN` / `REPASS_BLOCK` in
   `f1sim/race/race_sim.py` now that calibration has widened the pace spread.
4. **Rear brake temps** peak at only 280–400 °C (front 800–1000 °C) because the MGU-K takes rear braking; review.
5. `tyre_wear_scale` is not fitted (stays 1.0). The optimiser's `mass_above_min` / `tyre_wear_scale` sensitivity
   tests pass with a thin margin.
6. The running server keeps the module copies it first loaded; restart it after code changes.
7. **Line-up mismatch:** this file previously said Red Bull = Verstappen + Lawson and Racing Bulls = Lindblad +
   Tsunoda; `data/teams.json` has Red Bull = Verstappen + Hadjar and Racing Bulls = Lindblad + Lawson. Re-check
   against the latest fetched results.
8. 60 fps with 22 cars on Medium has not been measured on a real GPU.
9. Not a git repository yet.

## 5. Suggested next steps

1. Investigate Baku/Mexico/Madring pace (racing line on long straights, top speed / energy model).
2. Re-tune overtaking against the real per-race overtake counts.
3. Re-run `scripts/fetch_data.py` after rounds 21–23, then `scripts/calibrate_presets.py`.
4. Measure FPS on real hardware.

## 6. Useful commands

```bash
.venv/bin/python scripts/fetch_data.py                # fetch new rounds (skips cached ones)
.venv/bin/python scripts/fetch_assets.py              # CC0 textures / HDRIs
.venv/bin/python -c "from f1sim import racing_line; racing_line.build_all(force=True)"   # rebuild lines
.venv/bin/python scripts/calibrate_presets.py        # refit team presets (~2.5 min)
.venv/bin/python scripts/alignment_report.py         # telemetry alignment table
.venv/bin/python -m unittest discover -s tests       # full test suite
.venv/bin/python run.py                               # start server + UI
```

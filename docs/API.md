# f1sim interface contract

Shared contract between the Python backend, the web UI (`web/app.js`) and the 3D scene (`web/scene/`).
Units: metres, seconds, radians unless stated. World frame: x east, y north, z up. A track/line is a
closed loop sampled at uniform `ds`; index 0 is the start/finish line, increasing index = driving direction.
Left unit normal at a sample = `(-sin psi, cos psi)`.

## 1. HTTP API (stdlib http.server, JSON)

Server: `python run.py [--port 8765] [--no-browser]`. Static files are served from `web/` at `/`.
Errors: HTTP 4xx/5xx with `{"error": "message"}`: 400 bad input (invalid JSON, wrong types, non-finite numbers,
unknown team/driver, a car parameter outside its kind or choice list), 403 non-local request, 404 unknown
endpoint/track/setup/job, 405 wrong method, 409 calibration already running, 413 body over 2 MB, 503 an optional
module (race simulator, Monte Carlo, calibration) is not installed yet. JSON responses over 2 kB are gzipped when the
request sends `Accept-Encoding: gzip`. Non-finite floats in results are sent as `null`.

### GET
- `/api/tracks` -> `[{id, name, type:"permanent"|"street", night:bool, laps, length, width, round, date,
  align_rms_m|null, elevation_source, start_line, notes:[str], n_corners, pit_loss_s, overtake_difficulty}]`
- `/api/track/<id>` -> geometry for 3D:
  `{id, name, type, night, length, width, ds, x:[], y:[], z:[], psi:[], kappa:[],
    corners:[{num, apex (index), s_apex, i0, i1, dir:"L"|"R", radius}],
    kerbs:[{i0, i1, side:"L"|"R", kind:"apex"|"exit"}], kerb_width,
    runoff:[{i0, i1, side, kind:"gravel"|"asphalt"|"wall", depth}],
    pit:{entry_s, exit_s, side:"L"|"R"}, notes:[str]}`
  Index ranges `i0..i1` may wrap around the end of the arrays (i1 < i0).
- `/api/teams` -> `[{id, name, pu, drivers:[driver_id, driver_id], other_drivers:[...],
  standing:{pos, points}|null, calibrated:bool}]`
- `/api/drivers` -> `[{id, code, number, name, team, current:bool, ratings:{grip_use, brake_commit,
  throttle_commit, consistency, tyre_management, racecraft, wet_skill, balance_preference}}]`
- `/api/params/schema?team=<id>` -> `{groups:[str], params:[{key, group, label, default, preset, min, max,
  step, unit, prov:"regulation"|"public-derived"|"estimate", tip, kind:"float"|"choice"|"bool", options|null}],
  driver_params:[{key, label, min, max, step, default, tip}], calibrated:bool}`
  `preset` is the team's calibrated value (what "reset" restores); `default` is the baseline car.
- `/api/presets/<team>` -> `{team, values:{key:value}, prov:{key:prov}, fit:{...}|null, calibrated}`
- `/api/liveries` -> `{<team_id>: livery}`; livery =
  `{base:"#rrggbb", secondary, accent, finish:"gloss"|"matte"|"satin",
    regions:{nose, engine_cover, sidepods, front_wing, rear_wing, endplates, halo, floor, wheel_covers}
      (each value is "base"|"secondary"|"accent"|"carbon"|"#rrggbb"),
    numbers:{<driver_id>: int}, number_color:"#rrggbb"}`
- `/api/data_status` -> `{fetched_at, sources:[str], status:{...}, rounds_cached:int, stale_snapshot:bool,
  standings:[{pos, team, points}], calibration:{generated_at, global:{...}, teams:[{team, target_gap_pct,
  sim_gap_pct, residual_pct, params:{key:value}, standing_pos, pace_rank}], notes:[str]}|null,
  validation:[{track, real_pole, sim_pole, err_pct}]|null, notices:[str]}`
- `/api/setups` -> `[{name, track, team, driver, notes, saved_at}]`; `/api/setups/<name>` -> full setup
  `{name, track, team, driver, notes, values:{key:value}, driver_values:{...}, saved_at}`
- `/api/jobs/<id>` -> `{id, kind, status:"running"|"done"|"error", progress:0..1, message, result|null, error|null}`

### POST (JSON body)
- `/api/simulate` `{track, team, driver, overrides:{param:value}, driver_overrides:{...},
  mode:"quali"|"race_pace", tyre_age:int}` ->
  ```
  {label, track, team, driver, mode, lap_time, lap_time_str, run_time_s,
   line:{s:[], x:[], y:[], z:[], psi:[], offset:[]},        // racing line, same sample count as telemetry
   telemetry:{s, t, v (km/h), throttle (0..1), brake (0..1), gear, rpm, ax (g), ay (g), soc (MJ),
              mode (0 = Z corner, 1 = X straight), p_dep (kW), p_harv (kW), tyre_temp_f, tyre_temp_r (C),
              brake_temp_f, brake_temp_r (C), ride_h_f, ride_h_r (mm), balance, kappa (1/m), lift (0|1)},
   summary:{vmax, vmin, v_avg (km/h), e_deploy_mj, e_harvest_mj, e_budget_mj, mass, fuel, wear_per_lap_f,
            wear_per_lap_r, upshifts, max_lat_g, max_brake_g, x_mode_pct, sectors:[s1, s2, s3]},
   corners:[{num, s_apex, v_min (km/h), brake_s|null, dir}],
   flags:[{id, level:"ok"|"warn"|"bad", title, text}],       // suitability card
   warnings:[str]}
  ```
- `/api/compare` `{configs:[<simulate body + optional label>, ...]}` (2+, same track) ->
  `{results:[<simulate result>...], ranking:[{index, label, lap_time, gap}],
    delta:{s:[], series:[{index, label, dt:[]}]}}`  (dt = time lost vs configs[0] along distance)
- `/api/race` `{track, laps|null, entries:[{team, driver, overrides, driver_overrides,
  strategy:"auto"|{stops:[{lap, compound}], start_compound}}], grid:"quali"|[driver_id...],
  options:{safety_car:bool, dnf:bool, seed:int}, monte_carlo:int (0 = single race)}` -> `{job:id}`.
  Job result:
  ```
  {track, laps, lap_length, entries:[{code, driver, team, number, grid}],
   classification:[{pos, code, driver, team, grid, laps, time, gap, status:"FIN"|"DNF"|"+1 LAP"..., stops,
                    best_lap, best_lap_n}],
   fastest_lap:{code, time, lap},
   lap_chart:{<code>:[position at end of lap 0..N]},          // index 0 = grid
   gaps:{<code>:[gap to leader in s at end of each lap]},
   lap_times:{<code>:[s]},
   strategy:{<code>:[{compound:"C1".."C5", label:"hard"|"medium"|"soft", start_lap, end_lap}]},
   energy:{<code>:[MJ deployed per lap]},
   overtakes:[{lap, t, s, by, on}],
   events:[{t, lap, kind:"SC"|"VSC"|"DNF"|"PIT", code|null, text}],
   replay:{dt, t:[], cars:{<code>:{d:[] (cumulative distance m, lap = floor(d/lap_length)),
                                   pit:[0|1], speed:[] (km/h), compound_idx:[] }}},
   line:{s, x, y, z, psi},                                     // shared racing line for the replay
   monte_carlo:null|{runs, table:[{code, driver, team, win, podium, points, avg_finish, dist:[p1..pN]}]}}
  ```
- `/api/optimise` `{track, team, driver, overrides, driver_overrides, params:[key...],
  objective:"quali"|"race", max_evals:int|null (optional search budget, 2..800)}` -> `{job}`; result `{objective, before, after, gain, best:{key:value},
  sensitivities:[{key, label, unit, step, delta_per_step}], evaluations}`
- `/api/calibrate` `{}` -> `{job}` (re-runs preset calibration from cached public data)
- `/api/setups` `{name, track, team, driver, notes, values, driver_values}` -> `{ok, name}`
- `/api/setups/<name>/delete` `{}` -> `{ok}`
- `/api/liveries/<team>` `<livery>` -> `{ok}` (writes data/liveries/<team>.json)

## 2. Python internals

- `f1sim.tracks.load_track(id) -> dict` (numpy arrays `s, x, y, z, psi, kappa, grade, nx, ny`; scalars
  `ds, length, width`; lists `corners, kerbs, runoff`; `pit`; `type`, `night`, `laps`, `pit_loss_s`,
  `overtake_difficulty`).
- `f1sim.racing_line.get_line(track_id) -> path dict` with the same array keys evaluated on the racing line
  (plus `offset`, `grip` per-sample grip scale). Any path dict can be passed to the solver.
- `f1sim.car.params.make_car(team, overrides) -> car dict` (flat `{param: value}`); schema in `params.SCHEMA`.
- `f1sim.drivers.get_driver(driver_id, overrides) -> dict`.
- `f1sim.solver.solve(path, car, driver, state=None, quali=True, thermal=True, detail=True) -> dict`
  - `state`: `{fuel (kg), wear_f, wear_r (0..1), cl_scale, cd_scale (dirty air / slipstream), override (bool),
    grip_scale, rho, bumpiness, t_air, t_track}` - all optional.
  - fast call: `thermal=False, detail=False` -> `{lap_time, v, dt, vmax, mass, e_deploy_mj, ...}`;
    `thermal=True, detail=False` adds `wear_per_lap_f`, `wear_per_lap_r` (wear fraction per lap).
  - tyre compound / age are car params (`car["compound"]`), wear state is passed via `state`.
- `f1sim.car.tyres`: `COMPOUND` table, `wear_factor(wear)`, `WEAR_CLIFF`.

## 3. 3D scene module (`web/scene/scene.js`, ES module, Three.js vendored in `web/vendor/three/`)

```js
import { createScene } from './scene/scene.js';
const scene = await createScene(containerElement, { quality: 'medium' });   // appends its own <canvas>

scene.setTrack(trackGeom, { timeOfDay: 'day'|'sunset'|'night' });   // trackGeom = GET /api/track/<id>
scene.setLine(line, { colorBy: 'speed'|'throttle'|'off', telemetry }); // line/telemetry from /api/simulate
scene.setCars([{ id, team, code, number, livery, compound }]);     // replaces all cars (1..22)
scene.updateCars([{ id, x, y, z, psi, speed (m/s), kappa (1/m, steering), throttle, brake (0..1),
                    brakeTemp (C), xmode (bool), harvesting (bool), visible (bool) }]);  // call every frame
scene.setCamera(mode, targetId);  // 'orbit'|'chase'|'onboard'|'heli'|'tv'|'free'
scene.setOverlay({ line: 'speed'|'throttle'|'off', corners: bool, brakeMarkers: bool, tags: bool });
scene.setBrakeMarkers([{ x, y, z }]);
scene.setQuality('low'|'medium'|'high');
scene.setTimeOfDay('day'|'sunset'|'night');
scene.setLivery(teamId, livery);     // live preview from the livery editor
scene.onFrame((dtSeconds) => {});    // called before each render; the app advances the replay here
scene.getStats();                    // { fps, drawCalls, triangles, notes:[str] }  (notes = missing assets)
scene.resize(); scene.dispose();
```
The app owns replay time and computes each car's pose; the scene owns rendering, cameras, materials,
animation of car parts (wheel spin from speed, steering from kappa, flaps from xmode, brake glow).
Scene y-up convention is internal to the scene: it must map world (x east, y north, z up) itself.

## 3. Race result details (`f1sim/race/race_sim.py`, `montecarlo.py`)

Shape is the `/api/race` job result above, with these specifics:
- `classification[].time` is the total race time (s) and `gap` the gap to the winner in s; both are `null` for DNFs,
  and `gap` is `null` for lapped cars (`status:"+1 LAP"`). Order: finishers by laps then time, then DNFs by laps.
- `lap_chart[code]` has N+1 entries (index 0 = grid); `gaps[code]`, `lap_times[code]`, `energy[code]` have one entry per
  completed lap (a DNF car's arrays are shorter). Lap times include pit loss on in-laps and SC/VSC slowdowns.
- `events[].kind` is `SC|VSC|DNF|PIT`; SC/VSC rows carry `until_lap`. `overtakes[].s` is the passing-zone distance (m).
- `replay.t` is a uniform clock (`dt` = 2 s). `d` is cumulative distance (grid slots are at -8 m per row, so `d` starts
  negative); `compound_idx` is the index into C1..C5 (0..4); `pit` is 1 while in the pit lane; `speed` is km/h (0 once
  retired). Cars that finished hold `d = laps * lap_length`; retired cars hold their last `d`. `line` is the racing line
  decimated to about 600 points.
- `monte_carlo.table[]` adds `avg_points` and `dnf` to the documented fields; `win`, `podium` and `points` are
  probabilities (0..1, `points` = finishing in the top 10), `dist[p-1]` the share of runs finishing in position p.
  The top-level result of a Monte Carlo job is the run with `seed` (replayable), plus `monte_carlo`.
- Options: `safety_car` (default true), `dnf` (default true), `seed`, and `deg_scale` (multiplies tyre wear, default 1).

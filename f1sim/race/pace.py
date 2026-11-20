"""Per-car lap-time surface for the race simulator (brief §8.3.3).

A race needs ~22 cars x ~60 laps of lap times, far too many for the lap solver. Each entry gets a small
table instead, built from 13 fast solver calls plus one thermal call, and interpolated during the race:

    T(compound, fuel, wear) = T_wear(wear) + offset[compound] + (T_fuel(fuel) - T_fuel(fuel_mid)) + thermal offset

The fuel and wear effects are treated as additive: mass mostly costs time on traction/braking while wear
scales grip, and the cross term is a few hundredths of a second over the whole range (checked against a full
grid). Traffic effects are stored as per-sample time deltas along the lap (dirty air, tow, override) so the
race can apply them where they physically happen: downforce loss in corners, tow and override on straights.
"""
from __future__ import annotations

import hashlib
import json
import os
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from .. import drivers, solver, tracks
from ..car import params, tyres

WEAR_GRID = np.array([0.0, 0.40, 0.75, 0.90, 1.0])   # knots bracket the cliff at tyres.WEAR_CLIFF
FUEL_GRID = np.array([0.0, 45.0, 90.0])               # kg
REF_KNOT = 1                    # wear knot used for the sector shape and the traffic variants
WAKE_CL_SCALE = 0.92            # reference dirty-air variant: 8% downforce loss
WAKE_CD_SCALE = 0.90            # reference tow variant: 10% drag reduction
THERMAL_OFFSET = True           # shift the surface by (thermal - fast) solve so it matches race-pace runs

_CACHE: dict[str, "PaceSurface"] = {}
_PATHS: dict[str, dict] = {}


@dataclass
class PaceSurface:
    """Lap-time model of one entry (car + driver) on one track."""
    key: str
    compounds: list[str]                 # event compounds, hardest first
    fuel_grid: np.ndarray
    wear_grid: np.ndarray
    table: np.ndarray                    # [compound, fuel, wear] lap time, s
    wear_rate: np.ndarray                # [compound] wear fraction per lap of the faster-wearing axle
    wear_ratio_f: float                  # front / limiting-axle wear (<= 1 when rear limited)
    wear_ratio_r: float
    e_deploy_mj: float                   # MGU-K energy deployed per lap in race mode
    e_override_mj: float                 # extra deployment on an override lap
    quali_time: float
    v_ref: np.ndarray                    # speed trace of the reference race lap, m/s per sample
    dt_ref: np.ndarray                   # time per sample of the reference lap
    dt_wake: np.ndarray                  # per-sample time delta at WAKE_CL_SCALE (mostly > 0, in corners)
    dt_tow: np.ndarray                   # per-sample time delta at WAKE_CD_SCALE (mostly < 0, on straights)
    dt_override: np.ndarray              # per-sample time delta with override mode
    solver_calls: int = 0
    meta: dict = field(default_factory=dict)

    def lap_time(self, compound: int | str, fuel: float, wear: float) -> float:
        ci = self.compounds.index(compound) if isinstance(compound, str) else int(compound)
        row = np.array([np.interp(wear, self.wear_grid, self.table[ci, k]) for k in range(len(self.fuel_grid))])
        return float(np.interp(fuel, self.fuel_grid, row))

    def stint_times(self, compound: int | str, n_laps: int, fuel: float) -> np.ndarray:
        """Lap times of a stint at constant fuel (the degradation curve the strategy optimiser needs)."""
        ci = self.compounds.index(compound) if isinstance(compound, str) else int(compound)
        wear = np.minimum(self.wear_rate[ci] * np.arange(n_laps), 1.0)
        f = np.interp(fuel, self.fuel_grid, np.arange(len(self.fuel_grid)))
        lo = int(min(np.floor(f), len(self.fuel_grid) - 2))
        a = np.interp(wear, self.wear_grid, self.table[ci, lo])
        b = np.interp(wear, self.wear_grid, self.table[ci, lo + 1])
        return a + (b - a) * (f - lo)


def get_path(track_id: str) -> dict:
    """Racing line if the line module is available, else the centerline (same keys)."""
    if track_id not in _PATHS:
        try:
            from ..racing_line import get_line
            _PATHS[track_id] = get_line(track_id)
        except Exception:  # noqa: BLE001 - module still being written / cache miss: centerline is a valid path
            _PATHS[track_id] = tracks.load_track(track_id)
    return _PATHS[track_id]


def race_car(team: str | None, overrides: dict | None) -> dict:
    car = params.make_car(team, overrides)
    car["pu_mode"] = "race"
    return car


def _key(track_id: str, path: dict, car: dict, driver: dict, compounds: list[str], fuel_grid) -> str:
    kap = np.asarray(path["kappa"])
    blob = json.dumps({
        "track": track_id, "path": [len(kap), float(path["ds"]), round(float(np.abs(kap).sum()), 9),
                                    round(float(np.asarray(path.get("grip", [1.0])).sum()), 6)],
        "car": {k: v for k, v in sorted(car.items()) if k not in ("compound", "pu_mode")},
        "driver": {k: v for k, v in sorted(driver.items())}, "compounds": compounds,
        "fuel": [float(f) for f in fuel_grid], "thermal": THERMAL_OFFSET,
    }, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def _prepare(track_id, team, driver_id, overrides, driver_overrides, compounds, fuel_max, path):
    from .strategy import event_compounds
    path = path or get_path(track_id)
    compounds = list(compounds or event_compounds(track_id, path))
    car = race_car(team, overrides)
    drv = drivers.get_driver(driver_id, driver_overrides)
    fuel_grid = FUEL_GRID if not fuel_max or fuel_max <= FUEL_GRID[-1] else np.array([0.0, fuel_max / 2, fuel_max])
    return path, compounds, car, drv, fuel_grid, _key(track_id, path, car, drv, compounds, fuel_grid)


def _job_key(job: tuple, path: dict) -> str:
    return _prepare(*job, path)[-1]


def build_surface(track_id: str, team: str | None, driver_id: str | None, overrides: dict | None = None,
                  driver_overrides: dict | None = None, compounds: list[str] | None = None,
                  fuel_max: float | None = None, path: dict | None = None) -> PaceSurface:
    """Build (or fetch from the in-memory cache) the lap-time surface of one entry."""
    path, compounds, car, drv, fuel_grid, key = _prepare(track_id, team, driver_id, overrides, driver_overrides,
                                                         compounds, fuel_max, path)
    if key in _CACHE:
        return _CACHE[key]

    calls = 0
    ref = compounds[len(compounds) // 2]
    f_mid = float(fuel_grid[len(fuel_grid) // 2])

    def run(compound, fuel, wear_f, wear_r, thermal=False, **extra):
        nonlocal calls
        calls += 1
        c = dict(car, compound=compound)
        st = {"fuel": fuel, "wear_f": wear_f, "wear_r": wear_r, **extra}
        return solver.solve(path, c, drv, st, quali=False, thermal=thermal, detail=False)

    # One thermal solve gives the sliding energy split, hence which axle wears faster and how fast.
    th = run(ref, f_mid, 0.0, 0.0, thermal=True)
    wf, wr = max(th["wear_per_lap_f"], 1e-6), max(th["wear_per_lap_r"], 1e-6)
    lim = max(wf, wr)
    rf, rr = wf / lim, wr / lim
    # other compounds: same sliding energy, wear scales with the compound's wear coefficient
    k_ref = tyres.COMPOUND[ref]["wear"]
    wear_rate = np.array([lim * tyres.COMPOUND[c]["wear"] / k_ref for c in compounds])

    # Wear curve on the reference compound; the other compounds are a constant offset on top. Grip enters
    # the car model as mu_compound * wear_factor, so a compound change is equivalent to a shift along the
    # same grip axis and one extra solve per compound is enough.
    runs = [run(ref, f_mid, w * rf, w * rr) for w in WEAR_GRID]
    t_ref = np.array([r["lap_time"] for r in runs])
    c_off = np.array([0.0 if c == ref else run(c, f_mid, 0.0, 0.0)["lap_time"] - t_ref[0] for c in compounds])
    t_fuel = np.array([t_ref[0] if f == f_mid else run(ref, f, 0.0, 0.0)["lap_time"] for f in fuel_grid])
    offset = (th["lap_time"] - t_ref[0]) if THERMAL_OFFSET else 0.0
    table = (t_ref[None, None, :] + c_off[:, None, None]
             + (t_fuel - t_fuel[len(fuel_grid) // 2])[None, :, None] + offset)

    w0 = float(WEAR_GRID[REF_KNOT])
    base = runs[REF_KNOT]
    wake = run(ref, f_mid, w0 * rf, w0 * rr, cl_scale=WAKE_CL_SCALE)
    tow = run(ref, f_mid, w0 * rf, w0 * rr, cd_scale=WAKE_CD_SCALE)
    ovr = run(ref, f_mid, w0 * rf, w0 * rr, override=True)

    qcar = dict(car, compound=compounds[-1], pu_mode="quali")
    quali = solver.solve(path, qcar, drv, None, quali=True, thermal=False, detail=False)["lap_time"]
    calls += 1

    surf = PaceSurface(
        key=key, compounds=compounds, fuel_grid=np.asarray(fuel_grid, float), wear_grid=WEAR_GRID.copy(),
        table=table, wear_rate=wear_rate, wear_ratio_f=rf, wear_ratio_r=rr,
        e_deploy_mj=float(base["e_deploy_mj"]),
        e_override_mj=float(max(ovr["e_deploy_mj"] - base["e_deploy_mj"], 0.0)),
        quali_time=float(quali), v_ref=np.asarray(base["v"], float), dt_ref=np.asarray(base["dt"], float),
        dt_wake=np.asarray(wake["dt"] - base["dt"], float), dt_tow=np.asarray(tow["dt"] - base["dt"], float),
        dt_override=np.asarray(ovr["dt"] - base["dt"], float), solver_calls=calls,
        meta={"team": team, "driver": driver_id, "track": track_id, "thermal_offset": float(offset),
              "fuel_per_lap": float(car["fuel_per_lap"]) * float(path["length"]) / 5000.0},
    )
    _CACHE[key] = surf
    return surf


def _build_job(args):
    return build_surface(*args)


def _fork_is_safe() -> bool:
    # fork() from a process with other live threads can inherit held locks; only fan out to worker
    # processes from a single-threaded caller (CLI, tests, scripts). A threaded server builds in-process.
    return hasattr(os, "fork") and threading.active_count() == 1


def build_surfaces(track_id: str, entries: list[dict], compounds: list[str] | None = None,
                   fuel_max: float | None = None, progress=None, workers: int | None = None) -> list[PaceSurface]:
    """Surfaces for all entries, cached in memory by a hash of car + driver + track.

    The solver is CPU-bound Python/numba that holds the GIL, so threads do not help; missing surfaces are
    built in forked worker processes when that is safe, otherwise one after another.
    """
    from .strategy import event_compounds
    path = get_path(track_id)
    compounds = list(compounds or event_compounds(track_id, path))
    jobs = [(track_id, e.get("team"), e.get("driver"), e.get("overrides"), e.get("driver_overrides"),
             compounds, fuel_max) for e in entries]
    out: list[PaceSurface | None] = [None] * len(jobs)
    done = 0

    def tick():
        nonlocal done
        done += 1
        if progress:
            progress(done / max(len(jobs), 1), f"pace surface {done}/{len(jobs)}")

    todo = []
    for i, j in enumerate(jobs):
        key = _job_key(j, path)
        if key in _CACHE:
            out[i] = _CACHE[key]
            tick()
        else:
            todo.append(i)
    if not todo:
        return out
    solver.solve(path, race_car(None, None), None, {"fuel": 10.0}, quali=False, thermal=False, detail=False)  # JIT warm-up
    workers = workers if workers is not None else min(8, os.cpu_count() or 1, len(todo))
    if workers > 1 and len(todo) > 2 and _fork_is_safe():
        try:
            ctx = multiprocessing.get_context("fork")
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                for i, s in zip(todo, pool.map(_build_job, [jobs[i] for i in todo])):
                    out[i] = _CACHE.setdefault(s.key, s)
                    tick()
            return out
        except Exception:  # noqa: BLE001 - no fork / broken pool: fall through to the in-process build
            pass
    for i in todo:
        if out[i] is None:
            out[i] = build_surface(*jobs[i], path=path)
            tick()
    return out


def clear_cache() -> None:
    _CACHE.clear()
    _PATHS.clear()

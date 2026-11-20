"""Setup optimiser (brief §8.4): bounded search over chosen setup parameters with a lap-time cache.

Objectives
  quali  single flying lap: quali fuel, fresh tyres, thermal model on (tyre temperature and ride-height
         trade-offs count).
  race   race-pace proxy: Simpson average of the lap time over one stint, with fuel burning off and the
         tyres wearing at the rate the solver reports for this setup (PU in race mode, net-zero energy).

Search: every parameter lives on its schema step grid (value = min + k*step), normalised to [0, 1].
  1. differential evolution (scipy) with a small population seeded around the starting setup
  2. Nelder-Mead (scipy) from the best point
  3. coordinate polish on the step grid
All stages share one cache keyed on the integer grid vector and one evaluation budget.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from scipy.optimize import differential_evolution, minimize

from . import solver, tracks
from .car import params as P
from .drivers import get_driver

DEFAULT_PARAMS = ["front_wing", "rear_wing", "ride_height_f", "ride_height_r", "spring_f", "spring_r",
                  "arb_f", "arb_r", "brake_bias", "diff_lock", "camber_f", "camber_r", "pressure_f",
                  "pressure_r", "final_drive"]
# calibration factors, masses, power and rule values describe the car/regulations, not the setup:
# they are only optimised when the caller names them explicitly
PROTECTED = set(P.TEAM_FIT_KEYS) | set(P.GLOBAL_FIT_KEYS) | {"mass", "fuel_load", "quali_fuel", "mguk_power"}
OBJECTIVES = ("quali", "race")
FAIL = 1e6                 # objective value for a setup the solver cannot run
STINT_SHARE = 0.35         # stint length as a share of the race distance
STINT_MIN, STINT_MAX = 10, 25
SENS_SPAN = 0.02           # finite-difference half-span as a share of the parameter range (>= 1 step)


class _Budget(Exception):
    """Raised inside the objective when the evaluation budget is spent."""


def get_path(track_id: str) -> dict:
    """Racing line if the module is available, otherwise the track centreline."""
    try:
        from .racing_line import get_line
        return get_line(track_id)
    except ImportError:
        return tracks.load_track(track_id)


def tunable(key: str, explicit: bool = False) -> bool:
    """True if `key` is a numeric setup parameter the optimiser may move."""
    p = P.BY_KEY.get(key)
    if p is None or p["kind"] != "float" or not p["step"] or p["max"] <= p["min"]:
        return False
    return explicit or (key not in PROTECTED and p["prov"] != P.REG)


def select_params(params: list[str] | None) -> tuple[list[str], list[str]]:
    """(usable keys, skipped keys). Explicitly passed keys may include protected calibration values."""
    if params is None:
        return [k for k in DEFAULT_PARAMS if tunable(k)], []
    keys, skipped = [], []
    for k in dict.fromkeys(params):
        (keys if tunable(k, explicit=True) else skipped).append(k)
    return keys, skipped


class Grid:
    """Schema step grid for the chosen parameters: integer index vector <-> values <-> unit cube."""

    def __init__(self, keys: list[str]):
        self.keys = keys
        sch = [P.BY_KEY[k] for k in keys]
        self.lo = np.array([p["min"] for p in sch], float)
        self.step = np.array([p["step"] for p in sch], float)
        self.n = np.array([int(math.floor((p["max"] - p["min"]) / p["step"] + 1e-9)) for p in sch])
        self.dec = [max(0, -int(math.floor(math.log10(p["step"]) + 1e-9))) + 2 for p in sch]

    def index(self, values) -> np.ndarray:
        k = np.rint((np.asarray(values, float) - self.lo) / self.step).astype(int)
        return np.clip(k, 0, self.n)

    def from_unit(self, u) -> np.ndarray:
        return np.clip(np.rint(np.asarray(u, float) * self.n).astype(int), 0, self.n)

    def to_unit(self, k) -> np.ndarray:
        return np.asarray(k, float) / self.n

    def values(self, k) -> dict:
        return {key: round(float(self.lo[i] + k[i] * self.step[i]), self.dec[i]) for i, key in enumerate(self.keys)}


def stint_laps(path: dict) -> int:
    laps = path.get("laps") or round(305_000.0 / float(path.get("length", 5000.0)))
    return int(np.clip(round(STINT_SHARE * laps), STINT_MIN, STINT_MAX))


def lap_objective(path: dict, car: dict, driver: dict, objective: str = "quali", state: dict | None = None) -> float:
    """Objective value in seconds for one complete car dict."""
    st = dict(state or {})
    if objective == "quali":
        st.setdefault("wear_f", 0.0)
        st.setdefault("wear_r", 0.0)
        return float(solver.solve(path, car, driver, st, quali=True, thermal=True, detail=False)["lap_time"])
    return _stint_average(path, car, driver, st)


def _stint_average(path: dict, car: dict, driver: dict, st: dict) -> float:
    """Average lap over a stint from 1 thermal + 4 fast solver calls.

    The thermal call at mid-stint fuel gives the wear rates and the tyre-temperature effect on lap time;
    fast calls at the start, middle and end of the stint give the fuel burn-off and wear slope.
    """
    n = stint_laps(path)
    burn = car["fuel_per_lap"] * float(path.get("length", 5000.0)) / 5000.0
    fuel0 = float(st.get("fuel", car["fuel_load"]))
    fuel = [max(fuel0 - burn * lap, 1.0) for lap in (0.0, 0.5 * n, float(n))]

    def run(f, wf, wr, thermal):
        s = dict(st, fuel=f, wear_f=wf, wear_r=wr)
        return solver.solve(path, car, driver, s, quali=False, thermal=thermal, detail=False)

    ref = run(fuel[1], 0.0, 0.0, True)
    wf, wr = ref["wear_per_lap_f"], ref["wear_per_lap_r"]
    thermal_offset = ref["lap_time"] - run(fuel[1], 0.0, 0.0, False)["lap_time"]
    age = float(car.get("tyre_age", 0.0))
    t = [run(fuel[i], min(wf * (age + lap), 1.0), min(wr * (age + lap), 1.0), False)["lap_time"]
         for i, lap in enumerate((0.0, 0.5 * n, float(n)))]
    return float((t[0] + 4.0 * t[1] + t[2]) / 6.0 + thermal_offset)


class _Problem:
    """Objective on the step grid with the cache and the evaluation budget."""

    def __init__(self, path, car, driver, grid: Grid, objective, state, max_evals, progress):
        self.path, self.car, self.driver, self.grid = path, car, driver, grid
        self.objective, self.state = objective, state
        self.cache: dict[tuple, float] = {}
        self.max_evals = max_evals
        self.evals = 0
        self.hits = 0
        self.best_k, self.best_f = None, math.inf
        self.progress = progress
        self.limited = True

    def car_at(self, k) -> dict:
        return {**self.car, **self.grid.values(k)}

    def at(self, k) -> float:
        key = tuple(int(i) for i in k)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        if self.limited and self.evals >= self.max_evals:
            raise _Budget
        try:
            f = lap_objective(self.path, self.car_at(key), self.driver, self.objective, self.state)
            if not math.isfinite(f):
                f = FAIL
        except (ArithmeticError, ValueError, IndexError):
            f = FAIL
        self.evals += 1
        self.cache[key] = f
        if f < self.best_f:
            self.best_f, self.best_k = f, np.array(key)
        if self.progress and self.limited:
            self.progress(0.9 * self.evals / self.max_evals, f"{self.evals} setups tried, best {self.best_f:.3f} s")
        return f

    def unit(self, u) -> float:
        return self.at(self.grid.from_unit(u))


def _search(pb: _Problem, k0: np.ndarray, seed: int) -> None:
    g, n = pb.grid, len(pb.grid.keys)
    u0 = g.to_unit(k0)
    rng = np.random.default_rng(seed)
    try:
        pb.at(k0)
        # --- 1. global: differential evolution, population seeded around the starting setup
        pop = max(6, min(2 * n, 14))
        init = np.clip(u0 + rng.normal(0.0, 0.15, (pop, n)), 0.0, 1.0)
        init[0] = u0
        de_budget = int(0.5 * pb.max_evals)
        differential_evolution(pb.unit, [(0.0, 1.0)] * n, init=init, maxiter=max(1, de_budget // pop - 1),
                               mutation=(0.4, 0.9), recombination=0.8, tol=0.0, atol=0.0, polish=False,
                               seed=seed, updating="immediate")
        # --- 2. local: Nelder-Mead from the best point
        ub = g.to_unit(pb.best_k)
        d = np.maximum(0.06, 1.5 / g.n)
        simplex = np.vstack([ub] + [np.clip(ub + np.eye(n)[i] * (d[i] if ub[i] + d[i] <= 1.0 else -d[i]), 0, 1)
                                    for i in range(n)])
        minimize(pb.unit, ub, method="Nelder-Mead", bounds=[(0.0, 1.0)] * n,
                 options={"initial_simplex": simplex, "maxfev": int(0.3 * pb.max_evals),
                          "xatol": float(0.4 / g.n.max()), "fatol": 1e-4})
        # --- 3. polish: coordinate moves on the step grid, coarse to fine
        for m in (4, 2, 1):
            improved = True
            while improved:
                improved = False
                for i in range(n):
                    for sgn in (1, -1):
                        k = pb.best_k.copy()
                        k[i] = int(np.clip(k[i] + sgn * m, 0, g.n[i]))
                        f_prev = pb.best_f
                        if pb.at(k) < f_prev - 1e-9:
                            improved = True
                            break
    except _Budget:
        pass


def _sensitivities(pb: _Problem, k: np.ndarray, base_car: dict | None = None) -> list[dict]:
    """Central finite difference of the objective, in seconds per schema step, around grid point k.
    The half-span is at least one step and ~2% of the range, so solver discretisation noise averages out;
    at a bound the difference becomes one-sided."""
    g = pb.grid
    out = []
    for i, key in enumerate(g.keys):
        h = max(1, int(round(SENS_SPAN * g.n[i])))
        up, dn = np.array(k), np.array(k)
        up[i] = min(k[i] + h, g.n[i])
        dn[i] = max(k[i] - h, 0)
        span = int(up[i] - dn[i])
        f_up, f_dn = pb.at(up), pb.at(dn)
        ok = span > 0 and f_up < FAIL and f_dn < FAIL
        sch = P.BY_KEY[key]
        out.append({"key": key, "label": sch["label"], "unit": sch["unit"], "step": sch["step"],
                    "delta_per_step": round((f_up - f_dn) / span, 5) if ok else 0.0})
    return out


def _setup(track_id, team, driver, overrides, driver_overrides, params, objective, state):
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {OBJECTIVES}, got '{objective}'")
    keys, skipped = select_params(params)
    if not keys:
        raise ValueError("no optimisable numeric parameters selected")
    car = P.make_car(team, overrides)
    if objective == "race" and "pu_mode" not in (overrides or {}):
        car["pu_mode"] = "race"
    return get_path(track_id), car, get_driver(driver, driver_overrides), Grid(keys), skipped, dict(state or {})


def sensitivities(track_id: str, team: str | None, driver: str | None, overrides: dict | None = None,
                  driver_overrides: dict | None = None, params: list[str] | None = None,
                  objective: str = "quali", state: dict | None = None) -> list[dict]:
    """Sensitivity table at the given setup (no optimisation): [{key, label, unit, step, delta_per_step}]."""
    path, car, drv, grid, _, st = _setup(track_id, team, driver, overrides, driver_overrides, params, objective, state)
    pb = _Problem(path, car, drv, grid, objective, st, 0, None)
    pb.limited = False
    return _sensitivities(pb, grid.index([car[k] for k in grid.keys]))


def optimise(track_id: str, team: str | None, driver: str | None, overrides: dict | None = None,
             driver_overrides: dict | None = None, params: list[str] | None = None, objective: str = "quali",
             progress: Callable[[float, str], None] | None = None, max_evals: int | None = None,
             state: dict | None = None, seed: int = 0) -> dict:
    """Optimise the chosen setup parameters for a track / team / driver.

    params: schema keys to move (default DEFAULT_PARAMS); non-numeric or unknown keys are skipped.
    max_evals: budget of distinct setups for the search (default 40 + 50 per parameter, at most 800);
        the sensitivity table costs up to 2 more per parameter.
    Returns {objective, before, after, gain, best, sensitivities, evaluations, start, skipped}; `after` is
    never above `before` - if the search finds nothing better, `best` is the starting setup.
    """
    path, car, drv, grid, skipped, st = _setup(track_id, team, driver, overrides, driver_overrides, params,
                                               objective, state)
    n = len(grid.keys)
    if max_evals is None:
        max_evals = min(800, 40 + 50 * n)
    max_evals = max(int(max_evals), 2)
    if progress:
        progress(0.0, "evaluating the starting setup")
    start = {k: car[k] for k in grid.keys}
    before = lap_objective(path, car, drv, objective, st)

    pb = _Problem(path, car, drv, grid, objective, st, max_evals, progress)
    k0 = grid.index(list(start.values()))
    _search(pb, k0, seed)
    pb.limited = False
    if pb.best_k is not None and pb.best_f < before:
        k_best, after, best = pb.best_k, pb.best_f, grid.values(pb.best_k)
    else:  # nothing better than the starting setup (which may sit off the step grid)
        k_best, after, best = k0, before, {k: float(v) for k, v in start.items()}
    if progress:
        progress(0.92, "computing sensitivities")
    sens = _sensitivities(pb, k_best)
    if progress:
        progress(1.0, f"done: {before:.3f} s -> {after:.3f} s")
    return {"objective": objective, "before": round(before, 4), "after": round(after, 4),
            "gain": round(before - after, 4), "best": best, "sensitivities": sens,
            "evaluations": pb.evals + 1, "start": {k: float(v) for k, v in start.items()}, "skipped": skipped}

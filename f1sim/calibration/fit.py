"""Preset calibration (brief §6): global grip/aero fit to pole laps, then a per-team least-squares fit.

Everything comes from the cached public data in data/public/2026/<NN>/ (OpenF1 / Jolpica). Real team data
is secret, so the fitted values are *effective* parameters that reproduce public lap times, not real numbers.

  1. global: mu_scale, cla_floor, cda_body so the baseline car (default driver) matches the pole lap on a few
     dry tracks (robust least squares, mild regularisation toward the defaults).
  2. per team: ClA_scale, CdA_scale, ice_power, energy_efficiency, mass_above_min (tyre_wear_scale is left at
     its default: stint data is not fitted here) so the simulated gap to pole matches the team's quali gaps
     and, where telemetry exists, its top-speed difference. Regularised toward the baseline.
  3. reports: _report.json (fit vs standings), _validation.json (global model vs real pole times).

Wet sessions (rain fraction > 0.2) are skipped. Air density and temperatures come from each session's weather.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

from .. import solver
from ..car import params as P
from ..drivers import compute_nudges, get_driver
from ..optimise import get_path

ROOT = Path(__file__).resolve().parent.parent.parent
PUBLIC = ROOT / "data" / "public" / "2026"
PRESETS = ROOT / "data" / "presets"
RAIN_MAX = 0.2
GLOBAL_TRACKS = ["monza", "monaco", "silverstone", "suzuka", "baku", "mexico"]
TEAM_TRACKS = ["monza", "monaco", "silverstone", "suzuka", "baku", "mexico", "catalunya", "cota"]
TEAM_FIT = ["ClA_scale", "CdA_scale", "ice_power", "energy_efficiency", "mass_above_min"]
# regularisation: residual units are per cent of lap time; each parameter's prior sigma in schema units
PRIOR_SIGMA = {"ClA_scale": 0.06, "CdA_scale": 0.06, "ice_power": 20.0, "energy_efficiency": 0.06,
               "mass_above_min": 8.0, "mu_scale": 0.08, "cla_floor": 0.8, "cda_body": 0.15}
SPEED_WEIGHT = 0.08          # per-cent-equivalent residual per km/h of team top-speed error
REG_WEIGHT = 0.12


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def air_density(p_hpa: float | None, t_air: float) -> float:
    """Air density [kg/m3] from the station pressure (already includes altitude) and temperature (default sea-level 1.2)."""
    if not p_hpa:
        return 1.2
    return float(np.clip(p_hpa * 100.0 / (287.05 * (t_air + 273.15)), 0.85, 1.3))


def load_sessions() -> dict:
    """{track_id: {round, pole, teams:{team: best_lap}, state:{rho,t_air,t_track}, vmax:{team: km/h}}} for dry rounds."""
    out = {}
    for rd in sorted(PUBLIC.glob("[0-9][0-9]")):
        meta = _json(rd / "meta.json", {})
        quali = _json(rd / "qualifying.json", [])
        tel = _json(rd / "tel_quali.json", {})
        track = meta.get("track")
        w = tel.get("weather") or {}
        if not track or not quali or w.get("rain_frac", 0.0) > RAIN_MAX:
            continue
        best = {}
        for q in quali:
            v = [x for x in (q.get("q1"), q.get("q2"), q.get("q3")) if x]
            if v:
                best[q["team"]] = min(best.get(q["team"], 1e9), min(v))
        for lap in tel.get("laps", []):     # also catches laps missing from the classification
            if lap.get("team") and lap.get("lap"):
                best[lap["team"]] = min(best.get(lap["team"], 1e9), lap["lap"])
        if not best:
            continue
        vmax = {}
        for lap in tel.get("laps", []):
            v = lap.get("vmax_tel") or lap.get("speed_st_max") or lap.get("speed_st")
            if lap.get("team") and v:
                vmax[lap["team"]] = max(vmax.get(lap["team"], 0.0), float(v))
        t_air = float(w.get("air_temp", 25.0))
        pressure = w.get("pressure")
        rho = air_density(pressure, t_air)
        out[track] = {"round": meta.get("round"), "pole": min(best.values()), "teams": best, "vmax": vmax,
                      "state": {"rho": rho, "t_air": t_air, "t_track": float(w.get("track_temp", 38.0))}}
    return out


def quali_state(track_id: str) -> dict:
    """Solver state (air density, temperatures) of the real qualifying session at this track, or {}."""
    s = load_sessions().get(track_id)
    return dict(s["state"]) if s else {}


class _Sim:
    def __init__(self, tracks):
        self.sessions = {t: s for t, s in load_sessions().items() if t in tracks}
        self.paths = {t: get_path(t) for t in self.sessions}
        self.driver = get_driver(None)
        self.n_solves = 0

    def lap(self, track, car, vmax=False):
        r = solver.solve(self.paths[track], car, self.driver, dict(self.sessions[track]["state"]), quali=True,
                         thermal=True, detail=False)
        self.n_solves += 1
        return (r["lap_time"], r["vmax"] * 3.6) if vmax else r["lap_time"]


def _norm(key, v):
    return (v - P.DEFAULTS[key]) / PRIOR_SIGMA[key]


def _clip(key, v):
    p = P.BY_KEY[key]
    return float(min(max(v, p["min"]), p["max"]))


def fit_global(sim: _Sim, progress=None) -> dict:
    keys = P.GLOBAL_FIT_KEYS
    tracks = list(sim.sessions)

    def resid(x):
        car = P.make_car(None, dict(zip(keys, x)))
        car.update({k: _clip(k, v) for k, v in zip(keys, x)})
        r = [100.0 * (sim.lap(t, car) / sim.sessions[t]["pole"] - 1.0) for t in tracks]
        return np.array(r + [REG_WEIGHT * _norm(k, v) for k, v in zip(keys, x)])

    x0 = np.array([P.DEFAULTS[k] for k in keys], float)
    lo = np.array([P.BY_KEY[k]["min"] for k in keys], float)
    hi = np.array([P.BY_KEY[k]["max"] for k in keys], float)
    scale = np.array([PRIOR_SIGMA[k] for k in keys])
    sol = least_squares(resid, x0, bounds=(lo, hi), x_scale=scale, loss="soft_l1", f_scale=1.0, diff_step=0.02,
                        max_nfev=25)
    return {k: round(float(v), 4) for k, v in zip(keys, sol.x)}


def _team_targets(sessions: dict) -> dict:
    """{team: {track: gap to pole [%]}} and median gaps."""
    out = {}
    for t, s in sessions.items():
        for team, lap in s["teams"].items():
            out.setdefault(team, {})[t] = 100.0 * (lap / s["pole"] - 1.0)
    return out


def fit_team(sim: _Sim, team: str, targets: dict, base_values: dict, ref: dict) -> dict:
    """Least-squares fit of the team keys. `ref[track] = (lap, vmax)` of the global (pole) car."""
    tracks = [t for t in targets if t in sim.sessions]

    def car_for(x):
        car = P.make_car(None, base_values)
        for k, v in zip(TEAM_FIT, x):
            car[k] = _clip(k, v)
        return car

    def resid(x):
        car = car_for(x)
        r = []
        for t in tracks:
            lap, vm = sim.lap(t, car, vmax=True)
            r.append(100.0 * (lap / ref[t][0] - 1.0) - targets[t])
            vreal = sim.sessions[t]["vmax"]
            if team in vreal and vreal:
                real_diff = vreal[team] - float(np.median(list(vreal.values())))
                r.append(SPEED_WEIGHT * ((vm - ref[t][1]) - real_diff) / 5.0)
        r += [REG_WEIGHT * _norm(k, v) for k, v in zip(TEAM_FIT, x)]
        return np.array(r)

    x0 = np.array([P.DEFAULTS[k] for k in TEAM_FIT], float)
    lo = np.array([P.BY_KEY[k]["min"] for k in TEAM_FIT], float)
    hi = np.array([P.BY_KEY[k]["max"] for k in TEAM_FIT], float)
    sol = least_squares(resid, x0, bounds=(lo, hi), x_scale=np.array([PRIOR_SIGMA[k] for k in TEAM_FIT]),
                        diff_step=0.03, max_nfev=20)
    car = car_for(sol.x)
    sim_gaps = {t: 100.0 * (sim.lap(t, car) / ref[t][0] - 1.0) for t in tracks}
    return {"values": {k: round(float(v), 4) for k, v in zip(TEAM_FIT, sol.x)}, "sim_gaps": sim_gaps}


def write_driver_ratings() -> dict:
    """Teammate qualifying head-to-heads -> data/driver_ratings.json (read by f1sim.drivers)."""
    nud = compute_nudges()
    (ROOT / "data" / "driver_ratings.json").write_text(json.dumps({
        "grip_use_nudge": nud,
        "note": "Teammate qualifying head-to-head (dry rounds, public timing) mapped to a small grip_use nudge; "
                "half-weighted and capped; public-derived."}, indent=1))
    return nud


def run_calibration(progress=None) -> dict:
    """Run the whole calibration, write data/presets/*.json and return the report dict."""
    t0 = time.time()
    prog = progress or (lambda f, m=None: None)
    PRESETS.mkdir(parents=True, exist_ok=True)
    sessions_all = load_sessions()
    track_set = list(dict.fromkeys(GLOBAL_TRACKS + TEAM_TRACKS))
    sim = _Sim([t for t in track_set])
    gtracks = [t for t in GLOBAL_TRACKS if t in sim.sessions]
    ttracks = [t for t in TEAM_TRACKS if t in sim.sessions]
    notes = [f"dry sessions used: {len(sim.sessions)}; wet sessions skipped (rain fraction > {RAIN_MAX})",
             "calibrated with the default driver rating (no driver nudge); tyre_wear_scale is not fitted"]
    # clear stale presets so a failed run cannot leave a mixed set
    for f in PRESETS.glob("*.json"):
        f.unlink()
    prog(0.02, "global fit (grip / aero / drag)")
    gsim = _Sim(gtracks)
    gvals = fit_global(gsim)
    (PRESETS / "_global.json").write_text(json.dumps({"values": gvals, "prov": {k: P.PUB for k in gvals},
                                                      "tracks": gtracks}, indent=1))
    prog(0.25, "global fit done")

    gcar = P.make_car(None)
    ref = {t: sim.lap(t, gcar, vmax=True) for t in ttracks}
    # validation: global baseline car vs real pole on every dry track we have a line for
    validation = []
    for t, s in sorted(sessions_all.items()):
        try:
            path = get_path(t)
        except Exception:  # noqa: BLE001 - a track without a line is simply not validated
            continue
        r = solver.solve(path, gcar, sim.driver, dict(s["state"]), quali=True, thermal=True, detail=False)
        validation.append({"track": t, "real_pole": round(s["pole"], 3), "sim_pole": round(r["lap_time"], 3),
                           "err_pct": round(100.0 * (r["lap_time"] / s["pole"] - 1.0), 2),
                           "fitted": t in gtracks})
    (PRESETS / "_validation.json").write_text(json.dumps(validation, indent=1))

    tgt = _team_targets({t: sim.sessions[t] for t in ttracks})
    standings = _json(PUBLIC / "constructor_standings.json", {}).get("standings", [])
    pos = {s["team"]: s["pos"] for s in standings}
    teams_out = []
    team_ids = sorted(tgt)
    for i, team in enumerate(team_ids):
        prog(0.3 + 0.65 * i / max(len(team_ids), 1), f"fitting {team}")
        gaps = tgt[team]
        res = fit_team(sim, team, gaps, gvals, ref)
        med_t = float(np.median(list(gaps.values())))
        med_s = float(np.median([res["sim_gaps"][t] for t in gaps if t in res["sim_gaps"]]))
        fit = {"target_gap_pct": round(med_t, 3), "sim_gap_pct": round(med_s, 3),
               "residual_pct": round(med_s - med_t, 3), "tracks": list(res["sim_gaps"]),
               "per_track": {t: {"target": round(gaps[t], 3), "sim": round(res["sim_gaps"][t], 3)} for t in gaps
                             if t in res["sim_gaps"]}}
        pre = {"team": team, "values": res["values"], "prov": {k: P.PUB for k in res["values"]}, "fit": fit}
        (PRESETS / f"{team}.json").write_text(json.dumps(pre, indent=1))
        teams_out.append({"team": team, "target_gap_pct": fit["target_gap_pct"], "sim_gap_pct": fit["sim_gap_pct"],
                          "residual_pct": fit["residual_pct"], "params": res["values"],
                          "standing_pos": pos.get(team)})
    order = sorted(teams_out, key=lambda r: r["sim_gap_pct"])
    for rank, r in enumerate(order, 1):
        r["pace_rank"] = rank
    ranked = [r for r in teams_out if r["standing_pos"]]
    if len(ranked) > 2:
        a = np.argsort(np.argsort([r["standing_pos"] for r in ranked]))
        b = np.argsort(np.argsort([r["pace_rank"] for r in ranked]))
        rho = float(np.corrcoef(a, b)[0, 1])
        notes.append(f"Spearman correlation of fitted pace order vs constructor standings: {rho:.2f}")
    write_driver_ratings()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "global": gvals,
              "teams": sorted(teams_out, key=lambda r: r["pace_rank"]), "notes": notes,
              "runtime_s": round(time.time() - t0, 1)}
    (PRESETS / "_report.json").write_text(json.dumps(report, indent=1))
    prog(1.0, "done")
    return report

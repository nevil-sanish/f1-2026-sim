"""Driver layer. Ratings are SUBJECTIVE ESTIMATES (editable in the UI); they are optionally nudged from
public teammate qualifying head-to-head gaps by scripts/calibrate_presets.py (-> data/driver_ratings.json).
"""
from __future__ import annotations

import json

import numpy as np
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

SCHEMA = [
    {"key": "grip_use", "label": "Grip usage", "min": 0.95, "max": 1.0, "step": 0.001, "default": 0.985,
     "tip": "Share of the tyre's lateral limit the driver uses over a lap."},
    {"key": "brake_commit", "label": "Braking commitment", "min": 0.90, "max": 1.0, "step": 0.005, "default": 0.97,
     "tip": "Share of the maximum deceleration used (late, hard braking)."},
    {"key": "throttle_commit", "label": "Throttle commitment", "min": 0.90, "max": 1.0, "step": 0.005, "default": 0.97,
     "tip": "Share of the traction limit used on corner exit."},
    {"key": "consistency", "label": "Lap-time scatter (sigma)", "min": 0.05, "max": 0.6, "step": 0.01, "default": 0.22,
     "tip": "Standard deviation of race lap time in seconds. Lower is more consistent."},
    {"key": "tyre_management", "label": "Tyre management", "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.5,
     "tip": "Higher = less sliding energy, slower wear."},
    {"key": "racecraft", "label": "Racecraft", "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.5,
     "tip": "Overtaking and defending ability."},
    {"key": "wet_skill", "label": "Wet skill (stub)", "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.5,
     "tip": "Reserved for wet-weather modelling; not used yet."},
    {"key": "balance_preference", "label": "Balance preference", "min": -1.0, "max": 1.0, "step": 0.05, "default": 0.0,
     "tip": "-1 likes understeer (stable rear), +1 likes oversteer (pointy front)."},
]
DEFAULTS = {p["key"]: p["default"] for p in SCHEMA}

# (grip_use, brake_commit, throttle_commit, consistency, tyre_management, racecraft, wet, balance_pref)
# Subjective, provenance = estimate.
_R = {
    "max_verstappen": (0.998, 0.995, 0.990, 0.12, 0.80, 0.97, 0.97, 0.6),
    "russell": (0.994, 0.985, 0.985, 0.15, 0.72, 0.85, 0.85, 0.1),
    "leclerc": (0.996, 0.985, 0.985, 0.17, 0.66, 0.86, 0.80, 0.5),
    "norris": (0.995, 0.985, 0.985, 0.15, 0.78, 0.84, 0.86, 0.0),
    "piastri": (0.993, 0.985, 0.985, 0.14, 0.76, 0.86, 0.78, 0.0),
    "hamilton": (0.991, 0.985, 0.980, 0.15, 0.85, 0.93, 0.95, -0.2),
    "alonso": (0.992, 0.985, 0.985, 0.14, 0.84, 0.95, 0.92, 0.2),
    "antonelli": (0.991, 0.980, 0.980, 0.21, 0.62, 0.76, 0.75, 0.2),
    "sainz": (0.990, 0.980, 0.980, 0.16, 0.76, 0.85, 0.82, -0.2),
    "albon": (0.990, 0.980, 0.980, 0.17, 0.74, 0.82, 0.78, 0.2),
    "gasly": (0.989, 0.980, 0.980, 0.17, 0.70, 0.82, 0.82, 0.0),
    "hulkenberg": (0.988, 0.980, 0.980, 0.17, 0.70, 0.80, 0.86, 0.0),
    "hadjar": (0.988, 0.980, 0.975, 0.21, 0.62, 0.74, 0.72, 0.1),
    "bearman": (0.988, 0.980, 0.975, 0.22, 0.62, 0.78, 0.74, 0.1),
    "ocon": (0.986, 0.975, 0.975, 0.19, 0.70, 0.80, 0.84, -0.1),
    "lawson": (0.985, 0.975, 0.975, 0.21, 0.64, 0.80, 0.74, 0.0),
    "tsunoda": (0.986, 0.975, 0.975, 0.21, 0.62, 0.74, 0.74, 0.1),
    "bortoleto": (0.986, 0.975, 0.975, 0.22, 0.64, 0.74, 0.72, 0.0),
    "arvid_lindblad": (0.984, 0.975, 0.970, 0.25, 0.58, 0.72, 0.70, 0.1),
    "colapinto": (0.983, 0.975, 0.970, 0.25, 0.60, 0.76, 0.70, 0.0),
    "perez": (0.984, 0.975, 0.975, 0.20, 0.84, 0.82, 0.80, -0.4),
    "bottas": (0.985, 0.975, 0.975, 0.18, 0.74, 0.74, 0.78, -0.2),
    "stroll": (0.981, 0.970, 0.970, 0.23, 0.66, 0.74, 0.84, -0.1),
}
_KEYS = ["grip_use", "brake_commit", "throttle_commit", "consistency", "tyre_management", "racecraft",
         "wet_skill", "balance_preference"]


PACE_KEYS = ("grip_use", "brake_commit", "throttle_commit")
PACE_SCALE = 0.6


def _nudges() -> dict:
    try:
        return json.loads((DATA / "driver_ratings.json").read_text()).get("grip_use_nudge", {})
    except (OSError, ValueError):
        return {}


def get_driver(driver_id: str | None, overrides: dict | None = None) -> dict:
    d = dict(DEFAULTS)
    if driver_id in _R:
        d.update(dict(zip(_KEYS, _R[driver_id])))
        for k in PACE_KEYS:   # compress the spread: best vs worst driver on one car is ~0.2-0.45 s
            d[k] = DEFAULTS[k] + PACE_SCALE * (d[k] - DEFAULTS[k])
    d["grip_use"] = min(1.0, max(0.95, d["grip_use"] + _nudges().get(driver_id, 0.0)))
    for k, v in (overrides or {}).items():
        p = next((p for p in SCHEMA if p["key"] == k), None)
        if p is not None:
            try:
                d[k] = min(max(float(v), p["min"]), p["max"])
            except (TypeError, ValueError):
                pass
    d["id"] = driver_id
    return d


def driver_list() -> list[dict]:
    try:
        roster = json.loads((DATA / "drivers.json").read_text())
    except (OSError, ValueError):
        roster = [{"id": k, "code": k[:3].upper(), "name": k.replace("_", " ").title(), "team": None,
                   "number": 0, "current": True} for k in _R]
    nudges = _nudges()
    out = []
    for r in roster:
        g = get_driver(r["id"])
        out.append({**r, "ratings": {k: g[k] for k in _KEYS}, "rated": r["id"] in _R,
                    "ratings_prov": "estimate" + (" + public-derived teammate nudge" if r["id"] in nudges else "")})
    return out


NUDGE_CAP = 0.004          # largest grip_use change a teammate head-to-head may apply
NUDGE_WEIGHT = 0.5         # the subjective ratings already carry half of what the head-to-head says


def compute_nudges(pace_per_pct: float = 0.02, min_rounds: int = 4) -> dict:
    """Teammate qualifying head-to-head -> {driver_id: grip_use nudge}.

    For every dry round where both teammates set a time, the driver's gap to his teammate's best lap is taken in
    per cent; the mean gap (positive = faster) is mapped to grip_use at `pace_per_pct` per 1 % of lap time
    (~0.04 grip_use (used at half) is about 1 % lap time on the baseline car), halved and capped. Written to
    data/driver_ratings.json by f1sim.calibration.fit.run_calibration."""
    pub = DATA / "public" / "2026"
    gaps: dict[str, list[float]] = {}
    for rd in sorted(pub.glob("[0-9][0-9]")):
        try:
            q = json.loads((rd / "qualifying.json").read_text())
            rain = json.loads((rd / "tel_quali.json").read_text()).get("weather", {}).get("rain_frac", 0.0)
        except (OSError, ValueError):
            continue
        if rain > 0.2:
            continue
        by_team: dict[str, dict[str, float]] = {}
        for r in q:
            t = [x for x in (r.get("q1"), r.get("q2"), r.get("q3")) if x]
            if t:
                by_team.setdefault(r["team"], {})[r["driver"]] = min(t)
        for team, drv in by_team.items():
            if len(drv) != 2:
                continue
            (a, ta), (b, tb) = drv.items()
            if max(ta, tb) / min(ta, tb) > 1.03:   # a crash or a lap that did not count
                continue
            m = 0.5 * (ta + tb)
            gaps.setdefault(a, []).append(100.0 * (tb - ta) / m)
            gaps.setdefault(b, []).append(100.0 * (ta - tb) / m)
    out = {}
    for drv, g in gaps.items():
        if len(g) >= min_rounds:
            out[drv] = round(max(-NUDGE_CAP, min(NUDGE_CAP, NUDGE_WEIGHT * pace_per_pct * float(np.mean(g)))), 5)
    return out

"""Tyre strategy: event compound nomination, the two-compound rule, fixed plans and a simple optimiser.

A plan is `{"start_compound": "C3", "stops": [{"lap": 22, "compound": "C2"}], "time": est}` where `lap` is
the lap at the end of which the car pits (1-based, 1 <= lap <= laps-1).
"""
from __future__ import annotations

import itertools

import numpy as np

from ..car import tyres

LABELS = ("hard", "medium", "soft")
COMPOUND_NAMES = list(tyres.COMPOUND)           # C1 (hardest) .. C5 (softest)

# Index (into C1..C5) of the hardest of the three nominated compounds. ESTIMATE: follows the pattern of
# the supplier's recent nominations (abrasive / high-energy tracks get the hard end of the range, smooth
# street tracks the soft end); the real 2026 nomination can differ per event.
EVENT_HARDEST = {
    "suzuka": 0, "catalunya": 0, "bahrain": 0, "silverstone": 0, "lusail": 0, "zandvoort": 0, "sepang": 0,
    "shanghai": 1, "spa": 1, "hungaroring": 1, "cota": 1, "interlagos": 1, "jeddah": 1, "mexico": 1,
    "red_bull_ring": 1, "albert_park": 1, "miami": 1, "yas_marina": 1, "madring": 1,
    "monza": 2, "monaco": 2, "montreal": 2, "baku": 2, "marina_bay": 2, "las_vegas": 2,
}
OUTLAP_WARMUP = (1.1, 0.8, 0.5)    # s lost on the first lap of a stint, hard/medium/soft (estimate)
MIN_STINT_FRAC = 0.10              # shortest stint considered by the optimiser, share of race distance
MAX_PLAN_WEAR = 0.92               # plans that run a set past this wear are rejected (deep in the cliff)


def event_compounds(track_id: str, track: dict | None = None) -> list[str]:
    """Three consecutive compounds for the event, hardest first."""
    i = EVENT_HARDEST.get(track_id)
    if i is None:
        i = 2 if (track or {}).get("type") == "street" else 1
    return COMPOUND_NAMES[i:i + 3]


def label_of(compound: str, compounds: list[str]) -> str:
    return LABELS[compounds.index(compound)] if compound in compounds else tyres.COMPOUND[compound]["label"]


def plan_compounds(plan: dict) -> list[str]:
    return [plan["start_compound"]] + [s["compound"] for s in plan["stops"]]


def obeys_two_compound_rule(plan: dict) -> bool:
    return len(set(plan_compounds(plan))) >= 2


def _resolve(name, compounds: list[str]) -> str | None:
    """Accept 'C3', a label ('hard'/'medium'/'soft', 'H'/'M'/'S') or an index into the event compounds."""
    if isinstance(name, (int, np.integer)) and 0 <= int(name) < len(compounds):
        return compounds[int(name)]
    s = str(name).strip()
    if s.upper() in compounds:
        return s.upper()
    for lab, c in zip(LABELS, compounds):
        if s.lower() in (lab, lab[0]):
            return c
    return None


def fixed_plan(strategy: dict, compounds: list[str], laps: int) -> tuple[dict, list[str]]:
    """Validate a user plan against the event; returns (plan, warnings).

    Unknown compounds fall back to the event medium, stop laps are clamped into the race and, if the plan
    uses a single compound, a stop is added/changed so that the dry-race two-compound rule holds.
    """
    warn = []
    mid = compounds[len(compounds) // 2]

    def comp(v, what):
        c = _resolve(v, compounds)
        if c is None:
            warn.append(f"{what}: compound '{v}' is not nominated for this event, using {mid}")
            c = mid
        return c

    start = comp(strategy.get("start_compound", mid), "start")
    stops = []
    last_lap = 0
    for raw in sorted(strategy.get("stops") or [], key=lambda s: int(s.get("lap", 0))):
        lap = int(np.clip(int(raw.get("lap", 0)), 1, max(laps - 1, 1)))
        if lap <= last_lap or laps < 2:
            warn.append(f"stop on lap {raw.get('lap')} dropped (outside the race or duplicate)")
            continue
        stops.append({"lap": lap, "compound": comp(raw.get("compound", mid), f"stop lap {lap}")})
        last_lap = lap
    plan = {"start_compound": start, "stops": stops, "time": None}
    if laps >= 2 and not obeys_two_compound_rule(plan):
        other = next(c for c in compounds if c != start)
        if stops:
            stops[-1]["compound"] = other
            warn.append(f"last stop changed to {other}: two different dry compounds are mandatory")
        else:
            stops.append({"lap": max(1, laps // 2), "compound": other})
            warn.append(f"stop added on lap {stops[-1]['lap']} for {other}: two dry compounds are mandatory")
    return plan, warn


def candidates(surface, laps: int, pit_loss: float, fuel_start: float, max_stops: int = 2,
               keep: int = 6, window: float = 6.0) -> list[dict]:
    """Rank 1-stop and 2-stop plans by estimated race time; returns the best `keep` within `window` s.

    Race time = sum over stints of the degradation curve + pit losses. The fuel term is common to every
    plan (the surface is additive in fuel) so the stint curves are evaluated at the mean fuel load. For each
    compound order the pit laps are found by exhaustive search over the pit-lap window.
    """
    comps = surface.compounds
    nc = len(comps)
    f_avg = 0.5 * fuel_start
    cum = np.zeros((nc, laps + 1))                      # cum[c, n] = time of an n-lap stint on compound c
    for ci in range(nc):
        cum[ci, 1:] = np.cumsum(surface.stint_times(ci, laps, f_avg))
        cum[ci, 1:] += OUTLAP_WARMUP[min(ci, 2)]
        over = surface.wear_rate[ci] * np.arange(laps + 1) > MAX_PLAN_WEAR
        cum[ci, over] = np.inf
    if laps < 2:
        ci = int(np.argmin(cum[:, laps]))
        return [{"start_compound": comps[ci], "stops": [], "time": float(cum[ci, laps])}]
    m = max(1, min(int(round(MIN_STINT_FRAC * laps)), laps // (max_stops + 1)))
    out = []
    for a, b in itertools.permutations(range(nc), 2):
        n1 = np.arange(m, laps - m + 1)
        if not len(n1):
            continue
        cost = cum[a, n1] + cum[b, laps - n1] + pit_loss
        i = int(np.argmin(cost))
        if np.isfinite(cost[i]):
            out.append({"start_compound": comps[a], "stops": [{"lap": int(n1[i]), "compound": comps[b]}],
                        "time": float(cost[i])})
    if max_stops >= 2 and laps >= 3 * m:
        n1, n2 = np.meshgrid(np.arange(m, laps - 2 * m + 1), np.arange(m, laps - 2 * m + 1), indexing="ij")
        ok = n1 + n2 <= laps - m
        n3 = np.where(ok, laps - n1 - n2, 0)
        for a, b, c in itertools.product(range(nc), repeat=3):
            if a == b == c:
                continue
            cost = np.where(ok, cum[a, n1] + cum[b, n2] + cum[c, n3] + 2 * pit_loss, np.inf)
            i = np.unravel_index(int(np.argmin(cost)), cost.shape)
            if np.isfinite(cost[i]):
                out.append({"start_compound": comps[a], "time": float(cost[i]),
                            "stops": [{"lap": int(n1[i]), "compound": comps[b]},
                                      {"lap": int(n1[i] + n2[i]), "compound": comps[c]}]})
    if not out:
        # every stint length hits the wear limit (very high degradation): fall back to an even 2-stop
        h = comps[0]
        o = comps[1 if nc > 1 else 0]
        k = max(1, laps // 3)
        stops = [{"lap": k, "compound": o}] + ([{"lap": 2 * k, "compound": h}] if 2 * k < laps and k > 0 and laps >= 3 else [])
        return [{"start_compound": h, "stops": stops, "time": None}]
    out.sort(key=lambda p: p["time"])
    best = out[0]["time"]
    return [p for p in out if p["time"] - best <= window][:keep]


def optimise(surface, laps: int, pit_loss: float, fuel_start: float, max_stops: int = 2) -> dict:
    """Fastest plan for one car in clean air."""
    return candidates(surface, laps, pit_loss, fuel_start, max_stops, keep=1)[0]


def pick_plan(cands: list[dict], rng: np.random.Generator, laps: int, spread: float = 1.5) -> dict:
    """Pick one near-optimal plan (softmax over the estimated time loss) and jitter the pit laps by a lap.

    Teams do not all converge on the same plan: this gives the field a realistic strategy mix.
    """
    if len(cands) == 1 and cands[0]["time"] is None:
        return cands[0]
    t = np.array([p["time"] for p in cands])
    w = np.exp(-(t - t.min()) / spread)
    p = cands[int(rng.choice(len(cands), p=w / w.sum()))]
    stops, prev = [], 0
    for s in p["stops"]:
        lap = int(np.clip(s["lap"] + rng.integers(-1, 2), prev + 1, laps - 1))
        stops.append({"lap": lap, "compound": s["compound"]})
        prev = lap
    return {"start_compound": p["start_compound"], "stops": stops, "time": p["time"]}

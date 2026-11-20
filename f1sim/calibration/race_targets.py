"""Race parameters derived from cached public data -> data/race_params.json.

Run `python -m f1sim.calibration.race_targets` after `scripts/fetch_data.py`. Everything is computed from
the per-round caches in data/public/<season>/<NN>/ and tolerates missing rounds or files.

What is derived, and how (also written into the JSON under "method"):

pit-lane loss per track
    Public data gives the pit-lane time (entry line -> exit line, stationary time included), not the time
    lost. Conversion:  loss = median(lane time) - track_equivalent + PIT_BLEND_LOSS
      track_equivalent = time a racing car needs between the same two lines on the main straight
                       = pit span along the track / (PIT_STRAIGHT_SPEED_RATIO * mean race-lap speed)
      PIT_BLEND_LOSS   = braking down to the limiter before the entry line and accelerating from the
                         limiter on cold tyres after the exit line (estimate, 3 s)
    Lane times above 60 s (red-flag tyre changes, repairs) are excluded. The pit span uses the pit
    entry/exit positions of the track model, which are themselves estimates, so the result is
    "public-derived" with an estimated correction of a few seconds.

stationary time per team
    Mean and standard deviation of OpenF1 stop durations between 1.5 s and 6 s over all cached rounds;
    stops above 6 s are counted separately as the team's slow-stop probability.

overtaking difficulty per track (0 = trivial .. 1 = impossible)
    On-track passes are counted from the lap chart rebuilt from cumulative lap times: a pass is a pair of
    cars that swap order between two consecutive laps while neither is on an in/out lap, the loser is not
    on a damaged/off-track lap and the lap is green (lap 1 and SC/red-flag laps are skipped).
      difficulty_obs = clip(1 - ln(1 + N) / ln(1 + N_REF), 0.05, 0.98),  N_REF = 150 passes
    One race is a small sample (safety cars, grid penalties, weather), so the stored value is a 50/50
    blend of difficulty_obs and the catalogue estimate. Without lap data the proxy
    N = 0.75 * sum |grid - finish| over classified finishers is used instead. Wet races are skipped.

compounds per track
    Stint and lap counts per hard/medium/soft label, plus the median number of stops of the finishers.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent.parent / "data"
PUBLIC = DATA / "public"
OUT = DATA / "race_params.json"

PIT_BLEND_LOSS = 3.0               # s (estimate), see module docstring
PIT_STRAIGHT_SPEED_RATIO = 1.25    # pit-straight speed / mean lap speed (estimate)
LANE_TIME_MAX = 60.0               # s; longer "stops" are red flags or repairs
STOP_MIN, STOP_NORMAL_MAX, STOP_MAX = 1.5, 6.0, 40.0
N_REF = 150.0                      # passes per race that map to difficulty 0
BLEND = 0.5                        # weight of the observed difficulty vs the catalogue estimate
PROXY_FACTOR = 0.75
WET_FRAC = 0.3
DEFAULT_STATIONARY = {"mean": 2.6, "sd": 0.35, "slow_prob": 0.04}   # estimate, used without data
LABEL = {"SOFT": "soft", "MEDIUM": "medium", "HARD": "hard"}

METHOD = {
    "pit_loss_s": "median pit-lane time (<60 s) - pit span / (1.25 x mean race-lap speed) + 3.0 s blend loss "
                  "(limiter braking + cold-tyre acceleration, estimate). Includes a typical stationary time.",
    "stationary": "mean / sd of OpenF1 stop durations within 1.5..6 s per team; slow_prob = share of stops 6..40 s.",
    "overtake_difficulty": "passes N counted from the lap chart (order swaps on green laps, in/out laps and "
                           "damaged laps excluded); obs = clip(1 - ln(1+N)/ln(151), 0.05, 0.98); stored value = "
                           "0.5*obs + 0.5*catalogue estimate. Fallback N = 0.75 * sum|grid - finish|.",
    "compounds": "stints and laps per hard/medium/soft label from OpenF1 stint data.",
}


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):       # missing, or half-written by a fetch still in progress
        return default


def _season_dir() -> Path | None:
    seasons = sorted(p for p in PUBLIC.glob("[0-9][0-9][0-9][0-9]") if p.is_dir())
    return seasons[-1] if seasons else None


def _catalogue() -> dict:
    cat = _read(DATA / "track_catalog.json", {}) or {}
    return {t["id"]: t for t in cat.get("tracks", [])}


def _rounds(season: Path) -> list[tuple[str, Path]]:
    cal = {int(c.get("round", 0)): c.get("track") for c in (_read(season / "calendar.json", []) or [])}
    out = []
    for d in sorted(p for p in season.iterdir() if p.is_dir() and p.name.isdigit()):
        track = (_read(d / "meta.json", {}) or {}).get("track") or cal.get(int(d.name))
        if track:
            out.append((track, d))
    return out


def _race_drivers(tel: dict | None) -> dict:
    drv = (tel or {}).get("drivers")
    return drv if isinstance(drv, dict) else {}


def _median_green_lap(drivers: dict) -> float | None:
    laps = [t for d in drivers.values() for t in d.get("time", []) if t]
    if not laps:
        return None
    med = statistics.median(laps)
    green = [t for t in laps if t < 1.08 * med]
    return statistics.median(green) if green else med


def pit_lane_times(rd: Path, tel: dict | None) -> list[float]:
    lane = [p.get("lane_s") for p in (tel or {}).get("pit_lane", []) or []]
    if not any(lane):
        lane = [p.get("duration") for p in (_read(rd / "pitstops.json", []) or [])]
    return [float(x) for x in lane if x and 5.0 < float(x) < LANE_TIME_MAX]


def _pit_span(track_id: str) -> tuple[float | None, float | None]:
    """(pit entry->exit distance along the track, lap length); None when the geometry is unavailable."""
    try:
        from .. import tracks
        tr = tracks.load_track(track_id)
        pit = tr.get("pit") or {}
        length = float(tr["length"])
        span = (float(pit["exit_s"]) - float(pit["entry_s"])) % length
        return span, length
    except Exception:  # noqa: BLE001 - geometry not fetched yet
        return None, None


def pit_loss(lane: list[float], span: float | None, length: float | None, lap_time: float | None) -> dict | None:
    if len(lane) < 3 or not span or not length or not lap_time:
        return None
    med = statistics.median(lane)
    equiv = span / (PIT_STRAIGHT_SPEED_RATIO * length / lap_time)
    return {"pit_lane_time_s": round(med, 2), "pit_lane_n": len(lane), "pit_track_equiv_s": round(equiv, 2),
            "pit_loss_s": round(med - equiv + PIT_BLEND_LOSS, 2)}


def count_overtakes(drivers: dict) -> int | None:
    """Green-flag on-track passes from the lap chart rebuilt from cumulative lap times."""
    if len(drivers) < 6:
        return None
    cum, pit, slow = {}, {}, {}
    n_max = 0
    for code, d in drivers.items():
        times = d.get("time") or []
        own = [t for t in times if t]
        if len(own) < 3:
            continue
        med = statistics.median(own)
        total, c = 0.0, []
        for t in times:
            if not t:
                break                    # cumulative time is unknown from the first missing lap on
            total += t
            c.append(total)
        pin, pout = d.get("pit_in") or [], d.get("pit_out") or []
        cum[code] = c
        pit[code] = [bool(pin[i] if i < len(pin) else False) or bool(pout[i] if i < len(pout) else False)
                     for i in range(len(times))]
        slow[code] = [bool(t) and t > 1.12 * med for t in times]
        n_max = max(n_max, len(c))
    if len(cum) < 6:
        return None
    lap_med = []
    for i in range(n_max):
        ts = [c[i] - (c[i - 1] if i else 0.0) for c in cum.values() if len(c) > i]
        lap_med.append(statistics.median(ts) if ts else None)
    race_med = statistics.median([m for m in lap_med if m])
    passes = 0
    for i in range(1, n_max):            # i = 0-based index of the lap on which the swap happens
        if lap_med[i] is None or lap_med[i] > 1.12 * race_med or lap_med[i - 1] > 1.12 * race_med:
            continue
        codes = [k for k, c in cum.items() if len(c) > i and not pit[k][i] and not pit[k][i - 1]]
        for a_i, a in enumerate(codes):
            for b in codes[a_i + 1:]:
                before = cum[a][i - 1] - cum[b][i - 1]
                after = cum[a][i] - cum[b][i]
                if before * after >= 0 or abs(before) > 0.6 * race_med:   # no swap, or a lapping move
                    continue
                loser = a if after > 0 else b
                if not slow[loser][i]:
                    passes += 1
    return passes


def proxy_overtakes(results: list | None) -> float | None:
    rows = [r for r in (results or []) if r.get("grid") and r.get("pos")
            and str(r.get("status", "")).lower().startswith(("finished", "+", "lapped"))]
    if len(rows) < 6:
        return None
    return PROXY_FACTOR * sum(abs(int(r["grid"]) - int(r["pos"])) for r in rows)


def difficulty_from_overtakes(n: float) -> float:
    return min(max(1.0 - math.log1p(max(n, 0.0)) / math.log1p(N_REF), 0.05), 0.98)


def compound_usage(drivers: dict) -> dict:
    out = {k: {"stints": 0, "laps": 0} for k in ("hard", "medium", "soft")}
    for d in drivers.values():
        comp, stint = d.get("compound") or [], d.get("stint") or []
        seen = set()
        for i, c in enumerate(comp):
            lab = LABEL.get(str(c).upper())
            if lab is None:
                continue
            out[lab]["laps"] += 1
            key = (stint[i] if i < len(stint) else i, lab)
            if key not in seen:
                seen.add(key)
                out[lab]["stints"] += 1
    return out


def median_stops(rd: Path, results: list | None) -> float | None:
    stops = _read(rd / "pitstops.json", []) or []
    fin = {r["driver"] for r in (results or []) if str(r.get("status", "")).lower().startswith(("finished", "+", "lapped"))}
    per = {}
    for p in stops:
        if p.get("driver") in fin and p.get("duration") and float(p["duration"]) < LANE_TIME_MAX:
            per[p["driver"]] = per.get(p["driver"], 0) + 1
    return statistics.median([per.get(d, 0) for d in fin]) if fin else None


def derive() -> dict:
    """Build the race-parameter dict from whatever public data is cached."""
    cat = _catalogue()
    season = _season_dir()
    tracks_out, team_stops = {}, {}
    rounds = _rounds(season) if season else []
    for track_id, rd in rounds:
        tel = _read(rd / "tel_race.json")
        results = _read(rd / "results.json")
        drv = _race_drivers(tel)
        c = cat.get(track_id, {})
        row = {"round": int(rd.name), "catalogue": {"pit_loss_s": c.get("pit_loss_s"),
                                                    "overtake_difficulty": c.get("overtake_difficulty")}}
        lap_time = _median_green_lap(drv)
        if lap_time is None and results:
            fl = [r["fastest_lap"] for r in results if r.get("fastest_lap")]
            lap_time = 1.03 * statistics.median(fl) if fl else None
        span, length = _pit_span(track_id)
        pl = pit_loss(pit_lane_times(rd, tel), span, length, lap_time)
        if pl:
            row.update(pl)
            row["pit_loss_prov"] = "public-derived"
        elif c.get("pit_loss_s") is not None:
            row.update({"pit_loss_s": c["pit_loss_s"], "pit_loss_prov": "estimate"})

        wet = ((tel or {}).get("weather") or {}).get("rain_frac", 0.0) or 0.0
        n, how = (None, None) if wet > WET_FRAC else (count_overtakes(drv), "lap-chart")
        if n is None and wet <= WET_FRAC:
            n, how = proxy_overtakes(results), "grid-vs-finish proxy"
        est = c.get("overtake_difficulty")
        if n is not None:
            obs = difficulty_from_overtakes(n)
            row.update({"overtakes": round(float(n), 1), "overtakes_method": how,
                        "overtake_difficulty_obs": round(obs, 3),
                        "overtake_difficulty": round(obs if est is None else BLEND * obs + (1 - BLEND) * est, 3),
                        "overtake_prov": "public-derived" + ("" if est is None else " (50/50 blend with estimate)")})
        elif est is not None:
            row.update({"overtake_difficulty": est, "overtake_prov": "estimate" + (" (wet race skipped)" if wet > WET_FRAC else "")})
        if drv:
            row["compounds"] = compound_usage(drv)
        ms = median_stops(rd, results)
        if ms is not None:
            row["median_stops"] = ms
        tracks_out[track_id] = row
        for p in (tel or {}).get("pit_lane", []) or []:
            if p.get("team") and p.get("stop_s") and STOP_MIN <= float(p["stop_s"]) <= STOP_MAX:
                team_stops.setdefault(p["team"], []).append(float(p["stop_s"]))

    teams_out = {}
    all_normal = []
    for team, stops in sorted(team_stops.items()):
        normal = [s for s in stops if s <= STOP_NORMAL_MAX]
        all_normal += normal
        if len(normal) >= 5:
            teams_out[team] = {"stationary_mean": round(statistics.fmean(normal), 3),
                               "stationary_sd": round(statistics.pstdev(normal), 3),
                               "slow_prob": round(1.0 - len(normal) / len(stops), 3), "n": len(stops),
                               "prov": "public-derived"}
    ref = statistics.median(all_normal) if all_normal else DEFAULT_STATIONARY["mean"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "season": season.name if season else None,
        "rounds_used": [int(rd.name) for _, rd in rounds],
        "method": METHOD,
        "stationary_ref_s": round(ref, 3),
        "stationary_default": dict(DEFAULT_STATIONARY, prov="estimate"),
        "tracks": tracks_out,
        "teams": teams_out,
    }


_LOADED: dict = {"mtime": None, "data": None}


def load_race_params() -> dict:
    """data/race_params.json (re-read when the file changes); {} when it has not been generated."""
    try:
        mtime = OUT.stat().st_mtime
    except OSError:
        return {}
    if _LOADED["mtime"] != mtime:
        _LOADED.update(mtime=mtime, data=_read(OUT, {}) or {})
    return _LOADED["data"]


def track_race_params(track_id: str, track: dict | None = None) -> dict:
    """Pit loss and overtaking difficulty for a track: public-derived if available, else the catalogue."""
    cat = track if track is not None else _catalogue().get(track_id, {})
    out = {"pit_loss_s": float(cat.get("pit_loss_s", 21.0)), "pit_loss_prov": "estimate",
           "overtake_difficulty": float(cat.get("overtake_difficulty", 0.5)), "overtake_prov": "estimate"}
    row = (load_race_params().get("tracks") or {}).get(track_id) or {}
    if row.get("pit_loss_s") is not None:
        out.update(pit_loss_s=float(row["pit_loss_s"]), pit_loss_prov=row.get("pit_loss_prov", "public-derived"))
    if row.get("overtake_difficulty") is not None:
        out.update(overtake_difficulty=float(row["overtake_difficulty"]),
                   overtake_prov=row.get("overtake_prov", "public-derived"))
    return out


def team_pit_params(team: str | None) -> dict:
    """Stationary-time distribution of a team: {mean, sd, slow_prob, ref, prov}."""
    rp = load_race_params()
    out = dict(DEFAULT_STATIONARY, prov="estimate", ref=float(rp.get("stationary_ref_s", DEFAULT_STATIONARY["mean"])))
    row = (rp.get("teams") or {}).get(team or "")
    if row:
        out.update(mean=float(row["stationary_mean"]), sd=float(row["stationary_sd"]),
                   slow_prob=float(row.get("slow_prob", DEFAULT_STATIONARY["slow_prob"])), prov=row.get("prov", "public-derived"))
    return out


def main() -> dict:
    params = derive()
    OUT.write_text(json.dumps(params, indent=1))
    print(f"wrote {OUT}  ({len(params['tracks'])} tracks, {len(params['teams'])} teams)")
    for tid, r in params["tracks"].items():
        print(f"  {tid:14s} pit_loss {r.get('pit_loss_s')!s:>6} [{r.get('pit_loss_prov', '-')}]  "
              f"overtakes {r.get('overtakes')!s:>6} -> difficulty {r.get('overtake_difficulty')} [{r.get('overtake_prov', '-')}]")
    for team, r in params["teams"].items():
        print(f"  {team:14s} stationary {r['stationary_mean']:.2f} +- {r['stationary_sd']:.2f} s, slow {r['slow_prob']:.0%} (n={r['n']})")
    return params


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch all public data once and cache it under data/ so the simulator runs offline.

Sources (all public):
  * bacinger/f1-circuits      : centerline geometry + official length
  * TUMFTM/racetrack-database : ONLY mean track widths (coordinates are never mixed)
  * Jolpica F1 API            : calendar, standings, qualifying, results, pit stops
  * OpenF1 (default) / FastF1 : laps, sectors, speed traps, telemetry, tyres, weather
                                (FastF1 needs an F1 account in env F1_USERNAME/F1_PASSWORD; OpenF1 needs none)

Usage:
  python scripts/fetch_data.py                 # everything, skipping rounds already cached
  python scripts/fetch_data.py --rounds 1 2 3  # only these rounds
  python scripts/fetch_data.py --no-fastf1     # Jolpica + geometry only
  python scripts/fetch_data.py --force         # refetch
Needs: pip install -r requirements-fetch.txt   (never imported at simulator runtime)
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUB = DATA / "public"
YEAR = 2026
JOLPICA = f"https://api.jolpi.ca/ergast/f1/{YEAR}"
BACINGER = "https://raw.githubusercontent.com/bacinger/f1-circuits/master/f1-circuits.geojson"
TUM = "https://raw.githubusercontent.com/TUMFTM/racetrack-database/master/tracks/{name}.csv"

# Default entry list from the build brief (§5); verified against Jolpica below.
DEFAULT_TEAMS = [
    ("mercedes", "Mercedes", "Mercedes", ["russell", "antonelli"]),
    ("mclaren", "McLaren", "Mercedes", ["norris", "piastri"]),
    ("ferrari", "Ferrari", "Ferrari", ["leclerc", "hamilton"]),
    ("red_bull", "Red Bull", "Red Bull Ford", ["max_verstappen", "hadjar"]),
    ("racing_bulls", "Racing Bulls", "Red Bull Ford", ["lawson", "arvid_lindblad"]),
    ("aston_martin", "Aston Martin", "Honda", ["alonso", "stroll"]),
    ("alpine", "Alpine", "Mercedes", ["gasly", "colapinto"]),
    ("williams", "Williams", "Mercedes", ["albon", "sainz"]),
    ("haas", "Haas", "Ferrari", ["ocon", "bearman"]),
    ("audi", "Audi", "Audi", ["hulkenberg", "bortoleto"]),
    ("cadillac", "Cadillac", "Ferrari", ["perez", "bottas"]),
]
# Snapshot fallback (§3.4), only used for ordering if live standings can't be fetched.
STALE_ORDER = ["mercedes", "ferrari", "mclaren", "red_bull", "racing_bulls", "alpine",
               "haas", "audi", "williams", "aston_martin", "cadillac"]


def team_id(name: str) -> str:
    """Normalise a Jolpica constructorId or a FastF1 team name to our team id."""
    n = (name or "").lower().replace("-", " ").replace("_", " ")
    for key, tid in [("racing bulls", "racing_bulls"), ("rb", "racing_bulls"), ("red bull", "red_bull"),
                     ("mercedes", "mercedes"), ("mclaren", "mclaren"), ("ferrari", "ferrari"),
                     ("aston", "aston_martin"), ("alpine", "alpine"), ("williams", "williams"),
                     ("haas", "haas"), ("audi", "audi"), ("sauber", "audi"), ("cadillac", "cadillac")]:
        if key == "rb":
            if n.strip() in ("rb", "rb f1 team"):
                return tid
        elif key in n:
            return tid
    return n.replace(" ", "_")


def jget(path: str, params=None, tries=5):
    url = f"{JOLPICA}/{path}"
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "f1sim-local"})
            if r.status_code == 429:
                time.sleep(5 + 5 * i)
                continue
            r.raise_for_status()
            time.sleep(0.35)  # stay well under the public rate limit
            return r.json()["MRData"]
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"  retry {url}: {e}")
            time.sleep(2 + 2 * i)


def jget_all(path: str, table: str, key: str):
    """Paginated fetch; returns concatenated list found at MRData[table][key] (merging Races)."""
    out, offset = [], 0
    while True:
        d = jget(path, {"limit": 100, "offset": offset})
        items = d[table][key]
        out.extend(items)
        offset += 100
        if offset >= int(d["total"]):
            return out


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), default=str))


def tsec(v):
    """'1:27.470' or pandas Timedelta -> seconds (float) or None."""
    if v is None:
        return None
    if hasattr(v, "total_seconds"):
        s = v.total_seconds()
        return None if s != s else round(float(s), 3)
    v = str(v).strip()
    if not v:
        return None
    try:
        parts = v.split(":")
        s = float(parts[-1])
        if len(parts) > 1:
            s += 60 * float(parts[-2])
        if len(parts) > 2:
            s += 3600 * float(parts[-3])
        return round(s, 3)
    except ValueError:
        return None


def fnum(v):
    try:
        f = float(v)
        return None if f != f else round(f, 3)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- geometry
def fetch_geometry(catalog, sources):
    print("== Track geometry (bacinger/f1-circuits)")
    gj = requests.get(BACINGER, timeout=60).json()
    save(DATA / "geojson" / "f1-circuits.geojson", gj)
    sources.append(BACINGER)
    have = {f["properties"]["id"] for f in gj["features"]}
    for t in catalog:
        if t["bacinger"] not in have:
            print(f"  !! {t['id']}: bacinger id {t['bacinger']} MISSING -> will fall back to FastF1 X/Y centerline")
    print("== Track widths (TUMFTM/racetrack-database, widths only)")
    widths = {}
    for t in catalog:
        if not t.get("tum"):
            continue
        try:
            txt = requests.get(TUM.format(name=t["tum"]), timeout=30).text
            arr = np.loadtxt(io.StringIO(txt), delimiter=",", comments="#")
            # columns: x_m, y_m, w_tr_right_m, w_tr_left_m -> we keep only the mean total width
            widths[t["id"]] = round(float(np.mean(arr[:, 2] + arr[:, 3])), 2)
        except Exception as e:  # noqa: BLE001
            print(f"  width {t['id']}: failed ({e})")
    save(DATA / "tum_widths.json", {"_source": "TUMFTM/racetrack-database (LGPL-3.0), mean of w_right+w_left", "widths": widths})
    sources.append("https://github.com/TUMFTM/racetrack-database")
    print(f"  widths for {len(widths)} tracks")


# --------------------------------------------------------------------------- jolpica
def fetch_jolpica(catalog, sources, rounds_filter, force):
    print("== Jolpica: calendar / standings / entry list")
    sources.append(JOLPICA)
    races = jget_all("races.json", "RaceTable", "Races")
    cal = [{"round": int(r["round"]), "name": r["raceName"], "date": r["date"],
            "circuit": r["Circuit"]["circuitId"], "circuit_name": r["Circuit"]["circuitName"]} for r in races]
    by_jol = {t["jolpica"]: t["id"] for t in catalog}
    for c in cal:
        c["track"] = by_jol.get(c["circuit"])
        if c["track"] is None:
            print(f"  !! calendar round {c['round']} circuit '{c['circuit']}' is not in data/track_catalog.json")
    save(PUB / str(YEAR) / "calendar.json", cal)
    # Mismatch report vs the default list in the brief.
    default = ["albert_park", "shanghai", "suzuka", "miami", "montreal", "monaco", "catalunya", "red_bull_ring",
               "silverstone", "spa", "hungaroring", "zandvoort", "monza", "madring", "baku", "marina_bay", "cota",
               "mexico", "interlagos", "las_vegas", "lusail", "yas_marina"]
    live = [c["track"] or c["circuit"] for c in cal]
    extra = [x for x in live if x not in default]
    missing = [x for x in default if x not in live]
    print(f"  live calendar: {len(cal)} rounds. Not in default list: {extra or 'none'}. In default but not live: {missing or 'none'}")

    def standings(kind, key):
        d = jget(f"{kind}.json", {"limit": 100})
        lists = d["StandingsTable"]["StandingsLists"]
        return (lists[0][key], int(lists[0]["round"])) if lists else ([], 0)

    cs, cs_round = standings("constructorstandings", "ConstructorStandings")
    ds, _ = standings("driverstandings", "DriverStandings")
    save(PUB / str(YEAR) / "constructor_standings.json", {
        "after_round": cs_round,
        "standings": [{"pos": int(c["position"]), "team": team_id(c["Constructor"]["constructorId"]),
                       "points": float(c["points"]), "wins": int(c["wins"])} for c in cs]})
    save(PUB / str(YEAR) / "driver_standings.json", {
        "after_round": cs_round,
        "standings": [{"pos": int(d.get("position", 99)), "driver": d["Driver"]["driverId"],
                       "code": d["Driver"].get("code"), "points": float(d["points"]), "wins": int(d["wins"]),
                       "team": team_id(d["Constructors"][-1]["constructorId"]) if d["Constructors"] else None}
                      for d in ds]})
    print(f"  standings after round {cs_round}: " + ", ".join(team_id(c["Constructor"]["constructorId"]) for c in cs))

    today = dt.date.today().isoformat()
    done = [c for c in cal if c["date"] <= today]
    per_round_starts = {}
    for c in done:
        rnd = c["round"]
        if rounds_filter and rnd not in rounds_filter:
            continue
        rd = PUB / str(YEAR) / f"{rnd:02d}"
        if (rd / "results.json").exists() and not force:
            continue
        try:
            q = jget(f"{rnd}/qualifying.json", {"limit": 100})["RaceTable"]["Races"]
            r = jget(f"{rnd}/results.json", {"limit": 100})["RaceTable"]["Races"]
            if not r:
                print(f"  round {rnd}: no results yet")
                continue
            p = jget_all(f"{rnd}/pitstops.json", "RaceTable", "Races")
            quali = [{"pos": int(x["position"]), "driver": x["Driver"]["driverId"], "code": x["Driver"].get("code"),
                      "number": x["Driver"].get("permanentNumber"), "team": team_id(x["Constructor"]["constructorId"]),
                      "q1": tsec(x.get("Q1")), "q2": tsec(x.get("Q2")), "q3": tsec(x.get("Q3"))}
                     for x in (q[0]["QualifyingResults"] if q else [])]
            res = [{"pos": int(x["position"]), "grid": int(x["grid"]), "driver": x["Driver"]["driverId"],
                    "code": x["Driver"].get("code"), "team": team_id(x["Constructor"]["constructorId"]),
                    "laps": int(x["laps"]), "status": x["status"], "points": float(x["points"]),
                    "time_ms": int(x["Time"]["millis"]) if "Time" in x and "millis" in x["Time"] else None,
                    "fastest_lap": tsec(x.get("FastestLap", {}).get("Time", {}).get("time"))}
                   for x in r[0]["Results"]]
            pits = []
            for race in p:
                for x in race.get("PitStops", []):
                    pits.append({"driver": x["driverId"], "lap": int(x["lap"]), "stop": int(x["stop"]),
                                 "duration": tsec(x.get("duration"))})
            save(rd / "qualifying.json", quali)
            save(rd / "results.json", res)
            save(rd / "pitstops.json", pits)
            save(rd / "meta.json", c)
            print(f"  round {rnd:2d} {c['name']}: quali {len(quali)}, results {len(res)}, pit stops {len(pits)}")
        except Exception as e:  # noqa: BLE001
            print(f"  round {rnd}: Jolpica failed ({e})")

    # Entry list: who drove for whom, with race starts, from cached results.
    drivers_meta, team_drivers = {}, {}
    for c in done:
        f = PUB / str(YEAR) / f"{c['round']:02d}" / "results.json"
        if not f.exists():
            continue
        for x in json.loads(f.read_text()):
            per_round_starts.setdefault((x["team"], x["driver"]), []).append(c["round"])
    try:
        for d in jget_all("drivers.json", "DriverTable", "Drivers"):
            drivers_meta[d["driverId"]] = {"id": d["driverId"], "code": d.get("code") or d["familyName"][:3].upper(),
                                           "number": int(d.get("permanentNumber") or 0),
                                           "name": f"{d['givenName']} {d['familyName']}",
                                           "nationality": d.get("nationality")}
    except Exception as e:  # noqa: BLE001
        print(f"  drivers list failed ({e})")
    for (team, drv), rnds in per_round_starts.items():
        team_drivers.setdefault(team, []).append((drv, len(rnds), max(rnds)))
    build_entry_list(drivers_meta, team_drivers)
    return cal


def build_entry_list(drivers_meta, team_drivers):
    teams, drivers = [], []
    for tid, name, pu, default_drivers in DEFAULT_TEAMS:
        seen = team_drivers.get(tid)
        if seen:
            # current race drivers = the two with the most recent appearances; others are listed as reserves
            seen_sorted = sorted(seen, key=lambda s: (-s[2], -s[1]))
            current = [s[0] for s in seen_sorted[:2]]
            others = [s[0] for s in seen_sorted[2:]]
            if set(current) != set(default_drivers):
                print(f"  entry list: {tid} live drivers {current} differ from brief default {default_drivers}")
        else:
            current, others = default_drivers, []
        teams.append({"id": tid, "name": name, "pu": pu, "drivers": current, "other_drivers": others})
        for d in current + others:
            m = drivers_meta.get(d, {"id": d, "code": d[:3].upper(), "number": 0, "name": d.replace("_", " ").title()})
            drivers.append({**m, "team": tid, "current": d in current,
                            "starts": next((s[1] for s in (seen or []) if s[0] == d), 0)})
    save(DATA / "teams.json", teams)
    save(DATA / "drivers.json", drivers)
    print(f"  entry list: {len(teams)} teams, {len(drivers)} drivers -> data/teams.json, data/drivers.json")


# --------------------------------------------------------------------------- fastf1
def fetch_fastf1(cal, rounds_filter, force, sources):
    try:
        import fastf1
    except ImportError:
        print("== FastF1 not installed (pip install -r requirements-fetch.txt); skipping telemetry")
        return
    import logging
    logging.getLogger("fastf1").setLevel(logging.ERROR)
    cache = DATA / ".fastf1_cache"
    cache.mkdir(exist_ok=True)
    fastf1.Cache.enable_cache(str(cache))
    sources.append(f"FastF1 {fastf1.__version__} (F1 live timing)")
    today = dt.date.today().isoformat()
    print("== FastF1: laps, speed traps, telemetry, tyres, weather")
    for c in cal:
        rnd = c["round"]
        if c["date"] > today or (rounds_filter and rnd not in rounds_filter):
            continue
        rd = PUB / str(YEAR) / f"{rnd:02d}"
        rd.mkdir(parents=True, exist_ok=True)
        if not (rd / "tel_quali.json").exists() or force:
            try:
                t0 = time.time()
                s = fastf1.get_session(YEAR, rnd, "Q")
                s.load(laps=True, telemetry=True, weather=True, messages=False)
                _save_quali(s, rd)
                print(f"  round {rnd:2d} Q ok ({time.time() - t0:.0f}s)")
            except Exception as e:  # noqa: BLE001
                print(f"  round {rnd:2d} Q failed: {type(e).__name__}: {e}")
        if not (rd / "tel_race.json").exists() or force:
            try:
                t0 = time.time()
                s = fastf1.get_session(YEAR, rnd, "R")
                s.load(laps=True, telemetry=False, weather=True, messages=False)
                _save_race(s, rd)
                print(f"  round {rnd:2d} R ok ({time.time() - t0:.0f}s)")
            except Exception as e:  # noqa: BLE001
                print(f"  round {rnd:2d} R failed: {type(e).__name__}: {e}")


def _weather(s):
    try:
        w = s.weather_data
        return {"air_temp": fnum(w["AirTemp"].mean()), "track_temp": fnum(w["TrackTemp"].mean()),
                "rain_frac": fnum(w["Rainfall"].astype(float).mean()), "pressure": fnum(w["Pressure"].mean())}
    except Exception:  # noqa: BLE001
        return {}


def _save_quali(s, rd: Path):
    laps = s.laps
    rows, tel_arrays = [], {}
    best_by_team = {}
    for drv in laps["Driver"].dropna().unique():
        dl = laps[laps["Driver"] == drv]
        try:
            fast = dl.pick_fastest()
        except Exception:  # noqa: BLE001
            fast = None
        if fast is None or fast["LapTime"] != fast["LapTime"]:
            continue
        team = team_id(str(fast["Team"]))
        row = {"code": drv, "team": team, "lap": tsec(fast["LapTime"]),
               "s1": tsec(fast["Sector1Time"]), "s2": tsec(fast["Sector2Time"]), "s3": tsec(fast["Sector3Time"]),
               "speed_i1": fnum(fast["SpeedI1"]), "speed_i2": fnum(fast["SpeedI2"]),
               "speed_fl": fnum(fast["SpeedFL"]), "speed_st": fnum(fast["SpeedST"]),
               "speed_st_max": fnum(dl["SpeedST"].max()),
               "compound": str(fast["Compound"]), "tyre_life": fnum(fast["TyreLife"])}
        rows.append(row)
        if team not in best_by_team or row["lap"] < best_by_team[team][0]:
            best_by_team[team] = (row["lap"], drv, fast)
    # Telemetry for each team's best lap (keeps the cache small but gives every car's speed trace).
    for team, (_, drv, fast) in best_by_team.items():
        try:
            tel = fast.get_telemetry()
            cols = {"dist": "Distance", "speed": "Speed", "throttle": "Throttle", "brake": "Brake",
                    "gear": "nGear", "rpm": "RPM", "drs": "DRS", "x": "X", "y": "Y", "z": "Z"}
            for k, col in cols.items():
                if col in tel:
                    a = tel[col].to_numpy()
                    tel_arrays[f"{team}__{k}"] = a.astype(np.float32)
            tel_arrays[f"{team}__t"] = (tel["Time"].dt.total_seconds().to_numpy()).astype(np.float32)
            for r in rows:
                if r["code"] == drv:
                    r["has_tel"] = True
                    r["vmax_tel"] = fnum(np.nanmax(tel["Speed"].to_numpy()))
        except Exception as e:  # noqa: BLE001
            print(f"    telemetry {team}/{drv} failed: {e}")
    rows.sort(key=lambda r: r["lap"])
    save(rd / "tel_quali.json", {"source": "fastf1", "weather": _weather(s), "laps": rows})
    if tel_arrays:
        np.savez_compressed(rd / "tel_quali.npz", **tel_arrays)


def _save_race(s, rd: Path):
    laps = s.laps
    out = {}
    for drv in laps["Driver"].dropna().unique():
        dl = laps[laps["Driver"] == drv].sort_values("LapNumber")
        if dl.empty:
            continue
        out[drv] = {
            "team": team_id(str(dl["Team"].iloc[0])),
            "lap": [int(x) for x in dl["LapNumber"]],
            "time": [tsec(x) for x in dl["LapTime"]],
            "compound": [str(x) for x in dl["Compound"]],
            "tyre_life": [fnum(x) for x in dl["TyreLife"]],
            "stint": [fnum(x) for x in dl["Stint"]],
            "pit_in": [tsec(x) is not None for x in dl["PitInTime"]],
            "pit_out": [tsec(x) is not None for x in dl["PitOutTime"]],
            "status": [str(x) for x in dl["TrackStatus"]],
            "speed_st": [fnum(x) for x in dl["SpeedST"]],
            "pos": [fnum(x) for x in dl["Position"]],
        }
    save(rd / "tel_race.json", {"source": "fastf1", "weather": _weather(s), "total_laps": int(getattr(s, "total_laps", 0) or 0),
                                "drivers": out})



# --------------------------------------------------------------------------- openf1
OPENF1 = "https://api.openf1.org/v1"


def oget(path: str, query: str = "", tries=6):
    """OpenF1 GET. `query` is a raw query string because OpenF1 uses 'date>=...' style filters."""
    url = f"{OPENF1}/{path}?{query}"
    for i in range(tries):
        try:
            r = requests.get(url, timeout=90, headers={"User-Agent": "f1sim-local"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(8 + 8 * i)
                continue
            if r.status_code == 404:
                return []
            r.raise_for_status()
            time.sleep(0.45)
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(3 + 3 * i)
    raise RuntimeError(f"OpenF1 rate-limited: {url}")


def fetch_openf1(cal, rounds_filter, force, sources):
    """Timing + telemetry from OpenF1 (public, no login). Same output schema as the FastF1 path."""
    print("== OpenF1: laps, speed traps, telemetry, tyres, pit lane times, weather")
    sources.append(OPENF1)
    sessions = oget("sessions", f"year={YEAR}")
    today = dt.date.today().isoformat()
    for c in cal:
        rnd = c["round"]
        if c["date"] > today or (rounds_filter and rnd not in rounds_filter):
            continue
        rd = PUB / str(YEAR) / f"{rnd:02d}"
        rd.mkdir(parents=True, exist_ok=True)
        race_date = dt.date.fromisoformat(c["date"])
        race = [s for s in sessions if s["session_name"] == "Race" and not s.get("is_cancelled")
                and abs((dt.datetime.fromisoformat(s["date_start"]).date() - race_date).days) <= 1]
        if not race:
            print(f"  round {rnd:2d}: no OpenF1 race session found")
            continue
        mk = race[0]["meeting_key"]
        quali = [s for s in sessions if s["session_name"] == "Qualifying" and s["meeting_key"] == mk]
        if quali and (not (rd / "tel_quali.json").exists() or force):
            try:
                t0 = time.time()
                _openf1_quali(quali[0]["session_key"], rd)
                print(f"  round {rnd:2d} Q ok ({time.time() - t0:.0f}s)")
            except Exception as e:  # noqa: BLE001
                print(f"  round {rnd:2d} Q failed: {type(e).__name__}: {e}")
        if not (rd / "tel_race.json").exists() or force:
            try:
                t0 = time.time()
                _openf1_race(race[0]["session_key"], rd)
                print(f"  round {rnd:2d} R ok ({time.time() - t0:.0f}s)")
            except Exception as e:  # noqa: BLE001
                print(f"  round {rnd:2d} R failed: {type(e).__name__}: {e}")


def _o_weather(sk):
    w = oget("weather", f"session_key={sk}")
    if not w:
        return {}
    m = lambda k: fnum(np.mean([x[k] for x in w if x.get(k) is not None]))  # noqa: E731
    return {"air_temp": m("air_temperature"), "track_temp": m("track_temperature"),
            "rain_frac": m("rainfall"), "pressure": m("pressure")}


def _o_drivers(sk):
    return {d["driver_number"]: (d.get("name_acronym") or str(d["driver_number"]), team_id(d.get("team_name") or ""))
            for d in oget("drivers", f"session_key={sk}")}


def _o_stint_lookup(sk):
    """(driver_number, lap) -> (compound, tyre_life, stint_number)"""
    out = {}
    for s in oget("stints", f"session_key={sk}"):
        if s.get("lap_start") is None or s.get("lap_end") is None:
            continue
        for lap in range(s["lap_start"], s["lap_end"] + 1):
            age = (s.get("tyre_age_at_start") or 0) + lap - s["lap_start"] + 1
            out[(s["driver_number"], lap)] = (str(s.get("compound")), age, s.get("stint_number"))
    return out


def _openf1_quali(sk, rd: Path):
    drivers = _o_drivers(sk)
    stints = _o_stint_lookup(sk)
    laps = oget("laps", f"session_key={sk}")
    rows, best_by_team, tel_arrays = [], {}, {}
    by_drv = {}
    for l in laps:
        by_drv.setdefault(l["driver_number"], []).append(l)
    for num, dl in by_drv.items():
        code, team = drivers.get(num, (str(num), "unknown"))
        good = [l for l in dl if l.get("lap_duration") and not l.get("is_pit_out_lap")
                and l.get("duration_sector_1") and l.get("duration_sector_2") and l.get("duration_sector_3")]
        if not good:
            continue
        fast = min(good, key=lambda l: l["lap_duration"])
        comp, life, _ = stints.get((num, fast["lap_number"]), ("UNKNOWN", None, None))
        st = [l["st_speed"] for l in dl if l.get("st_speed")]
        row = {"code": code, "team": team, "lap": fnum(fast["lap_duration"]),
               "s1": fnum(fast["duration_sector_1"]), "s2": fnum(fast["duration_sector_2"]),
               "s3": fnum(fast["duration_sector_3"]),
               "speed_i1": fnum(fast.get("i1_speed")), "speed_i2": fnum(fast.get("i2_speed")),
               "speed_fl": None, "speed_st": fnum(fast.get("st_speed")),
               "speed_st_max": fnum(max(st)) if st else None, "compound": comp, "tyre_life": life}
        rows.append(row)
        if team not in best_by_team or row["lap"] < best_by_team[team][0]["lap"]:
            best_by_team[team] = (row, num, fast)
    for team, (row, num, fast) in best_by_team.items():
        try:
            t0 = dt.datetime.fromisoformat(fast["date_start"])
            t1 = t0 + dt.timedelta(seconds=fast["lap_duration"])
            rng = f"session_key={sk}&driver_number={num}&date>={t0.isoformat().replace('+00:00', 'Z')}&date<={t1.isoformat().replace('+00:00', 'Z')}"
            car = oget("car_data", rng)
            loc = oget("location", rng)
            if len(car) < 50:
                continue
            ts = lambda x: (dt.datetime.fromisoformat(x["date"]) - t0).total_seconds()  # noqa: E731
            t = np.array([ts(x) for x in car])
            order = np.argsort(t)
            t = t[order]
            col = lambda k: np.array([float(x.get(k) or 0) for x in car])[order]  # noqa: E731
            speed = col("speed")
            # distance by integrating speed (km/h -> m/s) over time
            dist = np.concatenate([[0.0], np.cumsum(0.5 * (speed[1:] + speed[:-1]) / 3.6 * np.diff(t))])
            arr = {"t": t, "dist": dist, "speed": speed, "throttle": col("throttle"), "brake": col("brake"),
                   "gear": col("n_gear"), "rpm": col("rpm"), "drs": col("drs")}
            if len(loc) > 20:
                tl = np.array([ts(x) for x in loc])
                o2 = np.argsort(tl)
                for k in ("x", "y", "z"):
                    arr[k] = np.interp(t, tl[o2], np.array([float(x.get(k) or 0) for x in loc])[o2])
            for k, a in arr.items():
                tel_arrays[f"{team}__{k}"] = a.astype(np.float32)
            row["has_tel"] = True
            row["vmax_tel"] = fnum(speed.max())
        except Exception as e:  # noqa: BLE001
            print(f"    telemetry {team} failed: {e}")
    rows.sort(key=lambda r: r["lap"])
    save(rd / "tel_quali.json", {"source": "openf1", "weather": _o_weather(sk), "laps": rows})
    if tel_arrays:
        np.savez_compressed(rd / "tel_quali.npz", **tel_arrays)


def _openf1_race(sk, rd: Path):
    drivers = _o_drivers(sk)
    stints = _o_stint_lookup(sk)
    laps = oget("laps", f"session_key={sk}")
    pits = oget("pit", f"session_key={sk}")
    pit_laps = {(p["driver_number"], p["lap_number"]) for p in pits}
    out = {}
    for l in sorted(laps, key=lambda l: (l["driver_number"], l["lap_number"])):
        num = l["driver_number"]
        code, team = drivers.get(num, (str(num), "unknown"))
        d = out.setdefault(code, {"team": team, "lap": [], "time": [], "compound": [], "tyre_life": [], "stint": [],
                                  "pit_in": [], "pit_out": [], "speed_st": []})
        comp, life, stint = stints.get((num, l["lap_number"]), ("UNKNOWN", None, None))
        d["lap"].append(l["lap_number"])
        d["time"].append(fnum(l.get("lap_duration")))
        d["compound"].append(comp)
        d["tyre_life"].append(life)
        d["stint"].append(stint)
        d["pit_in"].append((num, l["lap_number"]) in pit_laps)
        d["pit_out"].append(bool(l.get("is_pit_out_lap")))
        d["speed_st"].append(fnum(l.get("st_speed")))
    lane = [{"code": drivers.get(p["driver_number"], (str(p["driver_number"]), ""))[0],
             "team": drivers.get(p["driver_number"], ("", "unknown"))[1], "lap": p["lap_number"],
             "lane_s": fnum(p.get("lane_duration") or p.get("pit_duration")), "stop_s": fnum(p.get("stop_duration"))}
            for p in pits]
    total = max((max(d["lap"]) for d in out.values() if d["lap"]), default=0)
    save(rd / "tel_race.json", {"source": "openf1", "weather": _o_weather(sk), "total_laps": total,
                                "drivers": out, "pit_lane": lane})


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, nargs="*", help="only these round numbers")
    ap.add_argument("--no-fastf1", action="store_true", help="skip timing/telemetry (OpenF1/FastF1)")
    ap.add_argument("--source", choices=["auto", "openf1", "fastf1"], default="auto")
    ap.add_argument("--no-geometry", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    catalog = json.loads((DATA / "track_catalog.json").read_text())["tracks"]
    sources, status = [], {"jolpica": "ok", "geometry": "ok", "fastf1": "ok"}
    if not a.no_geometry:
        try:
            fetch_geometry(catalog, sources)
        except Exception as e:  # noqa: BLE001
            status["geometry"] = f"failed: {e}"
            print(f"!! geometry fetch failed: {e}")
    cal = None
    try:
        cal = fetch_jolpica(catalog, sources, a.rounds, a.force)
    except Exception as e:  # noqa: BLE001
        status["jolpica"] = f"failed: {e}"
        print(f"!! Jolpica fetch failed: {e}")
        if not (DATA / "teams.json").exists():
            build_entry_list({}, {})
        if not (PUB / str(YEAR) / "constructor_standings.json").exists():
            save(PUB / str(YEAR) / "constructor_standings.json", {
                "after_round": 0, "stale_snapshot": True,
                "standings": [{"pos": i + 1, "team": t, "points": None, "wins": None} for i, t in enumerate(STALE_ORDER)]})
            print("   using STALE SNAPSHOT constructor order (label shown in UI)")
    if not a.no_fastf1:
        if cal is None and (PUB / str(YEAR) / "calendar.json").exists():
            cal = json.loads((PUB / str(YEAR) / "calendar.json").read_text())
        if cal:
            # F1's live-timing archive (FastF1's upstream) needs an F1 account since 2026. If you export
            # F1_USERNAME / F1_PASSWORD yourself FastF1 is used; otherwise the public OpenF1 API is used.
            import os
            use_ff1 = a.source == "fastf1" or (a.source == "auto" and os.environ.get("F1_USERNAME"))
            try:
                if use_ff1:
                    fetch_fastf1(cal, a.rounds, a.force, sources)
                    status["fastf1"] = "ok (fastf1)"
                else:
                    fetch_openf1(cal, a.rounds, a.force, sources)
                    status["fastf1"] = "ok (openf1)"
            except Exception as e:  # noqa: BLE001
                status["fastf1"] = f"failed: {e}"
                print(f"!! telemetry fetch failed: {e}")
    else:
        status["fastf1"] = "skipped"
    save(PUB / "fetched_at.json", {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                   "year": YEAR, "sources": sources, "status": status})
    print("== done. Next: python scripts/calibrate_presets.py")


if __name__ == "__main__":
    sys.exit(main())

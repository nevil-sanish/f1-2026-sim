"""Local HTTP server (stdlib only): JSON API from docs/API.md §1 + static files from web/.

The API logic lives in plain functions (simulate, compare, tracks_list, ...) so tests and other modules
can call it without HTTP; Handler only does routing, JSON encoding and static files.
"""
from __future__ import annotations

import gzip
import importlib
import json
import math
import re
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import numpy as np

from . import drivers, setups, solver, suitability, tracks
from .car import params
from .jobs import JOBS

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
PUB = DATA / "public"
G = 9.81
MAX_BODY = 2 * 1024 * 1024       # request body limit [bytes]
MAX_COMPARE = 8
MAX_ENTRIES = 22
MAX_MONTE_CARLO = 2000
PUBLIC_MAX_MONTE_CARLO = 100  # shared free-tier CPU
SIM_CACHE_SIZE = 32
CORNER_PAD_M = 30.0              # search this far either side of a corner's i0..i1 for its minimum speed

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon", ".wasm": "application/wasm",
    ".hdr": "image/vnd.radiance", ".exr": "image/x-exr", ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json", ".bin": "application/octet-stream", ".ktx2": "image/ktx2",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
LIVERY_REGIONS = ("nose", "engine_cover", "sidepods", "front_wing", "rear_wing", "endplates", "halo", "floor",
                  "wheel_covers")
NEUTRAL_LIVERY = {
    "base": "#8a8f98", "secondary": "#2b2e33", "accent": "#d0d3d8", "finish": "satin",
    "regions": {"nose": "base", "engine_cover": "base", "sidepods": "secondary", "front_wing": "secondary",
                "rear_wing": "accent", "endplates": "accent", "halo": "carbon", "floor": "carbon",
                "wheel_covers": "carbon"},
    "numbers": {}, "number_color": "#ffffff",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------------------------- JSON helpers
def _json_file(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _arr(a, nd: int) -> list:
    """numpy array -> list of floats rounded to nd decimals (non-finite -> 0)."""
    return np.round(np.nan_to_num(np.asarray(a, dtype=float), nan=0.0, posinf=0.0, neginf=0.0), nd).tolist()


def _ints(a) -> list:
    return np.asarray(a).astype(int).tolist()


def clean(obj, nd: int = 4):
    """Recursively convert numpy types to JSON-safe Python (floats rounded to nd, non-finite -> None).
    Used for results produced by other modules (race, optimiser, calibration)."""
    if isinstance(obj, dict):
        return {str(k): clean(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v, nd) for v in obj]
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in "iub":
            return obj.tolist()
        if obj.dtype.kind == "f":
            a = np.round(obj, nd)
            return a.tolist() if np.isfinite(a).all() else [clean(v, nd) for v in a.tolist()]
        return [clean(v, nd) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return round(f, nd) if math.isfinite(f) else None
    if obj is None or isinstance(obj, str):
        return obj
    return str(obj)


def lap_time_str(t: float) -> str:
    m, s = divmod(float(t), 60.0)
    return f"{int(m)}:{s:06.3f}"


# --------------------------------------------------------------------------------------------- lookups
def _teams() -> list[dict]:
    return _json_file(DATA / "teams.json", []) or []


def _team_ids() -> set:
    return {t["id"] for t in _teams()}


def _check_team(team):
    if team is None:
        return None
    ids = _team_ids()
    if not isinstance(team, str) or (ids and team not in ids):
        raise ApiError(400, f"unknown team '{team}'")
    return team


def _check_driver(driver):
    if driver is None:
        return None
    ids = {d["id"] for d in drivers.driver_list()}
    if not isinstance(driver, str) or (ids and driver not in ids):
        raise ApiError(400, f"unknown driver '{driver}'")
    return driver


def _track(track_id) -> dict:
    if not isinstance(track_id, str) or not track_id:
        raise ApiError(400, "missing 'track'")
    try:
        return tracks.load_track(track_id)
    except KeyError:
        raise ApiError(404, f"unknown track '{track_id}'") from None
    except FileNotFoundError as e:
        raise ApiError(503, str(e)) from None


def _line(track_id: str, tr: dict) -> tuple[dict, bool]:
    """Racing line path for the solver; falls back to the centerline if racing_line is unavailable."""
    try:
        from . import racing_line
        return racing_line.get_line(track_id), True
    except ImportError:
        path = dict(tr)
        path["offset"] = np.zeros(len(tr["s"]))
        return path, False


def _scalars(d, what: str) -> dict:
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ApiError(400, f"'{what}' must be an object")
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and not math.isfinite(v):
            raise ApiError(400, f"'{what}.{k}' must be a finite number")
        if isinstance(v, (int, float, str, bool)):
            out[str(k)] = v
    return out


def _overrides(d) -> dict:
    """Car parameter overrides: known keys must match their schema kind (unknown keys pass through and are
    reported as warnings by simulate)."""
    out = _scalars(d, "overrides")
    for k, v in out.items():
        p = params.BY_KEY.get(k)
        if p is None:
            continue
        if p["kind"] == "choice":
            if v not in p["options"]:
                raise ApiError(400, f"'{k}' must be one of: " + ", ".join(map(str, p["options"])))
        elif isinstance(v, str):
            raise ApiError(400, f"'{k}' must be a number")
    return out


def _driver_overrides(d) -> dict:
    out = _scalars(d, "driver_overrides")
    keys = {p["key"] for p in drivers.SCHEMA}
    for k, v in out.items():
        if k in keys and isinstance(v, str):
            raise ApiError(400, f"driver parameter '{k}' must be a number")
    return out


# --------------------------------------------------------------------------------------------- GET endpoints
_tracks_lock = threading.Lock()
_tracks_cache: list | None = None


def tracks_list() -> list[dict]:
    """GET /api/tracks (cached: the first call loads and aligns every circuit, ~2 s)."""
    global _tracks_cache
    with _tracks_lock:
        if _tracks_cache is None:
            out = []
            for t in tracks.track_list():
                row = {"id": t["id"], "name": t["name"], "type": t["type"], "night": t["night"], "laps": t["laps"],
                       "round": t.get("round"), "date": t.get("date"), "pit_loss_s": t.get("pit_loss_s"),
                       "overtake_difficulty": t.get("overtake_difficulty")}
                try:
                    s = tracks.track_summary(t["id"])
                    row.update({k: s[k] for k in ("length", "width", "align_rms_m", "elevation_source", "start_line",
                                                  "n_corners", "pit_loss_s", "overtake_difficulty")})
                    row["notes"] = list(s["notes"])
                    row["available"] = True
                except Exception as e:  # noqa: BLE001 - one broken circuit must not hide the others
                    row.update({"length": None, "width": None, "align_rms_m": None, "elevation_source": "flat",
                                "start_line": None, "n_corners": 0, "notes": [f"Track unavailable: {e}"],
                                "available": False})
                out.append(row)
            _tracks_cache = clean(out, 2)
        return _tracks_cache


def track_geometry(track_id: str) -> dict:
    """GET /api/track/<id>."""
    tr = _track(track_id)
    return {
        "id": tr["id"], "name": tr["name"], "type": tr["type"], "night": tr["night"],
        "length": round(float(tr["length"]), 2), "width": round(float(tr["width"]), 2), "ds": round(float(tr["ds"]), 4),
        "x": _arr(tr["x"], 2), "y": _arr(tr["y"], 2), "z": _arr(tr["z"], 2), "psi": _arr(tr["psi"], 4),
        "kappa": _arr(tr["kappa"], 6),
        "corners": [{"num": c["num"], "apex": int(c["apex"]), "s_apex": round(float(c["s_apex"]), 1),
                     "i0": int(c["i0"]), "i1": int(c["i1"]), "dir": c["dir"], "radius": round(float(c["radius"]), 1)}
                    for c in tr["corners"]],
        "kerbs": clean(tr["kerbs"], 2), "kerb_width": tr["kerb_width"], "runoff": clean(tr["runoff"], 2),
        "pit": {"entry_s": round(float(tr["pit"]["entry_s"]), 1), "exit_s": round(float(tr["pit"]["exit_s"]), 1),
                "side": tr["pit"]["side"]},
        "notes": list(tr["notes"]), "elevation_source": tr["elevation_source"],
    }


def _standings() -> dict:
    return _json_file(PUB / "2026" / "constructor_standings.json", {}) or {}


def teams_list() -> list[dict]:
    pos = {s["team"]: s for s in _standings().get("standings", [])}
    out = []
    for t in _teams():
        s = pos.get(t["id"])
        out.append({"id": t["id"], "name": t["name"], "pu": t.get("pu"), "drivers": t.get("drivers", []),
                    "other_drivers": t.get("other_drivers", []),
                    "standing": {"pos": s["pos"], "points": s["points"]} if s else None,
                    "calibrated": (DATA / "presets" / f"{t['id']}.json").exists()})
    return out


def params_schema(team) -> dict:
    out = params.schema_for_api(_check_team(team or None))
    out["driver_params"] = [{k: p[k] for k in ("key", "label", "min", "max", "step", "default", "tip")}
                            for p in drivers.SCHEMA]
    return out


def preset(team: str) -> dict:
    if team not in _team_ids():
        raise ApiError(404, f"unknown team '{team}'")
    p = params.load_preset(team)
    return {"team": team, "values": p["values"], "prov": p["prov"], "fit": p["fit"], "calibrated": p["calibrated"]}


def _livery_numbers(team: dict, roster: dict) -> dict:
    return {d: roster[d] for d in team.get("drivers", []) + team.get("other_drivers", []) if d in roster}


def liveries() -> dict:
    roster = {d["id"]: d.get("number", 0) for d in (_json_file(DATA / "drivers.json", []) or [])}
    out = {}
    for t in _teams():
        liv = json.loads(json.dumps(NEUTRAL_LIVERY))
        saved = _json_file(DATA / "liveries" / f"{t['id']}.json")
        liv["numbers"] = _livery_numbers(t, roster)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in ("regions", "numbers") and isinstance(v, dict):
                    liv[k].update(v)
                else:
                    liv[k] = v
        else:
            liv["placeholder"] = True     # no livery file yet: neutral grey scheme
        out[t["id"]] = liv
    return out


def save_livery(team: str, body) -> dict:
    if team not in _team_ids() or not _ID.match(team):
        raise ApiError(404, f"unknown team '{team}'")
    if not isinstance(body, dict):
        raise ApiError(400, "livery must be a JSON object")
    liv = dict(body)
    liv.pop("placeholder", None)
    for k in ("base", "secondary", "accent", "number_color"):
        if k in liv and not (isinstance(liv[k], str) and _HEX.match(liv[k])):
            raise ApiError(400, f"livery '{k}' must be a colour like #rrggbb")
    if "finish" in liv and liv["finish"] not in ("gloss", "matte", "satin"):
        raise ApiError(400, "livery 'finish' must be gloss, matte or satin")
    regions = liv.get("regions", {})
    if not isinstance(regions, dict):
        raise ApiError(400, "livery 'regions' must be an object")
    for k, v in regions.items():
        if k not in LIVERY_REGIONS:
            raise ApiError(400, f"unknown livery region '{k}'")
        if not (v in ("base", "secondary", "accent", "carbon") or (isinstance(v, str) and _HEX.match(v))):
            raise ApiError(400, f"livery region '{k}' must be base, secondary, accent, carbon or #rrggbb")
    numbers = liv.get("numbers", {})
    if not isinstance(numbers, dict) or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 99
                                                    for v in numbers.values()):
        raise ApiError(400, "livery 'numbers' must map driver ids to integers 0-99")
    d = DATA / "liveries"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{team}.json.tmp"
    tmp.write_text(json.dumps(liv, indent=2))
    tmp.replace(d / f"{team}.json")
    return {"ok": True}


def data_status() -> dict:
    fetched = _json_file(PUB / "fetched_at.json", {}) or {}
    st = _standings()
    rounds = sum(1 for p in (PUB / "2026").glob("[0-9][0-9]") if (p / "results.json").exists())
    stale = bool(fetched.get("stale_snapshot") or st.get("stale_snapshot"))
    calibration = _json_file(DATA / "presets" / "_report.json")
    validation = _json_file(DATA / "presets" / "_validation.json")
    if isinstance(validation, dict):   # accept {"validation": [...]} / {"rows": [...]} / {"tracks": [...]}
        validation = next((validation[k] for k in ("validation", "rows", "tracks") if isinstance(validation.get(k), list)), None)
    notices = ["Car data is estimated: presets are reverse-calibrated from public timing data, never real team data.",
               "Results are good for trends and comparisons, not exact real lap times or race results.",
               "Liveries are generic team colour schemes: no real sponsor logos or team badges are included."]
    if not fetched.get("fetched_at"):
        notices.append("No public data has been fetched yet. Run: python scripts/fetch_data.py")
    if stale:
        notices.append("Standings come from the built-in snapshot because the live fetch failed; they may be out of date.")
    if not isinstance(calibration, dict):
        notices.append("Team presets are not calibrated yet: every team uses the baseline car until calibration has run.")
    try:
        flat = [t["name"] for t in tracks_list() if t.get("elevation_source") == "flat"]
        if flat:
            notices.append(f"{len(flat)} track(s) have no elevation data and are simulated flat: " + ", ".join(flat) + ".")
    except Exception:  # noqa: BLE001
        pass
    return {"fetched_at": fetched.get("fetched_at"), "sources": fetched.get("sources", []),
            "status": fetched.get("status", {}), "rounds_cached": rounds, "stale_snapshot": stale,
            "after_round": st.get("after_round"),
            "standings": [{"pos": s["pos"], "team": s["team"], "points": s["points"]} for s in st.get("standings", [])],
            "calibration": calibration if isinstance(calibration, dict) else None,
            "validation": validation if isinstance(validation, list) else None, "notices": notices,
            "numba": solver.HAVE_NUMBA}


# --------------------------------------------------------------------------------------------- simulate
class _LRU:
    def __init__(self, size: int):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()


_sim_cache = _LRU(SIM_CACHE_SIZE)


def _sim_config(body) -> dict:
    """Validated, normalised simulate request (also the cache key)."""
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    mode = body.get("mode") or "quali"
    if mode not in ("quali", "race_pace"):
        raise ApiError(400, "mode must be 'quali' or 'race_pace'")
    overrides = _overrides(body.get("overrides"))
    age = body.get("tyre_age")
    if age is None:
        age = overrides.get("tyre_age", 0)
    try:
        age = int(min(max(float(age), 0), params.BY_KEY["tyre_age"]["max"]))
    except (TypeError, ValueError):
        raise ApiError(400, "tyre_age must be a number") from None
    overrides.pop("tyre_age", None)
    track_id = body.get("track")
    _track(track_id)
    return {"track": track_id, "team": _check_team(body.get("team")), "driver": _check_driver(body.get("driver")),
            "overrides": overrides, "driver_overrides": _driver_overrides(body.get("driver_overrides")),
            "mode": mode, "tyre_age": age}


def _corner_table(tr: dict, path: dict, r: dict, s_line: np.ndarray) -> list[dict]:
    """Minimum speed and braking point per corner, with the track's corner ranges mapped onto line samples."""
    n = len(r["v"])
    idx_c = path.get("idx_center")
    n_tr = len(tr["s"])

    def to_line(i):
        if idx_c is not None:
            return int(min(np.searchsorted(idx_c, i), n - 1))
        return int(round(i * n / n_tr)) % n

    pad = int(round(CORNER_PAD_M / r["ds"]))
    v, brake = r["v"], r["brake"]
    out = []
    for c in tr["corners"]:
        j0, j1 = to_line(c["i0"]) - pad, to_line(c["i1"]) + pad
        if j1 < j0:
            j1 += n                                   # range wraps past the start line
        rng = np.arange(j0, j1 + 1) % n
        i_min = int(rng[np.argmin(v[rng])])
        # walk back from the slowest point to the brake application (tolerating a short coasting gap)
        j, gap = i_min, 0
        while brake[j % n] <= 0.05 and gap < pad:
            j -= 1
            gap += 1
        brake_s = None
        if brake[j % n] > 0.05:
            steps = 0
            while brake[(j - 1) % n] > 0.05 and steps < n:
                j -= 1
                steps += 1
            brake_s = round(float(s_line[j % n]), 1)
        out.append({"num": c["num"], "s_apex": round(float(s_line[to_line(c["apex"])]), 1),
                    "v_min": round(float(v[i_min]) * 3.6, 1), "brake_s": brake_s, "dir": c["dir"],
                    "i_min": i_min})
    return out


def _sectors(tr: dict, s_line: np.ndarray, length: float, dt: np.ndarray) -> list[float]:
    """Sector times: boundaries from the track dict if it has them (list of 2 distances), else equal thirds."""
    bounds = tr.get("sectors")
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 2):
        bounds = [length / 3.0, 2.0 * length / 3.0]
    else:
        bounds = [float(b) * length / float(tr["length"]) for b in bounds]   # centerline s -> line s
    k1, k2 = (int(np.searchsorted(s_line, b)) for b in bounds)
    return [float(dt[:k1].sum()), float(dt[k1:k2].sum()), float(dt[k2:].sum())]


def _simulate(cfg: dict) -> dict:
    tr = _track(cfg["track"])
    path, have_line = _line(cfg["track"], tr)
    car = params.make_car(cfg["team"], cfg["overrides"])
    quali = cfg["mode"] == "quali"
    if not quali and "pu_mode" not in cfg["overrides"]:
        car["pu_mode"] = "race"
    car["tyre_age"] = cfg["tyre_age"]
    drv = drivers.get_driver(cfg["driver"], cfg["driver_overrides"])
    from .calibration.fit import quali_state
    # the presets were fitted at each session's real air density and temperatures, so simulate at them too
    state = {**quali_state(cfg["track"]), "bumpiness": 1.0 if tr["type"] == "street" else 0.5}
    if cfg["tyre_age"] > 0:
        # wear state from tyre age: wear rate of this car on fresh tyres x laps done
        pre = solver.solve(path, car, drv, state, quali=quali, thermal=True, detail=False)
        state["wear_f"] = float(min(1.0, cfg["tyre_age"] * pre["wear_per_lap_f"]))
        state["wear_r"] = float(min(1.0, cfg["tyre_age"] * pre["wear_per_lap_r"]))
    r = solver.solve(path, car, drv, state, quali=quali, thermal=True, detail=True)

    n = int(r["n"])
    ds = float(r["ds"])
    length = float(path.get("length", n * ds))
    s_line = np.asarray(path["s"], dtype=float) if "s" in path else np.arange(n) * ds
    lap_time = float(r["lap_time"])
    # lap_time includes the upshift loss, which is not in the per-sample dt: spread it so that the time
    # channel, sectors and compare deltas all add up to the lap time
    dt = r["dt"] * (lap_time / float(np.sum(r["dt"])))
    t = np.concatenate([[0.0], np.cumsum(dt)[:-1]])
    v = r["v"]
    kappa = np.asarray(path["kappa"], dtype=float)
    offset = path.get("offset")
    line = {"s": _arr(s_line, 2), "x": _arr(path["x"], 2), "y": _arr(path["y"], 2),
            "z": _arr(path.get("z", np.zeros(n)), 2), "psi": _arr(path["psi"], 4),
            "offset": _arr(offset if offset is not None else np.zeros(n), 2)}
    telemetry = {
        "s": line["s"], "t": _arr(t, 3), "v": _arr(v * 3.6, 1), "throttle": _arr(r["throttle"], 3),
        "brake": _arr(r["brake"], 3), "gear": _ints(r["gear"]), "rpm": _ints(np.round(r["rpm"])),
        "ax": _arr(r["ax"] / G, 3), "ay": _arr(r["ay"] / G, 3), "soc": _arr(r["soc"] / 1e6, 3),
        "mode": _ints(r["mode"]), "p_dep": _arr(r["p_dep"] / 1e3, 1), "p_harv": _arr(r["p_harv"] / 1e3, 1),
        "tyre_temp_f": _arr(r["tyre_temp_f"], 1), "tyre_temp_r": _arr(r["tyre_temp_r"], 1),
        "brake_temp_f": _arr(r["brake_temp_f"], 1), "brake_temp_r": _arr(r["brake_temp_r"], 1),
        "ride_h_f": _arr(r["ride_h_f"], 2), "ride_h_r": _arr(r["ride_h_r"], 2), "balance": _arr(r["balance"], 4),
        "kappa": _arr(kappa, 6), "lift": _ints(r["lift"]),
    }
    summary = {
        "vmax": round(r["vmax"] * 3.6, 1), "vmin": round(r["vmin"] * 3.6, 1),
        "v_avg": round(length / lap_time * 3.6, 1),
        "e_deploy_mj": round(r["e_deploy_mj"], 3), "e_harvest_mj": round(r["e_harvest_mj"], 3),
        "e_budget_mj": round(r["e_budget_mj"], 3), "mass": round(float(r["mass"]), 1),
        "fuel": round(float(r["vehicle"]["fuel"]), 1),
        "wear_per_lap_f": round(float(r["wear_per_lap_f"]), 5), "wear_per_lap_r": round(float(r["wear_per_lap_r"]), 5),
        "wear_f": round(state.get("wear_f", 0.0), 4), "wear_r": round(state.get("wear_r", 0.0), 4),
        "upshifts": int(r["upshifts"]), "max_lat_g": round(float(np.max(np.abs(r["ay"]))) / G, 2),
        "max_brake_g": round(float(max(0.0, -np.min(r["ax"]))) / G, 2),
        "x_mode_pct": round(100.0 * float(np.mean(r["mode"] == 1)), 1),
        "sectors": [round(x, 3) for x in _sectors(tr, s_line, length, dt)],
        "length": round(length, 1), "compound": car["compound"], "tyre_age": cfg["tyre_age"],
    }
    warns = suitability.warnings(tr)
    if not have_line:
        warns.append("Racing line module not available: the lap is driven on the track centerline.")
    unknown = sorted(k for k in cfg["overrides"] if k not in params.BY_KEY)
    if unknown:
        warns.append("Ignored unknown parameters: " + ", ".join(unknown) + ".")
    return {"track": cfg["track"], "team": cfg["team"], "driver": cfg["driver"], "mode": cfg["mode"],
            "lap_time": round(lap_time, 4), "lap_time_str": lap_time_str(lap_time),
            "line": line, "telemetry": telemetry, "summary": summary,
            "corners": _corner_table(tr, path, r, s_line),
            "flags": suitability.flags(r, car, tr), "warnings": warns}


def _default_label(cfg: dict) -> str:
    team = next((t["name"] for t in _teams() if t["id"] == cfg["team"]), "Baseline car")
    drv = next((d.get("code") or d["id"] for d in drivers.driver_list() if d["id"] == cfg["driver"]), None)
    return " / ".join(x for x in (team, drv, "quali" if cfg["mode"] == "quali" else "race pace") if x)


def simulate(body: dict) -> dict:
    """POST /api/simulate. Identical requests are answered from a small LRU cache."""
    t0 = time.perf_counter()
    cfg = _sim_config(body)
    key = json.dumps(cfg, sort_keys=True)
    res = _sim_cache.get(key)
    cached = res is not None
    if res is None:
        res = _simulate(cfg)
        _sim_cache.put(key, res)
    label = body.get("label")
    out = {"label": str(label)[:80] if label else _default_label(cfg)}
    out.update(res)            # shallow copy: the cached arrays are shared and never mutated
    out["run_time_s"] = round(time.perf_counter() - t0, 4)
    out["cached"] = cached
    return out


def compare(body: dict) -> dict:
    """POST /api/compare: simulate each config, rank them, delta-time vs configs[0] along distance."""
    configs = body.get("configs") if isinstance(body, dict) else None
    if not isinstance(configs, list) or len(configs) < 2:
        raise ApiError(400, "'configs' must be a list of at least 2 simulate bodies")
    if len(configs) > MAX_COMPARE:
        raise ApiError(400, f"at most {MAX_COMPARE} configs can be compared")
    if not all(isinstance(c, dict) for c in configs):
        raise ApiError(400, "every config must be an object")
    if not all(isinstance(c.get("track"), str) for c in configs):
        raise ApiError(400, "every config needs a 'track'")
    if len({c["track"] for c in configs}) != 1:
        raise ApiError(400, "all configs must use the same track")
    for c in configs:
        _sim_config(c)         # validate everything before spending time on any solve
    with ThreadPoolExecutor(max_workers=min(4, len(configs))) as pool:
        results = list(pool.map(simulate, configs))
    order = sorted(range(len(results)), key=lambda i: results[i]["lap_time"])
    best = results[order[0]]["lap_time"]
    ranking = [{"index": i, "label": results[i]["label"], "lap_time": results[i]["lap_time"],
                "gap": round(results[i]["lap_time"] - best, 4)} for i in order]

    # cumulative time vs lap fraction, closed with (lap end, lap time); lines of different length are
    # compared at the same fraction of the lap
    def curve(res):
        s = np.append(np.asarray(res["telemetry"]["s"]), res["summary"]["length"])
        return s / s[-1], np.append(np.asarray(res["telemetry"]["t"]), res["lap_time"])

    f0, t0 = curve(results[0])
    series = []
    for i, res in enumerate(results):
        f, t = curve(res)
        series.append({"index": i, "label": res["label"], "dt": _arr(np.interp(f0, f, t) - t0, 3)})
    return {"results": results, "ranking": ranking,
            "delta": {"s": _arr(f0 * results[0]["summary"]["length"], 2), "series": series}}


# --------------------------------------------------------------------------------------------- jobs
def _unavailable(what: str, e: Exception) -> ApiError:
    return ApiError(503, f"{what} unavailable: not installed yet ({type(e).__name__}: {e})")


def _import(module: str, name: str, what: str):
    """Import a job entry point lazily, so modules written after the server started are picked up.
    A missing (or half-written) module is a clean 503, not a 500."""
    importlib.invalidate_caches()
    try:
        return getattr(importlib.import_module(module, __package__), name)
    except (ImportError, AttributeError, SyntaxError) as e:
        raise _unavailable(what, e) from None


def _submit(kind: str, fn, on_done=None) -> dict:
    """Run fn(progress) as a job; the result is made JSON-safe once, not on every poll."""
    return {"job": JOBS.submit(kind, lambda progress: clean(fn(progress)), on_done=on_done)}


def start_race(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    track_id = body.get("track")
    _track(track_id)
    entries = body.get("entries")
    if not isinstance(entries, list) or not 2 <= len(entries) <= MAX_ENTRIES:
        raise ApiError(400, f"'entries' must list 2 to {MAX_ENTRIES} cars")
    clean_entries = []
    for e in entries:
        if not isinstance(e, dict):
            raise ApiError(400, "every entry must be an object")
        strategy = e.get("strategy", "auto")
        if not (strategy == "auto" or isinstance(strategy, dict)):
            raise ApiError(400, "entry strategy must be 'auto' or {stops, start_compound}")
        clean_entries.append({"team": _check_team(e.get("team")), "driver": _check_driver(e.get("driver")),
                              "overrides": _overrides(e.get("overrides")),
                              "driver_overrides": _driver_overrides(e.get("driver_overrides")),
                              "strategy": strategy})
    laps = body.get("laps")
    if laps is not None:
        if isinstance(laps, bool) or not isinstance(laps, (int, float)) or not math.isfinite(laps) \
                or not 1 <= int(laps) <= 200:
            raise ApiError(400, "'laps' must be between 1 and 200 (or null for the real race distance)")
        laps = int(laps)
    grid = body.get("grid", "quali")
    if not (grid == "quali" or (isinstance(grid, list) and all(isinstance(g, str) for g in grid))):
        raise ApiError(400, "'grid' must be 'quali' or a list of driver ids")
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise ApiError(400, "'options' must be an object")
    try:
        seed = int(options.get("seed", 0) or 0)
        mc = int(body.get("monte_carlo") or 0)
    except (TypeError, ValueError, OverflowError):
        raise ApiError(400, "'seed' and 'monte_carlo' must be integers") from None
    if not 0 <= mc <= MAX_MONTE_CARLO:
        raise ApiError(400, f"'monte_carlo' must be between 0 and {MAX_MONTE_CARLO}")
    if mc > 1:
        run_monte_carlo = _import(".race.montecarlo", "run_monte_carlo", "race simulator (Monte Carlo)")

        def fn(progress):
            return run_monte_carlo(track_id, clean_entries, laps=laps, grid=grid, options=options, k=mc,
                                   seed=seed, progress=progress)
    else:
        run_race = _import(".race.race_sim", "run_race", "race simulator")

        def fn(progress):
            return run_race(track_id, clean_entries, laps=laps, grid=grid, options=options, seed=seed,
                            progress=progress)
    return _submit("race", fn)


def start_optimise(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "request body must be a JSON object")
    track_id = body.get("track")
    _track(track_id)
    team, driver = _check_team(body.get("team")), _check_driver(body.get("driver"))
    overrides = _overrides(body.get("overrides"))
    driver_overrides = _driver_overrides(body.get("driver_overrides"))
    keys = body.get("params")
    if keys is not None and not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
        raise ApiError(400, "'params' must be a list of parameter keys")
    objective = body.get("objective") or "quali"
    if objective not in ("quali", "race"):
        raise ApiError(400, "objective must be 'quali' or 'race'")
    max_evals = body.get("max_evals")
    if max_evals is not None:
        if isinstance(max_evals, bool) or not isinstance(max_evals, int) or not 2 <= max_evals <= 800:
            raise ApiError(400, "'max_evals' must be an integer between 2 and 800")
    optimise = _import(".optimise", "optimise", "setup optimiser")

    def fn(progress):
        return optimise(track_id, team, driver, overrides, driver_overrides, keys or None, objective,
                        progress=progress, max_evals=max_evals)
    return _submit("optimise", fn)


def start_calibrate(body=None) -> dict:
    run_calibration = _import(".calibration.fit", "run_calibration", "calibration")
    if JOBS.running("calibrate"):
        raise ApiError(409, "a calibration is already running")

    def done(_job):
        # presets changed on disk: cached laps and the calibrated flags are stale
        _sim_cache.clear()

    return _submit("calibrate", lambda progress: run_calibration(progress=progress), on_done=done)


def save_setup(body) -> dict:
    try:
        return {"ok": True, "name": setups.save_setup(body)}
    except ValueError as e:
        raise ApiError(400, str(e)) from None


def get_setup(name: str) -> dict:
    try:
        return setups.load_setup(name)
    except KeyError as e:
        raise ApiError(404, str(e.args[0]) if e.args else f"no setup named '{name}'") from None
    except ValueError as e:
        raise ApiError(400, str(e)) from None


def delete_setup(name: str) -> dict:
    try:
        if not setups.delete_setup(name):
            raise ApiError(404, f"no setup named '{name}'")
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True}


def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise ApiError(404, f"unknown job '{job_id}'")
    return job


# --------------------------------------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "f1sim"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- responses
    def _send(self, status: int, body: bytes, ctype: str, head_only: bool = False):
        if (len(body) > 2048 and "gzip" in self.headers.get("Accept-Encoding", "")
                and ctype.startswith(("application/json", "text/"))):
            body = gzip.compress(body, compresslevel=1)
            encoding = "gzip"
        else:
            encoding = None
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        if ctype.startswith(("application/json", "text/")):
            self.send_header("Vary", "Accept-Encoding")
        if status >= 400:
            self.send_header("Connection", "close")   # the request body may not have been read
            self.close_connection = True
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, obj, status: int = 200, head_only: bool = False):
        try:
            body = json.dumps(obj, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            body = json.dumps(clean(obj), separators=(",", ":"), allow_nan=False)
        self._send(status, body.encode(), "application/json; charset=utf-8", head_only)

    def _error(self, status: int, message: str):
        self._json({"error": message}, status, head_only=self.command == "HEAD")

    def _local_request(self) -> bool:
        """Local-only tool: when bound to loopback, refuse requests addressed to another host name
        (DNS rebinding) and cross-site POSTs from pages on other origins."""
        if not getattr(self.server, "loopback_only", True):
            return True
        raw = self.headers.get("Host", "")
        host = raw[:raw.find("]") + 1] if raw.startswith("[") else raw.split(":")[0]
        if host not in ("localhost", "127.0.0.1", "[::1]"):
            return False
        origin = self.headers.get("Origin")
        if origin and self.command == "POST":
            o = urlsplit(origin)
            if o.hostname not in ("localhost", "127.0.0.1", "::1"):
                return False
        return True

    def _dispatch(self, fn):
        try:
            if not self._local_request():
                raise ApiError(403, "this server only accepts local requests")
            fn()
        except ApiError as e:
            self._error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 - report instead of dropping the connection
            import traceback
            traceback.print_exc()
            try:
                self._error(500, f"{type(e).__name__}: {e}")
            except OSError:
                pass

    # ---- verbs
    def do_GET(self):
        self._dispatch(self._get)

    def do_HEAD(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def _other(self):
        self._error(405, f"method {self.command} not allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._dispatch(self._other)

    def _get(self):
        url = urlsplit(self.path)
        path = unquote(url.path)
        if not (path == "/api" or path.startswith("/api/")):
            return self._static(path)
        seg = [p for p in path.split("/")[2:] if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        head = self.command == "HEAD"
        out = None
        if len(seg) == 1:
            simple = {"tracks": tracks_list, "teams": teams_list, "drivers": drivers.driver_list,
                      "liveries": liveries, "data_status": data_status, "setups": setups.list_setups}
            if seg[0] in simple:
                out = simple[seg[0]]()
        elif len(seg) == 2:
            if seg == ["params", "schema"]:
                out = params_schema(query.get("team"))
            else:
                one = {"track": track_geometry, "presets": preset, "setups": get_setup, "jobs": get_job}
                if seg[0] in one:
                    out = one[seg[0]](seg[1])
        if out is None:
            raise ApiError(404, f"unknown endpoint GET {path}")
        self._json(out, head_only=head)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if length > MAX_BODY:
            raise ApiError(413, f"request body too large (limit {MAX_BODY // 1024} kB)")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body is not valid JSON") from None

    def _post(self):
        path = unquote(urlsplit(self.path).path)
        seg = [p for p in path.split("/")[2:] if p] if path.startswith("/api/") else []
        body = self._body()
        if getattr(self.server, "public", False):
            writes = (seg in (["calibrate"], ["setups"]) or (len(seg) == 2 and seg[0] == "liveries")
                      or (len(seg) == 3 and seg[0] == "setups"))
            if writes:
                raise ApiError(403, "read-only in the public demo: run the simulator locally to save setups,"
                                    " edit liveries or re-run calibration")
            if seg == ["race"] and isinstance(body, dict):
                try:
                    if int(body.get("monte_carlo") or 0) > PUBLIC_MAX_MONTE_CARLO:
                        raise ApiError(400, f"'monte_carlo' is limited to {PUBLIC_MAX_MONTE_CARLO} in the public demo")
                except (TypeError, ValueError):
                    pass
        simple = {"simulate": simulate, "compare": compare, "race": start_race, "optimise": start_optimise,
                  "calibrate": start_calibrate, "setups": save_setup}
        if len(seg) == 1 and seg[0] in simple:
            out = simple[seg[0]](body)
        elif len(seg) == 3 and seg[0] == "setups" and seg[2] == "delete":
            out = delete_setup(seg[1])
        elif len(seg) == 2 and seg[0] == "liveries":
            out = save_livery(seg[1], body)
        else:
            raise ApiError(404, f"unknown endpoint POST {path}")
        self._json(out)

    # ---- static files
    def _static(self, path: str):
        root = Path(getattr(self.server, "web_root", WEB)).resolve()
        parts = [p for p in path.split("/") if p]
        if any(p == ".." or "\\" in p or "\x00" in p or p.startswith(".") for p in parts):
            raise ApiError(403, "forbidden path")
        target = root.joinpath(*parts).resolve()
        if target != root and root not in target.parents:
            raise ApiError(403, "forbidden path")      # e.g. a symlink pointing out of web/
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if not parts:
                raise ApiError(404, "web UI not found: web/index.html is missing")
            raise ApiError(404, f"not found: {path}")
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        size = target.stat().st_size
        if size <= 512 * 1024 and ctype.startswith(("text/", "application/json", "image/svg")):
            return self._send(200, target.read_bytes(), ctype, head_only=self.command == "HEAD")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            with target.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, 256 * 1024)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str = "127.0.0.1", port: int = 8765, web_root: Path | str | None = None,
                verbose: bool = False, public: bool = False) -> Server:
    """Create (but do not start) the server. port=0 picks a free port: see server.server_address."""
    srv = Server((host, port), Handler)
    srv.web_root = Path(web_root) if web_root else WEB
    srv.verbose = verbose
    srv.loopback_only = host in ("127.0.0.1", "localhost", "::1")
    srv.public = public  # hosted demo: no writes to disk, capped Monte Carlo
    return srv


def warm_up():
    """Compile the numba kernels and fill the track list in the background so the first click is fast."""
    try:
        tracks_list()
        simulate({"track": "monza"})
    except Exception:  # noqa: BLE001 - warm-up is best effort
        pass

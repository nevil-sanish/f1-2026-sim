"""Track loading and processing (brief §3.5).

Pipeline: lon/lat -> local metres -> periodic cubic spline -> uniform arc-length resample scaled to the
official length -> analytic heading/curvature/normals -> width -> (optional) alignment with public
telemetry X/Y to find the start line, driving direction, RMS offset and an elevation profile ->
corners, kerbs, run-off and barrier zones.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.interpolate import splev, splprep

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUB = DATA / "public" / "2026"
DS = 3.0  # resample step [m]
R_EARTH = 6_371_000.0


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return _json(DATA / "track_catalog.json")["tracks"]


@lru_cache(maxsize=1)
def calendar() -> list[dict]:
    return _json(PUB / "calendar.json", []) or []


def track_list() -> list[dict]:
    """Catalogue entries on the live calendar (or all default-calendar tracks if no calendar is cached)."""
    cal = {c["track"]: c for c in calendar() if c.get("track")}
    out = []
    for t in catalog():
        c = cal.get(t["id"])
        if cal and c is None:
            continue  # not on the live calendar (e.g. Bahrain, Jeddah in 2026)
        if not cal and t["id"] in ("bahrain", "jeddah", "sepang"):
            continue
        out.append({**t, "round": c["round"] if c else None, "date": c["date"] if c else None})
    out.sort(key=lambda t: t["round"] or 99)
    return out


def _round_dir(track_id: str) -> Path | None:
    for c in calendar():
        if c.get("track") == track_id:
            return PUB / f"{c['round']:02d}"
    return None


def _spline_resample(pts: np.ndarray, length: float | None, ds: float = DS, smooth: float = 1.0):
    """Periodic cubic spline through pts (N,2), uniform arc-length resample, rescale to `length`.

    Returns dict with s, x, y, psi, kappa (all at uniform ds), computed analytically from the spline.
    """
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    # drop near-duplicate points (splprep needs strictly increasing parameter)
    keep = np.concatenate([[True], np.linalg.norm(np.diff(pts, axis=0), axis=1) > 0.5])
    pts = pts[keep]
    closed = np.vstack([pts, pts[:1]])
    # small smoothing: ~`smooth` m^2 per point absorbs digitising noise without rounding corners much
    tck, _ = splprep([closed[:, 0], closed[:, 1]], per=True, s=smooth * len(pts), k=3)
    u_f = np.linspace(0, 1, 20001)
    xf, yf = splev(u_f, tck)
    seg = np.hypot(np.diff(xf), np.diff(yf))
    s_f = np.concatenate([[0], np.cumsum(seg)])
    raw_len = s_f[-1]
    n = max(200, int(round((length or raw_len) / ds)))
    s_u = np.linspace(0, raw_len, n, endpoint=False)
    u = np.interp(s_u, s_f, u_f)
    x, y = splev(u, tck)
    dx, dy = splev(u, tck, der=1)
    ddx, ddy = splev(u, tck, der=2)
    kappa = (dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)
    psi = np.arctan2(dy, dx)
    scale = (length / raw_len) if length else 1.0
    cx, cy = np.mean(x), np.mean(y)
    x = (np.asarray(x) - cx) * scale
    y = (np.asarray(y) - cy) * scale
    L = raw_len * scale
    return {"s": np.arange(n) * (L / n), "x": x, "y": y, "psi": np.unwrap(psi), "kappa": kappa / scale,
            "length": L, "ds": L / n, "raw_length": raw_len, "scale": scale}


def _align(track: dict, tx: np.ndarray, ty: np.ndarray):
    """Similarity-align telemetry X/Y (arbitrary frame, lap starting at the S/F line) to the centerline.

    Uses FFT cross-correlation of the complex point sequences to test every start offset, both directions
    and both handedness at once (closed-form Procrustes per shift). Returns transform + per-sample index.
    """
    n = 512
    def resamp(x, y, closed_gap=True):
        d = np.concatenate([[0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
        if closed_gap:  # close the loop
            d_tot = d[-1] + np.hypot(x[0] - x[-1], y[0] - y[-1])
            x = np.append(x, x[0]); y = np.append(y, y[0]); d = np.append(d, d_tot)
        q = np.linspace(0, d[-1], n, endpoint=False)
        return np.interp(q, d, x) + 1j * np.interp(q, d, y)
    a = resamp(track["x"], track["y"])
    b0 = resamp(tx, ty)
    a_c = a - a.mean()
    best = None
    for reverse in (False, True):
        for mirror in (False, True):
            b = b0.conj() if mirror else b0
            if reverse:
                b = np.roll(b[::-1], 1)
            b_c = b - b.mean()
            # c[k] = sum_i a[i+k] * conj(b[i])  -> best complex scale for shift k is c[k]/sum|b|^2
            c = np.fft.ifft(np.fft.fft(a_c) * np.conj(np.fft.fft(b_c)))
            resid = np.sum(np.abs(a_c) ** 2) - np.abs(c) ** 2 / np.sum(np.abs(b_c) ** 2)
            k = int(np.argmin(resid))
            if best is None or resid[k] < best[0]:
                best = (resid[k], k, reverse, mirror, c[k] / np.sum(np.abs(b_c) ** 2), a.mean(), b.mean())
    _, k, reverse, mirror, alpha, a_mean, b_mean = best
    # transform every raw telemetry sample into the track frame, then nearest centerline sample
    z = tx + 1j * ty
    if mirror:
        z = z.conj()
    zt = alpha * (z - b_mean) + a_mean
    for _ in range(3):  # ICP refinement of the similarity transform
        d2 = (zt.real[:, None] - track["x"][None, :]) ** 2 + (zt.imag[:, None] - track["y"][None, :]) ** 2
        idx = np.argmin(d2, axis=1)
        tgt = track["x"][idx] + 1j * track["y"][idx]
        zc, tc = zt - zt.mean(), tgt - tgt.mean()
        g = np.sum(tc * zc.conj()) / np.sum(np.abs(zc) ** 2)
        zt = g * zc + tgt.mean()
        alpha = alpha * g
    d2 = (zt.real[:, None] - track["x"][None, :]) ** 2 + (zt.imag[:, None] - track["y"][None, :]) ** 2
    idx = np.argmin(d2, axis=1)
    rms = float(np.sqrt(np.mean(np.min(d2, axis=1))))
    return {"idx": idx, "rms": rms, "reverse": reverse, "mirror": mirror, "scale": float(abs(alpha)),
            "xy": zt}


def _telemetry_lap(track_id: str):
    """Fastest public quali lap telemetry (x, y, z, dist, speed) for this track, or None."""
    rd = _round_dir(track_id)
    if rd is None or not (rd / "tel_quali.npz").exists():
        return None
    try:
        z = np.load(rd / "tel_quali.npz")
        laps = _json(rd / "tel_quali.json", {}).get("laps", [])
        for row in laps:  # sorted fastest first
            t = row["team"]
            if row.get("has_tel") and f"{t}__x" in z.files:
                x, y = z[f"{t}__x"].astype(float), z[f"{t}__y"].astype(float)
                if np.ptp(x) < 1 or np.ptp(y) < 1:
                    continue
                return {"x": x, "y": y, "z": z[f"{t}__z"].astype(float), "dist": z[f"{t}__dist"].astype(float),
                        "speed": z[f"{t}__speed"].astype(float), "team": t, "lap": row["lap"], "code": row["code"]}
    except Exception:  # noqa: BLE001
        return None
    return None


def _smooth_periodic(v: np.ndarray, harmonics: int) -> np.ndarray:
    f = np.fft.rfft(v)
    f[harmonics + 1:] = 0
    return np.fft.irfft(f, len(v))


def _find_corners(s, kappa, ds, thr):
    """Corner = contiguous run with |kappa| > thr and constant sign; returns list of dicts."""
    n = len(s)
    kap = _smooth_periodic(kappa, n // 12)
    on = np.abs(kap) > thr
    sign = np.sign(kap)
    # start scanning from a straight sample so runs don't wrap awkwardly
    start = int(np.argmin(np.abs(kap)))
    corners, i = [], 0
    order = (np.arange(n) + start) % n
    while i < n:
        j = order[i]
        if not on[j]:
            i += 1
            continue
        k = i
        while k < n and on[order[k]] and sign[order[k]] == sign[j]:
            k += 1
        run = order[i:k]
        if len(run) * ds >= 12.0:
            apex = int(run[np.argmax(np.abs(kap[run]))])
            corners.append({"i0": int(run[0]), "i1": int(run[-1]), "apex": apex, "n": len(run),
                            "dir": "L" if kap[apex] > 0 else "R", "radius": float(1 / abs(kap[apex]))})
        i = k
    corners.sort(key=lambda c: c["i0"])
    for num, c in enumerate(corners, 1):
        c["num"] = num
        c["s_apex"] = float(s[c["apex"]])
    return corners


def _zones(tr: dict, cat: dict, ov: dict):
    """Kerbs, run-off and barriers generated from curvature and circuit type (overridable)."""
    s, ds, n = tr["s"], tr["ds"], len(tr["s"])
    thr = ov.get("kerb_kappa_threshold", 1 / 220.0)
    corners = _find_corners(s, tr["kappa"], ds, 1 / 400.0)
    kerbs, runoff = [], []
    street = cat["type"] == "street"
    for c in corners:
        inside = "L" if c["dir"] == "L" else "R"
        outside = "R" if inside == "L" else "L"
        if 1 / c["radius"] >= thr:
            half = max(4, int(min(c["n"] * 0.35, 30.0 / ds)))
            kerbs.append({"i0": (c["apex"] - half) % n, "i1": (c["apex"] + half) % n, "side": inside, "kind": "apex"})
            ex_len = max(5, int(min(45.0, 12 + c["radius"] * 0.35) / ds))
            kerbs.append({"i0": c["i1"] % n, "i1": (c["i1"] + ex_len) % n, "side": outside, "kind": "exit"})
        # run-off on the outside from the braking zone to the exit
        pre = int(min(90.0, 30 + 2500.0 / c["radius"]) / ds)
        kind = "wall" if street else ("gravel" if c["radius"] < 90 else "asphalt")
        runoff.append({"i0": (c["i0"] - pre) % n, "i1": (c["i1"] + int(40 / ds)) % n, "side": outside, "kind": kind,
                       "depth": 4.0 if street else float(np.clip(1800.0 / c["radius"], 12, 45))})
    tr["corners"] = corners
    tr["kerbs"] = ov.get("kerbs", kerbs)
    tr["runoff"] = ov.get("runoff", runoff)
    tr["kerb_width"] = 1.4
    L = tr["length"]
    tr["pit"] = {"entry_s": float(ov.get("pit_entry_s", L - 320.0)), "exit_s": float(ov.get("pit_exit_s", 330.0)),
                 "side": ov.get("pit_side", "R"), "provenance": "estimate" if "pit_entry_s" not in ov else "override"}


@lru_cache(maxsize=64)
def load_track(track_id: str) -> dict:
    cat = next((t for t in catalog() if t["id"] == track_id), None)
    if cat is None:
        raise KeyError(f"unknown track '{track_id}'")
    ov = (_json(DATA / "track_overrides.json", {}) or {}).get(track_id, {})
    gj = _json(DATA / "geojson" / "f1-circuits.geojson", {"features": []})
    feat = next((f for f in gj["features"] if f["properties"]["id"] == cat["bacinger"]), None)
    tel = _telemetry_lap(track_id)
    notes = []
    if feat is not None:
        ll = np.asarray(feat["geometry"]["coordinates"], float)[:, :2]
        lat0, lon0 = np.mean(ll[:, 1]), np.mean(ll[:, 0])
        # equirectangular projection about the centroid
        pts = np.column_stack([np.radians(ll[:, 0] - lon0) * R_EARTH * np.cos(np.radians(lat0)),
                               np.radians(ll[:, 1] - lat0) * R_EARTH])
        length = float(feat["properties"].get("length") or 0) or None
        source = "bacinger/f1-circuits"
        n_ctrl = len(ll)
    elif tel is not None:
        # Fallback (§3.1): build the centerline from public position data of a fastest lap.
        step = max(1, len(tel["x"]) // 400)
        pts = np.column_stack([tel["x"][::step], tel["y"][::step]]) * 0.1
        length = float(tel["dist"][-1])
        source = "telemetry X/Y fallback (racing line, not a surveyed centerline)"
        n_ctrl = len(pts)
        notes.append("Centerline built from telemetry positions because the circuit is missing from bacinger.")
    else:
        raise FileNotFoundError(f"No geometry for '{track_id}'. Run: python scripts/fetch_data.py")

    tr = _spline_resample(pts, length)
    align = None
    if tel is not None and feat is not None:
        try:
            al = _align(tr, tel["x"], tel["y"])
            if al["rms"] < 60.0:
                if al["reverse"] or al["mirror"]:
                    # digitised opposite to the driving direction (mirror should never happen; treat as reverse)
                    tr = _spline_resample(pts[::-1], length)
                    al = _align(tr, tel["x"], tel["y"])
                i0 = int(al["idx"][0])  # telemetry lap starts on the start/finish line
                for k in ("x", "y", "psi", "kappa"):
                    tr[k] = np.roll(tr[k], -i0)
                tr["psi"] = np.unwrap(tr["psi"])
                idx = (al["idx"] - i0) % len(tr["s"])
                align = {"rms_m": round(al["rms"], 2), "scale": al["scale"], "idx": idx, "team": tel["team"],
                         "lap_time": tel["lap"], "code": tel["code"]}
            else:
                notes.append(f"Telemetry alignment rejected (RMS {al['rms']:.0f} m).")
        except Exception as e:  # noqa: BLE001
            notes.append(f"Telemetry alignment failed: {e}")
    n = len(tr["s"])
    tr["nx"], tr["ny"] = -np.sin(tr["psi"]), np.cos(tr["psi"])  # left-pointing unit normal

    # width (§3.5.5): TUM mean -> override -> type default
    tum = (_json(DATA / "tum_widths.json", {}) or {}).get("widths", {})
    if "width_m" in ov:
        width, wsrc = float(ov["width_m"]), "override"
    elif track_id in tum:
        width, wsrc = float(np.clip(tum[track_id], 9.0, 16.0)), "TUM mean (public-derived)"
    else:
        width, wsrc = (11.0, "street default (estimate)") if cat["type"] == "street" else (14.0, "permanent default (estimate)")
    tr["width"] = width

    # elevation (§3.5.6): flat unless telemetry Z is available
    tr["z"] = np.zeros(n)
    tr["grade"] = np.zeros(n)
    tr["elevation_source"] = "flat"
    if align is not None and ov.get("elevation", True):
        zt = tel["z"] * align["scale"]  # telemetry units -> metres via the fitted similarity scale
        if np.ptp(zt) > 0.5:
            acc, cnt = np.zeros(n), np.zeros(n)
            np.add.at(acc, align["idx"], zt)
            np.add.at(cnt, align["idx"], 1)
            have = cnt > 0
            zi = np.interp(np.arange(n), np.flatnonzero(have), acc[have] / cnt[have], period=n)
            z = _smooth_periodic(zi, max(8, int(tr["length"] / 220)))
            z -= z.min()
            tr["z"] = z
            tr["grade"] = np.gradient(np.concatenate([z[-2:], z, z[:2]]), tr["ds"])[2:-2]
            tr["elevation_source"] = "public-derived (telemetry Z, smoothed)"
    _zones(tr, cat, ov)
    tr.update({
        "id": track_id, "name": cat["name"], "type": cat["type"], "night": cat["night"], "laps": cat["laps"],
        "pit_loss_s": cat["pit_loss_s"], "overtake_difficulty": cat["overtake_difficulty"],
        "official_length": length, "source": source, "width_source": wsrc, "n_control_points": n_ctrl,
        "align_rms_m": align["rms_m"] if align else None,
        "start_line": "public-derived (telemetry)" if align else "as digitised (estimate)",
        "notes": notes,
    })
    if n_ctrl and tr["length"] / n_ctrl > 45:
        tr["notes"].append("Sparse centerline: tight chicanes are smoothed and may run slightly fast.")
    return tr


def track_summary(track_id: str) -> dict:
    tr = load_track(track_id)
    return {k: tr[k] for k in ("id", "name", "type", "night", "laps", "length", "official_length", "width",
                               "width_source", "source", "align_rms_m", "start_line", "elevation_source",
                               "notes", "pit_loss_s", "overtake_difficulty")} | {
        "n_corners": len(tr["corners"]), "elevation_range_m": float(np.ptp(tr["z"]))}


def offset_path(tr: dict, alpha: np.ndarray):
    """x, y of a path offset laterally by alpha [m] (positive = left) from the centerline."""
    return tr["x"] + alpha * tr["nx"], tr["y"] + alpha * tr["ny"]

"""Telemetry-to-centerline alignment report for every calendar track (brief §12, stage 1).

For each track with cached quali telemetry (data/public/2026/<NN>/tel_quali.npz) this runs the same
alignment as f1sim.tracks.load_track (FFT cross-correlation over start offset / direction / handedness,
then ICP) and reports the RMS offset, the driving direction relative to the bacinger digitisation and
the start/finish offset along it. Writes data/alignment_report.json and prints a table.

    .venv/bin/python scripts/alignment_report.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from f1sim import tracks as T  # noqa: E402

OUT = ROOT / "data" / "alignment_report.json"
FLAG_RMS_M = 10.0
REJECT_RMS_M = 60.0  # load_track drops alignments above this


def _bacinger_points(cat: dict):
    gj = T._json(T.DATA / "geojson" / "f1-circuits.geojson", {"features": []})
    feat = next((f for f in gj["features"] if f["properties"]["id"] == cat["bacinger"]), None)
    if feat is None:
        return None, None
    ll = np.asarray(feat["geometry"]["coordinates"], float)[:, :2]
    lat0, lon0 = np.mean(ll[:, 1]), np.mean(ll[:, 0])
    pts = np.column_stack([np.radians(ll[:, 0] - lon0) * T.R_EARTH * np.cos(np.radians(lat0)),
                           np.radians(ll[:, 1] - lat0) * T.R_EARTH])
    return pts, float(feat["properties"].get("length") or 0) or None


def align_track(track_id: str) -> dict:
    cat = next(t for t in T.catalog() if t["id"] == track_id)
    row = {"track": track_id, "name": cat["name"], "round": None, "status": "", "rms_m": None,
           "p95_m": None, "max_m": None, "direction": None, "start_offset_m": None, "start_offset_frac": None,
           "scale": None, "tel_team": None, "tel_driver": None, "tel_samples": None, "load_track_rms_m": None}
    rd = T._round_dir(track_id)
    row["round"] = int(rd.name) if rd else None
    tel = T._telemetry_lap(track_id)
    if tel is None:
        row["status"] = "no telemetry cached (round not run yet)"
        return row
    pts, length = _bacinger_points(cat)
    if pts is None:
        row["status"] = "no bacinger centerline (telemetry fallback geometry, nothing to align against)"
        return row
    tr = T._spline_resample(pts, length)
    al = T._align(tr, tel["x"], tel["y"])
    reverse = bool(al["reverse"] or al["mirror"])
    if al["rms"] < REJECT_RMS_M and reverse:
        tr = T._spline_resample(pts[::-1], length)
        al = T._align(tr, tel["x"], tel["y"])
    d = np.sqrt((al["xy"].real - tr["x"][al["idx"]]) ** 2 + (al["xy"].imag - tr["y"][al["idx"]]) ** 2)
    i0 = int(al["idx"][0])
    row.update({
        "rms_m": round(al["rms"], 2), "p95_m": round(float(np.percentile(d, 95)), 2),
        "max_m": round(float(d.max()), 2),
        "direction": "reversed vs digitisation" if reverse else "as digitised",
        "mirror_detected": bool(al["mirror"]),
        "start_offset_m": round(i0 * tr["ds"], 1), "start_offset_frac": round(i0 / len(tr["s"]), 4),
        "scale": round(al["scale"], 5), "tel_team": tel["team"], "tel_driver": tel["code"],
        "tel_samples": int(len(tel["x"])),
        "status": "rejected (RMS > 60 m)" if al["rms"] >= REJECT_RMS_M else
                  ("FLAG: RMS > 10 m" if al["rms"] > FLAG_RMS_M else "ok"),
    })
    try:
        row["load_track_rms_m"] = T.load_track(track_id)["align_rms_m"]
    except Exception as e:  # noqa: BLE001
        row["load_track_rms_m"] = None
        row["status"] += f"; load_track failed: {e}"
    return row


def main() -> int:
    t0 = time.time()
    rows = [align_track(t["id"]) for t in T.track_list()]
    aligned = [r for r in rows if r["rms_m"] is not None]
    flagged = [r["track"] for r in aligned if r["rms_m"] > FLAG_RMS_M]
    rep = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": "FFT cross-correlation of 512-point complex sequences (all start offsets, both directions, "
                  "both handedness; closed-form similarity per shift) refined by 3 ICP iterations "
                  "(f1sim.tracks._align). RMS = nearest-centerline-sample distance over all telemetry samples.",
        "telemetry": "fastest public quali lap with X/Y position (OpenF1 location, or FastF1 if logged in)",
        "flag_threshold_m": FLAG_RMS_M,
        "n_tracks": len(rows), "n_aligned": len(aligned),
        "rms_median_m": round(float(np.median([r["rms_m"] for r in aligned])), 2) if aligned else None,
        "rms_max_m": max((r["rms_m"] for r in aligned), default=None),
        "flagged": flagged,
        "tracks": rows,
    }
    OUT.write_text(json.dumps(rep, indent=1))

    hdr = f"{'Rd':>2}  {'Track':<14} {'RMS m':>6} {'p95 m':>6} {'max m':>6}  {'Direction':<25} {'Start m':>8}  {'Tel':<4} Status"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        f = lambda v, w, p=1: f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'-':>{w}}"  # noqa: E731
        print(f"{r['round'] or 0:>2}  {r['track']:<14} {f(r['rms_m'], 6, 2)} {f(r['p95_m'], 6)} {f(r['max_m'], 6)}"
              f"  {(r['direction'] or '-'):<25} {f(r['start_offset_m'], 8)}  {(r['tel_driver'] or '-'):<4} {r['status']}")
    print(f"\n{len(aligned)}/{len(rows)} aligned; median RMS {rep['rms_median_m']} m, max {rep['rms_max_m']} m; "
          f"flagged (> {FLAG_RMS_M:.0f} m): {', '.join(flagged) or 'none'}  [{time.time() - t0:.1f} s]")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

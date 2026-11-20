"""Run the preset calibration against the cached public data and print a summary.

    .venv/bin/python scripts/calibrate_presets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from f1sim.calibration.fit import run_calibration  # noqa: E402


def main() -> int:
    last = [-1.0]

    def progress(frac, msg=None):
        if frac - last[0] >= 0.05 or frac >= 1.0:
            last[0] = frac
            print(f"[{frac * 100:3.0f}%] {msg or ''}", flush=True)

    rep = run_calibration(progress=progress)
    print("\nglobal:", rep["global"])
    print(f"\n{'team':14s} {'target%':>8s} {'sim%':>8s} {'resid%':>7s} {'stand':>5s} {'rank':>4s}")
    for t in rep["teams"]:
        print(f"{t['team']:14s} {t['target_gap_pct']:8.2f} {t['sim_gap_pct']:8.2f} {t['residual_pct']:7.2f} "
              f"{t['standing_pos'] or '-':>5} {t['pace_rank']:>4d}")
    import json
    val = json.loads((Path(__file__).resolve().parent.parent / "data/presets/_validation.json").read_text())
    print(f"\n{'track':14s} {'real':>8s} {'sim':>8s} {'err%':>6s}")
    for v in val:
        print(f"{v['track']:14s} {v['real_pole']:8.3f} {v['sim_pole']:8.3f} {v['err_pct']:6.2f}{' *' if v.get('fitted') else ''}")
    print("\n".join(rep["notes"]), f"\nruntime {rep['runtime_s']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

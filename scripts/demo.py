"""Test-run the simulator without the browser: one qualifying lap and one full 22-car race.

    .venv/bin/python scripts/demo.py [track_id] [--laps N] [--seed S]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from f1sim import drivers, solver  # noqa: E402
from f1sim.calibration.fit import quali_state  # noqa: E402
from f1sim.car import params  # noqa: E402
from f1sim.optimise import get_path  # noqa: E402
from f1sim.race.race_sim import run_race  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:06.3f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("track", nargs="?", default="monza")
    ap.add_argument("--laps", type=int, default=None, help="race distance (default: the real lap count)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    teams = json.loads((ROOT / "data/teams.json").read_text())

    print(f"== Qualifying lap at {a.track} (Ferrari, Leclerc)")
    t0 = time.time()
    path = get_path(a.track)  # builds and caches the racing line on first use (a few seconds)
    car = params.make_car("ferrari", {})
    r = solver.solve(path, car, drivers.get_driver("leclerc"), quali_state(a.track), quali=True, thermal=True,
                     detail=False)
    print(f"   lap {fmt(r['lap_time'])}   top speed {r['vmax'] * 3.6:.0f} km/h   ({time.time() - t0:.1f} s)")

    entries = [{"team": t["id"], "driver": d} for t in teams for d in t["drivers"]]
    print(f"== Race at {a.track}: {len(entries)} cars, seed {a.seed}")
    t0 = time.time()
    res = run_race(a.track, entries, laps=a.laps, seed=a.seed)
    print(f"   {res['laps']} laps simulated in {time.time() - t0:.1f} s, "
          f"{len(res.get('overtakes', []))} overtakes")
    for row in res["classification"][:10]:
        gap = "leader" if row["pos"] == 1 else (f"+{row['gap']:.3f}" if row["gap"] is not None else row["status"])
        print(f"   P{row['pos']:<2} {row['code']}  {row['team']:<13} grid {row['grid']:<2} stops {row['stops']}  {gap}")
    fl = res.get("fastest_lap")
    if fl:
        print(f"   fastest lap: {fl['code']} {fmt(fl['time'])} (lap {fl['lap']})")


if __name__ == "__main__":
    main()

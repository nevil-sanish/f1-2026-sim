"""Calibration checks: presets exist and load, the fitted global car is close to real poles, team order holds."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from f1sim import solver
from f1sim.calibration import fit
from f1sim.car import params
from f1sim.drivers import get_driver
from f1sim.optimise import get_path

PRESETS = Path(__file__).resolve().parent.parent / "data" / "presets"
TEAMS = ["mercedes", "ferrari", "mclaren", "red_bull", "racing_bulls", "alpine", "audi", "haas", "williams",
         "cadillac", "aston_martin"]


class Presets(unittest.TestCase):
    def test_files_and_loading(self):
        for name in ["_global", "_report", "_validation"] + TEAMS:
            self.assertTrue((PRESETS / f"{name}.json").exists(), name)
        for t in TEAMS:
            pre = params.load_preset(t)
            self.assertTrue(pre["calibrated"])
            car = params.make_car(t)
            for k in params.TEAM_FIT_KEYS:
                self.assertIn(k, pre["values"] if k != "tyre_wear_scale" else {k: 1})
                self.assertGreaterEqual(car[k], params.BY_KEY[k]["min"])
                self.assertLessEqual(car[k], params.BY_KEY[k]["max"])
        self.assertEqual(params.make_car(None)["mu_scale"], json.loads((PRESETS / "_global.json").read_text())["values"]["mu_scale"])

    def test_wet_sessions_skipped(self):
        self.assertNotIn("interlagos", fit.load_sessions())


class Pace(unittest.TestCase):
    def test_global_car_within_3pct_of_pole(self):
        sessions = fit.load_sessions()
        car = params.make_car(None)
        drv = get_driver(None)
        for tid in ("monza", "silverstone", "suzuka"):
            r = solver.solve(get_path(tid), car, drv, dict(sessions[tid]["state"]), quali=True, thermal=True, detail=False)
            err = 100.0 * (r["lap_time"] / sessions[tid]["pole"] - 1.0)
            with self.subTest(track=tid):
                self.assertLess(abs(err), 3.0, f"{tid}: {err:.2f}%")

    def test_team_order_roughly_matches_standings(self):
        rep = json.loads((PRESETS / "_report.json").read_text())
        gap = {t["team"]: t["sim_gap_pct"] for t in rep["teams"]}
        self.assertLess(gap["mercedes"], gap["racing_bulls"])
        self.assertLess(gap["ferrari"], gap["alpine"])
        self.assertLess(gap["racing_bulls"], gap["cadillac"])
        self.assertLess(gap["williams"], gap["cadillac"] + 1e-9)
        for t in rep["teams"]:
            self.assertLess(abs(t["residual_pct"]), 0.5, t["team"])
        # the front-runners really are faster than the back of the grid in the simulator
        top = [params.make_car(t) for t in ("mercedes", "ferrari")]
        back = params.make_car("aston_martin")
        sess = fit.load_sessions()["monza"]
        path, drv = get_path("monza"), get_driver(None)
        lap = lambda c: solver.solve(path, c, drv, dict(sess["state"]), quali=True, thermal=False, detail=False)["lap_time"]  # noqa: E731
        self.assertLess(max(lap(c) for c in top), lap(back) - 1.0)


if __name__ == "__main__":
    unittest.main()

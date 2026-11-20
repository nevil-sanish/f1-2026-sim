"""Driver-layer acceptance check (brief §12.8): same car, different driver -> small deltas (~0.1-0.5 s).

The best and worst rated current drivers (by the three pace ratings the solver uses: grip_use,
brake_commit, throttle_commit) drive the identical baseline car on one track of each kind: a low-drag
power track (Monza), a slow street circuit (Monaco) and a high-speed permanent circuit (Silverstone).
"""
from __future__ import annotations

import unittest

from f1sim import solver
from f1sim.car import params
from f1sim.drivers import driver_list, get_driver
from f1sim.optimise import get_path

TRACKS = ("monza", "monaco", "silverstone")
DELTA_MIN_S, DELTA_MAX_S = 0.1, 0.5
PACE_KEYS = ("grip_use", "brake_commit", "throttle_commit")


def pace_score(d: dict) -> float:
    return sum(d["ratings"][k] for k in PACE_KEYS)


class SameCarDifferentDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rated = [d for d in driver_list() if d.get("current") and d.get("rated")]
        rated.sort(key=pace_score)
        cls.worst, cls.best = rated[0]["id"], rated[-1]["id"]
        car = params.make_car(None, {})
        cls.laps = {}
        for tid in TRACKS:
            path = get_path(tid)
            cls.laps[tid] = {d: solver.solve(path, car, get_driver(d), None, quali=True, thermal=False,
                                             detail=False)["lap_time"] for d in (cls.best, cls.worst)}

    def test_best_and_worst_are_distinct(self):
        self.assertNotEqual(self.best, self.worst)

    def test_best_driver_is_faster(self):
        for tid in TRACKS:
            with self.subTest(track=tid):
                self.assertLess(self.laps[tid][self.best], self.laps[tid][self.worst])

    def test_delta_in_band(self):
        for tid in TRACKS:
            dt = self.laps[tid][self.worst] - self.laps[tid][self.best]
            with self.subTest(track=tid, best=self.best, worst=self.worst, delta_s=round(dt, 3)):
                self.assertGreaterEqual(dt, DELTA_MIN_S)
                self.assertLessEqual(dt, DELTA_MAX_S)


if __name__ == "__main__":
    unittest.main()

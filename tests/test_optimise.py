"""Setup optimiser acceptance checks (brief §12.10). Small parameter sets and budgets keep this fast."""
from __future__ import annotations

import json
import unittest

from f1sim import optimise as opt
from f1sim.car import params

TRACK, DRIVER = "monza", "norris"
KEYS = ["rear_wing", "brake_bias", "arb_f"]


def on_step_grid(key: str, value: float) -> bool:
    p = params.BY_KEY[key]
    k = (value - p["min"]) / p["step"]
    return abs(k - round(k)) < 1e-6


class OptimiseResult(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.progress = []
        cls.res = opt.optimise(TRACK, None, DRIVER, params=KEYS, objective="quali", max_evals=45,
                               progress=lambda f, m: cls.progress.append((f, m)))

    def test_shape(self):
        for k in ("objective", "before", "after", "gain", "best", "sensitivities", "evaluations"):
            self.assertIn(k, self.res)
        self.assertEqual(self.res["objective"], "quali")
        self.assertEqual(list(self.res["best"]), KEYS)
        self.assertEqual([s["key"] for s in self.res["sensitivities"]], KEYS)
        for s in self.res["sensitivities"]:
            sch = params.BY_KEY[s["key"]]
            self.assertEqual((s["label"], s["unit"], s["step"]), (sch["label"], sch["unit"], sch["step"]))
            self.assertIsInstance(s["delta_per_step"], float)

    def test_never_worse_than_start(self):
        self.assertLessEqual(self.res["after"], self.res["before"])
        self.assertAlmostEqual(self.res["gain"], self.res["before"] - self.res["after"], places=3)
        self.assertGreaterEqual(self.res["gain"], 0.0)

    def test_after_is_reproducible_from_best(self):
        """`after` is the objective of the returned setup, not of some unrounded intermediate."""
        car = params.make_car(None, self.res["best"])
        t = opt.lap_objective(opt.get_path(TRACK), car, opt.get_driver(DRIVER), "quali")
        self.assertAlmostEqual(t, self.res["after"], places=3)

    def test_bounds_and_steps(self):
        for k, v in self.res["best"].items():
            p = params.BY_KEY[k]
            self.assertGreaterEqual(v, p["min"])
            self.assertLessEqual(v, p["max"])
            self.assertTrue(on_step_grid(k, v), f"{k}={v} is off the schema step grid")

    def test_budget_and_progress(self):
        # search budget + starting setup + at most 2 per parameter for the sensitivity table
        self.assertLessEqual(self.res["evaluations"], 45 + 1 + 2 * len(KEYS))
        fr = [f for f, _ in self.progress]
        self.assertEqual(fr, sorted(fr))
        self.assertEqual(fr[-1], 1.0)
        self.assertTrue(all(0.0 <= f <= 1.0 for f in fr))

    def test_json_serialisable(self):
        self.assertEqual(json.loads(json.dumps(self.res))["best"], self.res["best"])


class OptimiseBehaviour(unittest.TestCase):
    def test_race_objective_never_worse(self):
        res = opt.optimise(TRACK, None, DRIVER, params=["rear_wing", "pressure_r"], objective="race", max_evals=12)
        self.assertEqual(res["objective"], "race")
        self.assertLessEqual(res["after"], res["before"])
        json.dumps(res)
        # stint average on race fuel with worn tyres is slower than a quali lap
        quali = opt.lap_objective(opt.get_path(TRACK), params.make_car(None), opt.get_driver(DRIVER), "quali")
        self.assertGreater(res["before"], quali)

    def test_falls_back_to_start_when_nothing_better(self):
        """A one-evaluation budget cannot beat a start that sits off the step grid: the start is returned."""
        res = opt.optimise(TRACK, None, DRIVER, overrides={"rear_wing": 22.2}, params=["rear_wing"], max_evals=1)
        self.assertLessEqual(res["after"], res["before"])
        if res["gain"] == 0.0:
            self.assertEqual(res["best"], {"rear_wing": 22.2})

    def test_overrides_are_the_starting_setup(self):
        res = opt.optimise(TRACK, None, DRIVER, overrides={"rear_wing": 30.0, "front_wing": 30.0},
                           params=["rear_wing"], max_evals=4)
        self.assertEqual(res["start"], {"rear_wing": 30.0})
        self.assertNotIn("front_wing", res["best"])

    def test_parameter_selection(self):
        keys, skipped = opt.select_params(None)
        self.assertEqual(skipped, [])
        protected = set(params.TEAM_FIT_KEYS) | set(params.GLOBAL_FIT_KEYS) | {"mass", "mguk_power"}
        self.assertFalse(protected & set(keys))
        for k in keys:
            self.assertEqual(params.BY_KEY[k]["kind"], "float")
            self.assertNotEqual(params.BY_KEY[k]["prov"], params.REG)
        # choice / bool / unknown keys are dropped, protected keys are allowed only when named explicitly
        keys, skipped = opt.select_params(["rear_wing", "compound", "blankets", "nope", "ClA_scale", "rear_wing"])
        self.assertEqual(keys, ["rear_wing", "ClA_scale"])
        self.assertEqual(skipped, ["compound", "blankets", "nope"])
        with self.assertRaises(ValueError):
            opt.optimise(TRACK, None, DRIVER, params=["compound"])
        with self.assertRaises(ValueError):
            opt.optimise(TRACK, None, DRIVER, params=KEYS, objective="fastest")


class SensitivitySigns(unittest.TestCase):
    """Signs that physics requires at the baseline setup on a power circuit (seconds per schema step)."""

    def test_quali(self):
        keys = ["rear_wing", "CdA_scale", "ice_power", "mass_above_min", "quali_fuel"]
        s = {r["key"]: r["delta_per_step"] for r in opt.sensitivities(TRACK, None, DRIVER, params=keys)}
        self.assertGreater(s["rear_wing"], 0.0, "more rear wing must cost lap time at Monza (drag on the straights)")
        self.assertGreater(s["CdA_scale"], 0.0, "more drag must cost lap time")
        self.assertLess(s["ice_power"], 0.0, "more power must gain lap time")
        self.assertGreater(s["mass_above_min"], 0.0, "more mass must cost lap time")
        self.assertGreater(s["quali_fuel"], 0.0, "more fuel must cost lap time")

    def test_race(self):
        keys = ["CdA_scale", "ice_power", "fuel_load", "tyre_wear_scale"]
        s = {r["key"]: r["delta_per_step"]
             for r in opt.sensitivities(TRACK, None, DRIVER, params=keys, objective="race")}
        self.assertGreater(s["CdA_scale"], 0.0)
        self.assertLess(s["ice_power"], 0.0)
        self.assertGreater(s["fuel_load"], 0.0, "a heavier stint must be slower on average")
        self.assertGreater(s["tyre_wear_scale"], 0.0, "faster wear must slow the stint average")


if __name__ == "__main__":
    unittest.main()

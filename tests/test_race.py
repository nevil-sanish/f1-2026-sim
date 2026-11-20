"""Race simulator tests: determinism, classification, pit stops, Monte Carlo, strategy trends.

Lap times are not asserted exactly (car calibration changes); only structure and relative behaviour.
"""
import json
import unittest
from unittest import mock

from f1sim.race import montecarlo, race_sim

TRACK = "monza"
LAPS = 18


def entry(team, driver, strategy="auto", **driver_overrides):
    return {"team": team, "driver": driver, "overrides": {}, "driver_overrides": driver_overrides,
            "strategy": strategy}


FIELD = [entry("mercedes", "russell"), entry("ferrari", "leclerc"), entry("mclaren", "norris"),
         entry("red_bull", "max_verstappen"), entry("alpine", "gasly"), entry("williams", "albon")]


def stops_of(res):
    return {c["code"]: c["stops"] for c in res["classification"]}


class RaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = race_sim.prepare(TRACK, FIELD, LAPS)       # builds and caches the pace surfaces once

    def run_race(self, seed=1, **kw):
        kw.setdefault("laps", LAPS)
        return race_sim.run_race(TRACK, FIELD, seed=seed, **kw)

    def test_deterministic_per_seed(self):
        a, b = self.run_race(seed=3), self.run_race(seed=3)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        c = self.run_race(seed=4)
        self.assertNotEqual(a["lap_times"], c["lap_times"])

    def test_result_shape_and_classification(self):
        r = self.run_race(seed=2)
        n = len(FIELD)
        for k in ("track", "laps", "lap_length", "entries", "classification", "fastest_lap", "lap_chart", "gaps",
                  "lap_times", "strategy", "energy", "overtakes", "events", "replay", "line", "monte_carlo"):
            self.assertIn(k, r)
        json.dumps(r)
        cl = r["classification"]
        self.assertEqual([c["pos"] for c in cl], list(range(1, n + 1)))
        self.assertEqual(sorted(c["code"] for c in cl), sorted(e["code"] for e in r["entries"]))
        for c in cl:
            self.assertTrue(c["status"] == "DNF" or c["status"] == "FIN" or c["status"].startswith("+"), c)
        self.assertEqual(cl[0]["status"], "FIN")
        self.assertEqual(cl[0]["gap"], 0.0)
        fin = [c for c in cl if c["status"] == "FIN"]
        self.assertEqual([c["time"] for c in fin], sorted(c["time"] for c in fin))
        for code, series in r["lap_chart"].items():
            self.assertEqual(len(series), LAPS + 1)
        for code, pos in ((c["code"], c["pos"]) for c in cl):
            self.assertEqual(r["lap_chart"][code][-1], pos)
        rp = r["replay"]
        self.assertEqual(len(rp["cars"]), n)
        for car in rp["cars"].values():
            for k in ("d", "pit", "speed", "compound_idx"):
                self.assertEqual(len(car[k]), len(rp["t"]))
            d = car["d"]
            self.assertTrue(all(b >= a - 1e-6 for a, b in zip(d, d[1:])), "replay distance must not go backwards")
        win = cl[0]["code"]
        self.assertAlmostEqual(rp["cars"][win]["d"][-1], LAPS * r["lap_length"], places=0)

    def test_all_cars_classified_with_dnfs(self):
        with mock.patch.object(race_sim, "P_DNF", 0.04):
            r = self.run_race(seed=5)
        cl = r["classification"]
        dnfs = [c for c in cl if c["status"] == "DNF"]
        self.assertTrue(dnfs)
        self.assertEqual(len(cl), len(FIELD))
        first_dnf = min(i for i, c in enumerate(cl) if c["status"] == "DNF")
        self.assertTrue(all(c["status"] == "DNF" for c in cl[first_dnf:]), "DNFs are classified after finishers")
        self.assertEqual({e["code"] for e in r["events"] if e["kind"] == "DNF"}, {c["code"] for c in dnfs})

    def test_dnf_and_safety_car_can_be_disabled(self):
        with mock.patch.object(race_sim, "P_DNF", 0.5), mock.patch.object(race_sim, "P_SC", 0.5):
            r = self.run_race(seed=6, options={"dnf": False, "safety_car": False})
        self.assertFalse([c for c in r["classification"] if c["status"] == "DNF"])
        self.assertFalse([e for e in r["events"] if e["kind"] in ("SC", "VSC", "DNF")])

    def test_safety_car_slows_the_laps(self):
        with mock.patch.object(race_sim, "P_SC", 0.3):
            r = self.run_race(seed=7, options={"dnf": False})
        sc = [e for e in r["events"] if e["kind"] == "SC"]
        self.assertTrue(sc)
        lap = sc[0]["lap"]
        lead = r["classification"][0]["code"]
        times = r["lap_times"][lead]
        green = min(times[1:])
        self.assertGreater(times[lap - 1], 1.2 * green)
        self.assertEqual(len(r["classification"]), len(FIELD))

    def test_pit_stops_occur_and_use_two_compounds(self):
        r = self.run_race(seed=8, options={"dnf": False, "safety_car": False})
        pits = [e for e in r["events"] if e["kind"] == "PIT"]
        self.assertTrue(pits)
        for c in r["classification"]:
            self.assertGreaterEqual(c["stops"], 1, c)
            comps = {s["compound"] for s in r["strategy"][c["code"]]}
            self.assertGreaterEqual(len(comps), 2, "two-compound rule")
            self.assertEqual(len(r["strategy"][c["code"]]), c["stops"] + 1)
        stint = r["strategy"][r["classification"][0]["code"]]
        self.assertEqual(stint[0]["start_lap"], 1)
        self.assertEqual(stint[-1]["end_lap"], LAPS)

    def test_fixed_strategy_is_followed(self):
        entries = [dict(e) for e in FIELD]
        entries[0]["strategy"] = {"start_compound": "C3", "stops": [{"lap": 7, "compound": "C4"}, {"lap": 13, "compound": "C3"}]}
        r = race_sim.run_race(TRACK, entries, laps=LAPS, seed=9, options={"dnf": False, "safety_car": False})
        code = r["entries"][0]["code"]
        st = r["strategy"][code]
        self.assertEqual([(s["compound"], s["start_lap"]) for s in st], [("C3", 1), ("C4", 8), ("C3", 14)])
        self.assertEqual([c for c in r["classification"] if c["code"] == code][0]["stops"], 2)

    def test_grid_list_sets_the_order(self):
        order = ["albon", "gasly", "norris", "russell", "leclerc", "max_verstappen"]
        r = self.run_race(seed=10, grid=order)
        by_driver = {e["driver"]: e["grid"] for e in r["entries"]}
        self.assertEqual([by_driver[d] for d in order], [1, 2, 3, 4, 5, 6])
        self.assertEqual(r["lap_chart"][r["entries"][5]["code"]][0], 1)

    def test_lap_times_are_consistent(self):
        r = self.run_race(seed=11, options={"dnf": False})
        win = r["classification"][0]
        self.assertAlmostEqual(sum(r["lap_times"][win["code"]]), win["time"], places=1)

    def test_fastest_lap_is_the_quickest_green_lap(self):
        r = self.run_race(seed=11, options={"dnf": False, "safety_car": False})
        best = min(t for v in r["lap_times"].values() for t in v[1:])
        self.assertAlmostEqual(r["fastest_lap"]["time"], best, places=3)
        self.assertAlmostEqual(r["lap_times"][r["fastest_lap"]["code"]][r["fastest_lap"]["lap"] - 1], best, places=3)

    def test_single_race_is_fast(self):
        import time
        t = time.time()
        race_sim.run_race(TRACK, FIELD, laps=None, seed=12)
        self.assertLess(time.time() - t, 3.0)


class MonteCarloTests(unittest.TestCase):
    ENTRIES = [entry("mercedes", "russell", grip_use=1.0), entry("mercedes", "russell", grip_use=0.95),
               entry("ferrari", "leclerc"), entry("mclaren", "norris"), entry("alpine", "gasly"),
               entry("williams", "albon")]

    @classmethod
    def setUpClass(cls):
        cls.res = montecarlo.run_monte_carlo(TRACK, cls.ENTRIES, laps=LAPS, k=40, seed=1,
                                             options={"dnf": False, "safety_car": False})

    def test_table_is_a_distribution(self):
        mc = self.res["monte_carlo"]
        self.assertEqual(mc["runs"], 40)
        t = mc["table"]
        self.assertEqual(len(t), len(self.ENTRIES))
        self.assertAlmostEqual(sum(r["win"] for r in t), 1.0, places=6)
        self.assertAlmostEqual(sum(r["podium"] for r in t), 3.0, places=6)
        for r in t:
            self.assertAlmostEqual(sum(r["dist"]), 1.0, places=6)
            self.assertEqual(len(r["dist"]), len(self.ENTRIES))
            self.assertGreaterEqual(r["points"], r["podium"] - 1e-9)

    def test_faster_car_wins_more_often(self):
        by = {r["code"]: r for r in self.res["monte_carlo"]["table"]}
        fast, slow = self.res["entries"][0]["code"], self.res["entries"][1]["code"]
        self.assertLess(by[fast]["avg_finish"], by[slow]["avg_finish"])
        self.assertGreaterEqual(by[fast]["win"], by[slow]["win"])

    def test_deterministic(self):
        again = montecarlo.run_monte_carlo(TRACK, self.ENTRIES, laps=LAPS, k=40, seed=1,
                                           options={"dnf": False, "safety_car": False})
        self.assertEqual(again["monte_carlo"], self.res["monte_carlo"])


class StrategyTrendTests(unittest.TestCase):
    def mean_stops(self, deg):
        opts = {"dnf": False, "safety_car": False, "deg_scale": deg}
        ctx = race_sim.prepare(TRACK, FIELD, 40, opts)
        tot = []
        for seed in range(4):
            raw = race_sim.simulate(ctx, "quali", opts, seed)
            tot += [len(s) - 1 for s in raw["stints"]]
        return sum(tot) / len(tot)

    def test_more_degradation_means_more_stops(self):
        self.assertLess(self.mean_stops(0.5), self.mean_stops(6.0))


if __name__ == "__main__":
    unittest.main()

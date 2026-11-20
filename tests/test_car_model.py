"""Acceptance checks for the car model and lap solver (brief §12.4 and §12.5).

Only signs and trends are asserted, never absolute lap times: the calibration is tuned separately.
"""
from __future__ import annotations

import unittest

import numpy as np

from f1sim import solver
from f1sim.car import aero, params, powertrain, vehicle
from f1sim.drivers import get_driver
from f1sim.optimise import get_path

VG = vehicle.VG
KMH = 3.6
CORNER_SPEEDS = (40, 50, 60, 70)     # m/s: medium and fast corners where downforce matters


def car(**overrides) -> dict:
    return params.make_car(None, overrides)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = get_driver("norris")
        cls.monza = get_path("monza")
        cls.monaco = get_path("monaco")

    def lap(self, path, state=None, quali=True, thermal=False, **overrides) -> dict:
        return solver.solve(path, car(**overrides), self.driver, state, quali=quali, thermal=thermal, detail=False)


class LoadTransferBalanceTyresBrakes(_Base):
    """§12.4"""

    def test_more_rear_wing_lowers_top_speed(self):
        vmax = [self.lap(self.monza, rear_wing=rw)["vmax"] for rw in (14, 22, 30)]
        self.assertGreater(vmax[0], vmax[1])
        self.assertGreater(vmax[1], vmax[2])

    def test_more_rear_wing_raises_drag_and_rear_downforce(self):
        lo, hi = (aero.aero_tables(car(rear_wing=rw), VG, False) for rw in (18, 26))
        self.assertTrue(np.all(hi["drag"][20:] > lo["drag"][20:]))
        self.assertTrue(np.all(hi["Fr"][20:91] > lo["Fr"][20:91]))

    def test_more_rear_wing_raises_corner_speed(self):
        """Lateral limit at cornering speeds must rise with rear wing from the baseline setup."""
        base = vehicle.build(car(), self.driver)
        more = vehicle.build(car(rear_wing=params.DEFAULTS["rear_wing"] + 4), self.driver)
        for v in CORNER_SPEEDS:
            with self.subTest(v_ms=v):
                self.assertGreater(more["ay"][0][v], base["ay"][0][v])

    def test_more_wing_at_constant_balance_raises_corner_speed(self):
        """Front and rear wing raised together: more load on both axles, so a higher lateral limit."""
        base = vehicle.build(car(), self.driver)
        more = vehicle.build(car(front_wing=params.DEFAULTS["front_wing"] + 4,
                                 rear_wing=params.DEFAULTS["rear_wing"] + 4), self.driver)
        for v in CORNER_SPEEDS:
            with self.subTest(v_ms=v):
                self.assertGreater(more["ay"][0][v], base["ay"][0][v])

    def test_stiffer_front_arb_gives_more_understeer(self):
        bal = [vehicle.build(car(arb_f=k), self.driver)["balance"][0] for k in (40, 130, 220, 320)]
        for soft, stiff in zip(bal, bal[1:]):
            self.assertTrue(np.all(stiff[20:91] < soft[20:91]))

    def test_stiffer_rear_arb_gives_more_oversteer(self):
        soft, stiff = (vehicle.build(car(arb_r=k), self.driver)["balance"][0] for k in (20, 140))
        self.assertTrue(np.all(stiff[20:91] > soft[20:91]))

    def test_ideal_brake_bias_moves_rearward_with_speed(self):
        V = vehicle.build(car(), self.driver)
        downforce = V["aero"][0]["Ff"] + V["aero"][0]["Fr"]
        ideal = V["ideal_bias"]
        speeds = (20, 35, 50, 65, 80)
        for lo, hi in zip(speeds, speeds[1:]):
            with self.subTest(v_ms=(lo, hi)):
                self.assertGreater(downforce[hi], downforce[lo])
                self.assertLess(ideal[hi], ideal[lo])

    def test_ride_height_below_stall_loses_downforce(self):
        # the gain law itself: rising towards the stall height, collapsing below it
        g_above = float(aero._floor_gain(np.array([aero.H_STALL_F + 0.5]), aero.H_REF_F, aero.H_STALL_F, 0.45)[0])
        g_below = float(aero._floor_gain(np.array([aero.H_STALL_F - 3.0]), aero.H_REF_F, aero.H_STALL_F, 0.45)[0])
        self.assertLess(g_below, g_above)
        # the whole car: slammed static heights stall the floor at speed and lose load vs the baseline
        base = aero.aero_tables(car(), VG, False)
        low = aero.aero_tables(car(ride_height_f=15, ride_height_r=30), VG, False)
        stalled = low["stall_f"] & low["stall_r"] & ~base["stall_f"] & ~base["stall_r"]
        self.assertTrue(stalled.any(), "low ride heights never reach the stall height")
        self.assertTrue(np.all((low["Ff"] + low["Fr"])[stalled] < (base["Ff"] + base["Fr"])[stalled]))
        # before the stall speed the lower car makes more load (ground effect)
        v = 30
        self.assertFalse(low["stall_f"][v] or low["stall_r"][v])
        self.assertGreater(low["Ff"][v] + low["Fr"][v], base["Ff"][v] + base["Fr"][v])


class EnergyAndActiveAero(_Base):
    """§12.5"""

    def test_net_energy_per_lap_within_budget(self):
        for name, path in (("monza", self.monza), ("monaco", self.monaco)):
            for mode, quali in (("quali", True), ("race", False)):
                with self.subTest(track=name, mode=mode):
                    c = car(pu_mode=mode)
                    r = solver.solve(path, c, self.driver, quali=quali, thermal=False, detail=False)
                    self.assertLessEqual(r["e_deploy_mj"], r["e_budget_mj"] + 1e-6)
                    self.assertLessEqual(r["e_harvest_mj"], c["harvest_cap"] + 1e-6)
                    net = r["e_deploy_mj"] - r["e_harvest_mj"]
                    if quali:   # may spend the battery swing once
                        self.assertLessEqual(net, c["battery_swing"] + 1e-6)
                    else:       # race: net zero per lap
                        self.assertLessEqual(net, 1e-6)

    def test_deployment_tapers_above_290(self):
        c = car()
        v = np.arange(0.0, 400.0, 1.0) / KMH
        p = powertrain.deploy_table(c, v)
        full = c["mguk_power"] * 1000.0 * powertrain.ETA_K
        below, above = v * KMH <= 290.0, v * KMH > 290.0
        self.assertTrue(np.allclose(p[below], full))
        self.assertTrue(np.all(p[above] < full))
        self.assertTrue(np.all(np.diff(p[above]) <= 0.0))
        self.assertTrue(np.all(p[v * KMH >= c["deploy_taper_end"]] == 0.0))
        # override mode keeps full power to 337 km/h
        po = powertrain.deploy_table(c, v, override=True)
        self.assertTrue(np.allclose(po[v * KMH <= 337.0], full))
        self.assertTrue(np.all(po >= p))

    def test_override_mode_gains_lap_time(self):
        for name, path in (("monza", self.monza), ("monaco", self.monaco)):
            with self.subTest(track=name):
                normal = self.lap(path, {"fuel": 40.0}, quali=False, pu_mode="race")
                boost = self.lap(path, {"fuel": 40.0, "override": True}, quali=False, pu_mode="race")
                self.assertLess(boost["lap_time"], normal["lap_time"])

    def test_x_mode_sheds_drag(self):
        c = car()
        z, x = aero.aero_tables(c, VG, False), aero.aero_tables(c, VG, True)
        self.assertTrue(np.all(x["drag"][20:] < z["drag"][20:]))


class LapTimeTrends(_Base):
    def test_lap_time_rises_with_fuel_mass(self):
        for thermal in (False, True):
            with self.subTest(thermal=thermal):
                t = [self.lap(self.monza, {"fuel": f}, quali=False, thermal=thermal, pu_mode="race")["lap_time"]
                     for f in (10.0, 40.0, 70.0, 100.0)]
                self.assertEqual(t, sorted(t))
                self.assertGreater(t[-1], t[0])

    def test_lap_time_rises_with_tyre_wear(self):
        for thermal in (False, True):
            with self.subTest(thermal=thermal):
                t = [self.lap(self.monza, {"fuel": 40.0, "wear_f": w, "wear_r": w}, quali=False, thermal=thermal,
                              pu_mode="race")["lap_time"] for w in (0.0, 0.3, 0.6, 0.9)]
                self.assertEqual(t, sorted(t))
                # the cliff: the last step costs more than the first
                self.assertGreater(t[3] - t[2], t[1] - t[0])

    def test_softer_compound_is_faster_on_a_fresh_lap(self):
        """Full model (tyre thermal on), fresh tyres from the blankets, C1 (hardest) .. C5 (softest)."""
        for name, path in (("monza", self.monza), ("monaco", self.monaco)):
            t = [self.lap(path, thermal=True, compound=c)["lap_time"] for c in params.COMPOUNDS]
            for (hard, th), (soft, ts) in zip(zip(params.COMPOUNDS, t), zip(params.COMPOUNDS[1:], t[1:])):
                with self.subTest(track=name, pair=f"{soft} vs {hard}"):
                    self.assertLess(ts, th)

    def test_softer_compound_has_more_grip_and_wears_faster(self):
        r = {c: solver.solve(self.monza, car(compound=c), self.driver, thermal=True, detail=False) for c in ("C2", "C4")}
        self.assertGreater(r["C4"]["wear_per_lap_r"], r["C2"]["wear_per_lap_r"])
        self.assertLess(self.lap(self.monza, compound="C4")["lap_time"], self.lap(self.monza, compound="C2")["lap_time"])


if __name__ == "__main__":
    unittest.main()

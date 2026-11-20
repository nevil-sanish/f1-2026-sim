"""End-to-end tests of the HTTP server (docs/API.md §1): a real server on a free port, hit with urllib.

    .venv/bin/python -m unittest tests.test_server -v
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from f1sim import server, setups

HAVE_RACE = importlib.util.find_spec("f1sim.race.race_sim") is not None
HAVE_MC = importlib.util.find_spec("f1sim.race.montecarlo") is not None
HAVE_CALIB = importlib.util.find_spec("f1sim.calibration.fit") is not None


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._setup_dir = tempfile.TemporaryDirectory()
        cls._old_dir = setups.DIR
        setups.DIR = Path(cls._setup_dir.name)          # never touch the real setups/ folder
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        setups.DIR = cls._old_dir
        cls._setup_dir.cleanup()

    # ---- helpers
    def req(self, path, body=None, method=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=60) as f:
                return f.status, dict(f.headers), f.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get(self, path, status=200):
        code, _, body = self.req(path)
        self.assertEqual(code, status, body[:300])
        return json.loads(body)

    def post(self, path, body, status=200):
        code, _, raw = self.req(path, body)
        self.assertEqual(code, status, raw[:300])
        return json.loads(raw)

    def assert_error(self, status, path, body=None, raw=None):
        code, hdr, data = self.req(path, body, raw=raw) if (body is not None or raw is not None) else self.req(path)
        self.assertEqual(code, status, data[:300])
        self.assertIn("error", json.loads(data))
        return json.loads(data)["error"]

    def wait_job(self, job_id, timeout=60.0):
        t_end = time.time() + timeout
        while time.time() < t_end:
            job = self.get(f"/api/jobs/{job_id}")
            self.assertEqual(set(job), {"id", "kind", "status", "progress", "message", "result", "error"})
            self.assertTrue(0.0 <= job["progress"] <= 1.0)
            if job["status"] != "running":
                return job
            time.sleep(0.2)
        self.fail(f"job {job_id} did not finish in {timeout} s")

    # ---- GET
    def test_tracks(self):
        rows = self.get("/api/tracks")
        self.assertGreaterEqual(len(rows), 20)
        keys = {"id", "name", "type", "night", "laps", "length", "width", "round", "date", "align_rms_m",
                "elevation_source", "start_line", "notes", "n_corners", "pit_loss_s", "overtake_difficulty"}
        for r in rows:
            self.assertLessEqual(keys, set(r), r["id"])
            self.assertIn(r["type"], ("permanent", "street"))
            self.assertIsInstance(r["notes"], list)
        self.assertIn("monza", {r["id"] for r in rows})

    def test_track_geometry(self):
        g = self.get("/api/track/monza")
        for k in ("id", "name", "type", "night", "length", "width", "ds", "x", "y", "z", "psi", "kappa", "corners",
                  "kerbs", "kerb_width", "runoff", "pit", "notes"):
            self.assertIn(k, g)
        n = len(g["x"])
        self.assertGreater(n, 100)
        for k in ("y", "z", "psi", "kappa"):
            self.assertEqual(len(g[k]), n)
        c = g["corners"][0]
        self.assertEqual(set(c), {"num", "apex", "s_apex", "i0", "i1", "dir", "radius"})
        self.assertIn(c["dir"], ("L", "R"))
        self.assertEqual(set(g["pit"]), {"entry_s", "exit_s", "side"})
        if g["kerbs"]:
            self.assertLessEqual({"i0", "i1", "side", "kind"}, set(g["kerbs"][0]))
        if g["runoff"]:
            self.assertLessEqual({"i0", "i1", "side", "kind", "depth"}, set(g["runoff"][0]))
        self.assert_error(404, "/api/track/no_such_track")

    def test_teams_drivers(self):
        teams = self.get("/api/teams")
        self.assertGreaterEqual(len(teams), 10)
        for t in teams:
            self.assertLessEqual({"id", "name", "pu", "drivers", "other_drivers", "standing", "calibrated"}, set(t))
        drv = self.get("/api/drivers")
        self.assertGreaterEqual(len(drv), 20)
        rk = {"grip_use", "brake_commit", "throttle_commit", "consistency", "tyre_management", "racecraft",
              "wet_skill", "balance_preference"}
        for d in drv:
            self.assertLessEqual({"id", "code", "number", "name", "team", "current", "ratings"}, set(d))
            self.assertLessEqual(rk, set(d["ratings"]))

    def test_params_schema_presets_misc(self):
        team = self.get("/api/teams")[0]["id"]
        sch = self.get(f"/api/params/schema?team={team}")
        self.assertLessEqual({"groups", "params", "driver_params", "calibrated"}, set(sch))
        for p in sch["params"]:
            self.assertLessEqual({"key", "group", "label", "default", "preset", "min", "max", "step", "unit", "prov",
                                  "tip", "kind", "options"}, set(p))
            self.assertIn(p["prov"], ("regulation", "public-derived", "estimate"))
            self.assertIn(p["kind"], ("float", "choice", "bool"))
        self.assertEqual(set(sch["driver_params"][0]), {"key", "label", "min", "max", "step", "default", "tip"})
        self.get("/api/params/schema")
        self.assert_error(400, "/api/params/schema?team=nope")
        pre = self.get(f"/api/presets/{team}")
        self.assertEqual(set(pre), {"team", "values", "prov", "fit", "calibrated"})
        self.assert_error(404, "/api/presets/nope")
        liv = self.get("/api/liveries")
        self.assertIn(team, liv)
        self.assertLessEqual({"base", "secondary", "accent", "finish", "regions", "numbers", "number_color"},
                             set(liv[team]))
        st = self.get("/api/data_status")
        self.assertLessEqual({"fetched_at", "sources", "status", "rounds_cached", "stale_snapshot", "standings",
                              "calibration", "validation", "notices"}, set(st))
        self.assert_error(404, "/api/nope")
        self.assert_error(404, "/api/jobs/doesnotexist")

    # ---- simulate / compare
    def check_sim(self, r, track):
        self.assertEqual(r["track"], track)
        for k in ("label", "track", "team", "driver", "mode", "lap_time", "lap_time_str", "run_time_s", "line",
                  "telemetry", "summary", "corners", "flags", "warnings"):
            self.assertIn(k, r)
        n = len(r["telemetry"]["s"])
        self.assertGreater(n, 100)
        self.assertEqual(set(r["line"]), {"s", "x", "y", "z", "psi", "offset"})
        for k, v in r["line"].items():
            self.assertEqual(len(v), n, k)
        tel = {"s", "t", "v", "throttle", "brake", "gear", "rpm", "ax", "ay", "soc", "mode", "p_dep", "p_harv",
               "tyre_temp_f", "tyre_temp_r", "brake_temp_f", "brake_temp_r", "ride_h_f", "ride_h_r", "balance",
               "kappa", "lift"}
        self.assertLessEqual(tel, set(r["telemetry"]))
        for k in tel:
            self.assertEqual(len(r["telemetry"][k]), n, k)
        self.assertLessEqual({"vmax", "vmin", "v_avg", "e_deploy_mj", "e_harvest_mj", "e_budget_mj", "mass", "fuel",
                              "wear_per_lap_f", "wear_per_lap_r", "upshifts", "max_lat_g", "max_brake_g",
                              "x_mode_pct", "sectors"}, set(r["summary"]))
        self.assertEqual(len(r["summary"]["sectors"]), 3)
        self.assertAlmostEqual(sum(r["summary"]["sectors"]), r["lap_time"], delta=0.05)
        self.assertTrue(40.0 < r["lap_time"] < 200.0)
        for c in r["corners"]:
            self.assertLessEqual({"num", "s_apex", "v_min", "brake_s", "dir"}, set(c))
        for f in r["flags"]:
            self.assertEqual(set(f), {"id", "level", "title", "text"})
            self.assertIn(f["level"], ("ok", "warn", "bad"))

    def test_simulate_monza_monaco(self):
        team = self.get("/api/teams")[0]
        drv = team["drivers"][0] if team["drivers"] else None
        for track in ("monza", "monaco"):
            r = self.post("/api/simulate", {"track": track, "team": team["id"], "driver": drv, "mode": "quali"})
            self.check_sim(r, track)
        r = self.post("/api/simulate", {"track": "monza", "mode": "race_pace", "tyre_age": 8,
                                        "overrides": {"rear_wing": 15}, "label": "mine"})
        self.check_sim(r, "monza")
        self.assertEqual(r["label"], "mine")
        self.assertEqual(r["mode"], "race_pace")

    def test_simulate_bad_input(self):
        self.assertIn("track", self.assert_error(400, "/api/simulate", {}))
        self.assert_error(404, "/api/simulate", {"track": "no_such_track"})
        self.assert_error(400, "/api/simulate", {"track": "monza", "mode": "fast"})
        self.assert_error(400, "/api/simulate", {"track": "monza", "team": "nope"})
        self.assert_error(400, "/api/simulate", {"track": "monza", "driver": "nope"})
        self.assert_error(400, "/api/simulate", {"track": "monza", "overrides": [1, 2]})
        self.assert_error(400, "/api/simulate", {"track": "monza", "overrides": {"rear_wing": "lots"}})
        self.assert_error(400, "/api/simulate", {"track": "monza", "overrides": {"compound": "C9"}})
        self.assert_error(400, "/api/simulate", {"track": "monza", "driver_overrides": {"grip_use": "x"}})
        self.assert_error(400, "/api/simulate", {"track": "monza", "tyre_age": "old"})
        self.assert_error(400, "/api/simulate", raw=b'{"track":"monza","overrides":{"rear_wing":NaN}}')
        self.assert_error(400, "/api/simulate", raw=b"not json")
        self.assert_error(400, "/api/simulate", raw=b"[1, 2]")
        self.assert_error(400, "/api/simulate", raw=b"\xff\xfe")
        self.assert_error(404, "/api/nope", {})
        code, _, _ = self.req("/api/tracks", method="DELETE")
        self.assertEqual(code, 405)

    def test_compare(self):
        out = self.post("/api/compare", {"configs": [{"track": "monza"},
                                                     {"track": "monza", "overrides": {"rear_wing": 10}, "label": "B"}]})
        self.assertEqual(set(out), {"results", "ranking", "delta"})
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual({r["index"] for r in out["ranking"]}, {0, 1})
        self.assertEqual(out["ranking"][0]["gap"], 0.0)
        for r in out["ranking"]:
            self.assertEqual(set(r), {"index", "label", "lap_time", "gap"})
        n = len(out["delta"]["s"])
        self.assertEqual(len(out["delta"]["series"]), 2)
        for s in out["delta"]["series"]:
            self.assertEqual(set(s), {"index", "label", "dt"})
            self.assertEqual(len(s["dt"]), n)
        self.assertTrue(all(abs(x) < 1e-6 for x in out["delta"]["series"][0]["dt"]))
        lt = [r["lap_time"] for r in out["results"]]
        self.assertAlmostEqual(out["delta"]["series"][1]["dt"][-1], lt[1] - lt[0], delta=0.01)
        self.assert_error(400, "/api/compare", {"configs": [{"track": "monza"}]})
        self.assert_error(400, "/api/compare", {"configs": [{"track": "monza"}, {"track": "monaco"}]})
        self.assert_error(400, "/api/compare", {"configs": [{"track": ["monza"]}, {"track": "monza"}]})
        self.assert_error(400, "/api/compare", {"configs": [1, 2]})
        self.assert_error(400, "/api/compare", {})

    def test_concurrent_simulations(self):
        bodies = [{"track": "monza", "overrides": {"rear_wing": 8 + i}} for i in range(8)]

        def one(b):
            code, _, raw = self.req("/api/simulate", b)
            return code, json.loads(raw)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, bodies))
        self.assertTrue(all(c == 200 for c, _ in results), [r.get("error") for _, r in results])
        self.assertEqual(len({r["lap_time"] for _, r in results}), 8)

    # ---- HTTP details
    def test_gzip(self):
        code, hdr, raw = self.req("/api/track/monza", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(code, 200)
        self.assertEqual(hdr.get("Content-Encoding"), "gzip")
        self.assertEqual(int(hdr["Content-Length"]), len(raw))
        self.assertEqual(json.loads(gzip.decompress(raw))["id"], "monza")
        code, hdr, raw = self.req("/api/track/monza")
        self.assertIsNone(hdr.get("Content-Encoding"))
        self.assertEqual(json.loads(raw)["id"], "monza")

    def test_static_files(self):
        if not (server.WEB / "index.html").exists():
            self.skipTest("web/index.html not written yet")
        code, hdr, raw = self.req("/")
        self.assertEqual(code, 200)
        self.assertTrue(hdr["Content-Type"].startswith("text/html"))
        self.assertIn(b"<", raw)
        if (server.WEB / "app.js").exists():
            code, hdr, _ = self.req("/app.js")
            self.assertEqual(code, 200)
            self.assertTrue(hdr["Content-Type"].startswith("text/javascript"))
        self.assertEqual(self.req("/no/such/file.js")[0], 404)
        self.assertEqual(self.req("/%2e%2e/run.py")[0], 403)
        self.assertEqual(self.req("/.hidden")[0], 403)
        self.assertEqual(self.req("/api/teams", headers={"Host": "evil.example"})[0], 403)

    def test_json_safety(self):
        self.assertEqual(server.clean({"a": np.float32(1.5), "b": np.nan, "c": np.arange(3), "d": np.bool_(True),
                                       "e": [np.inf, 1.0], "f": np.array([1.0, np.nan]), 3: (1, 2)}),
                         {"a": 1.5, "b": None, "c": [0, 1, 2], "d": True, "e": [None, 1.0], "f": [1.0, None],
                          "3": [1, 2]})
        job = server._submit("test", lambda progress: {"x": np.float64("nan"), "y": np.int64(4),
                                                        "z": np.linspace(0, 1, 3)})["job"]
        res = self.wait_job(job)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["result"], {"x": None, "y": 4, "z": [0.0, 0.5, 1.0]})
        bad = server._submit("test", lambda progress: 1 / 0)["job"]
        res = self.wait_job(bad)
        self.assertEqual(res["status"], "error")
        self.assertIn("ZeroDivisionError", res["error"])

    def test_liveries(self):
        liv = self.get("/api/liveries")
        files = {p.stem for p in (server.DATA / "liveries").glob("*.json")}
        for team in files & set(liv):
            self.assertNotIn("placeholder", liv[team], team)      # served from data/liveries/<team>.json
            saved = json.loads((server.DATA / "liveries" / f"{team}.json").read_text())
            if "base" in saved:
                self.assertEqual(liv[team]["base"], saved["base"])
        for team, l in liv.items():
            self.assertEqual(set(l["regions"]) >= set(server.LIVERY_REGIONS), True, team)
        # validation only: a successful POST would overwrite the real livery files
        team = next(iter(liv))
        self.assert_error(404, "/api/liveries/no_such_team", {"base": "#ffffff"})
        for bad in ({"base": "red"}, {"finish": "chrome"}, {"regions": {"roof": "base"}},
                    {"regions": {"nose": "pink"}}, {"regions": []}, {"numbers": {"x": "5"}},
                    {"numbers": {"x": 150}}, {"numbers": {"x": True}}):
            self.assert_error(400, f"/api/liveries/{team}", bad)
        self.assert_error(400, f"/api/liveries/{team}", raw=b"[]")

    # ---- setups
    def test_setups_roundtrip(self):
        body = {"name": "Test Setup A", "track": "monza", "team": None, "driver": None, "notes": "n",
                "values": {"rear_wing": 12.5, "compound": "C3"}, "driver_values": {"grip_use": 0.99}}
        out = self.post("/api/setups", body)
        self.assertEqual(out, {"ok": True, "name": "test_setup_a"})
        rows = self.get("/api/setups")
        row = next(r for r in rows if r["name"] == "Test Setup A")
        self.assertLessEqual({"name", "track", "team", "driver", "notes", "saved_at"}, set(row))
        full = self.get("/api/setups/test_setup_a")
        self.assertLessEqual({"name", "track", "team", "driver", "notes", "values", "driver_values", "saved_at"},
                             set(full))
        self.assertEqual(full["values"], body["values"])
        self.assertEqual(full["driver_values"], body["driver_values"])
        self.assertEqual(self.get("/api/setups/Test%20Setup%20A")["values"], body["values"])
        self.assertEqual(self.post("/api/setups/test_setup_a/delete", {}), {"ok": True})
        self.assertIn("test_setup_a", self.assert_error(404, "/api/setups/test_setup_a"))
        self.assert_error(404, "/api/setups/test_setup_a/delete", {})
        self.assert_error(400, "/api/setups", {"name": "!!!"})
        self.assert_error(400, "/api/setups", raw=b"[]")

    # ---- jobs
    def test_optimise_job(self):
        out = self.post("/api/optimise", {"track": "monza", "params": ["rear_wing"], "objective": "quali",
                                          "max_evals": 4})
        self.assertEqual(set(out), {"job"})
        job = self.wait_job(out["job"])
        self.assertEqual(job["status"], "done", job["error"])
        self.assertEqual(job["kind"], "optimise")
        res = job["result"]
        self.assertLessEqual({"objective", "before", "after", "gain", "best", "sensitivities", "evaluations"}, set(res))
        self.assertLessEqual(res["after"], res["before"])
        self.assertIn("rear_wing", res["best"])
        for s in res["sensitivities"]:
            self.assertLessEqual({"key", "label", "unit", "step", "delta_per_step"}, set(s))
        self.assert_error(400, "/api/optimise", {"track": "monza", "objective": "win"})
        self.assert_error(400, "/api/optimise", {"track": "monza", "params": "rear_wing"})
        self.assert_error(400, "/api/optimise", {"track": "monza", "max_evals": 0})
        self.assert_error(404, "/api/optimise", {"track": "no_such_track"})

    RACE = {"track": "monza", "laps": 3, "grid": "quali", "options": {"safety_car": False, "dnf": False, "seed": 1},
            "monte_carlo": 0}

    def race_entries(self):
        teams = self.get("/api/teams")[:2]
        return [{"team": t["id"], "driver": d, "strategy": "auto"} for t in teams for d in t["drivers"][:2]]

    def test_race_validation(self):
        self.assert_error(400, "/api/race", {"track": "monza", "entries": []})
        self.assert_error(400, "/api/race", {**self.RACE, "entries": self.race_entries(), "laps": 0})
        self.assert_error(400, "/api/race", {**self.RACE, "entries": self.race_entries(), "grid": 5})
        self.assert_error(400, "/api/race", {**self.RACE, "entries": self.race_entries(), "monte_carlo": -1})
        self.assert_error(404, "/api/race", {**self.RACE, "track": "nope", "entries": self.race_entries()})

    def test_race_job(self):
        body = {**self.RACE, "entries": self.race_entries()}
        if not HAVE_RACE:
            self.assertIn("unavailable", self.assert_error(503, "/api/race", body))
            self.skipTest("f1sim.race.race_sim not written yet (503 checked)")
        job = self.wait_job(self.post("/api/race", body)["job"], timeout=50)
        self.assertEqual(job["status"], "done", job["error"])
        res = job["result"]
        self.assertLessEqual({"track", "laps", "lap_length", "entries", "classification", "fastest_lap", "lap_chart",
                              "gaps", "lap_times", "strategy", "energy", "overtakes", "events", "replay", "line",
                              "monte_carlo"}, set(res))
        n = len(body["entries"])
        self.assertEqual(res["laps"], 3)
        self.assertEqual(len(res["classification"]), n)
        self.assertEqual([c["pos"] for c in res["classification"]], list(range(1, n + 1)))
        for c in res["classification"]:
            self.assertLessEqual({"pos", "code", "driver", "team", "grid", "laps", "time", "gap", "status", "stops",
                                  "best_lap", "best_lap_n"}, set(c))
        codes = {c["code"] for c in res["classification"]}
        self.assertEqual(set(res["lap_chart"]), codes)
        for code in codes:
            self.assertEqual(len(res["lap_chart"][code]), 4)          # grid + 3 laps
            self.assertLessEqual(len(res["lap_times"][code]), 3)
        self.assertLessEqual({"dt", "t", "cars"}, set(res["replay"]))
        car = res["replay"]["cars"][next(iter(codes))]
        self.assertLessEqual({"d", "pit", "speed", "compound_idx"}, set(car))
        self.assertEqual(len(car["d"]), len(res["replay"]["t"]))
        self.assertLessEqual({"s", "x", "y", "z", "psi"}, set(res["line"]))
        self.assertIsNone(res["monte_carlo"])

    def test_monte_carlo_job(self):
        body = {**self.RACE, "entries": self.race_entries(), "monte_carlo": 3}
        if not HAVE_MC:
            self.assertIn("unavailable", self.assert_error(503, "/api/race", body))
            self.skipTest("f1sim.race.montecarlo not written yet (503 checked)")
        job = self.wait_job(self.post("/api/race", body)["job"], timeout=50)
        self.assertEqual(job["status"], "done", job["error"])
        mc = job["result"]["monte_carlo"]
        self.assertEqual(mc["runs"], 3)
        self.assertEqual(len(mc["table"]), len(body["entries"]))
        for row in mc["table"]:
            self.assertLessEqual({"code", "driver", "team", "win", "podium", "points", "avg_finish", "dist"}, set(row))
            self.assertTrue(0.0 <= row["win"] <= 1.0)
            self.assertEqual(len(row["dist"]), len(body["entries"]))
        self.assertAlmostEqual(sum(r["win"] for r in mc["table"]), 1.0, delta=1e-3)

    def test_calibrate_job(self):
        if not HAVE_CALIB:
            self.assertIn("unavailable", self.assert_error(503, "/api/calibrate", {}))
            self.skipTest("f1sim.calibration.fit not written yet (503 checked)")
        self.assertTrue(callable(server._import(".calibration.fit", "run_calibration", "calibration")))
        if not os.environ.get("F1SIM_TEST_CALIBRATE"):
            self.skipTest("calibration rewrites data/presets; set F1SIM_TEST_CALIBRATE=1 to run it")
        out = self.post("/api/calibrate", {})
        job = self.wait_job(out["job"], timeout=600)
        self.assertEqual(job["status"], "done", job["error"])


class PublicModeTest(unittest.TestCase):
    """The hosted demo (run.py with PORT set) answers reads and sims but refuses anything that writes to disk."""

    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server("127.0.0.1", 0, public=True)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def code(self, path, body=None):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as f:
                return f.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_reads_and_sims_work(self):
        self.assertEqual(self.code("/api/tracks"), 200)
        self.assertEqual(self.code("/api/simulate", {"track": "monza"}), 200)

    def test_writes_are_refused(self):
        self.assertEqual(self.code("/api/setups", {"name": "x", "overrides": {}}), 403)
        self.assertEqual(self.code("/api/setups/x/delete", {}), 403)
        self.assertEqual(self.code("/api/liveries/ferrari", {"primary": "#ff0000"}), 403)
        self.assertEqual(self.code("/api/calibrate", {}), 403)

    def test_monte_carlo_is_capped(self):
        body = {"track": "monza", "entries": [{"team": "ferrari", "driver": "leclerc"}], "monte_carlo": 5000}
        self.assertEqual(self.code("/api/race", body), 400)


if __name__ == "__main__":
    unittest.main()

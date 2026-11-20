"""Monte Carlo over race seeds: win / podium / points probabilities and finishing-position distributions.

The lap-time surfaces and strategy candidates are built once (`race_sim.prepare`); every run only redraws
the random parts (grid noise, pit strategy pick, lap scatter, launches, safety cars, DNFs, passing).
"""
from __future__ import annotations

from . import race_sim

POINTS = race_sim.POINTS


def run_monte_carlo(track_id: str, entries: list, laps=None, grid="quali", options: dict | None = None,
                    k: int = 50, seed: int = 0, progress=None) -> dict:
    """Race result of the run with `seed` (replayable) plus `monte_carlo:{runs, table}` over `k` seeds."""
    options = options or {}
    k = max(int(k), 1)
    ctx = race_sim.prepare(track_id, entries, laps, options, progress, progress_span=0.3)
    n = len(ctx.cars)
    wins = [0] * n
    podium = [0] * n
    pts_prob = [0] * n
    pts_sum = [0.0] * n
    dnf = [0] * n
    pos_sum = [0.0] * n
    dist = [[0] * n for _ in range(n)]
    first = None
    for r in range(k):
        raw = race_sim.simulate(ctx, grid, options, seed + r)
        cls = race_sim.classify(raw)
        for row in cls["rows"]:
            i, p = row["_i"], row["pos"]
            dist[i][p - 1] += 1
            pos_sum[i] += p
            wins[i] += p == 1
            podium[i] += p <= 3
            if p <= len(POINTS):
                pts_prob[i] += 1
                pts_sum[i] += POINTS[p - 1]
            dnf[i] += row["status"] == "DNF"
        if r == 0:
            first = raw
        if progress and (r % 4 == 0 or r == k - 1):
            progress(0.3 + 0.65 * (r + 1) / k, f"race {r + 1}/{k}")
    res = race_sim.build_result(first)
    table = [{"code": c.code, "driver": c.driver, "team": c.team, "win": wins[i] / k, "podium": podium[i] / k,
              "points": pts_prob[i] / k, "avg_points": round(pts_sum[i] / k, 3), "dnf": dnf[i] / k,
              "avg_finish": round(pos_sum[i] / k, 3), "dist": [d / k for d in dist[i]]}
             for i, c in enumerate(ctx.cars)]
    table.sort(key=lambda t: (-t["win"], t["avg_finish"]))
    res["monte_carlo"] = {"runs": k, "seed": seed, "table": table}
    if progress:
        progress(1.0, "done")
    return res

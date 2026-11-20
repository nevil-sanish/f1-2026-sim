"""Lap-by-lap race simulator (brief §8.3).

Each car gets a lap-time surface (race/pace.py): T(compound, fuel, wear) plus per-sample traffic deltas.
The race loop advances every car one lap at a time on a shared clock (cumulative time at each line crossing):

  * grid: quali-sim lap time + noise, or a given driver order; standing-start loss and launch variance
  * lap time: surface + fuel burn + tyre wear/cliff + driver scatter + race-day form
  * traffic: dirty air / tow from the gap to the car ahead at the line, override mode inside 1 s
  * passing: a faster car that would end the lap on the car ahead passes with probability
    overtaking.pass_probability (pace advantage vs track difficulty and racecraft), else it is held up
  * pit stops: strategy.py plans (auto = near-optimal pick, or a fixed plan), pit loss per track,
    stationary-time scatter per team, cheaper stops under SC/VSC, forced stop at the tyre cliff
  * random events (seeded): safety car, VSC, DNFs (options.safety_car / options.dnf, both default on)

Everything is deterministic in `seed`. `run_race` returns the docs/API.md race result; `simulate` is the
bare loop that the Monte Carlo reuses.
"""
from __future__ import annotations

import bisect
import dataclasses
import math

import numpy as np

from .. import drivers as drivers_mod
from .. import tracks
from ..calibration import race_targets
from ..car import tyres
from . import overtaking as ot
from . import pace, strategy

POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)
GRID_SLOT_S = 0.19            # time offset per grid row at the start
GRID_SLOT_M = 8.0             # metres between grid slots (replay)
START_LOSS_S = 5.2            # standing start vs flying lap
LAUNCH_SD = 0.30              # s, launch variance
LAP1_SD = 0.45                # s, opening-lap scatter (contact avoided, lines taken)
FORM_SD = 0.07                # s, per-car race-day pace offset
PASS_MARGIN = 0.25            # s/lap of extra pace needed on top of the overtaking model (wake of the car in front)
REPASS_BLOCK = 3              # laps before a passed car can re-pass
MIN_SEP = 0.10                # s, closest line-crossing separation of two cars
WAKE_REF = 1.0 - pace.WAKE_CL_SCALE
TOW_REF = 1.0 - pace.WAKE_CD_SCALE
MAX_FOLLOW_GAIN = -0.30       # s, the net benefit of following (tow - wake) is capped
P_SC = 0.009                  # per lap
P_VSC = 0.009
P_DNF = 0.00055               # per car and lap
P_DNF_LAP1 = 0.006
P_SC_AFTER_DNF = 0.55
SC_FACTOR, VSC_FACTOR = 1.45, 1.35
SC_PIT, VSC_PIT = 0.55, 0.65  # pit loss multiplier under SC / VSC
SC_WEAR = 0.3
FORCED_STOP_WEAR = 0.93
REPLAY_DT = 2.0
PIT_LANE_FRAC = 0.06          # share of the lap length the replay spends in the pit lane on a stop lap


@dataclasses.dataclass
class Car:
    idx: int
    code: str
    driver: str
    team: str | None
    name: str
    number: int
    surf: pace.PaceSurface
    rc: float
    sigma: float
    tm: float
    pit: dict
    cands: list
    fixed: dict | None
    entry: dict
    tab: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Ctx:
    track_id: str
    track: dict
    path: dict
    laps: int
    length: float
    compounds: list
    cars: list
    pit_loss: float
    difficulty: float
    zones: tuple
    deg: float
    warnings: list


# ------------------------------------------------------------------------------------------ set-up
def _roster() -> dict:
    try:
        import json
        return {r["id"]: r for r in json.loads((drivers_mod.DATA / "drivers.json").read_text())}
    except (OSError, ValueError):
        return {}


def prepare(track_id: str, entries: list, laps=None, options: dict | None = None, progress=None,
            progress_span: float = 1.0) -> Ctx:
    options = options or {}
    track = tracks.load_track(track_id)
    path = pace.get_path(track_id)
    laps = int(laps or track.get("laps") or 50)
    tp = race_targets.track_race_params(track_id, track)
    deg = float(options.get("deg_scale", 1.0) or 1.0)

    def sub(f, msg=None):
        if progress:
            progress(f * progress_span, msg)

    surfs = pace.build_surfaces(track_id, entries, progress=sub)
    compounds = list(surfs[0].compounds)
    roster = _roster()
    seen: dict[str, int] = {}
    cars, warnings = [], []
    for i, (e, s) in enumerate(zip(entries, surfs)):
        did = e.get("driver")
        r = roster.get(did, {})
        code = r.get("code") or (str(did or f"C{i + 1}")[:3].upper())
        if code in seen:
            seen[code] += 1
            code = f"{code}{seen[code]}"
        else:
            seen[code] = 1
        drv = drivers_mod.get_driver(did, e.get("driver_overrides"))
        s = dataclasses.replace(s, wear_rate=s.wear_rate * deg) if deg != 1.0 else s
        strat = e.get("strategy", "auto")
        fixed = None
        cands = []
        if isinstance(strat, dict):
            fixed, w = strategy.fixed_plan(strat, compounds, laps)
            warnings += [f"{code}: {x}" for x in w]
        else:
            fuel0 = s.meta["fuel_per_lap"] * laps
            cands = strategy.candidates(s, laps, tp["pit_loss_s"], fuel0)
        tab = [[list(map(float, s.table[c, k])) for k in range(len(s.fuel_grid))] for c in range(len(s.compounds))]
        cars.append(Car(idx=i, code=code, driver=did or code, team=e.get("team"), name=r.get("name", did or code),
                        number=int(r.get("number") or 0), surf=s, rc=float(drv["racecraft"]),
                        sigma=float(drv["consistency"]), tm=float(drv["tyre_management"]),
                        pit=race_targets.team_pit_params(e.get("team")), cands=cands, fixed=fixed, entry=e, tab=tab))
    s0 = surfs[0]
    zones = ot.passing_zones(s0.v_ref, float(path["ds"]), 12)
    return Ctx(track_id, track, path, laps, float(path.get("length", track["length"])), compounds, cars,
               float(tp["pit_loss_s"]), float(tp["overtake_difficulty"]), zones, deg, warnings)


def _lap_time(car: Car, ci: int, fuel: float, wear: float) -> float:
    s = car.surf
    wg, fg = s.wear_grid, s.fuel_grid
    j = min(max(bisect.bisect_right(wg, wear) - 1, 0), len(wg) - 2)
    a = min(max((wear - wg[j]) / (wg[j + 1] - wg[j]), 0.0), 1.0)
    k = min(max(bisect.bisect_right(fg, fuel) - 1, 0), len(fg) - 2)
    b = min(max((fuel - fg[k]) / (fg[k + 1] - fg[k]), 0.0), 1.0)
    t = car.tab[ci]
    lo = t[k][j] + (t[k][j + 1] - t[k][j]) * a
    hi = t[k + 1][j] + (t[k + 1][j + 1] - t[k + 1][j]) * a
    return lo + (hi - lo) * b


# ------------------------------------------------------------------------------------------- the race
def simulate(ctx: Ctx, grid="quali", options: dict | None = None, seed: int = 0) -> dict:
    options = options or {}
    rng = np.random.default_rng(int(seed))
    use_sc = bool(options.get("safety_car", True))
    use_dnf = bool(options.get("dnf", True))
    cars, n, N = ctx.cars, len(ctx.cars), ctx.laps
    diff = ctx.difficulty
    ds = float(ctx.path["ds"])

    # ---- grid
    qt = np.array([c.surf.quali_time + rng.normal(0, 0.10 + 0.5 * c.sigma * 0.3) for c in cars])
    if isinstance(grid, list):
        pos = {d: k for k, d in enumerate(grid)}
        order = sorted(range(n), key=lambda i: (pos.get(cars[i].driver, 10 ** 6), qt[i]))
    else:
        order = list(np.argsort(qt, kind="stable"))
    order = [int(i) for i in order]
    grid_pos = {i: k + 1 for k, i in enumerate(order)}

    form = rng.normal(0, FORM_SD, n)
    noise = rng.normal(0, 1.0, (n, N))
    dnf_u = rng.random((n, N))
    sc_u = rng.random((N + 2, 3))
    stat_z = rng.normal(0, 1.0, (n, 8))
    stat_slow = rng.random((n, 8))
    stat_extra = rng.uniform(3.0, 12.0, (n, 8))
    plan_rng = np.random.default_rng(int(seed) + 7919)

    # ---- state
    ncomp = len(ctx.compounds)
    plan = []
    for c in cars:
        p = c.fixed if c.fixed is not None else strategy.pick_plan(c.cands, plan_rng, N)
        plan.append({"start": p["start_compound"], "stops": [dict(s) for s in p["stops"]]})
    ci = [ctx.compounds.index(p["start"]) for p in plan]
    wear = [0.0] * n
    age = [0] * n
    sp = [0] * n
    fpl = [c.surf.meta["fuel_per_lap"] for c in cars]
    fuel = [fp * N + 1.0 for fp in fpl]
    tm_fac = [min(max(1.0 + 0.9 * (0.7 - c.tm), 0.7), 1.3) for c in cars]
    T = [0.0] * n
    for k, i in enumerate(order):
        T[i] = GRID_SLOT_S * k
    alive = [True] * n
    retired: list[int] = []
    ret_info: dict = {}
    cum = [[0.0] for _ in range(n)]
    lapt: list[list] = [[] for _ in range(n)]
    pit_laps = [dict() for _ in range(n)]          # lap -> stationary extra (s) for the replay/events
    stints = [[{"ci": ci[i], "start": 1}] for i in range(n)]
    energy = [[] for _ in range(n)]
    pit_dur: list[dict] = [dict() for _ in range(n)]
    ptime: dict = {}
    block: dict = {}              # (attacker, defender) -> lap of the last pass of the defender on the attacker
    overtakes, events = [], []
    lap_order = [list(order)]
    sc_left, sc_kind, sc_pending = 0, None, None
    first_zone_share = ctx.zones[0]
    zone_s = ctx.zones[1]

    def pit_time(i, lap, mult):
        pp = cars[i].pit
        k = len(pit_laps[i]) % 8
        st = max(1.9, pp["mean"] + pp["sd"] * stat_z[i, k])
        if stat_slow[i, k] < pp["slow_prob"]:
            st += stat_extra[i, k]
        loss = ctx.pit_loss + (st - pp["ref"])
        loss = max(loss, 0.5 * ctx.pit_loss) * mult
        ptime[i] = loss
        return loss

    for lap in range(1, N + 1):
        run = [i for i in order if alive[i]]
        if not run:
            break
        # ---- race control
        status = None
        if sc_pending and use_sc and lap < N:
            sc_kind, sc_left = sc_pending
            sc_pending = None
            events.append({"t": round(T[run[0]], 2), "lap": lap, "kind": sc_kind, "code": None,
                           "text": ("Safety car" if sc_kind == "SC" else "Virtual safety car") + " deployed",
                           "until_lap": min(lap + sc_left - 1, N)})
        elif sc_left == 0 and use_sc and 1 < lap < N - 1:
            u = sc_u[lap]
            if u[0] < P_SC:
                sc_kind, sc_left = "SC", 3 + int(u[1] * 3)
            elif u[0] < P_SC + P_VSC:
                sc_kind, sc_left = "VSC", 2 + int(u[1] * 2)
            if sc_left:
                events.append({"t": round(T[run[0]], 2), "lap": lap, "kind": sc_kind, "code": None,
                               "text": ("Safety car" if sc_kind == "SC" else "Virtual safety car") + " deployed",
                               "until_lap": min(lap + sc_left - 1, N)})
        if sc_left > 0:
            status = sc_kind
            sc_left -= 1

        # ---- pit decisions (stop at the end of this lap)
        pitting = {}
        for i in run:
            if lap >= N:
                break
            p = plan[i]
            want = sp[i] < len(p["stops"]) and p["stops"][sp[i]]["lap"] == lap
            if status and sp[i] < len(p["stops"]) and not want:
                nxt = p["stops"][sp[i]]["lap"]
                if nxt - lap <= 8 and age[i] >= 6 and nxt < N:
                    want = True
            forced = wear[i] >= FORCED_STOP_WEAR and not want
            if want:
                comp = p["stops"][sp[i]]["compound"]
                sp[i] += 1
                pitting[i] = ctx.compounds.index(comp)
            elif forced:
                cur = ci[i]
                alt = [c for c in range(ncomp) if c != cur]
                pitting[i] = alt[len(alt) // 2] if alt else cur
                plan[i]["stops"] = plan[i]["stops"][:sp[i]] + [{"lap": lap, "compound": ctx.compounds[pitting[i]]}]
                sp[i] += 1

        # ---- lap times
        clean, extra, ovr_dt, wake_loss, ovr_used, pace_det = {}, {}, {}, {}, {}, {}
        for pos, i in enumerate(run):
            c = cars[i]
            lt = _lap_time(c, ci[i], max(fuel[i], 0.0), wear[i]) + form[i]
            if age[i] == 0 and lap > 1:
                lt += strategy.OUTLAP_WARMUP[min(ci[i], 2)]
            pace_det[i] = lt
            lt += c.sigma * noise[i, lap - 1]
            if lap == 1:
                lt += START_LOSS_S
            clean[i] = lt
            g = T[i] - T[run[pos - 1]] if pos > 0 else 1e9
            ex, wl, od = 0.0, 0.0, 0.0
            if pos > 0 and not status:
                wl = ot.wake_downforce_loss(g)
                tow = ot.wake_drag_drop(g)
                fol = wl / WAKE_REF * c.surf.dt_wake.sum() + tow / TOW_REF * c.surf.dt_tow.sum()
                ex += max(fol, MAX_FOLLOW_GAIN)
                if g < ot.OVERRIDE_GAP and lap > 1:
                    od = float(c.surf.dt_override.sum())
                    ex += od
                    ovr_used[i] = True
            extra[i], ovr_dt[i], wake_loss[i] = ex, od, wl

        newly_dnf = []
        if use_dnf:
            for i in run:
                p = P_DNF * (1.0 + 4.0 * max(wear[i] - 0.85, 0.0) / 0.15) + (P_DNF_LAP1 if lap == 1 else 0.0)
                if dnf_u[i, lap - 1] < p and len(run) - len(newly_dnf) > 2:
                    newly_dnf.append(i)

        E = {}
        pitnow = {}
        if status == "SC":
            first = run[0]
            E[first] = T[first] + clean[first] * SC_FACTOR
            prev = first
            for i in run[1:]:
                gap = min(max(T[i] - T[prev], 0.6), 1.0)
                E[i] = E[prev] + gap
                prev = i
            for i in pitting:
                E[i] += pit_time(i, lap, SC_PIT)
                pitnow[i] = True
            seq = sorted(run, key=lambda i: E[i])
            for k in range(1, len(seq)):
                E[seq[k]] = max(E[seq[k]], E[seq[k - 1]] + 0.5)
            new = seq
        elif status == "VSC":
            for i in run:
                E[i] = T[i] + clean[i] * VSC_FACTOR
            for i in pitting:
                E[i] += pit_time(i, lap, VSC_PIT)
                pitnow[i] = True
            seq = sorted(run, key=lambda i: E[i])
            for k in range(1, len(seq)):
                E[seq[k]] = max(E[seq[k]], E[seq[k - 1]] + 0.2)
            new = seq
        elif lap == 1:
            for i in run:
                E[i] = T[i] + clean[i] + extra[i] + rng.normal(0, LAUNCH_SD) + rng.normal(0, LAP1_SD) \
                    + 0.01 * grid_pos[i]
            new = sorted(run, key=lambda i: E[i])
            for k in range(1, len(new)):
                E[new[k]] = max(E[new[k]], E[new[k - 1]] + MIN_SEP)
            pitting = {}
        else:
            new = []
            for i in run:
                e = T[i] + clean[i] + extra[i]
                if i in pitting:
                    e += pit_time(i, lap, 1.0)
                    pitnow[i] = True
                E[i] = e
                j = len(new)
                while j > 0:
                    a = new[j - 1]
                    if E[i] >= E[a] + ot.FOLLOW_GAP or i in pitnow:
                        break
                    if a in pitnow:
                        j -= 1                         # the car ahead is in the pits: a free place
                        continue
                    delta = pace_det[a] - (pace_det[i] + ovr_dt[i]) - PASS_MARGIN
                    p = ot.pass_probability(delta, diff, cars[i].rc, cars[a].rc, 1.0, False)
                    if block.get((i, a), -99) > lap - REPASS_BLOCK:
                        p = 0.0
                    if rng.random() < p:
                        block[(a, i)] = lap
                        s_idx = int(rng.choice(len(first_zone_share), p=first_zone_share))
                        overtakes.append({"lap": lap, "t": round(T[i] + 0.5 * clean[i], 2),
                                          "s": round(float(zone_s[s_idx]), 1), "by": cars[i].code, "on": cars[a].code})
                        E[a] = max(E[a], E[i] + ot.PASSED_GAP)
                        j -= 1
                    else:
                        E[i] = E[a] + ot.FOLLOW_GAP
                        break
                new.insert(j, i)
            for k in range(1, len(new)):
                E[new[k]] = max(E[new[k]], E[new[k - 1]] + MIN_SEP)

        # ---- book-keeping
        dnf_set = set(newly_dnf)
        for i in run:
            c = cars[i]
            if i in dnf_set:
                f = float(rng.uniform(0.1, 0.9))
                alive[i] = False
                retired.append(i)
                ret_info[i] = {"lap": lap, "t": T[i] + f * clean[i], "frac": f}
                events.append({"t": round(T[i] + f * clean[i], 2), "lap": lap, "kind": "DNF", "code": c.code,
                               "text": f"{c.code} retires"})
                continue
            # timed from lights out, so lap 1 includes the grid-slot offset (as in real timing)
            dur = E[i] - cum[i][-1]
            lapt[i].append(dur)
            cum[i].append(E[i])
            fuel[i] -= fpl[i]
            w = c.surf.wear_rate[ci[i]] * (1.0 + ot.WAKE_WEAR_GAIN * wake_loss[i]) * tm_fac[i]
            if status == "SC":
                w *= SC_WEAR
            elif status == "VSC":
                w *= 0.5
            wear[i] += float(w)
            age[i] += 1
            energy[i].append(round(c.surf.e_deploy_mj + (c.surf.e_override_mj if ovr_used.get(i) else 0.0), 3)
                             if not status else round(0.4 * c.surf.e_deploy_mj, 3))
            if i in pitnow:
                stints[i][-1]["end"] = lap
                ci[i] = pitting[i]
                wear[i] = 0.0
                age[i] = 0
                stints[i].append({"ci": ci[i], "start": lap + 1})
                pit_dur[i][lap] = float(ptime[i])
                events.append({"t": round(E[i], 2), "lap": lap, "kind": "PIT", "code": c.code,
                               "text": f"{c.code} pits for {ctx.compounds[ci[i]]}"})
        for i in run:
            if alive[i]:
                T[i] = E[i]
        if newly_dnf and use_sc and not sc_left and lap < N - 1 and not sc_pending:
            u = sc_u[lap][2]
            if u < P_SC_AFTER_DNF:
                sc_pending = ("SC", 3 + int(sc_u[lap][1] * 3)) if sc_u[lap][1] > 0.35 else ("VSC", 2 + int(sc_u[lap][1] * 4))
        order = [i for i in new if alive[i]]
        lap_order.append(list(order) + [i for i in reversed(retired)])
    return {"ctx": ctx, "grid": [cars[i].code for i in lap_order[0]], "grid_idx": lap_order[0], "cum": cum,
            "lapt": lapt, "plan": plan, "stints": stints, "lap_order": lap_order, "retired": retired,
            "ret_info": ret_info, "overtakes": overtakes, "events": events, "energy": energy,
            "pit_dur": pit_dur, "grid_pos": grid_pos}


# ------------------------------------------------------------------------------------ classification
def classify(raw: dict) -> dict:
    ctx: Ctx = raw["ctx"]
    cars, N = ctx.cars, ctx.laps
    n = len(cars)
    cum = raw["cum"]
    finishers = [i for i in range(n) if i not in raw["ret_info"]]
    tw_i = min(finishers, key=lambda i: cum[i][N]) if finishers else None
    rows = []
    for i in range(n):
        c = cars[i]
        if i in raw["ret_info"]:
            lc = raw["ret_info"][i]["lap"] - 1
            rows.append((0, -lc, raw["ret_info"][i]["t"], i, lc, "DNF", None))
            continue
        tw = cum[tw_i][N]
        done = sum(1 for k in range(1, N + 1) if cum[i][k] <= tw)
        lc = min(done + 1, N)
        rows.append((-1 + (0 if i == tw_i else 0), -lc, cum[i][lc], i, lc, "FIN" if lc == N else f"+{N - lc} LAP", cum[i][lc]))
    # finishers by laps then time; DNFs after them by laps then time of retirement
    fin = sorted([r for r in rows if r[0] == -1], key=lambda r: (r[1], r[2]))
    dnf = sorted([r for r in rows if r[0] == 0], key=lambda r: (r[1], -r[2]))
    ordered = fin + dnf
    tw = cum[tw_i][N] if tw_i is not None else 0.0
    out = []
    best = None
    for p, (_, _, _, i, lc, status, tt) in enumerate(ordered, 1):
        c = cars[i]
        lt = raw["lapt"][i]
        bl = min(lt) if lt else None
        bln = (int(np.argmin(lt)) + 1) if lt else None
        # laps run under SC/VSC are not fastest-lap candidates
        stops = len(raw["stints"][i]) - 1
        out.append({"pos": p, "code": c.code, "driver": c.driver, "team": c.team, "grid": raw["grid_pos"][i],
                    "laps": lc, "time": None if tt is None else round(tt, 3),
                    "gap": None if (tt is None or status != "FIN") else round(tt - tw, 3),
                    "status": status, "stops": stops, "best_lap": None if bl is None else round(bl, 3),
                    "best_lap_n": bln, "_i": i})
    return {"rows": out, "winner": tw_i}


def _fastest(raw: dict, sc_laps: set) -> dict | None:
    best = None
    for i, lt in enumerate(raw["lapt"]):
        for k, t in enumerate(lt, 1):
            if k in sc_laps or k == 1:
                continue
            if best is None or t < best[0]:
                best = (t, i, k)
    if best is None:
        return None
    return {"code": raw["ctx"].cars[best[1]].code, "time": round(float(best[0]), 3), "lap": best[2]}


def _sc_laps(raw: dict) -> set:
    s = set()
    for e in raw["events"]:
        if e["kind"] in ("SC", "VSC"):
            s.update(range(e["lap"], e.get("until_lap", e["lap"]) + 1))
    return s


# ---------------------------------------------------------------------------------------- replay
def build_replay(raw: dict, cls: dict, dt: float = REPLAY_DT) -> dict:
    ctx: Ctx = raw["ctx"]
    cars, L = ctx.cars, ctx.length
    s0 = cars[0].surf
    dtr = np.asarray(s0.dt_ref, float)
    u_s = np.concatenate([[0.0], np.cumsum(dtr) / dtr.sum()])
    f_s = np.linspace(0.0, 1.0, len(u_s))
    v_ref = np.asarray(s0.v_ref, float)
    vk = np.concatenate([v_ref, v_ref[:1]]) * 3.6
    ref_time = float(dtr.sum())
    ends = {}
    for r in cls["rows"]:
        i = r["_i"]
        ends[i] = r["laps"]
    t_end = 0.0
    for i, k in ends.items():
        t_end = max(t_end, raw["cum"][i][min(k, len(raw["cum"][i]) - 1)])
    t = np.arange(0.0, t_end + dt, dt)
    out = {}
    for i, c in enumerate(cars):
        Tk = raw["cum"][i][: ends[i] + 1]
        n = len(t)
        d = np.zeros(n)
        pit = np.zeros(n, dtype=np.int8)
        spd = np.zeros(n)
        cidx = np.zeros(n, dtype=np.int8)
        slot = GRID_SLOT_M * (raw["grid_pos"][i] - 1)
        stint_ci = []
        for st in raw["stints"][i]:
            stint_ci.append((st["start"], tyres_idx(ctx.compounds[st["ci"]])))
        for k in range(1, len(Tk)):
            a, b = np.searchsorted(t, [Tk[k - 1], Tk[k]], side="left")
            if k == len(Tk) - 1:
                b = np.searchsorted(t, Tk[k], side="right")
            if b <= a:
                continue
            tau = t[a:b] - Tk[k - 1]
            dur = Tk[k] - Tk[k - 1]
            pd = raw["pit_dur"][i].get(k)
            if pd:
                A = max(dur - pd, 0.5 * dur) * (1.0 - PIT_LANE_FRAC)
                inA = tau < A
                fA = np.interp(np.minimum(tau / A, 1.0), u_s, f_s) * (1.0 - PIT_LANE_FRAC)
                fB = (1.0 - PIT_LANE_FRAC) + PIT_LANE_FRAC * np.clip((tau - A) / max(dur - A, 1e-6), 0, 1)
                f = np.where(inA, fA, fB)
                pit[a:b] = (~inA).astype(np.int8)
                fa = np.minimum(f / (1.0 - PIT_LANE_FRAC), 1.0)
                sp_ = np.where(inA, np.interp(fa, np.linspace(0, 1, len(vk)), vk) * ref_time / max(A, 1e-6), 70.0)
            else:
                f = np.interp(tau / dur, u_s, f_s)
                sp_ = np.interp(f, np.linspace(0, 1, len(vk)), vk) * ref_time / dur
            if k == 1:
                d[a:b] = f * (L + slot) - slot
            else:
                d[a:b] = (k - 1 + f) * L
            spd[a:b] = sp_
            ci_k = [x for s_, x in stint_ci if s_ <= k][-1]
            cidx[a:b] = ci_k
        # after the last knot
        j = np.searchsorted(t, Tk[-1], side="right")
        ri = raw["ret_info"].get(i)
        if ri is not None:
            lap = ri["lap"]
            a = np.searchsorted(t, Tk[-1], side="left")
            b = np.searchsorted(t, ri["t"], side="right")
            if b > a:
                dur = ri["t"] - Tk[-1]
                f = np.interp((t[a:b] - Tk[-1]) / max(dur, 1e-6) * ri["frac"], u_s, f_s)
                d[a:b] = ((lap - 1) + f) * L if lap > 1 else f * (L + slot) - slot
                spd[a:b] = 0.0
            fin = ((lap - 1) + float(np.interp(ri["frac"], u_s, f_s))) * L if lap > 1 else \
                float(np.interp(ri["frac"], u_s, f_s)) * (L + slot) - slot
            lastci = stint_ci[-1][1]
            d[b:] = fin
            cidx[b:] = lastci
            if b > a:
                cidx[a:b] = lastci
        else:
            d[j:] = ends[i] * L
            cidx[j:] = stint_ci[-1][1]
        out[c.code] = {"d": np.round(d, 1).tolist(), "pit": pit.tolist(), "speed": np.round(spd, 1).tolist(),
                       "compound_idx": cidx.tolist()}
    return {"dt": dt, "t": np.round(t, 2).tolist(), "cars": out}


def tyres_idx(compound: str) -> int:
    return list(tyres.COMPOUND).index(compound)


# ---------------------------------------------------------------------------------------- results
def build_result(raw: dict, replay: bool = True) -> dict:
    ctx: Ctx = raw["ctx"]
    cars, N = ctx.cars, ctx.laps
    cls = classify(raw)
    rows = cls["rows"]
    code_of = {c.idx: c.code for c in cars}
    grid_pos = raw["grid_pos"]
    final_pos = {r["_i"]: r["pos"] for r in rows}

    lap_chart = {}
    for i, c in enumerate(cars):
        series = [grid_pos[i]]
        for lo in raw["lap_order"][1:]:
            series.append(lo.index(i) + 1 if i in lo else len(cars))
        series = (series + [series[-1]] * (N + 1))[: N + 1]
        series[-1] = final_pos[i]
        lap_chart[c.code] = series

    gaps, lap_times, strat, energy = {}, {}, {}, {}
    leader_cum = {}
    for k in range(1, N + 1):
        cs = [raw["cum"][i][k] for i in range(len(cars)) if len(raw["cum"][i]) > k]
        leader_cum[k] = min(cs) if cs else 0.0
    for i, c in enumerate(cars):
        cm = raw["cum"][i]
        gaps[c.code] = [round(cm[k] - leader_cum[k], 3) for k in range(1, len(cm))]
        lap_times[c.code] = [round(x, 3) for x in raw["lapt"][i]]
        energy[c.code] = raw["energy"][i]
        sl = []
        last_lap = len(cm) - 1
        for st in raw["stints"][i]:
            comp = ctx.compounds[st["ci"]]
            end = st.get("end", last_lap)
            if st["start"] > max(end, 1) and st is not raw["stints"][i][0]:
                continue
            sl.append({"compound": comp, "label": strategy.label_of(comp, ctx.compounds), "start_lap": st["start"],
                       "end_lap": max(end, st["start"])})
        strat[c.code] = sl

    classification = [{k: v for k, v in r.items() if k != "_i"} for r in rows]
    sc = _sc_laps(raw)
    out = {
        "track": ctx.track_id, "laps": N, "lap_length": round(ctx.length, 2),
        "entries": [{"code": c.code, "driver": c.driver, "team": c.team, "number": c.number,
                     "grid": grid_pos[c.idx]} for c in cars],
        "classification": classification,
        "fastest_lap": _fastest(raw, sc), "lap_chart": lap_chart, "gaps": gaps, "lap_times": lap_times,
        "strategy": strat, "energy": energy,
        "overtakes": sorted(raw["overtakes"], key=lambda o: (o["lap"], o["t"])),
        "events": sorted(raw["events"], key=lambda e: e["t"]),
        "warnings": list(ctx.warnings), "monte_carlo": None,
    }
    if replay:
        out["replay"] = build_replay(raw, cls)
        p = ctx.path
        step = max(1, len(p["x"]) // 600)
        zs = p["z"] if "z" in p else np.zeros(len(p["x"]))
        out["line"] = {k: np.round(np.asarray(v)[::step], 3).tolist()
                       for k, v in (("s", p["s"]), ("x", p["x"]), ("y", p["y"]), ("z", zs), ("psi", p["psi"]))}
    return out


def run_race(track_id: str, entries: list, laps=None, grid="quali", options: dict | None = None, seed: int = 0,
             progress=None) -> dict:
    options = options or {}
    ctx = prepare(track_id, entries, laps, options, progress, progress_span=0.8)
    if progress:
        progress(0.85, "racing")
    raw = simulate(ctx, grid, options, seed)
    if progress:
        progress(0.95, "building replay")
    res = build_result(raw)
    if progress:
        progress(1.0, "done")
    return res

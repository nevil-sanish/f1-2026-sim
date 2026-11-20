"""Quasi-steady-state lap solver (brief §8.1).

Point mass with speed-indexed limits from f1sim.car.vehicle:
  1. corner speed from v^2*|kappa| = ay_max(v)            (monotone table lookup)
  2. forward pass: traction limit, friction ellipse, power P/v - drag - rolling, rev limit
  3. backward pass: braking limit with bias model, drag assist
  4. energy: MGU-K deployment water-filled under the per-lap budget, SoC trace, active aero modes
  5. tyre thermal + wear and brake temperature models, fed back with one re-evaluation
"""
from __future__ import annotations

import math

import numpy as np

from .car import brakes, tyres, vehicle
from .car.powertrain import ETA_K

try:  # numba is optional: same kernel runs as plain Python without it
    from numba import njit
    HAVE_NUMBA = True
except Exception:  # noqa: BLE001
    HAVE_NUMBA = False

    def njit(*a, **k):
        def wrap(f):
            return f
        return wrap(a[0]) if a and callable(a[0]) else wrap

G = 9.81
CUT_RAMP = 1.5       # m/s width of the deployment cut-off ramp
P_LIFT_REGEN = 120e3   # W harvested while lifting and coasting (estimate)


@njit(cache=True)
def _tab(t, v):
    """Linear interpolation in a table on a 1 m/s grid."""
    if v <= 0.0:
        return t[0]
    i = int(v)
    if i >= t.shape[0] - 1:
        return t[t.shape[0] - 1]
    f = v - i
    return t[i] * (1.0 - f) + t[i + 1] * f


@njit(cache=True)
def _passes(kap, grade, gsc, mode, lift, v_lat, ds, m, ay, axt, axb, resist, p_ice, p_k, v_cut, p_sc, i0,
            v_fwd, v):
    """Forward (acceleration) then backward (braking) pass around the closed lap, starting at the slowest
    corner i0. Arrays kap/grade/gsc/mode/lift/v_lat are per sample; ay/axt/resist are [mode, v] tables."""
    n = kap.shape[0]
    v_fwd[i0] = v_lat[i0]
    for k in range(n):
        i = (i0 + k) % n
        j = (i + 1) % n
        vi = v_fwd[i]
        md = mode[i]
        ay_lim = _tab(ay[md], vi) * gsc[i]
        u = vi * vi * kap[i] / ay_lim
        lat_left = math.sqrt(1.0 - u * u) if u < 1.0 else 0.0     # friction ellipse
        if lift[i] > 0:
            f_drive = -P_LIFT_REGEN / max(vi, 10.0)
        else:
            p = _tab(p_ice, vi)
            fd = min(max((v_cut - vi) / CUT_RAMP + 0.5, 0.0), 1.0)   # smooth cut keeps e_dep continuous in v_cut
            p += _tab(p_k, vi) * fd         # MGU-K deployment (tapered by regulation)
            p -= p_sc * (1.0 - fd)          # super-clipping: ICE drives the MGU-K as a generator
            f_drive = p / max(vi, 8.0)
            f_trac = m * _tab(axt[md], vi) * gsc[i] * lat_left
            if f_drive > f_trac:
                f_drive = f_trac
        a = (f_drive - _tab(resist[md], vi)) / m - G * grade[i]
        v2 = vi * vi + 2.0 * a * ds
        vn = math.sqrt(v2) if v2 > 1.0 else 1.0
        if vn > v_lat[j]:
            vn = v_lat[j]
        if k < n - 1:
            v_fwd[j] = vn
    v[i0] = v_fwd[i0]
    for k in range(n):
        j = (i0 - k) % n          # known point
        i = (j - 1) % n           # point before it
        vj = v[j]
        ay_lim = _tab(ay[0], vj) * gsc[j]
        u = vj * vj * kap[j] / ay_lim
        lat_left = math.sqrt(1.0 - u * u) if u < 1.0 else 0.0
        # braking always in Z mode (flaps closed): grip-limited decel + drag + gravity on a climb
        d = _tab(axb, vj) * gsc[j] * lat_left + _tab(resist[0], vj) / m + G * grade[i]
        vb = math.sqrt(vj * vj + 2.0 * d * ds)
        if k < n - 1:
            v[i] = vb if vb < v_fwd[i] else v_fwd[i]


def _corner_speed(V, kap_abs, gsc, mode):
    """v_lat per sample from v^2*|kappa| = ay_max(v)*grip: kappa_lim(v) = ay(v)/v^2 is monotone decreasing."""
    vg = vehicle.VG[1:]
    out = np.empty(len(kap_abs))
    for md in (0, 1):
        klim = V["ay"][md][1:] / (vg * vg)
        klim = np.minimum.accumulate(klim)            # enforce monotone
        sel = mode == md
        if sel.any():
            out[sel] = np.interp(kap_abs[sel] / gsc[sel], klim[::-1], vg[::-1], left=vg[-1], right=vg[0])
    return np.minimum(out, V["v_rev_limit"])


KAPPA_SMOOTH_M = 18.0   # a car (3.4 m wheelbase, 5.5 m long) cannot follow curvature spikes shorter than this


def smooth_kappa(kappa, ds: float, length_m: float = KAPPA_SMOOTH_M):
    """Signed curvature low-passed on the closed loop (triangular window): removes single-sample spikes of the
    fitted line that no vehicle could follow, keeps the corner shape."""
    k = np.asarray(kappa, float)
    h = max(int(round(0.5 * length_m / ds)), 1)
    w = np.concatenate([np.arange(1, h + 1), np.arange(h - 1, 0, -1)]).astype(float)
    w /= w.sum()
    pad = np.concatenate([k[-h:], k, k[:h]])
    return np.convolve(pad, w, mode="valid")[: len(k)] if len(w) == 2 * h - 1 else k


def aero_modes(kappa, ds, car, v_typ=75.0):
    """X-mode (1) on sections straighter than the threshold radius and long enough to matter; the zone start
    is delayed by the flap transition time. Z-mode (0) elsewhere. (Braking always uses Z in the solver.)"""
    n = len(kappa)
    straight = np.abs(kappa) < 1.0 / car["aero_kappa_thr"]
    mode = np.zeros(n, dtype=np.int64)
    if straight.all():
        mode[:] = 1
        return mode
    start = int(np.argmin(straight))
    order = (np.arange(n) + start) % n
    trans = int(round(car["aero_transition"] * v_typ / ds))
    i = 0
    while i < n:
        if not straight[order[i]]:
            i += 1
            continue
        k = i
        while k < n and straight[order[k]]:
            k += 1
        if (k - i) * ds >= 140.0:
            mode[order[i + trans:k]] = 1
        i = k
    return mode


def _energy(V, car, v, dt, ax, grade, mode, lift, v_cut, p_sc):
    """Per-sample electrical power: deployment (+) and harvest (-), from the solved speed trace."""
    m = V["m"]
    idx = np.arange(len(v))
    res = np.where(mode == 1, np.interp(v, vehicle.VG, V["resist"][1]), np.interp(v, vehicle.VG, V["resist"][0]))
    f_req = m * (ax + G * grade) + res              # tractive force actually needed [N]
    p_ice = np.interp(v, vehicle.VG, V["p_ice"])
    p_k = np.interp(v, vehicle.VG, V["p_k"])
    p_req = f_req * v
    driving = (f_req > 0) & (lift == 0)
    fd = np.clip((v_cut - v) / CUT_RAMP + 0.5, 0.0, 1.0)
    p_dep = np.where(driving, np.clip(p_req - p_ice, 0.0, p_k * fd), 0.0) / ETA_K   # battery side
    full = driving & (fd < 1.0) & (p_req > 0.9 * (p_ice - p_sc))
    p_clip = np.where(full, p_sc * (1.0 - fd), 0.0) * ETA_K
    # braking regen: MGU-K works on the rear axle only, limited to its power rating
    braking = f_req < 0
    rear_share = 1.0 - V["bias"]
    p_brake = np.where(braking, -f_req * v, 0.0)
    p_regen = np.minimum(p_brake * rear_share * car["regen_blend"] / 100.0, car["mguk_power"] * 1000.0) * ETA_K
    p_lift = np.where(lift > 0, P_LIFT_REGEN, 0.0) * ETA_K
    p_harv = (p_regen + p_clip + p_lift) * car["energy_efficiency"]
    return {"p_dep": p_dep, "p_harv": p_harv, "p_regen": p_regen, "p_brake": p_brake, "f_req": f_req,
            "p_req": p_req, "p_ice_avail": p_ice, "e_dep": float(np.sum(p_dep * dt)),
            "e_harv_raw": float(np.sum(p_harv * dt)), "idx": idx}


def solve(path: dict, car: dict, driver: dict | None = None, state: dict | None = None, quali: bool = True,
          thermal: bool = True, detail: bool = True) -> dict:
    """Solve one lap on `path` (dict with s, kappa, grade, ds, optional grip [per-sample], length).

    state: fuel, wear_f/r (0..1), cl_scale (dirty air), rho, bumpiness, override, t_air, t_track.
    """
    st = dict(state or {})
    ds = float(path["ds"])
    kappa_s = smooth_kappa(path["kappa"], ds)
    kap = np.ascontiguousarray(np.abs(kappa_s), dtype=np.float64)
    n = len(kap)
    grade = np.ascontiguousarray(path.get("grade", np.zeros(n)), dtype=np.float64)
    gsc = np.ascontiguousarray(path.get("grip", np.ones(n)), dtype=np.float64)
    mode = aero_modes(kappa_s, ds, car)
    lift = np.zeros(n, dtype=np.int64)
    t_air, t_track = st.get("t_air", 25.0), st.get("t_track", 38.0)
    race_energy = (not quali) or car["pu_mode"] != "quali"
    swing = car["battery_swing"] * 1e6
    p_sc = float(car["super_clip"]) * 1000.0
    v_fwd, v = np.empty(n), np.empty(n)
    vstate = {k: st[k] for k in ("fuel", "wear_f", "wear_r", "rho", "cl_scale", "cd_scale", "bumpiness",
                                 "override", "grip_scale") if k in st}
    result = {}
    temp_f = temp_r = None
    n_iter = 2 if thermal else 1
    for it in range(n_iter):
        if it > 0:
            vstate.update({"temp_f": float(np.mean(result["tyre_temp_f"])), "temp_r": float(np.mean(result["tyre_temp_r"])),
                           "brake_fade": brakes.fade_factor(float(np.max(result["brake_temp_f"])))})
        V = vehicle.build(car, driver, vstate, quali)
        v_lat = _corner_speed(V, kap, gsc, mode)
        i0 = int(np.argmin(v_lat))
        args = (kap, grade, gsc, mode, lift, v_lat, ds, V["m"], V["ay"], V["axt"], V["axb"], V["resist"],
                V["p_ice"], V["p_k"])

        def run(v_cut):
            _passes(*args, v_cut, p_sc, i0, v_fwd, v)
            vm = 0.5 * (v + np.roll(v, -1))
            dt = ds / vm
            ax = (np.roll(v, -1) ** 2 - v ** 2) / (2 * ds)
            en = _energy(V, car, v, dt, ax, grade, mode, lift, v_cut, p_sc)
            e_h = min(en["e_harv_raw"], car["harvest_cap"] * 1e6)
            budget = e_h + (0.5e6 if V["override"] else 0.0)
            if not race_energy:
                budget += swing * car["soc_start"] / 100.0     # quali: may spend the battery
            return dt, ax, en, e_h, budget

        # Water-filling: energy is worth most at low speed (dt/dE ~ 1/v^2), so the optimum is to deploy
        # everywhere below a single cut speed and clip above it. Bisect the cut speed onto the budget.
        v_hi = car["deploy_taper_end"] / 3.6 + 1.0
        dt, ax, en, e_h, budget = run(v_hi)
        v_cut = v_hi
        if en["e_dep"] > budget:
            lo, hi = 15.0, v_hi
            for _ in range(15):
                mid = 0.5 * (lo + hi)
                dt, ax, en, e_h, budget = run(mid)
                if en["e_dep"] > budget:
                    hi = mid
                else:
                    lo = mid
            v_cut = lo
            dt, ax, en, e_h, budget = run(v_cut)
        if it == 0 and car["lift_coast"] > 0:
            # lift-and-coast zones: the last metres before each braking onset
            brk = en["f_req"] < -0.3 * V["m"] * G
            onset = np.flatnonzero(brk & ~np.roll(brk, 1))
            nl = int(round(car["lift_coast"] / ds))
            for o in onset:
                lift[(o - 1 - np.arange(nl)) % n] = 1
            dt, ax, en, e_h, budget = run(v_cut)
        if not thermal and not detail:
            break
        # ---- tyre + brake thermal models on the solved lap
        ay_s = v * v * kappa_s
        m = V["m"]
        wd = V["wd_f"]
        ay_lim = np.where(mode == 1, np.interp(v, vehicle.VG, V["ay"][1]), np.interp(v, vehicle.VG, V["ay"][0])) * gsc
        u_lat = np.clip(np.abs(ay_s) / ay_lim, 0, 1)
        fy_f, fy_r = m * np.abs(ay_s) * wd, m * np.abs(ay_s) * (1 - wd)
        f_drive = np.maximum(en["f_req"], 0.0)
        f_brk = np.maximum(-en["f_req"], 0.0)
        u_trac = np.clip(f_drive / (m * np.interp(v, vehicle.VG, V["axt"][0]) * gsc), 0, 1)
        u_brk = np.clip(f_brk / (m * np.interp(v, vehicle.VG, V["axb"])), 0, 1)
        slip = tyres.SLIP_AT_LIMIT
        # sliding power = force * speed * slip, slip grows with utilisation (linear tyre up to the limit)
        ps_f = (fy_f * u_lat + f_brk * V["bias"] * u_brk * tyres.BRAKE_SLIP_REL) * v * slip
        ps_r = (fy_r * u_lat + f_drive * u_trac * tyres.DRIVE_SLIP_REL
                + f_brk * (1 - V["bias"]) * u_brk * tyres.BRAKE_SLIP_REL) * v * slip
        t0 = car["tyre_start_temp"] if car["blankets"] else t_track
        warm = 2 if quali else 3
        # carcass hysteresis (rolling resistance) heats the tread even on a straight
        a_i = V["aero"]
        ff = np.where(mode == 1, np.interp(v, vehicle.VG, a_i[1]["Ff"]), np.interp(v, vehicle.VG, a_i[0]["Ff"]))
        fr = np.where(mode == 1, np.interp(v, vehicle.VG, a_i[1]["Fr"]), np.interp(v, vehicle.VG, a_i[0]["Fr"]))
        roll_f = tyres.ROLL_HEAT * 0.012 * (m * G * wd + ff) * v
        roll_r = tyres.ROLL_HEAT * 0.012 * (m * G * (1 - wd) + fr) * v
        temp_f = tyres.thermal_lap(ps_f, v, dt, t0, t_air, t_track, tyres.heat_gain(car, "f"), warm, tyres.HEAT_FRACTION_F, roll_f)
        temp_r = tyres.thermal_lap(ps_r, v, dt, t0, t_air, t_track, tyres.heat_gain(car, "r"), warm, tyres.HEAT_FRACTION_R, roll_r)
        p_fr_f = f_brk * V["bias"] * v
        p_fr_r = np.maximum(f_brk * (1 - V["bias"]) * v - en["p_regen"] / ETA_K, 0.0)
        bt_f = brakes.thermal_lap(p_fr_f, v, dt, car["brake_duct"], t_air)
        bt_r = brakes.thermal_lap(p_fr_r, v, dt, car["brake_duct"], t_air)
        result = {"tyre_temp_f": temp_f, "tyre_temp_r": temp_r, "brake_temp_f": bt_f, "brake_temp_r": bt_r,
                  "ps_f": ps_f, "ps_r": ps_r, "u_lat": u_lat, "u_trac": u_trac, "u_brk": u_brk}

    lap_time = float(np.sum(dt))
    # each upshift interrupts torque for shift_time; ~half of that interval's speed gain is lost
    gear_f = np.interp(v, vehicle.VG, V["gear"])
    gear = gear_f.round().astype(int)
    # fractional gear increments keep the lap time continuous in the setup (an integer count flips by whole shifts)
    up_f = float(np.sum(np.clip(np.roll(gear_f, -1) - gear_f, 0.0, 1.0)[en["f_req"] > 0]))
    upshifts = int(round(up_f))
    shift_loss = up_f * car["shift_time"] / 1000.0 * 0.12
    lap_time += shift_loss
    out = {"lap_time": lap_time, "v": v.copy(), "dt": dt, "n": n, "ds": ds, "mass": V["m"], "v_cut": v_cut,
           "e_deploy_mj": en["e_dep"] / 1e6, "e_harvest_mj": e_h / 1e6, "e_budget_mj": budget / 1e6,
           "vmax": float(v.max()), "vmin": float(v.min()), "upshifts": upshifts, "shift_loss": shift_loss}
    if not detail and not thermal:
        return out
    tm = (driver or {}).get("tyre_management", 0.5)
    e_f, e_r = float(np.sum(result["ps_f"] * dt)) / 1e6, float(np.sum(result["ps_r"] * dt)) / 1e6
    out["wear_per_lap_f"] = tyres.wear_per_lap(car, e_f, float(np.mean(temp_f)), tm)
    out["wear_per_lap_r"] = tyres.wear_per_lap(car, e_r, float(np.mean(temp_r)), tm)
    out["slide_energy_mj"] = (e_f, e_r)
    if not detail:
        return out
    # ---- telemetry channels
    soc0 = swing * car["soc_start"] / 100.0 if not race_energy else 0.5 * swing
    harv_scale = e_h / max(en["e_harv_raw"], 1.0)
    soc = soc0 + np.cumsum((en["p_harv"] * harv_scale - en["p_dep"]) * dt)
    p_avail = en["p_ice_avail"] + np.clip((v_cut - v) / CUT_RAMP + 0.5, 0.0, 1.0) * np.interp(v, vehicle.VG, V["p_k"])
    f_avail = np.minimum(p_avail / np.maximum(v, 8.0), m * np.interp(v, vehicle.VG, V["axt"][0]) * gsc)
    throttle = np.clip(en["f_req"] / np.maximum(f_avail, 1.0), 0, 1)
    throttle[lift > 0] = 0.0
    brake = np.clip(-en["f_req"] / (m * np.interp(v, vehicle.VG, V["axb"]) + 1.0), 0, 1)
    mode_out = np.where((mode == 1) & (brake < 0.02), 1, 0)
    rpm = np.interp(v, vehicle.VG, V["rpm"])
    a0, a1 = V["aero"][0], V["aero"][1]
    pick = lambda k: np.where(mode_out == 1, np.interp(v, vehicle.VG, a1[k]), np.interp(v, vehicle.VG, a0[k]))  # noqa: E731
    bal_s = np.where(mode_out == 1, np.interp(v, vehicle.VG, V["balance"][1]), np.interp(v, vehicle.VG, V["balance"][0]))
    out.update({
        "t": np.concatenate([[0.0], np.cumsum(dt)[:-1]]), "ax": ax, "ay": ay_s, "throttle": throttle, "brake": brake,
        "gear": gear, "rpm": rpm, "mode": mode_out, "soc": soc, "p_dep": en["p_dep"], "p_harv": en["p_harv"] * harv_scale,
        "tyre_temp_f": temp_f, "tyre_temp_r": temp_r, "brake_temp_f": result["brake_temp_f"],
        "brake_temp_r": result["brake_temp_r"], "ride_h_f": pick("hf"), "ride_h_r": pick("hr"),
        "downforce": pick("Ff") + pick("Fr"), "balance": bal_s, "u_lat": result["u_lat"], "lift": lift,
        "ideal_bias": np.interp(v, vehicle.VG, V["ideal_bias"]), "bias_eff": np.interp(v, vehicle.VG, V["bias_eff"]),
        "vehicle": V, "soc_min": float(soc.min()), "soc_max": float(soc.max()), "swing": swing,
        "race_energy": race_energy,
    })
    return out

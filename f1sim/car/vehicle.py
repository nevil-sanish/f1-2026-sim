"""Assemble the car model into speed-indexed performance tables used by the lap solver.

For each speed on a 1 m/s grid and each aero mode (0 = Z corner mode, 1 = X straight mode):
  ay_max   lateral acceleration limit (fixed-point over lateral load transfer, per-axle capacity)
  ax_trac  traction-limited acceleration (rear drive, longitudinal load transfer)
  ax_brake grip-limited deceleration with the chosen brake bias (Z mode)
  resist   aero drag + rolling resistance [N]
"""
from __future__ import annotations

import numpy as np

from . import aero, brakes, powertrain, suspension, tyres

G = 9.81
VG = np.arange(0.0, 131.0)   # speed grid [m/s]
CRR = 0.012


def _softmin(a, b, p=6.0):
    """Axle-limited lateral acceleration: the weaker axle limits the car but not as a hard minimum, since slip
    angle can be traded between axles (a stronger rear still helps an understeering car). Equal inputs -> same."""
    return ((a ** -p + b ** -p) / 2.0) ** (-1.0 / p)


def default_state() -> dict:
    return {"fuel": None, "temp_f": None, "temp_r": None, "wear_f": 0.0, "wear_r": 0.0, "rho": aero.RHO0,
            "cl_scale": 1.0, "cd_scale": 1.0, "brake_fade": 1.0, "bumpiness": 0.5, "override": False,
            "grip_scale": 1.0}


def build(car: dict, driver: dict | None = None, state: dict | None = None, quali: bool = True) -> dict:
    st = default_state()
    st.update(state or {})
    drv = driver or {}
    fuel = st["fuel"] if st["fuel"] is not None else (car["quali_fuel"] if quali else car["fuel_load"])
    m = car["mass"] + car["mass_above_min"] + fuel
    wd_f = float(np.clip(car["weight_dist_f"] / 100.0 + car["ballast_shift"] * 4e-5, 0.40, 0.52))
    h, L, ls = car["cg_height"], suspension.WHEELBASE, car["load_sens"]
    sus = suspension.analyse(car, m, wd_f, st["bumpiness"])
    mu_f_lat, mu_f_lon = tyres.axle_mu(car, "f", st["temp_f"], st["wear_f"])
    mu_r_lat, mu_r_lon = tyres.axle_mu(car, "r", st["temp_r"], st["wear_r"])
    gs = st["grip_scale"]
    mu_f_lat *= sus["damper_eff_f"] * sus["comp_f"] * (1.0 - sus["preload_us"]) * gs
    mu_r_lat *= sus["damper_eff_r"] * sus["comp_r"] * gs
    mu_f_lon *= sus["damper_eff_f"] * gs
    mu_r_lon *= sus["damper_eff_r"] * gs
    grip_use = drv.get("grip_use", 1.0)
    pref = drv.get("balance_preference", 0.0) * 0.03   # preferred balance (+ = likes oversteer)
    bias = car["brake_bias"] / 100.0

    ay = np.zeros((2, len(VG)))
    axt = np.zeros_like(ay)
    resist = np.zeros_like(ay)
    balance = np.zeros_like(ay)
    aero_out = []
    for mode in (0, 1):
        a = aero.aero_tables(car, VG, bool(mode), st["rho"], st["cl_scale"], st["cd_scale"])
        aero_out.append(a)
        fzf0 = m * G * wd_f + a["Ff"]
        fzr0 = m * G * (1 - wd_f) + a["Fr"]
        # --- lateral limit: iterate because load transfer depends on ay
        ay_m = np.full(len(VG), 15.0)
        for _ in range(10):
            dff = np.minimum(sus["lt_f"] * ay_m, 0.5 * fzf0)   # inner wheel cannot go below zero load
            dfr = np.minimum(sus["lt_r"] * ay_m, 0.5 * fzr0)
            fy_f = tyres.tyre_force(mu_f_lat, 0.5 * fzf0 + dff, ls) + tyres.tyre_force(mu_f_lat, 0.5 * fzf0 - dff, ls)
            fy_r = tyres.tyre_force(mu_r_lat, 0.5 * fzr0 + dfr, ls) + tyres.tyre_force(mu_r_lat, 0.5 * fzr0 - dfr, ls)
            ay_f = fy_f / (m * wd_f)          # steady state: front axle carries wd_f of m*ay
            ay_r = fy_r / (m * (1 - wd_f))
            ay_m = 0.5 * ay_m + 0.5 * _softmin(ay_f, ay_r)
        bal = 2.0 * (ay_f - ay_r) / (ay_f + ay_r)   # < 0: front saturates first = understeer
        # a driver is quickest with the balance they like; penalty grows with the mismatch
        drv_pen = 1.0 - 0.25 * np.clip(np.abs(bal - pref) - 0.01, 0.0, 0.2)
        ay[mode] = _softmin(ay_f, ay_r) * grip_use * drv_pen
        balance[mode] = bal
        # --- traction limit (rear-wheel drive): load transfers rearward under acceleration
        ax = np.full(len(VG), 8.0)
        for _ in range(8):
            fzr = fzr0 + m * ax * h / L
            ax = 0.5 * ax + 0.5 * 2.0 * tyres.tyre_force(mu_r_lon, 0.5 * fzr, ls) * sus["traction_factor"] / m
        axt[mode] = ax * drv.get("throttle_commit", 1.0)
        resist[mode] = a["drag"] + CRR * (m * G + a["Ff"] + a["Fr"])
    # --- braking limit (always Z mode: flaps close on the brakes)
    a = aero_out[0]
    fzf0 = m * G * wd_f + a["Ff"]
    fzr0 = m * G * (1 - wd_f) + a["Fr"]
    d = np.full(len(VG), 30.0)
    for _ in range(10):
        fzf = fzf0 + m * d * h / L
        fzr = np.maximum(fzr0 - m * d * h / L, 0.0)
        cap_f = 2.0 * tyres.tyre_force(mu_f_lon, 0.5 * fzf, ls)
        cap_r = 2.0 * tyres.tyre_force(mu_r_lon, 0.5 * fzr, ls)
        f_tot, eff = brakes.bias_efficiency(cap_f, cap_r, bias)
        d = 0.5 * d + 0.5 * f_tot / m
    ideal_bias = cap_f / np.maximum(cap_f + cap_r, 1.0)
    axb = d * st["brake_fade"] * drv.get("brake_commit", 1.0)

    pw = powertrain.power_tables(car, VG)
    override = bool(st["override"] or car["override"])
    p_k = powertrain.deploy_table(car, VG, override)
    return {"m": m, "fuel": fuel, "wd_f": wd_f, "ay": ay, "axt": axt, "axb": axb, "resist": resist,
            "balance": balance, "aero": aero_out, "p_ice": pw["p_ice"], "gear": pw["gear"], "rpm": pw["rpm"],
            "v_rev_limit": pw["v_rev_limit"], "p_k": p_k, "ideal_bias": ideal_bias, "bias_eff": eff,
            "bias": bias, "sus": sus, "override": override, "mu": (mu_f_lat, mu_r_lat, mu_f_lon, mu_r_lon),
            "state": st, "quali": quali}

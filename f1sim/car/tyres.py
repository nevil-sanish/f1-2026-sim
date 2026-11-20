"""Tyres: compounds, load sensitivity, pressure/camber penalties, single-node thermal model, energy wear.

Coefficients are estimates; the global grip level (mu_scale) and wear (tyre_wear_scale, DEG_GAIN) are
calibrated against public pole laps and race-stint degradation.
"""
from __future__ import annotations

import numpy as np

# mu: peak friction at the reference load; wear: wear fraction per MJ of sliding energy;
# t_opt: centre of the working window [C]. Softer = more grip, faster wear, lower window.
COMPOUND = {
    "C1": {"mu": 1.620, "wear": 0.0040, "t_opt": 110.0, "label": "hard"},
    "C2": {"mu": 1.645, "wear": 0.0052, "t_opt": 105.0, "label": "hard"},
    "C3": {"mu": 1.670, "wear": 0.0068, "t_opt": 100.0, "label": "medium"},
    "C4": {"mu": 1.695, "wear": 0.0090, "t_opt": 95.0, "label": "soft"},
    "C5": {"mu": 1.720, "wear": 0.0120, "t_opt": 90.0, "label": "soft"},
}
FZ_REF = 4500.0        # reference tyre load for the load-sensitivity law [N]
T_WINDOW = 20.0        # half-width of the working window [K]
P_OPT_F, P_OPT_R = 24.0, 21.5
CAMBER_OPT_F, CAMBER_OPT_R = -3.2, -1.6
WEAR_CLIFF = 0.75      # wear fraction where the cliff starts
DEG_GAIN = 0.085       # grip lost per unit wear before the cliff (calibrated vs stint slopes)
SLIP_AT_LIMIT = 0.10   # ~6 deg slip angle / 10% slip ratio at the limit
DRIVE_SLIP_REL = 0.3    # traction slip ratio (~3%) relative to the lateral slip at the limit: heats less per newton
BRAKE_SLIP_REL = 0.5    # braking slip (~5%)
HEAT_FRACTION_F, HEAT_FRACTION_R = 0.28, 0.20  # fronts steer and brake: more of their slip power stays in the tread
ROLL_HEAT = 0.34        # share of rolling-resistance power that ends up as tread heat
HEAT_FRACTION = 0.35   # share of sliding power that heats the tread node
C_TYRE = 6000.0        # J/K per axle (tread + part of the carcass)


def thermal_factor(temp, compound: str):
    """Grip multiplier vs tread temperature: parabolic window around the compound optimum."""
    d = (np.asarray(temp, float) - COMPOUND[compound]["t_opt"]) / T_WINDOW
    return np.clip(1.0 - 0.025 * d * d, 0.90, 1.0)


def wear_factor(wear):
    """Grip multiplier vs wear fraction (0 new .. 1 gone): linear loss then the cliff."""
    w = np.asarray(wear, float)
    return np.clip(1.0 - DEG_GAIN * w - 0.9 * np.maximum(w - WEAR_CLIFF, 0.0), 0.5, 1.0)


def axle_mu(car: dict, axle: str, temp=None, wear=0.0) -> tuple[float, float]:
    """(lateral mu, longitudinal mu) at the reference load for one axle."""
    c = COMPOUND[car["compound"]]
    front = axle == "f"
    p, p_opt = (car["pressure_f"], P_OPT_F) if front else (car["pressure_r"], P_OPT_R)
    cam, cam_opt = (car["camber_f"], CAMBER_OPT_F) if front else (car["camber_r"], CAMBER_OPT_R)
    base = c["mu"] * car["mu_scale"] * (1.0 - 0.004 * (p - p_opt) ** 2)
    if temp is not None:
        base *= float(thermal_factor(temp, car["compound"]))
    base *= float(wear_factor(wear))
    lat = base * (1.0 - 0.012 * (cam - cam_opt) ** 2)
    lon = base * (1.0 - 0.006 * abs(cam))  # camber shrinks the contact patch for traction / braking
    if front:
        lat *= 1.0 + float(np.clip(0.02 * (car["toe_f"] - 0.10), -0.004, 0.004))  # toe-out helps turn-in a touch
    return lat, lon


def tyre_force(mu: float, fz, ls: float):
    """Load-sensitive tyre force: F = mu * Fz * (Fz/Fz_ref)^(-ls)  (i.e. c * Fz^(1-ls))."""
    fz = np.maximum(fz, 0.0)
    return mu * fz * np.power(np.maximum(fz, 1.0) / FZ_REF, -ls)


def heat_gain(car: dict, axle: str) -> float:
    """Setup effect on tyre heating: low pressure, toe and camber add heat."""
    front = axle == "f"
    p, p_opt = (car["pressure_f"], P_OPT_F) if front else (car["pressure_r"], P_OPT_R)
    toe = car["toe_f"] if front else car["toe_r"]
    cam = car["camber_f"] if front else car["camber_r"]
    return 1.0 + 0.03 * (p_opt - p) + 0.25 * abs(toe) + 0.02 * (abs(cam) - (3.2 if front else 1.6))


def thermal_lap(p_slide: np.ndarray, v: np.ndarray, dt: np.ndarray, t0: float, t_air: float, t_track: float,
                gain: float, laps: int = 2, frac: float = HEAT_FRACTION,
                q_extra=0.0) -> np.ndarray:
    """Integrate the single-node axle temperature along the lap (explicit Euler on the sample grid).

    Heat in  = HEAT_FRACTION * sliding power * setup gain
    Heat out = convection to air (rises with speed) + conduction to the track surface
    `laps` > 1 repeats the lap so the returned trace is a warmed-up (flying) lap.
    """
    n = len(v)
    temp = np.empty(n)
    t = t0
    h_air = 22.0 + 1.1 * v
    q_in = frac * gain * p_slide + q_extra
    for lap in range(laps):
        for i in range(n):
            t += (q_in[i] - h_air[i] * (t - t_air) - 18.0 * (t - t_track)) * dt[i] / C_TYRE
            temp[i] = t
    return temp


def wear_per_lap(car: dict, e_slide_mj: float, mean_temp: float, tyre_management: float = 0.5) -> float:
    """Energy-based wear fraction per lap for one axle."""
    c = COMPOUND[car["compound"]]
    hot = max(0.0, mean_temp - c["t_opt"] - 10.0)
    return (c["wear"] * e_slide_mj * car["tyre_wear_scale"] * (1.0 + 0.02 * hot)
            * (1.0 - 0.25 * (tyre_management - 0.5)))

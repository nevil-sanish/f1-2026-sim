"""Aerodynamics: active aero (Z = corner / X = straight), ride-height-dependent floor, drag.

All coefficients are engineering estimates scaled by calibration factors (ClA_scale, CdA_scale and the
global cla_floor / cda_body). Real 2026 aero maps are confidential.
"""
from __future__ import annotations

import numpy as np

RHO0 = 1.20  # kg/m3 air density at ~25 C sea level
# reference dynamic ride heights where the floor makes its nominal load, and stall heights [mm]
H_REF_F, H_REF_R = 18.0, 42.0
H_STALL_F, H_STALL_R = 7.0, 15.0
HEAVE_FACTOR = 1.6  # heave (third) springs make the axle stiffer in pure heave than 2x wheel rate


def _floor_gain(h, h_ref, h_stall, sens):
    """Ground-effect gain vs dynamic ride height: rises as the floor gets closer to the road,
    then collapses below the stall height (choked diffuser / plank contact -> bottoming, porpoising)."""
    h = np.maximum(h, 0.0)
    g = 1.0 + sens * (1.0 - h / h_ref)
    g_at_stall = 1.0 + sens * (1.0 - h_stall / h_ref)
    stalled = g_at_stall * np.maximum(0.78 - 4.0 * (h_stall - h) / h_stall, 0.25)
    return np.where(h >= h_stall, g, stalled)


def aero_tables(car: dict, v: np.ndarray, x_mode: bool, rho: float = RHO0, cl_scale: float = 1.0,
                cd_scale: float = 1.0) -> dict:
    """Downforce per axle [N], drag [N] and dynamic ride heights [mm] on the speed grid v [m/s]."""
    q = 0.5 * rho * v * v
    fw = 0.036 * car["front_wing"]                       # front wing ClA
    rw = 0.046 * car["rear_wing"] + 0.06 * car["beam_wing"]  # rear wing + beam wing ClA
    wing_cd = 0.0055 * car["front_wing"] + 0.0135 * car["rear_wing"] + 0.02 * car["beam_wing"]
    if x_mode:  # active flaps open: most wing load and drag is shed, the floor keeps working
        fw *= car["x_wing_cl_frac"]
        rw *= car["x_wing_cl_frac"]
        wing_cd *= car["x_wing_cd_frac"]
    fs = car["floor_setting"]
    floor = car["cla_floor"] * (1.0 + 0.025 * (fs - 5))
    sens = 0.45 + 0.04 * (fs - 5)                        # more aggressive floor = more height-sensitive
    kf = 2.0 * car["spring_f"] * HEAVE_FACTOR            # axle heave rate [N/mm]
    kr = 2.0 * car["spring_r"] * HEAVE_FACTOR
    scale = car["ClA_scale"] * cl_scale
    hf = np.full_like(v, float(car["ride_height_f"]))
    hr = np.full_like(v, float(car["ride_height_r"]))
    for _ in range(16):  # fixed point: load -> ride height -> floor gain -> load
        gf = _floor_gain(hf, H_REF_F, H_STALL_F, sens)
        gr = _floor_gain(hr, H_REF_R, H_STALL_R, sens)
        cla_f = (0.435 * floor * gf + 1.10 * fw - 0.05 * rw) * scale
        cla_r = (0.565 * floor * gr - 0.10 * fw + 1.15 * rw) * scale
        hf = 0.65 * hf + 0.35 * (car["ride_height_f"] - q * cla_f / kf)
        hr = 0.65 * hr + 0.35 * (car["ride_height_r"] - q * cla_r / kr)
    gf = _floor_gain(hf, H_REF_F, H_STALL_F, sens)
    gr = _floor_gain(hr, H_REF_R, H_STALL_R, sens)
    cla_f = np.maximum((0.435 * floor * gf + 1.10 * fw - 0.05 * rw) * scale, 0.0)
    cla_r = np.maximum((0.565 * floor * gr - 0.10 * fw + 1.15 * rw) * scale, 0.0)
    cda = (car["cda_body"] + wing_cd + 0.02 * 0.5 * (gf + gr) + 0.006 * (car["brake_duct"] - 3)
           + 0.012 * (abs(car["toe_f"]) + abs(car["toe_r"]))) * car["CdA_scale"] * cd_scale
    return {"Ff": q * cla_f, "Fr": q * cla_r, "drag": q * cda, "cla_f": cla_f, "cla_r": cla_r, "cda": cda,
            "hf": hf, "hr": hr, "stall_f": hf < H_STALL_F, "stall_r": hr < H_STALL_R}

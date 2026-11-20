"""Power unit and driveline: ICE power curve, gears, MGU-K deployment taper (2026 rules)."""
from __future__ import annotations

import numpy as np

ETA_ICE = 0.96   # gearbox + driveline efficiency
ETA_K = 0.97     # MGU-K to wheels
PU_MODE = {"quali": 1.0, "race": 0.985, "save": 0.955}


def ice_shape(rpm):
    """Normalised ICE power vs rpm. Fuel-energy-flow limited: flat from ~10,500 rpm, falling below."""
    rpm = np.asarray(rpm, float)
    low = 1.0 - 0.9 * np.power(np.clip((10500.0 - rpm) / 10500.0, 0, 1), 1.3)
    high = 1.0 - 0.03 * np.clip((rpm - 12000.0) / 1000.0, 0, 5)
    return np.where(rpm < 10500.0, low, high)


def gear_ratios(car: dict) -> np.ndarray:
    return np.array([car[f"gear_{i}"] for i in range(1, 9)]) * car["final_drive"]


def power_tables(car: dict, v: np.ndarray) -> dict:
    """Best-gear ICE wheel power, gear and rpm on the speed grid; zero power beyond the rev limit in 8th."""
    ratios = gear_ratios(car)
    rpm_all = v[None, :] / car["wheel_radius"] * ratios[:, None] * 60.0 / (2 * np.pi)   # (8, nv)
    p_all = car["ice_power"] * 1000.0 * PU_MODE[car["pu_mode"]] * ETA_ICE * ice_shape(rpm_all)
    p_all = np.where(rpm_all <= car["rev_limit"], p_all, -1.0)
    gear = np.argmax(p_all, axis=0)
    p = np.take_along_axis(p_all, gear[None, :], 0)[0]
    rpm = np.take_along_axis(rpm_all, gear[None, :], 0)[0]
    v_rev = car["rev_limit"] * 2 * np.pi / 60.0 * car["wheel_radius"] / ratios[-1]
    limited = p < 0
    return {"p_ice": np.where(limited, 0.0, p), "gear": gear + 1, "rpm": np.where(limited, car["rev_limit"], rpm),
            "v_rev_limit": float(v_rev)}


def deploy_table(car: dict, v: np.ndarray, override: bool = False) -> np.ndarray:
    """MGU-K wheel power available vs speed [W]: full to the taper start, linear to zero at the taper end.
    Override mode (within 1 s of the car ahead) holds full power to 337 km/h."""
    v1 = (337.0 if override else car["deploy_taper_start"]) / 3.6
    v2 = max(car["deploy_taper_end"] / 3.6, v1 + 0.5)
    frac = np.clip((v2 - v) / (v2 - v1), 0.0, 1.0)
    return car["mguk_power"] * 1000.0 * ETA_K * frac

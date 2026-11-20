"""Brakes: bias efficiency and a lumped disc temperature model with fade."""
from __future__ import annotations

import numpy as np

C_DISC = 1700.0      # J/K per axle (two carbon discs + pads)
T_FADE = 1050.0      # C: carbon-carbon oxidation / fade onset
T_COLD = 250.0       # C: poor bite below this


def bias_efficiency(cap_f, cap_r, bias: float):
    """Share of the total tyre braking capacity usable with a fixed front bias.
    Total brake force F is limited by F*bias <= cap_f and F*(1-bias) <= cap_r."""
    f = np.minimum(cap_f / max(bias, 1e-3), cap_r / max(1.0 - bias, 1e-3))
    return f, f / np.maximum(cap_f + cap_r, 1.0)


def thermal_lap(p_fric: np.ndarray, v: np.ndarray, dt: np.ndarray, duct: float, t_air: float, laps: int = 2,
                t0: float = 450.0) -> np.ndarray:
    """Disc temperature along the lap. p_fric = friction power into this axle's discs [W]."""
    n = len(v)
    out = np.empty(n)
    t = t0
    cool = 9.0 * (1.0 + 0.15 * (duct - 3.0)) * np.power(np.maximum(v, 5.0), 0.8)   # W/K
    for _ in range(laps):
        for i in range(n):
            # 4th-power radiation matters above ~700 C
            rad = 2.2e-9 * ((t + 273.0) ** 4 - (t_air + 273.0) ** 4)
            t += (p_fric[i] - cool[i] * (t - t_air) - rad) * dt[i] / C_DISC
            out[i] = t
    return out


def fade_factor(t_peak: float) -> float:
    return float(np.clip(1.0 - 0.0006 * max(0.0, t_peak - T_FADE), 0.8, 1.0))

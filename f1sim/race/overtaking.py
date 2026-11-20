"""Traffic model: dirty air, tow, override mode, overtaking probability and blue flags.

All magnitudes here are ESTIMATES (no team publishes wake data); the per-track overtaking difficulty is
public-derived where race data is cached (see f1sim.calibration.race_targets). The scalar functions are
numba-compiled when numba is available because the race kernel calls them once per car per mini-sector.
"""
from __future__ import annotations

import math

import numpy as np

try:
    from numba import njit
    HAVE_NUMBA = True
except Exception:  # noqa: BLE001 - numba is optional, the same code runs as plain Python
    HAVE_NUMBA = False

    def njit(*a, **k):
        def wrap(f):
            return f
        return wrap(a[0]) if a and callable(a[0]) else wrap

OVERRIDE_GAP = 1.0        # s; 2026 rule: override mode is armed within 1 s of the car ahead (regulation)
FOLLOW_GAP = 0.30         # s; closest a car can follow through a mini-sector without passing
PASSED_GAP = 0.20         # s; a car that has just been overtaken drops this far behind the passer
WAKE_CL_LOSS0 = 0.11      # downforce fraction lost at zero gap (2026 cars are designed to follow closer)
WAKE_CL_TAU = 1.1         # s; e-folding gap of the downforce loss
WAKE_CD_DROP0 = 0.16      # drag fraction saved in the tow at zero gap
WAKE_CD_TAU = 0.7         # s; the tow fades faster than the turbulence
WAKE_WEAR_GAIN = 2.5      # extra tyre wear per unit of downforce lost (sliding in dirty air)
BLUE_FLAG_LOSS = 0.45     # s lost by a lapped car letting a leader through
BLUE_FLAG_PASS_LOSS = 0.10  # s lost by the lapping car
PASS_DELTA_BASE = 0.55    # s/lap pace advantage for a 50% chance per lap on the easiest track (difficulty 0)
PASS_DELTA_SPAN = 3.4     # extra s/lap needed at difficulty 1 (applied to difficulty squared)
RACECRAFT_GAIN = 0.6      # s/lap equivalent of a full-scale racecraft difference
FIRST_LAP_EASE = 0.35     # the threshold shrinks on the opening lap: the field is bunched and two-wide


@njit(cache=True)
def wake_downforce_loss(gap: float) -> float:
    """Fraction of downforce lost when following `gap` seconds behind another car."""
    return WAKE_CL_LOSS0 * math.exp(-max(gap, 0.0) / WAKE_CL_TAU)


@njit(cache=True)
def wake_drag_drop(gap: float) -> float:
    """Fraction of drag saved in the slipstream `gap` seconds behind another car."""
    return WAKE_CD_DROP0 * math.exp(-max(gap, 0.0) / WAKE_CD_TAU)


@njit(cache=True)
def pass_threshold(difficulty: float) -> float:
    """Pace advantage [s/lap] that gives a 50% chance of completing a pass within one lap.

    Quadratic in difficulty: easy tracks (Monza, Spa ~0.3) need well under a second, Monaco (~0.97)
    needs several seconds, i.e. passes there only happen on tyre offsets or mistakes.
    """
    return PASS_DELTA_BASE + PASS_DELTA_SPAN * difficulty * difficulty


@njit(cache=True)
def pass_probability(delta: float, difficulty: float, rc_attack: float, rc_defend: float, zone_share: float,
                     first_lap: bool) -> float:
    """Probability that an attack in one passing zone succeeds.

    delta       attacker's free-air pace advantage over the defender [s/lap], override and tyres included
    zone_share  this zone's share of the lap's overtaking opportunity (all zones sum to 1)

    A logistic in (delta - threshold) gives the per-lap probability; it is split over the zones so that
    the chance of at least one success over a full lap equals the per-lap value.
    """
    if zone_share <= 0.0:
        return 0.0
    thr = pass_threshold(difficulty)
    if first_lap:
        thr *= FIRST_LAP_EASE
    width = 0.18 + 0.45 * difficulty
    x = (delta + RACECRAFT_GAIN * (rc_attack - rc_defend) - thr) / width
    p_lap = 1.0 / (1.0 + math.exp(-max(min(x, 30.0), -30.0)))
    return 1.0 - (1.0 - p_lap) ** min(zone_share, 1.0)


def passing_zones(v: np.ndarray, ds: float, n_sectors: int) -> tuple[np.ndarray, np.ndarray]:
    """Overtaking opportunity per mini-sector from a speed trace.

    A zone is a heavy braking event; its weight grows with the speed shed and with the length of the
    straight leading to it (time spent in the tow / on override). Returns (share per sector summing to 1,
    distance of the braking point representative of each sector).
    """
    n = len(v)
    ax = (np.roll(v, -1) ** 2 - v ** 2) / (2.0 * ds)
    brk = ax < -12.0
    onset = np.flatnonzero(brk & ~np.roll(brk, 1))
    share = np.zeros(n_sectors)
    s_zone = (np.arange(n_sectors) + 0.5) * n * ds / n_sectors
    best = np.zeros(n_sectors)
    for o in onset:
        e = o
        while brk[e % n] and e - o < n:
            e += 1
        dv = v[o] - v[e % n]
        # full-throttle run before the braking point: walk back while the car is not decelerating hard
        b = o - 1
        while ax[b % n] > -3.0 and o - b < n:
            b -= 1
        straight = (o - b) * ds
        w = max(dv - 12.0, 0.0) * min(straight, 1000.0) / 1000.0 * (v[o] / 80.0)
        k = int((o % n) * n_sectors / n)
        share[k] += w
        if w > best[k]:
            best[k], s_zone[k] = w, (o % n) * ds
    if share.sum() <= 0.0:
        share[:] = 1.0
    return share / share.sum(), s_zone

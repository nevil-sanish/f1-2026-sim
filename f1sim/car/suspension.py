"""Suspension: roll stiffness distribution, lateral load transfer, damper efficiency, compliance."""
from __future__ import annotations

import math

TRACK_F, TRACK_R = 1.62, 1.56   # track widths [m] (1900 mm wide car, narrower 2026 tyres)
WHEELBASE = 3.40                # 2026 maximum [m]
K_REF_F, K_REF_R = 190.0, 150.0


def analyse(car: dict, mass: float, wd_f: float, bumpiness: float = 0.5) -> dict:
    """Derived suspension quantities.

    lt_f / lt_r: lateral load transfer per axle per unit lateral acceleration [N per m/s2]
        dFz_f = m * [ (h - h_ra) * roll_dist_f + wd_f * rc_f ] / t_f     (elastic + geometric part)
    """
    kphi_f = (car["spring_f"] + car["arb_f"]) * 1000.0 * TRACK_F ** 2 / 2.0   # N*m/rad
    kphi_r = (car["spring_r"] + car["arb_r"]) * 1000.0 * TRACK_R ** 2 / 2.0
    roll_dist_f = kphi_f / (kphi_f + kphi_r)
    rc_f, rc_r = car["rc_height_f"] / 1000.0, car["rc_height_r"] / 1000.0
    h = car["cg_height"]
    h_ra = rc_f * wd_f + rc_r * (1.0 - wd_f)          # roll-axis height under the CG
    arm = max(h - h_ra, 0.02)
    lt_f = mass * (arm * roll_dist_f + wd_f * rc_f) / TRACK_F
    lt_r = mass * (arm * (1.0 - roll_dist_f) + (1.0 - wd_f) * rc_r) / TRACK_R
    roll_grad = math.degrees(mass * arm / (kphi_f + kphi_r)) * 9.81   # deg per g

    def zeta(bump, reb, k, k_ref):
        # 10 clicks at the reference spring = 0.65 of critical; critical damping scales with sqrt(k)
        return 0.65 * ((bump + reb) / 20.0) * math.sqrt(k_ref / k)

    z_f = zeta(car["damp_bump_f"], car["damp_reb_f"], car["spring_f"], K_REF_F)
    z_r = zeta(car["damp_bump_r"], car["damp_reb_r"], car["spring_r"], K_REF_R)
    eff_f = 1.0 - 0.10 * (z_f - 0.65) ** 2            # optimum damping ratio ~0.6-0.7
    eff_r = 1.0 - 0.10 * (z_r - 0.65) ** 2
    # mechanical compliance: stiff cars lose tyre contact over bumps and kerbs (worse on street circuits)
    stiff_f, stiff_r = car["spring_f"] / K_REF_F, car["spring_r"] / K_REF_R
    comp_f = 1.0 - 0.014 * bumpiness * (stiff_f - 1.0)
    comp_r = 1.0 - 0.014 * bumpiness * (stiff_r - 1.0)
    kerb = 1.0 - 0.05 * (0.5 * (stiff_f + stiff_r)) ** 0.7     # grip factor while riding a kerb
    lock = car["diff_lock"]
    diff_eff = 1.0 - 0.5 * ((lock - 60.0) / 100.0) ** 2        # traction optimum near 60% lock
    preload_us = 0.00006 * (car["diff_preload"] - 60.0)        # preload -> a little mid-corner understeer
    traction = diff_eff * (1.0 - 0.02 * (stiff_r - 1.0))        # softer rear = better traction
    return {"roll_dist_f": roll_dist_f, "lt_f": lt_f, "lt_r": lt_r, "roll_gradient": roll_grad,
            "zeta_f": z_f, "zeta_r": z_r, "damper_eff_f": eff_f, "damper_eff_r": eff_r,
            "comp_f": comp_f, "comp_r": comp_r, "kerb_factor": kerb, "diff_eff": diff_eff,
            "traction_factor": traction, "preload_us": preload_us}

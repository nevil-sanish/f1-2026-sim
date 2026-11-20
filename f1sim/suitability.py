"""Setup suitability flags (brief §1 item 7) from one detailed solver result.

flags(result, car, track) -> [{id, level: "ok"|"warn"|"bad", title, text}]
warnings(track)           -> [str]   persistent notices about the track / data quality
"""
from __future__ import annotations

import numpy as np

from .car import aero, brakes, tyres

G = 9.81
LOW_SPEED, HIGH_SPEED = 45.0, 60.0     # m/s split between slow and fast corners
BAL_OK, BAL_BAD = 0.06, 0.15           # |balance| thresholds (relative front/rear lateral capacity gap)


def _flag(fid, level, title, text):
    return {"id": fid, "level": level, "title": title, "text": text}


def _where(result, track, i) -> str:
    """Human-readable position of sample i: nearest corner or the distance along the lap."""
    n = len(result["v"])
    s = i * result["ds"]
    corners = (track or {}).get("corners") or []
    if corners:
        n_tr = len(track["s"])
        j = i * n_tr / n
        c = min(corners, key=lambda c: min(abs(c["apex"] - j), n_tr - abs(c["apex"] - j)))
        d = min(abs(c["apex"] - j), n_tr - abs(c["apex"] - j)) * track["ds"]
        if d < 60:
            return f"at turn {c['num']} ({s:.0f} m)"
        if d < 220:
            return f"near turn {c['num']} ({s:.0f} m)"
    return f"on the straight at {s:.0f} m"


def _balance(r, car):
    cornering = r["u_lat"] > 0.7
    if not cornering.any():
        return _flag("balance", "ok", "Balance", "No significant cornering on this lap.")
    bal, v = r["balance"], r["v"]
    lo, hi = cornering & (v < LOW_SPEED), cornering & (v > HIGH_SPEED)
    b_all = float(bal[cornering].mean())
    b_lo = float(bal[lo].mean()) if lo.any() else None
    b_hi = float(bal[hi].mean()) if hi.any() else None

    def word(b):
        if abs(b) < BAL_OK:
            return "neutral"
        return ("understeer" if b < 0 else "oversteer") + f" ({abs(b) * 100:.0f}%)"

    worst = max((b for b in (b_lo, b_hi, b_all) if b is not None), key=abs)
    level = "ok" if abs(worst) < BAL_OK else ("warn" if abs(worst) < BAL_BAD else "bad")
    parts = []
    if b_lo is not None:
        parts.append(f"slow corners: {word(b_lo)}")
    if b_hi is not None:
        parts.append(f"fast corners: {word(b_hi)}")
    if not parts:
        parts.append(f"mid-speed corners: {word(b_all)}")
    text = "; ".join(parts).capitalize() + "."
    if level != "ok":
        if worst < 0:
            text += (" The front axle saturates first - try more front wing, a softer front bar or a stiffer"
                     " rear bar.")
        else:
            text += (" The rear axle saturates first - try more rear wing, a stiffer front bar or a softer"
                     " rear bar.")
    else:
        text += " Both axles reach their limit together."
    title = "Balance" if level == "ok" else ("Understeer" if worst < 0 else "Oversteer")
    return _flag("balance", level, title, text)


def _bottoming(r, car, track):
    out = []
    worst_level, texts = "ok", []
    for name, key, stall in (("front", "ride_h_f", aero.H_STALL_F), ("rear", "ride_h_r", aero.H_STALL_R)):
        h = r[key]
        i = int(np.argmin(h))
        hmin, margin = float(h[i]), float(h[i]) - stall
        if margin < 0:
            dist = float(np.sum(h < stall)) * r["ds"]
            texts.append(f"{name} floor stalls: {hmin:.1f} mm (stall at {stall:.0f} mm) for {dist:.0f} m,"
                         f" lowest {_where(r, track, i)} at {r['v'][i] * 3.6:.0f} km/h")
            worst_level = "bad"
        elif margin < 2.0:
            texts.append(f"{name} is {margin:.1f} mm from stalling ({hmin:.1f} mm) {_where(r, track, i)}")
            if worst_level == "ok":
                worst_level = "warn"
        else:
            texts.append(f"{name} minimum {hmin:.1f} mm ({margin:.1f} mm above stall)")
    text = "; ".join(texts).capitalize() + "."
    if worst_level != "ok":
        text += " Raise the static ride height or stiffen the springs."
    out.append(_flag("bottoming", worst_level, "Bottoming / floor stall" if worst_level != "ok" else "Ride height", text))
    return out


def _brake_temp(r, car):
    pf, pr = float(r["brake_temp_f"].max()), float(r["brake_temp_r"].max())
    peak = max(pf, pr)
    axle = "front" if pf >= pr else "rear"
    braking = r["brake"] > 0.1
    # temperature the front discs have when the driver hits the pedal: below T_COLD they lack bite
    t_on = float(np.median(r["brake_temp_f"][braking])) if braking.any() else pf
    if peak > brakes.T_FADE:
        return _flag("brake_temp", "bad", "Brakes overheating",
                     f"{axle.capitalize()} discs peak at {peak:.0f} C, above the {brakes.T_FADE:.0f} C fade limit."
                     " Open the brake ducts" + (" or move bias rearward." if axle == "front" else " or move bias forward."))
    if peak > brakes.T_FADE - 100:
        return _flag("brake_temp", "warn", "Brakes running hot",
                     f"{axle.capitalize()} discs peak at {peak:.0f} C, within {brakes.T_FADE - peak:.0f} C of fade"
                     f" ({brakes.T_FADE:.0f} C). A bigger duct gives margin for traffic.")
    if t_on < brakes.T_COLD:
        return _flag("brake_temp", "warn", "Brakes too cold",
                     f"Front discs are typically {t_on:.0f} C when braking starts (poor bite below"
                     f" {brakes.T_COLD:.0f} C). Close the brake ducts a step - it also cuts drag.")
    return _flag("brake_temp", "ok", "Brake temperature",
                 f"Peak {pf:.0f} C front / {pr:.0f} C rear, fade starts at {brakes.T_FADE:.0f} C.")


def _energy(r, car):
    dep, bud, harv = r["e_deploy_mj"], r["e_budget_mj"], r["e_harvest_mj"]
    swing = r["swing"] / 1e6
    soc_min, soc_max = r["soc_min"] / 1e6, r["soc_max"] / 1e6
    v_cut_kmh = r["v_cut"] * 3.6
    taper_end = car["deploy_taper_end"]
    clipping = v_cut_kmh < taper_end - 0.5 and r["vmax"] * 3.6 > v_cut_kmh
    clip_m = float(np.sum((r["v"] >= r["v_cut"]) & (r["throttle"] > 0.95))) * r["ds"] if clipping else 0.0
    base = f"Deploys {dep:.1f} MJ of a {bud:.1f} MJ budget (harvest {harv:.1f} MJ)"
    if soc_min < -0.05:
        return _flag("energy", "bad", "Battery runs empty",
                     f"{base}, but the battery would be {abs(soc_min):.1f} MJ below empty mid-lap: deployment"
                     " comes before the harvest that pays for it. Start with more charge or harvest earlier"
                     " (lift-and-coast, super-clipping).")
    if soc_max - soc_min > swing + 0.05:
        return _flag("energy", "warn", "Battery swing exceeded",
                     f"{base}; the state of charge moves {soc_max - soc_min:.1f} MJ over the lap, more than the"
                     f" {swing:.1f} MJ usable swing, so some harvest would be lost.")
    if clip_m > 0.12 * r["n"] * r["ds"]:
        return _flag("energy", "warn", "Energy limited (clipping)",
                     f"{base}. Electric power is cut above {v_cut_kmh:.0f} km/h (taper ends at {taper_end:.0f})"
                     f" for {clip_m:.0f} m of full throttle. Less drag or more harvest would help.")
    if clipping:
        return _flag("energy", "ok", "Energy",
                     f"{base}. Deployment stops above {v_cut_kmh:.0f} km/h ({clip_m:.0f} m of clipping), normal for"
                     " this track.")
    return _flag("energy", "ok", "Energy", f"{base}. Not energy limited: full deployment up to the {taper_end:.0f} km/h taper.")


def _rev_limit(r, car):
    v_lim = float(r["vehicle"]["v_rev_limit"])
    vmax = r["vmax"]
    on_lim = float(np.sum(r["v"] >= 0.995 * v_lim)) * r["ds"]
    txt = f"Top speed {vmax * 3.6:.0f} km/h, top gear rev limit at {v_lim * 3.6:.0f} km/h"
    if on_lim > 150:
        return _flag("rev_limit", "bad", "On the rev limiter",
                     f"{txt}: the car sits on the limiter for {on_lim:.0f} m. Use a longer final drive or top gear.")
    if on_lim > 0:
        return _flag("rev_limit", "warn", "Touching the rev limiter",
                     f"{txt}: the limiter is reached for {on_lim:.0f} m. A slightly longer final drive is safer"
                     " with a slipstream.")
    if vmax < 0.88 * v_lim:
        return _flag("rev_limit", "warn", "Gearing too long",
                     f"{txt}: top gear is never fully used. A shorter final drive would accelerate harder.")
    return _flag("rev_limit", "ok", "Gearing", f"{txt} ({(v_lim - vmax) * 3.6:.0f} km/h margin).")


def _tyre_temp(r, car):
    c = tyres.COMPOUND[car["compound"]]
    t_opt, win = c["t_opt"], tyres.T_WINDOW
    tf, tr_ = float(np.mean(r["tyre_temp_f"])), float(np.mean(r["tyre_temp_r"]))
    level, notes = "ok", []
    for name, t in (("front", tf), ("rear", tr_)):
        d = (t - t_opt) / win
        if abs(d) <= 0.6:
            continue
        lv = "bad" if abs(d) > 1.0 else "warn"
        level = "bad" if "bad" in (lv, level) else "warn"
        loss = (1.0 - float(tyres.thermal_factor(t, car["compound"]))) * 100
        notes.append(f"{name}s {'overheating' if d > 0 else 'too cold'} at {t:.0f} C ({loss:.1f}% grip lost)")
    window = f"{car['compound']} window {t_opt - win:.0f}-{t_opt + win:.0f} C"
    if level == "ok":
        return _flag("tyre_temp", "ok", "Tyre temperature", f"Front {tf:.0f} C / rear {tr_:.0f} C average, inside the {window}.")
    hot = any("overheating" in s for s in notes)
    cold = any("too cold" in s for s in notes)
    tip = []
    if hot:
        tip.append("for the hot axle: a harder compound, more pressure, less toe/camber or less sliding")
    if cold:
        tip.append("for the cold axle: a softer compound, lower pressure or more toe")
    title = "Tyres overheating" if hot and not cold else ("Tyres too cold" if cold and not hot else "Tyre temperatures out of window")
    return _flag("tyre_temp", level, title, "; ".join(notes).capitalize() + f" ({window}). Try " + "; ".join(tip) + ".")


def _wear(r, car, track):
    wf, wr = r["wear_per_lap_f"], r["wear_per_lap_r"]
    st = r["vehicle"]["state"]
    if max(wf, wr) <= 0:
        return _flag("wear", "ok", "Tyre wear", "No measurable tyre wear on this lap.")

    def laps_left(a):
        return max(0.0, tyres.WEAR_CLIFF - a[2]) / max(a[1], 1e-9)

    # the limiting axle is the one that reaches the cliff first from its current wear
    axle, rate, used = min((("front", wf, st.get("wear_f", 0.0)), ("rear", wr, st.get("wear_r", 0.0))), key=laps_left)
    left = laps_left((axle, rate, used))
    race_laps = (track or {}).get("laps") or 0
    txt = (f"Wear per lap {wf * 100:.1f}% front / {wr * 100:.1f}% rear: the {axle}s reach the cliff"
           f" ({tyres.WEAR_CLIFF * 100:.0f}% worn) in about {left:.0f} more laps on {car['compound']}")
    if used >= tyres.WEAR_CLIFF:
        return _flag("wear", "bad", "Tyres past the cliff",
                     f"The {axle}s are {used * 100:.0f}% worn, past the {tyres.WEAR_CLIFF * 100:.0f}% cliff: grip is"
                     " collapsing. Pit for a new set.")
    if race_laps:
        txt += f" (race distance {race_laps} laps)."
        # a one-stop needs roughly half distance from a set, a three-stop a quarter
        level = "bad" if left < 0.2 * race_laps else ("warn" if left < 0.4 * race_laps else "ok")
    else:
        txt += "."
        level = "bad" if left < 8 else ("warn" if left < 16 else "ok")
    if level != "ok":
        txt += " Expect extra stops, or use a harder compound."
    return _flag("wear", level, "Tyre wear" if level == "ok" else "High tyre wear", txt)


def _brake_bias(r, car):
    braking = r["brake"] > 0.1
    if not braking.any():
        return _flag("brake_bias", "ok", "Brake bias", "No braking zones on this lap.")
    w = r["brake"][braking]
    ideal = float(np.average(r["ideal_bias"][braking], weights=w)) * 100
    eff = float(np.average(r["bias_eff"][braking], weights=w)) * 100
    bias = car["brake_bias"]
    d = bias - ideal
    txt = f"Bias {bias:.1f}% front, ideal about {ideal:.1f}% over the braking zones; {eff:.0f}% of the tyres' braking grip is used"
    if abs(d) < 1.5 and eff > 96:
        return _flag("brake_bias", "ok", "Brake bias", txt + ".")
    level = "bad" if (abs(d) > 4.0 or eff < 90) else "warn"
    move = f"Move the bias about {abs(d):.1f}% {'rearward' if d > 0 else 'forward'}"
    lock = "fronts lock first" if d > 0 else "rears lock first"
    return _flag("brake_bias", level, "Brake bias too far " + ("forward" if d > 0 else "rearward"),
                 f"{txt} ({lock}). {move}.")


def flags(result: dict, car: dict, track: dict | None = None) -> list[dict]:
    """Suitability card for one detailed solver result (solve(..., thermal=True, detail=True))."""
    out = []
    for fn in (lambda: [_balance(result, car)], lambda: _bottoming(result, car, track),
               lambda: [_brake_temp(result, car)], lambda: [_brake_bias(result, car)],
               lambda: [_energy(result, car)], lambda: [_rev_limit(result, car)],
               lambda: [_tyre_temp(result, car)], lambda: [_wear(result, car, track)]):
        try:
            out.extend(fn())
        except (KeyError, IndexError, ValueError, ZeroDivisionError) as e:
            # a missing solver channel must not break the whole card
            out.append(_flag("internal", "warn", "Check unavailable", f"One suitability check could not run ({e})."))
    return out


def warnings(track: dict | None) -> list[str]:
    """Persistent notices for a track (flat track, sparse centerline, estimated data)."""
    out = []
    tr = track or {}
    if tr.get("elevation_source", "flat") == "flat" or not np.any(np.asarray(tr.get("z", 0.0))):
        out.append("Flat track: no elevation data for this circuit, so gradients are ignored.")
    for note in tr.get("notes") or []:
        if note not in out:
            out.append(note)
    if tr.get("align_rms_m") is None:
        out.append("Start line and driving direction are as digitised (not aligned with public telemetry).")
    out.append("Car data is estimated: team values are reverse-calibrated from public timing, not real team data.")
    out.append("Good for trends and comparisons, not exact real lap times or race results.")
    return out

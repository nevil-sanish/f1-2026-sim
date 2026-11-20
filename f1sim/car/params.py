"""Parameter schema: every adjustable value with default, range, unit, tooltip and provenance.

Provenance tags (shown next to every slider):
  regulation     - an FIA 2026 rule value
  public-derived - fitted from public timing / telemetry (see scripts/calibrate_presets.py)
  estimate       - engineering guess; real team values are secret
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent.parent / "data"
REG, PUB, EST = "regulation", "public-derived", "estimate"


def P(key, group, label, default, lo, hi, step, unit, prov, tip, kind="float", options=None):
    return {"key": key, "group": group, "label": label, "default": default, "min": lo, "max": hi, "step": step,
            "unit": unit, "prov": prov, "tip": tip, "kind": kind, "options": options}


COMPOUNDS = ["C1", "C2", "C3", "C4", "C5"]

SCHEMA = [
    # ---------------------------------------------------------------- mass & balance
    P("mass", "Mass & balance", "Car + driver mass", 768, 768, 800, 1, "kg", REG, "2026 minimum is 768 kg (car + driver, no fuel)."),
    P("mass_above_min", "Mass & balance", "Overweight vs minimum", 0.0, 0, 20, 0.5, "kg", PUB, "Team-specific excess mass fitted from timing data."),
    P("fuel_load", "Mass & balance", "Race start fuel", 70, 5, 100, 1, "kg", EST, "Race fuel load at lights-out. Quali runs use 'Quali fuel'."),
    P("quali_fuel", "Mass & balance", "Quali fuel", 4, 1, 20, 0.5, "kg", EST, "Fuel on board for a qualifying lap."),
    P("weight_dist_f", "Mass & balance", "Front weight distribution", 45.5, 43, 48, 0.1, "%", EST, "Static front axle load share."),
    P("cg_height", "Mass & balance", "CG height", 0.270, 0.22, 0.34, 0.005, "m", EST, "Higher CG = more load transfer."),
    P("ballast_shift", "Mass & balance", "Ballast position", 0, -100, 100, 5, "mm", EST, "Moves ballast forward (+) or rearward (-); shifts weight distribution ~0.4% per 100 mm."),
    # ---------------------------------------------------------------- aero
    P("front_wing", "Aerodynamics", "Front wing angle", 25, 8, 40, 0.5, "deg", EST, "More angle = more front downforce and a little drag."),
    P("rear_wing", "Aerodynamics", "Rear wing angle", 22, 6, 38, 0.5, "deg", EST, "More angle = more rear downforce and more drag."),
    P("beam_wing", "Aerodynamics", "Beam wing level", 1, 0, 3, 1, "", EST, "Extra rear load and drag per step."),
    P("floor_setting", "Aerodynamics", "Floor / diffuser setting", 5, 0, 10, 1, "", EST, "Floor edge/diffuser aggressiveness: more load but more ride-height sensitivity."),
    P("ride_height_f", "Aerodynamics", "Front ride height (static)", 30, 15, 60, 0.5, "mm", EST, "Lower = more floor load until the floor stalls / plank hits."),
    P("ride_height_r", "Aerodynamics", "Rear ride height (static)", 62, 30, 110, 0.5, "mm", EST, "Rear minus front = rake."),
    P("ClA_scale", "Aerodynamics", "Downforce scale (team)", 1.0, 0.85, 1.15, 0.005, "x", PUB, "Team aero efficiency fitted from corner speeds and quali gaps."),
    P("CdA_scale", "Aerodynamics", "Drag scale (team)", 1.0, 0.85, 1.15, 0.005, "x", PUB, "Team drag level fitted from speed-trap data."),
    P("cla_floor", "Aerodynamics", "Floor ClA at reference height", 2.55, 1.5, 3.5, 0.05, "m2", EST, "Base underfloor downforce coefficient x area (2026 ~30% below 2025)."),
    P("cda_body", "Aerodynamics", "Body + wheels CdA", 0.69, 0.4, 0.9, 0.01, "m2", EST, "Drag of the car without wing contribution."),
    P("x_wing_cl_frac", "Aerodynamics", "X-mode wing load fraction", 0.35, 0.1, 0.8, 0.01, "x", EST, "Share of wing downforce left with the active flaps open (straight mode)."),
    P("x_wing_cd_frac", "Aerodynamics", "X-mode wing drag fraction", 0.30, 0.1, 0.8, 0.01, "x", EST, "Share of wing drag left with the active flaps open."),
    P("aero_kappa_thr", "Aerodynamics", "X-mode curvature threshold", 1100, 300, 3000, 50, "m radius", EST, "Flaps open on sections straighter than this radius (real zones are FIA-designated)."),
    P("aero_transition", "Aerodynamics", "Flap transition time", 0.30, 0.05, 1.0, 0.05, "s", EST, "Time for the active aero to switch mode."),
    # ---------------------------------------------------------------- suspension
    P("spring_f", "Suspension", "Front wheel rate", 190, 90, 380, 5, "N/mm", EST, "Stiffer = better aero platform, worse over bumps and kerbs."),
    P("spring_r", "Suspension", "Rear wheel rate", 150, 70, 320, 5, "N/mm", EST, "Stiffer rear helps the platform but hurts traction."),
    P("arb_f", "Suspension", "Front anti-roll bar", 130, 0, 320, 5, "N/mm", EST, "Stiffer front bar moves load transfer forward -> more understeer."),
    P("arb_r", "Suspension", "Rear anti-roll bar", 55, 0, 260, 5, "N/mm", EST, "Stiffer rear bar -> more oversteer."),
    P("damp_bump_f", "Suspension", "Front bump damping", 10, 1, 20, 1, "clicks", EST, "Optimum total damping ratio is about 0.6-0.7."),
    P("damp_reb_f", "Suspension", "Front rebound damping", 10, 1, 20, 1, "clicks", EST, "Rebound damping, front."),
    P("damp_bump_r", "Suspension", "Rear bump damping", 10, 1, 20, 1, "clicks", EST, "Bump damping, rear."),
    P("damp_reb_r", "Suspension", "Rear rebound damping", 10, 1, 20, 1, "clicks", EST, "Rebound damping, rear."),
    P("camber_f", "Suspension", "Front camber", -3.2, -4.5, -1.0, 0.05, "deg", EST, "Negative camber helps cornering grip but heats the inner shoulder."),
    P("camber_r", "Suspension", "Rear camber", -1.6, -3.0, 0.0, 0.05, "deg", EST, "Rear camber trades cornering grip against traction."),
    P("toe_f", "Suspension", "Front toe (out +)", 0.10, -0.2, 0.4, 0.01, "deg", EST, "Toe-out sharpens turn-in, adds tyre heat and a little drag."),
    P("toe_r", "Suspension", "Rear toe (in +)", 0.15, -0.1, 0.5, 0.01, "deg", EST, "Rear toe-in adds stability, heat and drag."),
    P("rc_height_f", "Suspension", "Front roll-centre height", 30, -20, 90, 5, "mm", EST, "Higher = more geometric load transfer at the front."),
    P("rc_height_r", "Suspension", "Rear roll-centre height", 60, 0, 130, 5, "mm", EST, "Higher = more geometric load transfer at the rear."),
    # ---------------------------------------------------------------- tyres
    P("compound", "Tyres", "Compound", "C4", None, None, None, "", EST, "C1 hardest ... C5 softest. Each event nominates three.", kind="choice", options=COMPOUNDS),
    P("pressure_f", "Tyres", "Front pressure", 24.0, 20, 28, 0.1, "psi", EST, "Off-optimum pressure costs grip; low pressure adds heat."),
    P("pressure_r", "Tyres", "Rear pressure", 21.5, 18, 26, 0.1, "psi", EST, "Rear cold pressure."),
    P("tyre_age", "Tyres", "Tyre age", 0, 0, 60, 1, "laps", EST, "Laps already done on this set."),
    P("tyre_start_temp", "Tyres", "Start temperature", 70, 15, 110, 1, "C", REG, "Blanket temperature (70 C) or ambient without blankets."),
    P("blankets", "Tyres", "Tyre blankets", 1, 0, 1, 1, "", REG, "Off = tyres start at track temperature.", kind="bool"),
    P("mu_scale", "Tyres", "Grip scale (global)", 1.0, 0.85, 1.15, 0.005, "x", PUB, "Global tyre grip calibration against public pole laps."),
    P("tyre_wear_scale", "Tyres", "Tyre wear scale (team)", 1.0, 0.6, 1.6, 0.01, "x", PUB, "How hard the car is on its tyres, fitted from race stint degradation."),
    P("load_sens", "Tyres", "Load sensitivity", 0.13, 0.05, 0.25, 0.005, "", EST, "Fy = c*Fz^(1-ls): grip coefficient falls as load rises."),
    # ---------------------------------------------------------------- brakes
    P("brake_bias", "Brakes", "Brake bias (front)", 56.5, 48, 66, 0.1, "%", EST, "The ideal bias moves rearward as downforce rises with speed."),
    P("brake_duct", "Brakes", "Brake duct size", 3, 1, 5, 1, "", EST, "Bigger ducts cool better but add drag."),
    P("regen_blend", "Brakes", "Regen share of rear braking", 70, 0, 100, 1, "%", EST, "How much rear-axle braking is done by the MGU-K (harvest) instead of the discs."),
    # ---------------------------------------------------------------- driveline
    P("final_drive", "Driveline", "Final drive ratio", 5.00, 4.2, 6.0, 0.01, "", EST, "Shorter (higher number) = more acceleration, lower top speed at the rev limit."),
    P("gear_1", "Driveline", "Gear 1", 3.05, 2.4, 3.6, 0.01, "", EST, "Gear ratios default to a geometric progression."),
    P("gear_2", "Driveline", "Gear 2", 2.40, 1.9, 2.9, 0.01, "", EST, ""),
    P("gear_3", "Driveline", "Gear 3", 1.95, 1.5, 2.4, 0.01, "", EST, ""),
    P("gear_4", "Driveline", "Gear 4", 1.62, 1.25, 2.0, 0.01, "", EST, ""),
    P("gear_5", "Driveline", "Gear 5", 1.37, 1.05, 1.7, 0.01, "", EST, ""),
    P("gear_6", "Driveline", "Gear 6", 1.18, 0.9, 1.45, 0.01, "", EST, ""),
    P("gear_7", "Driveline", "Gear 7", 1.03, 0.8, 1.3, 0.01, "", EST, ""),
    P("gear_8", "Driveline", "Gear 8", 0.91, 0.7, 1.15, 0.01, "", EST, ""),
    P("wheel_radius", "Driveline", "Rear rolling radius", 0.352, 0.33, 0.37, 0.001, "m", REG, "2026 18-inch rear tyre, ~710 mm diameter."),
    P("rev_limit", "Driveline", "Rev limit", 12500, 10500, 15000, 100, "rpm", EST, "FIA limit is 15,000 rpm; fuel-flow limits make teams shift near 12,000."),
    P("diff_preload", "Driveline", "Diff preload", 60, 0, 200, 5, "Nm", EST, "More preload = entry stability, more mid-corner understeer."),
    P("diff_lock", "Driveline", "Diff power lock", 60, 20, 100, 1, "%", EST, "Traction is best around 55-65% lock."),
    P("shift_time", "Driveline", "Shift time", 30, 10, 150, 5, "ms", EST, "Torque interruption per upshift."),
    # ---------------------------------------------------------------- power unit
    P("ice_power", "Power unit", "ICE power", 400, 340, 440, 1, "kW", PUB, "Energy-flow limited 1.6 V6 turbo (~400 kW). Team value fitted from speed traps."),
    P("mguk_power", "Power unit", "MGU-K power", 350, 0, 350, 5, "kW", REG, "2026 limit 350 kW."),
    P("deploy_taper_start", "Power unit", "Deployment taper start", 290, 200, 340, 1, "km/h", REG, "Full MGU-K power up to this speed."),
    P("deploy_taper_end", "Power unit", "Deployment taper end", 355, 300, 380, 1, "km/h", REG, "MGU-K power reaches zero here (normal mode)."),
    P("battery_swing", "Power unit", "Usable battery swing", 4.0, 1, 4, 0.1, "MJ", REG, "Maximum SoC window."),
    P("harvest_cap", "Power unit", "Harvest cap per lap", 8.5, 4, 9, 0.1, "MJ", REG, "FIA recovery limit per lap (circuit dependent values were discussed)."),
    P("energy_efficiency", "Power unit", "Harvest efficiency (team)", 0.92, 0.75, 1.0, 0.005, "x", PUB, "Round-trip efficiency of recovered energy, fitted from timing data."),
    P("override", "Power unit", "Override mode", 0, 0, 1, 1, "", REG, "Within 1 s of the car ahead: 350 kW to 337 km/h and +0.5 MJ.", kind="bool"),
    P("lift_coast", "Power unit", "Lift-and-coast distance", 0, 0, 200, 5, "m", EST, "Lift this far before each braking zone to harvest and save fuel."),
    P("super_clip", "Power unit", "Super-clipping harvest", 50, 0, 250, 10, "kW", EST, "Harvest at full throttle at the end of straights once deployment has stopped."),
    P("soc_start", "Power unit", "SoC at lap start", 100, 0, 100, 1, "%", EST, "Share of the battery swing available at the start line."),
    P("pu_mode", "Power unit", "PU mode", "quali", None, None, None, "", EST, "quali: spend the battery; race: net-zero per lap; save: reduced power.", kind="choice", options=["quali", "race", "save"]),
    P("fuel_per_lap", "Power unit", "Fuel burn per 5 km", 1.20, 0.8, 1.8, 0.01, "kg", EST, "Fuel mass burned per 5 km of racing (scaled by track length)."),
    P("reliability", "Power unit", "Reliability (DNF per race)", 4, 0, 30, 1, "%", EST, "Used only when race events are enabled."),
]

GROUPS = []
for _p in SCHEMA:
    if _p["group"] not in GROUPS:
        GROUPS.append(_p["group"])
BY_KEY = {p["key"]: p for p in SCHEMA}
DEFAULTS = {p["key"]: p["default"] for p in SCHEMA}
# parameters calibration is allowed to move per team (brief §6.2)
TEAM_FIT_KEYS = ["ClA_scale", "CdA_scale", "ice_power", "energy_efficiency", "mass_above_min", "tyre_wear_scale"]
# global calibration factors, one per parameter group (brief §12.13: no per-track fudge)
GLOBAL_FIT_KEYS = ["mu_scale", "cla_floor", "cda_body"]


def clamp(key: str, value):
    p = BY_KEY[key]
    if p["kind"] == "choice":
        return value if value in p["options"] else p["default"]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return p["default"]
    return min(max(v, p["min"]), p["max"])


def load_preset(team: str | None) -> dict:
    """Preset file -> {'values': {...}, 'prov': {...}, 'fit': {...}}; falls back to the baseline car."""
    base = {"team": team, "values": {}, "prov": {}, "fit": None, "calibrated": False}
    g = DATA / "presets" / "_global.json"
    if g.exists():
        try:
            base["values"].update(json.loads(g.read_text()).get("values", {}))
        except ValueError:
            pass
    if team:
        f = DATA / "presets" / f"{team}.json"
        if f.exists():
            try:
                d = json.loads(f.read_text())
                base["values"].update(d.get("values", {}))
                base["prov"] = d.get("prov", {})
                base["fit"] = d.get("fit")
                base["calibrated"] = True
            except ValueError:
                pass
    return base


def make_car(team: str | None = None, overrides: dict | None = None) -> dict:
    """Full parameter dict = defaults <- global calibration <- team preset <- user overrides (clamped)."""
    car = dict(DEFAULTS)
    for k, v in load_preset(team)["values"].items():
        if k in BY_KEY:
            car[k] = clamp(k, v)
    for k, v in (overrides or {}).items():
        if k in BY_KEY:
            car[k] = clamp(k, v)
    car["_team"] = team
    return car


def schema_for_api(team: str | None = None) -> dict:
    pre = load_preset(team)
    out = []
    for p in SCHEMA:
        q = dict(p)
        if p["key"] in pre["values"]:
            q["preset"] = clamp(p["key"], pre["values"][p["key"]])
            q["prov"] = pre["prov"].get(p["key"], p["prov"])
        else:
            q["preset"] = p["default"]
        out.append(q)
    return {"groups": GROUPS, "params": out, "calibrated": pre["calibrated"]}

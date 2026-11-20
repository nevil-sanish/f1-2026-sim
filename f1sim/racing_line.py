"""Racing line (brief §8.2).

Lateral offsets alpha_i along the centerline normals, bounded by the track edges. Cost
    (1 - lam) * sum |second difference|^2 / ds^4  (curvature)  +  lam * sum |first difference|^2 (length)
is linear least squares in alpha -> scipy lsq_linear on sparse matrices, re-linearised with the current
segment lengths. An outer golden-section search over lam minimises the simulated lap time.
The line is optimised once per track with the baseline car and cached (data/cache/); it is shared by all cars.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.interpolate import CubicSpline
from scipy.optimize import lsq_linear

from . import solver, tracks
from .car import params

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
VERSION = 5
# Track limits: a car is only "off" when all four wheels are beyond the white line, so drivers place the
# car centre close to the line (two wheels on the kerb / painted run-off).
EDGE_MARGIN = 0.35      # car centre stays this far inside the white line away from kerbs [m]
KERB_USE = 1.0          # extra lateral room where a kerb exists (centre up to ~0.65 m past the line) [m]
KERB_GRIP = 0.955       # grip multiplier while riding a kerb (suspension compliance penalty)
CTRL_SPACING = 9.0


def _bounds(tr: dict, idx: np.ndarray, use_kerbs: bool):
    n = len(tr["s"])
    half = tr["width"] / 2 - EDGE_MARGIN
    hi = np.full(n, half)     # left limit  (+)
    lo = np.full(n, -half)    # right limit (-)
    if use_kerbs:
        for k in tr["kerbs"]:
            rng = np.arange(k["i0"], k["i1"] + 1 if k["i1"] >= k["i0"] else k["i1"] + 1 + n) % n
            if k["side"] == "L":
                hi[rng] = half + KERB_USE
            else:
                lo[rng] = -half - KERB_USE
    return lo[idx], hi[idx], half


def _solve_alpha(cx, cy, nx, ny, lo, hi, lam, iters=3):
    m = len(cx)
    I = sparse.identity(m, format="csr")
    S1 = sparse.csr_matrix((np.ones(m), (np.arange(m), (np.arange(m) + 1) % m)), shape=(m, m))
    Sm = S1.T
    D1 = S1 - I
    D2 = S1 - 2 * I + Sm
    Nx, Ny = sparse.diags(nx), sparse.diags(ny)
    alpha = np.zeros(m)
    for _ in range(iters):
        px, py = cx + alpha * nx, cy + alpha * ny
        seg = np.hypot(np.roll(px, -1) - px, np.roll(py, -1) - py)
        h2 = np.maximum(0.5 * (seg + np.roll(seg, 1)), 1.0) ** 2      # local ds^2 -> D2/ds^2 ~ curvature
        w2 = sparse.diags(np.sqrt(1 - lam) / h2 * 1e2)                # 1e2 keeps both terms O(1)
        w1 = np.sqrt(lam) * 1e-1
        A = sparse.vstack([w2 @ D2 @ Nx, w2 @ D2 @ Ny, w1 * (D1 @ Nx), w1 * (D1 @ Ny)]).tocsr()
        b = -np.concatenate([w2 @ (D2 @ cx), w2 @ (D2 @ cy), w1 * (D1 @ cx), w1 * (D1 @ cy)])
        alpha = lsq_linear(A, b, bounds=(lo, hi), lsmr_tol="auto", max_iter=80).x
    return alpha


def build_path(tr: dict, alpha_ctrl: np.ndarray | None, idx: np.ndarray | None, half: float = 1e9) -> dict:
    """Path dict (uniform ds along the line, same sample count as the centerline) from control offsets."""
    n = len(tr["s"])
    L = tr["length"]
    if alpha_ctrl is None:
        alpha = np.zeros(n)
    else:
        sc = np.append(tr["s"][idx], L)
        alpha = CubicSpline(sc, np.append(alpha_ctrl, alpha_ctrl[0]), bc_type="periodic")(tr["s"])
    px, py = tr["x"] + alpha * tr["nx"], tr["y"] + alpha * tr["ny"]
    sc_full = np.append(tr["s"], L)
    spx = CubicSpline(sc_full, np.append(px, px[0]), bc_type="periodic")
    spy = CubicSpline(sc_full, np.append(py, py[0]), bc_type="periodic")
    fine = np.linspace(0, L, 8 * n + 1)
    seg = np.hypot(np.diff(spx(fine)), np.diff(spy(fine)))
    s_line = np.concatenate([[0], np.cumsum(seg)])
    Ll = s_line[-1]
    su = np.arange(n) * (Ll / n)
    u = np.interp(su, s_line, fine)                 # centerline parameter of each uniform line sample
    dx, dy, ddx, ddy = spx(u, 1), spy(u, 1), spx(u, 2), spy(u, 2)
    kappa = (dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)
    per = lambda a: np.interp(u, sc_full, np.append(a, a[0]))  # noqa: E731
    off = per(alpha)
    z = per(tr["z"])
    ds = Ll / n
    grade = (np.roll(z, -1) - np.roll(z, 1)) / (2 * ds)
    on_kerb = np.clip((np.abs(off) - half) / KERB_USE, 0, 1) if half < 1e8 else np.zeros(n)
    return {"s": su, "x": spx(u), "y": spy(u), "z": z, "psi": np.unwrap(np.arctan2(dy, dx)), "kappa": kappa,
            "grade": grade, "ds": ds, "length": Ll, "offset": off, "grip": 1.0 - (1.0 - KERB_GRIP) * on_kerb,
            "idx_center": u / tr["ds"], "track_id": tr["id"], "center_length": L}


def optimise_line(track_id: str, car: dict | None = None, use_kerbs: bool = True, verbose: bool = False) -> dict:
    tr = tracks.load_track(track_id)
    car = car or params.make_car()
    n = len(tr["s"])
    step = max(1, int(round(CTRL_SPACING / tr["ds"])))
    idx = np.arange(0, n, step)
    if n - idx[-1] < step * 0.6:
        idx = idx[:-1]
    lo, hi, half = _bounds(tr, idx, use_kerbs)
    cx, cy, nx, ny = tr["x"][idx], tr["y"][idx], tr["nx"][idx], tr["ny"][idx]
    state = {"bumpiness": 1.0 if tr["type"] == "street" else 0.5}

    def lap(lam):
        a = _solve_alpha(cx, cy, nx, ny, lo, hi, lam)
        p = build_path(tr, a, idx, half)
        return solver.solve(p, car, None, state, thermal=False, detail=False)["lap_time"], p

    # golden-section search on lam in [0, 0.6]
    gr = (np.sqrt(5) - 1) / 2
    a, b = 0.0, 0.6
    c, d = b - gr * (b - a), a + gr * (b - a)
    fc, pc = lap(c)
    fd, pd = lap(d)
    best = min([(fc, c, pc), (fd, d, pd)], key=lambda t: t[0])
    for _ in range(5):
        if fc < fd:
            b, d, fd, pd = d, c, fc, pc
            c = b - gr * (b - a)
            fc, pc = lap(c)
            cand = (fc, c, pc)
        else:
            a, c, fc, pc = c, d, fd, pd
            d = a + gr * (b - a)
            fd, pd = lap(d)
            cand = (fd, d, pd)
        if cand[0] < best[0]:
            best = cand
    f0, p0 = lap(0.0)
    if f0 < best[0]:
        best = (f0, 0.0, p0)
    t_center = solver.solve(build_path(tr, None, None), car, None, state, thermal=False, detail=False)["lap_time"]
    path = best[2]
    path.update({"lam": float(best[1]), "t_line": float(best[0]), "t_center": float(t_center)})
    if verbose:
        print(f"{track_id}: centerline {t_center:.3f}s -> line {best[0]:.3f}s (lam={best[1]:.3f})")
    return path


_ARR = ["s", "x", "y", "z", "psi", "kappa", "grade", "offset", "grip", "idx_center"]
_SCAL = ["ds", "length", "lam", "t_line", "t_center", "center_length"]


@lru_cache(maxsize=64)
def get_line(track_id: str) -> dict:
    """Cached optimal line for a track (computed on first use, then stored in data/cache/)."""
    f = CACHE / f"line_{track_id}_v{VERSION}.npz"
    tr = tracks.load_track(track_id)
    if f.exists():
        try:
            z = np.load(f)
            if len(z["s"]) == len(tr["s"]) and abs(float(z["center_length"]) - tr["length"]) < 0.5:
                p = {k: z[k] for k in _ARR}
                p.update({k: float(z[k]) for k in _SCAL})
                p["track_id"] = track_id
                return p
        except Exception:  # noqa: BLE001
            pass
    p = optimise_line(track_id)
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(f, **{k: p[k] for k in _ARR + _SCAL})
    return p


def centerline_path(track_id: str) -> dict:
    return build_path(tracks.load_track(track_id), None, None)


def _build_one(track_id: str):
    try:
        p = get_line(track_id)
        return track_id, p["t_center"], p["t_line"], None
    except Exception as e:  # noqa: BLE001
        return track_id, None, None, repr(e)


def build_all(force: bool = False, progress=None) -> list:
    """Pre-compute and cache the racing line for every calendar track (parallel across CPU cores)."""
    from concurrent.futures import ProcessPoolExecutor
    ids = [t["id"] for t in tracks.track_list()]
    if force:
        for f in CACHE.glob("line_*.npz"):
            f.unlink()
        get_line.cache_clear()
    out = []
    with ProcessPoolExecutor() as ex:
        for i, r in enumerate(ex.map(_build_one, ids)):
            out.append(r)
            if progress:
                progress((i + 1) / len(ids), f"racing line {r[0]}")
    get_line.cache_clear()
    return out

#!/usr/bin/env python3
"""Download the CC0 textures and HDRIs used by the 3D scene.

Everything comes from Poly Haven (https://polyhaven.com, all assets CC0). Files land in
web/assets/textures/ and web/assets/hdri/ under fixed names the scene looks for, and
web/assets/manifest.json records source URL, licence and sha256 for each file.

The scene falls back to procedural materials for anything missing, so a partial or failed
fetch is never fatal. Re-running skips files whose hash already matches the manifest.

Usage: python scripts/fetch_assets.py [--force] [--res 1k|2k]
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "web" / "assets"
MANIFEST = ASSETS / "manifest.json"
API = "https://api.polyhaven.com"
HEADERS = {"User-Agent": "f1sim-asset-fetch/1.0"}
LICENCE = "CC0-1.0"

# slot -> candidate Poly Haven asset ids, first one that resolves wins
TEXTURES = {
    "asphalt": ["asphalt_02", "asphalt_04", "asphalt_track", "asphalt_01"],
    "grass": ["sparse_grass", "leafy_grass", "aerial_grass_rock", "forrest_ground_01"],
    "gravel": ["gravel_floor_02", "gravel_floor", "gravelly_sand", "ganges_river_pebbles"],
    "concrete": ["concrete_floor_02", "concrete_wall_008", "concrete_floor"],
}
# output suffix -> candidate map names in the Poly Haven files JSON
TEXTURE_MAPS = {
    "diff": ["Diffuse", "diff", "col_1"],
    "nor": ["nor_gl", "Normal"],
    "rough": ["Rough", "rough", "arm"],
}
HDRIS = {
    "day": ["kloofendal_48d_partly_cloudy_puresky", "kloofendal_43d_clear_puresky", "sunflowers_puresky"],
    "sunset": ["belfast_sunset_puresky", "kloppenheim_06_puresky", "venice_sunset"],
    "night": ["rogland_clear_night", "dikhololo_night", "moonless_golf"],
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_json(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def download(session, url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session.get(url, headers=HEADERS, timeout=120, stream=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(dest)


def fetch_one(session, manifest, rel, url, asset_id, force):
    dest = ASSETS / rel
    old = manifest.get(rel)
    if not force and old and dest.exists() and old.get("sha256") == sha256(dest):
        print(f"  ok       {rel}")
        return True
    try:
        download(session, url, dest)
    except requests.RequestException as e:
        print(f"  FAILED   {rel}: {e}")
        return False
    manifest[rel] = {
        "url": url,
        "source": f"https://polyhaven.com/a/{asset_id}",
        "asset": asset_id,
        "licence": LICENCE,
        "sha256": sha256(dest),
        "bytes": dest.stat().st_size,
    }
    print(f"  fetched  {rel} ({dest.stat().st_size // 1024} kB)")
    return True


def resolve_files(session, candidates):
    for asset_id in candidates:
        try:
            return asset_id, get_json(session, f"{API}/files/{asset_id}")
        except (requests.RequestException, ValueError):
            continue
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="re-download even if the hash matches")
    ap.add_argument("--res", default="1k", choices=["1k", "2k"])
    args = ap.parse_args()

    manifest = {}
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text()).get("files", {})
        except ValueError:
            manifest = {}

    session = requests.Session()
    missing = []

    for slot, candidates in TEXTURES.items():
        print(f"texture {slot}")
        asset_id, files = resolve_files(session, candidates)
        if not files:
            missing.append(f"textures/{slot}_*")
            print("  FAILED   no candidate resolved")
            continue
        for suffix, names in TEXTURE_MAPS.items():
            url = None
            for n in names:
                url = files.get(n, {}).get(args.res, {}).get("jpg", {}).get("url")
                if url:
                    break
            rel = f"textures/{slot}_{suffix}.jpg"
            if not url or not fetch_one(session, manifest, rel, url, asset_id, args.force):
                missing.append(rel)

    for slot, candidates in HDRIS.items():
        print(f"hdri {slot}")
        asset_id, files = resolve_files(session, candidates)
        url = (files or {}).get("hdri", {}).get(args.res, {}).get("hdr", {}).get("url")
        rel = f"hdri/{slot}.hdr"
        if not url or not fetch_one(session, manifest, rel, url, asset_id, args.force):
            missing.append(rel)

    manifest = {k: v for k, v in sorted(manifest.items()) if (ASSETS / k).exists()}
    MANIFEST.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "licence_note": "All files are CC0 assets from Poly Haven (https://polyhaven.com/license).",
        "files": manifest,
    }, indent=2) + "\n")
    print(f"\n{len(manifest)} files in manifest -> {MANIFEST.relative_to(ROOT)}")
    if missing:
        print("missing (scene will use procedural fallbacks): " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())

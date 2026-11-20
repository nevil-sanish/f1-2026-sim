"""Setup files: one JSON per setup in setups/ (brief §7 "Setup files")."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "setups"
MAX_NAME = 60
_META = ("name", "track", "team", "driver", "notes", "saved_at")


def safe_name(name) -> str:
    """Display name -> safe lower-case file stem (letters, digits, '-', '_'), so the display name and the
    stem both address the same file; raises ValueError if nothing is left."""
    stem = re.sub(r"[^a-z0-9_-]+", "_", str(name or "").strip().lower()).strip("_")[:MAX_NAME]
    if not stem:
        raise ValueError("setup name must contain letters or digits")
    return stem


def _path(name, base: Path | None = None) -> Path:
    return (base or DIR) / f"{safe_name(name)}.json"


def _flat(d) -> dict:
    """Keep only scalar values: a setup is a flat {param: value} map."""
    if not isinstance(d, dict):
        return {}
    return {str(k): v for k, v in d.items()
            if isinstance(v, (int, str, bool)) or (isinstance(v, float) and math.isfinite(v))}


def save_setup(data: dict, base: Path | None = None) -> str:
    """Write a setup; returns the file stem to use in /api/setups/<name>."""
    if not isinstance(data, dict):
        raise ValueError("setup must be a JSON object")
    stem = safe_name(data.get("name"))
    doc = {
        "name": str(data.get("name")).strip()[:MAX_NAME],
        "track": data.get("track") if isinstance(data.get("track"), str) else None,
        "team": data.get("team") if isinstance(data.get("team"), str) else None,
        "driver": data.get("driver") if isinstance(data.get("driver"), str) else None,
        "notes": str(data.get("notes") or "")[:2000],
        "values": _flat(data.get("values")),
        "driver_values": _flat(data.get("driver_values")),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = _path(stem, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")   # unique: concurrent saves
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)
    return stem


def load_setup(name, base: Path | None = None) -> dict:
    path = _path(name, base)
    try:
        doc = json.loads(path.read_text())
    except OSError:
        raise KeyError(f"no setup named '{name}'") from None
    except ValueError:
        raise KeyError(f"setup '{name}' is not a valid JSON file") from None
    if not isinstance(doc, dict):
        raise KeyError(f"setup '{name}' is not a valid setup file")
    doc.setdefault("name", path.stem)
    doc.setdefault("values", {})
    doc.setdefault("driver_values", {})
    doc["file"] = path.stem
    return doc


def list_setups(base: Path | None = None) -> list[dict]:
    out = []
    for path in sorted((base or DIR).glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        row = {k: doc.get(k) for k in _META}
        row["name"] = row["name"] or path.stem
        row["file"] = path.stem
        out.append(row)
    out.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return out


def delete_setup(name, base: Path | None = None) -> bool:
    path = _path(name, base)
    if not path.exists():
        return False
    path.unlink()
    return True

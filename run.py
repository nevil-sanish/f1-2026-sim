#!/usr/bin/env python3
"""Start the local F1 2026 simulator: python run.py [--port 8765] [--no-browser] [--host 127.0.0.1]"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GEOJSON = ROOT / "data" / "geojson" / "f1-circuits.geojson"
FETCH_CMD = "python scripts/fetch_data.py"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local F1 2026 lap and race simulator")
    # hosts such as Render set PORT; there the app must listen on all interfaces and run as the read-only demo
    hosted = "PORT" in os.environ
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default="0.0.0.0" if hosted else "127.0.0.1",
                    help="bind address (default: local only, or 0.0.0.0 when PORT is set)")
    ap.add_argument("--public", action="store_true", default=hosted or os.environ.get("F1SIM_PUBLIC") == "1",
                    help="read-only public demo: no saving setups/liveries, no calibration runs")
    ap.add_argument("--no-browser", action="store_true", default=hosted, help="do not open the browser")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args(argv)

    if not GEOJSON.exists():
        print("Track data is missing (data/geojson/f1-circuits.geojson).\n"
              "Fetch it once (needs internet), then start again:\n\n"
              "    pip install -r requirements-fetch.txt\n"
              f"    {FETCH_CMD}\n", file=sys.stderr)
        return 1
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError as e:
        print(f"Missing dependency: {e}.\nInstall with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    sys.path.insert(0, str(ROOT))
    from f1sim import server, solver

    try:
        fetched = json.loads((ROOT / "data" / "public" / "fetched_at.json").read_text()).get("fetched_at")
    except (OSError, ValueError):
        fetched = None
    print("F1 2026 simulator (local only; for trends and comparisons, not exact lap times)")
    print("  numba:      " + ("yes (fast solver)" if solver.HAVE_NUMBA else "not installed (pure-Python solver; pip install numba to speed up)"))
    print("  data as of: " + (fetched or f"no public timing data cached - run: {FETCH_CMD}"))
    if not (server.WEB / "index.html").exists():
        print("  warning:    web/index.html is missing - only the JSON API will work")

    try:
        srv = server.make_server(args.host, args.port, verbose=args.verbose, public=args.public)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"\nPort {args.port} is already in use (is the simulator already running?).\n"
                  f"Stop the other program or choose another port: python run.py --port {args.port + 1}", file=sys.stderr)
        else:
            print(f"\nCould not start the server on {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2
    if args.public:
        print("  mode:       public read-only demo")
    elif not srv.loopback_only:
        print(f"  warning:    bound to {args.host}: the simulator has no authentication, anyone who can reach"
              " this address can use it")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else (f"[{args.host}]" if ":" in args.host else args.host)
    url = f"http://{shown}:{srv.server_address[1]}/"
    print(f"\n  {url}   (Ctrl-C to stop)\n")

    threading.Thread(target=server.warm_up, name="warm-up", daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

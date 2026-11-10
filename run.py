#!/usr/bin/env python3
"""Start Academia Shield: python run.py  (options: --port, --rps, --mode, --reset, --no-browser)"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("Academia Shield needs Python 3.10 or newer. You have " + sys.version.split()[0])

missing = []
for mod in ("fastapi", "uvicorn", "aiohttp", "psutil", "numpy", "sklearn", "joblib"):
    try:
        __import__(mod)
    except ImportError:
        missing.append(mod)
if missing:
    sys.exit("Missing packages: " + ", ".join(missing) + "\nRun: python -m pip install -r requirements.txt")


def main() -> None:
    ap = argparse.ArgumentParser(description="Academia Shield: microservice failure prediction and recovery")
    ap.add_argument("--port", type=int, default=None, help="dashboard port (default 8800)")
    ap.add_argument("--rps", type=float, default=None, help="synthetic traffic, requests per second (default 18)")
    ap.add_argument("--mode", choices=["autonomous", "reactive", "observe"], default="autonomous")
    ap.add_argument("--reset", action="store_true", help="delete incident history before starting")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    args = ap.parse_args()
    if args.port:
        os.environ["FORESIGHT_PORT"] = str(args.port)
    if args.rps is not None:
        os.environ["FORESIGHT_RPS"] = str(args.rps)

    import uvicorn

    from foresight import config as cfg
    from foresight.control.api import create_app
    from foresight.control.supervisor import port_free

    if not port_free(cfg.API_PORT):
        sys.exit(f"Port {cfg.API_PORT} is already in use. Academia Shield may already be running. "
                 f"Close it, or start on another port: python run.py --port {cfg.API_PORT + 10}")
    if args.reset and cfg.DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = cfg.DB_PATH.with_name(cfg.DB_PATH.name + suffix)
            if p.exists():
                p.unlink()

    url = f"http://{cfg.HOST}:{cfg.API_PORT}"
    print(f"\nACADEMIA SHIELD is starting. Dashboard: {url}   API docs: {url}/docs\nPress Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        def opener() -> None:
            # Open once the server is really up; a first-run model retrain can take minutes.
            for _ in range(1200):
                time.sleep(0.5)
                if not port_free(cfg.API_PORT):
                    time.sleep(1.0)
                    webbrowser.open(url)
                    return
        threading.Thread(target=opener, daemon=True).start()
    app = create_app(mode=args.mode, rps=cfg.DEFAULT_RPS)
    uvicorn.run(app, host=cfg.HOST, port=cfg.API_PORT, log_level="warning", ws_ping_interval=20)


if __name__ == "__main__":
    main()

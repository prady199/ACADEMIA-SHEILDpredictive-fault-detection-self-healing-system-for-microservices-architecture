"""A/B/C experiment: the same failures against three protection settings, on the real stack.

    python benchmarks/run_benchmark.py --repeats 1

Every trial starts a fresh platform, warms up, injects one failure, then counts what the
synthetic users experienced over a fixed window. Results go to models/benchmark.json.
Without protection a failed service stays failed, so its numbers depend on the window length.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8870
ENV = {"FORESIGHT_PORT": str(PORT), "FORESIGHT_LB_PORT": "8871", "FORESIGHT_LOADGEN_PORT": "8872",
       "FORESIGHT_SERVICE_PORT_BASE": "19100"}
FAULTS = [
    ("payment", "memory_leak", {"rate_mb_s": 4.0}),
    ("orders", "pool_leak", {"rate_slots_s": 0.6}),
    ("inventory", "latency_ramp", {"rate_ms_s": 12.0}),
    ("auth", "error_ramp", {"rate_per_s": 0.01}),
    ("catalog", "cpu_ramp", {"rate_ms_s": 10.0}),
    ("catalog", "crash", {}),
    ("inventory", "hang", {}),
]
MODES = ["observe", "reactive", "autonomous"]
WARMUP_S = 15
WINDOW_S = 100


def call(path: str, body: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_up(timeout: float = 60) -> None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            if call("/api/state").get("instances"):
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("platform did not start")


def trial(mode: str, rps: float, fault: tuple) -> dict:
    target, kind, params = fault
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, **ENV, "FORESIGHT_DB": str(Path(tmp) / "b.db"), "FORESIGHT_RPS": str(rps)}
        proc = subprocess.Popen([sys.executable, "run.py", "--no-browser", "--mode", mode],
                                cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_up()
            time.sleep(WARMUP_S)
            t0 = int(time.time()) + 1
            call("/api/chaos", {"target": target, "kind": kind, "params": params})
            time.sleep(WINDOW_S + 7)   # requests can take up to 5 s to report
            secs = [s for s in call("/api/traffic?seconds=300") if t0 <= s["ts"] < t0 + WINDOW_S]
            ok = sum(s["ok"] for s in secs)
            fail = sum(s["fail"] for s in secs)
            bad = sum(s["fail"] > 0.05 * max(1, s["ok"] + s["fail"]) for s in secs)
            incs = [i for i in call("/api/incidents") if i["service"] == target]
            state = call("/api/state")
            return {"mode": mode, "target": target, "kind": kind, "ok": ok, "failed": fail, "bad_seconds": bad,
                    "availability_pct": 100 * ok / max(1, ok + fail),
                    "instance_failed": any(i["failed_at"] for i in incs),
                    "prevented": any(i.get("outcome") == "prevented" for i in incs),
                    "ttr_s": next((i["ttr_s"] for i in incs if i.get("ttr_s")), None),
                    "proactive": state["actions"]["proactive"], "reactive": state["actions"]["reactive"]}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--rps", type=float, default=18.0)
    a = ap.parse_args()
    rows = []
    out = ROOT / "models" / "benchmark.json"
    for rep in range(a.repeats):
        for fault in FAULTS:
            for mode in MODES:
                r = trial(mode, a.rps, fault)
                r["repeat"] = rep
                rows.append(r)
                print(json.dumps(r), flush=True)
    summary = {}
    for mode in MODES:
        ms = [r for r in rows if r["mode"] == mode]
        ok, failed = sum(r["ok"] for r in ms), sum(r["failed"] for r in ms)
        ttrs = sorted(r["ttr_s"] for r in ms if r["ttr_s"])
        summary[mode] = {"ok": ok, "failed": failed, "availability_pct": 100 * ok / max(1, ok + failed),
                         "bad_seconds": sum(r["bad_seconds"] for r in ms),
                         "instance_failures": sum(r["instance_failed"] for r in ms),
                         "prevented": sum(r["prevented"] for r in ms), "trials": len(ms),
                         "ttr_median_s": ttrs[len(ttrs) // 2] if ttrs else None}
    out.write_text(json.dumps({
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repeats": a.repeats, "rps": a.rps,
        "window_s": WINDOW_S, "faults": [f"{k} on {t}" for t, k, _ in FAULTS], "modes": summary, "trials": rows,
    }, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()

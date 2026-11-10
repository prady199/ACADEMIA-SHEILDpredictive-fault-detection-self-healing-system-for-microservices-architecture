"""Synthetic users. The per-second success/failure counts here are the ground truth for
user-facing availability: they are measured at the edge, not inferred by the platform."""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import threading
import time
from collections import deque

import psutil
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from foresight import config as cfg


class Gen:
    def __init__(self, lb_port: int, rps: float) -> None:
        self.url = f"http://{cfg.HOST}:{lb_port}/svc/gateway/op"
        self.rps = rps
        self.buckets: dict[int, dict] = {}
        self.history: deque[dict] = deque(maxlen=3600)
        self.totals = {"ok": 0, "fail": 0}
        self.by_op = {op: {"ok": 0, "fail": 0} for op, _ in cfg.TRAFFIC_MIX}
        self.inflight = 0
        self.session: ClientSession | None = None

    def bucket(self, ts: int) -> dict:
        b = self.buckets.get(ts)
        if b is None:
            b = self.buckets[ts] = {"ts": ts, "ok": 0, "fail": 0, "lat": []}
        return b

    async def one(self, op: str) -> None:
        t0 = time.perf_counter()
        ok = False
        self.inflight += 1
        try:
            async with self.session.post(
                f"{self.url}/{op}",
                timeout=ClientTimeout(total=5.0),
                headers={"X-Timeout-Ms": "4500", "X-Caller": "user"},
            ) as r:
                await r.read()
                ok = r.status < 500
        except (asyncio.TimeoutError, OSError, Exception):  # noqa: B014 - users see any failure
            ok = False
        finally:
            self.inflight -= 1
        ms = (time.perf_counter() - t0) * 1000
        b = self.bucket(int(time.time()))
        b["ok" if ok else "fail"] += 1
        self.totals["ok" if ok else "fail"] += 1
        self.by_op[op]["ok" if ok else "fail"] += 1
        if ok:
            b["lat"].append(ms)

    async def run(self) -> None:
        ops, weights = zip(*cfg.TRAFFIC_MIX)
        while True:
            if self.rps <= 0:
                await asyncio.sleep(0.2)
                self.roll()
                continue
            # Poisson arrivals, so traffic has the natural burstiness of real users.
            await asyncio.sleep(random.expovariate(self.rps))
            if self.inflight < 400:  # a client gives up rather than queue forever
                asyncio.create_task(self.one(random.choices(ops, weights)[0]))
            self.roll()

    def roll(self) -> None:
        cutoff = int(time.time()) - 6  # requests may finish up to 5 s after they start
        for ts in [t for t in self.buckets if t < cutoff]:
            b = self.buckets.pop(ts)
            lat = sorted(b.pop("lat"))
            b["p95_ms"] = lat[int(0.95 * (len(lat) - 1))] if lat else None
            self.history.append(b)


async def stats(request: web.Request) -> web.Response:
    g: Gen = request.app["gen"]
    since = int(request.query.get("since", "0"))
    live = []
    for ts, b in sorted(g.buckets.items()):
        if ts > since:
            lat = sorted(b["lat"])
            live.append({"ts": ts, "ok": b["ok"], "fail": b["fail"],
                         "p95_ms": lat[int(0.95 * (len(lat) - 1))] if lat else None})
    done = [b for b in g.history if b["ts"] > since]
    return web.json_response({"rps": g.rps, "totals": g.totals, "by_op": g.by_op,
                              "inflight": g.inflight, "seconds": done + live})


async def set_rate(request: web.Request) -> web.Response:
    g: Gen = request.app["gen"]
    body = await request.json()
    g.rps = max(0.0, min(200.0, float(body["rps"])))
    return web.json_response({"rps": g.rps})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def parent_watchdog(parent_pid: int) -> None:
    while True:
        time.sleep(2)
        if not psutil.pid_exists(parent_pid):
            os._exit(0)


async def main_async(args: argparse.Namespace) -> None:
    gen = Gen(args.lb_port, args.rps)
    app = web.Application()
    app["gen"] = gen
    app.router.add_get("/stats", stats)
    app.router.add_post("/rate", set_rate)
    app.router.add_get("/healthz", health)
    gen.session = ClientSession(connector=TCPConnector(limit=0))
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, cfg.HOST, args.port).start()
    await gen.run()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--lb-port", type=int, required=True)
    p.add_argument("--rps", type=float, required=True)
    p.add_argument("--parent-pid", type=int, required=True)
    args = p.parse_args()
    threading.Thread(target=parent_watchdog, args=(args.parent_pid,), daemon=True).start()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

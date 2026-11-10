"""Layer-7 load balancer that every service-to-service call passes through.

mode=intelligent  picks a replica at random, with odds set by weight / (latency x load). Weight
                  comes from the platform's failure predictions, so traffic leaves an instance
                  *before* it fails. Per-instance circuit breakers plus retries elsewhere.
mode=round_robin  plain rotation over the registered instances: the baseline to beat.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import random
import threading
import time

import psutil
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from foresight import config as cfg

BREAKER_FAILS = 5        # consecutive own failures that open the breaker
BREAKER_OPEN_S = 3.0     # how long it stays open before one trial request
EWMA = 0.2


class Backend:
    def __init__(self, instance: str, port: int) -> None:
        self.instance = instance
        self.port = port
        self.weight = 1.0
        self.draining = False
        self.inflight = 0
        self.ewma_ms = 20.0
        self.requests = 0
        self.failures = 0
        self.consec_fail = 0
        self.open_until = 0.0
        self.trial = False

    def available(self, now: float) -> bool:
        if self.draining or self.weight <= 0:
            return False
        if self.open_until > now:
            return False
        if self.open_until and self.trial:   # half-open: one trial in flight
            return False
        return True

    def score(self) -> float:
        return (self.ewma_ms + 1.0) * (self.inflight + 1) / max(self.weight, 0.01)

    def state(self, now: float) -> str:
        if self.draining:
            return "draining"
        if self.open_until > now:
            return "open"
        if self.open_until:
            return "half-open"
        return "closed"

    def on_result(self, ms: float, own_failure: bool, now: float) -> None:
        self.requests += 1
        if own_failure:
            self.failures += 1
            self.consec_fail += 1
            # A failure is at least as slow as what it cost the caller.
            self.ewma_ms += EWMA * (max(ms, 200.0) - self.ewma_ms)
            if self.consec_fail >= BREAKER_FAILS or (self.open_until and self.trial):
                self.open_until = now + BREAKER_OPEN_S
        else:
            self.consec_fail = 0
            self.open_until = 0.0
            self.ewma_ms += EWMA * (ms - self.ewma_ms)
        self.trial = False


class Balancer:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.routes: dict[str, dict[str, Backend]] = {}
        self.rr: dict[str, itertools.count] = {}
        self.session: ClientSession | None = None
        self.rejected: dict[str, int] = {}
        self.retried = 0

    def update_routes(self, table: dict) -> None:
        for service, rows in table.items():
            current = self.routes.setdefault(service, {})
            seen = set()
            for row in rows:
                seen.add(row["instance"])
                b = current.get(row["instance"])
                if b is None or b.port != row["port"]:
                    b = current[row["instance"]] = Backend(row["instance"], row["port"])
                b.weight = float(row.get("weight", 1.0))
                b.draining = bool(row.get("draining", False))
            for gone in set(current) - seen:
                del current[gone]

    def pick(self, service: str, exclude: set[str]) -> Backend | None:
        backends = list(self.routes.get(service, {}).values())
        if not backends:
            return None
        if self.mode == "round_robin":
            n = next(self.rr.setdefault(service, itertools.count()))
            return backends[n % len(backends)]
        now = time.monotonic()
        usable = [b for b in backends if b.available(now) and b.instance not in exclude]
        if not usable:
            # An open breaker whose timer has expired gets one trial request.
            for b in backends:
                if b.open_until and b.open_until <= now and not b.draining and b.instance not in exclude:
                    b.trial = True
                    return b
            return None
        # Weighted odds rather than always-the-best: every healthy replica keeps some traffic,
        # so its health stays measurable and no single replica takes the whole load.
        chosen = random.choices(usable, [1.0 / b.score() for b in usable])[0] if len(usable) > 1 else usable[0]
        if chosen.open_until:
            chosen.trial = True
        return chosen


async def proxy(request: web.Request) -> web.Response:
    lb: Balancer = request.app["lb"]
    service = request.match_info["service"]
    tail = request.match_info["tail"]
    budget_s = float(request.headers.get("X-Timeout-Ms", "3000")) / 1000.0
    deadline = time.monotonic() + budget_s
    headers = {"X-Timeout-Ms": request.headers.get("X-Timeout-Ms", "3000")}
    tried: set[str] = set()
    intelligent = lb.mode == "intelligent"
    attempts = 2 if intelligent else 1
    last_error = "no instance available"

    for attempt in range(attempts):
        b = lb.pick(service, tried)
        if b is None:
            break
        tried.add(b.instance)
        remaining = deadline - time.monotonic()
        if remaining <= 0.02:
            break
        # Keep time back for a retry whenever another replica could take it.
        spare = len(lb.routes.get(service, {})) > len(tried)
        timeout = remaining * 0.6 if (intelligent and attempt == 0 and spare) else remaining
        t0 = time.perf_counter()
        b.inflight += 1
        own_failure = True
        try:
            async with lb.session.post(
                f"http://{cfg.HOST}:{b.port}/{tail}", headers=headers, timeout=ClientTimeout(total=timeout)
            ) as r:
                body = await r.read()
                source = r.headers.get("X-Error-Source", "")
                # A 503 caused by *its* dependency says nothing about this instance's health.
                own_failure = r.status >= 500 and source != "dep"
                if not own_failure:
                    out = {"Content-Type": "application/json", "X-Served-By": b.instance}
                    if source:
                        out["X-Error-Source"] = source
                    return web.Response(body=body, status=r.status, headers=out)
                last_error = f"{b.instance} returned {r.status}"
        except asyncio.TimeoutError:
            last_error = f"{b.instance} timed out"
        except Exception as e:  # noqa: BLE001 - connection refused, reset, etc.
            last_error = f"{b.instance}: {type(e).__name__}"
        finally:
            b.inflight -= 1
            b.on_result((time.perf_counter() - t0) * 1000, own_failure, time.monotonic())
        if attempt + 1 < attempts:
            lb.retried += 1

    lb.rejected[service] = lb.rejected.get(service, 0) + 1
    return web.json_response(
        {"error": last_error, "source": "lb"}, status=503, headers={"X-Error-Source": "self"}
    )


async def set_routes(request: web.Request) -> web.Response:
    request.app["lb"].update_routes(await request.json())
    return web.json_response({"ok": True})


async def set_mode(request: web.Request) -> web.Response:
    body = await request.json()
    if body.get("mode") not in ("intelligent", "round_robin"):
        return web.json_response({"error": "unknown mode"}, status=400)
    request.app["lb"].mode = body["mode"]
    return web.json_response({"mode": body["mode"]})


async def stats(request: web.Request) -> web.Response:
    lb: Balancer = request.app["lb"]
    now = time.monotonic()
    out = {}
    for service, rows in lb.routes.items():
        out[service] = [
            {
                "instance": b.instance,
                "weight": round(b.weight, 3),
                "inflight": b.inflight,
                "ewma_ms": round(b.ewma_ms, 1),
                "requests": b.requests,
                "failures": b.failures,
                "breaker": b.state(now),
            }
            for b in rows.values()
        ]
    return web.json_response({"mode": lb.mode, "retried": lb.retried, "rejected": lb.rejected, "services": out})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def parent_watchdog(parent_pid: int) -> None:
    while True:
        time.sleep(2)
        if not psutil.pid_exists(parent_pid):
            os._exit(0)


async def main_async(args: argparse.Namespace) -> None:
    lb = Balancer(args.mode)
    app = web.Application()
    app["lb"] = lb
    app.router.add_post("/svc/{service}/{tail:.*}", proxy)
    app.router.add_post("/lb/routes", set_routes)
    app.router.add_post("/lb/mode", set_mode)
    app.router.add_get("/lb/stats", stats)
    app.router.add_get("/healthz", health)
    lb.session = ClientSession(connector=TCPConnector(limit=0))
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, cfg.HOST, args.port).start()
    while True:
        await asyncio.sleep(3600)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--mode", default="intelligent")
    p.add_argument("--parent-pid", type=int, required=True)
    args = p.parse_args()
    threading.Thread(target=parent_watchdog, args=(args.parent_pid,), daemon=True).start()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""A demo microservice: one real OS process, real HTTP, real failure modes.

All instances run this file, parameterised by --service. Each op does a little CPU
work and a little waiting, then calls its downstream services through the load balancer.
Business logic is simulated. Failures are not: a memory leak really allocates memory,
a hang really blocks the process, a crash really kills it.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import os
import random
import sys
import threading
import time
from collections import deque

import psutil
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from foresight import config as cfg

ROLLING_S = 5.0


class Metrics:
    """Rolling window of finished requests plus cumulative per-dependency counters."""

    def __init__(self) -> None:
        self.done: deque[tuple] = deque()
        self.total = 0
        self.deps: dict[str, dict] = {}

    def record(self, total_ms: float, self_ms: float, outcome: str) -> None:
        now = time.monotonic()
        self.done.append((now, total_ms, self_ms, outcome))
        self.total += 1
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self.done and now - self.done[0][0] > ROLLING_S:
            self.done.popleft()

    def edge(self, dep: str, required: bool, ms: float, ok: bool) -> None:
        e = self.deps.setdefault(dep, {"calls": 0, "errors": 0, "ms": 0.0, "required": required})
        e["calls"] += 1
        e["errors"] += 0 if ok else 1
        e["ms"] += ms

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._trim(now)
        rows = list(self.done)
        n = len(rows)
        if not n:
            return dict(rps=0.0, err_self=0.0, err_dep=0.0, p50_ms=0.0, p95_ms=0.0, self_p95_ms=0.0, n=0)
        totals = sorted(r[1] for r in rows)
        selfs = sorted(r[2] for r in rows)
        return dict(
            rps=n / ROLLING_S,
            err_self=sum(r[3] == "self" for r in rows) / n,
            err_dep=sum(r[3] == "dep" for r in rows) / n,
            p50_ms=totals[int(0.50 * (n - 1))],
            p95_ms=totals[int(0.95 * (n - 1))],
            self_p95_ms=selfs[int(0.95 * (n - 1))],
            n=n,
        )


class Faults:
    """Failure modes. Progressive ones can be seen coming; sudden ones cannot."""

    def __init__(self, st: "State") -> None:
        self.st = st
        self.active: dict[str, dict] = {}
        self.leak: list[bytes] = []
        self.ballast: list[bytes] = []
        self.pressure = 0.0
        self.extra_ms = 0.0
        self.error_p = 0.0
        self.cpu_block_ms = 0.0
        self.leaked_slots = 0

    # Sudden faults take effect immediately and bypass the loop.
    def crash(self) -> None:
        os._exit(1)

    def hang(self) -> None:
        # Blocks the event loop for good: the process stays alive but answers nothing.
        def _hang() -> None:
            while True:
                time.sleep(1)
        asyncio.get_running_loop().call_soon(_hang)

    def inject(self, kind: str, params: dict) -> None:
        if kind == "crash":
            asyncio.get_running_loop().call_later(0.05, self.crash)
        elif kind == "hang":
            self.hang()
        else:
            self.active[kind] = {"t0": time.monotonic(), **params}

    def clear(self) -> None:
        self.active.clear()
        self.leak.clear()
        self.ballast.clear()
        self.extra_ms = self.error_p = self.cpu_block_ms = 0.0
        while self.leaked_slots:
            self.st.pool.release()
            self.leaked_slots -= 1
        self._trim_heap()

    @staticmethod
    def _trim_heap() -> None:
        gc.collect()
        if sys.platform.startswith("linux"):
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except OSError:
                pass

    async def run(self) -> None:
        dt = 0.2
        proc = psutil.Process()
        while True:
            await asyncio.sleep(dt)
            now = time.monotonic()
            rss = proc.memory_info().rss / 1_048_576
            if rss > cfg.MEMORY_LIMIT_MB:
                print(f"OOM: rss {rss:.0f} MB over limit {cfg.MEMORY_LIMIT_MB} MB", flush=True)
                os._exit(137)

            # Under memory pressure a real runtime spends its time in GC and paging.
            frac = rss / cfg.MEMORY_LIMIT_MB
            self.pressure = max(0.0, (frac - 0.70) / 0.30)
            if self.pressure > 0:
                self._busy(self.pressure ** 2 * 60)

            a = self.active
            if "memory_leak" in a:
                self.leak.append(b"x" * int(a["memory_leak"]["rate_mb_s"] * dt * 1_048_576))
            if "pool_leak" in a:
                f = a["pool_leak"]
                f["acc"] = f.get("acc", 0.0) + f["rate_slots_s"] * dt
                while f["acc"] >= 1 and self.leaked_slots < cfg.POOL_SIZE:
                    if self.st.pool.locked():
                        break
                    await self.st.pool.acquire()
                    self.leaked_slots += 1
                    f["acc"] -= 1
            if "latency_ramp" in a:
                f = a["latency_ramp"]
                self.extra_ms = min(3000.0, f["rate_ms_s"] * (now - f["t0"]))
            if "error_ramp" in a:
                f = a["error_ramp"]
                self.error_p = min(1.0, f["rate_per_s"] * (now - f["t0"]))
            if "cpu_ramp" in a:
                f = a["cpu_ramp"]
                self.cpu_block_ms = min(2000.0, f["rate_ms_s"] * (now - f["t0"]))
                self._busy(self.cpu_block_ms)

            # Harmless look-alikes: spikes that end on their own. They exist so the
            # model learns that "high" is not the same as "about to fail".
            if "cache_growth" in a:
                f = a["cache_growth"]
                held = sum(map(len, self.ballast)) / 1_048_576
                if now - f["t0"] > f["duration_s"]:
                    self.ballast.clear()
                    del a["cache_growth"]
                    self._trim_heap()
                elif held < f["target_mb"]:
                    self.ballast.append(b"c" * int(f["rate_mb_s"] * dt * 1_048_576))
            if "cpu_burst" in a:
                f = a["cpu_burst"]
                if now - f["t0"] > f["duration_s"]:
                    del a["cpu_burst"]
                else:
                    self._busy(f["block_ms"])
            if "latency_blip" in a:
                f = a["latency_blip"]
                if now - f["t0"] > f["duration_s"]:
                    del a["latency_blip"]
                    self.extra_ms = 0.0
                else:
                    self.extra_ms = f["extra_ms"]
            if "error_blip" in a:
                f = a["error_blip"]
                if now - f["t0"] > f["duration_s"]:
                    del a["error_blip"]
                    self.error_p = 0.0
                else:
                    self.error_p = f["p"]

    @staticmethod
    def _busy(ms: float) -> None:
        end = time.perf_counter() + ms / 1000.0
        while time.perf_counter() < end:
            pass


class State:
    def __init__(self, args: argparse.Namespace) -> None:
        self.service = args.service
        self.instance = args.instance
        self.spec = cfg.TOPOLOGY[args.service]
        self.lb = f"http://{cfg.HOST}:{args.lb_port}"
        self.started = time.monotonic()
        self.metrics = Metrics()
        self.pool = asyncio.Semaphore(cfg.POOL_SIZE)
        self.inflight = 0
        self.lag_ms = deque(maxlen=40)
        self.faults = Faults(self)
        self.session: ClientSession | None = None


async def call_dep(st: State, dep: str, op: str, required: bool) -> bool:
    t0 = time.perf_counter()
    timeout = cfg.TOPOLOGY[dep]["timeout_s"]
    ok = False
    try:
        async with st.session.post(
            f"{st.lb}/svc/{dep}/op/{op}",
            timeout=ClientTimeout(total=timeout),
            headers={"X-Timeout-Ms": str(int(timeout * 1000)), "X-Caller": st.service},
        ) as r:
            await r.read()
            ok = r.status < 500
    except (asyncio.TimeoutError, OSError, Exception):  # noqa: B014 - any transport failure counts
        ok = False
    st.metrics.edge(dep, required, (time.perf_counter() - t0) * 1000, ok)
    return ok


def fail(status: int, source: str, why: str) -> web.Response:
    return web.json_response({"error": why, "source": source}, status=status, headers={"X-Error-Source": source})


async def handle_op(request: web.Request) -> web.Response:
    st: State = request.app["state"]
    op = request.match_info["op"]
    if op not in st.spec["ops"]:
        return fail(404, "client", f"unknown op {op}")
    t0 = time.perf_counter()
    dep_wait = 0.0
    outcome = "ok"
    st.inflight += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(st.pool.acquire(), cfg.POOL_WAIT_S)
            acquired = True
        except asyncio.TimeoutError:
            outcome = "self"
            return fail(503, "self", "worker pool exhausted")

        work_ms, calls = st.spec["ops"][op]
        f = st.faults
        await asyncio.sleep((work_ms * random.uniform(0.7, 1.4) + f.extra_ms) / 1000.0)
        if f.error_p and random.random() < f.error_p:
            outcome = "self"
            return fail(500, "self", "internal error")
        for dep, dep_op, required in calls:
            t1 = time.perf_counter()
            ok = await call_dep(st, dep, dep_op, required)
            dep_wait += time.perf_counter() - t1
            if not ok and required:
                outcome = "dep"
                return fail(503, "dep", f"dependency {dep} failed")
        return web.json_response({"ok": True, "by": st.instance})
    finally:
        if acquired:
            st.pool.release()
        st.inflight -= 1
        total = (time.perf_counter() - t0) * 1000
        st.metrics.record(total, max(0.0, total - dep_wait * 1000), outcome)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_metrics(request: web.Request) -> web.Response:
    st: State = request.app["state"]
    snap = st.metrics.snapshot()
    used = cfg.POOL_SIZE - st.pool._value  # slots held, including leaked ones
    lags = list(st.lag_ms)[-20:]
    snap.update(
        service=st.service,
        instance=st.instance,
        uptime_s=time.monotonic() - st.started,
        inflight=st.inflight,
        pool_pct=max(0, used) / cfg.POOL_SIZE,
        lag_ms=max(lags) if lags else 0.0,
        total=st.metrics.total,
        deps=st.metrics.deps,
        faults=list(st.faults.active),
    )
    return web.json_response(snap)


async def handle_inject(request: web.Request) -> web.Response:
    st: State = request.app["state"]
    body = await request.json()
    st.faults.inject(body["kind"], body.get("params") or {})
    return web.json_response({"ok": True})


async def handle_clear(request: web.Request) -> web.Response:
    request.app["state"].faults.clear()
    return web.json_response({"ok": True})


async def lag_probe(st: State) -> None:
    period = 0.05
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(period)
        st.lag_ms.append(max(0.0, (time.perf_counter() - t0 - period) * 1000))


def parent_watchdog(parent_pid: int) -> None:
    """Never outlive the control plane, even if it is killed outright."""
    while True:
        time.sleep(2)
        if not psutil.pid_exists(parent_pid):
            os._exit(0)


async def main_async(args: argparse.Namespace) -> None:
    st = State(args)
    app = web.Application()
    app["state"] = st
    app.router.add_post("/op/{op}", handle_op)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_post("/chaos/inject", handle_inject)
    app.router.add_post("/chaos/clear", handle_clear)
    st.session = ClientSession(connector=TCPConnector(limit=0))
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, cfg.HOST, args.port, reuse_address=None).start()
    tasks = [asyncio.create_task(lag_probe(st)), asyncio.create_task(st.faults.run())]
    await asyncio.gather(*tasks)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--service", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--lb-port", type=int, required=True)
    p.add_argument("--parent-pid", type=int, required=True)
    args = p.parse_args()
    if os.environ.get("FORESIGHT_BAD_RELEASE") == "1":
        # A bad release: the new process dies during start-up, every time.
        print("FATAL: bad configuration in this release, exiting during start-up", flush=True)
        time.sleep(0.4)
        sys.exit(78)
    threading.Thread(target=parent_watchdog, args=(args.parent_pid,), daemon=True).start()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Health probing, metric scraping, the failure definition and the learned dependency map."""
from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import aiohttp
import psutil

from foresight import config as cfg
from foresight.control.supervisor import Instance


async def scrape(session: aiohttp.ClientSession, inst: Instance) -> dict:
    """One sample. Process stats come from the OS, so they survive a hung service."""
    s: dict = {
        "ts": time.time(), "instance": inst.id, "service": inst.service,
        "alive": inst.alive, "probe_ok": False, "uptime_s": time.time() - inst.started_at,
        "rss_mb": math.nan, "mem_pct": math.nan, "cpu": math.nan, "fds": math.nan,
        "rps": math.nan, "err_self": math.nan, "err_dep": math.nan, "p50_ms": math.nan,
        "p95_ms": math.nan, "self_p95_ms": math.nan, "n": 0, "inflight": math.nan,
        "pool_pct": math.nan, "lag_ms": math.nan, "deps": {}, "faults": [],
    }
    if inst.alive and inst.ps is not None:
        try:
            with inst.ps.oneshot():
                rss = inst.ps.memory_info().rss / 1_048_576
                s["rss_mb"] = rss
                s["mem_pct"] = rss / cfg.MEMORY_LIMIT_MB
                s["cpu"] = inst.ps.cpu_percent(None) / 100.0
                s["fds"] = float(inst.ps.num_fds() if hasattr(inst.ps, "num_fds") else inst.ps.num_handles())
        except psutil.Error:
            pass
    if inst.alive:
        try:
            async with session.get(
                f"http://{cfg.HOST}:{inst.port}/metrics",
                timeout=aiohttp.ClientTimeout(total=cfg.PROBE_TIMEOUT_S),
            ) as r:
                if r.status == 200:
                    s.update(await r.json())
                    s["probe_ok"] = True
        except Exception:  # noqa: BLE001 - timeout or refused both mean "probe failed"
            pass
    return s


@dataclass
class Verdict:
    failed: bool = False
    reason: str | None = None


class FailureDetector:
    """The single definition of "this instance has failed", for labelling and for runtime."""

    def __init__(self) -> None:
        self.probe_fails = 0
        self.err_s = 0
        self.lat_s = 0
        self.stall_s = 0

    def update(self, s: dict) -> Verdict:
        if not s["alive"]:
            return Verdict(True, "process_exit")
        if s["probe_ok"]:
            self.probe_fails = 0
        else:
            self.probe_fails += 1
            if self.probe_fails >= cfg.PROBE_FAILS_DOWN:
                return Verdict(True, "unresponsive")
            return Verdict()
        # With too little traffic to measure a rate, keep the running count rather than reset
        # it: a sick instance that callers have stopped using has not got better.
        if s.get("n", 0) >= cfg.MIN_REQUESTS_FOR_RATE:
            self.err_s = self.err_s + 1 if s["err_self"] >= cfg.SELF_ERROR_FAIL else 0
            self.lat_s = self.lat_s + 1 if s["self_p95_ms"] >= cfg.SELF_P95_FAIL_MS else 0
        self.stall_s = self.stall_s + 1 if s["lag_ms"] >= cfg.LOOP_STALL_FAIL_MS else 0
        if self.stall_s >= cfg.LOOP_STALL_FAIL_S:
            return Verdict(True, "stalled")
        if self.err_s >= cfg.SELF_ERROR_FAIL_S:
            return Verdict(True, "error_rate")
        if self.lat_s >= cfg.SELF_P95_FAIL_S:
            return Verdict(True, "latency")
        return Verdict()


class DependencyMap:
    """Learned purely from observed calls: a service appears here because it made the call."""

    def __init__(self) -> None:
        self.prev: dict[tuple[str, str], dict] = {}
        self.edges: dict[tuple[str, str], dict] = {}
        self.services: set[str] = set()

    def observe(self, s: dict) -> None:
        self.services.add(s["service"])
        for dep, c in (s.get("deps") or {}).items():
            key_prev = (s["instance"], dep)
            p = self.prev.get(key_prev, {"calls": 0, "errors": 0, "ms": 0.0})
            dc = max(0, c["calls"] - p["calls"])
            de = max(0, c["errors"] - p["errors"])
            dm = max(0.0, c["ms"] - p["ms"])
            self.prev[key_prev] = c
            self.services.add(dep)
            e = self.edges.setdefault(
                (s["service"], dep),
                {"src": s["service"], "dst": dep, "required": c.get("required", True), "calls": 0,
                 "errors": 0, "first_seen": time.time(), "window": deque(maxlen=10), "pending": [0, 0, 0.0]},
            )
            e["calls"] += dc
            e["errors"] += de
            e["last_seen"] = time.time()
            e["required"] = c.get("required", True)
            e["pending"][0] += dc
            e["pending"][1] += de
            e["pending"][2] += dm

    def roll(self) -> None:
        """Close this tick's per-edge counters into a 10-second rolling window."""
        for e in self.edges.values():
            e["window"].append(tuple(e["pending"]))
            e["pending"] = [0, 0, 0.0]

    def edge_rows(self) -> list[dict]:
        rows = []
        for e in self.edges.values():
            w = list(e["window"]) or [(0, 0, 0.0)]
            calls = sum(x[0] for x in w)
            rows.append({
                "src": e["src"], "dst": e["dst"], "required": e["required"],
                "rate": calls / max(1, len(w)),
                "error_rate": (sum(x[1] for x in w) / calls) if calls else 0.0,
                "avg_ms": (sum(x[2] for x in w) / calls) if calls else 0.0,
                "total_calls": e["calls"], "total_errors": e["errors"],
            })
        return sorted(rows, key=lambda r: (r["src"], r["dst"]))

    def direct_dependents(self, service: str, required_only: bool = True) -> set[str]:
        return {e["src"] for e in self.edges.values()
                if e["dst"] == service and (e["required"] or not required_only)}

    def direct_dependencies(self, service: str, required_only: bool = True) -> set[str]:
        return {e["dst"] for e in self.edges.values()
                if e["src"] == service and (e["required"] or not required_only)}

    def blast_radius(self, service: str) -> list[str]:
        """Everything that stops working if this service stops, nearest first."""
        seen, order, frontier = {service}, [], deque([service])
        while frontier:
            for dep in sorted(self.direct_dependents(frontier.popleft())):
                if dep not in seen:
                    seen.add(dep)
                    order.append(dep)
                    frontier.append(dep)
        return order

    def transitive_dependencies(self, service: str) -> set[str]:
        seen: set[str] = set()
        stack = [service]
        while stack:
            for d in self.direct_dependencies(stack.pop()):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return seen

    def root_causes(self, own_fault: set[str]) -> set[str]:
        """Only a service with a fault of its own can be a root cause. A service that just
        passes up dependency errors is a symptom, even for the seconds after the origin heals."""
        return set(own_fault)


class History:
    """Recent per-instance samples: feature windows and the dashboard's sparklines."""

    def __init__(self, seconds: int = 900) -> None:
        self.by_instance: dict[str, deque] = defaultdict(lambda: deque(maxlen=seconds))

    def add(self, s: dict) -> None:
        self.by_instance[s["instance"]].append(s)

    def window(self, instance: str, seconds: int = cfg.WINDOW_S) -> list[dict]:
        d = self.by_instance.get(instance)
        return list(d)[-seconds:] if d else []

    def forget(self, instance: str) -> None:
        self.by_instance.pop(instance, None)


async def gather_samples(session: aiohttp.ClientSession, instances: list[Instance]) -> list[dict]:
    return list(await asyncio.gather(*(scrape(session, i) for i in instances)))

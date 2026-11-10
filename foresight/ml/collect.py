"""Chaos campaign: runs the real stack, injects real faults, records telemetry per instance
lifetime. Each process lifetime is one "episode", so a split by episode can't leak.

    python -m foresight.ml.collect --minutes 30
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import random
import time

import aiohttp

from foresight import config as cfg
from foresight.control.supervisor import Instance, Supervisor
from foresight.control.telemetry import FailureDetector, gather_samples
from foresight.ml import faults as fl

KEEP = ["ts", "instance", "service", "alive", "probe_ok", "uptime_s", "rss_mb", "mem_pct", "cpu", "fds", "rps",
        "err_self", "err_dep", "p50_ms", "p95_ms", "self_p95_ms", "n", "inflight", "pool_pct", "lag_ms"]


def clean(v):
    return None if isinstance(v, float) and math.isnan(v) else v


class Campaign:
    def __init__(self, minutes: float, out: str, concurrency: int, seed: int, topup: bool = False) -> None:
        self.topup = topup
        self.prefix = f"s{seed}/"     # keeps instance ids unique when runs share a file
        self.minutes = minutes
        self.out = out
        self.sem = asyncio.Semaphore(concurrency)
        self.rng = random.Random(seed)
        self.sup: Supervisor | None = None
        self.detectors: dict[str, FailureDetector] = {}
        self.meta: dict[str, dict] = {}      # instance id -> episode record
        self.fail_at: dict[str, tuple] = {}
        self.cpu_busy = 0
        self.fh = None
        self.done = 0

    def new_episode(self, inst: Instance) -> None:
        self.detectors[inst.id] = FailureDetector()
        self.meta[inst.id] = {"episode": self.prefix + inst.id, "service": inst.service, "kind": "none", "params": {},
                              "inject_ts": None, "fail_ts": None, "fail_reason": None, "started": time.time()}

    async def sampler(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            insts = [i for i in self.sup.instances.values() if i.ready]
            for s in await gather_samples(session, insts):
                iid = s["instance"]
                if iid in self.detectors:
                    v = self.detectors[iid].update(s)
                    row = {k: clean(s.get(k)) for k in KEEP}
                    row["instance"] = self.prefix + iid
                    self.fh.write(json.dumps({"r": row}) + "\n")
                    if v.failed and iid not in self.fail_at:
                        self.fail_at[iid] = (s["ts"], v.reason)
                        self.meta[iid]["fail_ts"] = s["ts"]
                        self.meta[iid]["fail_reason"] = v.reason
            await asyncio.sleep(max(0.0, cfg.TICK_S - (time.monotonic() - t0)))

    async def publish(self, session: aiohttp.ClientSession) -> None:
        try:
            await session.post(f"http://{cfg.HOST}:{cfg.LB_PORT}/lb/routes", json=self.sup.routes())
        except Exception:  # noqa: BLE001 - LB start-up race; the next call fixes it
            pass

    async def retire(self, session: aiohttp.ClientSession, inst: Instance) -> Instance:
        """Close one lifetime and start the next instance of this service."""
        self.fh.write(json.dumps({"e": self.meta[inst.id]}) + "\n")
        self.fh.flush()
        new = self.sup.spawn(inst.service)
        self.new_episode(new)
        ok = await self.sup.wait_ready(new, 30.0)
        inst.ready = False
        await self.sup.stop(inst)
        if ok:
            await self.publish(session)
        self.done += 1
        return new

    def pick(self) -> str:
        if self.topup:
            return self.rng.choice(["cpu_ramp"] * 4 + ["pool_leak"] * 4 + ["memory_leak"] * 2 + ["hang"] * 2 +
                                   ["cpu_burst"] * 3 + ["cache_growth"] * 3 + ["latency_blip", "error_blip"])
        roll = self.rng.random()
        if roll < 0.55:
            return self.rng.choice(fl.PROGRESSIVE)
        if roll < 0.65:
            return self.rng.choice(fl.SUDDEN)
        if roll < 0.92:
            return self.rng.choice(fl.BENIGN)
        return "none"

    async def slot(self, session: aiohttp.ClientSession, inst: Instance, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.rng.uniform(3, 15))
            kind = self.pick()
            is_cpu = kind in ("cpu_ramp", "cpu_burst")
            async with self.sem:
                if is_cpu and self.cpu_busy >= 1:
                    kind = self.rng.choice(["memory_leak", "cache_growth", "pool_leak", "error_blip"])
                    is_cpu = False
                self.cpu_busy += 1 if is_cpu else 0
                try:
                    await asyncio.sleep(self.rng.uniform(8, 22))    # healthy lead-in
                    await self.run_episode(session, inst, kind)
                finally:
                    self.cpu_busy -= 1 if is_cpu else 0
                inst = await self.retire(session, inst)

    async def run_episode(self, session: aiohttp.ClientSession, inst: Instance, kind: str) -> None:
        params = fl.sample_params(kind, self.rng)
        m = self.meta[inst.id]
        m.update(kind=kind, params=params, inject_ts=time.time())
        if kind != "none":
            try:
                await session.post(
                    f"http://{cfg.HOST}:{inst.port}/chaos/inject",
                    json={"kind": kind, "params": params}, timeout=aiohttp.ClientTimeout(total=1.0),
                )
            except Exception:  # noqa: BLE001 - a hang can swallow its own response
                pass
        # A failing fault runs until the failure; a harmless one runs out, plus a tail
        # at least as long as the horizon, so "did not fail" is known for every kept sample.
        limit = 240 if kind in fl.PROGRESSIVE or kind in fl.SUDDEN else params.get("duration_s", 30) + cfg.HORIZON_S + 8
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            if inst.id in self.fail_at:
                await asyncio.sleep(2)
                return
            await asyncio.sleep(0.5)

    async def traffic(self, session: aiohttp.ClientSession, stop: asyncio.Event) -> None:
        while not stop.is_set():
            roll = self.rng.random()
            rps = self.rng.uniform(40, 60) if roll < 0.08 else self.rng.uniform(8, 26)
            try:
                await session.post(f"http://{cfg.HOST}:{cfg.LOADGEN_PORT}/rate", json={"rps": rps})
            except Exception:  # noqa: BLE001 - loadgen still starting
                pass
            await asyncio.sleep(self.rng.uniform(10, 25))

    async def run(self) -> None:
        cfg.ensure_dirs()
        self.fh = gzip.open(self.out, "at", encoding="utf-8")
        async with aiohttp.ClientSession() as session:
            self.sup = Supervisor(session)
            self.sup.start_infra("intelligent", 15.0)
            await asyncio.sleep(1.0)
            try:
                await self.sup.start_all()
                for inst in self.sup.instances.values():
                    self.new_episode(inst)
                await self.publish(session)
                stop = asyncio.Event()
                tasks = [asyncio.create_task(self.sampler(session, stop)),
                         asyncio.create_task(self.traffic(session, stop))]
                for inst in list(self.sup.instances.values()):
                    tasks.append(asyncio.create_task(self.slot(session, inst, stop)))
                t_end = time.monotonic() + self.minutes * 60
                while time.monotonic() < t_end:
                    await asyncio.sleep(30)
                    print(f"{(t_end - time.monotonic()) / 60:5.1f} min left, {self.done} episodes", flush=True)
                stop.set()
                await asyncio.sleep(0.1)
                for t in tasks:
                    t.cancel()
                for iid, m in self.meta.items():
                    if iid in self.sup.instances and m["kind"] == "none" and m["fail_ts"] is None:
                        self.fh.write(json.dumps({"e": m}) + "\n")
            finally:
                self.fh.close()
                self.sup.shutdown()
        print("campaign finished, episodes:", self.done, flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--out", default=str(cfg.DATA_DIR / "telemetry.jsonl.gz"))
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--topup", action="store_true", help="favour the rarer failure types")
    a = p.parse_args()
    os.environ.setdefault("FORESIGHT_RPS", "15")
    asyncio.run(Campaign(a.minutes, a.out, a.concurrency, a.seed, a.topup).run())


if __name__ == "__main__":
    main()

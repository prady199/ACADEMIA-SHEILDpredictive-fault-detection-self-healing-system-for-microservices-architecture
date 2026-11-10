"""Starts, stops and replaces real OS processes (the job Kubernetes does in production)."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

import aiohttp
import psutil

from foresight import config as cfg


@dataclass
class Instance:
    id: str
    service: str
    port: int
    proc: subprocess.Popen
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    retiring: bool = False     # replacement in progress; do not act on it again
    draining: bool = False
    ps: psutil.Process | None = None

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # Ignore TIME_WAIT from a run that just ended, as the servers themselves do.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((cfg.HOST, port))
            return True
        except OSError:
            return False


class Supervisor:
    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        cfg.ensure_dirs()
        self.instances: dict[str, Instance] = {}
        self.counters: dict[str, int] = {}
        self.next_port = cfg.SERVICE_PORT_BASE
        self.infra: dict[str, subprocess.Popen] = {}
        self.session = session
        self.bad_release: set[str] = set()   # services whose next start-up will fail
        self._used_ports: set[int] = set()

    # ------------------------------------------------------------ process plumbing
    def _popen(self, module: str, args: list[str], log_name: str, env: dict | None = None) -> subprocess.Popen:
        log = open(cfg.LOG_DIR / f"{log_name}.log", "ab")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        full_env = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}
        try:
            return subprocess.Popen(
                [sys.executable, "-m", module, *args, "--parent-pid", str(os.getpid())],
                cwd=str(cfg.ROOT), stdout=log, stderr=subprocess.STDOUT, env=full_env, **kwargs,
            )
        finally:
            log.close()

    def _alloc_port(self) -> int:
        for _ in range(4000):
            port = self.next_port
            self.next_port += 1
            if self.next_port > cfg.SERVICE_PORT_BASE + 4000:
                self.next_port = cfg.SERVICE_PORT_BASE
            live = {i.port for i in self.instances.values()}
            if port not in live and port_free(port):
                return port
        raise RuntimeError("no free port for a new service instance")

    # ------------------------------------------------------------ infrastructure
    def start_infra(self, lb_mode: str = "intelligent", rps: float = cfg.DEFAULT_RPS) -> None:
        for port in (cfg.LB_PORT, cfg.LOADGEN_PORT):
            if not port_free(port):
                raise RuntimeError(
                    f"Port {port} is already in use. Close the other program, or set "
                    "FORESIGHT_LB_PORT / FORESIGHT_LOADGEN_PORT to free ports."
                )
        self.infra["lb"] = self._popen(
            "foresight.services.balancer", ["--port", str(cfg.LB_PORT), "--mode", lb_mode], "balancer"
        )
        self.infra["loadgen"] = self._popen(
            "foresight.services.loadgen",
            ["--port", str(cfg.LOADGEN_PORT), "--lb-port", str(cfg.LB_PORT), "--rps", str(rps)],
            "loadgen",
        )

    # ------------------------------------------------------------ instance lifecycle
    def spawn(self, service: str) -> Instance:
        n = self.counters[service] = self.counters.get(service, 0) + 1
        iid = f"{service}-{n}"
        port = self._alloc_port()
        env = {"FORESIGHT_BAD_RELEASE": "1"} if service in self.bad_release else {"FORESIGHT_BAD_RELEASE": "0"}
        proc = self._popen(
            "foresight.services.app",
            ["--service", service, "--instance", iid, "--port", str(port), "--lb-port", str(cfg.LB_PORT)],
            service, env,
        )
        inst = Instance(id=iid, service=service, port=port, proc=proc)
        try:
            inst.ps = psutil.Process(proc.pid)
            inst.ps.cpu_percent(None)
        except psutil.Error:
            inst.ps = None
        self.instances[iid] = inst
        return inst

    async def wait_ready(self, inst: Instance, timeout: float = cfg.READY_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://{cfg.HOST}:{inst.port}/healthz"
        while time.monotonic() < deadline:
            if not inst.alive:
                return False
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=0.5)) as r:
                    if r.status == 200:
                        inst.ready = True
                        return True
            except Exception:  # noqa: BLE001 - not listening yet
                pass
            await asyncio.sleep(0.1)
        return False

    async def stop(self, inst: Instance, grace_s: float = 1.5) -> None:
        if inst.alive:
            inst.proc.terminate()
            t0 = time.monotonic()
            while inst.alive and time.monotonic() - t0 < grace_s:
                await asyncio.sleep(0.05)
            if inst.alive:
                inst.proc.kill()
        # Reap in a worker thread so a stuck process can never stall the control loop.
        await asyncio.to_thread(self._reap, inst.proc)
        self.instances.pop(inst.id, None)

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    async def start_all(self) -> None:
        spawned = [self.spawn(s) for s, spec in cfg.TOPOLOGY.items() for _ in range(spec["replicas"])]
        results = await asyncio.gather(*(self.wait_ready(i, 30.0) for i in spawned))
        if not all(results):
            bad = [i.id for i, ok in zip(spawned, results) if not ok]
            raise RuntimeError(f"Instances failed to start: {bad}. See the logs folder.")

    def routes(self, weights: dict[str, float] | None = None, keep_dead: bool = False) -> dict:
        """keep_dead leaves failed instances in rotation: a balancer with no health checks."""
        weights = weights or {}
        table: dict[str, list] = {s: [] for s in cfg.TOPOLOGY}
        for inst in self.instances.values():
            if inst.ready and (inst.alive or keep_dead):
                table[inst.service].append(
                    {"instance": inst.id, "port": inst.port,
                     "weight": weights.get(inst.id, 1.0), "draining": inst.draining}
                )
        return table

    def shutdown(self) -> None:
        procs = [i.proc for i in self.instances.values()] + list(self.infra.values())
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.monotonic() + 2.0
        for p in procs:
            try:
                p.wait(timeout=max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        self.instances.clear()
        self.infra.clear()

"""The control loop: every second it observes, predicts, diagnoses, decides and reports."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque

import aiohttp

from foresight import config as cfg
from foresight.control.assistant import briefing
from foresight.control.incidents import IncidentManager
from foresight.control.notifier import Notifier
from foresight.control.predictor import Predictor, kind_label
from foresight.control.recovery import Recovery
from foresight.control.scenario import Scenario
from foresight.control.store import Store
from foresight.control.supervisor import Instance, Supervisor
from foresight.control.telemetry import DependencyMap, FailureDetector, History, Verdict, gather_samples
from foresight.ml import faults as fl

log = logging.getLogger("foresight")

MODES = {
    "autonomous": "Predict and recover",
    "reactive": "Recover after failure",
    "observe": "No protection",
}
SEVERITY_ORDER = ["healthy", "impacted", "degraded", "at_risk", "replacing", "failing", "down", "quarantined"]


def strict_json(v):
    """Browsers reject NaN and Infinity in JSON, so they never leave this process."""
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, dict):
        return {k: strict_json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [strict_json(x) for x in v]
    return v


def num(v, digits: int = 3):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), digits)


class Platform:
    def __init__(self, mode: str = "autonomous", rps: float = cfg.DEFAULT_RPS, db_path=None) -> None:
        self.mode = mode
        self.rps = rps
        self.db_path = db_path or cfg.DB_PATH
        self.session: aiohttp.ClientSession | None = None
        self.sup: Supervisor | None = None
        self.store: Store | None = None
        self.notifier: Notifier | None = None
        self.incidents: IncidentManager | None = None
        self.recovery = Recovery(self)
        self.predictor = Predictor()
        self.deps = DependencyMap()
        self.history = History()
        self.detectors: dict[str, FailureDetector] = {}
        self.verdicts: dict[str, Verdict] = {}
        self.latest: dict[str, dict] = {}
        self.states: dict[str, str] = {}
        self.degraded_ticks: dict[str, int] = {}
        self.last_good: dict[str, float] = {}
        self.root_causes: set[str] = set()
        self.injected: dict[str, list[str]] = {}
        self.lb_stats: dict = {}
        self.traffic: dict[int, dict] = {}
        self.persisted_until = 0
        self.scenario = Scenario(self)
        self.started_at = time.time()
        self.subscribers: set[asyncio.Queue] = set()
        self.snapshot: dict = {}
        self.preds: dict = {}
        self.loop_task: asyncio.Task | None = None
        self.tick_ms = deque(maxlen=60)
        self.model_message = ""

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        cfg.ensure_dirs()
        self.store = Store(self.db_path)
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))
        self.sup = Supervisor(self.session)
        self.notifier = Notifier(self.store, self.session)
        self.notifier.subscribers.append(self._on_note)
        self.incidents = IncidentManager(self.store, self.notifier)
        ok, self.model_message = await asyncio.to_thread(self.predictor.load)
        if not ok:
            log.warning("Prediction is off: %s", self.model_message)
            if self.mode == "autonomous":
                self.mode = "reactive"
        target_rps = self.rps
        self.sup.start_infra(self._lb_mode(), 0.0)   # no users until there is something to serve
        await self._wait_infra()
        await self.sup.start_all()
        for inst in self.sup.instances.values():
            self.register(inst)
        await self.publish_routes()
        await self.set_load(target_rps)
        self.loop_task = asyncio.create_task(self._loop())

    async def _wait_infra(self) -> None:
        for port in (cfg.LB_PORT, cfg.LOADGEN_PORT):
            for _ in range(150):
                try:
                    async with self.session.get(f"http://{cfg.HOST}:{port}/healthz",
                                                timeout=aiohttp.ClientTimeout(total=0.5)) as r:
                        if r.status == 200:
                            break
                except Exception:  # noqa: BLE001 - still starting
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f"A platform process on port {port} did not start. See the logs folder.")

    async def stop(self) -> None:
        if self.loop_task:
            self.loop_task.cancel()
        self.scenario.cancel()
        if self.sup:
            await asyncio.to_thread(self.sup.shutdown)
        if self.session:
            await self.session.close()
        if self.store:
            self.store.close()

    def register(self, inst: Instance) -> None:
        self.detectors[inst.id] = FailureDetector()

    def forget(self, iid: str) -> None:
        for d in (self.detectors, self.verdicts, self.latest, self.states, self.degraded_ticks, self.injected, self.last_good):
            d.pop(iid, None)
        self.history.forget(iid)
        self.predictor.forget(iid)
        self.preds.pop(iid, None)

    # ------------------------------------------------------------ modes & routing
    def _lb_mode(self) -> str:
        return "intelligent" if self.mode == "autonomous" else "round_robin"

    async def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode}")
        if mode == "autonomous" and not self.predictor.available:
            raise ValueError("Prediction is off because no model is available")
        self.mode = mode
        try:
            await self.session.post(f"http://{cfg.HOST}:{cfg.LB_PORT}/lb/mode", json={"mode": self._lb_mode()},
                                    timeout=aiohttp.ClientTimeout(total=1))
        except Exception:  # noqa: BLE001 - the watchdog restarts the balancer with the right mode
            pass
        await self.publish_routes()

    def weights(self) -> dict[str, float]:
        if self.mode != "autonomous":
            return {}
        out = {}
        for iid, p in self.preds.items():
            # Traffic leaves an instance in step with its risk, well before any failure.
            out[iid] = max(0.05, (1.0 - p.ema) ** 2) if p.ema > 0.2 else 1.0
        return out

    async def publish_routes(self) -> None:
        try:
            async with self.session.post(
                f"http://{cfg.HOST}:{cfg.LB_PORT}/lb/routes",
                json=self.sup.routes(self.weights(), keep_dead=self.mode == "observe"),
                timeout=aiohttp.ClientTimeout(total=1),
            ):
                pass
        except Exception:  # noqa: BLE001 - next tick publishes again
            pass

    async def _check_infra(self) -> None:
        """Platform processes get the same treatment as the services."""
        lb, gen = self.sup.infra.get("lb"), self.sup.infra.get("loadgen")
        if (lb and lb.poll() is not None) or (gen and gen.poll() is not None):
            if lb and lb.poll() is not None:
                self.sup.infra["lb"] = self.sup._popen(
                    "foresight.services.balancer",
                    ["--port", str(cfg.LB_PORT), "--mode", self._lb_mode()], "balancer")
            if gen and gen.poll() is not None:
                self.sup.infra["loadgen"] = self.sup._popen(
                    "foresight.services.loadgen",
                    ["--port", str(cfg.LOADGEN_PORT), "--lb-port", str(cfg.LB_PORT), "--rps", str(self.rps)], "loadgen")
            self.notifier.notify("warning", "Platform process restarted",
                                 "The load balancer or traffic generator stopped and was restarted.")

    # ------------------------------------------------------------ the loop
    async def _loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end monitoring
                log.exception("control loop tick failed")
            elapsed = time.monotonic() - t0
            self.tick_ms.append(elapsed * 1000)
            await asyncio.sleep(max(0.05, cfg.TICK_S - elapsed))

    async def tick(self) -> None:
        await self._check_infra()
        insts = [i for i in self.sup.instances.values() if i.ready]
        samples = await gather_samples(self.session, insts)

        windows = {}
        for inst, s in zip(insts, samples):
            if not inst.ready:          # retired while the probe was in flight
                continue
            self.latest[inst.id] = s
            self.history.add(s)
            self.deps.observe(s)
            self.verdicts[inst.id] = self.detectors.setdefault(inst.id, FailureDetector()).update(s)
            if s["probe_ok"] and not self.verdicts[inst.id].failed:
                self.last_good[inst.id] = s["ts"]
            if inst.alive:
                windows[inst.id] = self.history.window(inst.id)
        self.deps.roll()

        self.preds = await asyncio.to_thread(self.predictor.predict, windows)
        for iid, pred in self.preds.items():
            if iid in self.latest:
                self.latest[iid]["risk_ema"] = pred.ema
        states = self._classify()
        self._diagnose(states)
        self.recovery.evaluate(states, self.verdicts, self.preds)
        await self.publish_routes()
        await self._pull_infra_stats()
        self._close_incidents(states)
        self.store.commit()
        self._broadcast()

    # ------------------------------------------------------------ health
    def _classify(self) -> dict[str, str]:
        states = {}
        for inst in self.sup.instances.values():
            if not inst.ready:
                states[inst.id] = "starting"
                continue
            s = self.latest.get(inst.id, {})
            v = self.verdicts.get(inst.id, Verdict())
            p = self.preds.get(inst.id)
            enough = (s.get("n") or 0) >= cfg.MIN_REQUESTS_FOR_RATE
            if inst.draining:
                st = "draining"
            elif not inst.alive or (v.failed and v.reason == "unresponsive"):
                st = "down"
            elif v.failed:
                st = "failing"
            elif p and p.alarm:
                st = "at_risk"
            elif not s.get("probe_ok", True):
                st = "degraded"     # missed a health check: suspect it before calling it down
            elif enough and (s.get("err_self", 0) >= cfg.DEGRADED_SELF_ERROR or s.get("self_p95_ms", 0) >= cfg.DEGRADED_SELF_P95_MS):
                st = "degraded"
            elif enough and s.get("err_dep", 0) >= cfg.IMPACTED_DEP_ERROR:
                st = "impacted"
            else:
                st = "healthy"
            states[inst.id] = st
        self.states = states
        return states

    def service_state(self, service: str) -> str:
        if service in self.recovery.quarantined:
            return "quarantined"
        rows = [(i, self.states.get(i.id, "starting")) for i in self.sup.instances.values() if i.service == service]
        if not rows:
            return "down"
        serving = [st for i, st in rows if st in ("healthy", "impacted", "degraded", "at_risk")]
        sts = [st for _, st in rows]
        if not serving:
            if any(st in ("draining", "starting") for st in sts) and self.recovery.busy[service]:
                return "replacing"
            return "failing" if "failing" in sts and "down" not in sts else "down"
        if self.recovery.busy[service]:
            return "replacing"
        if "at_risk" in sts:
            return "at_risk"
        if any(st in ("down", "failing", "degraded") for st in sts):
            return "degraded"
        return "impacted" if "impacted" in sts else "healthy"

    # ------------------------------------------------------------ root cause & incidents
    def _diagnose(self, states: dict[str, str]) -> None:
        own: set[str] = set()
        impacted: set[str] = set()
        for inst in self.sup.instances.values():
            st = states.get(inst.id)
            svc = inst.service
            inj = self.injected.get(inst.id, [])
            v = self.verdicts.get(inst.id)
            if st in ("down", "failing"):
                own.add(svc)
                reason = v.reason if v and v.reason else "process_exit"
                self.incidents.failed(svc, inst.id, reason, inst.exit_code, inj, self._onset(inst.id, reason))
            elif st == "at_risk":
                own.add(svc)
                self.incidents.predicted(svc, inst.id, self.preds[inst.id], inj)
            elif st == "degraded":
                own.add(svc)
                n = self.degraded_ticks[inst.id] = self.degraded_ticks.get(inst.id, 0) + 1
                if n == 5:
                    s = self.latest.get(inst.id, {})
                    detail = f"{(s.get('err_self') or 0) * 100:.0f}% errors, p95 {s.get('self_p95_ms') or 0:.0f} ms"
                    self.incidents.degraded(svc, inst.id, inj, detail)
            else:
                self.degraded_ticks[inst.id] = 0
                if st == "impacted":
                    impacted.add(svc)
        roots = self.deps.root_causes(own)
        self.root_causes = roots
        for root in roots:
            hit = [s for s in impacted if root in self.deps.transitive_dependencies(s)]
            if hit:
                self.incidents.touch_affected(root, hit)
                self.incidents.note(root, f"Root cause is {root}. {', '.join(sorted(hit))} left alone: restarting them would not help.", "rca")

    def _onset(self, iid: str, reason: str) -> float:
        """When it really broke, which is earlier than when it could be confirmed."""
        sustain = {"error_rate": cfg.SELF_ERROR_FAIL_S, "latency": cfg.SELF_P95_FAIL_S,
                   "stalled": cfg.LOOP_STALL_FAIL_S}.get(reason, 0)
        return min(self.last_good.get(iid, time.time()), time.time() - sustain)

    def _close_incidents(self, states: dict[str, str]) -> None:
        for service in list(self.incidents.open):
            rows = [i for i in self.sup.instances.values() if i.service == service]
            calm = (len([i for i in rows if i.alive and i.ready]) >= cfg.TOPOLOGY[service]["replicas"]
                    and all(states.get(i.id) in ("healthy", "impacted") for i in rows))
            busy = self.recovery.busy[service] > 0 or service in self.recovery.quarantined
            self.incidents.maybe_close(service, calm, busy, self.traffic_between)

    # ------------------------------------------------------------ traffic & balancer stats
    async def _pull_infra_stats(self) -> None:
        now = int(time.time())
        try:
            async with self.session.get(f"http://{cfg.HOST}:{cfg.LOADGEN_PORT}/stats",
                                        params={"since": str(now - 15)},
                                        timeout=aiohttp.ClientTimeout(total=0.5)) as r:
                data = await r.json()
            self.rps = data["rps"]
            for b in data["seconds"]:
                self.traffic[b["ts"]] = b
        except Exception:  # noqa: BLE001 - shown as stale on the next successful pull
            pass
        for ts in sorted(t for t in self.traffic if self.persisted_until < t < now - 6):
            self.store.add_second(ts, self.traffic[ts]["ok"], self.traffic[ts]["fail"])
            self.persisted_until = ts
        for ts in [t for t in self.traffic if t < now - 3600]:
            del self.traffic[ts]
        try:
            async with self.session.get(f"http://{cfg.HOST}:{cfg.LB_PORT}/lb/stats",
                                        timeout=aiohttp.ClientTimeout(total=0.5)) as r:
                self.lb_stats = await r.json()
        except Exception:  # noqa: BLE001 - as above
            pass

    def traffic_between(self, a: float, b: float) -> tuple[int, int]:
        ok = fail = 0
        for ts, v in self.traffic.items():
            if a - 1 <= ts <= b:
                ok += v["ok"]
                fail += v["fail"]
        return ok, fail

    def availability(self, seconds: int) -> dict:
        now = int(time.time())
        ok, fail = self.traffic_between(now - seconds, now)
        total = ok + fail
        return {"ok": ok, "fail": fail, "total": total, "pct": (100.0 * ok / total) if total else None}

    # ------------------------------------------------------------ chaos & manual actions
    def pick_instance(self, service: str) -> Instance | None:
        for inst in sorted(self.sup.instances.values(), key=lambda i: i.started_at):
            if inst.service == service and inst.ready and inst.alive and not inst.retiring:
                return inst
        return None

    async def inject(self, target: str, kind: str, params: dict | None = None) -> dict:
        catalog = fl.PROGRESSIVE + fl.SUDDEN + fl.BENIGN + ["bad_release"]
        if kind not in catalog:
            raise ValueError(f"Unknown failure type {kind}")
        service = target if target in cfg.TOPOLOGY else None
        inst = self.sup.instances.get(target) if service is None else self.pick_instance(service)
        if service is None and inst is None:
            raise ValueError(f"{target} is not a service or a running instance")
        if kind == "bad_release":
            service = service or inst.service
            self.sup.bad_release.add(service)
            victims = [i for i in self.sup.instances.values() if i.service == service and i.alive]
            for v in victims:
                await self._send_fault(v, "crash", {})
                self.injected[v.id] = ["bad_release"]
            return {"target": service, "kind": kind, "instances": [v.id for v in victims]}
        if inst is None or not inst.alive:
            raise ValueError(f"No running instance of {target} can take this right now. Try again in a few seconds.")
        params = params or fl.sample_params(kind)
        self.injected.setdefault(inst.id, []).append(kind)
        await self._send_fault(inst, kind, params)
        return {"target": inst.id, "kind": kind, "params": params}

    async def _send_fault(self, inst: Instance, kind: str, params: dict) -> None:
        try:
            async with self.session.post(f"http://{cfg.HOST}:{inst.port}/chaos/inject",
                                         json={"kind": kind, "params": params},
                                         timeout=aiohttp.ClientTimeout(total=1.0)):
                pass
        except Exception:  # noqa: BLE001 - a crash or hang takes its own response with it
            pass

    async def restart(self, target: str) -> dict:
        insts = ([self.sup.instances[target]] if target in self.sup.instances
                 else [i for i in self.sup.instances.values() if i.service == target])
        if not insts:
            raise ValueError(f"{target} is not a service or a running instance")
        if insts[0].service in self.recovery.quarantined:
            raise ValueError(f"{insts[0].service} is quarantined. Roll it back first.")
        ids = []
        for inst in insts:
            if inst.retiring:
                continue
            inst.retiring = True
            ids.append(inst.id)
            asyncio.create_task(self._manual(inst))
        return {"restarting": ids}

    async def _manual(self, inst: Instance) -> None:
        await self.recovery.replace(inst, proactive=False, why="manual", manual=True)

    async def set_load(self, rps: float) -> float:
        async with self.session.post(f"http://{cfg.HOST}:{cfg.LOADGEN_PORT}/rate", json={"rps": rps},
                                     timeout=aiohttp.ClientTimeout(total=1)) as r:
            data = await r.json()
        self.rps = data["rps"]
        return self.rps

    # ------------------------------------------------------------ what the dashboard sees
    def instance_view(self, inst: Instance) -> dict:
        s = self.latest.get(inst.id, {})
        p = self.preds.get(inst.id)
        v = self.verdicts.get(inst.id)
        lb_row = next((r for r in (self.lb_stats.get("services", {}).get(inst.service) or [])
                       if r["instance"] == inst.id), {})
        return {
            "id": inst.id, "service": inst.service, "port": inst.port, "pid": inst.proc.pid,
            "state": self.states.get(inst.id, "starting"), "uptime_s": num(time.time() - inst.started_at, 0),
            "reason": v.reason if v else None, "exit_code": inst.exit_code,
            "mem_pct": num(s.get("mem_pct")), "rss_mb": num(s.get("rss_mb"), 1), "cpu": num(s.get("cpu")),
            "rps": num(s.get("rps"), 1), "err_self": num(s.get("err_self")), "err_dep": num(s.get("err_dep")),
            "p95_ms": num(s.get("p95_ms"), 1), "self_p95_ms": num(s.get("self_p95_ms"), 1),
            "lag_ms": num(s.get("lag_ms"), 1), "pool_pct": num(s.get("pool_pct")), "inflight": num(s.get("inflight"), 0),
            "probe_ok": s.get("probe_ok"), "faults": self.injected.get(inst.id, []),
            "risk": num(p.risk) if p else 0.0, "risk_ema": num(p.ema) if p else 0.0,
            "alarm": bool(p and p.alarm), "ttf_s": num(p.ttf_s, 0) if p else None,
            "kind": p.kind if p else None, "kind_label": kind_label(p.kind) if p else None,
            "kind_confidence": num(p.kind_confidence) if p else None,
            "anomaly": num(p.anomaly) if p else None, "anomalous": bool(p and p.anomalous),
            "factors": p.factors if p else [],
            "weight": lb_row.get("weight"), "breaker": lb_row.get("breaker"),
            "lb_requests": lb_row.get("requests"), "lb_failures": lb_row.get("failures"),
            "lb_inflight": lb_row.get("inflight"), "lb_ewma_ms": lb_row.get("ewma_ms"),
        }

    def build_snapshot(self) -> dict:
        insts = sorted(self.sup.instances.values(), key=lambda i: i.started_at)
        views = [self.instance_view(i) for i in insts]
        services = []
        for name, spec in cfg.TOPOLOGY.items():
            rows = [v for v in views if v["service"] == name]
            services.append({
                "name": name, "label": spec["label"], "desired": spec["replicas"],
                "state": self.service_state(name), "instances": [v["id"] for v in rows],
                "risk": max([v["risk_ema"] or 0 for v in rows], default=0.0),
                "rps": num(sum(v["rps"] or 0 for v in rows), 1),
                "quarantined": self.recovery.quarantined.get(name),
                "blast_radius": self.deps.blast_radius(name),
                "depends_on": sorted(self.deps.direct_dependencies(name, required_only=False)),
            })
        latest_ts = int(time.time()) - 2
        last = self.traffic.get(latest_ts) or {}
        snap = {
            "ts": time.time(), "mode": self.mode, "mode_label": MODES[self.mode],
            "lb_mode": self._lb_mode(), "rps_target": self.rps, "services": services, "instances": views,
            "edges": self.deps.edge_rows(), "roots": sorted(self.root_causes),
            "availability": {"minute": self.availability(60), "five_min": self.availability(300),
                             "session": self.availability(3600)},
            "traffic_now": {"ts": latest_ts, "ok": last.get("ok"), "fail": last.get("fail"), "p95_ms": last.get("p95_ms")},
            "open_incidents": sorted(self.incidents.open.values(), key=lambda i: -i["id"]),
            "actions": {"proactive": self.recovery.proactive_count, "reactive": self.recovery.reactive_count},
            "lb": {"retried": self.lb_stats.get("retried", 0), "rejected": self.lb_stats.get("rejected", {})},
            "model": {"available": self.predictor.available, "threshold": self.predictor.threshold,
                      "horizon_s": cfg.HORIZON_S},
            "tick_ms": num(sum(self.tick_ms) / len(self.tick_ms), 1) if self.tick_ms else None,
            "scenario": self.scenario.view(),
        }
        snap["traffic_recent"] = [self.traffic[t] for t in sorted(self.traffic) if t >= latest_ts - 8]
        snap["briefing"] = briefing(self, snap)
        return strict_json(snap)

    def _broadcast(self) -> None:
        self.snapshot = self.build_snapshot()
        self._send({"type": "state", "data": self.snapshot})

    def _on_note(self, note: dict) -> None:
        self._send({"type": "notification", "data": note})

    def _send(self, msg: dict) -> None:
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()   # a slow browser skips frames rather than holding up the loop
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    # ------------------------------------------------------------ analytics
    def analytics(self) -> dict:
        rows = self.store.incidents(1000)
        closed = [r for r in rows if r["status"] == "resolved"]
        ttrs = sorted(r["ttr_s"] for r in rows if r.get("ttr_s") is not None)
        leads = sorted(r["lead_s"] for r in rows if r.get("lead_s") is not None)
        predicted_prevented = [r for r in rows if r.get("outcome") == "prevented"]
        ahead = []
        for r in predicted_prevented:
            if r.get("predicted_ttf_s"):
                ahead.append(r["predicted_ttf_s"])
        by_service: dict[str, dict] = {}
        by_kind: dict[str, int] = {}
        for r in rows:
            b = by_service.setdefault(r["service"], {"service": r["service"], "incidents": 0, "prevented": 0, "recovered": 0, "other": 0, "failed_requests": 0})
            b["incidents"] += 1
            if r.get("outcome") == "prevented":
                b["prevented"] += 1
            elif r.get("outcome") == "recovered":
                b["recovered"] += 1
            else:
                b["other"] += 1
            b["failed_requests"] += r.get("failed_requests") or 0
            label = r.get("kind_label") or "Unclassified"
            by_kind[label] = by_kind.get(label, 0) + 1
        totals = self.store.totals()
        total = totals["ok"] + totals["fail"]
        alarms = [r for r in rows if r.get("predicted_at")]
        real_alarms = sum(bool(r.get("injected")) for r in alarms)
        return {
            "summary": {
                "incidents": len(rows), "closed": len(closed),
                "prevented": len(predicted_prevented),
                "recovered": sum(r.get("outcome") == "recovered" for r in rows),
                "self_resolved": sum(r.get("outcome") == "self_resolved" for r in rows),
                "escalated": sum(r.get("outcome") in ("escalated", "resolved_by_operator") for r in rows),
                "mttr_s": num(sum(ttrs) / len(ttrs), 1) if ttrs else None,
                "mttr_median_s": num(ttrs[len(ttrs) // 2], 1) if ttrs else None,
                "lead_median_s": num(leads[len(leads) // 2], 1) if leads else None,
                "predicted_ttf_median_s": num(sorted(ahead)[len(ahead) // 2], 1) if ahead else None,
                "availability_pct": num(100.0 * totals["ok"] / total, 3) if total else None,
                "requests": total, "failed_requests": totals["fail"],
            },
            "alarm_truth": {"alarms": len(alarms), "with_injected_fault": real_alarms, "without": len(alarms) - real_alarms},
            "by_service": sorted(by_service.values(), key=lambda b: -b["incidents"]),
            "by_kind": sorted(({"kind": k, "incidents": v} for k, v in by_kind.items()), key=lambda b: -b["incidents"]),
            "minutes": self.store.minutes(time.time() - 3 * 3600),
            "recent": [{k: r.get(k) for k in ("id", "service", "kind_label", "outcome", "ttr_s", "lead_s", "predicted_ttf_s", "failed_requests", "total_requests", "opened_at")} for r in rows[:40]],
        }

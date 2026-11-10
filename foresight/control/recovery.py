"""Decides and carries out recovery.

Rules, in order:
1. Never restart a symptom. A service failing only because of a dependency is left alone.
2. Failed: replace now. At risk: start the replacement first, move traffic, then retire the
   old instance, so there is no moment without capacity.
3. Restarting can't fix everything. Too many restarts in a short window means something a
   restart won't solve, so stop trying and call a person.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from foresight import config as cfg


class Recovery:
    def __init__(self, platform) -> None:
        self.p = platform
        self.restarts: dict[str, deque] = defaultdict(deque)
        self.last_action: dict[str, float] = {}
        self.quarantined: dict[str, str] = {}
        self.busy: dict[str, int] = defaultdict(int)
        self.tasks: set[asyncio.Task] = set()
        self.actions: deque[dict] = deque(maxlen=200)
        self.proactive_count = 0
        self.reactive_count = 0

    def _spawn_task(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def _storm(self, service: str) -> bool:
        now = time.time()
        d = self.restarts[service]
        while d and now - d[0] > cfg.STORM_WINDOW_S:
            d.popleft()
        return len(d) >= cfg.STORM_MAX_RESTARTS

    def evaluate(self, states: dict, verdicts: dict, preds: dict) -> None:
        """Called every tick by the platform."""
        mode = self.p.mode
        if mode == "observe":
            return
        now = time.time()
        for inst in list(self.p.sup.instances.values()):
            if inst.retiring or not inst.ready or inst.service in self.quarantined:
                continue
            v = verdicts.get(inst.id)
            if v and v.failed:
                inst.retiring = True
                self.reactive_count += 1
                self._spawn_task(self.replace(inst, proactive=False, why=v.reason))
                continue
            p = preds.get(inst.id)
            if (mode == "autonomous" and p and p.alarm and not self.busy[inst.service]
                    and now - inst.started_at >= 8
                    and now - self.last_action.get(inst.service, 0) >= cfg.ACTION_COOLDOWN_S):
                inst.retiring = True
                self.proactive_count += 1
                self._spawn_task(self.replace(inst, proactive=True, why="predicted"))

    async def replace(self, inst, proactive: bool, why: str, manual: bool = False) -> bool:
        service = inst.service
        self.busy[service] += 1
        self.last_action[service] = time.time()
        p = self.p
        t0 = time.time()
        try:
            if not inst.alive:
                await p.publish_routes()      # dead backend leaves the load balancer at once
            label = "Manual restart" if manual else ("Proactive replacement" if proactive else "Restart")
            if not manual or service in p.incidents.open:   # a planned restart is not an incident
                p.incidents.action(service, f"{label} of {inst.id} started.", inst.id)
            new = None
            for attempt in range(cfg.STORM_MAX_RESTARTS + 1):
                if self._storm(service):
                    self.quarantine(service)
                    return False
                self.restarts[service].append(time.time())
                new = p.sup.spawn(service)
                new.retiring = True           # hold off automatic decisions while starting
                if await p.sup.wait_ready(new):
                    new.retiring = False
                    break
                code = new.exit_code
                p.incidents.note(service, f"{new.id} failed during start-up (exit code {code}). Retrying.", "action")
                await p.sup.stop(new)
                p.forget(new.id)
                new = None
                await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt))
            if new is None:
                self.quarantine(service)
                return False

            p.register(new)
            await p.publish_routes()
            if proactive or (inst.alive and manual):
                # Traffic first: nothing new reaches the old instance, then let requests finish.
                inst.draining = True
                await p.publish_routes()
                await self._drain(inst)
            inst.ready = False
            await p.sup.stop(inst, grace_s=0.5)
            p.forget(inst.id)
            await p.publish_routes()

            elapsed = time.time() - t0
            self.actions.append({"ts": time.time(), "service": service, "old": inst.id, "new": new.id,
                                 "proactive": proactive, "manual": manual, "seconds": elapsed})
            p.incidents.recovered(service, new.id, proactive)
            if not manual:
                p.notifier.notify("info", f"{inst.id} replaced by {new.id}",
                                  f"{label} finished in {elapsed:.1f} s.", dedup_key=f"act:{inst.id}")
            return True
        except Exception as e:  # noqa: BLE001 - recovery must never take the control loop down
            p.incidents.note(service, f"Recovery of {inst.id} hit an error: {e}", "action")
            inst.retiring = False
            return False
        finally:
            self.busy[service] -= 1

    async def _drain(self, inst) -> None:
        deadline = time.monotonic() + cfg.DRAIN_TIMEOUT_S
        while time.monotonic() < deadline and inst.alive:
            s = self.p.latest.get(inst.id)
            if s and s.get("probe_ok") and (s.get("inflight") or 0) <= 1 and time.monotonic() > deadline - cfg.DRAIN_TIMEOUT_S + 1.0:
                return
            await asyncio.sleep(0.2)

    def quarantine(self, service: str) -> None:
        why = (f"{service} failed {cfg.STORM_MAX_RESTARTS} restarts within {cfg.STORM_WINDOW_S} s, so automatic "
               "restarts have stopped. Restarting will not fix this. Roll back the release, then retry.")
        self.quarantined[service] = why
        self.p.incidents.escalated(service, why)

    async def rollback(self, service: str) -> bool:
        """The operator's fix: clear the bad release, lift quarantine, restore replicas."""
        self.p.sup.bad_release.discard(service)
        self.quarantined.pop(service, None)
        self.restarts[service].clear()
        for inst in [i for i in self.p.sup.instances.values() if i.service == service and not i.alive]:
            inst.ready = False
            await self.p.sup.stop(inst, grace_s=0.1)
            self.p.forget(inst.id)
        self.p.incidents.note(service, "Operator rolled back the release.", "action")
        await self.reconcile(service)
        return True

    async def reconcile(self, service: str) -> None:
        """Bring the service back up to its configured replica count."""
        want = cfg.TOPOLOGY[service]["replicas"]
        have = [i for i in self.p.sup.instances.values() if i.service == service and i.alive]
        for _ in range(want - len(have)):
            new = self.p.sup.spawn(service)
            new.retiring = True
            if await self.p.sup.wait_ready(new):
                new.retiring = False
                self.p.register(new)
                self.p.incidents.recovered(service, new.id, False)
        await self.p.publish_routes()

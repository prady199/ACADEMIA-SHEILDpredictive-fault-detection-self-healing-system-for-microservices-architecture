"""Guided demo. Injects real failures in sequence and narrates what actually happened.
Every result sentence is built from the resulting incident; nothing here is scripted to succeed."""
from __future__ import annotations

import asyncio
import time

from foresight import config as cfg

STEPS = [
    {"id": "baseline", "title": "Normal traffic",
     "intro": "Synthetic users are shopping. Every call between services is measured, and the dependency map comes from that traffic."},
    {"id": "predict", "title": "Predict a failure before it happens",
     "intro": "Payment has started leaking memory. Nothing is failing yet, so a normal health check still says all is fine. Watch its risk rise."},
    {"id": "crash", "title": "Recover from a sudden crash",
     "intro": "A catalog instance has been killed outright. No model can see this coming, so the platform relies on the other replica and on fast recovery."},
    {"id": "cascade", "title": "Find the root cause of a cascade",
     "intro": "Inventory has frozen. Catalog, cart and orders all depend on it, so the failure spreads upward. Restarting all of them would be wrong: only the origin needs it."},
    {"id": "escalate", "title": "Know when restarting will not help",
     "intro": "Notifications received a bad release that dies at start-up. Restarting can't fix that, so the platform should stop trying and call a person. Checkout should keep working, because notifications are optional."},
]


class Scenario:
    def __init__(self, platform) -> None:
        self.p = platform
        self.task: asyncio.Task | None = None
        self.state = "idle"
        self.current = -1
        self.steps: list[dict] = []

    def view(self) -> dict:
        return {"state": self.state, "current": self.current, "steps": self.steps}

    def start(self) -> None:
        if self.state == "running":
            raise ValueError("The guided demo is already running")
        self.steps = [{**s, "status": "pending", "result": None} for s in STEPS]
        self.current = -1
        self.state = "running"
        self.task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        if self.state == "running":
            self.state = "stopped"

    def _begin(self, i: int) -> None:
        self.current = i
        self.steps[i]["status"] = "running"

    def _end(self, i: int, result: str, ok: bool = True) -> None:
        self.steps[i]["status"] = "done" if ok else "failed"
        self.steps[i]["result"] = result

    async def _wait_closed(self, service: str, since: float, timeout: float, condition=None) -> dict | None:
        """Wait for an incident on this service, opened after `since`, to resolve or escalate."""
        t_end = time.time() + timeout
        while time.time() < t_end:
            for inc in self.p.store.incidents(25):
                if inc["service"] == service and inc["opened_at"] >= since - 1 and (
                    inc["status"] == "resolved" or (condition and condition(inc))
                ):
                    return inc
            await asyncio.sleep(0.5)
        return None

    async def _settle(self, seconds: float = 45.0) -> None:
        """Let the system return to all-healthy before the next failure, up to a limit."""
        t_end = time.time() + seconds
        while time.time() < t_end:
            if not self.p.incidents.open and all(s["state"] == "healthy" for s in self.p.snapshot.get("services", [])):
                return
            await asyncio.sleep(0.5)

    async def _run(self) -> None:
        p = self.p
        try:
            await p.set_mode("autonomous")
            self._begin(0)
            await asyncio.sleep(10)
            a = p.availability(60)
            self._end(0, f"{p.rps:.0f} requests a second, {a['pct'] or 0:.1f}% succeeding, {len(p.deps.edges)} dependencies discovered from traffic.")

            # 1. Progressive failure.
            self._begin(1)
            t0 = time.time()
            await p.inject("payment", "memory_leak", {"rate_mb_s": 3.5})
            inc = await self._wait_closed("payment", t0, 110)
            if inc is None:
                self._end(1, "No resolved incident within the time limit. See the Incidents tab.", False)
            elif inc["outcome"] == "prevented":
                ttf = f" It estimated about {inc['predicted_ttf_s']:.0f} s left." if inc.get("predicted_ttf_s") else ""
                self._end(1, f"Replaced before it failed.{ttf} {inc['failed_requests']} of {inc['total_requests']} user requests failed during the incident.")
            else:
                self._end(1, f"This time it failed first ({inc['outcome']}), and recovered in {(inc.get('ttr_s') or 0):.1f} s. "
                             f"{inc['failed_requests']} of {inc['total_requests']} user requests failed.", False)
            await self._settle()

            # 2. Sudden failure.
            self._begin(2)
            t0 = time.time()
            await p.inject("catalog", "crash")
            inc = await self._wait_closed("catalog", t0, 45)
            if inc:
                share = inc["failed_requests"] / max(1, inc["total_requests"])
                tail = " The other replica carried the traffic." if share < 0.05 else ""
                self._end(2, f"Recovered in {(inc.get('ttr_s') or 0):.1f} s. {inc['failed_requests']} of {inc['total_requests']} user requests failed.{tail}")
            else:
                self._end(2, "No resolved incident within the time limit. See the Incidents tab.", False)
            await self._settle()

            # 3. Cascade.
            self._begin(3)
            t0 = time.time()
            await p.inject("inventory", "hang")
            inc = await self._wait_closed("inventory", t0, 45)
            if inc:
                other = [c for c in p.store.incidents(10) if c["opened_at"] >= t0 - 1 and c["service"] != "inventory" and c.get("restarts")]
                aff = ", ".join(inc["affected"]) or "no other services"
                self._end(3, f"Root cause: inventory. Spread to {aff}. {'Only inventory was restarted' if not other else 'Other services were also restarted'}, "
                             f"back in {(inc.get('ttr_s') or 0):.1f} s. {inc['failed_requests']} of {inc['total_requests']} user requests failed.")
            else:
                self._end(3, "No resolved incident within the time limit. See the Incidents tab.", False)
            await self._settle()

            # 4. A failure restarting can't fix.
            self._begin(4)
            t0 = time.time()
            await p.inject("notification", "bad_release")
            inc = await self._wait_closed("notification", t0, 90, lambda i: i["status"] == "escalated")
            if inc:
                await asyncio.sleep(6)   # stay paused long enough to read the alert
                await p.recovery.rollback("notification")
                done = await self._wait_closed("notification", t0, 30)
                ok, fail = p.traffic_between(t0, time.time())
                held = " Checkout kept working throughout." if fail / max(1, ok + fail) < 0.02 else ""
                self._end(4, f"Restarts stopped after {cfg.STORM_MAX_RESTARTS} attempts and a person was alerted. After the rollback it came back.{held} "
                             f"{fail} of {ok + fail} user requests failed." if done else
                             "Escalated correctly, but the rollback did not finish within the time limit.", bool(done))
            else:
                self._end(4, "The platform did not escalate within the time limit.", False)
                p.sup.bad_release.discard("notification")
            self.state = "finished"
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as e:  # noqa: BLE001 - shown in the demo panel
            self.state = "failed"
            if 0 <= self.current < len(self.steps):
                self._end(self.current, f"The demo stopped: {e}", False)

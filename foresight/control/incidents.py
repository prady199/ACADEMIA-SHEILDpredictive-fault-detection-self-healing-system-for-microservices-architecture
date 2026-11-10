"""Groups raw events into incidents: one story per root service, from first sign to resolution."""
from __future__ import annotations

import time

from foresight.control.notifier import Notifier
from foresight.control.store import Store
from foresight.ml import faults as fl

OPEN_STATES = ("predicted", "active", "mitigating", "escalated", "degraded")


def kind_from_exit(reason: str, exit_code: int | None) -> str | None:
    if reason == "process_exit":
        if exit_code == 137:
            return "memory_leak"
        if exit_code == 78:
            return "bad_release"
        return "crash"
    return {"unresponsive": "hang", "stalled": "cpu_ramp", "error_rate": "error_ramp", "latency": "latency_ramp"}.get(reason)


REASON_TEXT = {
    "process_exit": "the process exited",
    "unresponsive": "it stopped answering health checks",
    "stalled": "its event loop kept stalling",
    "error_rate": "over half of its requests were failing",
    "latency": "it became too slow for its callers",
}


class IncidentManager:
    def __init__(self, store: Store, notifier: Notifier) -> None:
        self.store = store
        self.notifier = notifier
        self.open: dict[str, dict] = {}     # service -> incident
        self.next_id = store.next_incident_id()
        self.quiet_ticks: dict[str, int] = {}

    def _save(self, inc: dict) -> None:
        self.store.save_incident(inc)

    def _new(self, service: str, instance: str, status: str, kind: str | None, severity: str) -> dict:
        now = time.time()
        inc = {
            "id": self.next_id, "service": service, "instance": instance, "status": status,
            "severity": severity, "kind": kind, "kind_label": fl.LABELS.get(kind, kind),
            "ml_kind": None, "outcome": None, "opened_at": now, "predicted_at": None,
            "predicted_ttf_s": None, "failed_at": None, "detected_at": None, "failure_reason": None, "action_at": None,
            "recovered_at": None, "closed_at": None, "lead_s": None, "ttr_s": None,
            "affected": [], "factors": [], "risk_peak": 0.0, "injected": None,
            "failed_requests": None, "total_requests": None, "restarts": 0,
            "recommendation": fl.ROOT_FIX.get(kind), "timeline": [],
        }
        self.next_id += 1
        self.open[service] = inc
        self.quiet_ticks[service] = 0
        return inc

    @staticmethod
    def _log(inc: dict, kind: str, text: str) -> None:
        inc["timeline"].append({"ts": time.time(), "kind": kind, "text": text})

    def _set_kind(self, inc: dict, kind: str | None) -> None:
        if kind:
            inc["kind"] = kind
            inc["kind_label"] = fl.LABELS.get(kind, kind)
            inc["recommendation"] = fl.ROOT_FIX.get(kind)

    # ------------------------------------------------------------ events
    def predicted(self, service: str, instance: str, pred, injected: list[str]) -> None:
        inc = self.open.get(service)
        if inc and inc["predicted_at"]:
            inc["risk_peak"] = max(inc["risk_peak"], pred.ema)
            return
        fresh = inc is None
        if fresh:
            inc = self._new(service, instance, "predicted", pred.kind, "warning")
        inc["predicted_at"] = time.time()
        inc["predicted_ttf_s"] = pred.ttf_s
        inc["ml_kind"] = pred.kind
        inc["instance"] = instance
        inc["risk_peak"] = pred.ema
        inc["factors"] = pred.factors
        inc["injected"] = injected or inc["injected"]
        if inc["status"] == "degraded":
            inc["status"] = "predicted"
            inc["severity"] = "warning"
        self._set_kind(inc, pred.kind)
        ttf = f" in about {pred.ttf_s:.0f} s" if pred.ttf_s else ""
        self._log(inc, "predict", f"Model predicted {instance} will fail{ttf} (risk {pred.ema * 100:.0f}%, likely {inc['kind_label']}).")
        self._save(inc)
        why = pred.factors[0]["text"] + ". " if pred.factors else ""
        self.notifier.notify("warning", f"{instance} is predicted to fail{ttf}",
                             f"{why}Likely cause: {inc['kind_label']}.", inc["id"], f"pred:{service}")

    def failed(self, service: str, instance: str, reason: str, exit_code: int | None, injected: list[str],
               onset: float | None = None) -> None:
        inc = self.open.get(service)
        if inc is None:
            inc = self._new(service, instance, "active", None, "critical")
        if inc["failed_at"] is None:
            inc["detected_at"] = time.time()
            inc["failed_at"] = min(onset or inc["detected_at"], inc["detected_at"])
            inc["failure_reason"] = reason
            inc["instance"] = instance
            inc["status"] = "active" if inc["status"] in ("predicted", "degraded") else inc["status"]
            inc["severity"] = "critical"
            inc["injected"] = injected or inc["injected"]
            observed = kind_from_exit(reason, exit_code)
            # An exit code or a hang is evidence; latency or errors may have a deeper cause
            # the model already named.
            if observed and (reason in ("process_exit", "unresponsive") or not inc["kind"]):
                if not (reason == "process_exit" and exit_code not in (137, 78) and inc["kind"]):
                    self._set_kind(inc, observed)
            if inc["predicted_at"]:
                inc["lead_s"] = max(0.0, inc["failed_at"] - inc["predicted_at"])
            self._log(inc, "fail", f"{instance} failed: {REASON_TEXT.get(reason, reason)}.")
            self._save(inc)
            self.notifier.notify("critical", f"{instance} failed",
                                 f"{instance} failed because {REASON_TEXT.get(reason, reason)}.", inc["id"], f"fail:{service}")

    def degraded(self, service: str, instance: str, injected: list[str], detail: str) -> None:
        if service not in self.open:
            inc = self._new(service, instance, "degraded", None, "minor")
            inc["injected"] = injected or None
            self._log(inc, "degrade", f"{instance} is degraded: {detail}.")
            self._save(inc)

    def action(self, service: str, text: str, instance: str | None = None) -> None:
        inc = self.open.get(service)
        if inc is None:
            inc = self._new(service, instance or service, "mitigating", None, "warning")
        inc["restarts"] += 1
        inc["action_at"] = inc["action_at"] or time.time()
        if inc["status"] in ("predicted", "active", "degraded"):
            inc["status"] = "mitigating"
        self._log(inc, "action", text)
        self._save(inc)

    def note(self, service: str, text: str, kind: str = "note") -> None:
        inc = self.open.get(service)
        if inc:
            if not inc["timeline"] or inc["timeline"][-1]["text"] != text:
                self._log(inc, kind, text)
                self._save(inc)

    def recovered(self, service: str, new_instance: str, proactive: bool) -> None:
        inc = self.open.get(service)
        if not inc:
            return
        now = time.time()
        inc["recovered_at"] = now
        if inc["failed_at"]:
            inc["ttr_s"] = now - inc["failed_at"]
        self._log(inc, "recover", f"{new_instance} is ready and receiving traffic.")
        self._save(inc)

    def escalated(self, service: str, why: str) -> None:
        inc = self.open.get(service)
        if inc is None:
            inc = self._new(service, service, "escalated", "bad_release", "critical")
        inc["status"] = "escalated"
        inc["severity"] = "critical"
        inc["outcome"] = "escalated"
        if inc["kind"] in (None, "crash"):
            self._set_kind(inc, "bad_release")
        self._log(inc, "escalate", why)
        self._save(inc)
        self.notifier.notify("critical", f"{service} needs a person", why, inc["id"], f"esc:{service}")

    def touch_affected(self, service: str, impacted: list[str]) -> None:
        inc = self.open.get(service)
        if inc:
            added = [s for s in impacted if s not in inc["affected"]]
            if added:
                inc["affected"].extend(added)
                self._log(inc, "cascade", f"Failure spreading: {', '.join(added)} now seeing errors from {service}.")
                self._save(inc)

    def maybe_close(self, service: str, calm: bool, busy: bool, traffic_between) -> dict | None:
        """Close once the service has stayed calm for a few ticks with nothing in progress."""
        inc = self.open.get(service)
        if not inc or inc["status"] == "escalated" and not calm:
            return None
        self.quiet_ticks[service] = self.quiet_ticks.get(service, 0) + 1 if (calm and not busy) else 0
        if self.quiet_ticks[service] < 4:
            return None
        now = time.time()
        inc["closed_at"] = now
        inc["status"] = "resolved"
        ok, fail = traffic_between(inc["opened_at"], now)
        inc["failed_requests"], inc["total_requests"] = fail, ok + fail
        if inc["failed_at"] is None and inc["action_at"]:
            inc["outcome"] = "prevented"
            self._log(inc, "resolve", "Resolved before any failure. The instance was replaced in time.")
            title = f"Failure on {service} prevented"
        elif inc["action_at"]:
            inc["outcome"] = "recovered" if inc["outcome"] != "escalated" else "resolved_by_operator"
            self._log(inc, "resolve", f"Resolved after recovery. Time to recover: {(inc['ttr_s'] or 0):.1f} s.")
            title = f"{service} recovered"
        else:
            inc["outcome"] = "self_resolved"
            self._log(inc, "resolve", "Cleared on its own. No action was needed.")
            title = None
        self._save(inc)
        del self.open[service]
        if title:
            detail = f"{fail} of {ok + fail} user requests failed during this incident."
            self.notifier.notify("info", title, detail, inc["id"], f"done:{inc['id']}")
        return inc


def incident_report(inc: dict) -> str:
    """A shareable post-incident report in Markdown."""
    def t(ts):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "not applicable"
    lines = [
        f"# Incident {inc['id']}: {inc['service']}",
        "",
        f"- Type: {inc.get('kind_label') or 'unknown'}",
        f"- Outcome: {inc.get('outcome') or inc['status']}",
        f"- Opened: {t(inc['opened_at'])}",
        f"- Predicted: {t(inc['predicted_at'])}",
        f"- Failed: {t(inc['failed_at'])}",
        f"- Recovered: {t(inc['recovered_at'])}",
        f"- Time to recover: {inc['ttr_s']:.1f} s" if inc.get("ttr_s") else "- Time to recover: not applicable",
        f"- Affected services: {', '.join(inc['affected']) or 'none'}",
        f"- User requests failed: {inc.get('failed_requests')} of {inc.get('total_requests')}",
        "", "## Timeline", "",
    ]
    lines += [f"- {t(e['ts'])}: {e['text']}" for e in inc["timeline"]]
    if inc.get("factors"):
        lines += ["", "## Why the model raised the alarm", ""]
        lines += [f"- {f['text']}" for f in inc["factors"]]
    if inc.get("recommendation"):
        lines += ["", "## Suggested fix", "", inc["recommendation"]]
    return "\n".join(lines) + "\n"

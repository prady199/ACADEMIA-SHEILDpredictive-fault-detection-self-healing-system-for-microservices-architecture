"""The operations assistant: answers questions from live platform state.

It matches intent with rules and links entities by name, then builds answers only from
real data: nothing here is a language model and nothing is invented. Anything that changes
the system comes back as a button for the operator to press, never as an automatic action.
"""
from __future__ import annotations

import difflib
import re
import time

from foresight import config as cfg
from foresight.ml import faults as fl

ALIASES = {
    "notifications": "notification", "notify": "notification", "email": "notification",
    "pay": "payment", "payments": "payment", "billing": "payment", "stock": "inventory",
    "login": "auth", "authentication": "auth", "api": "gateway", "products": "catalog",
    "product": "catalog", "order": "orders", "basket": "cart",
}
FAULT_WORDS = [
    (r"memory|leak|oom", "memory_leak"), (r"\bcpu\b|saturat", "cpu_ramp"), (r"pool|connection", "pool_leak"),
    (r"latency|slow", "latency_ramp"), (r"error", "error_ramp"), (r"crash|kill", "crash"),
    (r"hang|freeze|deadlock|stuck", "hang"), (r"bad release|bad deploy|crash loop", "bad_release"),
]


def find_service(text: str) -> str | None:
    words = re.findall(r"[a-z]+", text.lower())
    for w in words:
        if w in cfg.TOPOLOGY:
            return w
        if w in ALIASES:
            return ALIASES[w]
    for w in words:
        if len(w) >= 5:
            m = difflib.get_close_matches(w, list(cfg.TOPOLOGY), n=1, cutoff=0.8)
            if m:
                return m[0]
    return None


def pct(v) -> str:
    return "not enough data" if v is None else f"{v:.2f}%"


def briefing(p, snap: dict) -> dict:
    """The one-paragraph situation summary at the top of the dashboard."""
    services = snap["services"]
    at_risk = [v for v in snap["instances"] if v["alarm"] and v["state"] == "at_risk"]
    replacing = [s for s in services if s["state"] == "replacing"]
    down = [s for s in services if s["state"] in ("down", "failing", "quarantined")]
    impacted = [s for s in services if s["state"] == "impacted"]
    degraded = [s for s in services if s["state"] == "degraded"]
    avail = snap["availability"]["minute"]["pct"]
    a = f"{avail:.1f}%" if avail is not None else "no traffic yet"
    if any(s["quarantined"] for s in services):
        s = next(s for s in services if s["quarantined"])
        return {"level": "critical", "text": f"{s['label']} needs you: automatic restarts stopped working, so they have been paused. Roll back the release to continue.",
                "focus": s["name"]}
    if down:
        names = ", ".join(s["label"] for s in down)
        root = sorted(snap["roots"])
        extra = f" {len(impacted)} other services are seeing errors because of it." if impacted else ""
        if p.mode == "observe":
            extra += " Protection is off, so nothing will restart it."
        return {"level": "critical", "text": f"{names} {'is' if len(down) == 1 else 'are'} down.{extra} Requests succeeding over the last minute: {a}.",
                "focus": root[0] if root else down[0]["name"]}
    if at_risk:
        v = at_risk[0]
        ttf = f" in about {v['ttf_s']:.0f} seconds" if v["ttf_s"] else ""
        what = v["kind_label"] or "a developing problem"
        act = "A replacement is being prepared." if p.mode == "autonomous" else "Automatic prevention is off, so it will be restarted only after it fails."
        return {"level": "warning", "text": f"{v['id']} looks likely to fail{ttf}. Likely cause: {what.lower()}. {act}", "focus": v["service"]}
    if replacing:
        s = replacing[0]
        return {"level": "warning", "text": f"Replacing an instance of {s['label']}. Requests succeeding over the last minute: {a}.", "focus": s["name"]}
    if degraded:
        s = degraded[0]
        return {"level": "warning", "text": f"{s['label']} is running below normal, but has not failed. Requests succeeding over the last minute: {a}.", "focus": s["name"]}
    if impacted:
        names = ", ".join(s["label"] for s in impacted)
        return {"level": "watch", "text": f"{names} {'is' if len(impacted) == 1 else 'are'} still seeing errors from a dependency. Requests succeeding over the last minute: {a}.",
                "focus": impacted[0]["name"]}
    rising = sorted((v for v in snap["instances"] if (v["risk_ema"] or 0) >= 0.3), key=lambda v: -(v["risk_ema"] or 0))
    if rising:
        v = rising[0]
        return {"level": "watch", "text": f"All services are working. Watching {v['id']}: its failure risk has risen to {(v['risk_ema'] or 0) * 100:.0f}%.", "focus": v["service"]}
    n = len(services)
    tail = f"{a} succeeding over the last minute" if avail is not None else "measuring success now"
    return {"level": "ok", "text": f"All {n} services are healthy. {snap['rps_target']:.0f} requests a second, {tail}. Nothing is predicted to fail in the next {cfg.HORIZON_S} seconds.", "focus": None}


class Answer:
    def __init__(self, text: str, actions: list | None = None, highlight: list | None = None) -> None:
        self.text = text
        self.actions = actions or []
        self.highlight = highlight or []

    def dict(self) -> dict:
        return {"text": self.text, "actions": self.actions, "highlight": self.highlight}


def act(label: str, url: str, body: dict | None = None) -> dict:
    return {"label": label, "url": url, "body": body or {}}


def describe_service(p, service: str) -> Answer:
    snap = p.snapshot
    s = next(x for x in snap["services"] if x["name"] == service)
    insts = [v for v in snap["instances"] if v["service"] == service]
    lines = []
    if s["state"] == "healthy":
        lines.append(f"{s['label']} is healthy across {len(insts)} instance{'s' if len(insts) != 1 else ''}.")
    else:
        lines.append(f"{s['label']} is {s['state'].replace('_', ' ')}.")
    for v in insts:
        if v["state"] == "healthy":
            continue
        if v["state"] == "impacted":
            below = sorted(p.deps.transitive_dependencies(service) & set(snap["roots"]))
            cause = f" The root cause is {', '.join(below)}, so restarting this service would not help." if below else ""
            lines.append(f"{v['id']} is fine itself, but {(v['err_dep'] or 0) * 100:.0f}% of its requests fail because of a dependency.{cause}")
        elif v["alarm"] or (v["risk_ema"] or 0) >= 0.3:
            ttf = f", in about {v['ttf_s']:.0f} s" if v["ttf_s"] else ""
            lines.append(f"{v['id']}: {(v['risk_ema'] or 0) * 100:.0f}% failure risk{ttf}. Most likely {(v['kind_label'] or 'unclear').lower()}.")
            lines += [f"{f['text']}." for f in v["factors"]]
        else:
            lines.append(f"{v['id']} is {v['state'].replace('_', ' ')}. Errors {(v['err_self'] or 0) * 100:.0f}%, own p95 {v['self_p95_ms'] or 0:.0f} ms.")
    if s["state"] == "healthy":
        peak = max(insts, key=lambda v: v["risk_ema"] or 0, default=None)
        if peak:
            lines.append(f"Highest failure risk: {(peak['risk_ema'] or 0) * 100:.0f}%. Memory {(peak['mem_pct'] or 0) * 100:.0f}% of limit, p95 {peak['p95_ms'] or 0:.0f} ms.")
    past = [i for i in p.store.incidents(50) if i["service"] == service]
    if past:
        i = past[0]
        lines.append(f"Latest incident: #{i['id']}, {(i.get('kind_label') or 'unclassified').lower()}, {_outcome(i)}.")
    actions = [act(f"Restart {service}", f"/api/services/{service}/restart")]
    if s["quarantined"]:
        actions = [act(f"Roll back {service}", f"/api/services/{service}/rollback")]
    return Answer(" ".join(lines), actions, [service])


def _outcome(i: dict) -> str:
    return {
        "prevented": "prevented before failure",
        "recovered": f"recovered in {(i.get('ttr_s') or 0):.1f} s",
        "self_resolved": "cleared on its own",
        "escalated": "escalated to a person",
        "resolved_by_operator": "fixed by an operator",
    }.get(i.get("outcome"), "still open")


def ask(p, question: str) -> dict:
    q = question.strip().lower()
    if not q:
        return Answer("Ask about a service, risk, or an incident. For example: why is payment failing?").dict()
    svc = find_service(q)
    snap = p.snapshot
    words = set(re.findall(r"[a-z]+", q))

    # Actions come back as buttons, so a misread sentence can't change anything.
    if re.search(r"\b(inject|simulate|trigger|break|cause|make)\b", q) or (svc and re.search(r"\b(crash|kill|hang|freeze)\b", q)):
        kind = next((k for pat, k in FAULT_WORDS[::-1] if re.search(pat, q)), None)
        if kind and svc:
            return Answer(f"Ready to inject {fl.LABELS[kind].lower()} into {svc}. Press the button to start it.",
                          [act(f"Inject into {svc}", "/api/chaos", {"target": svc, "kind": kind})], [svc]).dict()
        return Answer("Name a failure and a service. For example: inject a memory leak into payment.").dict()
    if re.search(r"\b(roll ?back)\b", q) and svc:
        return Answer(f"Rolling back clears the bad release on {svc} and brings it back up.",
                      [act(f"Roll back {svc}", f"/api/services/{svc}/rollback")], [svc]).dict()
    if re.search(r"\b(restart|reboot|replace)\b", q) and svc:
        return Answer(f"This replaces every instance of {svc}, starting the new one before stopping the old one.",
                      [act(f"Restart {svc}", f"/api/services/{svc}/restart")], [svc]).dict()
    if re.search(r"\b(demo|walkthrough|tour)\b", q):
        return Answer("The guided demo runs four real failures, about four minutes in total, and narrates what happens.",
                      [act("Start guided demo", "/api/scenario/start")]).dict()
    m = re.search(r"\b(?:load|traffic|rps)\b.*?(\d+)", q)
    if m:
        rps = min(100, int(m.group(1)))
        return Answer(f"Ready to set traffic to {rps} requests a second.", [act(f"Set traffic to {rps}/s", "/api/load", {"rps": rps})]).dict()

    # Dependencies and what-if.
    if svc and re.search(r"what if|what happens|blast|impact|if .* (fail|down|die)|who depends|depends on .*\?|affect", q):
        hit = p.deps.blast_radius(svc)
        soft = sorted(p.deps.direct_dependents(svc, required_only=False) - p.deps.direct_dependents(svc, required_only=True))
        if hit:
            text = f"If {svc} stops, {len(hit)} service{'s' if len(hit) != 1 else ''} stop working with it: {', '.join(hit)}."
        else:
            text = f"Nothing else stops if {svc} stops."
        if soft:
            text += f" {', '.join(soft)} also call it, but carry on without it."
        return Answer(text, highlight=[svc, *hit]).dict()
    if svc and re.search(r"depend|calls|call |needs|upstream|downstream|connected", q):
        deps = sorted(p.deps.direct_dependencies(svc, required_only=False))
        req = p.deps.direct_dependencies(svc)
        if not deps:
            return Answer(f"{svc} calls no other services.", highlight=[svc]).dict()
        parts = [f"{d}{'' if d in req else ' (optional)'}" for d in deps]
        return Answer(f"{svc} calls {', '.join(parts)}. This comes from traffic the platform observed, not a config file.",
                      highlight=[svc, *deps]).dict()

    # Risk.
    if words & {"risk", "predict", "prediction", "predicted", "next", "likely", "soon", "forecast"} and not svc:
        rows = sorted(snap["instances"], key=lambda v: -(v["risk_ema"] or 0))
        top = [v for v in rows if (v["risk_ema"] or 0) >= 0.15][:3]
        if not top:
            r = rows[0] if rows else None
            tail = f" The highest is {r['id']} at {(r['risk_ema'] or 0) * 100:.0f}%." if r else ""
            return Answer(f"Nothing is predicted to fail in the next {cfg.HORIZON_S} seconds.{tail}").dict()
        lines = []
        for v in top:
            ttf = f", in about {v['ttf_s']:.0f} s" if v["ttf_s"] else ""
            lines.append(f"{v['id']}: {(v['risk_ema'] or 0) * 100:.0f}% risk{ttf}, likely {(v['kind_label'] or 'unclear').lower()}.")
        return Answer(" ".join(lines), highlight=[v["service"] for v in top]).dict()

    # Model quality.
    if words & {"model", "accuracy", "accurate", "precision", "recall", "auc", "trained", "training"}:
        c = p.predictor.card
        if not c:
            return Answer("No model report is available.").dict()
        e = c["evaluation"]["event"]
        return Answer(
            f"Trained on {c['n_episodes']} real failure experiments. On {c['n_test_episodes']} held-out experiments, "
            f"it predicted {e['caught']} of {e['failures']} progressive failures, a median {e['lead_median_s']:.0f} s ahead, "
            f"and raised {e['false_alarms']} false alarms across {e['survivors']} experiments with no failure. "
            "Sudden crashes can't be predicted by any model, so the platform handles those by fast recovery.").dict()

    # History & numbers.
    if words & {"mttr", "availability", "uptime", "stats", "numbers", "metrics", "prevented", "many"}:
        a = p.analytics()["summary"]
        mt = f"{a['mttr_s']:.1f} s" if a["mttr_s"] is not None else "no failures yet"
        return Answer(f"Availability so far: {pct(a['availability_pct'])} of {a['requests']:,} requests. "
                      f"{a['incidents']} incidents: {a['prevented']} prevented, {a['recovered']} recovered, "
                      f"{a['self_resolved']} cleared on their own, {a['escalated']} escalated. Mean time to recover: {mt}.").dict()
    if words & {"incident", "incidents", "happened", "last", "latest", "recent", "summary", "postmortem"}:
        rows = p.store.incidents(3)
        if not rows:
            return Answer("No incidents yet. Start the guided demo or inject a failure to see one.").dict()
        i = rows[0]
        t = time.strftime("%H:%M:%S", time.localtime(i["opened_at"]))
        text = f"Latest is incident #{i['id']} at {t}: {i['service']}, {(i.get('kind_label') or 'unclassified').lower()}, {_outcome(i)}."
        if i.get("affected"):
            text += f" It also affected {', '.join(i['affected'])}."
        if i.get("failed_requests") is not None:
            text += f" {i['failed_requests']} of {i['total_requests']} user requests failed during it."
        if i.get("recommendation"):
            text += f" Next step: {i['recommendation']}"
        return Answer(text, highlight=[i["service"]]).dict()
    if words & {"fix", "recommend", "recommendation", "should", "advice", "suggest"} and not svc:
        rows = p.store.incidents(50)
        if not rows:
            return Answer("Nothing to fix yet: no incidents on record.").dict()
        counts: dict[tuple, int] = {}
        for r in rows:
            if r.get("kind") in fl.ROOT_FIX:
                k = (r["service"], r["kind"])
                counts[k] = counts.get(k, 0) + 1
        if not counts:
            return Answer("No repeated failures on record.").dict()
        (service, kind), n = max(counts.items(), key=lambda kv: kv[1])
        return Answer(f"Start with {service}: {fl.LABELS[kind].lower()}, {n} time{'s' if n != 1 else ''}. {fl.ROOT_FIX[kind]}", highlight=[service]).dict()

    if svc:
        return describe_service(p, svc).dict()
    if words & {"status", "health", "healthy", "overview", "how", "ok", "everything", "wrong", "down", "failing", "problem", "root", "cause", "why"}:
        return Answer(snap["briefing"]["text"], highlight=[snap["briefing"]["focus"]] if snap["briefing"]["focus"] else []).dict()
    return Answer("I can answer questions on service health, predicted failures, dependencies, incidents and the model. "
                  "Try: why is payment failing, what happens if inventory fails, or how accurate is the model.").dict()

"""End-to-end tests: real processes, real faults, real recovery, through the public API.
Takes a few minutes. Uses its own ports and its own database."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PORT = 8890
ENV = {"FORESIGHT_PORT": str(PORT), "FORESIGHT_LB_PORT": "8891", "FORESIGHT_LOADGEN_PORT": "8892",
       "FORESIGHT_SERVICE_PORT_BASE": "19500", "FORESIGHT_RPS": "18"}


def call(path, body=None, method=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()


def state():
    return call("/api/state")


def wait_for(fn, timeout, what):
    t_end = time.time() + timeout
    last = None
    while time.time() < t_end:
        try:
            last = fn()
            if last:
                return last
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for {what}. Last: {last}")


def settled():
    s = state()
    return not s["open_incidents"] and all(x["state"] == "healthy" for x in s["services"]) and len(s["instances"]) == 11


def incident_for(service, since, closed=True):
    for i in call("/api/incidents"):
        if i["service"] == service and i["opened_at"] >= since - 1 and (i["status"] == "resolved" or not closed):
            return i
    return None


@pytest.fixture(scope="module", autouse=True)
def platform(tmp_path_factory):
    env = {**os.environ, **ENV, "FORESIGHT_DB": str(tmp_path_factory.mktemp("db") / "e2e.db")}
    proc = subprocess.Popen([sys.executable, "run.py", "--no-browser"], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: state().get("instances"), 90, "platform start")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@pytest.fixture(autouse=True)
def clean_slate():
    call("/api/mode", {"mode": "autonomous"})
    wait_for(settled, 90, "system to settle")


def test_dashboard_and_docs_are_served():
    assert "Academia Shield" in call("/")
    assert "swagger" in call("/docs").lower()
    assert "lineChart" in call("/static/app.js")


def test_dependency_map_discovered_from_traffic():
    from foresight import config as cfg
    expected = {(s, d) for s in cfg.TOPOLOGY for _, calls in cfg.TOPOLOGY[s]["ops"].values() for d, _, _ in calls}
    def found():
        edges = {(e["src"], e["dst"]) for e in state()["edges"]}
        return edges == expected
    wait_for(found, 45, "all edges discovered")
    edge = next(e for e in state()["edges"] if e["src"] == "orders" and e["dst"] == "notification")
    assert edge["required"] is False


def test_traffic_is_healthy_at_rest():
    wait_for(lambda: state()["availability"]["minute"]["total"] > 100, 30, "traffic")
    assert state()["availability"]["minute"]["pct"] > 99


def test_memory_leak_is_predicted_and_prevented():
    t0 = time.time()
    call("/api/chaos", {"target": "payment", "kind": "memory_leak", "params": {"rate_mb_s": 3.5}})
    inc = wait_for(lambda: incident_for("payment", t0), 120, "incident to close")
    assert inc["outcome"] == "prevented", inc["timeline"]
    assert inc["failed_at"] is None
    assert inc["predicted_at"] is not None and inc["factors"]
    assert inc["failed_requests"] / max(1, inc["total_requests"]) < 0.02


def test_crash_is_recovered_quickly_with_little_user_impact():
    t0 = time.time()
    call("/api/chaos", {"target": "catalog", "kind": "crash"})
    inc = wait_for(lambda: incident_for("catalog", t0), 45, "recovery")
    assert inc["outcome"] == "recovered"
    assert inc["ttr_s"] < 8
    assert inc["kind"] == "crash"
    assert inc["failed_requests"] / max(1, inc["total_requests"]) < 0.05


def test_cascade_restarts_only_the_root_cause():
    before = {v["id"] for v in state()["instances"]}
    t0 = time.time()
    call("/api/chaos", {"target": "inventory", "kind": "hang"})
    # The state stays valid JSON for browsers while an instance is unreachable.
    def strict_while_down():
        raw = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=5).read()
        def boom(c):
            raise AssertionError(f"non-standard JSON constant {c}")
        s = json.loads(raw, parse_constant=boom)
        return any(v["probe_ok"] is False for v in s["instances"])
    wait_for(strict_while_down, 6, "instance to be seen as unreachable")
    inc = wait_for(lambda: incident_for("inventory", t0), 45, "recovery")
    assert inc["kind"] == "hang" and inc["outcome"] == "recovered"
    assert inc["ttr_s"] >= 2.0, "recovery time must include detection"
    assert inc["affected"], "the cascade should have been seen"
    after = {v["id"] for v in state()["instances"]}
    replaced = before - after
    assert replaced == {i for i in before if i.startswith("inventory")}, replaced


def test_harmless_spike_does_not_trigger_a_restart():
    before = {v["id"] for v in state()["instances"]}
    call("/api/chaos", {"target": "cart", "kind": "cache_growth", "params": {"target_mb": 70, "rate_mb_s": 3.0, "duration_s": 35}})
    time.sleep(45)
    assert before == {v["id"] for v in state()["instances"]}


def test_bad_release_escalates_then_rollback_restores():
    t0 = time.time()
    call("/api/chaos", {"target": "notification", "kind": "bad_release"})
    def paused():
        s = next(x for x in state()["services"] if x["name"] == "notification")
        return s["state"] == "quarantined"
    wait_for(paused, 90, "quarantine")
    n = call("/api/notifications")
    assert any("needs a person" in x["title"] for x in n)
    # Notifications are optional: checkout must keep working.
    time.sleep(5)
    assert state()["availability"]["minute"]["pct"] > 95
    call("/api/services/notification/rollback", {})
    inc = wait_for(lambda: incident_for("notification", t0), 45, "incident close")
    assert inc["outcome"] == "resolved_by_operator"


def test_no_protection_mode_leaves_a_failed_instance_down():
    call("/api/mode", {"mode": "observe"})
    call("/api/chaos", {"target": "cart", "kind": "crash"})
    time.sleep(8)
    s = state()
    assert next(x for x in s["services"] if x["name"] == "cart")["state"] == "down"
    assert "down" in s["briefing"]["text"].lower()
    call("/api/mode", {"mode": "autonomous"})
    wait_for(settled, 45, "recovery once protection returns")


def test_manual_restart_has_no_downtime():
    t0 = int(time.time())
    before = len(call("/api/incidents"))
    r = call("/api/services/auth/restart", {})
    assert r["restarting"]
    wait_for(lambda: r["restarting"][0] not in {v["id"] for v in state()["instances"]}, 30, "replacement")
    wait_for(settled, 45, "settle")
    time.sleep(7)
    rows = [s for s in call("/api/traffic?seconds=120") if s["ts"] >= t0]
    assert sum(s["fail"] for s in rows) <= 2, rows
    assert len(call("/api/incidents")) == before, "a planned restart is not an incident"


def test_webhook_notification_is_delivered():
    got = []
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 8899), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        call("/api/settings", {"webhook_url": "http://127.0.0.1:8899/hook"}, method="PUT")
        res = call("/api/settings/test", {})
        assert res["delivery"]["webhook"] == "sent"
        assert got and got[0]["title"] == "Test notification"
    finally:
        call("/api/settings", {"webhook_url": ""}, method="PUT")
        srv.shutdown()


@pytest.mark.parametrize("q,needle", [
    ("what is at risk?", "predicted"), ("what happens if inventory fails?", "catalog"),
    ("how accurate is the model?", "held-out"), ("what does orders depend on?", "notification"),
    ("restart payment", "replaces"), ("inject a memory leak into auth", "memory leak"),
    ("asdf qwer", "I can answer"), ("", "Ask about"),
])
def test_assistant_answers(q, needle):
    a = call("/api/assistant", {"question": q})
    assert needle.lower() in a["text"].lower()


def test_assistant_never_acts_by_itself():
    before = {v["id"] for v in state()["instances"]}
    a = call("/api/assistant", {"question": "crash payment"})
    assert a["actions"]
    time.sleep(3)
    assert before == {v["id"] for v in state()["instances"]}


@pytest.mark.parametrize("path,body", [
    ("/api/chaos", {"target": "nope", "kind": "crash"}),
    ("/api/chaos", {"target": "cart", "kind": "nope"}),
    ("/api/mode", {"mode": "nope"}),
    ("/api/services/nope/restart", {}),
])
def test_bad_requests_get_clear_errors(path, body):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(path, body)
    assert e.value.code == 400
    assert json.loads(e.value.read())["detail"]


def test_guided_demo_can_start_and_stop():
    assert call("/api/scenario/start", {})["state"] == "running"
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/scenario/start", {})
    assert e.value.code == 409
    assert call("/api/scenario/stop", {})["state"] == "stopped"

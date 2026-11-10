"""Fast tests for the decision logic. No processes or network."""
import asyncio
import math
import time

import numpy as np
import pytest

from foresight import config as cfg
from foresight.control.assistant import find_service
from foresight.control.incidents import IncidentManager, incident_report, kind_from_exit
from foresight.control.notifier import webhook_payload
from foresight.control.store import Store
from foresight.control.telemetry import DependencyMap, FailureDetector
from foresight.ml.features import FEATURE_NAMES, compute_features
from foresight.ml.train import alarm_time, event_scores
from foresight.services.balancer import Backend, Balancer, BREAKER_FAILS


def sample(**kw):
    base = dict(ts=time.time(), instance="a-1", service="a", alive=True, probe_ok=True, uptime_s=60, rss_mb=50, mem_pct=0.2,
                cpu=0.05, fds=10, rps=5.0, err_self=0.0, err_dep=0.0, p50_ms=5, p95_ms=10, self_p95_ms=10, n=25,
                inflight=1, pool_pct=0.0, lag_ms=1.0)
    base.update(kw)
    return base


# ------------------------------------------------------------ features
def test_features_have_fixed_shape_and_are_finite_when_healthy():
    x = compute_features([sample() for _ in range(30)])
    assert x.shape == (len(FEATURE_NAMES),)
    assert np.isfinite(x).all()


def test_features_detect_a_rising_trend():
    i = FEATURE_NAMES.index("mem_slope30")
    eta = FEATURE_NAMES.index("mem_eta")
    x = compute_features([sample(mem_pct=0.2 + 0.01 * t) for t in range(30)])
    assert x[i] == pytest.approx(0.01, abs=1e-6)
    assert x[eta] == pytest.approx((1 - 0.49) / 0.01, rel=0.01)


def test_features_survive_missing_data():
    nan = math.nan
    rows = [sample(probe_ok=False, rss_mb=nan, mem_pct=nan, cpu=nan, fds=nan, rps=nan, err_self=nan, err_dep=nan,
                   p95_ms=nan, self_p95_ms=nan, inflight=nan, pool_pct=nan, lag_ms=nan) for _ in range(6)]
    x = compute_features(rows)
    assert x.shape == (len(FEATURE_NAMES),)
    assert x[FEATURE_NAMES.index("probe_miss10")] == 6


# ------------------------------------------------------------ failure definition
def test_detector_process_exit_is_immediate():
    assert FailureDetector().update(sample(alive=False)).reason == "process_exit"


def test_detector_needs_consecutive_probe_failures():
    d = FailureDetector()
    assert not d.update(sample(probe_ok=False)).failed
    assert not d.update(sample(probe_ok=False)).failed
    assert not d.update(sample()).failed            # one good probe resets it
    for _ in range(cfg.PROBE_FAILS_DOWN - 1):
        assert not d.update(sample(probe_ok=False)).failed
    assert d.update(sample(probe_ok=False)).reason == "unresponsive"


def test_detector_ignores_dependency_errors():
    d = FailureDetector()
    for _ in range(10):
        assert not d.update(sample(err_dep=1.0)).failed


def test_detector_requires_sustained_own_errors_with_enough_traffic():
    d = FailureDetector()
    assert not d.update(sample(err_self=1.0)).failed
    assert not d.update(sample(err_self=0.0)).failed
    for _ in range(cfg.SELF_ERROR_FAIL_S):
        v = d.update(sample(err_self=0.9))
    assert v.reason == "error_rate"
    low = FailureDetector()
    for _ in range(10):
        assert not low.update(sample(err_self=1.0, n=1)).failed


# ------------------------------------------------------------ dependency map
def graph():
    g = DependencyMap()
    for src, inst, deps in [("gateway", "g", {"catalog": True, "cart": True}), ("cart", "c", {"catalog": True}),
                            ("catalog", "k", {"inventory": True}), ("orders", "o", {"notification": False, "inventory": True}),
                            ("inventory", "i", {}), ("notification", "n", {})]:
        g.observe({"service": src, "instance": inst,
                   "deps": {d: {"calls": 5, "errors": 0, "ms": 50.0, "required": r} for d, r in deps.items()}})
    return g


def test_graph_learned_from_calls_and_blast_radius():
    g = graph()
    assert set(g.blast_radius("inventory")) == {"catalog", "orders", "cart", "gateway"}
    assert g.blast_radius("gateway") == []


def test_optional_dependency_has_no_blast_radius():
    assert graph().blast_radius("notification") == []


def test_root_cause_is_the_origin_not_the_symptoms():
    g = graph()
    assert g.root_causes({"inventory"}) == {"inventory"}
    assert g.root_causes(set()) == set()     # symptoms alone never get called the root cause


def test_counters_are_differenced_not_double_counted():
    g = DependencyMap()
    for calls in (10, 15):
        g.observe({"service": "a", "instance": "a-1", "deps": {"b": {"calls": calls, "errors": 0, "ms": 1.0, "required": True}}})
    assert g.edges[("a", "b")]["calls"] == 15


# ------------------------------------------------------------ load balancer
def make_lb(mode="intelligent"):
    lb = Balancer(mode)
    lb.update_routes({"s": [{"instance": "s-1", "port": 1}, {"instance": "s-2", "port": 2}]})
    return lb


def test_round_robin_rotates():
    lb = make_lb("round_robin")
    assert {lb.pick("s", set()).instance for _ in range(4)} == {"s-1", "s-2"}


def test_weight_moves_traffic_away_from_a_risky_instance():
    lb = make_lb()
    lb.update_routes({"s": [{"instance": "s-1", "port": 1, "weight": 0.05}, {"instance": "s-2", "port": 2, "weight": 1.0}]})
    picks = [lb.pick("s", set()).instance for _ in range(400)]
    assert picks.count("s-2") > 340
    assert picks.count("s-1") > 0      # still sampled, so its health stays measurable


def test_equal_replicas_share_traffic():
    lb = make_lb()
    picks = [lb.pick("s", set()).instance for _ in range(400)]
    assert 120 < picks.count("s-1") < 280


def test_breaker_opens_then_allows_one_trial():
    lb = make_lb()
    b = lb.routes["s"]["s-1"]
    now = time.monotonic()
    for _ in range(BREAKER_FAILS):
        b.on_result(100, True, now)
    assert b.state(now) == "open"
    assert all(lb.pick("s", set()).instance == "s-2" for _ in range(20))
    assert b.available(b.open_until + 1)
    b.on_result(10, False, now)
    assert b.state(now) == "closed"


def test_draining_gets_no_traffic_and_excludes_are_respected():
    lb = make_lb()
    lb.routes["s"]["s-1"].draining = True
    assert lb.pick("s", set()).instance == "s-2"
    assert lb.pick("s", {"s-2"}) is None


def test_dependency_errors_do_not_trip_the_breaker_logic_directly():
    b = Backend("x", 1)
    for _ in range(50):
        b.on_result(5, False, time.monotonic())
    assert b.state(time.monotonic()) == "closed"


# ------------------------------------------------------------ incidents
class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, level, title, body, incident_id=None, dedup_key=None):
        self.sent.append((level, title))


class Pred:
    ema, risk, ttf_s, kind, factors = 0.8, 0.8, 20.0, "memory_leak", [{"text": "Memory is rising", "impact": 0.5, "signal": "mem"}]


@pytest.fixture
def mgr(tmp_path):
    return IncidentManager(Store(tmp_path / "t.db"), FakeNotifier())


def closes(m, service="s"):
    out = None
    for _ in range(5):
        out = m.maybe_close(service, True, False, lambda a, b: (100, 2)) or out
    return out


def test_prevented_incident(mgr):
    mgr.predicted("s", "s-1", Pred, ["memory_leak"])
    mgr.action("s", "Proactive replacement started")
    mgr.recovered("s", "s-2", True)
    inc = closes(mgr)
    assert inc["outcome"] == "prevented" and inc["failed_at"] is None and inc["failed_requests"] == 2
    assert "s" not in mgr.open


def test_failure_after_prediction_records_lead_and_keeps_the_ml_type(mgr):
    mgr.predicted("s", "s-1", Pred, [])
    mgr.failed("s", "s-1", "process_exit", 1, [])
    assert mgr.open["s"]["lead_s"] is not None and mgr.open["s"]["kind"] == "memory_leak"
    assert mgr.open["s"]["detected_at"] >= mgr.open["s"]["failed_at"]
    mgr.action("s", "Restart")
    mgr.recovered("s", "s-2", False)
    assert closes(mgr)["outcome"] == "recovered"


def test_oom_exit_code_is_read_as_memory(mgr):
    assert kind_from_exit("process_exit", 137) == "memory_leak"
    assert kind_from_exit("process_exit", 78) == "bad_release"
    assert kind_from_exit("unresponsive", None) == "hang"


def test_recovery_time_counts_from_when_it_really_broke(mgr):
    mgr.failed("s", "s-1", "unresponsive", None, [], onset=time.time() - 3.0)
    mgr.recovered("s", "s-2", False)
    assert mgr.open["s"]["ttr_s"] >= 3.0


def test_incident_stays_open_while_not_calm(mgr):
    mgr.failed("s", "s-1", "process_exit", 1, [])
    for _ in range(10):
        assert mgr.maybe_close("s", False, False, lambda a, b: (0, 0)) is None
    assert "s" in mgr.open


def test_incident_report_is_complete(mgr):
    mgr.predicted("s", "s-1", Pred, [])
    mgr.action("s", "Proactive replacement started")
    text = incident_report(closes(mgr))
    assert "# Incident" in text and "Memory is rising" in text and "Suggested fix" in text


def test_incidents_persist(tmp_path):
    st = Store(tmp_path / "p.db")
    m = IncidentManager(st, FakeNotifier())
    m.failed("s", "s-1", "process_exit", 1, [])
    st.close()
    st2 = Store(tmp_path / "p.db")
    assert st2.incidents()[0]["service"] == "s"
    assert st2.next_incident_id() == 2


# ------------------------------------------------------------ evaluation logic
def test_alarm_needs_sustained_risk():
    ts = np.arange(10.0)
    assert alarm_time(ts, np.array([0.0, 0.99, 0.0] * 3 + [0.0]), 0.9) is None
    assert alarm_time(ts, np.ones(10), 0.6) is not None


def test_event_scores_count_misses_and_false_alarms():
    t = np.arange(50.0)
    data = [
        dict(ts=t, kind="memory_leak", fail_ts=60.0, end_ts=60.0),  # caught
        dict(ts=t, kind="memory_leak", fail_ts=60.0, end_ts=60.0),  # missed
        dict(ts=t, kind="cache_growth", fail_ts=None, end_ts=50.0),  # false alarm
        dict(ts=t, kind="none", fail_ts=None, end_ts=50.0),         # quiet
    ]
    s = event_scores(data, [np.ones(50), np.zeros(50), np.ones(50), np.zeros(50)], 0.6)
    assert (s["caught"], s["missed"], s["false_alarms"], s["survivors"]) == (1, 1, 1, 2)
    assert s["lead_median_s"] > 0


# ------------------------------------------------------------ assistant & notifications
@pytest.mark.parametrize("text,want", [("why is payment failing", "payment"), ("restart the paymnt service", "payment"),
                                       ("what about stock", "inventory"), ("hello there", None)])
def test_service_linking(text, want):
    assert find_service(text) == want


def test_webhook_formats():
    note = dict(level="critical", title="T", body="B", ts=1.0, incident_id=3)
    assert "content" in webhook_payload("https://discord.com/api/webhooks/1", note)
    assert "text" in webhook_payload("https://hooks.slack.com/x", note)
    assert webhook_payload("https://example.org/h", note)["incident_id"] == 3

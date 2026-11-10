"""Live failure prediction: risk, time to failure, likely failure type and the reasons why."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

os.environ.setdefault("OMP_NUM_THREADS", "1")

import joblib
import numpy as np
import sklearn

from foresight import config as cfg
from foresight.ml import faults as fl
from foresight.ml.features import FEATURE_GROUP, FEATURE_NAMES, compute_features

EXPLAIN_FROM = 0.30


@dataclass
class Prediction:
    risk: float = 0.0          # raw probability of failure within the horizon
    ema: float = 0.0           # smoothed risk, the value alarms use
    streak: int = 0
    alarm: bool = False
    ttf_s: float | None = None
    kind: str | None = None
    kind_confidence: float | None = None
    anomaly: float = 0.0
    anomalous: bool = False
    factors: list[dict] = field(default_factory=list)


def _f(v, default=0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(v) else v


def describe_signal(group: str, window: list[dict]) -> str:
    """One plain sentence stating what a signal is doing right now, from the live numbers."""
    last = window[-1]
    rows = window[-20:]

    def rate(key: str, scale: float = 1.0) -> float:
        vals = [(i, _f(s.get(key), math.nan)) for i, s in enumerate(rows)]
        vals = [(i, v) for i, v in vals if not math.isnan(v)]
        if len(vals) < 3:
            return 0.0
        xs, ys = zip(*vals)
        return float(np.polyfit(xs, ys, 1)[0]) * scale

    if group == "mem":
        return f"Memory is at {_f(last.get('mem_pct')) * 100:.0f}% of its limit, changing {rate('rss_mb'):+.1f} MB per second"
    if group == "cpu":
        return f"CPU is at {_f(last.get('cpu')) * 100:.0f}% of one core"
    if group == "pool":
        return f"{_f(last.get('pool_pct')) * 100:.0f}% of worker slots are taken, and they are not coming back"
    if group == "lag":
        return f"The event loop is stalling for up to {_f(last.get('lag_ms')):.0f} ms"
    if group == "err_self":
        return f"{_f(last.get('err_self')) * 100:.0f}% of requests fail in this instance's own code"
    if group == "err_dep":
        return f"{_f(last.get('err_dep')) * 100:.0f}% of requests fail because of a dependency"
    if group in ("self_p95", "p95"):
        return f"Own p95 latency is {_f(last.get('self_p95_ms')):.0f} ms, changing {rate('self_p95_ms'):+.1f} ms per second"
    if group == "inflight":
        return f"{_f(last.get('inflight')):.0f} requests are in progress at once"
    if group == "rps":
        return f"Traffic here is {_f(last.get('rps')):.1f} requests per second"
    if group == "fds":
        return f"The process has {_f(last.get('fds')):.0f} open handles"
    return "The instance has only just started"


class Predictor:
    def __init__(self) -> None:
        self.models: dict | None = None
        self.threshold = cfg.DEFAULT_RISK_THRESHOLD
        self.state: dict[str, Prediction] = {}
        self.card: dict = {}
        self.groups: dict[str, list[int]] = {}
        for i, n in enumerate(FEATURE_NAMES):
            self.groups.setdefault(FEATURE_GROUP[n], []).append(i)

    @property
    def available(self) -> bool:
        return self.models is not None

    def load(self) -> tuple[bool, str]:
        """Load the saved model, or retrain from recorded telemetry if it can't be used here."""
        path = cfg.MODEL_DIR / "predictor.joblib"
        try:
            m = joblib.load(path)
            if m.get("sklearn_version") != sklearn.__version__:
                raise ValueError(f"saved with scikit-learn {m.get('sklearn_version')}, this is {sklearn.__version__}")
            if m.get("feature_names") != FEATURE_NAMES:
                raise ValueError("feature set changed")
            m["clf"].predict_proba(np.zeros((1, len(FEATURE_NAMES))))
            msg = "model loaded"
        except Exception as e:  # noqa: BLE001 - any load problem has the same remedy
            data = cfg.DATA_DIR / "telemetry.jsonl.gz"
            if not data.exists():
                return False, f"model unavailable ({e}) and no telemetry to retrain from"
            print(f"Retraining the model on this machine ({e}). This takes a few minutes, once.", flush=True)
            from foresight.ml.train import run  # imported late: only needed here
            run(data)
            m = joblib.load(path)
            msg = "model retrained for this machine"
        self.models = m
        self.threshold = float(m["threshold"])
        for est in (m["clf"], m["ttf"], m["type"]):
            if hasattr(est, "n_jobs"):
                est.n_jobs = 1
        card = cfg.MODEL_DIR / "model_card.json"
        self.card = json.loads(card.read_text(encoding="utf-8")) if card.exists() else {}
        return True, msg

    def forget(self, instance: str) -> None:
        self.state.pop(instance, None)

    def predict(self, windows: dict[str, list[dict]]) -> dict[str, Prediction]:
        """windows: instance id to recent samples. Keeps risk state between calls."""
        if not self.available:
            return {i: self.state.setdefault(i, Prediction()) for i in windows}
        ready = [i for i, w in windows.items() if len(w) >= cfg.MIN_SAMPLES_FOR_PREDICTION]
        out = {i: self.state.setdefault(i, Prediction()) for i in windows}
        if not ready:
            return out
        X = np.vstack([compute_features(windows[i]) for i in ready])
        m = self.models
        probs = m["clf"].predict_proba(X)[:, 1]
        scores = -m["iso"].score_samples(np.where(np.isnan(X), m["baseline"], X))
        ttf = np.expm1(m["ttf"].predict(X))
        for row, iid in enumerate(ready):
            p = out[iid]
            p.risk = float(probs[row])
            p.ema = cfg.RISK_EMA_ALPHA * p.risk + (1 - cfg.RISK_EMA_ALPHA) * p.ema
            p.streak = p.streak + 1 if p.ema >= self.threshold else 0
            p.alarm = p.streak >= cfg.RISK_CONSECUTIVE
            p.anomaly = float(scores[row])
            p.anomalous = p.anomaly >= m["anomaly_threshold"]
            if p.ema >= EXPLAIN_FROM:
                p.ttf_s = float(max(1.0, ttf[row]))
                proba = m["type"].predict_proba(X[[row]])[0]
                best = int(np.argmax(proba))
                p.kind = str(m["type"].classes_[best])
                p.kind_confidence = float(proba[best])
                p.factors = self._explain(X[row], p.risk, windows[iid])
            else:
                p.ttf_s, p.kind, p.kind_confidence, p.factors = None, None, None, []
        return out

    def _explain(self, x: np.ndarray, base: float, window: list[dict]) -> list[dict]:
        """Occlusion: reset one signal's features to healthy values and see how far risk falls."""
        names = [g for g in self.groups if g != "warm"]
        batch = np.tile(x, (len(names), 1))
        for r, g in enumerate(names):
            idx = self.groups[g]
            batch[r, idx] = self.models["baseline"][idx]
        probs = self.models["clf"].predict_proba(batch)[:, 1]
        rows = [{"signal": g, "impact": float(base - q), "text": describe_signal(g, window)}
                for g, q in zip(names, probs)]
        rows = [r for r in rows if r["impact"] > 0.03]
        rows.sort(key=lambda r: -r["impact"])
        return rows[:3]


def kind_label(kind: str | None) -> str | None:
    return fl.LABELS.get(kind, kind) if kind else None

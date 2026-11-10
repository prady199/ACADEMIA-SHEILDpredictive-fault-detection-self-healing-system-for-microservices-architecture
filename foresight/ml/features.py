"""Turns a telemetry window into model features. Training and live prediction both call
compute_features, so what the model sees in production is what it was trained on."""
from __future__ import annotations

import math

import numpy as np

from foresight import config as cfg

# Signal, raw sample key, whether to compress with log1p.
SIGNALS: list[tuple[str, str, bool]] = [
    ("mem", "mem_pct", False),
    ("cpu", "cpu", False),
    ("pool", "pool_pct", False),
    ("lag", "lag_ms", True),
    ("err_self", "err_self", False),
    ("err_dep", "err_dep", False),
    ("self_p95", "self_p95_ms", True),
    ("p95", "p95_ms", True),
    ("inflight", "inflight", True),
    ("rps", "rps", False),
    ("fds", "fds", True),
]
STATS = ["last", "mean10", "max10", "slope10", "slope30"]
ETA_CAP_S = 300.0


def _names() -> list[str]:
    names = [f"{sig}_{st}" for sig, _, _ in SIGNALS for st in STATS]
    names += ["mem_eta", "pool_eta", "err_eta", "lat_eta", "probe_miss10", "warm"]
    return names


FEATURE_NAMES = _names()
# Which plain-English signal each feature belongs to, for explanations.
FEATURE_GROUP = {
    **{f"{sig}_{st}": sig for sig, _, _ in SIGNALS for st in STATS},
    "mem_eta": "mem", "pool_eta": "pool", "err_eta": "err_self", "lat_eta": "self_p95",
    "probe_miss10": "lag", "warm": "warm",
}


def _slope(y: np.ndarray) -> float:
    """Least-squares slope per sample, ignoring gaps."""
    mask = ~np.isnan(y)
    n = int(mask.sum())
    if n < 3:
        return math.nan
    x = np.nonzero(mask)[0].astype(float)
    v = y[mask]
    x = x - x.mean()
    denom = float((x * x).sum())
    return float((x * (v - v.mean())).sum() / denom) if denom else math.nan


def _nan_stat(y: np.ndarray, fn) -> float:
    v = y[~np.isnan(y)]
    return float(fn(v)) if v.size else math.nan


def _eta(last: float, slope: float, limit: float) -> float:
    if math.isnan(last) or math.isnan(slope) or slope <= 1e-6:
        return ETA_CAP_S
    return float(min(ETA_CAP_S, max(0.0, (limit - last) / slope)))


def compute_features(window: list[dict]) -> np.ndarray:
    """window: samples oldest to newest, at about one per second (up to 30)."""
    out: list[float] = []
    series: dict[str, np.ndarray] = {}
    for sig, key, logged in SIGNALS:
        y = np.array([float(s.get(key, math.nan)) for s in window], dtype=float)
        if logged:
            y = np.log1p(np.maximum(y, 0.0))  # NaN passes through untouched
        series[sig] = y
        tail = y[-10:]
        out += [
            _nan_stat(tail, lambda v: v[-1]),   # most recent known value
            _nan_stat(tail, np.mean),
            _nan_stat(tail, np.max),
            _slope(tail),
            _slope(y),
        ]
    def last_of(sig: str) -> float:
        return _nan_stat(series[sig][-10:], lambda v: v[-1])
    out.append(_eta(last_of("mem"), _slope(series["mem"]), 1.0))
    out.append(_eta(last_of("pool"), _slope(series["pool"]), 1.0))
    out.append(_eta(last_of("err_self"), _slope(series["err_self"]), cfg.SELF_ERROR_FAIL))
    out.append(_eta(last_of("self_p95"), _slope(series["self_p95"]), math.log1p(cfg.SELF_P95_FAIL_MS)))
    out.append(float(sum(not s.get("probe_ok", True) for s in window[-10:])))
    out.append(min(float(window[-1].get("uptime_s", 0.0)), 30.0) / 30.0)
    return np.array(out, dtype=float)

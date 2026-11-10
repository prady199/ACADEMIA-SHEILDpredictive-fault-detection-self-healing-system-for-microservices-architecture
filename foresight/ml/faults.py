"""The failure catalogue used by data collection, the chaos lab and the benchmark."""
from __future__ import annotations

import random

# Progressive failures build up over time, so they can be predicted.
PROGRESSIVE = ["memory_leak", "cpu_ramp", "pool_leak", "latency_ramp", "error_ramp"]
# Sudden failures give no warning: no model can predict them, so recovery handles them.
SUDDEN = ["crash", "hang"]
# Harmless look-alikes that end on their own. Acting on these would be a false alarm.
BENIGN = ["cache_growth", "cpu_burst", "latency_blip", "error_blip"]

LABELS = {
    "memory_leak": "Memory leak",
    "cpu_ramp": "CPU saturation",
    "pool_leak": "Connection pool exhaustion",
    "latency_ramp": "Latency degradation",
    "error_ramp": "Rising error rate",
    "crash": "Process crash",
    "hang": "Process hang",
    "bad_release": "Bad release (crash loop)",
    "cache_growth": "Cache growth (harmless)",
    "cpu_burst": "CPU burst (harmless)",
    "latency_blip": "Latency blip (harmless)",
    "error_blip": "Error blip (harmless)",
}

# Fix guidance shown with incidents: a restart only buys time for most of these.
ROOT_FIX = {
    "memory_leak": "A restart clears the memory but the leak will return. Take a heap profile and look at what grows with traffic.",
    "cpu_ramp": "Look for a blocking or CPU-heavy code path on the request thread, then profile it.",
    "pool_leak": "Connections are being taken and not returned. Check error paths for a missing release.",
    "latency_ramp": "Compare recent changes and slow-query logs. Latency that grows steadily usually means something is accumulating.",
    "error_ramp": "Read this instance's error log around the start of the rise and check recent releases.",
    "crash": "Read the exit code and the last log lines to find the crash. Repeated crashes point to a bug or bad input.",
    "hang": "Capture a thread dump next time it stalls, and look for a deadlock or a blocking call.",
    "bad_release": "Roll the release back. Restarting cannot fix a version that dies at start-up.",
}


def sample_params(kind: str, rng: random.Random | None = None) -> dict:
    """Randomised severity, so the model can't memorise one fixed failure curve."""
    r = rng or random
    if kind == "memory_leak":
        return {"rate_mb_s": r.uniform(1.8, 5.0)}
    if kind == "cpu_ramp":
        return {"rate_ms_s": r.uniform(6.0, 14.0)}
    if kind == "pool_leak":
        return {"rate_slots_s": r.uniform(0.28, 0.9)}
    if kind == "latency_ramp":
        return {"rate_ms_s": r.uniform(5.0, 15.0)}
    if kind == "error_ramp":
        return {"rate_per_s": r.uniform(0.005, 0.012)}
    if kind == "cache_growth":
        return {"target_mb": r.uniform(35, 115), "rate_mb_s": r.uniform(1.8, 5.0), "duration_s": r.uniform(30, 70)}
    if kind == "cpu_burst":
        return {"block_ms": r.uniform(40, 130), "duration_s": r.uniform(8, 30)}
    if kind == "latency_blip":
        return {"extra_ms": r.uniform(60, 320), "duration_s": r.uniform(8, 25)}
    if kind == "error_blip":
        return {"p": r.uniform(0.05, 0.30), "duration_s": r.uniform(8, 25)}
    return {}

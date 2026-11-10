"""Central configuration: the demo application's topology and every platform threshold.

The topology below is what the *demo application* actually does. The platform never
reads it to build the dependency map: that map is learned from live call telemetry
(see control/deps.py). Only the services themselves and the supervisor read it.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "models"
LOG_DIR = ROOT / "logs"
WEB_DIR = Path(__file__).resolve().parent / "web"
DB_PATH = Path(os.environ.get("FORESIGHT_DB", str(DATA_DIR / "foresight.db")))

HOST = os.environ.get("FORESIGHT_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("FORESIGHT_PORT", "8800"))
LB_PORT = int(os.environ.get("FORESIGHT_LB_PORT", "8801"))
LOADGEN_PORT = int(os.environ.get("FORESIGHT_LOADGEN_PORT", "8802"))
SERVICE_PORT_BASE = int(os.environ.get("FORESIGHT_SERVICE_PORT_BASE", "18100"))

# ---------------------------------------------------------------- demo application
# ops: name -> (base work in ms, downstream calls [(service, op, required)])
TOPOLOGY: dict[str, dict] = {
    "gateway": {
        "replicas": 2,
        "label": "API gateway",
        "timeout_s": 3.5,
        "ops": {
            "browse": (2, [("auth", "verify", True), ("catalog", "list", True)]),
            "add_to_cart": (2, [("auth", "verify", True), ("cart", "add", True)]),
            "checkout": (3, [("auth", "verify", True), ("orders", "create", True)]),
        },
    },
    "auth": {"replicas": 1, "label": "Auth", "timeout_s": 1.0, "ops": {"verify": (4, [])}},
    "catalog": {
        "replicas": 2,
        "label": "Catalog",
        "timeout_s": 1.2,
        "ops": {
            "list": (6, [("inventory", "stock", True)]),
            "get": (4, [("inventory", "stock", True)]),
        },
    },
    "cart": {
        "replicas": 1,
        "label": "Cart",
        "timeout_s": 1.8,
        "ops": {"add": (5, [("catalog", "get", True)])},
    },
    "orders": {
        "replicas": 2,
        "label": "Orders",
        "timeout_s": 1.8,
        "ops": {
            "create": (
                8,
                [
                    ("inventory", "reserve", True),
                    ("payment", "charge", True),
                    ("notification", "send", False),
                ],
            )
        },
    },
    "inventory": {
        "replicas": 1,
        "label": "Inventory",
        "timeout_s": 0.8,
        "ops": {"stock": (3, []), "reserve": (6, [])},
    },
    "payment": {"replicas": 1, "label": "Payment", "timeout_s": 0.8, "ops": {"charge": (18, [])}},
    "notification": {
        "replicas": 1,
        "label": "Notifications",
        "timeout_s": 0.6,
        "ops": {"send": (5, [])},
    },
}

TRAFFIC_MIX = [("browse", 0.60), ("add_to_cart", 0.25), ("checkout", 0.15)]
DEFAULT_RPS = float(os.environ.get("FORESIGHT_RPS", "18"))

# ---------------------------------------------------------------- instance limits
MEMORY_LIMIT_MB = 256       # an instance over this is killed, like a container OOM kill
POOL_SIZE = 32              # worker/connection slots per instance
POOL_WAIT_S = 0.5           # queue time before a request is rejected

# ---------------------------------------------------------------- failure definition
# One definition, shared by the labeller (training) and the monitor (runtime).
PROBE_TIMEOUT_S = 0.9
PROBE_FAILS_DOWN = 3        # consecutive failed probes => instance is down
SELF_ERROR_FAIL = 0.50      # own error rate at/over this ...
SELF_ERROR_FAIL_S = 3       # ... for this many seconds => failed
SELF_P95_FAIL_MS = 500.0    # own p95 latency at/over this (callers start timing out) ...
SELF_P95_FAIL_S = 5         # ... for this many seconds => failed
MIN_REQUESTS_FOR_RATE = 5
LOOP_STALL_FAIL_MS = 500.0   # event loop blocked this long, sustained => failed
LOOP_STALL_FAIL_S = 5

# ---------------------------------------------------------------- health display thresholds
DEGRADED_SELF_ERROR = 0.15
DEGRADED_SELF_P95_MS = 250.0
IMPACTED_DEP_ERROR = 0.10

# ---------------------------------------------------------------- machine learning
HORIZON_S = 30              # "will this instance fail within the next 30 seconds?"
WINDOW_S = 30               # telemetry look-back used for features
MIN_SAMPLES_FOR_PREDICTION = 5
RISK_EMA_ALPHA = 0.5
RISK_CONSECUTIVE = 3        # ticks over threshold before acting
DEFAULT_RISK_THRESHOLD = 0.60  # replaced by the threshold chosen in models/model_card.json

# ---------------------------------------------------------------- recovery policy
TICK_S = 1.0
ACTION_COOLDOWN_S = 20      # no second action on the same service inside this window
STORM_MAX_RESTARTS = 5      # restarts of one service ...
STORM_WINDOW_S = 120        # ... inside this window => quarantine and page a human
READY_TIMEOUT_S = 12.0
DRAIN_TIMEOUT_S = 3.0
DEGRADED_ACTION_S = 8       # sustained own failure before a reactive restart


def required_deps(service: str) -> set[str]:
    out: set[str] = set()
    for _, calls in TOPOLOGY[service]["ops"].values():
        out |= {c[0] for c in calls if c[2]}
    return out


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODEL_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

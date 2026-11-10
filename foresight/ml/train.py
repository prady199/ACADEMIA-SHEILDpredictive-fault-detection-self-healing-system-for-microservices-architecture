"""Trains the failure-prediction models from recorded chaos telemetry and evaluates them on
episodes the models never saw. Metrics that matter here are per failure, not per second:
was the alarm early enough, and how often does it fire for nothing?

    python -m foresight.ml.train
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor, IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score

from foresight import config as cfg
from foresight.control.telemetry import FailureDetector
from foresight.ml import faults as fl
from foresight.ml.features import FEATURE_NAMES, compute_features

MAX_LEAD_FOR_TTF = 150.0


def load_episodes(path: Path) -> list[dict]:
    rows: dict[str, list] = defaultdict(list)
    meta: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a truncated last line from an interrupted run
            if "r" in d:
                rows[d["r"]["instance"]].append(d["r"])
            elif "e" in d:
                meta[d["e"]["episode"]] = d["e"]
    eps = []
    for iid, m in meta.items():
        rs = sorted(rows.get(iid, []), key=lambda r: r["ts"])
        if len(rs) < 10:
            continue
        for r in rs:
            for k, v in r.items():
                if v is None:
                    r[k] = math.nan
        ep = {**m, "rows": rs}
        relabel(ep)
        # A progressive fault that never reached failure before the run ended has an unknown
        # outcome. Calling it "survived" would teach the model that rising trouble is safe.
        if ep["kind"] in fl.PROGRESSIVE and ep["fail_ts"] is None:
            continue
        eps.append(ep)
    return eps


def relabel(ep: dict) -> None:
    """Derive the failure time from the raw samples, using the runtime's failure definition."""
    det = FailureDetector()
    ep["fail_ts"], ep["fail_reason"] = None, None
    for r in ep["rows"]:
        v = det.update(r)
        if v.failed:
            ep["fail_ts"], ep["fail_reason"] = r["ts"], v.reason
            return


def build_episode(ep: dict) -> dict:
    """Features plus labels for one lifetime. Samples whose outcome is unknown are masked."""
    rows, fail_ts = ep["rows"], ep["fail_ts"]
    kind = ep["kind"]
    X, y, ttf, ts, usable = [], [], [], [], []
    for i in range(len(rows)):
        t = rows[i]["ts"]
        if fail_ts is not None and t >= fail_ts:
            break                                   # already failed: nothing left to predict
        if i + 1 < cfg.MIN_SAMPLES_FOR_PREDICTION:
            continue
        window = rows[max(0, i + 1 - cfg.WINDOW_S):i + 1]
        lead = (fail_ts - t) if fail_ts is not None else math.inf
        label = int(lead <= cfg.HORIZON_S)
        keep = True
        if fail_ts is None and rows[-1]["ts"] - t < cfg.HORIZON_S:
            keep = False                            # survived, but not for a full horizon yet
        if kind in fl.SUDDEN and label:
            keep = False                            # nothing in the data foretells these
        X.append(compute_features(window))
        y.append(label)
        ttf.append(min(lead, MAX_LEAD_FOR_TTF))
        ts.append(t)
        usable.append(keep)
    return {"X": np.array(X).reshape(-1, len(FEATURE_NAMES)), "y": np.array(y, dtype=int),
            "ttf": np.array(ttf, dtype=float), "ts": np.array(ts, dtype=float),
            "usable": np.array(usable, dtype=bool), "kind": kind, "episode": ep["episode"],
            "service": ep["service"], "fail_ts": fail_ts, "inject_ts": ep["inject_ts"],
            "fail_reason": ep["fail_reason"], "end_ts": rows[-1]["ts"]}


def stack(data: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    X = np.vstack([d["X"][d["usable"]] for d in data])
    y = np.concatenate([d["y"][d["usable"]] for d in data])
    return X, y


def new_classifier(seed: int = 0) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.06, max_leaf_nodes=24, min_samples_leaf=30,
        l2_regularization=1.0, class_weight="balanced", early_stopping=False, random_state=seed,
    )


def alarm_time(ts: np.ndarray, p: np.ndarray, threshold: float) -> float | None:
    """The same logic the live platform uses: smoothed risk over threshold for N ticks."""
    ema, streak = 0.0, 0
    for t, v in zip(ts, p):
        ema = cfg.RISK_EMA_ALPHA * v + (1 - cfg.RISK_EMA_ALPHA) * ema
        streak = streak + 1 if ema >= threshold else 0
        if streak >= cfg.RISK_CONSECUTIVE:
            return float(t)
    return None


def event_scores(data: list[dict], probs: list[np.ndarray], threshold: float) -> dict:
    """Per-episode outcome. A failure caught needs an alarm before the failure itself, and
    within the horizon plus a margin; an alarm far earlier would only be luck."""
    caught, missed, leads = 0, 0, []
    false_alarms, quiet = 0, 0
    per_kind: dict[str, dict] = defaultdict(lambda: {"failures": 0, "caught": 0, "leads": []})
    hours = 0.0
    fa_events = 0
    for d, p in zip(data, probs):
        if not len(p):
            continue
        t_alarm = alarm_time(d["ts"], p, threshold)
        if d["fail_ts"] is not None and d["kind"] in fl.PROGRESSIVE:
            k = per_kind[d["kind"]]
            k["failures"] += 1
            if t_alarm is not None:
                caught += 1
                k["caught"] += 1
                lead = d["fail_ts"] - t_alarm
                leads.append(lead)
                k["leads"].append(lead)
            else:
                missed += 1
        elif d["fail_ts"] is None:
            hours += max(0.0, d["end_ts"] - d["ts"][0]) / 3600
            if t_alarm is not None:
                false_alarms += 1
                fa_events += 1
            else:
                quiet += 1
    recall = caught / max(1, caught + missed)
    precision = caught / max(1, caught + false_alarms)
    return {
        "failures": caught + missed, "caught": caught, "missed": missed,
        "recall": recall, "precision": precision,
        "survivors": false_alarms + quiet, "false_alarms": false_alarms,
        "false_alarm_episode_rate": false_alarms / max(1, false_alarms + quiet),
        "false_alarms_per_instance_hour": fa_events / max(hours, 1e-6),
        "lead_median_s": float(np.median(leads)) if leads else None,
        "lead_p10_s": float(np.percentile(leads, 10)) if leads else None,
        "per_kind": {
            k: {"failures": v["failures"], "caught": v["caught"],
                "lead_median_s": float(np.median(v["leads"])) if v["leads"] else None}
            for k, v in sorted(per_kind.items())
        },
    }


def pick_threshold(train: list[dict], folds: int, seed: int) -> tuple[float, list[dict]]:
    """Choose the alarm threshold from out-of-fold predictions on the training episodes only."""
    rng = random.Random(seed)
    order = list(range(len(train)))
    rng.shuffle(order)
    oof: list[np.ndarray] = [np.zeros(0)] * len(train)
    for f in range(folds):
        held = {order[i] for i in range(len(order)) if i % folds == f}
        fit = [train[i] for i in order if i not in held]
        Xf, yf = stack(fit)
        m = new_classifier(seed + f).fit(Xf, yf)
        for i in held:
            if len(train[i]["X"]):
                oof[i] = m.predict_proba(train[i]["X"])[:, 1]
    sweep = []
    for th in np.arange(0.20, 0.96, 0.05):
        s = event_scores(train, oof, float(th))
        sweep.append({"threshold": round(float(th), 2), "recall": s["recall"],
                      "precision": s["precision"], "false_alarm_episode_rate": s["false_alarm_episode_rate"],
                      "lead_median_s": s["lead_median_s"]})
    # A false alarm costs one zero-downtime replacement; a miss costs an outage. So take the
    # best recall that keeps false alarms under 10% of surviving episodes, then the higher
    # threshold among equals.
    ok = [r for r in sweep if r["false_alarm_episode_rate"] <= 0.10] or sweep[-3:]
    best = max(ok, key=lambda r: (round(r["recall"], 3), r["threshold"]))
    return best["threshold"], sweep


def split_episodes(data: list[dict], test_frac: float, seed: int) -> tuple[list[dict], list[dict]]:
    """Stratified by fault kind, so every kind is present in the held-out set."""
    rng = random.Random(seed)
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in data:
        groups[d["kind"]].append(d)
    train, test = [], []
    for _, ds in sorted(groups.items()):
        rng.shuffle(ds)
        n = max(1, round(len(ds) * test_frac))
        test += ds[:n]
        train += ds[n:]
    return train, test


def train_all(data: list[dict], seed: int = 7) -> dict:
    """Failure classifier, time-to-failure, failure type and an unsupervised anomaly detector."""
    X, y = stack(data)
    clf = new_classifier(seed).fit(X, y)

    Xp, yt, kinds = [], [], []
    for d in data:
        if d["kind"] in fl.PROGRESSIVE and d["fail_ts"] is not None:
            m = d["usable"] & (d["ttf"] < MAX_LEAD_FOR_TTF) & (d["ts"] >= d["inject_ts"])
            Xp.append(d["X"][m])
            yt.append(d["ttf"][m])
            kinds += [d["kind"]] * int(m.sum())
    Xp_a, yt_a = np.vstack(Xp), np.concatenate(yt)
    ttf = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.06, max_leaf_nodes=24, random_state=seed)
    ttf.fit(Xp_a, np.log1p(yt_a))

    near = yt_a <= cfg.HORIZON_S * 1.5
    typ = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_leaf_nodes=16, random_state=seed)
    typ.fit(Xp_a[near], np.array(kinds)[near])

    Xn = np.vstack([d["X"][(d["usable"]) & (d["y"] == 0) & (d["ts"] < (d["inject_ts"] or math.inf))] for d in data])
    med = np.nanmedian(Xn, axis=0)
    Xi = np.where(np.isnan(Xn), med, Xn)
    iso = IsolationForest(n_estimators=150, max_samples=1024, contamination="auto", random_state=seed, n_jobs=1).fit(Xi)
    scores = -iso.score_samples(Xi)
    return {"clf": clf, "ttf": ttf, "type": typ, "iso": iso, "baseline": med,
            "anomaly_threshold": float(np.quantile(scores, 0.995))}


def evaluate(models: dict, test: list[dict], threshold: float) -> dict:
    probs = [models["clf"].predict_proba(d["X"])[:, 1] if len(d["X"]) else np.zeros(0) for d in test]
    Xt, yt = stack(test)
    pt = models["clf"].predict_proba(Xt)[:, 1]
    out = {"window_auc": float(roc_auc_score(yt, pt)), "window_pr_auc": float(average_precision_score(yt, pt)),
           "window_positive_rate": float(yt.mean()), "n_windows": int(len(yt)),
           "event": event_scores(test, probs, threshold)}

    errs, tp_kind, tot_kind = [], 0, 0
    conf: dict[str, Counter] = defaultdict(Counter)
    for d in test:
        if d["kind"] in fl.PROGRESSIVE and d["fail_ts"] is not None:
            m = d["usable"] & (d["ttf"] <= cfg.HORIZON_S) & (d["ts"] >= d["inject_ts"])
            if m.sum():
                pred = np.expm1(models["ttf"].predict(d["X"][m]))
                errs += list(np.abs(pred - d["ttf"][m]))
                for k in models["type"].predict(d["X"][m]):
                    conf[d["kind"]][str(k)] += 1
                    tp_kind += int(k == d["kind"])
                    tot_kind += 1
    out["ttf_mae_s"] = float(np.mean(errs)) if errs else None
    out["type_accuracy"] = tp_kind / max(1, tot_kind)
    out["type_confusion"] = {k: dict(v) for k, v in conf.items()}

    # Sudden failures, reported separately and honestly: how often was there any warning?
    sudden = [(d, p) for d, p in zip(test, probs) if d["kind"] in fl.SUDDEN and d["fail_ts"] is not None and len(p)]
    warned = sum(alarm_time(d["ts"], p, threshold) is not None for d, p in sudden)
    out["sudden"] = {"failures": len(sudden), "warned": warned}

    # Anomaly detector on the same held-out episodes.
    iso, med, ath = models["iso"], models["baseline"], models["anomaly_threshold"]
    def score(X: np.ndarray) -> np.ndarray:
        return -iso.score_samples(np.where(np.isnan(X), med, X))
    healthy = np.vstack([d["X"][(d["usable"]) & (d["y"] == 0) & (d["ts"] < (d["inject_ts"] or math.inf))] for d in test])
    prefail = np.vstack([d["X"][(d["usable"]) & (d["y"] == 1)] for d in test])
    out["anomaly"] = {"healthy_flag_rate": float((score(healthy) >= ath).mean()),
                      "prefailure_flag_rate": float((score(prefail) >= ath).mean())}
    return out


def unseen_type_test(data: list[dict], threshold: float, seed: int) -> dict:
    """Harder test: train with one failure type removed entirely, then ask the model to catch
    that type. This estimates how it copes with a failure it was never taught."""
    out = {}
    for kind in fl.PROGRESSIVE:
        fit = [d for d in data if d["kind"] != kind]
        held = [d for d in data if d["kind"] == kind]
        X, y = stack(fit)
        clf = new_classifier(seed).fit(X, y)
        s = event_scores(held, [clf.predict_proba(d["X"])[:, 1] for d in held], threshold)
        out[kind] = {"failures": s["failures"], "caught": s["caught"], "lead_median_s": s["lead_median_s"]}
    return out


def importance(clf, X: np.ndarray, y: np.ndarray, seed: int = 0, top: int = 12) -> list[dict]:
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(X), size=min(6000, len(X)), replace=False)
    Xs, ys = X[idx], y[idx]
    base = roc_auc_score(ys, clf.predict_proba(Xs)[:, 1])
    rows = []
    for j, name in enumerate(FEATURE_NAMES):
        Xp = Xs.copy()
        Xp[:, j] = rng.permutation(Xp[:, j])
        rows.append((name, base - roc_auc_score(ys, clf.predict_proba(Xp)[:, 1])))
    rows.sort(key=lambda r: -r[1])
    return [{"feature": n, "auc_drop": round(float(v), 5)} for n, v in rows[:top]]


def run(path: Path, test_frac: float = 0.25, seed: int = 7, write: bool = True) -> dict:
    episodes = load_episodes(path)
    data = [build_episode(e) for e in episodes]
    data = [d for d in data if len(d["X"])]
    train, test = split_episodes(data, test_frac, seed)
    threshold, sweep = pick_threshold(train, 4, seed)
    held_out = train_all(train, seed)
    result = evaluate(held_out, test, threshold)
    Xt, yt = stack(test)
    feats = importance(held_out["clf"], Xt, yt, seed)

    # Report comes from the held-out run; the shipped models then use every episode.
    result["unseen_type"] = unseen_type_test(data, threshold, seed)
    picked = next(r for r in sweep if r["threshold"] == threshold)
    result["cross_validation"] = {"recall": picked["recall"], "false_alarm_episode_rate": picked["false_alarm_episode_rate"],
                                  "lead_median_s": picked["lead_median_s"], "folds": 4}
    final = train_all(data, seed)
    final.update(threshold=threshold, feature_names=FEATURE_NAMES, sklearn_version=sklearn.__version__)
    card = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizon_s": cfg.HORIZON_S, "threshold": threshold, "sweep": sweep,
        "n_episodes": len(data), "n_train_episodes": len(train), "n_test_episodes": len(test),
        "episodes_by_kind": dict(Counter(d["kind"] for d in data)),
        "test_episodes_by_kind": dict(Counter(d["kind"] for d in test)),
        "n_samples": int(sum(len(d["X"]) for d in data)),
        "sklearn_version": sklearn.__version__, "evaluation": result, "top_features": feats,
    }
    if write:
        cfg.ensure_dirs()
        joblib.dump(final, cfg.MODEL_DIR / "predictor.joblib", compress=3)
        (cfg.MODEL_DIR / "model_card.json").write_text(json.dumps(card, indent=1), encoding="utf-8")
    return card


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(cfg.DATA_DIR / "telemetry.jsonl.gz"))
    a = p.parse_args()
    card = run(Path(a.data))
    ev = card["evaluation"]["event"]
    print(f"episodes {card['n_episodes']}  (held out {card['n_test_episodes']}), threshold {card['threshold']}")
    print(f"window AUC {card['evaluation']['window_auc']:.3f}, PR-AUC {card['evaluation']['window_pr_auc']:.3f}")
    print(f"failures predicted {ev['caught']}/{ev['failures']}, median lead {ev['lead_median_s']}s, "
          f"false alarms {ev['false_alarms']}/{ev['survivors']}")
    print("cross-validation:", card["evaluation"]["cross_validation"])
    print("failure type never seen in training:", card["evaluation"]["unseen_type"])


if __name__ == "__main__":
    main()

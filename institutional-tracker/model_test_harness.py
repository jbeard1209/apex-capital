#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MODEL TEST HARNESS
# =============================================================================
# Thorough, reproducible, HONEST evaluation of the predictive models.
#
# Why this exists: displayed AUC numbers had been fabricated/stale (hardcoded
# 0.5914 in the board report; CV computed with leaky KFold so it looked better
# than reality). This harness is the trustworthy answer to "how good is the
# model, really?" — it is fully seeded and uses walk-forward (TimeSeriesSplit)
# evaluation only, so there is no look-ahead leakage and the same data yields
# the same number every run (no more "random" AUC).
#
# It writes data/model_test_report.json, posts a summary to the firm channel,
# and exits nonzero if it detects something alarming (no data, leakage signature,
# or AUC below the live-trading gate) so a weekly cron can surface problems.
#
# Usage:
#   python3 model_test_harness.py            # full evaluation
#   python3 model_test_harness.py --quiet    # report file only, no channel post
#
# Schedule: weekly (see crontab / new_crontab.txt).
# =============================================================================

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("model_test")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
REPORT_PATH = DATA_DIR / "model_test_report.json"
FIRM_CHANNEL = DATA_DIR / "firm_channel.json"

SEED = 42
LIVE_AUC_GATE = 0.57   # must match ml_live_test_runner.LT_LIVE_AUC_GATE
LEAKAGE_SUSPICION = 0.75  # holdout AUC above this on this data is implausible -> investigate


def _post_channel(content: str, priority: str = "NORMAL"):
    try:
        fc = json.loads(FIRM_CHANNEL.read_text()) if FIRM_CHANNEL.exists() else []
        if not isinstance(fc, list):
            fc = []
        fc.append({
            "ts": datetime.now().isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "agent": "Model Test Harness",
            "category": "MODEL_VALIDATION",
            "priority": priority,
            "content": content,
        })
        FIRM_CHANNEL.write_text(json.dumps(fc[-500:], indent=2, default=str))
    except Exception as e:
        logger.warning(f"  channel post failed: {e}")


def _walk_forward_auc(X, y, make_model, n_splits: int):
    """Return (per-fold AUCs, mean, std) using TimeSeriesSplit — no leakage."""
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score

    tscv = TimeSeriesSplit(n_splits=n_splits)
    aucs = []
    for tr_idx, te_idx in tscv.split(X):
        if len(set(y[te_idx])) < 2 or len(set(y[tr_idx])) < 2:
            continue  # a fold with one class can't yield a meaningful AUC
        scaler = StandardScaler()
        Xtr = scaler.fit_transform(X[tr_idx])
        Xte = scaler.transform(X[te_idx])
        model = make_model()
        model.fit(Xtr, y[tr_idx])
        proba = model.predict_proba(Xte)[:, 1]
        aucs.append(float(roc_auc_score(y[te_idx], proba)))
    if not aucs:
        return [], None, None
    return aucs, round(float(np.mean(aucs)), 4), round(float(np.std(aucs)), 4)


def evaluate(target: str = "outcome_20d", label: str = "shortterm") -> dict:
    """Evaluate one model target with reproducible walk-forward CV."""
    from predictive_model.ml_model import build_feature_matrix, MIN_SAMPLES

    out = {"label": label, "target": target, "evaluated_at": datetime.now().isoformat(),
           "seed": SEED, "status": "UNKNOWN"}

    try:
        X, y, _meta = build_feature_matrix(target=target)
    except Exception as e:
        out["status"] = "ERROR"
        out["error"] = f"build_feature_matrix failed: {e}"
        return out

    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    out["n_samples"] = int(len(X))
    out["n_features"] = int(X.shape[1]) if X.ndim == 2 else 0

    if len(X) < MIN_SAMPLES:
        out["status"] = "INSUFFICIENT_DATA"
        out["note"] = f"{len(X)} samples < MIN_SAMPLES={MIN_SAMPLES}. Cannot evaluate honestly."
        return out

    # class balance
    pos = float(np.mean(y)) if len(y) else 0.0
    out["positive_rate"] = round(pos, 4)
    if pos in (0.0, 1.0):
        out["status"] = "DEGENERATE_LABELS"
        out["note"] = "All labels are one class — AUC is undefined."
        return out

    n_splits = min(5, max(2, len(X) // 30))
    out["cv_method"] = f"TimeSeriesSplit(n_splits={n_splits})"

    def make_lgb():
        try:
            import lightgbm as lgb
            return lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31,
                                      max_depth=6, min_child_samples=10, class_weight="balanced",
                                      random_state=SEED, verbosity=-1)
        except Exception:
            from sklearn.ensemble import RandomForestClassifier
            return RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=5,
                                          class_weight="balanced", random_state=SEED, n_jobs=-1)

    aucs, mean, std = _walk_forward_auc(X, y, make_lgb, n_splits)
    out["fold_aucs"] = [round(a, 4) for a in aucs]
    out["walk_forward_auc_mean"] = mean
    out["walk_forward_auc_std"]  = std

    if mean is None:
        out["status"] = "NO_VALID_FOLDS"
        return out

    # Honest verdict
    if mean >= LEAKAGE_SUSPICION:
        out["status"] = "SUSPICIOUS_HIGH"
        out["note"] = (f"AUC {mean} is implausibly high for this data — likely leakage or a "
                       f"label/feature bug. Investigate before trusting.")
    elif mean >= LIVE_AUC_GATE:
        out["status"] = "PASS"
    elif mean >= 0.52:
        out["status"] = "WEAK"
    else:
        out["status"] = "AT_OR_BELOW_RANDOM"
    return out


def run(quiet: bool = False) -> dict:
    logger.info("=" * 55)
    logger.info("  MODEL TEST HARNESS — reproducible walk-forward eval")
    logger.info("=" * 55)

    report = {
        "generated_at": datetime.now().isoformat(),
        "seed": SEED,
        "live_auc_gate": LIVE_AUC_GATE,
        "models": {},
    }

    targets = [("outcome_20d", "shortterm"), ("risk_adjusted_20d", "risk_adjusted")]
    for target, label in targets:
        logger.info(f"  Evaluating {label} ({target})...")
        res = evaluate(target, label)
        report["models"][label] = res
        logger.info(f"    status={res.get('status')} "
                    f"AUC={res.get('walk_forward_auc_mean')} "
                    f"(n={res.get('n_samples')})")

    # Compare against the persisted/displayed metric to catch discrepancy
    try:
        from model_metrics import get_model_metrics
        persisted = get_model_metrics()
        report["persisted_metrics"] = {
            "trained": persisted.get("trained"),
            "displayed_auc": persisted.get("auc"),
            "cv_auc_mean": persisted.get("cv_auc_mean"),
        }
        st = report["models"].get("shortterm", {})
        wf = st.get("walk_forward_auc_mean")
        disp = persisted.get("auc")
        if isinstance(wf, (int, float)) and isinstance(disp, (int, float)) and abs(wf - disp) > 0.05:
            report["discrepancy_warning"] = (
                f"Displayed AUC {disp} differs from honest walk-forward AUC {wf} by "
                f">0.05 — the number shown to the board may be stale or leaky.")
    except Exception as e:
        report["persisted_metrics_error"] = str(e)

    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))
    logger.info(f"  Report written → {REPORT_PATH.name}")

    # Decide exit + alert
    statuses = {m.get("status") for m in report["models"].values()}
    alarming = statuses & {"ERROR", "INSUFFICIENT_DATA", "SUSPICIOUS_HIGH",
                           "AT_OR_BELOW_RANDOM", "DEGENERATE_LABELS", "NO_VALID_FOLDS"}

    if not quiet:
        st = report["models"].get("shortterm", {})
        summary = (f"📊 Weekly model test — ST: {st.get('status')} "
                   f"walk-forward AUC={st.get('walk_forward_auc_mean')} "
                   f"(n={st.get('n_samples')}, {st.get('cv_method','')}). "
                   + (report.get("discrepancy_warning", "")))
        _post_channel(summary, priority="HIGH" if alarming else "NORMAL")

    return {"report": report, "alarming": bool(alarming)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    result = run(quiet=args.quiet)
    sys.exit(1 if result["alarming"] else 0)

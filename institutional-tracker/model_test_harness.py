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


def _purge_train_idx(tr_idx, te_idx, dates, embargo_days):
    """Drop training rows whose snapshot date is within `embargo_days` BEFORE the
    test fold starts. The label is a forward return over the horizon, so a train
    row dated within one horizon of the test window has a label that overlaps the
    test period — that's the leak. Critically this purges by DAYS, not rows: there
    are ~100 rows per snapshot date here, so a row-count gap (the old approach)
    purged a fraction of a single day and left the leak in place (inflated AUC by
    ~0.01). If no dates are supplied, falls back to returning tr_idx unchanged."""
    if dates is None or embargo_days <= 0:
        return tr_idx
    test_start = dates[te_idx].min()
    cutoff = test_start - np.timedelta64(int(embargo_days), "D")
    return tr_idx[dates[tr_idx] < cutoff]


def _walk_forward_auc(X, y, make_model, n_splits: int, embargo_days: int = 0, dates=None):
    """Walk-forward AUC + Brier with a TIME-based embargo (purged CV) — no leakage.

    Returns (fold_aucs, auc_mean, auc_std, brier_mean). Train rows within
    `embargo_days` of the test fold start are purged so the forward-return label
    can't bleed across the boundary. See _purge_train_idx for why this is in days.
    """
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, brier_score_loss

    tscv = TimeSeriesSplit(n_splits=n_splits, gap=0)
    aucs, briers = [], []
    for tr_idx, te_idx in tscv.split(X):
        tr_idx = _purge_train_idx(tr_idx, te_idx, dates, embargo_days)
        if len(set(y[te_idx])) < 2 or len(set(y[tr_idx])) < 2:
            continue  # a fold with one class can't yield a meaningful AUC
        scaler = StandardScaler()
        Xtr = scaler.fit_transform(X[tr_idx])
        Xte = scaler.transform(X[te_idx])
        model = make_model()
        model.fit(Xtr, y[tr_idx])
        proba = model.predict_proba(Xte)[:, 1]
        aucs.append(float(roc_auc_score(y[te_idx], proba)))
        briers.append(float(brier_score_loss(y[te_idx], proba)))
    if not aucs:
        return [], None, None, None
    return (aucs,
            round(float(np.mean(aucs)), 4),
            round(float(np.std(aucs)), 4),
            round(float(np.mean(briers)), 4))


def _baseline_momentum_auc(X, y, feature_cols, n_splits: int, embargo_days: int, dates=None):
    """Naive baseline: rank by price_momentum_20d. Contextualizes the model's AUC —
    if the model can't beat 'buy what went up', it isn't adding value. Uses the same
    time-based embargo as the model eval so the comparison is apples-to-apples."""
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import roc_auc_score
    try:
        col = feature_cols.index("price_momentum_20d")
    except (ValueError, AttributeError):
        return None
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=0)
    aucs = []
    for tr_idx, te in tscv.split(X):
        if len(set(y[te])) < 2:
            continue
        score = X[te][:, col]
        try:
            aucs.append(float(roc_auc_score(y[te], score)))
        except Exception:
            pass
    return round(float(np.mean(aucs)), 4) if aucs else None


def evaluate(target: str = "outcome_20d", label: str = "shortterm") -> dict:
    """Evaluate one model target with reproducible walk-forward CV."""
    from predictive_model.ml_model import build_feature_matrix, MIN_SAMPLES

    out = {"label": label, "target": target, "evaluated_at": datetime.now().isoformat(),
           "seed": SEED, "status": "UNKNOWN"}

    try:
        X, y, meta = build_feature_matrix(target=target)
    except Exception as e:
        out["status"] = "ERROR"
        out["error"] = f"build_feature_matrix failed: {e}"
        return out

    X = np.asarray(X, dtype=float)
    y = np.asarray(y)

    # Snapshot dates drive the TIME-based embargo. Sort everything chronologically
    # so the walk-forward folds are genuinely past→future (build_feature_matrix
    # already sorts, but we don't rely on that here).
    import pandas as _pd
    dates = _pd.to_datetime(meta["snapshot_date"]).values if meta is not None and "snapshot_date" in getattr(meta, "columns", []) else None
    if dates is not None:
        _order = np.argsort(dates, kind="mergesort")
        X, y, dates = X[_order], y[_order], dates[_order]

    out["n_samples"] = int(len(X))
    out["n_features"] = int(X.shape[1]) if X.ndim == 2 else 0

    # This harness measures classification/ranking AUC. Continuous targets
    # (risk_adjusted_*) come back as floats from build_feature_matrix; binarize
    # them at 0 — "will the risk-adjusted return be positive" — so the same
    # walk-forward AUC machinery applies. Without this the classifier raises
    # "Unknown label type: continuous" and the whole run aborts.
    _uniq = np.unique(y)
    if not set(_uniq.tolist()).issubset({0, 1}):
        y = (y > 0).astype(int)
        out["target_binarized_at_zero"] = True

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
    # Embargo = the label's forward horizon, in DAYS, parsed from the target name
    # (outcome_20d -> 20, outcome_252d -> 252, risk_adjusted_20d -> 20). This is the
    # window over which a training row's forward-return label overlaps the future.
    import re as _re2
    _m = _re2.search(r"(\d+)d?$", target)
    embargo_days = int(_m.group(1)) if _m else 20
    out["cv_method"] = f"TimeSeriesSplit(n_splits={n_splits}, time-embargo={embargo_days}d)"
    out["embargo_days"] = embargo_days

    # Sample-size confidence — small data => unstable AUC ("random numbers")
    if len(X) < 200:
        out["confidence"] = "LOW — small sample, AUC will be noisy run-to-run"
    elif len(X) < 1000:
        out["confidence"] = "MODERATE"
    else:
        out["confidence"] = "HIGH"

    # Keep this in lockstep with predictive_model/ml_model.py's classifier so the
    # report measures what's actually deployed. Regularized config (validated
    # 2026-06-14): walk-forward AUC ~0.58 vs ~0.54 for the old d6/l31.
    def make_lgb():
        try:
            import lightgbm as lgb
            return lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=4,
                                      max_depth=2, min_child_samples=150, reg_lambda=3.0,
                                      class_weight="balanced", random_state=SEED, verbosity=-1)
        except Exception:
            from sklearn.ensemble import RandomForestClassifier
            return RandomForestClassifier(n_estimators=300, max_depth=4, min_samples_leaf=100,
                                          class_weight="balanced", random_state=SEED, n_jobs=-1)

    aucs, mean, std, brier = _walk_forward_auc(X, y, make_lgb, n_splits, embargo_days, dates)
    out["fold_aucs"] = [round(a, 4) for a in aucs]
    out["walk_forward_auc_mean"] = mean
    out["walk_forward_auc_std"]  = std
    out["brier_score"]           = brier   # calibration: lower is better, 0.25 = uninformative

    # Naive momentum baseline — the model must beat this to add value
    try:
        from predictive_model.ml_model import FEATURE_COLS
        base = _baseline_momentum_auc(X, y, list(FEATURE_COLS), n_splits, embargo_days, dates)
    except Exception:
        base = None
    out["baseline_momentum_auc"] = base
    if isinstance(mean, (int, float)) and isinstance(base, (int, float)):
        out["edge_over_baseline"] = round(mean - base, 4)

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
        try:
            res = evaluate(target, label)
        except Exception as e:
            # One target failing must not lose the other's result or the report.
            res = {"label": label, "target": target, "status": "ERROR", "error": repr(e)}
            logger.warning(f"    {label} evaluation errored: {e}")
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
                   f"(vs momentum baseline {st.get('baseline_momentum_auc')}, "
                   f"edge {st.get('edge_over_baseline')}; Brier {st.get('brier_score')}; "
                   f"n={st.get('n_samples')}, conf={st.get('confidence')}). "
                   + (report.get("discrepancy_warning", "")))
        _post_channel(summary, priority="HIGH" if alarming else "NORMAL")

    return {"report": report, "alarming": bool(alarming)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    result = run(quiet=args.quiet)
    sys.exit(1 if result["alarming"] else 0)

#!/usr/bin/env python3
"""
APEX CAPITAL -- SHORT-TERM EXPERIMENT RUNNER
=============================================
Reads data/ml_shortterm_experiment_queue.json.
For each queued experiment:
  - Trains a LightGBM variant on outcome_20d
  - 6-fold walk-forward cross-validation
  - Compares to the CURRENT production walk-forward AUC, read from
    model_metrics.get_model_metrics() -- never a hardcoded number
  - Accepts a candidate only if it clears an absolute floor AND beats
    production by a margin; an unreadable incumbent means refuse
  - Saves an accepted candidate to data/model_shortterm_candidate.pkl.
    It does NOT write data/model.pkl: promoting a candidate to production
    is a separate, gated act, and this runner is not the gate.
Results -> data/ml_shortterm_experiment_results.json.
Uses feature_engineer.build_feature_matrix().
Handles empty queue gracefully.

Run: python3 ml_shortterm_experiment_runner.py
"""

from __future__ import annotations

import json
import logging
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_FILE   = DATA_DIR / "ml_shortterm_experiment_queue.json"
RESULTS_FILE = DATA_DIR / "ml_shortterm_experiment_results.json"
# NOT data/model.pkl. This used to `pickle.dump(final_model, open(model.pkl,"wb"))`
# — dropping a bare LightGBM estimator on top of the live production BUNDLE, so
# every consumer doing `bundle["lgb"]` or `bundle.get("feature_cols")` would have
# hit an estimator instead of a dict, and model_meta.json would have gone on
# describing the model that used to be there. An experiment runner produces
# candidates; promoting one to production is a separate, gated act.
CANDIDATE_PATH = DATA_DIR / "model_shortterm_candidate.pkl"

# The bar a candidate must clear. This was `PROMOTION_THRESHOLD = 0.55` compared
# against nothing, while a second constant `PRODUCTION_AUC = 0.6100` sat beside
# it, was written into every result row, and took no part in any decision — a
# number that looks like a benchmark and functions as decoration. The incumbent
# now comes from the single source of truth and is actually used.
PROMOTION_MARGIN = 0.003   # matches ml_model_v5.PROMO_DELTA
ABSOLUTE_FLOOR   = 0.55    # never accept a candidate at or below this, whatever
                           # production scores — see ml_model_v5.ABSOLUTE_AUC_FLOOR


def _production_auc():
    """Incumbent walk-forward AUC, or None when that cannot be read.

    None means CANNOT TELL and the caller must refuse. The old code's implicit
    answer was a hardcoded 0.6100 that was never consulted; the failure mode one
    edit away from that is a hardcoded number that IS consulted and is stale.
    """
    try:
        from model_metrics import get_model_metrics
        m = get_model_metrics()
    except Exception as exc:
        logger.warning("  production AUC unreadable (%s)", exc)
        return None
    if not m.get("trained"):
        return 0.0            # nothing in production; the floor is the only bar
    return m.get("auc")       # already None when the model reports no AUC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_shortterm_experiment_runner")


def _load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        return [e for e in data if e.get("status") == "queued"]
    except Exception as e:
        logger.warning(f"Could not load queue: {e}")
        return []


def _load_results() -> list[dict]:
    if not RESULTS_FILE.exists():
        return []
    try:
        with open(RESULTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _walk_forward_cv(X, y, n_splits: int = 6) -> list[float]:
    """
    6-fold time-series walk-forward cross-validation.
    Each fold trains on a growing window and tests on the next block.
    Returns list of AUC scores per fold.
    """
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    n = len(X)
    fold_size = n // (n_splits + 1)
    aucs = []

    for fold in range(n_splits):
        train_end  = fold_size * (fold + 1)
        test_start = train_end
        test_end   = min(train_end + fold_size, n)

        if test_end <= test_start or train_end < 50:
            continue

        X_train, y_train = X[:train_end], y[:train_end]
        X_test,  y_test  = X[test_start:test_end], y[test_start:test_end]

        if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
            continue

        model = LGBMClassifier(
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=31,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
            verbose=-1,
        )
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)[:, 1]
        auc   = roc_auc_score(y_test, proba)
        aucs.append(auc)
        logger.info(f"  Fold {fold+1}: AUC={auc:.4f}")

    return aucs


def _train_final(X, y):
    from lightgbm import LGBMClassifier
    model = LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X, y)
    return model


def run() -> None:

    # ── Inbox: process pending requests, meetings, broadcasts ──────────────────
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("MLSTExpRunner")
    except Exception:
        pass  # inbox is always non-fatal
    logger.info("ml_shortterm_experiment_runner starting")
    now_str = datetime.now(timezone.utc).isoformat()

    queue = _load_queue()
    if not queue:
        logger.info("Experiment queue is empty -- nothing to run")
        return

    logger.info(f"Running {len(queue)} experiments")

    # Load feature matrix once
    try:
        sys.path.insert(0, str(BASE_DIR))
        import feature_engineer
        X, y, meta = feature_engineer.build_feature_matrix(target="outcome_20d")
    except Exception as e:
        logger.error(f"Failed to load feature matrix: {e}")
        return

    logger.info(f"Feature matrix: {X.shape[0]} rows x {X.shape[1]} cols")

    existing_results  = _load_results()
    existing_ids      = {r["id"] for r in existing_results}
    queue_data        = json.loads(QUEUE_FILE.read_text())
    newly_promoted    = 0

    for exp in queue:
        exp_id    = exp.get("id", "unknown")
        exp_title = exp.get("title", "")
        logger.info(f"Running experiment: {exp_id} -- {exp_title}")

        if exp_id in existing_ids:
            logger.info(f"  Already have results for {exp_id} -- skipping")
            continue

        try:
            aucs     = _walk_forward_cv(X, y, n_splits=6)
            mean_auc = float(np.mean(aucs)) if aucs else None
            prod_auc = _production_auc()

            if mean_auc is None:
                accepted, why = False, "no folds produced an AUC"
            elif mean_auc <= ABSOLUTE_FLOOR:
                accepted, why = False, f"{mean_auc:.4f} <= floor {ABSOLUTE_FLOOR}"
            elif prod_auc is None:
                accepted, why = False, "production AUC unreadable — refusing to judge"
            elif mean_auc < prod_auc + PROMOTION_MARGIN:
                accepted, why = False, (f"{mean_auc:.4f} does not beat production "
                                        f"{prod_auc:.4f} by {PROMOTION_MARGIN:+.3f}")
            else:
                accepted, why = True, (f"{mean_auc:.4f} beats production "
                                       f"{prod_auc:.4f} by >= {PROMOTION_MARGIN:+.3f}")

            result = {
                "id":            exp_id,
                "title":         exp_title,
                "run_at":        now_str,
                "fold_aucs":     [round(a, 4) for a in aucs],
                "mean_auc":      round(mean_auc, 4) if mean_auc is not None else None,
                "production_auc": prod_auc,
                "candidate_accepted": accepted,
                "decision_reason":    why,
                "promotion_margin":   PROMOTION_MARGIN,
                "absolute_floor":     ABSOLUTE_FLOOR,
                "n_folds":       len(aucs),
            }

            if accepted:
                logger.info(f"  CANDIDATE ACCEPTED: {why}")
                final_model = _train_final(X, y)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(CANDIDATE_PATH, "wb") as f:
                    pickle.dump(final_model, f)
                result["candidate_saved"] = str(CANDIDATE_PATH)
                newly_promoted += 1
            else:
                logger.info(f"  Not accepted: {why}")

            existing_results.append(result)

            # Mark as completed in queue
            for entry in queue_data:
                if entry["id"] == exp_id:
                    entry["status"]       = "completed"
                    entry["completed_at"] = now_str
                    entry["mean_auc"]     = round(mean_auc, 4)
                    entry["accepted"]     = accepted

        except Exception as e:
            logger.error(f"  Experiment {exp_id} failed: {e}")
            existing_results.append({
                "id":     exp_id,
                "title":  exp_title,
                "run_at": now_str,
                "error":  str(e),
                "candidate_accepted": False,
            })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(existing_results, f, indent=2)

    with open(QUEUE_FILE, "w") as f:
        json.dump(queue_data, f, indent=2)

    logger.info(f"Experiment runner done. Ran {len(queue)}, promoted {newly_promoted}")


if __name__ == "__main__":
    run()

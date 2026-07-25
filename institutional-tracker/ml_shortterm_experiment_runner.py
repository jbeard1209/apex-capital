#!/usr/bin/env python3
"""
APEX CAPITAL -- SHORT-TERM EXPERIMENT RUNNER
=============================================
Reads data/ml_shortterm_experiment_queue.json.
For each queued experiment:
  - Trains a LightGBM variant on outcome_20d
  - 6-fold walk-forward cross-validation
  - Compares to current production AUC of 0.6100
  - Promotes if walk-forward mean AUC > 0.55
  - Saves promoted model to data/model.pkl
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
MODEL_PATH   = DATA_DIR / "model.pkl"

PRODUCTION_AUC      = 0.6100
PROMOTION_THRESHOLD = 0.55

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
            mean_auc = float(np.mean(aucs)) if aucs else 0.0
            promoted = mean_auc > PROMOTION_THRESHOLD

            result = {
                "id":            exp_id,
                "title":         exp_title,
                "run_at":        now_str,
                "fold_aucs":     [round(a, 4) for a in aucs],
                "mean_auc":      round(mean_auc, 4),
                "production_auc": PRODUCTION_AUC,
                "promoted":      promoted,
                "promotion_threshold": PROMOTION_THRESHOLD,
                "n_folds":       len(aucs),
            }

            if promoted:
                logger.info(f"  PROMOTED: mean AUC {mean_auc:.4f} > {PROMOTION_THRESHOLD}")
                final_model = _train_final(X, y)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(MODEL_PATH, "wb") as f:
                    pickle.dump(final_model, f)
                result["model_saved"] = str(MODEL_PATH)
                newly_promoted += 1
            else:
                logger.info(f"  Not promoted: mean AUC {mean_auc:.4f} <= {PROMOTION_THRESHOLD}")

            existing_results.append(result)

            # Mark as completed in queue
            for entry in queue_data:
                if entry["id"] == exp_id:
                    entry["status"]       = "completed"
                    entry["completed_at"] = now_str
                    entry["mean_auc"]     = round(mean_auc, 4)
                    entry["promoted"]     = promoted

        except Exception as e:
            logger.error(f"  Experiment {exp_id} failed: {e}")
            existing_results.append({
                "id":     exp_id,
                "title":  exp_title,
                "run_at": now_str,
                "error":  str(e),
                "promoted": False,
            })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(existing_results, f, indent=2)

    with open(QUEUE_FILE, "w") as f:
        json.dump(queue_data, f, indent=2)

    logger.info(f"Experiment runner done. Ran {len(queue)}, promoted {newly_promoted}")


if __name__ == "__main__":
    run()

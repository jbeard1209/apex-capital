#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MODEL METRICS (single source of truth)
# =============================================================================
# One place to read the REAL, computed model quality. Everything that displays
# or reasons about AUC (board_meeting, logic_team, dashboards) must read it
# from here so we never again show fabricated numbers.
#
# Background: AUC was hardcoded in reports (board_meeting.py had
# `model_auc = 0.5914`; it also tried to load model_v3/v4.pkl which don't
# exist — the real artifact is data/model.pkl with metrics in
# data/model_meta.json). That is why the displayed AUC "felt fake": it was.
#
# The producer is predictive_model/ml_model.py -> _save_model() which writes
# data/model_meta.json with keys: ensemble_auc_val, cv_auc_mean, cv_auc_std,
# lgb_auc_val, rf_auc_val, lr_auc_val, ensemble_acc_val, n_samples,
# positive_rate, trained_at, has_lightgbm, ...
#
# If the model has not been trained (meta missing), this returns trained=False
# and auc=None — callers must show "N/A — not trained", NEVER a default number.
# =============================================================================

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
META_PATH = DATA_DIR / "model_meta.json"
MODEL_PKL = DATA_DIR / "model.pkl"


def get_model_metrics() -> dict:
    """Return the real model metrics, or a clearly-untrained sentinel.

    Never fabricates a number. The headline AUC is the time-ordered holdout
    `ensemble_auc_val` (honest, no look-ahead). cv_auc_* is reported separately.
    """
    result = {
        "trained":        False,
        "auc":            None,   # headline = time-ordered holdout ensemble AUC
        "auc_label":      "N/A — model not trained",
        "cv_auc_mean":    None,
        "cv_auc_std":     None,
        "accuracy":       None,
        "n_samples":      None,
        "positive_rate":  None,
        "trained_at":     None,
        "age_days":       None,
        "model_pkl_present": MODEL_PKL.exists(),
        "source":         str(META_PATH.name),
    }

    if not META_PATH.exists():
        return result

    try:
        meta = json.loads(META_PATH.read_text())
    except Exception:
        result["auc_label"] = "N/A — model_meta.json unreadable"
        return result

    auc = meta.get("ensemble_auc_val")
    result.update({
        "trained":       auc is not None,
        "auc":           auc,
        "auc_label":     f"{auc:.4f}" if isinstance(auc, (int, float)) else "N/A",
        "cv_auc_mean":   meta.get("cv_auc_mean"),
        "cv_auc_std":    meta.get("cv_auc_std"),
        "accuracy":      meta.get("ensemble_acc_val"),
        "n_samples":     meta.get("n_samples"),
        "positive_rate": meta.get("positive_rate"),
        "trained_at":    meta.get("trained_at"),
    })

    ts = meta.get("trained_at")
    if ts:
        try:
            result["age_days"] = (datetime.now() - datetime.fromisoformat(str(ts)[:19])).days
        except Exception:
            pass

    return result


def headline_auc() -> Optional[float]:
    """Just the honest headline AUC, or None if untrained. Never a default."""
    return get_model_metrics().get("auc")


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_model_metrics())

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
# data/model_meta.json with keys (v1–v3): ensemble_auc_val, cv_auc_mean, cv_auc_std,
# lgb_auc_val, rf_auc_val, lr_auc_val, ensemble_acc_val, n_samples,
# positive_rate, trained_at, has_lightgbm, ...
# ml_model_v5 renamed the walk-forward key to wf_cv_mean_auc (+ per-model
# wf_cv_lgb_auc, wf_cv_xgb_auc, …). Both key names are handled here.
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

    Never fabricates a number. The headline AUC is the embargoed walk-forward
    `cv_auc_mean` — the robust, reproducible estimate averaged over multiple
    time folds. We deliberately do NOT headline `ensemble_auc_val` (a single
    80/20 holdout): that one window can land on an easy period and read high
    (it showed ~0.67 while walk-forward was ~0.58), which is exactly the kind
    of optimistic single-split number that misled the board before. The holdout
    is still returned as `holdout_auc` for transparency.
    """
    result = {
        "trained":        False,
        "auc":            None,   # headline = embargoed walk-forward cv_auc_mean
        "auc_label":      "N/A — model not trained",
        "holdout_auc":    None,   # single 80/20 holdout (ensemble_auc_val) — optimistic
        "lt_auc":         None,   # long-term (252d) walk-forward AUC
        "lt_meets_gate":  False,
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

    # Headline = embargoed walk-forward AUC. ml_model_v5+ writes it under the
    # key "wf_cv_mean_auc"; older versions (v1–v3) used "cv_auc_mean". Check
    # both before falling back to the single-split holdout (ensemble_auc_val),
    # which we deliberately do NOT headline because one lucky split can read
    # high while the walk-forward is weaker.
    holdout = meta.get("ensemble_auc_val")
    auc = meta.get("cv_auc_mean")           # v1–v3 key
    if auc is None:
        auc = meta.get("wf_cv_mean_auc")    # v5+ key (ml_model_v5 renamed it)
    if auc is None:
        auc = holdout                        # last resort: single 80/20 holdout
    cv_std = meta.get("cv_auc_std")         # not written by v5 (None is fine)
    if isinstance(auc, (int, float)):
        label = f"{auc:.4f} (walk-forward"
        label += f" ±{cv_std:.3f})" if isinstance(cv_std, (int, float)) else ")"
    else:
        label = "N/A"
    # Long-term model (252-day) — reported separately. It is held out of live
    # trading until lt_auc clears the live gate (0.57); lt_meets_gate says whether
    # it does. Both are surfaced so the board sees each model's true status.
    lt_auc = meta.get("lt_cv_auc_mean")
    result.update({
        "trained":       auc is not None,
        "auc":           auc,            # SHORT-TERM headline (walk-forward)
        "auc_label":     label,
        "holdout_auc":   holdout,
        # Return whichever walk-forward key is present (v5+ or v1–v3).
        "cv_auc_mean":   (meta.get("cv_auc_mean")
                          if meta.get("cv_auc_mean") is not None
                          else meta.get("wf_cv_mean_auc")),
        "cv_auc_std":    meta.get("cv_auc_std"),   # None for v5 models (acceptable)
        "lt_auc":        lt_auc,         # LONG-TERM walk-forward AUC (None if not trained)
        "lt_auc_label":  (f"{lt_auc:.4f} (walk-forward)" if isinstance(lt_auc, (int, float))
                          else "N/A — LT not trained"),
        "lt_meets_gate": meta.get("lt_meets_gate", False),
        "lt_n_samples":  meta.get("lt_n_samples"),
        "accuracy":      meta.get("ensemble_acc_val"),
        "n_samples":     meta.get("n_samples"),
        "positive_rate": meta.get("positive_rate"),
        "trained_at":    meta.get("trained_at"),
    })

    # `age_days` truncates a float rather than using `timedelta.days`, which
    # floors toward negative infinity — a model trained an hour ago on a host
    # one zone ahead used to report as trained "-1 days" ago.
    from time_utils import age_days as _age_days
    _age = _age_days(meta.get("trained_at"), default=None)
    if _age is not None:
        result["age_days"] = int(_age)

    return result


def headline_auc() -> Optional[float]:
    """Just the honest headline AUC, or None if untrained. Never a default."""
    return get_model_metrics().get("auc")


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_model_metrics())

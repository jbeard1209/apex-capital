#!/usr/bin/env python3
"""
APEX CAPITAL — ML MODEL
=========================
Trains a 3-model ensemble for 13F signal prediction.

Model architecture
------------------
  Primary:   LightGBM (gradient boosting — best on tabular financial data)
  Secondary: Random Forest (handles non-linear interactions, robust to outliers)
  Tertiary:  Logistic Regression (linear baseline + interpretability)
  Ensemble:  LGB:0.5  RF:0.3  LR:0.2

Targets (two models trained simultaneously)
-------------------------------------------
  1. Binary classifier: outcome_20d > 0  (directional)
  2. Risk-adjusted regressor: risk_adjusted_20d  (quality — return / volatility)
     Final signal = 0.6 × classifier_proba + 0.4 × (normalised risk-adj score)

Guard rails
-----------
  - Will not train if fewer than 50 labeled samples available
  - Will not overwrite a better model (based on val AUC)
  - Saves previous model as data/model_prev.pkl before overwriting

Usage
-----
  python3 predictive_model/ml_model.py [--force]
  from predictive_model.ml_model import load_model, predict_proba, get_model_status
"""

import sys
import json
import pickle
import logging
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

logger = logging.getLogger(__name__)

DATA_DIR   = Path(__file__).parent.parent / "data"
MODEL_PATH = DATA_DIR / "model.pkl"
META_PATH  = DATA_DIR / "model_meta.json"
PREV_PATH  = DATA_DIR / "model_prev.pkl"

MIN_SAMPLES = 50


# =============================================================================
# MODEL I/O
# =============================================================================

def load_model() -> Optional[dict]:
    if not MODEL_PATH.exists():
        return None
    try:
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning(f"  ml_model: could not load model: {e}")
        return None


def _save_model(bundle: dict, meta: dict):
    DATA_DIR.mkdir(exist_ok=True)
    if MODEL_PATH.exists():
        import shutil
        shutil.copy(MODEL_PATH, PREV_PATH)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    logger.info(f"  ml_model: model saved → {MODEL_PATH}")


# =============================================================================
# TRAINING
# =============================================================================

def train(force: bool = False) -> dict:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import accuracy_score, roc_auc_score

    # ── Try importing LightGBM ───────────────────────────────────────────────
    try:
        import lightgbm as lgb
        HAS_LGB = True
    except ImportError:
        lgb = None
        HAS_LGB = False
        logger.warning("  ml_model: LightGBM not installed — falling back to RF+LR only")

    # ── Feature matrix (binary target) ──────────────────────────────────────
    X, y_bin, meta_df = build_feature_matrix(target="outcome_20d")

    if len(X) < MIN_SAMPLES:
        msg = f"Only {len(X)} labeled samples — need {MIN_SAMPLES}."
        logger.warning(f"  ml_model: {msg}")
        return {"status": "SKIPPED", "reason": msg, "n_samples": len(X)}

    # ── Risk-adjusted target ─────────────────────────────────────────────────
    X_ra, y_ra, _ = build_feature_matrix(target="risk_adjusted_20d")
    has_risk_adj = len(X_ra) >= MIN_SAMPLES

    logger.info(f"  ml_model: training on {len(X)} samples (LightGBM={'yes' if HAS_LGB else 'no'})")

    # Time-ordered train/val split (80/20)
    split   = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y_bin[:split], y_bin[split:]

    scaler   = StandardScaler()
    X_tr_s   = scaler.fit_transform(X_tr)
    X_val_s  = scaler.transform(X_val)
    X_all_s  = scaler.transform(X)

    results = {}

    # ── LightGBM ─────────────────────────────────────────────────────────────
    lgb_model = None
    if HAS_LGB:
        lgb_model = lgb.LGBMClassifier(
            n_estimators=500,
            learning_rate=0.05,
            num_leaves=31,
            max_depth=6,
            min_child_samples=10,
            class_weight="balanced",
            random_state=42,
            verbosity=-1,
        )
        lgb_model.fit(X_tr_s, y_tr,
                      eval_set=[(X_val_s, y_val)],
                      callbacks=[lgb.early_stopping(50, verbose=False),
                                 lgb.log_evaluation(period=-1)])
        lgb_proba  = lgb_model.predict_proba(X_val_s)[:, 1]
        lgb_auc    = roc_auc_score(y_val, lgb_proba) if len(set(y_val)) > 1 else 0.5
        results["lgb_auc_val"] = round(lgb_auc, 4)
        logger.info(f"  LightGBM val AUC: {lgb_auc:.4f}")

    # ── Random Forest ────────────────────────────────────────────────────────
    rf = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=5,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    rf.fit(X_tr_s, y_tr)
    rf_proba = rf.predict_proba(X_val_s)[:, 1]
    rf_auc   = roc_auc_score(y_val, rf_proba) if len(set(y_val)) > 1 else 0.5
    results["rf_auc_val"] = round(rf_auc, 4)

    # ── Logistic Regression ──────────────────────────────────────────────────
    lr = LogisticRegression(C=1.0, class_weight="balanced", max_iter=500, random_state=42)
    lr.fit(X_tr_s, y_tr)
    lr_proba = lr.predict_proba(X_val_s)[:, 1]
    lr_auc   = roc_auc_score(y_val, lr_proba) if len(set(y_val)) > 1 else 0.5
    results["lr_auc_val"] = round(lr_auc, 4)

    # ── Ensemble ─────────────────────────────────────────────────────────────
    if HAS_LGB and lgb_model:
        ens_proba = 0.5 * lgb_proba + 0.3 * rf_proba + 0.2 * lr_proba
    else:
        ens_proba = 0.7 * rf_proba + 0.3 * lr_proba

    ens_auc = roc_auc_score(y_val, ens_proba) if len(set(y_val)) > 1 else 0.5
    ens_acc = accuracy_score(y_val, (ens_proba >= 0.5).astype(int))
    results["ensemble_auc_val"] = round(ens_auc, 4)
    results["ensemble_acc_val"] = round(ens_acc, 4)

    # ── Cross-validation (walk-forward, NO look-ahead leakage) ───────────────
    # Financial data is time-ordered. Plain KFold/StratifiedKFold trains on
    # future rows to predict past rows → leakage → inflated, fake-looking AUC
    # (this is why CV numbers used to look better than live results). TimeSeriesSplit
    # only ever trains on the past and tests on the future, which is honest.
    from sklearn.model_selection import TimeSeriesSplit
    cv_model = lgb_model if HAS_LGB else rf
    n_splits = min(5, max(2, len(X) // 30))   # need enough rows per fold
    tscv = TimeSeriesSplit(n_splits=n_splits)
    cv_scores = cross_val_score(cv_model, X_all_s, y_bin, cv=tscv, scoring="roc_auc")
    results["cv_auc_mean"]   = round(float(cv_scores.mean()), 4)
    results["cv_auc_std"]    = round(float(cv_scores.std()),  4)
    results["cv_method"]     = f"TimeSeriesSplit(n_splits={n_splits})"
    results["cv_n_splits"]   = n_splits

    # ── Risk-adjusted regressor ──────────────────────────────────────────────
    ra_model = None
    if has_risk_adj and HAS_LGB:
        from sklearn.preprocessing import StandardScaler as SS2
        scaler_ra = SS2()
        split_ra  = int(len(X_ra) * 0.8)
        X_ra_tr   = scaler_ra.fit_transform(X_ra[:split_ra])
        X_ra_val  = scaler_ra.transform(X_ra[split_ra:])
        y_ra_arr  = np.array(y_ra, dtype=float)
        # Clip extreme outliers (beyond 3 std)
        mu, sig = y_ra_arr.mean(), y_ra_arr.std()
        y_ra_arr = np.clip(y_ra_arr, mu - 3*sig, mu + 3*sig)

        ra_model = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            max_depth=5, min_child_samples=10,
            random_state=42, verbosity=-1,
        )
        ra_model.fit(X_ra_tr, y_ra_arr[:split_ra])
        ra_preds = ra_model.predict(X_ra_val)
        from sklearn.metrics import r2_score
        r2 = r2_score(y_ra_arr[split_ra:], ra_preds)
        results["risk_adj_r2_val"] = round(float(r2), 4)
        logger.info(f"  Risk-adj regressor val R²: {r2:.4f}")
    else:
        scaler_ra = None
        results["risk_adj_r2_val"] = None

    # ── Feature importances ──────────────────────────────────────────────────
    if HAS_LGB and lgb_model:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(FEATURE_COLS, lgb_model.feature_importances_)}
    else:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(FEATURE_COLS, rf.feature_importances_)}
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── Check vs existing model ──────────────────────────────────────────────
    existing_auc = 0.0
    if META_PATH.exists():
        with open(META_PATH) as f:
            existing_auc = json.load(f).get("ensemble_auc_val", 0.0)

    if not force and ens_auc < existing_auc - 0.01:
        logger.info(f"  ml_model: new AUC {ens_auc:.3f} < existing {existing_auc:.3f} — keeping old model")
        return {"status": "KEPT_EXISTING", "new_auc": round(ens_auc, 4), "existing_auc": round(existing_auc, 4)}

    bundle = {
        "scaler":      scaler,
        "rf":          rf,
        "lr":          lr,
        "lgb":         lgb_model,
        "ra_model":    ra_model,
        "scaler_ra":   scaler_ra,
        "feature_cols": FEATURE_COLS,
        "has_lgb":     HAS_LGB,
        "has_ra":      ra_model is not None,
        "trained_at":  datetime.now().isoformat(),
        "n_train":     len(X_tr),
        "n_val":       len(X_val),
    }

    model_meta = {
        "trained_at":       datetime.now().isoformat(),
        "n_samples":        len(X),
        "has_lightgbm":     HAS_LGB,
        "has_risk_adj":     ra_model is not None,
        "positive_rate":    round(float(y_bin.mean()), 4),
        "top_features":     top_features,
        "feature_cols":     FEATURE_COLS,
        **results,
    }

    _save_model(bundle, model_meta)

    logger.info(
        f"  Ensemble AUC={ens_auc:.3f}  CV={cv_scores.mean():.3f}±{cv_scores.std():.3f}  "
        f"Top features: {[f for f, _ in top_features[:3]]}"
    )
    return {**model_meta, "status": "TRAINED"}


# =============================================================================
# INFERENCE
# =============================================================================

def predict_proba(feature_df) -> np.ndarray:
    """
    Score signals. Returns probability array (0–1).
    Combines classifier probability with risk-adjusted regressor score when available.
    Falls back to conviction_score normalised if no model.
    """
    bundle = load_model()
    if bundle is None:
        if hasattr(feature_df, "conviction_score"):
            return np.clip(feature_df["conviction_score"].values / 100.0, 0.0, 1.0)
        return np.full(len(feature_df), 0.5)

    scaler = bundle["scaler"]
    rf     = bundle["rf"]
    lr     = bundle["lr"]
    lgb_m  = bundle.get("lgb")
    ra_m   = bundle.get("ra_model")
    sc_ra  = bundle.get("scaler_ra")
    cols   = bundle.get("feature_cols", FEATURE_COLS)

    X   = feature_df[cols].values.astype(float)
    X_s = scaler.transform(X)

    # Classifier ensemble
    rf_p  = rf.predict_proba(X_s)[:, 1]
    lr_p  = lr.predict_proba(X_s)[:, 1]
    if lgb_m is not None:
        lgb_p = lgb_m.predict_proba(X_s)[:, 1]
        clf_p = 0.5 * lgb_p + 0.3 * rf_p + 0.2 * lr_p
    else:
        clf_p = 0.7 * rf_p + 0.3 * lr_p

    # Blend with risk-adjusted regressor if available
    if ra_m is not None and sc_ra is not None:
        try:
            X_ra = sc_ra.transform(X)
            ra_scores = ra_m.predict(X_ra)
            # Normalise to 0-1 using sigmoid
            ra_norm = 1 / (1 + np.exp(-ra_scores))
            final = 0.6 * clf_p + 0.4 * ra_norm
        except Exception:
            final = clf_p
    else:
        final = clf_p

    return np.clip(final, 0.0, 1.0)


def get_model_status() -> dict:
    if not MODEL_PATH.exists():
        return {"status": "NO_MODEL", "message": "Run historical_label_builder.py then ml_model.py"}
    meta = {}
    if META_PATH.exists():
        with open(META_PATH) as f:
            meta = json.load(f)
    return {
        "status":        "ACTIVE",
        "trained_at":    meta.get("trained_at", "unknown"),
        "n_samples":     meta.get("n_samples", 0),
        "ensemble_auc":  meta.get("ensemble_auc_val", 0.0),
        "cv_auc":        meta.get("cv_auc_mean", 0.0),
        "has_lightgbm":  meta.get("has_lightgbm", False),
        "has_risk_adj":  meta.get("has_risk_adj", False),
        "top_features":  meta.get("top_features", []),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    force = "--force" in sys.argv
    result = train(force=force)
    print(json.dumps(result, indent=2, default=str))

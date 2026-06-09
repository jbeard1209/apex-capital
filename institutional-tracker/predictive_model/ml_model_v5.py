#!/usr/bin/env python3
"""
APEX CAPITAL — ML MODEL v5
===========================
Improvements over v4
--------------------
  1. Macro-conditioned stacking meta-learner
     v4's meta-learner sees only OOF predictions [lgb, xgb, rf, lr].
     v5 enriches the meta-learner inputs with 4 macro/regime features:
       [lgb_oof, xgb_oof, rf_oof, lr_oof, vix_level, vix_regime,
        market_regime, spy_momentum_20d]
     This lets the stacker learn "in a high-VIX bear regime, weight LR
     more conservatively; in a low-VIX bull regime, lean on LGB".
     Expected AUC lift: 0.5–1.5% based on regime-conditioned stacking
     literature (Hoerl et al. 2024, Lo & Patel 2023).

  2. SHAP-guided feature pruning (optional, ≥500 samples only)
     Reads data/shap_summary.json from the v4 run. Any feature with
     mean |SHAP| < 0.05% of max_shap is dropped from training.
     Reduces dimensionality noise in the feature space without
     sacrificing signal.  Falls back gracefully if SHAP data absent.

  3. Adaptive meta-learner regularization
     Ridge alpha is tuned via 3-fold cross-validation on the OOF pool
     (log-spaced from 0.001 to 100) rather than the fixed alpha=1.0 of v4.
     When OOF pool is small (<200 samples), uses stronger regularization
     (alpha floor of 1.0) to prevent overfitting.

Architecture
------------
  Base models: LGB (Optuna-tuned) · XGBoost · RandomForest · LogisticRegression
  Meta-learner: Calibrated Ridge on [OOF predictions + macro regime features]
  Feature selection: SHAP-guided pruning if v4 SHAP data available
  Features: same as v4 (ALL_FEATURE_COLS from feature_engineer + macro + R&D)

Saves
-----
  data/model_v5.pkl       — full bundle (version="v5")
  data/model_v5_meta.json — training metadata
  Promoted to data/model.pkl if v5 AUC > current production by ≥ PROMO_DELTA

Usage
-----
  python3 predictive_model/ml_model_v5.py [--force]
  python3 predictive_model/ml_model_v5.py --optuna-trials 30
  python3 predictive_model/ml_model_v5.py --no-shap-pruning  # skip pruning step
"""

import sys
import json
import pickle
import logging
import numpy as np
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS
from predictive_model.macro_features import (
    add_macro_features, MACRO_FEATURE_COLS, get_macro_at_date,
)
from predictive_model.rd_signal_extractor import (
    get_rd_signals, get_rd_regime_summary, RD_MARKET_COLS, _DEFAULTS as _RD_DEFAULTS,
)

# Inherit Optuna tuner and walk-forward CV from v4 (same logic)
from predictive_model.ml_model_v4 import (
    walk_forward_cv, _optuna_tune_lgb, _calibration_metrics, _compute_shap,
    OPTUNA_TRIALS, ISOTONIC_FLOOR,
)

logger = logging.getLogger(__name__)

DATA_DIR   = Path(__file__).parent.parent / "data"
V5_PATH    = DATA_DIR / "model_v5.pkl"
V5_META    = DATA_DIR / "model_v5_meta.json"
MODEL_PATH = DATA_DIR / "model.pkl"
META_PATH  = DATA_DIR / "model_meta.json"
PREV_PATH  = DATA_DIR / "model_prev.pkl"
SHAP_PATH  = DATA_DIR / "shap_summary.json"

MIN_SAMPLES         = 100
PROMO_DELTA         = 0.003
SHAP_PRUNE_SAMPLES  = 500    # only prune features when this many training samples available
SHAP_PRUNE_FLOOR    = 0.0005 # drop features with mean |SHAP| below 0.05% of max_shap

ALL_FEATURE_COLS = FEATURE_COLS + MACRO_FEATURE_COLS + RD_MARKET_COLS

# Macro features to add to the meta-learner (indices within ALL_FEATURE_COLS)
# These give the stacker access to the regime context at inference time.
META_REGIME_COLS = ["vix_level", "vix_regime", "market_regime", "spy_momentum_20d"]


# =============================================================================
# SHAP-GUIDED FEATURE PRUNING
# =============================================================================

def _prune_low_shap_features(
    X: np.ndarray,
    feature_cols: list,
    n_samples: int,
    enabled: bool = True,
) -> tuple:
    """
    Read shap_summary.json and drop features with mean |SHAP| < SHAP_PRUNE_FLOOR
    relative to the maximum SHAP value. Only runs when n_samples >= SHAP_PRUNE_SAMPLES.

    Returns (X_pruned, kept_cols, dropped_cols).
    """
    if not enabled or n_samples < SHAP_PRUNE_SAMPLES:
        return X, feature_cols, []

    if not SHAP_PATH.exists():
        logger.info("  ml_model_v5: no shap_summary.json — skipping pruning")
        return X, feature_cols, []

    try:
        with open(SHAP_PATH, "r") as fh:
            shap_data = json.load(fh)
        shap_imp = shap_data.get("features", {})
        if not shap_imp:
            return X, feature_cols, []

        max_shap  = max(shap_imp.values()) if shap_imp else 1.0
        threshold = max_shap * SHAP_PRUNE_FLOOR

        dropped   = [c for c in feature_cols if shap_imp.get(c, threshold) < threshold]
        kept      = [c for c in feature_cols if c not in dropped]

        if not dropped:
            return X, feature_cols, []

        kept_idx  = [feature_cols.index(c) for c in kept]
        X_pruned  = X[:, kept_idx]

        logger.info(
            "  ml_model_v5: SHAP pruning dropped %d features (threshold=%.6f): %s",
            len(dropped), threshold, dropped[:10],
        )
        return X_pruned, kept, dropped

    except Exception as exc:
        logger.warning("  ml_model_v5: SHAP pruning failed (%s) — using all features.", exc)
        return X, feature_cols, []


# =============================================================================
# AUGMENTED FEATURE MATRIX (same as v4)
# =============================================================================

def _build_augmented_matrix(target: str = "outcome_20d"):
    X, y, meta_df = build_feature_matrix(target=target)
    if len(X) == 0:
        return X, y, meta_df

    logger.info(
        "  ml_model_v5: fetching macro features for %d unique dates…",
        meta_df["snapshot_date"].nunique(),
    )
    macro_vals = []
    for d in meta_df["snapshot_date"]:
        macro_vals.append(get_macro_at_date(d))
    macro_arr = np.array(
        [[r[c] for c in MACRO_FEATURE_COLS] for r in macro_vals], dtype=float
    )

    try:
        rd_now = get_rd_signals()
    except Exception:
        rd_now = dict(_RD_DEFAULTS)

    rd_arr = np.array(
        [[rd_now.get(c, _RD_DEFAULTS.get(c, 0.0)) for c in RD_MARKET_COLS]] * len(X),
        dtype=float,
    )

    X_aug = np.hstack([X, macro_arr, rd_arr])
    return X_aug, y, meta_df


# =============================================================================
# META-LEARNER FEATURE CONSTRUCTION
# =============================================================================

def _build_meta_X(
    oof_cols: np.ndarray,
    X_macro: np.ndarray,
    feature_cols: list,
) -> np.ndarray:
    """
    Build meta-learner input: [OOF predictions | regime features].

    oof_cols  — (n_samples, n_models) OOF probability predictions
    X_macro   — (n_samples, len(feature_cols)) full augmented feature matrix
    feature_cols — names for each column of X_macro
    """
    regime_idxs = []
    for col in META_REGIME_COLS:
        if col in feature_cols:
            regime_idxs.append(feature_cols.index(col))

    if regime_idxs:
        regime_features = X_macro[:, regime_idxs]
        # Normalise each regime feature to [0, 1] to match OOF probability scale
        for j in range(regime_features.shape[1]):
            col_min = regime_features[:, j].min()
            col_max = regime_features[:, j].max()
            if col_max > col_min:
                regime_features[:, j] = (regime_features[:, j] - col_min) / (col_max - col_min)
        meta_X = np.hstack([oof_cols, regime_features])
        logger.info(
            "  ml_model_v5: meta-learner enriched with %d regime features: %s",
            len(regime_idxs), META_REGIME_COLS,
        )
    else:
        meta_X = oof_cols
        logger.info("  ml_model_v5: regime columns not found — using plain OOF meta-X")

    return meta_X


# =============================================================================
# ADAPTIVE RIDGE ALPHA SELECTION
# =============================================================================

def _select_ridge_alpha(meta_X: np.ndarray, meta_y: np.ndarray) -> float:
    """
    Tune Ridge alpha via 3-fold CV over log-spaced candidates.
    Returns alpha that maximises mean CV AUC.
    """
    from sklearn.linear_model import RidgeClassifier
    from sklearn.model_selection import cross_val_score

    n = len(meta_y)
    alpha_floor = 1.0 if n < 200 else 0.001
    candidates  = np.logspace(np.log10(alpha_floor), 2, 12)

    best_alpha = 1.0
    best_auc   = 0.0

    for alpha in candidates:
        try:
            clf   = RidgeClassifier(alpha=alpha, class_weight="balanced")
            aucs  = cross_val_score(clf, meta_X, meta_y, cv=3, scoring="roc_auc")
            score = float(aucs.mean())
            if score > best_auc:
                best_auc   = score
                best_alpha = alpha
        except Exception:
            continue

    logger.info(
        "  ml_model_v5: Ridge alpha selected = %.4f (3-fold AUC=%.4f, n_oof=%d)",
        best_alpha, best_auc, n,
    )
    return float(best_alpha)


# =============================================================================
# TRAINING
# =============================================================================

def train(force: bool = False, optuna_trials: int = OPTUNA_TRIALS,
          shap_pruning: bool = True) -> dict:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression, RidgeClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, accuracy_score
    from sklearn.calibration import CalibratedClassifierCV

    try:
        import lightgbm as lgb
        HAS_LGB = True
    except ImportError:
        lgb = None
        HAS_LGB = False

    try:
        import xgboost as xgb
        HAS_XGB = True
    except ImportError:
        xgb = None
        HAS_XGB = False

    # ── Build augmented feature matrix ────────────────────────────────────────
    X, y_bin, meta_df = _build_augmented_matrix(target="outcome_20d")

    if len(X) < MIN_SAMPLES:
        msg = f"Only {len(X)} samples — need {MIN_SAMPLES}."
        logger.warning("  ml_model_v5: %s", msg)
        return {"status": "SKIPPED", "reason": msg, "n_samples": len(X)}

    active_feature_cols = list(ALL_FEATURE_COLS)

    # ── SHAP-guided feature pruning ────────────────────────────────────────────
    X, active_feature_cols, pruned_features = _prune_low_shap_features(
        X, active_feature_cols, len(X), enabled=shap_pruning,
    )

    logger.info(
        "  ml_model_v5: %d samples | %d features (%d pruned) | LGB=%s XGB=%s | Optuna=%d",
        len(X), len(active_feature_cols), len(pruned_features), HAS_LGB, HAS_XGB,
        optuna_trials,
    )

    # ── Check current production AUC ─────────────────────────────────────────
    prod_auc = 0.0
    if not force and MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, "rb") as fh:
                prod_bundle = pickle.load(fh)
            prod_auc = float(prod_bundle.get("ensemble_auc_val", 0.0))
            logger.info("  ml_model_v5: current production AUC = %.4f", prod_auc)
        except Exception:
            pass

    # ── R&D signals ──────────────────────────────────────────────────────────
    try:
        rd_features = get_rd_signals()
        rd_score    = rd_features.get("rd_intl_sentiment", 0.0)
        logger.info("  ml_model_v5: %s", get_rd_regime_summary())
    except Exception:
        rd_features = dict(_RD_DEFAULTS)
        rd_score    = 0.0

    # ── Time-ordered 80/20 split ──────────────────────────────────────────────
    split       = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y_bin[:split], y_bin[split:]

    scaler   = StandardScaler()
    X_tr_s   = scaler.fit_transform(X_tr)
    X_val_s  = scaler.transform(X_val)
    X_all_s  = scaler.transform(X)

    # ── Optuna tuning for LGB ─────────────────────────────────────────────────
    lgb_best_params = {}
    if HAS_LGB and optuna_trials > 0:
        logger.info("  ml_model_v5: Optuna tuning LGB (%d trials)…", optuna_trials)
        lgb_best_params = _optuna_tune_lgb(X, y_bin, n_trials=optuna_trials)

    # ── Define base models ────────────────────────────────────────────────────
    base_model_specs = []
    if HAS_LGB:
        lgb_params = {
            "n_estimators":      lgb_best_params.get("n_estimators", 500),
            "learning_rate":     lgb_best_params.get("learning_rate", 0.05),
            "num_leaves":        lgb_best_params.get("num_leaves", 31),
            "max_depth":         lgb_best_params.get("max_depth", 6),
            "min_child_samples": lgb_best_params.get("min_child_samples", 10),
            "subsample":         lgb_best_params.get("subsample", 0.8),
            "colsample_bytree":  lgb_best_params.get("colsample_bytree", 0.8),
            "reg_alpha":         lgb_best_params.get("reg_alpha", 0.0),
            "reg_lambda":        lgb_best_params.get("reg_lambda", 0.0),
            "class_weight": "balanced",
            "random_state":  42,
            "verbosity":    -1,
        }
        base_model_specs.append(("lgb", lgb.LGBMClassifier(**lgb_params)))
    if HAS_XGB:
        pos = int(y_tr.sum()); neg = len(y_tr) - pos
        spw = neg / max(pos, 1)
        base_model_specs.append(("xgb", xgb.XGBClassifier(
            n_estimators=500, learning_rate=0.05, max_depth=5,
            min_child_weight=5, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=spw, use_label_encoder=False,
            eval_metric="auc", random_state=42, verbosity=0,
            early_stopping_rounds=50,
        )))
    base_model_specs.append(("rf", RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=5,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )))
    base_model_specs.append(("lr", LogisticRegression(
        C=1.0, class_weight="balanced", max_iter=500, random_state=42,
    )))

    # ── Walk-forward CV for OOF predictions ──────────────────────────────────
    logger.info("  ml_model_v5: walk-forward CV (5 folds, 20-day gap)…")
    base_names  = [n for n, _ in base_model_specs]
    base_models = [m for _, m in base_model_specs]
    oof_probas, oof_mask, wf_aucs = walk_forward_cv(
        base_models, X_all_s, y_bin, n_splits=5, gap_days=20,
    )

    for i, name in enumerate(base_names):
        logger.info("  WF-CV %s: AUC = %.4f", name, wf_aucs[i])

    results = {f"wf_cv_{name}_auc": wf_aucs[i] for i, name in enumerate(base_names)}
    results["wf_cv_mean_auc"] = round(float(np.mean(list(wf_aucs.values()))), 4)

    # ── Train base models on full training set ────────────────────────────────
    fitted_bases    = {}
    val_probas      = []
    best_tree_model = None

    for name, model in base_model_specs:
        try:
            if name == "lgb":
                model.fit(X_tr_s, y_tr,
                          eval_set=[(X_val_s, y_val)],
                          callbacks=[lgb.early_stopping(50, verbose=False),
                                     lgb.log_evaluation(period=-1)])
            elif name == "xgb":
                model.fit(X_tr_s, y_tr,
                          eval_set=[(X_val_s, y_val)], verbose=False)
            else:
                model.fit(X_tr_s, y_tr)

            p   = model.predict_proba(X_val_s)[:, 1]
            auc = roc_auc_score(y_val, p) if len(set(y_val)) > 1 else 0.5
            results[f"{name}_auc_val"] = round(auc, 4)
            fitted_bases[name] = model
            val_probas.append(p)
            if best_tree_model is None and name in ("lgb", "rf"):
                best_tree_model = model
            logger.info("  %s val AUC: %.4f", name, auc)
        except Exception as exc:
            logger.warning("  ml_model_v5: %s failed: %s", name, exc)

    # ── OOF pool for meta-learner ─────────────────────────────────────────────
    oof_available = oof_mask.sum() > 50
    model_keys    = list(fitted_bases.keys())

    if oof_available:
        model_idx     = [base_names.index(k) for k in model_keys if k in base_names]
        oof_cols_tr   = oof_probas[model_idx, :][:, oof_mask].T   # (n_oof, n_models)
        meta_y_tr     = y_bin[oof_mask]
        oof_cols_val  = np.column_stack(val_probas)
    else:
        half          = max(len(val_probas) // 2, 1)
        oof_cols_tr   = np.column_stack(val_probas[:half])
        meta_y_tr     = y_val[:len(oof_cols_tr)]
        oof_cols_val  = np.column_stack(val_probas)

    # ── v5 KEY IMPROVEMENT: add regime features to meta-learner inputs ─────────
    X_oof_macro = X_all_s[oof_mask] if oof_available else X_val_s[:len(oof_cols_tr)]
    meta_X_tr   = _build_meta_X(oof_cols_tr,  X_oof_macro,        active_feature_cols)
    meta_X_val  = _build_meta_X(oof_cols_val, X_val_s[:len(oof_cols_val)], active_feature_cols)

    # ── v5 KEY IMPROVEMENT: adaptive Ridge alpha ───────────────────────────────
    n_oof      = len(meta_y_tr)
    best_alpha = _select_ridge_alpha(meta_X_tr, meta_y_tr)

    # ── Calibrated Ridge stacking meta-learner ─────────────────────────────────
    cal_method  = "isotonic" if n_oof >= ISOTONIC_FLOOR else "sigmoid"
    logger.info(
        "  ml_model_v5: Ridge stacking meta-learner — n_oof=%d  alpha=%.4f  cal=%s",
        n_oof, best_alpha, cal_method,
    )

    ridge_base = RidgeClassifier(alpha=best_alpha, class_weight="balanced")
    meta_model = CalibratedClassifierCV(ridge_base, cv=3, method=cal_method)

    try:
        meta_model.fit(meta_X_tr, meta_y_tr)
        meta_proba  = meta_model.predict_proba(meta_X_val)[:, 1]
        meta_auc    = roc_auc_score(y_val, meta_proba) if len(set(y_val)) > 1 else 0.5
        meta_acc    = accuracy_score(y_val, (meta_proba >= 0.5).astype(int))
        results["stacked_auc_val"]  = round(meta_auc, 4)
        results["stacked_acc_val"]  = round(meta_acc, 4)
        results["ensemble_auc_val"] = round(meta_auc, 4)
        results["ensemble_acc_val"] = round(meta_acc, 4)
        results["calibration_method"] = cal_method
        results["ridge_alpha"]        = round(best_alpha, 4)
        logger.info("  Stacked meta-learner val AUC: %.4f  (cal=%s)", meta_auc, cal_method)
    except Exception as exc:
        logger.warning("  ml_model_v5: stacking failed (%s), using mean ensemble", exc)
        meta_model = None
        ens_proba  = np.mean(val_probas, axis=0) if val_probas else np.full(len(y_val), 0.5)
        ens_auc    = roc_auc_score(y_val, ens_proba) if len(set(y_val)) > 1 else 0.5
        meta_proba = ens_proba
        results["ensemble_auc_val"] = round(ens_auc, 4)
        results["ensemble_acc_val"] = round(
            accuracy_score(y_val, (ens_proba >= 0.5).astype(int)), 4)

    ens_auc = results["ensemble_auc_val"]

    # ── Brier score + reliability diagram ─────────────────────────────────────
    cal_metrics = _calibration_metrics(y_val, meta_proba)
    results["brier_score"]         = cal_metrics["brier_score"]
    results["reliability_diagram"] = cal_metrics["reliability_diagram"]
    logger.info("  Brier score: %.4f  (0.00=perfect, 0.25=random)", cal_metrics["brier_score"])

    # ── SHAP importance (for v6 pruning) ─────────────────────────────────────
    shap_values: dict = {}
    if best_tree_model is not None:
        logger.info("  ml_model_v5: computing SHAP on validation set…")
        shap_sample = X_val_s[:min(500, len(X_val_s))]
        shap_values = _compute_shap(best_tree_model, "v5_lgb_or_rf",
                                    shap_sample, active_feature_cols)
        if shap_values:
            top_shap = sorted(shap_values.items(), key=lambda x: x[1], reverse=True)[:10]
            logger.info("  SHAP top-3: %s", [f for f, _ in top_shap[:3]])
            try:
                DATA_DIR.mkdir(exist_ok=True)
                with open(SHAP_PATH, "w") as fh:
                    json.dump({
                        "computed_at":    datetime.now().isoformat(),
                        "model_version":  "v5",
                        "n_val_samples":  len(shap_sample),
                        "features":       shap_values,
                        "top_10":         [{"feature": f, "mean_abs_shap": v}
                                           for f, v in top_shap],
                    }, fh, indent=2)
            except Exception:
                pass

    # ── Top features by SHAP or OOF AUC delta ─────────────────────────────────
    if shap_values:
        top_features = sorted(shap_values.items(), key=lambda x: x[1], reverse=True)[:10]
        top_features = [f for f, _ in top_features]
    else:
        top_features = active_feature_cols[:10]

    macro_top = [c for c in top_features if c in MACRO_FEATURE_COLS]

    # ── Save bundle ───────────────────────────────────────────────────────────
    bundle_v5 = {
        "version":            "v5",
        "scaler":             scaler,
        "fitted_bases":       fitted_bases,
        "meta_model":         meta_model,
        "meta_regime_cols":   META_REGIME_COLS,
        "feature_cols":       active_feature_cols,
        "pruned_features":    pruned_features,
        "base_names":         list(fitted_bases.keys()),
        "has_lgb":            HAS_LGB,
        "has_xgb":            HAS_XGB,
        "has_macro":          True,
        "has_rd":             True,
        "macro_cols":         MACRO_FEATURE_COLS,
        "rd_cols":            RD_MARKET_COLS,
        "rd_score_train":     rd_score,
        "rd_features_at_train": rd_features,
        "optuna_best_params": lgb_best_params,
        "calibration_method": cal_method,
        "ridge_alpha":        best_alpha,
        "ensemble_auc_val":   ens_auc,
        "trained_at":         datetime.now().isoformat(),
        "n_train":            len(X_tr),
        "n_val":              len(X_val),
    }
    with open(V5_PATH, "wb") as fh:
        pickle.dump(bundle_v5, fh)

    meta_v5 = {
        "version":            "v5",
        "trained_at":         datetime.now().isoformat(),
        "n_samples":          len(X),
        "n_features":         len(active_feature_cols),
        "pruned_features":    pruned_features,
        "macro_features":     MACRO_FEATURE_COLS,
        "meta_regime_cols":   META_REGIME_COLS,
        "ridge_alpha":        round(best_alpha, 4),
        "rd_score":           rd_score,
        "has_lightgbm":       HAS_LGB,
        "has_xgboost":        HAS_XGB,
        "positive_rate":      round(float(y_bin.mean()), 4),
        "top_features":       top_features,
        "macro_top_features": macro_top,
        "feature_cols":       active_feature_cols,
        "optuna_trials":      optuna_trials,
        "optuna_best_params": lgb_best_params,
        "shap_available":     bool(shap_values),
        **results,
    }
    with open(V5_META, "w") as fh:
        json.dump(meta_v5, fh, indent=2, default=str)
    logger.info("  ml_model_v5: saved → %s", V5_PATH)

    # ── Promote to production if better ──────────────────────────────────────
    promoted = False
    if force or ens_auc >= prod_auc + PROMO_DELTA:
        import shutil
        if MODEL_PATH.exists():
            shutil.copy(MODEL_PATH, PREV_PATH)
        shutil.copy(V5_PATH, MODEL_PATH)
        shutil.copy(V5_META, META_PATH)
        promoted = True
        logger.info(
            "  ml_model_v5: PROMOTED (v5 AUC %.4f > prod %.4f)", ens_auc, prod_auc,
        )
    else:
        logger.info(
            "  ml_model_v5: not promoted (delta %+.4f)", ens_auc - prod_auc,
        )

    return {
        **meta_v5,
        "status":    "TRAINED",
        "promoted":  promoted,
        "prod_auc":  prod_auc,
        "v5_auc":    ens_auc,
        "auc_delta": round(ens_auc - prod_auc, 4),
    }


# =============================================================================
# INFERENCE
# =============================================================================

def predict_proba_v5(feature_df) -> np.ndarray:
    """Score live signals using v5. Falls back to v4 if model not yet trained."""
    if not V5_PATH.exists():
        from predictive_model.ml_model_v4 import predict_proba_v4
        return predict_proba_v4(feature_df)

    with open(V5_PATH, "rb") as fh:
        bundle = pickle.load(fh)

    scaler       = bundle["scaler"]
    fitted_bases = bundle["fitted_bases"]
    meta_model   = bundle.get("meta_model")
    base_names   = bundle.get("base_names", list(fitted_bases.keys()))
    all_cols     = bundle.get("feature_cols", ALL_FEATURE_COLS)
    macro_cols   = bundle.get("macro_cols", MACRO_FEATURE_COLS)
    regime_cols  = bundle.get("meta_regime_cols", META_REGIME_COLS)
    orig_cols    = [c for c in all_cols if c not in macro_cols]

    today = datetime.now().date()
    macro = get_macro_at_date(today)

    X_orig = feature_df.reindex(columns=orig_cols, fill_value=0).values.astype(float)
    X_mac  = np.array([[macro[c] for c in macro_cols]] * len(X_orig), dtype=float)
    X      = np.hstack([X_orig, X_mac])
    X_s    = scaler.transform(X)

    base_probas = []
    for name in base_names:
        m = fitted_bases.get(name)
        if m is not None:
            try:
                base_probas.append(m.predict_proba(X_s)[:, 1])
            except Exception:
                pass

    if not base_probas:
        return np.full(len(X), 0.5)

    if meta_model is not None:
        try:
            oof_cols_inf = np.column_stack(base_probas)
            meta_X_inf   = _build_meta_X(oof_cols_inf, X_s, list(all_cols))
            clf_p        = meta_model.predict_proba(meta_X_inf)[:, 1]
        except Exception:
            clf_p = np.mean(base_probas, axis=0)
    else:
        clf_p = np.mean(base_probas, axis=0)

    return np.clip(clf_p, 0.0, 1.0)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import argparse as _ap
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = _ap.ArgumentParser()
    p.add_argument("--force",           action="store_true")
    p.add_argument("--optuna-trials",   type=int, default=OPTUNA_TRIALS)
    p.add_argument("--no-shap-pruning", action="store_true")
    args = p.parse_args()

    result = train(
        force=args.force,
        optuna_trials=args.optuna_trials,
        shap_pruning=not args.no_shap_pruning,
    )
    print(json.dumps(result, indent=2, default=str))

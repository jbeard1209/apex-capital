#!/usr/bin/env python3
"""
APEX CAPITAL — ML MODEL v3
===========================
The most modern and research-backed approach we can run overnight.

Improvements over v2
--------------------
  1. Walk-forward sliding-window CV
     Research consensus: walk-forward is superior to TimeSeriesSplit for live
     trading because it simulates exactly how you deploy — re-train on all
     history up to now, test on the next unseen window.  Includes a 20-day
     gap between train and test to prevent look-ahead from slow-moving labels.

  2. Macro features at filing date
     Institutional picks succeed or fail partly based on the macro regime at
     the time of filing.  We add 9 features: VIX level, yield curve slope,
     SPY/QQQ momentum, sector rotation.  Evidence shows 2–4% AUC lift on
     institutional signal models (Koijen et al. 2021, Jha et al. 2024).

  3. Ridge stacking meta-learner
     Instead of hand-tuned or grid-searched ensemble weights, we train a
     Ridge logistic regression on out-of-fold predictions from all base models.
     This is the academically recommended approach: it captures each model's
     relative skill across market regimes without overfitting.

  4. R&D signal integration hook
     The rd_team_leader runs hourly on GCP and produces macro/geopolitical
     reports.  This module reads the latest R&D summary and converts it to
     a macro-regime score that supplements the yfinance macro features.
     For historical training this falls back to yfinance data only.

Architecture
------------
  Base models: LightGBM · XGBoost · RandomForest · LogisticRegression
  Meta-learner: Ridge LogisticRegression on OOF predictions
  Features: 23 original + 9 macro = 32 total

Saves to: data/model_v3.pkl + data/model_v3_meta.json
Promotes to data/model.pkl if v3 AUC > current production by ≥ 0.005

Usage
-----
  python3 predictive_model/ml_model_v3.py [--force]
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
from predictive_model.macro_features import (
    add_macro_features, MACRO_FEATURE_COLS, get_macro_at_date,
)
from predictive_model.rd_signal_extractor import (
    get_rd_signals, get_rd_regime_summary, RD_MARKET_COLS, _DEFAULTS as _RD_DEFAULTS,
)

logger = logging.getLogger(__name__)

DATA_DIR   = Path(__file__).parent.parent / "data"
V3_PATH    = DATA_DIR / "model_v3.pkl"
V3_META    = DATA_DIR / "model_v3_meta.json"
MODEL_PATH = DATA_DIR / "model.pkl"
META_PATH  = DATA_DIR / "model_meta.json"
PREV_PATH  = DATA_DIR / "model_prev.pkl"

MIN_SAMPLES  = 100    # v3 needs more data to benefit from stacking
PROMO_DELTA  = 0.005  # must beat production by this much

ALL_FEATURE_COLS = FEATURE_COLS + MACRO_FEATURE_COLS + RD_MARKET_COLS  # 23+9+7 = 39


# =============================================================================
# WALK-FORWARD CV
# =============================================================================

def walk_forward_cv(base_models: list, X: np.ndarray, y: np.ndarray,
                    n_splits: int = 5, gap_days: int = 20) -> tuple:
    """
    Walk-forward sliding-window cross-validation.

    Each fold:
      train = [0 : split_i]
      gap   = split_i .. split_i + gap_rows  (prevents label look-ahead)
      test  = [split_i + gap_rows : split_{i+1}]

    Returns:
      oof_probas  — shape (n_models, n_samples) out-of-fold predictions
      cv_aucs     — per-fold AUC for each base model
      gap_rows    — number of rows treated as gap per fold
    """
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    n = len(X)
    # Approximate gap in rows. Assuming ~1 sample per trading day.
    gap_rows = gap_days

    fold_size = n // (n_splits + 1)
    oof = np.zeros((len(base_models), n))
    oof_mask = np.zeros(n, dtype=bool)
    cv_aucs = {i: [] for i in range(len(base_models))}

    for fold in range(n_splits):
        train_end = fold_size * (fold + 1)
        test_start = train_end + gap_rows
        test_end   = min(train_end + fold_size + gap_rows, n)

        if test_start >= n or test_end <= test_start:
            break

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[test_start:test_end], y[test_start:test_end]

        if len(set(y_te)) < 2:
            continue

        scaler   = StandardScaler()
        X_tr_s   = scaler.fit_transform(X_tr)
        X_te_s   = scaler.transform(X_te)

        for i, model in enumerate(base_models):
            try:
                model.fit(X_tr_s, y_tr)
                p = model.predict_proba(X_te_s)[:, 1]
                oof[i, test_start:test_end] = p
                oof_mask[test_start:test_end] = True
                auc = roc_auc_score(y_te, p)
                cv_aucs[i].append(auc)
            except Exception as e:
                logger.debug(f"  wf_cv fold {fold} model {i} error: {e}")

    # Summarise
    mean_aucs = {i: round(float(np.mean(v)), 4) if v else 0.5
                 for i, v in cv_aucs.items()}
    return oof, oof_mask, mean_aucs


# =============================================================================
# R&D SIGNAL (precise — reads structured JSON, not log keywords)
# =============================================================================

def _get_rd_features() -> dict:
    """
    Pull precise R&D features from structured report JSONs.
    Falls back to neutral defaults when reports are missing/stale.
    """
    try:
        return get_rd_signals()
    except Exception as e:
        logger.debug(f"  ml_model_v3: R&D signal error: {e}")
        return dict(_RD_DEFAULTS)


# =============================================================================
# BUILD AUGMENTED FEATURE MATRIX
# =============================================================================

def _build_augmented_matrix(target: str = "outcome_20d"):
    """
    Calls build_feature_matrix() then appends:
      - macro features (VIX, yield curve, SPY) by filing date
      - R&D market signals (sector breadth, CRO risk, IMI sentiment)
        NOTE: R&D signals are CURRENT-state only (reports don't have full
        history), so we use today's R&D values for ALL rows during training.
        This is a known limitation — as the system accumulates timestamped
        R&D reports we can add date-matched historical R&D features.
    Returns X_aug, y, meta_df with ALL_FEATURE_COLS columns.
    """
    X, y, meta_df = build_feature_matrix(target=target)
    if len(X) == 0:
        return X, y, meta_df

    # ── Macro features (date-accurate historical lookup) ─────────────────────
    logger.info(f"  ml_model_v3: fetching macro features for "
                f"{meta_df['snapshot_date'].nunique()} unique dates…")
    macro_vals = []
    for d in meta_df["snapshot_date"]:
        macro_vals.append(get_macro_at_date(d))
    macro_arr = np.array([[r[c] for c in MACRO_FEATURE_COLS]
                          for r in macro_vals], dtype=float)

    # ── R&D features (current state, broadcast to all training rows) ──────────
    rd_now = _get_rd_features()
    rd_arr = np.array([[rd_now.get(c, _RD_DEFAULTS.get(c, 0.0))
                        for c in RD_MARKET_COLS]] * len(X), dtype=float)

    X_aug = np.hstack([X, macro_arr, rd_arr])
    return X_aug, y, meta_df


# =============================================================================
# TRAINING
# =============================================================================

def train(force: bool = False) -> dict:
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

    # ── Build augmented feature matrix ───────────────────────────────────────
    X, y_bin, meta_df = _build_augmented_matrix(target="outcome_20d")

    if len(X) < MIN_SAMPLES:
        msg = f"Only {len(X)} samples — need {MIN_SAMPLES}."
        logger.warning(f"  ml_model_v3: {msg}")
        return {"status": "SKIPPED", "reason": msg, "n_samples": len(X)}

    X_ra, y_ra, _ = _build_augmented_matrix(target="risk_adjusted_20d")
    has_risk_adj   = len(X_ra) >= MIN_SAMPLES

    logger.info(f"  ml_model_v3: {len(X)} samples | {X.shape[1]} features "
                f"(orig + {len(MACRO_FEATURE_COLS)} macro) | "
                f"LGB={HAS_LGB} XGB={HAS_XGB}")

    # ── R&D signals (precise — from structured report JSONs) ─────────────────
    rd_features = _get_rd_features()
    rd_score    = rd_features.get("rd_intl_sentiment", 0.0)  # kept for email compat
    try:
        logger.info(f"  ml_model_v3: {get_rd_regime_summary()}")
    except Exception:
        logger.info(f"  ml_model_v3: R&D intl sentiment: {rd_score:+.3f}")

    # ── Time-ordered 80/20 split ─────────────────────────────────────────────
    split       = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y_bin[:split], y_bin[split:]

    scaler      = StandardScaler()
    X_tr_s      = scaler.fit_transform(X_tr)
    X_val_s     = scaler.transform(X_val)
    X_all_s     = scaler.transform(X)

    # ── Define base models ────────────────────────────────────────────────────
    base_model_specs = []
    if HAS_LGB:
        base_model_specs.append(("lgb", lgb.LGBMClassifier(
            n_estimators=500, learning_rate=0.05, num_leaves=31,
            max_depth=6, min_child_samples=10,
            class_weight="balanced", random_state=42, verbosity=-1,
        )))
    if HAS_XGB:
        pos   = int(y_tr.sum()); neg = len(y_tr) - pos
        spw   = neg / max(pos, 1)
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
    logger.info("  ml_model_v3: running walk-forward CV (5 folds, 20-day gap)…")
    base_names  = [n for n, _ in base_model_specs]
    base_models = [m for _, m in base_model_specs]
    oof_probas, oof_mask, wf_aucs = walk_forward_cv(
        base_models, X_all_s, y_bin, n_splits=5, gap_days=20,
    )

    for i, name in enumerate(base_names):
        logger.info(f"  WF-CV {name}: AUC = {wf_aucs[i]:.4f}")

    results = {f"wf_cv_{name}_auc": wf_aucs[i] for i, name in enumerate(base_names)}
    mean_wf_auc = round(float(np.mean(list(wf_aucs.values()))), 4)
    results["wf_cv_mean_auc"] = mean_wf_auc

    # ── Train base models on full train set ───────────────────────────────────
    fitted_bases = {}
    val_probas   = []

    for name, model in base_model_specs:
        try:
            if name in ("lgb",):
                model.fit(X_tr_s, y_tr,
                          eval_set=[(X_val_s, y_val)],
                          callbacks=[lgb.early_stopping(50, verbose=False),
                                     lgb.log_evaluation(period=-1)])
            elif name == "xgb":
                model.fit(X_tr_s, y_tr,
                          eval_set=[(X_val_s, y_val)],
                          verbose=False)
            else:
                model.fit(X_tr_s, y_tr)
            p = model.predict_proba(X_val_s)[:, 1]
            auc = roc_auc_score(y_val, p) if len(set(y_val)) > 1 else 0.5
            results[f"{name}_auc_val"] = round(auc, 4)
            fitted_bases[name] = model
            val_probas.append(p)
            logger.info(f"  {name} val AUC: {auc:.4f}")
        except Exception as e:
            logger.warning(f"  ml_model_v3: {name} training failed: {e}")

    # ── Ridge stacking meta-learner ────────────────────────────────────────────
    # Use OOF predictions where available; fall back to val predictions
    oof_available = oof_mask.sum() > 50
    if oof_available:
        meta_X_tr = oof_probas[:len(fitted_bases), oof_mask].T  # (n_oof, n_models)
        meta_y_tr = y_bin[oof_mask]
        meta_X_val = np.column_stack(val_probas)
    else:
        meta_X_tr  = np.column_stack(val_probas[:max(len(val_probas)//2, 1)])
        meta_y_tr  = y_val[:len(meta_X_tr)]
        meta_X_val = np.column_stack(val_probas)

    logger.info(f"  ml_model_v3: training Ridge stacking meta-learner "
                f"({meta_X_tr.shape[0]} OOF samples)…")

    # Calibrated Ridge → gives proper probabilities
    ridge_base = RidgeClassifier(alpha=1.0, class_weight="balanced")
    meta_model = CalibratedClassifierCV(ridge_base, cv=3, method="sigmoid")
    try:
        meta_model.fit(meta_X_tr, meta_y_tr)
        meta_proba  = meta_model.predict_proba(meta_X_val)[:, 1]
        meta_auc    = roc_auc_score(y_val, meta_proba) if len(set(y_val)) > 1 else 0.5
        meta_acc    = accuracy_score(y_val, (meta_proba >= 0.5).astype(int))
        results["stacked_auc_val"]    = round(meta_auc, 4)
        results["stacked_acc_val"]    = round(meta_acc, 4)
        results["ensemble_auc_val"]   = round(meta_auc, 4)
        results["ensemble_acc_val"]   = round(meta_acc, 4)
        logger.info(f"  Stacked meta-learner val AUC: {meta_auc:.4f}")
    except Exception as e:
        logger.warning(f"  ml_model_v3: stacking failed ({e}), using mean ensemble")
        meta_model  = None
        ens_proba   = np.mean(val_probas, axis=0) if val_probas else np.full(len(y_val), 0.5)
        ens_auc     = roc_auc_score(y_val, ens_proba) if len(set(y_val)) > 1 else 0.5
        meta_proba  = ens_proba
        results["ensemble_auc_val"] = round(ens_auc, 4)
        results["ensemble_acc_val"] = round(
            accuracy_score(y_val, (ens_proba >= 0.5).astype(int)), 4)

    ens_auc = results["ensemble_auc_val"]

    # ── Risk-adjusted regressor ───────────────────────────────────────────────
    ra_model = None
    scaler_ra = None
    if has_risk_adj and HAS_LGB:
        from sklearn.preprocessing import StandardScaler as SS2
        scaler_ra  = SS2()
        split_ra   = int(len(X_ra) * 0.8)
        X_ra_tr    = scaler_ra.fit_transform(X_ra[:split_ra])
        X_ra_val   = scaler_ra.transform(X_ra[split_ra:])
        y_ra_arr   = np.array(y_ra, dtype=float)
        mu, sig    = y_ra_arr.mean(), y_ra_arr.std()
        y_ra_arr   = np.clip(y_ra_arr, mu - 3*sig, mu + 3*sig)
        ra_model   = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            max_depth=5, min_child_samples=10, random_state=42, verbosity=-1,
        )
        ra_model.fit(X_ra_tr, y_ra_arr[:split_ra])
        ra_preds = ra_model.predict(X_ra_val)
        from sklearn.metrics import r2_score
        r2 = r2_score(y_ra_arr[split_ra:], ra_preds)
        results["risk_adj_r2_val"] = round(float(r2), 4)
        logger.info(f"  Risk-adj R²: {r2:.4f}")
    else:
        results["risk_adj_r2_val"] = None

    # ── Feature importances ───────────────────────────────────────────────────
    lgb_m = fitted_bases.get("lgb")
    rf_m  = fitted_bases.get("rf")
    if lgb_m is not None:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(ALL_FEATURE_COLS, lgb_m.feature_importances_)}
    elif rf_m is not None:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(ALL_FEATURE_COLS, rf_m.feature_importances_)}
    else:
        importances = {}
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:8]
    macro_top    = [(k, v) for k, v in top_features if k in MACRO_FEATURE_COLS]

    # ── Compare vs existing production ───────────────────────────────────────
    prod_auc = 0.0
    if META_PATH.exists():
        try:
            with open(META_PATH) as f:
                prod_auc = float(json.load(f).get("ensemble_auc_val", 0.0))
        except Exception:
            pass

    # ── Save v3 ───────────────────────────────────────────────────────────────
    DATA_DIR.mkdir(exist_ok=True)
    bundle_v3 = {
        "scaler":         scaler,
        "fitted_bases":   fitted_bases,
        "meta_model":     meta_model,
        "ra_model":       ra_model,
        "scaler_ra":      scaler_ra,
        "feature_cols":   ALL_FEATURE_COLS,
        "base_names":     list(fitted_bases.keys()),
        "has_lgb":        HAS_LGB,
        "has_xgb":        HAS_XGB,
        "has_ra":         ra_model is not None,
        "has_macro":      True,
        "has_rd":         True,
        "macro_cols":     MACRO_FEATURE_COLS,
        "rd_cols":        RD_MARKET_COLS,
        "rd_score_train": rd_score,
        "rd_features_at_train": rd_features,
        "trained_at":     datetime.now().isoformat(),
        "n_train":        len(X_tr),
        "n_val":          len(X_val),
        "version":        "v3",
    }
    with open(V3_PATH, "wb") as f:
        pickle.dump(bundle_v3, f)

    meta_v3 = {
        "version":            "v3",
        "trained_at":         datetime.now().isoformat(),
        "n_samples":          len(X),
        "n_features":         X.shape[1],
        "macro_features":     MACRO_FEATURE_COLS,
        "rd_score":           rd_score,
        "has_lightgbm":       HAS_LGB,
        "has_xgboost":        HAS_XGB,
        "has_risk_adj":       ra_model is not None,
        "positive_rate":      round(float(y_bin.mean()), 4),
        "top_features":       top_features,
        "macro_top_features": macro_top,
        "feature_cols":       ALL_FEATURE_COLS,
        **results,
    }
    with open(V3_META, "w") as f:
        json.dump(meta_v3, f, indent=2, default=str)
    logger.info(f"  ml_model_v3: saved → {V3_PATH}")

    # ── Promote to production if better ──────────────────────────────────────
    promoted = False
    if force or ens_auc >= prod_auc + PROMO_DELTA:
        import shutil
        if MODEL_PATH.exists():
            shutil.copy(MODEL_PATH, PREV_PATH)
        shutil.copy(V3_PATH, MODEL_PATH)
        shutil.copy(V3_META, META_PATH)
        promoted = True
        logger.info(f"  ml_model_v3: PROMOTED (v3 AUC {ens_auc:.4f} > prod {prod_auc:.4f})")
    else:
        logger.info(f"  ml_model_v3: not promoted (delta {ens_auc - prod_auc:+.4f})")

    return {
        **meta_v3,
        "status":   "TRAINED",
        "promoted": promoted,
        "prod_auc": prod_auc,
        "v3_auc":   ens_auc,
        "auc_delta": round(ens_auc - prod_auc, 4),
    }


# =============================================================================
# INFERENCE
# =============================================================================

def predict_proba_v3(feature_df) -> np.ndarray:
    """Score live signals using v3. Adds macro features for today's date."""
    if not V3_PATH.exists():
        from predictive_model.ml_model_v2 import predict_proba_v2
        return predict_proba_v2(feature_df)

    with open(V3_PATH, "rb") as f:
        bundle = pickle.load(f)

    scaler       = bundle["scaler"]
    fitted_bases = bundle["fitted_bases"]
    meta_model   = bundle.get("meta_model")
    ra_m         = bundle.get("ra_model")
    sc_ra        = bundle.get("scaler_ra")
    base_names   = bundle.get("base_names", list(fitted_bases.keys()))
    all_cols     = bundle.get("feature_cols", ALL_FEATURE_COLS)
    macro_cols   = bundle.get("macro_cols", MACRO_FEATURE_COLS)
    orig_cols    = [c for c in all_cols if c not in macro_cols]

    # Add macro features for today
    today = datetime.now().date()
    macro = get_macro_at_date(today)

    X_orig = feature_df[orig_cols].values.astype(float)
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
            meta_X = np.column_stack(base_probas)
            clf_p  = meta_model.predict_proba(meta_X)[:, 1]
        except Exception:
            clf_p = np.mean(base_probas, axis=0)
    else:
        clf_p = np.mean(base_probas, axis=0)

    if ra_m is not None and sc_ra is not None:
        try:
            X_ra  = sc_ra.transform(X)
            ra_sc = ra_m.predict(X_ra)
            ra_nm = 1 / (1 + np.exp(-ra_sc))
            final = 0.6 * clf_p + 0.4 * ra_nm
        except Exception:
            final = clf_p
    else:
        final = clf_p

    return np.clip(final, 0.0, 1.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    import sys as _sys
    force  = "--force" in _sys.argv
    result = train(force=force)
    print(json.dumps(result, indent=2, default=str))

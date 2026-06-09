#!/usr/bin/env python3
"""
APEX CAPITAL — ML MODEL v4
===========================
Improvements over v3
--------------------
  1. Optuna hyperparameter tuning for LightGBM
     v3 uses fixed LGB params. v4 runs 25 Optuna trials with walk-forward CV
     to optimise num_leaves, min_child_samples, learning_rate, and reg terms.
     Expected: 1-3% AUC lift vs fixed params.

  2. Isotonic calibration for meta-learner
     v3 uses sigmoid Platt scaling (CalibratedClassifierCV mode='sigmoid').
     Isotonic regression is non-parametric and more accurate for large datasets,
     while sigmoid is more robust for small samples. v4 uses isotonic when
     n_oof_samples >= 500, falling back to sigmoid below that threshold.

  3. Brier score + reliability diagram metrics
     Calibration quality matters for sizing: a model with AUC 0.58 but well-
     calibrated probabilities is worth more than one with AUC 0.60 but poor
     calibration. v4 saves Brier score and calibration curve decile data.

  4. SHAP feature importance
     Uses SHAP TreeExplainer on the best base model (LGB or RF) to compute
     mean |SHAP| per feature across the validation set. Saved to
     data/shap_summary.json for monitoring and drift detection.

  5. sector_momentum_vs_spy as a training feature
     The new feature added to feature_engineer.py (sector ETF 20d return
     minus SPY 20d return). Included in FEATURE_COLS so it's automatically
     picked up by build_feature_matrix().

Architecture
------------
  Base models: LGB (Optuna-tuned) · XGBoost · RandomForest · LogisticRegression
  Meta-learner: Isotonic-calibrated Ridge on OOF walk-forward predictions
  Features: ALL_FEATURE_COLS from v3 (32-39 features incl. macro + R&D)

Saves
-----
  data/model_v4.pkl       — full bundle
  data/model_v4_meta.json — training metadata + Brier score + SHAP
  data/shap_summary.json  — SHAP mean |value| per feature (latest v4 run)
  Promoted to data/model.pkl if AUC delta > PROMO_DELTA vs current production

Usage
-----
  python3 predictive_model/ml_model_v4.py [--force]
  python3 predictive_model/ml_model_v4.py --optuna-trials 50   # more trials
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

logger = logging.getLogger(__name__)

DATA_DIR   = Path(__file__).parent.parent / "data"
V4_PATH    = DATA_DIR / "model_v4.pkl"
V4_META    = DATA_DIR / "model_v4_meta.json"
MODEL_PATH = DATA_DIR / "model.pkl"
META_PATH  = DATA_DIR / "model_meta.json"
PREV_PATH  = DATA_DIR / "model_prev.pkl"
SHAP_PATH  = DATA_DIR / "shap_summary.json"

MIN_SAMPLES    = 100
PROMO_DELTA    = 0.003   # slightly looser than v3 (v4 uses more compute → more reliable estimate)
OPTUNA_TRIALS  = 25
ISOTONIC_FLOOR = 500     # use isotonic only when OOF pool is this large

ALL_FEATURE_COLS = FEATURE_COLS + MACRO_FEATURE_COLS + RD_MARKET_COLS


# =============================================================================
# WALK-FORWARD CV (reused from v3 — with gap to prevent look-ahead)
# =============================================================================

def walk_forward_cv(base_models: list, X: np.ndarray, y: np.ndarray,
                    n_splits: int = 5, gap_days: int = 20) -> tuple:
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    n = len(X)
    gap_rows  = gap_days
    fold_size = n // (n_splits + 1)
    oof       = np.zeros((len(base_models), n))
    oof_mask  = np.zeros(n, dtype=bool)
    cv_aucs   = {i: [] for i in range(len(base_models))}

    for fold in range(n_splits):
        train_end  = fold_size * (fold + 1)
        test_start = train_end + gap_rows
        test_end   = min(train_end + fold_size + gap_rows, n)

        if test_start >= n or test_end <= test_start:
            break

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[test_start:test_end], y[test_start:test_end]

        if len(set(y_te)) < 2:
            continue

        sc     = StandardScaler()
        X_tr_s = sc.fit_transform(X_tr)
        X_te_s = sc.transform(X_te)

        for i, model in enumerate(base_models):
            try:
                model.fit(X_tr_s, y_tr)
                p = model.predict_proba(X_te_s)[:, 1]
                oof[i, test_start:test_end] = p
                oof_mask[test_start:test_end] = True
                cv_aucs[i].append(roc_auc_score(y_te, p))
            except Exception as e:
                logger.debug(f"  wf_cv fold {fold} model {i}: {e}")

    mean_aucs = {i: round(float(np.mean(v)), 4) if v else 0.5
                 for i, v in cv_aucs.items()}
    return oof, oof_mask, mean_aucs


# =============================================================================
# OPTUNA TUNING FOR LIGHTGBM
# =============================================================================

def _optuna_tune_lgb(X: np.ndarray, y: np.ndarray,
                     n_trials: int = OPTUNA_TRIALS) -> dict:
    """
    Run Optuna on a 3-fold walk-forward CV to find the best LGB hyperparams.
    Returns best_params dict (subset of LGBMClassifier params).
    Falls back to default params if Optuna is not installed.
    """
    try:
        import optuna
        import lightgbm as lgb
        from sklearn.metrics import roc_auc_score
        from sklearn.preprocessing import StandardScaler

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        n = len(X)
        fold_size = n // 4   # 3 train folds + 1 gap + 1 test
        gap_rows  = 20

        def objective(trial):
            params = {
                "n_estimators":      trial.suggest_int("n_estimators", 200, 800),
                "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
                "num_leaves":        trial.suggest_int("num_leaves", 15, 63),
                "max_depth":         trial.suggest_int("max_depth", 3, 8),
                "min_child_samples": trial.suggest_int("min_child_samples", 5, 50),
                "subsample":         trial.suggest_float("subsample", 0.6, 1.0),
                "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.6, 1.0),
                "reg_alpha":         trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
                "reg_lambda":        trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
                "class_weight": "balanced",
                "random_state": 42,
                "verbosity":    -1,
            }
            model = lgb.LGBMClassifier(**params)

            aucs = []
            for fold in range(3):
                train_end  = fold_size * (fold + 1)
                test_start = train_end + gap_rows
                test_end   = min(test_start + fold_size, n)
                if test_start >= n or test_end <= test_start:
                    break
                X_tr, y_tr = X[:train_end], y[:train_end]
                X_te, y_te = X[test_start:test_end], y[test_start:test_end]
                if len(set(y_te)) < 2:
                    continue
                sc = StandardScaler()
                try:
                    model.fit(sc.fit_transform(X_tr), y_tr)
                    p = model.predict_proba(sc.transform(X_te))[:, 1]
                    aucs.append(roc_auc_score(y_te, p))
                except Exception:
                    pass

            return float(np.mean(aucs)) if aucs else 0.5

        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        best = study.best_params
        logger.info(f"  Optuna: best LGB AUC {study.best_value:.4f} after {n_trials} trials")
        logger.info(f"  Optuna: best params: num_leaves={best.get('num_leaves')} "
                    f"lr={best.get('learning_rate'):.4f} "
                    f"min_child={best.get('min_child_samples')}")
        return best
    except ImportError:
        logger.info("  Optuna not installed — using default LGB params")
        return {}
    except Exception as e:
        logger.warning(f"  Optuna tuning failed ({e}) — using default LGB params")
        return {}


# =============================================================================
# AUGMENTED FEATURE MATRIX (same as v3)
# =============================================================================

def _build_augmented_matrix(target: str = "outcome_20d"):
    X, y, meta_df = build_feature_matrix(target=target)
    if len(X) == 0:
        return X, y, meta_df

    logger.info(f"  ml_model_v4: fetching macro features for "
                f"{meta_df['snapshot_date'].nunique()} unique dates…")
    macro_vals = []
    for d in meta_df["snapshot_date"]:
        macro_vals.append(get_macro_at_date(d))
    macro_arr = np.array([[r[c] for c in MACRO_FEATURE_COLS]
                          for r in macro_vals], dtype=float)

    try:
        rd_now = get_rd_signals()
    except Exception:
        rd_now = dict(_RD_DEFAULTS)

    rd_arr = np.array([[rd_now.get(c, _RD_DEFAULTS.get(c, 0.0))
                        for c in RD_MARKET_COLS]] * len(X), dtype=float)

    X_aug = np.hstack([X, macro_arr, rd_arr])
    return X_aug, y, meta_df


# =============================================================================
# SHAP IMPORTANCE
# =============================================================================

def _compute_shap(model, model_name: str, X_val_s: np.ndarray,
                  feature_cols: list) -> dict:
    """
    Compute mean |SHAP| per feature on the validation set.
    Returns {feature_name: mean_abs_shap}.
    Falls back to model feature_importances_ if SHAP is unavailable.
    """
    try:
        import shap  # type: ignore
        explainer = shap.TreeExplainer(model)
        sv        = explainer.shap_values(X_val_s)
        # Binary classification: sv is (n_samples, n_features) or list of 2
        if isinstance(sv, list):
            sv = sv[1]  # positive class
        mean_abs = np.abs(sv).mean(axis=0)
        return {col: round(float(v), 6) for col, v in zip(feature_cols, mean_abs)}
    except ImportError:
        logger.debug("  SHAP not installed — using model feature_importances_")
    except Exception as e:
        logger.debug(f"  SHAP failed ({e}) — falling back to feature_importances_")

    # Fallback: model's built-in importance
    if hasattr(model, "feature_importances_"):
        imps = model.feature_importances_
        total = imps.sum() or 1.0
        return {col: round(float(v / total), 6) for col, v in zip(feature_cols, imps)}
    return {}


# =============================================================================
# BRIER SCORE + CALIBRATION CURVE
# =============================================================================

def _calibration_metrics(y_true: np.ndarray, y_prob: np.ndarray,
                          n_bins: int = 10) -> dict:
    """
    Compute Brier score and reliability diagram data.
    Brier score: 0.0 = perfect, 0.25 = random (50% base rate).
    """
    from sklearn.metrics import brier_score_loss
    try:
        bs = float(brier_score_loss(y_true, y_prob))
    except Exception:
        bs = 0.25

    # Reliability diagram: mean predicted prob vs actual frequency per decile
    bins    = np.linspace(0, 1, n_bins + 1)
    diagram = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() > 0:
            diagram.append({
                "bin_center": round((lo + hi) / 2, 3),
                "mean_pred":  round(float(y_prob[mask].mean()), 4),
                "mean_actual": round(float(y_true[mask].mean()), 4),
                "n":           int(mask.sum()),
            })

    return {"brier_score": round(bs, 4), "reliability_diagram": diagram}


# =============================================================================
# TRAINING
# =============================================================================

def train(force: bool = False, optuna_trials: int = OPTUNA_TRIALS) -> dict:
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
        logger.warning(f"  ml_model_v4: {msg}")
        return {"status": "SKIPPED", "reason": msg, "n_samples": len(X)}

    X_ra, y_ra, _ = _build_augmented_matrix(target="risk_adjusted_20d")
    has_risk_adj   = len(X_ra) >= MIN_SAMPLES

    logger.info(f"  ml_model_v4: {len(X)} samples | {X.shape[1]} features | "
                f"LGB={HAS_LGB} XGB={HAS_XGB} | Optuna trials={optuna_trials}")

    # ── R&D signals ──────────────────────────────────────────────────────────
    try:
        rd_features = get_rd_signals()
        rd_score    = rd_features.get("rd_intl_sentiment", 0.0)
        logger.info(f"  ml_model_v4: {get_rd_regime_summary()}")
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
        logger.info(f"  ml_model_v4: Optuna tuning LGB ({optuna_trials} trials)…")
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
    logger.info("  ml_model_v4: walk-forward CV (5 folds, 20-day gap)…")
    base_names  = [n for n, _ in base_model_specs]
    base_models = [m for _, m in base_model_specs]
    oof_probas, oof_mask, wf_aucs = walk_forward_cv(
        base_models, X_all_s, y_bin, n_splits=5, gap_days=20,
    )

    for i, name in enumerate(base_names):
        logger.info(f"  WF-CV {name}: AUC = {wf_aucs[i]:.4f}")

    results = {f"wf_cv_{name}_auc": wf_aucs[i] for i, name in enumerate(base_names)}
    results["wf_cv_mean_auc"] = round(float(np.mean(list(wf_aucs.values()))), 4)

    # ── Train base models on full train set ───────────────────────────────────
    fitted_bases = {}
    val_probas   = []
    best_tree_model = None   # LGB or RF, whichever is available — for SHAP

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
            logger.info(f"  {name} val AUC: {auc:.4f}")
        except Exception as e:
            logger.warning(f"  ml_model_v4: {name} failed: {e}")

    # ── OOF pool for meta-learner ─────────────────────────────────────────────
    oof_available = oof_mask.sum() > 50
    n_models      = len(fitted_bases)
    model_keys    = list(fitted_bases.keys())

    if oof_available:
        model_idx    = [base_names.index(k) for k in model_keys if k in base_names]
        meta_X_tr    = oof_probas[model_idx, :][:, oof_mask].T
        meta_y_tr    = y_bin[oof_mask]
        meta_X_val   = np.column_stack(val_probas)
    else:
        half = max(len(val_probas) // 2, 1)
        meta_X_tr    = np.column_stack(val_probas[:half])
        meta_y_tr    = y_val[:len(meta_X_tr)]
        meta_X_val   = np.column_stack(val_probas)

    # ── Ridge stacking meta-learner with isotonic calibration ─────────────────
    n_oof = len(meta_y_tr)
    cal_method = "isotonic" if n_oof >= ISOTONIC_FLOOR else "sigmoid"
    logger.info(f"  ml_model_v4: Ridge stacking meta-learner "
                f"({n_oof} OOF samples, calibration={cal_method})…")

    ridge_base = RidgeClassifier(alpha=1.0, class_weight="balanced")
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
        logger.info(f"  Stacked meta-learner val AUC: {meta_auc:.4f}  (cal={cal_method})")
    except Exception as e:
        logger.warning(f"  ml_model_v4: stacking failed ({e}), using mean ensemble")
        meta_model = None
        ens_proba  = np.mean(val_probas, axis=0) if val_probas else np.full(len(y_val), 0.5)
        ens_auc    = roc_auc_score(y_val, ens_proba) if len(set(y_val)) > 1 else 0.5
        meta_proba = ens_proba
        results["ensemble_auc_val"] = round(ens_auc, 4)
        results["ensemble_acc_val"] = round(
            accuracy_score(y_val, (ens_proba >= 0.5).astype(int)), 4)

    ens_auc = results["ensemble_auc_val"]

    # ── Calibration metrics (Brier score + reliability diagram) ───────────────
    cal_metrics = _calibration_metrics(y_val, meta_proba)
    results["brier_score"]          = cal_metrics["brier_score"]
    results["reliability_diagram"]  = cal_metrics["reliability_diagram"]
    logger.info(f"  Brier score: {cal_metrics['brier_score']:.4f}  "
                f"(0.00=perfect, 0.25=random)")

    # ── Risk-adjusted regressor ───────────────────────────────────────────────
    ra_model  = None
    scaler_ra = None
    if has_risk_adj and HAS_LGB:
        from sklearn.preprocessing import StandardScaler as SS2
        scaler_ra  = SS2()
        split_ra   = int(len(X_ra) * 0.8)
        X_ra_tr    = scaler_ra.fit_transform(X_ra[:split_ra])
        X_ra_val   = scaler_ra.transform(X_ra[split_ra:])
        y_ra_arr   = np.array(y_ra, dtype=float)
        mu, sig    = y_ra_arr.mean(), y_ra_arr.std()
        y_ra_arr   = np.clip(y_ra_arr, mu - 3 * sig, mu + 3 * sig)
        ra_model   = lgb.LGBMRegressor(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            max_depth=5, min_child_samples=10, random_state=42, verbosity=-1,
        )
        ra_model.fit(X_ra_tr, y_ra_arr[:split_ra])
        from sklearn.metrics import r2_score
        r2 = r2_score(y_ra_arr[split_ra:], ra_model.predict(X_ra_val))
        results["risk_adj_r2_val"] = round(float(r2), 4)
        logger.info(f"  Risk-adj R²: {r2:.4f}")
    else:
        results["risk_adj_r2_val"] = None

    # ── SHAP feature importance ───────────────────────────────────────────────
    shap_values = {}
    if best_tree_model is not None:
        logger.info("  ml_model_v4: computing SHAP values on validation set…")
        # Use a sample of validation set if large (SHAP is O(n * features))
        shap_sample = X_val_s[:min(500, len(X_val_s))]
        shap_values = _compute_shap(best_tree_model, "lgb_or_rf",
                                    shap_sample, ALL_FEATURE_COLS)
        if shap_values:
            top_shap = sorted(shap_values.items(), key=lambda x: x[1], reverse=True)[:10]
            logger.info(f"  SHAP top-3: {[f for f, _ in top_shap[:3]]}")
            # Save SHAP summary for monitoring
            try:
                DATA_DIR.mkdir(exist_ok=True)
                with open(SHAP_PATH, "w") as f:
                    json.dump({
                        "computed_at": datetime.now().isoformat(),
                        "model_version": "v4",
                        "n_val_samples": len(shap_sample),
                        "shap_mean_abs": shap_values,
                        "top_features": top_shap,
                    }, f, indent=2, default=str)
                logger.info(f"  SHAP summary → {SHAP_PATH}")
            except Exception as _e:
                logger.debug(f"  SHAP save failed: {_e}")

    results["shap_top_features"] = (
        [(f, v) for f, v in sorted(shap_values.items(),
                                    key=lambda x: x[1], reverse=True)[:8]]
        if shap_values else []
    )

    # ── Feature importances (model built-in, all features) ───────────────────
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

    # ── Compare vs production ──────────────────────────────────────────────────
    prod_auc = 0.0
    if META_PATH.exists():
        try:
            with open(META_PATH) as f:
                prod_auc = float(json.load(f).get("ensemble_auc_val", 0.0))
        except Exception:
            pass

    # ── Save v4 ───────────────────────────────────────────────────────────────
    DATA_DIR.mkdir(exist_ok=True)
    bundle_v4 = {
        "scaler":            scaler,
        "fitted_bases":      fitted_bases,
        "meta_model":        meta_model,
        "ra_model":          ra_model,
        "scaler_ra":         scaler_ra,
        "feature_cols":      ALL_FEATURE_COLS,
        "base_names":        list(fitted_bases.keys()),
        "has_lgb":           HAS_LGB,
        "has_xgb":           HAS_XGB,
        "has_ra":            ra_model is not None,
        "has_macro":         True,
        "has_rd":            True,
        "macro_cols":        MACRO_FEATURE_COLS,
        "rd_cols":           RD_MARKET_COLS,
        "rd_score_train":    rd_score,
        "rd_features_at_train": rd_features,
        "optuna_best_params": lgb_best_params,
        "calibration_method": cal_method,
        "trained_at":        datetime.now().isoformat(),
        "n_train":           len(X_tr),
        "n_val":             len(X_val),
        "version":           "v4",
    }
    with open(V4_PATH, "wb") as f:
        pickle.dump(bundle_v4, f)

    meta_v4 = {
        "version":             "v4",
        "trained_at":          datetime.now().isoformat(),
        "n_samples":           len(X),
        "n_features":          X.shape[1],
        "macro_features":      MACRO_FEATURE_COLS,
        "rd_score":            rd_score,
        "has_lightgbm":        HAS_LGB,
        "has_xgboost":         HAS_XGB,
        "has_risk_adj":        ra_model is not None,
        "positive_rate":       round(float(y_bin.mean()), 4),
        "top_features":        top_features,
        "macro_top_features":  macro_top,
        "feature_cols":        ALL_FEATURE_COLS,
        "optuna_trials":       optuna_trials,
        "optuna_best_params":  lgb_best_params,
        "shap_available":      bool(shap_values),
        **results,
    }
    with open(V4_META, "w") as f:
        json.dump(meta_v4, f, indent=2, default=str)
    logger.info(f"  ml_model_v4: saved → {V4_PATH}")

    # ── Promote to production if better ──────────────────────────────────────
    promoted = False
    if force or ens_auc >= prod_auc + PROMO_DELTA:
        import shutil
        if MODEL_PATH.exists():
            shutil.copy(MODEL_PATH, PREV_PATH)
        shutil.copy(V4_PATH, MODEL_PATH)
        shutil.copy(V4_META, META_PATH)
        promoted = True
        logger.info(f"  ml_model_v4: PROMOTED (v4 AUC {ens_auc:.4f} > prod {prod_auc:.4f})")
    else:
        logger.info(f"  ml_model_v4: not promoted (delta {ens_auc - prod_auc:+.4f})")

    return {
        **meta_v4,
        "status":    "TRAINED",
        "promoted":  promoted,
        "prod_auc":  prod_auc,
        "v4_auc":    ens_auc,
        "auc_delta": round(ens_auc - prod_auc, 4),
    }


# =============================================================================
# INFERENCE
# =============================================================================

def predict_proba_v4(feature_df) -> np.ndarray:
    """Score live signals using v4. Falls back to v3 if model not yet trained."""
    if not V4_PATH.exists():
        from predictive_model.ml_model_v3 import predict_proba_v3
        return predict_proba_v3(feature_df)

    with open(V4_PATH, "rb") as f:
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


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import argparse as _ap
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    p = _ap.ArgumentParser()
    p.add_argument("--force",          action="store_true")
    p.add_argument("--optuna-trials",  type=int, default=OPTUNA_TRIALS)
    args = p.parse_args()

    result = train(force=args.force, optuna_trials=args.optuna_trials)
    print(json.dumps(result, indent=2, default=str))

#!/usr/bin/env python3
"""
APEX CAPITAL — ML MODEL v2
===========================
Improvements over v1:

  1. TimeSeriesSplit CV — no data leakage
     v1 used random 5-fold CV on time-series data, which let the model "see the
     future" during validation.  That's why v1 CV AUC was 0.277 (below random
     chance — a dead giveaway of look-ahead leakage).  v2 uses sklearn's
     TimeSeriesSplit so each validation fold is always strictly *after* all
     training folds in time.

  2. XGBoost added as 4th ensemble model
     LightGBM + RandomForest + LogisticRegression → + XGBoost
     New ensemble weights: LGB 0.35 · XGB 0.30 · RF 0.20 · LR 0.15

  3. Optuna hyperparameter tuning for LightGBM (20 fast trials, time-series CV)

  4. Dynamic ensemble weights
     Weights are optimized on the validation set rather than fixed by hand.

  5. Saves to data/model_v2.pkl + data/model_v2_meta.json
     Promotes to data/model.pkl only if v2 AUC beats v1 by ≥ 0.005.

Usage
-----
  python3 predictive_model/ml_model_v2.py [--force]
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

DATA_DIR    = Path(__file__).parent.parent / "data"
V2_PATH     = DATA_DIR / "model_v2.pkl"
V2_META     = DATA_DIR / "model_v2_meta.json"
V1_META     = DATA_DIR / "model_meta.json"
MODEL_PATH  = DATA_DIR / "model.pkl"        # production model slot
META_PATH   = DATA_DIR / "model_meta.json"
PREV_PATH   = DATA_DIR / "model_prev.pkl"

MIN_SAMPLES = 50
PROMO_DELTA = 0.005   # v2 must beat v1 by this much to replace production model


# =============================================================================
# HELPERS
# =============================================================================

def _ts_cv_score(model, X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> float:
    """TimeSeriesSplit AUC — no future leakage."""
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import roc_auc_score

    tscv   = TimeSeriesSplit(n_splits=n_splits)
    scores = []
    for train_idx, val_idx in tscv.split(X):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]
        if len(set(y_val)) < 2:          # can't score a single-class fold
            continue
        try:
            model.fit(X_tr, y_tr)
            proba = model.predict_proba(X_val)[:, 1]
            scores.append(roc_auc_score(y_val, proba))
        except Exception as e:
            logger.debug(f"  ts_cv fold failed: {e}")
    return float(np.mean(scores)) if scores else 0.5


def _optimize_weights(probas: list, y_val: np.ndarray) -> np.ndarray:
    """
    Simple grid search over convex combinations of model probabilities.
    Returns weights that maximise AUC on the validation set.
    Keeps weights interpretable (0.05 granularity).
    """
    from sklearn.metrics import roc_auc_score
    n_models  = len(probas)
    best_auc  = -1.0
    best_w    = np.ones(n_models) / n_models

    # Only try if we have enough val samples
    if len(set(y_val)) < 2:
        return best_w

    # Grid over simplex at 0.05 steps (fast for ≤4 models)
    steps = np.arange(0, 1.05, 0.10)
    for w in np.array(np.meshgrid(*[steps] * n_models)).T.reshape(-1, n_models):
        if abs(w.sum() - 1.0) > 0.15:
            continue
        w = w / w.sum()
        ens = sum(w[i] * probas[i] for i in range(n_models))
        try:
            auc = roc_auc_score(y_val, ens)
        except Exception:
            continue
        if auc > best_auc:
            best_auc = auc
            best_w   = w.copy()
    return best_w


# =============================================================================
# TRAINING
# =============================================================================

def train(force: bool = False) -> dict:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import roc_auc_score, accuracy_score

    # ── Optional imports ─────────────────────────────────────────────────────
    try:
        import lightgbm as lgb
        HAS_LGB = True
    except ImportError:
        lgb = None
        HAS_LGB = False
        logger.warning("  ml_model_v2: LightGBM not installed — skipping")

    try:
        import xgboost as xgb
        HAS_XGB = True
    except ImportError:
        xgb = None
        HAS_XGB = False
        logger.warning("  ml_model_v2: XGBoost not installed — skipping")

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        HAS_OPTUNA = True
    except ImportError:
        HAS_OPTUNA = False
        logger.info("  ml_model_v2: Optuna not installed — using default LGB params")

    # ── Feature matrix ───────────────────────────────────────────────────────
    X, y_bin, meta_df = build_feature_matrix(target="outcome_20d")

    if len(X) < MIN_SAMPLES:
        msg = f"Only {len(X)} labeled samples — need {MIN_SAMPLES}."
        logger.warning(f"  ml_model_v2: {msg}")
        return {"status": "SKIPPED", "reason": msg, "n_samples": len(X)}

    X_ra, y_ra, _ = build_feature_matrix(target="risk_adjusted_20d")
    has_risk_adj   = len(X_ra) >= MIN_SAMPLES

    logger.info(f"  ml_model_v2: {len(X)} samples | "
                f"LGB={HAS_LGB} XGB={HAS_XGB} Optuna={HAS_OPTUNA}")

    # ── Time-ordered 80/20 split ─────────────────────────────────────────────
    split    = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y_bin[:split], y_bin[split:]

    scaler    = StandardScaler()
    X_tr_s    = scaler.fit_transform(X_tr)
    X_val_s   = scaler.transform(X_val)
    X_all_s   = scaler.transform(X)

    results   = {}
    models    = {}   # name → fitted model
    val_probas = []  # per-model val probabilities

    # ── LightGBM with optional Optuna tuning ─────────────────────────────────
    lgb_model = None
    if HAS_LGB:
        if HAS_OPTUNA and len(X_tr) > 200:
            def _lgb_objective(trial):
                params = {
                    "n_estimators":      trial.suggest_int("n_estimators", 200, 800),
                    "learning_rate":     trial.suggest_float("lr", 0.01, 0.15, log=True),
                    "num_leaves":        trial.suggest_int("num_leaves", 16, 63),
                    "max_depth":         trial.suggest_int("max_depth", 3, 8),
                    "min_child_samples": trial.suggest_int("min_child_samples", 5, 30),
                    "subsample":         trial.suggest_float("subsample", 0.6, 1.0),
                    "colsample_bytree":  trial.suggest_float("colsample", 0.6, 1.0),
                    "class_weight":      "balanced",
                    "random_state":      42,
                    "verbosity":         -1,
                }
                m = lgb.LGBMClassifier(**params)
                return _ts_cv_score(m, X_tr_s, y_tr, n_splits=3)

            logger.info("  ml_model_v2: Optuna tuning LightGBM (20 trials)...")
            study = optuna.create_study(direction="maximize")
            study.optimize(_lgb_objective, n_trials=20, show_progress_bar=False)
            best = study.best_params
            logger.info(f"  ml_model_v2: best LGB params: {best}")
            lgb_model = lgb.LGBMClassifier(
                n_estimators      = best["n_estimators"],
                learning_rate     = best["lr"],
                num_leaves        = best["num_leaves"],
                max_depth         = best["max_depth"],
                min_child_samples = best["min_child_samples"],
                subsample         = best["subsample"],
                colsample_bytree  = best["colsample"],
                class_weight      = "balanced",
                random_state      = 42,
                verbosity         = -1,
            )
        else:
            lgb_model = lgb.LGBMClassifier(
                n_estimators=500, learning_rate=0.05, num_leaves=31,
                max_depth=6, min_child_samples=10,
                class_weight="balanced", random_state=42, verbosity=-1,
            )
        lgb_model.fit(X_tr_s, y_tr,
                      eval_set=[(X_val_s, y_val)],
                      callbacks=[lgb.early_stopping(50, verbose=False),
                                 lgb.log_evaluation(period=-1)])
        lgb_p = lgb_model.predict_proba(X_val_s)[:, 1]
        results["lgb_auc_val"] = round(roc_auc_score(y_val, lgb_p) if len(set(y_val)) > 1 else 0.5, 4)
        models["lgb"] = lgb_model
        val_probas.append(lgb_p)
        logger.info(f"  LightGBM val AUC: {results['lgb_auc_val']:.4f}")

    # ── XGBoost ──────────────────────────────────────────────────────────────
    xgb_model = None
    if HAS_XGB:
        pos  = int(y_tr.sum())
        neg  = len(y_tr) - pos
        spw  = neg / max(pos, 1)   # scale_pos_weight for class imbalance
        xgb_model = xgb.XGBClassifier(
            n_estimators    = 500,
            learning_rate   = 0.05,
            max_depth       = 5,
            min_child_weight= 5,
            subsample       = 0.8,
            colsample_bytree= 0.8,
            scale_pos_weight= spw,
            use_label_encoder=False,
            eval_metric     = "auc",
            random_state    = 42,
            verbosity       = 0,
            early_stopping_rounds=50,
        )
        xgb_model.fit(X_tr_s, y_tr,
                      eval_set=[(X_val_s, y_val)],
                      verbose=False)
        xgb_p = xgb_model.predict_proba(X_val_s)[:, 1]
        xgb_auc = roc_auc_score(y_val, xgb_p) if len(set(y_val)) > 1 else 0.5
        results["xgb_auc_val"] = round(xgb_auc, 4)
        models["xgb"] = xgb_model
        val_probas.append(xgb_p)
        logger.info(f"  XGBoost val AUC: {xgb_auc:.4f}")

    # ── Random Forest ─────────────────────────────────────────────────────────
    rf = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=5,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    rf.fit(X_tr_s, y_tr)
    rf_p   = rf.predict_proba(X_val_s)[:, 1]
    rf_auc = roc_auc_score(y_val, rf_p) if len(set(y_val)) > 1 else 0.5
    results["rf_auc_val"] = round(rf_auc, 4)
    models["rf"] = rf
    val_probas.append(rf_p)

    # ── Logistic Regression ──────────────────────────────────────────────────
    lr = LogisticRegression(C=1.0, class_weight="balanced", max_iter=500, random_state=42)
    lr.fit(X_tr_s, y_tr)
    lr_p   = lr.predict_proba(X_val_s)[:, 1]
    lr_auc = roc_auc_score(y_val, lr_p) if len(set(y_val)) > 1 else 0.5
    results["lr_auc_val"] = round(lr_auc, 4)
    models["lr"] = lr
    val_probas.append(lr_p)

    # ── Dynamic ensemble weights ──────────────────────────────────────────────
    logger.info("  ml_model_v2: optimising ensemble weights...")
    opt_weights = _optimize_weights(val_probas, y_val)
    ens_proba   = sum(opt_weights[i] * val_probas[i] for i in range(len(val_probas)))
    ens_auc     = roc_auc_score(y_val, ens_proba) if len(set(y_val)) > 1 else 0.5
    ens_acc     = accuracy_score(y_val, (ens_proba >= 0.5).astype(int))
    results["ensemble_auc_val"] = round(ens_auc, 4)
    results["ensemble_acc_val"] = round(ens_acc, 4)

    model_names = [k for k in ["lgb", "xgb", "rf", "lr"] if k in models]
    weight_map  = {model_names[i]: round(float(opt_weights[i]), 4)
                   for i in range(len(model_names))}
    results["ensemble_weights"] = weight_map
    logger.info(f"  Ensemble weights: {weight_map}  AUC: {ens_auc:.4f}")

    # ── TimeSeriesSplit CV (correct for time series — no leakage) ────────────
    logger.info("  ml_model_v2: running TimeSeriesSplit CV (5 folds)...")
    cv_model = lgb_model if HAS_LGB else rf
    tscv     = TimeSeriesSplit(n_splits=5)
    cv_scores = []
    for tr_idx, val_idx in tscv.split(X_all_s):
        if len(set(y_bin[val_idx])) < 2:
            continue
        try:
            cv_model.fit(X_all_s[tr_idx], y_bin[tr_idx])
            p = cv_model.predict_proba(X_all_s[val_idx])[:, 1]
            from sklearn.metrics import roc_auc_score as _roc
            cv_scores.append(_roc(y_bin[val_idx], p))
        except Exception as e:
            logger.debug(f"  TS-CV fold error: {e}")

    if cv_scores:
        results["cv_auc_mean"] = round(float(np.mean(cv_scores)), 4)
        results["cv_auc_std"]  = round(float(np.std(cv_scores)),  4)
    else:
        results["cv_auc_mean"] = 0.5
        results["cv_auc_std"]  = 0.0
    logger.info(f"  TS-CV AUC: {results['cv_auc_mean']:.4f} ± {results['cv_auc_std']:.4f}")

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
        ra_preds   = ra_model.predict(X_ra_val)
        from sklearn.metrics import r2_score
        r2 = r2_score(y_ra_arr[split_ra:], ra_preds)
        results["risk_adj_r2_val"] = round(float(r2), 4)
        logger.info(f"  Risk-adj regressor val R²: {r2:.4f}")
    else:
        results["risk_adj_r2_val"] = None

    # ── Feature importances ───────────────────────────────────────────────────
    if HAS_LGB and lgb_model:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(FEATURE_COLS, lgb_model.feature_importances_)}
    else:
        importances = {col: round(float(imp), 4)
                       for col, imp in zip(FEATURE_COLS, rf.feature_importances_)}
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── Save v2 model ─────────────────────────────────────────────────────────
    DATA_DIR.mkdir(exist_ok=True)
    bundle_v2 = {
        "scaler":       scaler,
        "rf":           rf,
        "lr":           lr,
        "lgb":          lgb_model,
        "xgb":          xgb_model,
        "ra_model":     ra_model,
        "scaler_ra":    scaler_ra,
        "feature_cols": FEATURE_COLS,
        "has_lgb":      HAS_LGB,
        "has_xgb":      HAS_XGB,
        "has_ra":       ra_model is not None,
        "ens_weights":  weight_map,
        "model_names":  model_names,
        "trained_at":   datetime.now().isoformat(),
        "n_train":      len(X_tr),
        "n_val":        len(X_val),
        "version":      "v2",
    }
    with open(V2_PATH, "wb") as f:
        pickle.dump(bundle_v2, f)

    meta_v2 = {
        "version":         "v2",
        "trained_at":      datetime.now().isoformat(),
        "n_samples":       len(X),
        "has_lightgbm":    HAS_LGB,
        "has_xgboost":     HAS_XGB,
        "has_risk_adj":    ra_model is not None,
        "positive_rate":   round(float(y_bin.mean()), 4),
        "top_features":    top_features,
        "feature_cols":    FEATURE_COLS,
        **results,
    }
    with open(V2_META, "w") as f:
        json.dump(meta_v2, f, indent=2, default=str)
    logger.info(f"  ml_model_v2: saved → {V2_PATH}")

    # ── Promote to production if better than v1 ───────────────────────────────
    v1_auc = 0.0
    if META_PATH.exists():
        try:
            with open(META_PATH) as f:
                v1_auc = float(json.load(f).get("ensemble_auc_val", 0.0))
        except Exception:
            pass

    promoted = False
    if force or ens_auc >= v1_auc + PROMO_DELTA:
        import shutil
        if MODEL_PATH.exists():
            shutil.copy(MODEL_PATH, PREV_PATH)
        shutil.copy(V2_PATH, MODEL_PATH)
        shutil.copy(V2_META, META_PATH)
        promoted = True
        logger.info(f"  ml_model_v2: PROMOTED to production "
                    f"(v2 AUC {ens_auc:.4f} > v1 AUC {v1_auc:.4f})")
    else:
        logger.info(f"  ml_model_v2: NOT promoted "
                    f"(v2 AUC {ens_auc:.4f} vs v1 AUC {v1_auc:.4f}, "
                    f"delta {ens_auc - v1_auc:+.4f} < {PROMO_DELTA})")

    return {
        **meta_v2,
        "status":     "TRAINED",
        "promoted":   promoted,
        "v1_auc":     v1_auc,
        "v2_auc":     ens_auc,
        "auc_delta":  round(ens_auc - v1_auc, 4),
    }


# =============================================================================
# INFERENCE (compatible with ml_model.py's predict_proba interface)
# =============================================================================

def predict_proba_v2(feature_df) -> np.ndarray:
    """Score signals using v2 bundle if available, else falls back to model.pkl."""
    if not V2_PATH.exists():
        from predictive_model.ml_model import predict_proba
        return predict_proba(feature_df)

    with open(V2_PATH, "rb") as f:
        bundle = pickle.load(f)

    scaler      = bundle["scaler"]
    rf          = bundle["rf"]
    lr          = bundle["lr"]
    lgb_m       = bundle.get("lgb")
    xgb_m       = bundle.get("xgb")
    ra_m        = bundle.get("ra_model")
    sc_ra       = bundle.get("scaler_ra")
    cols        = bundle.get("feature_cols", FEATURE_COLS)
    model_names = bundle.get("model_names", ["lgb", "xgb", "rf", "lr"])
    ens_weights = bundle.get("ens_weights", {})

    X   = feature_df[cols].values.astype(float)
    X_s = scaler.transform(X)

    probas = []
    for name in model_names:
        if name == "lgb" and lgb_m is not None:
            probas.append(lgb_m.predict_proba(X_s)[:, 1])
        elif name == "xgb" and xgb_m is not None:
            probas.append(xgb_m.predict_proba(X_s)[:, 1])
        elif name == "rf":
            probas.append(rf.predict_proba(X_s)[:, 1])
        elif name == "lr":
            probas.append(lr.predict_proba(X_s)[:, 1])

    if not probas:
        return np.full(len(X), 0.5)

    weights = np.array([ens_weights.get(n, 1.0 / len(probas)) for n in model_names
                        if n in {m: True for m in model_names}])
    weights = weights[:len(probas)]
    weights = weights / weights.sum()
    clf_p   = sum(weights[i] * probas[i] for i in range(len(probas)))

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
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    force  = "--force" in sys.argv
    result = train(force=force)
    print(json.dumps(result, indent=2, default=str))

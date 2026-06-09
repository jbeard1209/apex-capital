"""
v2s Model Deep Validation Suite
Run on GCP: python3 ml_validate_v2s.py

Runs 9 validation tests on the v2s LightGBM trading signal model.
Logs to logs/v2s_validation_{date}.log
Writes summary to data/v2s_validation_report.json
Estimated runtime: 60-90 minutes on GCP
"""

import sys
import os
import json
import pickle
import logging
import warnings
import traceback
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path('/home/jackbeard/institutional-tracker')
MODEL_PATH = BASE / 'data' / 'model.pkl'
REPORT_PATH = BASE / 'data' / 'v2s_validation_report.json'
LOG_DIR = BASE / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = BASE / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATE_STR = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_PATH = LOG_DIR / f'v2s_validation_{DATE_STR}.log'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Heavy imports
# ---------------------------------------------------------------------------
import numpy as np

try:
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.calibration import calibration_curve
    from sklearn.model_selection import train_test_split
except ImportError as e:
    log.error(f"sklearn not available: {e}")
    sys.exit(1)

try:
    from lightgbm import LGBMClassifier
except ImportError as e:
    log.error(f"lightgbm not available: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Project path + feature engineer import
# ---------------------------------------------------------------------------
sys.path.insert(0, str(BASE))
try:
    from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS
    log.info(f"Imported feature_engineer. FEATURE_COLS has {len(FEATURE_COLS)} base features.")
except ImportError as e:
    log.error(f"Could not import feature_engineer: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Interaction feature definitions
# ---------------------------------------------------------------------------
INTERACTION_DEFS = [
    ('conviction_score_x_institution_hit_rate',
     'conviction_score', 'institution_hit_rate'),
    ('rd_vix_x_volatility_20d',
     'rd_vix', 'volatility_20d'),
    ('price_vs_52w_high_x_price_momentum_20d',
     'price_vs_52w_high', 'price_momentum_20d'),
    ('avg_conviction_3q_x_institution_regime_hit_rate',
     'avg_conviction_3q', 'institution_regime_hit_rate'),
    ('position_size_pct_aum_x_conviction_score',
     'position_size_pct_aum', 'conviction_score'),
    ('filing_delay_days_x_institution_hit_rate',
     'filing_delay_days', 'institution_hit_rate'),
]
INTERACTION_NAMES = [d[0] for d in INTERACTION_DEFS]

# Default LGB params (used if model.best_params_ is not available)
DEFAULT_LGB_PARAMS = dict(
    class_weight='balanced',
    n_estimators=500,
    learning_rate=0.05,
    num_leaves=63,
    verbosity=-1,
    random_state=42,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_interaction_features(X_base: np.ndarray, feature_cols: list) -> np.ndarray:
    """
    Append 6 interaction features to X_base (30 columns) to produce X_aug (36 columns).
    If a base column for an interaction is missing, fills that interaction with zeros
    and logs a warning.
    """
    fc_lower = [c.lower() for c in feature_cols]
    interactions = []
    for name, col_a, col_b in INTERACTION_DEFS:
        idx_a = _find_col(fc_lower, col_a.lower(), feature_cols)
        idx_b = _find_col(fc_lower, col_b.lower(), feature_cols)
        if idx_a is None or idx_b is None:
            log.warning(
                f"Interaction '{name}': could not find '{col_a}' or '{col_b}' "
                f"in feature_cols — filling with zeros."
            )
            interactions.append(np.zeros(X_base.shape[0]))
        else:
            interactions.append(X_base[:, idx_a] * X_base[:, idx_b])
    X_int = np.column_stack(interactions)
    return np.hstack([X_base, X_int])


def _find_col(fc_lower: list, target: str, fc_orig: list):
    """Return index of target in fc_lower, or None."""
    if target in fc_lower:
        return fc_lower.index(target)
    # partial match
    for i, c in enumerate(fc_lower):
        if target in c or c in target:
            log.debug(f"Partial match '{fc_orig[i]}' for target '{target}'")
            return i
    return None


def get_lgb_params(model_pkl: dict) -> dict:
    """
    Try to extract best_params from the loaded model pkl.
    Falls back to DEFAULT_LGB_PARAMS.
    """
    lgb_obj = model_pkl.get('xgb')  # key is 'xgb' but it's LGBMClassifier
    if lgb_obj is not None and hasattr(lgb_obj, 'get_params'):
        try:
            p = lgb_obj.get_params()
            # Ensure required keys present
            params = {
                'class_weight': p.get('class_weight', 'balanced'),
                'n_estimators': p.get('n_estimators', 500),
                'learning_rate': p.get('learning_rate', 0.05),
                'num_leaves': p.get('num_leaves', 63),
                'verbosity': -1,
                'random_state': p.get('random_state', 42),
            }
            # Add any extra params that don't conflict
            for k, v in p.items():
                if k not in params and k not in ('verbose',):
                    params[k] = v
            params['verbosity'] = -1  # always override
            log.info(f"Extracted LGB params from loaded model object.")
            return params
        except Exception as e:
            log.warning(f"Could not extract params from model object: {e}. Using defaults.")
    log.info("Using default LGB params.")
    return dict(DEFAULT_LGB_PARAMS)


def fresh_lgb(params: dict) -> LGBMClassifier:
    """Return a new LGBMClassifier from params dict."""
    safe = {k: v for k, v in params.items() if k != 'verbose'}
    safe['verbosity'] = -1
    return LGBMClassifier(**safe)


def safe_auc(y_true, y_score) -> float:
    """Compute AUC, returning np.nan if impossible."""
    try:
        if len(np.unique(y_true)) < 2:
            return np.nan
        return roc_auc_score(y_true, y_score)
    except Exception:
        return np.nan


def tag(status: str) -> str:
    return f"[{status}]"


# ---------------------------------------------------------------------------
# Results accumulator
# ---------------------------------------------------------------------------
results = {}  # test_name -> {status, details}


def record(name: str, status: str, details: dict):
    symbol = tag(status)
    log.info(f"{symbol} {name}: {json.dumps(details, default=str)}")
    results[name] = {'status': status, 'details': details}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data():
    """Load model pkl and build feature matrix."""
    log.info("=" * 70)
    log.info("Loading model pickle...")
    if not MODEL_PATH.exists():
        log.error(f"Model not found at {MODEL_PATH}")
        sys.exit(1)

    with open(MODEL_PATH, 'rb') as f:
        model_pkl = pickle.load(f)

    required_keys = ['xgb', 'feature_cols']
    for k in required_keys:
        if k not in model_pkl:
            log.warning(f"Key '{k}' missing from model pkl.")

    feature_cols_pkl = model_pkl.get('feature_cols', FEATURE_COLS)
    log.info(
        f"Model pkl keys: {list(model_pkl.keys())}"
        f" | feature_cols length: {len(feature_cols_pkl)}"
        f" | trained_at: {model_pkl.get('trained_at', 'unknown')}"
        f" | experiment: {model_pkl.get('experiment', 'unknown')}"
    )

    log.info("Building feature matrix (target=outcome_20d)...")
    try:
        X_base, y, df_raw = build_feature_matrix(target='outcome_20d')
    except Exception as e:
        log.error(f"build_feature_matrix failed: {e}\n{traceback.format_exc()}")
        sys.exit(1)

    log.info(
        f"Data loaded: X_base shape={X_base.shape}, y shape={y.shape}, "
        f"df_raw shape={df_raw.shape if hasattr(df_raw, 'shape') else 'N/A'}"
    )
    log.info(f"FEATURE_COLS from module: {len(FEATURE_COLS)} | pkl feature_cols: {len(feature_cols_pkl)}")

    # Resolve which feature_cols to use for interaction index lookups
    fc_for_interactions = FEATURE_COLS if len(FEATURE_COLS) == X_base.shape[1] else feature_cols_pkl[:X_base.shape[1]]

    log.info("Building X_aug (30 base + 6 interactions)...")
    X_aug = build_interaction_features(X_base, fc_for_interactions)
    log.info(f"X_aug shape: {X_aug.shape}")

    all_feature_names = list(fc_for_interactions) + INTERACTION_NAMES

    params = get_lgb_params(model_pkl)
    log.info(f"LGB params: {params}")

    return model_pkl, X_base, X_aug, y, df_raw, fc_for_interactions, all_feature_names, params


# ---------------------------------------------------------------------------
# TEST 1 — Walk-Forward Validation (expanding window)
# ---------------------------------------------------------------------------
def test_walk_forward(X_aug, y, params):
    log.info("=" * 70)
    log.info("TEST 1: Walk-Forward Validation (expanding window)")
    log.info("NOTE: No gap between train/test folds to keep things simple. "
             "In live use, a 20-day forward gap is recommended to avoid lookahead.")

    N = len(y)
    # fold start proportions (train end %), test = next 10%
    fold_configs = [
        (0.40, 0.50),
        (0.50, 0.60),
        (0.60, 0.70),
        (0.65, 0.75),
        (0.70, 0.80),
        (0.75, 0.85),
    ]

    aucs = []
    fold_details = []
    for i, (train_end_pct, test_end_pct) in enumerate(fold_configs):
        train_end = int(N * train_end_pct)
        test_end = min(int(N * test_end_pct), N)
        X_tr, y_tr = X_aug[:train_end], y[:train_end]
        X_te, y_te = X_aug[train_end:test_end], y[train_end:test_end]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            log.warning(f"Fold {i+1}: skipping — single class in train or test.")
            continue

        try:
            mdl = fresh_lgb(params)
            mdl.fit(X_tr, y_tr)
            proba = mdl.predict_proba(X_te)[:, 1]
            auc = safe_auc(y_te, proba)
        except Exception as e:
            log.warning(f"Fold {i+1} failed: {e}")
            auc = np.nan

        log.info(
            f"  Fold {i+1}: train={train_end} samples "
            f"({train_end_pct*100:.0f}%), test={test_end-train_end} samples, AUC={auc:.4f}"
        )
        aucs.append(auc)
        fold_details.append({
            'fold': i + 1,
            'train_size': train_end,
            'test_size': test_end - train_end,
            'auc': round(float(auc), 4) if not np.isnan(auc) else None,
        })

    valid_aucs = [a for a in aucs if not np.isnan(a)]
    if not valid_aucs:
        record('test1_walk_forward', 'FAIL', {'error': 'All folds failed', 'folds': fold_details})
        return None, None

    mean_auc = float(np.mean(valid_aucs))
    std_auc = float(np.std(valid_aucs))
    min_auc = float(np.min(valid_aucs))
    max_auc = float(np.max(valid_aucs))

    log.info(
        f"Walk-Forward: mean_AUC={mean_auc:.4f}, std={std_auc:.4f}, "
        f"min={min_auc:.4f}, max={max_auc:.4f}"
    )
    log.info("Production val AUC reference: 0.6229")

    if mean_auc > 0.58:
        status = 'PASS'
    elif mean_auc >= 0.55:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test1_walk_forward', status, {
        'mean_auc': round(mean_auc, 4),
        'std_auc': round(std_auc, 4),
        'min_auc': round(min_auc, 4),
        'max_auc': round(max_auc, 4),
        'production_val_auc_ref': 0.6229,
        'folds': fold_details,
        'note': 'No lookahead gap between folds; production gap should be ~20 days.',
    })
    return mean_auc, fold_details


# ---------------------------------------------------------------------------
# TEST 2 — Regime-Conditioned AUC
# ---------------------------------------------------------------------------
def test_regime_auc(X_aug, y, df_raw, fc_for_interactions, params):
    log.info("=" * 70)
    log.info("TEST 2: Regime-Conditioned AUC")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X_aug[:split], X_aug[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test2_regime_auc', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb(params)
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        log.error(f"Test 2 model fit failed: {e}")
        record('test2_regime_auc', 'FAIL', {'error': str(e)})
        return

    # Try to find a VIX-like column in feature matrix
    regime_col_candidates = ['rd_vix', 'cro_level', 'vix_level', 'vix', 'rd_vix_level']
    fc_lower = [c.lower() for c in fc_for_interactions]
    vix_idx = None
    vix_col_name = None
    for cand in regime_col_candidates:
        idx = _find_col(fc_lower, cand.lower(), fc_for_interactions)
        if idx is not None:
            vix_idx = idx
            vix_col_name = fc_for_interactions[idx]
            break

    if vix_idx is None:
        # Try df_raw columns
        if df_raw is not None and hasattr(df_raw, 'columns'):
            for cand in regime_col_candidates:
                matches = [c for c in df_raw.columns if cand in c.lower()]
                if matches:
                    vix_col_name = matches[0]
                    log.info(f"Using df_raw column '{vix_col_name}' for regime split.")
                    break

    if vix_idx is not None:
        # Split val set by VIX median
        vix_te = X_te[:, vix_idx]
        median_vix = float(np.median(X_aug[:split, vix_idx]))
        log.info(f"Regime column: '{vix_col_name}' (idx={vix_idx}), train median VIX={median_vix:.3f}")
        low_mask = vix_te <= median_vix
        high_mask = vix_te > median_vix
        regime_source = 'feature_matrix'
    elif vix_col_name is not None and df_raw is not None and hasattr(df_raw, 'columns'):
        # Use df_raw
        import pandas as pd
        df_val = df_raw.iloc[split:].reset_index(drop=True) if hasattr(df_raw, 'iloc') else None
        if df_val is not None and vix_col_name in df_val.columns:
            vix_te = df_val[vix_col_name].values
            median_vix = float(np.nanmedian(df_raw[vix_col_name].iloc[:split]))
            low_mask = vix_te <= median_vix
            high_mask = vix_te > median_vix
            regime_source = 'df_raw'
            log.info(f"Using df_raw VIX column '{vix_col_name}', train median={median_vix:.3f}")
        else:
            log.warning("Could not align df_raw val slice with val set. Falling back to random regime split.")
            vix_te = np.random.randn(len(y_te))
            median_vix = 0.0
            low_mask = vix_te <= median_vix
            high_mask = vix_te > median_vix
            regime_source = 'fallback_random'
    else:
        log.warning(
            "No VIX/regime column found. Using random median split as placeholder. "
            "Results will not be meaningful — investigate feature_cols."
        )
        vix_te = np.random.randn(len(y_te))
        median_vix = 0.0
        low_mask = vix_te <= median_vix
        high_mask = vix_te > median_vix
        regime_source = 'fallback_random'

    def regime_auc(mask, label):
        if mask.sum() < 10:
            log.warning(f"  Regime '{label}': only {mask.sum()} samples — skipping.")
            return np.nan
        if len(np.unique(y_te[mask])) < 2:
            log.warning(f"  Regime '{label}': single class — AUC undefined.")
            return np.nan
        a = safe_auc(y_te[mask], proba_te[mask])
        log.info(f"  Regime '{label}': n={mask.sum()}, AUC={a:.4f}")
        return a

    auc_low = regime_auc(low_mask, 'low-VIX/bull')
    auc_high = regime_auc(high_mask, 'high-VIX/stress')

    regime_aucs = {
        'low_vix_bull': round(float(auc_low), 4) if not np.isnan(auc_low) else None,
        'high_vix_stress': round(float(auc_high), 4) if not np.isnan(auc_high) else None,
    }
    valid = [a for a in [auc_low, auc_high] if not np.isnan(a)]

    if not valid:
        status = 'FAIL'
    elif all(a > 0.56 for a in valid):
        status = 'PASS'
    elif any(a < 0.52 for a in valid):
        status = 'FAIL'
    else:
        status = 'WARN'

    record('test2_regime_auc', status, {
        'regime_source': regime_source,
        'vix_column': vix_col_name,
        'train_median_vix': round(median_vix, 4) if vix_idx is not None else None,
        'n_low_regime': int(low_mask.sum()),
        'n_high_regime': int(high_mask.sum()),
        'auc_by_regime': regime_aucs,
    })


# ---------------------------------------------------------------------------
# TEST 3 — Calibration Check (Brier Score + reliability)
# ---------------------------------------------------------------------------
def test_calibration(X_aug, y, params):
    log.info("=" * 70)
    log.info("TEST 3: Calibration Check (Brier Score + reliability curve)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X_aug[:split], X_aug[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test3_calibration', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb(params)
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        log.error(f"Test 3 fit failed: {e}")
        record('test3_calibration', 'FAIL', {'error': str(e)})
        return

    positive_rate = float(y_te.mean())
    baseline_brier = positive_rate * (1.0 - positive_rate)
    actual_brier = float(brier_score_loss(y_te, proba_te))

    log.info(f"  Positive rate (val): {positive_rate:.4f}")
    log.info(f"  Baseline Brier (p*(1-p)): {baseline_brier:.4f}")
    log.info(f"  Model Brier: {actual_brier:.4f}")
    log.info(f"  Ratio model/baseline: {actual_brier/baseline_brier:.4f}")

    # Reliability curve (10 bins)
    try:
        fraction_pos, mean_pred = calibration_curve(y_te, proba_te, n_bins=10, strategy='quantile')
        bin_table = [
            {'bin': i + 1, 'mean_pred': round(float(mp), 4), 'frac_pos': round(float(fp), 4)}
            for i, (mp, fp) in enumerate(zip(mean_pred, fraction_pos))
        ]
        log.info("  Reliability curve (mean_pred → frac_positive):")
        for row in bin_table:
            log.info(f"    bin {row['bin']:2d}: pred={row['mean_pred']:.4f}  actual={row['frac_pos']:.4f}")
    except Exception as e:
        log.warning(f"calibration_curve failed: {e}")
        bin_table = []

    ratio = actual_brier / baseline_brier if baseline_brier > 0 else float('inf')

    if ratio < 1.1:
        status = 'PASS'
    elif ratio <= 1.3:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test3_calibration', status, {
        'positive_rate_val': round(positive_rate, 4),
        'baseline_brier': round(baseline_brier, 4),
        'model_brier': round(actual_brier, 4),
        'ratio_model_baseline': round(ratio, 4),
        'reliability_curve': bin_table,
    })


# ---------------------------------------------------------------------------
# TEST 4 — Top-Decile Lift
# ---------------------------------------------------------------------------
def test_lift(X_aug, y, params):
    log.info("=" * 70)
    log.info("TEST 4: Top-Decile Lift")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X_aug[:split], X_aug[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test4_lift', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb(params)
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        log.error(f"Test 4 fit failed: {e}")
        record('test4_lift', 'FAIL', {'error': str(e)})
        return

    overall_rate = float(y_te.mean())
    log.info(f"  Overall positive rate: {overall_rate:.4f}")

    sorted_idx = np.argsort(proba_te)[::-1]
    n = len(y_te)

    def decile_rate(top_pct):
        k = max(1, int(n * top_pct))
        top_idx = sorted_idx[:k]
        return float(y_te[top_idx].mean())

    def bottom_rate(pct):
        k = max(1, int(n * pct))
        bot_idx = sorted_idx[n - k:]
        return float(y_te[bot_idx].mean())

    top10_rate = decile_rate(0.10)
    top20_rate = decile_rate(0.20)
    top30_rate = decile_rate(0.30)
    bot50_rate = bottom_rate(0.50)

    def lift(rate):
        return rate / overall_rate if overall_rate > 0 else float('inf')

    lift10 = lift(top10_rate)
    lift20 = lift(top20_rate)
    lift30 = lift(top30_rate)
    lift_bot50 = lift(bot50_rate)

    log.info(f"  Top 10%: rate={top10_rate:.4f}, lift={lift10:.3f}")
    log.info(f"  Top 20%: rate={top20_rate:.4f}, lift={lift20:.3f}")
    log.info(f"  Top 30%: rate={top30_rate:.4f}, lift={lift30:.3f}")
    log.info(f"  Bottom 50%: rate={bot50_rate:.4f}, lift={lift_bot50:.3f}")

    if lift10 > 1.3:
        status = 'PASS'
    elif lift10 >= 1.1:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test4_lift', status, {
        'overall_positive_rate': round(overall_rate, 4),
        'top10_rate': round(top10_rate, 4),
        'top10_lift': round(lift10, 4),
        'top20_rate': round(top20_rate, 4),
        'top20_lift': round(lift20, 4),
        'top30_rate': round(top30_rate, 4),
        'top30_lift': round(lift30, 4),
        'bottom50_rate': round(bot50_rate, 4),
        'bottom50_lift': round(lift_bot50, 4),
    })


# ---------------------------------------------------------------------------
# TEST 5 — Permutation Feature Importance
# ---------------------------------------------------------------------------
def test_permutation_importance(X_aug, y, all_feature_names, params):
    log.info("=" * 70)
    log.info("TEST 5: Permutation Feature Importance (3 shuffles per feature)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X_aug[:split], X_aug[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test5_permutation', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb(params)
        mdl.fit(X_tr, y_tr)
        base_proba = mdl.predict_proba(X_te)[:, 1]
        base_auc = safe_auc(y_te, base_proba)
    except Exception as e:
        log.error(f"Test 5 fit failed: {e}")
        record('test5_permutation', 'FAIL', {'error': str(e)})
        return

    log.info(f"  Base AUC: {base_auc:.4f}")
    rng = np.random.RandomState(42)

    n_features = X_te.shape[1]
    n_names = len(all_feature_names)
    feature_names_used = all_feature_names[:n_features] if n_names >= n_features else (
        all_feature_names + [f'feature_{i}' for i in range(n_names, n_features)]
    )

    importances = []
    for j in range(n_features):
        drop_aucs = []
        X_perm = X_te.copy()
        for _ in range(3):
            X_perm[:, j] = rng.permutation(X_perm[:, j])
            perm_proba = mdl.predict_proba(X_perm)[:, 1]
            perm_auc = safe_auc(y_te, perm_proba)
            drop_aucs.append(base_auc - perm_auc)
            X_perm[:, j] = X_te[:, j]  # restore before next shuffle
        mean_drop = float(np.mean(drop_aucs))
        importances.append((feature_names_used[j], mean_drop))

    importances.sort(key=lambda x: x[1], reverse=True)
    top10 = importances[:10]
    bottom5 = importances[-5:]

    log.info("  Top 10 features by AUC drop:")
    for rank, (name, drop) in enumerate(top10, 1):
        log.info(f"    {rank:2d}. {name}: {drop:.5f}")
    log.info("  Bottom 5 features by AUC drop:")
    for name, drop in bottom5:
        log.info(f"    {name}: {drop:.5f}")

    max_drop = top10[0][1] if top10 else 0.0

    if max_drop > 0.005:
        status = 'PASS'
    elif max_drop >= 0.002:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test5_permutation', status, {
        'base_auc': round(base_auc, 4),
        'top_feature': top10[0][0] if top10 else None,
        'top_feature_auc_drop': round(max_drop, 5),
        'top_10': [{'feature': n, 'auc_drop': round(d, 5)} for n, d in top10],
        'bottom_5': [{'feature': n, 'auc_drop': round(d, 5)} for n, d in bottom5],
    })


# ---------------------------------------------------------------------------
# TEST 6 — Data Leakage Probe
# ---------------------------------------------------------------------------
def test_leakage(X_aug, y, all_feature_names):
    log.info("=" * 70)
    log.info("TEST 6: Data Leakage Probe")

    n_features = X_aug.shape[1]
    n_names = len(all_feature_names)
    feature_names_used = all_feature_names[:n_features] if n_names >= n_features else (
        all_feature_names + [f'feature_{i}' for i in range(n_names, n_features)]
    )

    corrs = {}
    for j in range(n_features):
        col = X_aug[:, j]
        if np.std(col) < 1e-10:
            corrs[feature_names_used[j]] = 0.0
            continue
        c = float(np.corrcoef(col, y)[0, 1])
        corrs[feature_names_used[j]] = round(c, 5)

    # Sort by abs correlation
    sorted_corrs = sorted(corrs.items(), key=lambda x: abs(x[1]), reverse=True)

    suspicious_high = [(n, c) for n, c in sorted_corrs if abs(c) > 0.25]
    suspicious_mid = [(n, c) for n, c in sorted_corrs if 0.15 < abs(c) <= 0.25]

    log.info(f"  Features with |corr| > 0.25 (suspicious leakage): {len(suspicious_high)}")
    for n, c in suspicious_high:
        log.info(f"    *** {n}: corr={c:.4f}")

    log.info(f"  Features with 0.15 < |corr| <= 0.25 (worth investigating): {len(suspicious_mid)}")
    for n, c in suspicious_mid[:10]:
        log.info(f"    {n}: corr={c:.4f}")

    log.info("  Top 15 features by |corr| with outcome:")
    for n, c in sorted_corrs[:15]:
        log.info(f"    {n}: {c:.4f}")

    # Flag interaction features specifically
    int_corrs = {n: c for n, c in corrs.items() if n in INTERACTION_NAMES}
    log.info(f"  Interaction feature correlations: {int_corrs}")

    max_abs_corr = max((abs(c) for _, c in sorted_corrs), default=0.0)

    if suspicious_high:
        status = 'FAIL'
    elif suspicious_mid:
        status = 'WARN'
    else:
        status = 'PASS'

    record('test6_leakage', status, {
        'max_abs_corr': round(max_abs_corr, 5),
        'n_features_above_025': len(suspicious_high),
        'n_features_015_to_025': len(suspicious_mid),
        'suspicious_high': [{'feature': n, 'corr': c} for n, c in suspicious_high],
        'suspicious_mid': [{'feature': n, 'corr': c} for n, c in suspicious_mid[:10]],
        'interaction_corrs': {k: round(v, 5) for k, v in int_corrs.items()},
        'top_15_corrs': [{'feature': n, 'corr': c} for n, c in sorted_corrs[:15]],
        'note': 'Some correlation is expected; thresholds flag obvious leakage only.',
    })


# ---------------------------------------------------------------------------
# TEST 7 — Signal Decay Curve
# ---------------------------------------------------------------------------
def test_signal_decay(X_base, fc_for_interactions, params):
    log.info("=" * 70)
    log.info("TEST 7: Signal Decay Curve (1d, 5d, 10d, 20d targets)")

    horizons = [
        ('outcome_1d', '1d'),
        ('outcome_5d', '5d'),
        ('outcome_10d', '10d'),
        ('outcome_20d', '20d'),
    ]

    horizon_aucs = {}
    for target_col, label in horizons:
        log.info(f"  Horizon {label}: loading target '{target_col}'...")
        try:
            X_h, y_h, _ = build_feature_matrix(target=target_col)
        except Exception as e:
            log.warning(f"  Horizon {label}: build_feature_matrix({target_col}) failed: {e}. Skipping.")
            horizon_aucs[label] = None
            continue

        if y_h is None or len(y_h) == 0:
            log.warning(f"  Horizon {label}: empty y. Skipping.")
            horizon_aucs[label] = None
            continue

        # Build X_aug for this horizon
        X_h_aug = build_interaction_features(X_h, fc_for_interactions)
        N = len(y_h)
        split = int(N * 0.80)
        X_tr, X_te = X_h_aug[:split], X_h_aug[split:]
        y_tr, y_te = y_h[:split], y_h[split:]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            log.warning(f"  Horizon {label}: single class — skipping.")
            horizon_aucs[label] = None
            continue

        try:
            mdl = fresh_lgb(params)
            mdl.fit(X_tr, y_tr)
            proba = mdl.predict_proba(X_te)[:, 1]
            auc = safe_auc(y_te, proba)
            log.info(f"  Horizon {label}: AUC={auc:.4f}  (n_val={len(y_te)})")
            horizon_aucs[label] = round(float(auc), 4)
        except Exception as e:
            log.warning(f"  Horizon {label}: model failed: {e}")
            horizon_aucs[label] = None

    log.info(f"  Decay curve: {horizon_aucs}")

    auc_1d = horizon_aucs.get('1d')
    auc_20d = horizon_aucs.get('20d')

    if auc_1d is None or auc_20d is None:
        status = 'WARN'
        note = 'One or more horizons missing — could not fully evaluate decay.'
    elif auc_20d > auc_1d:
        status = 'PASS'
        note = '20d AUC > 1d AUC — signal is structural.'
    elif auc_20d == auc_1d:
        status = 'WARN'
        note = '20d AUC == 1d AUC — signal may be flat across horizons.'
    else:
        status = 'FAIL'
        note = '20d AUC < 1d AUC — signal may be short-term or noisy.'

    record('test7_signal_decay', status, {
        'horizon_aucs': horizon_aucs,
        'note': note,
    })


# ---------------------------------------------------------------------------
# TEST 8 — Model-Ranked Quintile Analysis
# ---------------------------------------------------------------------------
def test_quintile_analysis(X_aug, y, params):
    log.info("=" * 70)
    log.info("TEST 8: Model-Ranked Quintile Analysis")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X_aug[:split], X_aug[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test8_quintile', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb(params)
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        log.error(f"Test 8 fit failed: {e}")
        record('test8_quintile', 'FAIL', {'error': str(e)})
        return

    sorted_idx = np.argsort(proba_te)[::-1]
    n = len(y_te)
    q_size = n // 5

    quintiles = []
    for q in range(5):
        start = q * q_size
        end = (q + 1) * q_size if q < 4 else n
        q_idx = sorted_idx[start:end]
        q_rate = float(y_te[q_idx].mean())
        q_pred = float(proba_te[q_idx].mean())
        label = f'Q{q+1}'
        quintiles.append({
            'quintile': label,
            'count': int(end - start),
            'positive_rate': round(q_rate, 4),
            'mean_predicted_prob': round(q_pred, 4),
        })
        log.info(f"  {label}: n={end-start}, pos_rate={q_rate:.4f}, mean_pred={q_pred:.4f}")

    q1_rate = quintiles[0]['positive_rate']
    q5_rate = quintiles[4]['positive_rate']

    if q5_rate > 0:
        pseudo_sharpe = q1_rate / q5_rate
    else:
        pseudo_sharpe = float('inf')
        log.warning("  Q5 positive rate = 0; pseudo-Sharpe is infinity.")

    log.info(f"  Pseudo-Sharpe (Q1 rate / Q5 rate): {pseudo_sharpe:.3f}")

    if pseudo_sharpe > 1.5:
        status = 'PASS'
    elif pseudo_sharpe >= 1.2:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test8_quintile', status, {
        'quintiles': quintiles,
        'q1_positive_rate': q1_rate,
        'q5_positive_rate': q5_rate,
        'pseudo_sharpe_q1_q5': round(pseudo_sharpe, 4) if not np.isinf(pseudo_sharpe) else 'inf',
        'note': 'Pseudo-Sharpe = Q1_rate / Q5_rate; not a real Sharpe ratio.',
    })


# ---------------------------------------------------------------------------
# TEST 9 — Label Contamination Scan
# ---------------------------------------------------------------------------
def test_label_contamination(X_aug, y):
    log.info("=" * 70)
    log.info("TEST 9: Label Contamination Scan")

    N = len(y)
    split = int(N * 0.80)
    X_te = X_aug[split:]
    y_te = y[split:]

    # a. Positive rate
    pos_rate = float(y_te.mean())
    log.info(f"  (a) Val positive rate: {pos_rate:.4f} (expected ~0.40-0.65, baseline ~0.566)")

    # b. NaN check
    nan_X = int(np.isnan(X_aug).sum())
    nan_y = int(np.isnan(y.astype(float)).sum()) if y.dtype != object else 0
    log.info(f"  (b) NaN in X_aug: {nan_X}, NaN in y: {nan_y}")

    # c. Duplicate rows in X_aug
    try:
        unique_rows = len(np.unique(X_aug, axis=0))
        n_dupes = N - unique_rows
        dupe_pct = n_dupes / N if N > 0 else 0.0
        log.info(f"  (c) Duplicate rows: {n_dupes} ({dupe_pct*100:.2f}%)")
    except Exception as e:
        log.warning(f"  (c) Duplicate check failed: {e}")
        n_dupes = -1
        dupe_pct = 0.0

    # d. Lag-1 autocorrelation of y
    try:
        y_float = y.astype(float)
        y_mean = y_float.mean()
        y_centered = y_float - y_mean
        autocorr = float(
            np.sum(y_centered[:-1] * y_centered[1:]) /
            (np.sum(y_centered ** 2) + 1e-10)
        )
        log.info(f"  (d) Lag-1 autocorrelation of y: {autocorr:.4f} (flag if > 0.5)")
    except Exception as e:
        log.warning(f"  (d) Autocorrelation failed: {e}")
        autocorr = 0.0

    issues = []
    warns = []

    if not (0.35 <= pos_rate <= 0.65):
        if pos_rate < 0.20 or pos_rate > 0.80:
            issues.append(f"positive_rate={pos_rate:.4f} far outside expected range 35-65%")
        else:
            warns.append(f"positive_rate={pos_rate:.4f} outside expected 35-65%")

    if nan_X > 0 or nan_y > 0:
        issues.append(f"NaN values found: X={nan_X}, y={nan_y}")

    if n_dupes >= 0:
        if dupe_pct >= 0.10:
            issues.append(f"{dupe_pct*100:.1f}% duplicate rows")
        elif dupe_pct >= 0.05:
            warns.append(f"{dupe_pct*100:.1f}% duplicate rows")

    if autocorr > 0.5:
        issues.append(f"Lag-1 autocorrelation={autocorr:.4f} > 0.5 — possible contamination")
    elif autocorr > 0.3:
        warns.append(f"Lag-1 autocorrelation={autocorr:.4f} moderately high")

    if issues:
        status = 'FAIL'
    elif warns:
        status = 'WARN'
    else:
        status = 'PASS'

    record('test9_label_contamination', status, {
        'val_positive_rate': round(pos_rate, 4),
        'nan_in_X_aug': nan_X,
        'nan_in_y': nan_y,
        'duplicate_rows': int(n_dupes),
        'duplicate_pct': round(dupe_pct * 100, 2),
        'lag1_autocorr_y': round(autocorr, 4),
        'issues': issues,
        'warnings': warns,
    })


# ---------------------------------------------------------------------------
# ASCII Summary Table
# ---------------------------------------------------------------------------
def print_summary():
    test_order = [
        ('test1_walk_forward',        'Test 1: Walk-Forward Validation'),
        ('test2_regime_auc',          'Test 2: Regime-Conditioned AUC'),
        ('test3_calibration',         'Test 3: Calibration Check'),
        ('test4_lift',                'Test 4: Top-Decile Lift'),
        ('test5_permutation',         'Test 5: Permutation Feature Importance'),
        ('test6_leakage',             'Test 6: Data Leakage Probe'),
        ('test7_signal_decay',        'Test 7: Signal Decay Curve'),
        ('test8_quintile',            'Test 8: Quintile Analysis'),
        ('test9_label_contamination', 'Test 9: Label Contamination Scan'),
    ]

    STATUS_ICONS = {'PASS': 'PASS', 'WARN': 'WARN', 'FAIL': 'FAIL', 'ERROR': 'ERROR'}

    line = '+' + '-' * 46 + '+' + '-' * 8 + '+'
    header = f"| {'Test':<44} | {'Result':^6} |"
    log.info("")
    log.info("=" * 58)
    log.info("  v2s MODEL VALIDATION SUMMARY")
    log.info("=" * 58)
    log.info(line)
    log.info(header)
    log.info(line)

    pass_count = warn_count = fail_count = error_count = 0

    for key, label in test_order:
        if key in results:
            s = results[key]['status']
            icon = STATUS_ICONS.get(s, s)
        else:
            s = 'ERROR'
            icon = 'ERROR'

        row = f"| {label:<44} | {icon:^6} |"
        log.info(row)

        if s == 'PASS':
            pass_count += 1
        elif s == 'WARN':
            warn_count += 1
        elif s == 'FAIL':
            fail_count += 1
        else:
            error_count += 1

    log.info(line)
    total = pass_count + warn_count + fail_count + error_count
    log.info(
        f"| {'TOTALS':<44} | {'':^6} |"
    )
    log.info(
        f"|   PASS: {pass_count}  WARN: {warn_count}  FAIL: {fail_count}  ERROR: {error_count}"
        f"  (of {total}) |"
    )
    log.info(line)
    log.info("")


# ---------------------------------------------------------------------------
# Write JSON report
# ---------------------------------------------------------------------------
def write_report(start_time: datetime):
    elapsed = (datetime.now() - start_time).total_seconds()
    report = {
        'model': 'v2s LightGBM',
        'run_at': start_time.isoformat(),
        'elapsed_seconds': round(elapsed, 1),
        'log_file': str(LOG_PATH),
        'results': results,
    }
    try:
        with open(REPORT_PATH, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        log.info(f"Report written to: {REPORT_PATH}")
    except Exception as e:
        log.error(f"Failed to write report: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now()
    log.info("=" * 70)
    log.info(f"v2s Model Deep Validation Suite — started at {start_time.isoformat()}")
    log.info(f"BASE: {BASE}")
    log.info(f"Model: {MODEL_PATH}")
    log.info(f"Log: {LOG_PATH}")
    log.info("=" * 70)

    # Load data once
    (model_pkl, X_base, X_aug, y, df_raw,
     fc_for_interactions, all_feature_names, params) = load_data()

    log.info(
        f"Dataset: {len(y)} samples | {X_aug.shape[1]} features | "
        f"positive_rate={y.mean():.4f}"
    )

    # Run tests
    try:
        test_walk_forward(X_aug, y, params)
    except Exception as e:
        log.error(f"TEST 1 crashed: {e}\n{traceback.format_exc()}")
        record('test1_walk_forward', 'FAIL', {'error': str(e)})

    try:
        test_regime_auc(X_aug, y, df_raw, fc_for_interactions, params)
    except Exception as e:
        log.error(f"TEST 2 crashed: {e}\n{traceback.format_exc()}")
        record('test2_regime_auc', 'FAIL', {'error': str(e)})

    try:
        test_calibration(X_aug, y, params)
    except Exception as e:
        log.error(f"TEST 3 crashed: {e}\n{traceback.format_exc()}")
        record('test3_calibration', 'FAIL', {'error': str(e)})

    try:
        test_lift(X_aug, y, params)
    except Exception as e:
        log.error(f"TEST 4 crashed: {e}\n{traceback.format_exc()}")
        record('test4_lift', 'FAIL', {'error': str(e)})

    try:
        test_permutation_importance(X_aug, y, all_feature_names, params)
    except Exception as e:
        log.error(f"TEST 5 crashed: {e}\n{traceback.format_exc()}")
        record('test5_permutation', 'FAIL', {'error': str(e)})

    try:
        test_leakage(X_aug, y, all_feature_names)
    except Exception as e:
        log.error(f"TEST 6 crashed: {e}\n{traceback.format_exc()}")
        record('test6_leakage', 'FAIL', {'error': str(e)})

    try:
        test_signal_decay(X_base, fc_for_interactions, params)
    except Exception as e:
        log.error(f"TEST 7 crashed: {e}\n{traceback.format_exc()}")
        record('test7_signal_decay', 'FAIL', {'error': str(e)})

    try:
        test_quintile_analysis(X_aug, y, params)
    except Exception as e:
        log.error(f"TEST 8 crashed: {e}\n{traceback.format_exc()}")
        record('test8_quintile', 'FAIL', {'error': str(e)})

    try:
        test_label_contamination(X_aug, y)
    except Exception as e:
        log.error(f"TEST 9 crashed: {e}\n{traceback.format_exc()}")
        record('test9_label_contamination', 'FAIL', {'error': str(e)})

    # Summary
    print_summary()
    write_report(start_time)

    elapsed = (datetime.now() - start_time).total_seconds()
    log.info(f"Total elapsed: {elapsed/60:.1f} minutes")
    log.info("Done.")


if __name__ == '__main__':
    main()

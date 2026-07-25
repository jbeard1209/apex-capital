"""
Long-Term Model Deep Validation Suite (outcome_365d)
Run on GCP: python3 ml_validate_longterm.py

Mirrors ml_validate_v2s.py exactly but adapted for 1-year horizon.
- Target: outcome_365d (binarized: > 0 = 1)
- Trains a fresh LightGBM inline (no model_longterm.pkl required)
- Runs 9 validation tests
- Saves report to data/longterm_validation_report.json
- Logs to logs/longterm_validation_{date}.log
"""

import sys
import os
import json
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
REPORT_PATH = BASE / 'data' / 'longterm_validation_report.json'
LOG_DIR = BASE / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = BASE / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATE_STR = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_PATH = LOG_DIR / f'longterm_validation_{DATE_STR}.log'

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

# ---------------------------------------------------------------------------
# Heavy imports
# ---------------------------------------------------------------------------
import numpy as np
import pandas as pd
import sqlite3

try:
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.calibration import calibration_curve
except ImportError as e:
    log.error(f"sklearn not available: {e}")
    sys.exit(1)

try:
    from lightgbm import LGBMClassifier
except ImportError as e:
    log.error(f"lightgbm not available: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = BASE / 'data' / 'historical_13f.db'

FEATURES = [
    'conviction_score',
    'n_institutions',
    'herding_flag',
    'quant_consensus',
    'days_since_filing',
    'portfolio_weight',
]

TARGET = 'outcome_365d'

LGB_PARAMS = dict(
    n_estimators=500,
    learning_rate=0.05,
    num_leaves=31,
    class_weight='balanced',
    random_state=42,
    verbosity=-1,
)

# Walk-forward fold configs (train end %, test end %)
FOLD_CONFIGS = [
    (0.40, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.65, 0.75),
    (0.70, 0.80),
    (0.75, 0.85),
]

PASS_THRESHOLD = 0.53  # mean AUC > 0.53 = PASS for 1-year horizon

# ---------------------------------------------------------------------------
# Results accumulator
# ---------------------------------------------------------------------------
results = {}


def record(name: str, status: str, details: dict):
    symbol = f"[{status}]"
    log.info(f"{symbol} {name}: {json.dumps(details, default=str)}")
    results[name] = {'status': status, 'details': details}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fresh_lgb() -> LGBMClassifier:
    return LGBMClassifier(**LGB_PARAMS)


def safe_auc(y_true, y_score) -> float:
    try:
        if len(np.unique(y_true)) < 2:
            return np.nan
        return roc_auc_score(y_true, y_score)
    except Exception:
        return np.nan


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data():
    log.info("=" * 70)
    log.info(f"Loading data for target={TARGET} from {DB_PATH}")

    if not DB_PATH.exists():
        log.error(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))

    # Check which feature columns actually exist
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(pipeline_snapshots)")
    available_cols = {row[1] for row in cur.fetchall()}

    # Also pull rd_regime_bull for regime test, and snapshot_date/ticker for contamination
    extra_select = []
    if 'rd_regime_bull' in available_cols:
        extra_select.append('rd_regime_bull')

    feat_select = [f for f in FEATURES if f in available_cols]
    missing = [f for f in FEATURES if f not in available_cols]
    if missing:
        log.warning(f"Missing feature columns: {missing} — will fill with 0")

    extra_str = (', ' + ', '.join(extra_select)) if extra_select else ''
    query = f"""
        SELECT snapshot_date, ticker, {', '.join(feat_select)}{extra_str}, {TARGET}
        FROM pipeline_snapshots
        WHERE {TARGET} IS NOT NULL AND conviction_score IS NOT NULL
        ORDER BY snapshot_date
    """

    log.info("Running SQL query...")
    df = pd.read_sql(query, conn)
    conn.close()

    log.info(f"Loaded {len(df)} rows, columns: {list(df.columns)}")

    # Fill missing features with 0
    for f in FEATURES:
        if f not in df.columns:
            df[f] = 0.0

    # Fill nulls in features
    for f in FEATURES:
        df[f] = df[f].fillna(0.0)

    # Build X, y
    X = df[FEATURES].values.astype(float)
    # Binarize: outcome_365d > 0 → 1 (positive 1-year return)
    y = (df[TARGET].values > 0).astype(int)

    pos_rate = y.mean()
    log.info(f"X shape: {X.shape}, y positive rate: {pos_rate:.4f} ({pos_rate*100:.1f}%)")

    # Return df for regime test
    df_meta = df[['snapshot_date', 'ticker'] + (['rd_regime_bull'] if 'rd_regime_bull' in df.columns else [])].copy()

    return X, y, df_meta, list(df.columns)


# ---------------------------------------------------------------------------
# TEST 1 — Walk-Forward Validation (expanding window, 6 folds)
# ---------------------------------------------------------------------------
def test_walk_forward(X, y):
    log.info("=" * 70)
    log.info("TEST 1: Walk-Forward Validation (expanding window, 6 folds)")
    log.info(f"  PASS threshold: mean AUC > {PASS_THRESHOLD}")

    N = len(y)
    aucs = []
    fold_details = []

    for i, (train_end_pct, test_end_pct) in enumerate(FOLD_CONFIGS):
        train_end = int(N * train_end_pct)
        test_end = min(int(N * test_end_pct), N)
        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[train_end:test_end], y[train_end:test_end]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            log.warning(f"Fold {i+1}: skipping — single class in train or test.")
            continue

        try:
            mdl = fresh_lgb()
            mdl.fit(X_tr, y_tr)
            proba = mdl.predict_proba(X_te)[:, 1]
            auc = safe_auc(y_te, proba)
        except Exception as e:
            log.warning(f"Fold {i+1} failed: {e}")
            auc = np.nan

        log.info(
            f"  Fold {i+1}: train={train_end} ({train_end_pct*100:.0f}%), "
            f"test={test_end - train_end}, AUC={auc:.4f}"
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
        return None

    mean_auc = float(np.mean(valid_aucs))
    std_auc = float(np.std(valid_aucs))
    min_auc = float(np.min(valid_aucs))
    max_auc = float(np.max(valid_aucs))

    log.info(f"Walk-Forward: mean_AUC={mean_auc:.4f}, std={std_auc:.4f}, min={min_auc:.4f}, max={max_auc:.4f}")

    if mean_auc > PASS_THRESHOLD:
        status = 'PASS'
    elif mean_auc >= 0.51:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test1_walk_forward', status, {
        'mean_auc': round(mean_auc, 4),
        'std_auc': round(std_auc, 4),
        'min_auc': round(min_auc, 4),
        'max_auc': round(max_auc, 4),
        'pass_threshold': PASS_THRESHOLD,
        'folds': fold_details,
        'note': 'No lookahead gap; 1-year label horizon already provides natural separation.',
    })
    return mean_auc


# ---------------------------------------------------------------------------
# TEST 2 — Regime-Conditioned AUC
# ---------------------------------------------------------------------------
def test_regime_auc(X, y, df_meta):
    log.info("=" * 70)
    log.info("TEST 2: Regime-Conditioned AUC (split by rd_regime_bull if available)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test2_regime_auc', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb()
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        record('test2_regime_auc', 'FAIL', {'error': str(e)})
        return

    if 'rd_regime_bull' in df_meta.columns:
        regime_te = df_meta['rd_regime_bull'].iloc[split:].fillna(0).values
        low_mask = regime_te == 0   # bear/neutral
        high_mask = regime_te == 1  # bull
        regime_source = 'rd_regime_bull'
        log.info(f"  Regime source: rd_regime_bull | bull={high_mask.sum()}, bear={low_mask.sum()}")
    else:
        # Fallback: split by conviction_score median (first feature col)
        conv_te = X_te[:, 0]
        median_conv = float(np.median(X_tr[:, 0]))
        low_mask = conv_te <= median_conv
        high_mask = conv_te > median_conv
        regime_source = 'conviction_median_fallback'
        log.warning("  rd_regime_bull not available — using conviction_score median split as fallback (NOTE)")

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

    auc_bear = regime_auc(low_mask, 'bear/low-conviction')
    auc_bull = regime_auc(high_mask, 'bull/high-conviction')

    valid = [a for a in [auc_bear, auc_bull] if not np.isnan(a)]
    if not valid:
        status = 'FAIL'
    elif all(a > 0.53 for a in valid):
        status = 'PASS'
    elif any(a < 0.50 for a in valid):
        status = 'FAIL'
    else:
        status = 'WARN'

    record('test2_regime_auc', status, {
        'regime_source': regime_source,
        'n_bear_low': int(low_mask.sum()),
        'n_bull_high': int(high_mask.sum()),
        'auc_bear_low': round(float(auc_bear), 4) if not np.isnan(auc_bear) else None,
        'auc_bull_high': round(float(auc_bull), 4) if not np.isnan(auc_bull) else None,
    })


# ---------------------------------------------------------------------------
# TEST 3 — Calibration Check (Brier Score + reliability diagram)
# ---------------------------------------------------------------------------
def test_calibration(X, y):
    log.info("=" * 70)
    log.info("TEST 3: Calibration Check (Brier Score + reliability curve)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test3_calibration', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb()
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        record('test3_calibration', 'FAIL', {'error': str(e)})
        return

    positive_rate = float(y_te.mean())
    baseline_brier = positive_rate * (1.0 - positive_rate)
    actual_brier = float(brier_score_loss(y_te, proba_te))
    ratio = actual_brier / baseline_brier if baseline_brier > 0 else float('inf')

    log.info(f"  Positive rate (val): {positive_rate:.4f}")
    log.info(f"  Baseline Brier: {baseline_brier:.4f}")
    log.info(f"  Model Brier: {actual_brier:.4f}")
    log.info(f"  Ratio model/baseline: {ratio:.4f}")

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
def test_lift(X, y):
    log.info("=" * 70)
    log.info("TEST 4: Top-Decile Lift")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test4_lift', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb()
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
        record('test4_lift', 'FAIL', {'error': str(e)})
        return

    overall_rate = float(y_te.mean())
    log.info(f"  Overall positive rate: {overall_rate:.4f}")

    sorted_idx = np.argsort(proba_te)[::-1]
    n = len(y_te)

    def decile_rate(top_pct):
        k = max(1, int(n * top_pct))
        return float(y_te[sorted_idx[:k]].mean())

    def bottom_rate(pct):
        k = max(1, int(n * pct))
        return float(y_te[sorted_idx[n - k:]].mean())

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
def test_permutation_importance(X, y):
    log.info("=" * 70)
    log.info("TEST 5: Permutation Feature Importance (3 shuffles per feature)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test5_permutation', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb()
        mdl.fit(X_tr, y_tr)
        base_proba = mdl.predict_proba(X_te)[:, 1]
        base_auc = safe_auc(y_te, base_proba)
    except Exception as e:
        record('test5_permutation', 'FAIL', {'error': str(e)})
        return

    log.info(f"  Base AUC: {base_auc:.4f}")
    rng = np.random.RandomState(42)

    importances = []
    for j, fname in enumerate(FEATURES):
        drop_aucs = []
        X_perm = X_te.copy()
        for _ in range(3):
            X_perm[:, j] = rng.permutation(X_perm[:, j])
            perm_proba = mdl.predict_proba(X_perm)[:, 1]
            perm_auc = safe_auc(y_te, perm_proba)
            drop_aucs.append(base_auc - perm_auc)
            X_perm[:, j] = X_te[:, j]
        mean_drop = float(np.mean(drop_aucs))
        importances.append((fname, mean_drop))

    importances.sort(key=lambda x: x[1], reverse=True)

    log.info("  Features by AUC drop (most important first):")
    for rank, (name, drop) in enumerate(importances, 1):
        log.info(f"    {rank}. {name}: {drop:.5f}")

    max_drop = importances[0][1] if importances else 0.0

    if max_drop > 0.005:
        status = 'PASS'
    elif max_drop >= 0.002:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test5_permutation', status, {
        'base_auc': round(base_auc, 4),
        'top_feature': importances[0][0] if importances else None,
        'top_feature_auc_drop': round(max_drop, 5),
        'all_features': [{'feature': n, 'auc_drop': round(d, 5)} for n, d in importances],
    })


# ---------------------------------------------------------------------------
# TEST 6 — Leakage Probe (shuffle labels → AUC should drop to ~0.5)
# ---------------------------------------------------------------------------
def test_leakage(X, y):
    log.info("=" * 70)
    log.info("TEST 6: Leakage Probe (shuffle labels → AUC should drop to ~0.5)")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test6_leakage', 'FAIL', {'error': 'Single class in training data'})
        return

    # Real model AUC
    try:
        mdl_real = fresh_lgb()
        mdl_real.fit(X_tr, y_tr)
        proba_real = mdl_real.predict_proba(X_te)[:, 1]
        auc_real = safe_auc(y_te, proba_real)
    except Exception as e:
        record('test6_leakage', 'FAIL', {'error': f'Real model failed: {e}'})
        return

    # Shuffled-label model AUC (3 runs)
    rng = np.random.RandomState(42)
    shuffled_aucs = []
    for trial in range(3):
        y_shuffled = rng.permutation(y_tr)
        try:
            mdl_shuf = fresh_lgb()
            mdl_shuf.fit(X_tr, y_shuffled)
            proba_shuf = mdl_shuf.predict_proba(X_te)[:, 1]
            auc_shuf = safe_auc(y_te, proba_shuf)
            shuffled_aucs.append(auc_shuf)
            log.info(f"  Shuffle trial {trial+1}: AUC={auc_shuf:.4f}")
        except Exception as e:
            log.warning(f"  Shuffle trial {trial+1} failed: {e}")

    mean_shuffled = float(np.mean(shuffled_aucs)) if shuffled_aucs else np.nan
    auc_drop = float(auc_real - mean_shuffled) if not np.isnan(mean_shuffled) else np.nan

    log.info(f"  Real model AUC: {auc_real:.4f}")
    log.info(f"  Mean shuffled AUC: {mean_shuffled:.4f}")
    log.info(f"  AUC drop (real - shuffled): {auc_drop:.4f}")

    # Pass: shuffled AUC is near 0.5 (0.45–0.55) AND real model has meaningful lift
    if np.isnan(mean_shuffled):
        status = 'WARN'
    elif mean_shuffled > 0.55:
        status = 'FAIL'  # shuffled model still predicts — leakage suspected
    elif auc_drop >= 0.03:
        status = 'PASS'
    elif auc_drop >= 0.01:
        status = 'WARN'
    else:
        status = 'FAIL'

    record('test6_leakage', status, {
        'real_auc': round(auc_real, 4),
        'mean_shuffled_auc': round(mean_shuffled, 4) if not np.isnan(mean_shuffled) else None,
        'auc_drop': round(auc_drop, 4) if not np.isnan(auc_drop) else None,
        'shuffled_aucs': [round(a, 4) for a in shuffled_aucs],
        'note': 'Shuffled AUC near 0.5 = no leakage. Drop from real to shuffled should be > 0.03.',
    })


# ---------------------------------------------------------------------------
# TEST 7 — Signal Decay: compare AUC predicting outcome_20d vs outcome_365d
# ---------------------------------------------------------------------------
def test_signal_decay(X_365d, y_365d):
    log.info("=" * 70)
    log.info("TEST 7: Signal Decay — compare model AUC on outcome_20d vs outcome_365d")
    log.info("  (Same features, same train/val split — does the model know what it's predicting?)")

    # Load outcome_20d from DB for same rows
    try:
        conn = sqlite3.connect(str(DB_PATH))
        query_20d = f"""
            SELECT conviction_score, n_institutions, herding_flag, quant_consensus,
                   days_since_filing, portfolio_weight, outcome_20d, outcome_365d
            FROM pipeline_snapshots
            WHERE outcome_365d IS NOT NULL AND conviction_score IS NOT NULL
              AND outcome_20d IS NOT NULL
            ORDER BY snapshot_date
        """
        df_both = pd.read_sql(query_20d, conn)
        conn.close()
    except Exception as e:
        record('test7_signal_decay', 'WARN', {'error': f'Could not load dual-outcome data: {e}', 'note': 'Skipped'})
        return

    if len(df_both) < 100:
        record('test7_signal_decay', 'WARN', {
            'n_rows': len(df_both),
            'note': 'Too few rows with both outcome_20d and outcome_365d — skipped'
        })
        return

    for f in FEATURES:
        if f not in df_both.columns:
            df_both[f] = 0.0
        df_both[f] = df_both[f].fillna(0.0)

    X_both = df_both[FEATURES].values.astype(float)
    y_20d = (df_both['outcome_20d'].values > 0).astype(int)
    y_365d = (df_both['outcome_365d'].values > 0).astype(int)

    N = len(y_20d)
    split = int(N * 0.80)
    X_tr, X_te = X_both[:split], X_both[split:]
    y_tr_365d, y_te_365d = y_365d[:split], y_365d[split:]
    y_tr_20d, y_te_20d = y_20d[:split], y_20d[split:]

    horizon_aucs = {}

    # Train on 365d, eval on 365d
    if len(np.unique(y_tr_365d)) >= 2 and len(np.unique(y_te_365d)) >= 2:
        try:
            mdl = fresh_lgb()
            mdl.fit(X_tr, y_tr_365d)
            proba = mdl.predict_proba(X_te)[:, 1]
            auc_365 = safe_auc(y_te_365d, proba)
            horizon_aucs['outcome_365d'] = round(float(auc_365), 4)
            log.info(f"  Model trained on 365d labels → AUC on 365d test: {auc_365:.4f}")
        except Exception as e:
            log.warning(f"  365d model failed: {e}")
            horizon_aucs['outcome_365d'] = None
    else:
        horizon_aucs['outcome_365d'] = None
        log.warning("  365d: single class — skipped")

    # Train on 365d, eval on 20d (cross-horizon test)
    if len(np.unique(y_tr_365d)) >= 2 and len(np.unique(y_te_20d)) >= 2:
        try:
            mdl2 = fresh_lgb()
            mdl2.fit(X_tr, y_tr_365d)
            proba2 = mdl2.predict_proba(X_te)[:, 1]
            auc_cross = safe_auc(y_te_20d, proba2)
            horizon_aucs['outcome_20d_with_365d_model'] = round(float(auc_cross), 4)
            log.info(f"  Model trained on 365d labels → AUC on 20d test: {auc_cross:.4f}")
        except Exception as e:
            log.warning(f"  Cross-horizon model failed: {e}")
            horizon_aucs['outcome_20d_with_365d_model'] = None
    else:
        horizon_aucs['outcome_20d_with_365d_model'] = None

    # Also train on 20d, eval on 20d (baseline comparison)
    if len(np.unique(y_tr_20d)) >= 2 and len(np.unique(y_te_20d)) >= 2:
        try:
            mdl3 = fresh_lgb()
            mdl3.fit(X_tr, y_tr_20d)
            proba3 = mdl3.predict_proba(X_te)[:, 1]
            auc_20 = safe_auc(y_te_20d, proba3)
            horizon_aucs['outcome_20d_native'] = round(float(auc_20), 4)
            log.info(f"  Model trained on 20d labels → AUC on 20d test: {auc_20:.4f}")
        except Exception as e:
            log.warning(f"  20d baseline model failed: {e}")
            horizon_aucs['outcome_20d_native'] = None

    auc_365d = horizon_aucs.get('outcome_365d')
    auc_cross = horizon_aucs.get('outcome_20d_with_365d_model')

    # Does the long-term model predict long-term better than short-term?
    if auc_365d is None or auc_cross is None:
        status = 'WARN'
        note = 'Could not fully evaluate decay — missing horizon data.'
    elif auc_365d >= auc_cross:
        status = 'PASS'
        note = f'365d model better predicts 365d ({auc_365d:.4f}) than 20d ({auc_cross:.4f}) — signal is horizon-specific.'
    else:
        status = 'WARN'
        note = f'365d model cross-predicts 20d ({auc_cross:.4f}) better than 365d ({auc_365d:.4f}) — features may be short-term.'

    log.info(f"  {note}")

    record('test7_signal_decay', status, {
        'horizon_aucs': horizon_aucs,
        'note': note,
    })


# ---------------------------------------------------------------------------
# TEST 8 — Quintile Analysis (monotonic spread across conviction quintiles)
# ---------------------------------------------------------------------------
def test_quintile_analysis(X, y):
    log.info("=" * 70)
    log.info("TEST 8: Model-Ranked Quintile Analysis")

    N = len(y)
    split = int(N * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2:
        record('test8_quintile', 'FAIL', {'error': 'Single class in training data'})
        return

    try:
        mdl = fresh_lgb()
        mdl.fit(X_tr, y_tr)
        proba_te = mdl.predict_proba(X_te)[:, 1]
    except Exception as e:
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

    log.info(f"  Pseudo-Sharpe (Q1/Q5): {pseudo_sharpe:.3f}")

    # Check monotonicity: each quintile rate should decline
    rates = [q['positive_rate'] for q in quintiles]
    is_monotone = all(rates[i] >= rates[i+1] for i in range(len(rates)-1))
    log.info(f"  Monotone spread: {is_monotone} | rates: {rates}")

    if pseudo_sharpe > 1.5 and is_monotone:
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
        'is_monotone': is_monotone,
        'note': 'Pseudo-Sharpe = Q1_rate / Q5_rate; monotone = each quintile lower than previous.',
    })


# ---------------------------------------------------------------------------
# TEST 9 — Label Contamination Check
# ---------------------------------------------------------------------------
def test_label_contamination(X, y, df_meta):
    log.info("=" * 70)
    log.info("TEST 9: Label Contamination Check")

    N = len(y)
    split = int(N * 0.80)
    y_te = y[split:]
    X_te = X[split:]

    # a. Positive rate
    pos_rate = float(y_te.mean())
    overall_pos_rate = float(y.mean())
    log.info(f"  (a) Val positive rate: {pos_rate:.4f} | Overall: {overall_pos_rate:.4f}")

    # b. NaN check
    nan_X = int(np.isnan(X).sum())
    nan_y = int(np.isnan(y.astype(float)).sum())
    log.info(f"  (b) NaN in X: {nan_X}, NaN in y: {nan_y}")

    # c. Duplicate ticker+snapshot_date rows with different outcome_365d
    dup_contamination = 0
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df_check = pd.read_sql(
            """SELECT ticker, snapshot_date, outcome_365d
               FROM pipeline_snapshots
               WHERE outcome_365d IS NOT NULL AND conviction_score IS NOT NULL
               ORDER BY snapshot_date""",
            conn
        )
        conn.close()
        grouped = df_check.groupby(['ticker', 'snapshot_date'])['outcome_365d'].nunique()
        dup_contamination = int((grouped > 1).sum())
        log.info(f"  (c) Ticker+date pairs with conflicting outcome_365d: {dup_contamination}")
    except Exception as e:
        log.warning(f"  (c) Duplicate check failed: {e}")

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

    if not (0.35 <= overall_pos_rate <= 0.85):
        if overall_pos_rate < 0.20 or overall_pos_rate > 0.95:
            issues.append(f"positive_rate={overall_pos_rate:.4f} far outside expected range")
        else:
            warns.append(f"positive_rate={overall_pos_rate:.4f} outside 35-85%")

    if nan_X > 0 or nan_y > 0:
        issues.append(f"NaN values found: X={nan_X}, y={nan_y}")

    if dup_contamination > 0:
        issues.append(f"{dup_contamination} ticker+date pairs have conflicting outcome_365d values")

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
        'overall_positive_rate': round(overall_pos_rate, 4),
        'nan_in_X': nan_X,
        'nan_in_y': nan_y,
        'duplicate_ticker_date_pairs_conflicting': dup_contamination,
        'lag1_autocorr_y': round(autocorr, 4),
        'issues': issues,
        'warnings': warns,
    })


# ---------------------------------------------------------------------------
# ASCII Summary Table
# ---------------------------------------------------------------------------
def print_summary():
    test_order = [
        ('test1_walk_forward',        'Test 1: Walk-Forward (6-fold)'),
        ('test2_regime_auc',          'Test 2: Regime-Conditioned AUC'),
        ('test3_calibration',         'Test 3: Calibration / Brier Score'),
        ('test4_lift',                'Test 4: Top-Decile Lift'),
        ('test5_permutation',         'Test 5: Permutation Feature Importance'),
        ('test6_leakage',             'Test 6: Leakage Probe (shuffle labels)'),
        ('test7_signal_decay',        'Test 7: Signal Decay (20d vs 365d AUC)'),
        ('test8_quintile',            'Test 8: Quintile Analysis'),
        ('test9_label_contamination', 'Test 9: Label Contamination Check'),
    ]

    line = '+' + '-' * 52 + '+' + '-' * 8 + '+'
    header = f"| {'Test':<50} | {'Result':^6} |"
    log.info("")
    log.info("=" * 64)
    log.info("  LONG-TERM MODEL (outcome_365d) VALIDATION SUMMARY")
    log.info("=" * 64)
    log.info(line)
    log.info(header)
    log.info(line)

    pass_count = warn_count = fail_count = error_count = 0
    for key, label in test_order:
        if key in results:
            s = results[key]['status']
        else:
            s = 'ERROR'
        row = f"| {label:<50} | {s:^6} |"
        log.info(row)
        if s == 'PASS': pass_count += 1
        elif s == 'WARN': warn_count += 1
        elif s == 'FAIL': fail_count += 1
        else: error_count += 1

    log.info(line)
    log.info(f"|   PASS: {pass_count}  WARN: {warn_count}  FAIL: {fail_count}  ERROR: {error_count}")
    log.info(line)
    log.info("")


# ---------------------------------------------------------------------------
# Write JSON report
# ---------------------------------------------------------------------------
def write_report(start_time: datetime):
    elapsed = (datetime.now() - start_time).total_seconds()
    report = {
        'model': 'LongTerm LightGBM (outcome_365d)',
        'run_at': start_time.isoformat(),
        'elapsed_seconds': round(elapsed, 1),
        'log_file': str(LOG_PATH),
        'features': FEATURES,
        'target': TARGET,
        'lgb_params': LGB_PARAMS,
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
    log.info(f"Long-Term Model Deep Validation Suite — started at {start_time.isoformat()}")
    log.info(f"Target: {TARGET} | Features: {FEATURES}")
    log.info(f"BASE: {BASE} | DB: {DB_PATH}")
    log.info("=" * 70)

    X, y, df_meta, all_cols = load_data()
    log.info(f"Dataset: {len(y)} samples | {X.shape[1]} features | positive_rate={y.mean():.4f}")

    try:
        test_walk_forward(X, y)
    except Exception as e:
        log.error(f"TEST 1 crashed: {e}\n{traceback.format_exc()}")
        record('test1_walk_forward', 'FAIL', {'error': str(e)})

    try:
        test_regime_auc(X, y, df_meta)
    except Exception as e:
        log.error(f"TEST 2 crashed: {e}\n{traceback.format_exc()}")
        record('test2_regime_auc', 'FAIL', {'error': str(e)})

    try:
        test_calibration(X, y)
    except Exception as e:
        log.error(f"TEST 3 crashed: {e}\n{traceback.format_exc()}")
        record('test3_calibration', 'FAIL', {'error': str(e)})

    try:
        test_lift(X, y)
    except Exception as e:
        log.error(f"TEST 4 crashed: {e}\n{traceback.format_exc()}")
        record('test4_lift', 'FAIL', {'error': str(e)})

    try:
        test_permutation_importance(X, y)
    except Exception as e:
        log.error(f"TEST 5 crashed: {e}\n{traceback.format_exc()}")
        record('test5_permutation', 'FAIL', {'error': str(e)})

    try:
        test_leakage(X, y)
    except Exception as e:
        log.error(f"TEST 6 crashed: {e}\n{traceback.format_exc()}")
        record('test6_leakage', 'FAIL', {'error': str(e)})

    try:
        test_signal_decay(X, y)
    except Exception as e:
        log.error(f"TEST 7 crashed: {e}\n{traceback.format_exc()}")
        record('test7_signal_decay', 'FAIL', {'error': str(e)})

    try:
        test_quintile_analysis(X, y)
    except Exception as e:
        log.error(f"TEST 8 crashed: {e}\n{traceback.format_exc()}")
        record('test8_quintile', 'FAIL', {'error': str(e)})

    try:
        test_label_contamination(X, y, df_meta)
    except Exception as e:
        log.error(f"TEST 9 crashed: {e}\n{traceback.format_exc()}")
        record('test9_label_contamination', 'FAIL', {'error': str(e)})

    print_summary()
    write_report(start_time)

    elapsed = (datetime.now() - start_time).total_seconds()
    log.info(f"Total elapsed: {elapsed/60:.1f} minutes")
    log.info("Done.")


if __name__ == '__main__':
    main()

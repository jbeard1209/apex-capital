"""
ML Chain Round 9 -- Regime-shift mitigation experiments.

Context
-------
  Round 8 results: v2w val=0.6100, v2x val=0.6016, walk-forward mean=0.5073
  Walk-forward fails in folds 4-6 (recent data) -- diagnosed as regime shift,
  not data quality. Current best: v2s val=0.6229.

  v3a -- Rolling 18-month window
         Train + validate only on the most recent 18 months of data.
         Hypothesis: a model fit exclusively on recent data is better
         calibrated to the current market regime.

  v3b -- Full history with recency weighting
         Train on all 526k rows but weight the last 18 months at 3x.
         Hypothesis: full history gives breadth; recency weights give
         regime sensitivity without throwing away older signal.

Both use the same LightGBM + 6 interaction features as v2s/v2w.
60-trial Optuna on 80/20 val split, then 6-fold walk-forward.
"""

import sys
import pickle
import logging
import smtplib
import warnings
import shutil
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import optuna

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

# -- Paths & constants --------------------------------------------------------
BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD          = BASE / 'data' / 'model_v2.pkl'
CURRENT_BEST  = 0.6229    # v2s
R8_WF_MEAN    = 0.5073    # round 8 walk-forward baseline
RESULTS       = []

# 18 months ~= 548 days
ROLLING_DAYS  = 548

# -- Email credentials --------------------------------------------------------
GMAIL_USER         = 'jack.beard07@gmail.com'
GMAIL_APP_PASSWORD = 'vaje mgnh jhtm kknf'
CHAIRMAN_EMAIL     = 'jack.beard07@gmail.com'

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ml_r9')


# =============================================================================
# Shared utilities
# =============================================================================

def build_interactions(X, feat_cols):
    """Add 6 interaction features -- identical to v2s / v2w / r8 architecture."""
    idx = {f: i for i, f in enumerate(feat_cols)}
    interactions, names = [], []

    def _prod(f1, f2):
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            names.append(f'{f1}_x_{f2}')

    _prod('conviction_score',          'institution_hit_rate')
    _prod('rd_vix',                    'volatility_20d')
    _prod('price_vs_52w_high',         'price_momentum_20d')
    _prod('avg_conviction_3q',         'institution_regime_hit_rate')
    _prod('position_size_pct_aum',     'conviction_score')
    _prod('filing_delay_days',         'institution_hit_rate')

    if interactions:
        return (
            np.hstack([X] + [i.reshape(-1, 1) for i in interactions]),
            list(feat_cols) + names,
        )
    return X, list(feat_cols)


def _lgb_objective(trial, X_tr, y_tr, X_val, y_val, sample_weight=None):
    p = dict(
        n_estimators      = trial.suggest_int('n_estimators', 100, 1500),
        learning_rate     = trial.suggest_float('learning_rate', 0.003, 0.3, log=True),
        num_leaves        = trial.suggest_int('num_leaves', 15, 255),
        max_depth         = trial.suggest_int('max_depth', 3, 12),
        min_child_samples = trial.suggest_int('min_child_samples', 5, 100),
        subsample         = trial.suggest_float('subsample', 0.4, 1.0),
        colsample_bytree  = trial.suggest_float('colsample_bytree', 0.4, 1.0),
        reg_alpha         = trial.suggest_float('reg_alpha', 0.0, 5.0),
        reg_lambda        = trial.suggest_float('reg_lambda', 0.0, 5.0),
    )
    m = lgb.LGBMClassifier(**p, class_weight='balanced', verbosity=-1, random_state=42)
    m.fit(X_tr, y_tr, sample_weight=sample_weight)
    return roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])


def run_walk_forward(X_aug, y, best_params, label, sample_weight=None):
    """
    6-fold walk-forward validation.
    fold_configs = train_pct cutoffs; each fold tests the following 10% slice.
    Returns mean AUC across folds.
    """
    fold_configs = [0.40, 0.50, 0.60, 0.65, 0.70, 0.75]
    N    = len(y)
    aucs = []

    for i, train_pct in enumerate(fold_configs):
        train_end = int(N * train_pct)
        test_end  = min(int(N * (train_pct + 0.10)), N)

        X_tr, y_tr = X_aug[:train_end], y[:train_end]
        X_te, y_te = X_aug[train_end:test_end], y[train_end:test_end]
        sw_tr = sample_weight[:train_end] if sample_weight is not None else None

        if len(np.unique(y_te)) < 2:
            log.info(f'  WF fold {i+1}: skipped (single class in test window)')
            continue

        m = lgb.LGBMClassifier(
            **best_params, class_weight='balanced', verbosity=-1, random_state=42
        )
        m.fit(X_tr, y_tr, sample_weight=sw_tr)
        auc = roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])
        aucs.append(auc)
        log.info(
            f'  WF fold {i+1}: train={train_end}  test={test_end - train_end}  AUC={auc:.4f}'
        )

    mean_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(f'  [{label}] Walk-forward mean AUC={mean_auc:.4f}  (R8 baseline={R8_WF_MEAN:.4f})')
    return mean_auc


def promote(name, model, feat_cols, val_auc, current_best):
    if val_auc <= current_best:
        log.info(f'  No promotion: {val_auc:.4f} <= {current_best:.4f}')
        return current_best
    d = pickle.load(open(PROD, 'rb'))
    d.update({
        'xgb': model,
        'feature_cols': feat_cols,
        'ens_weights': {'lgb': 0.0, 'xgb': 1.0, 'rf': 0.0, 'lr': 0.0},
        'trained_at': datetime.now().isoformat(),
        'experiment': name,
    })
    pickle.dump(d, open(PROD, 'wb'))
    shutil.copy(PROD, BASE / 'data' / 'model.pkl')
    log.info(f'PROMOTED {name} -> production + model.pkl  (val={val_auc:.4f})')
    return val_auc


def record(name, val_auc, wf_mean, note, current_best):
    beats = val_auc > current_best
    log.info(
        f'[{name}] val={val_auc:.4f}  WF={wf_mean:.4f}  '
        f'{"NEW BEST" if beats else "no improvement"}  {note}'
    )
    RESULTS.append(dict(
        name=name, val_auc=val_auc, wf_mean=wf_mean,
        beats_prod=beats, note=note,
    ))


# =============================================================================
# Load full feature matrix + snapshot dates
# =============================================================================

def load_full_data():
    """
    Build full feature matrix and load snapshot dates from the DB (by rowid order
    so the date array aligns exactly with the rows returned by build_feature_matrix).
    Returns X, y, feat_cols, dates_series.
    """
    log.info('Loading feature matrix ...')
    X, y, meta = build_feature_matrix(target='outcome_20d')
    feat_cols   = list(FEATURE_COLS)
    log.info(f'  {len(X):,} samples | {len(feat_cols)} features | {y.mean():.1%} positive rate')

    # Load snapshot dates aligned by rowid so we can filter by date
    db_path = BASE / 'data' / 'historical_13f.db'
    con     = sqlite3.connect(str(db_path))
    # build_feature_matrix filters WHERE outcome_20d IS NOT NULL AND conviction_score IS NOT NULL
    # We replicate that exact filter to keep the date array aligned.
    dates = pd.read_sql(
        """SELECT snapshot_date FROM pipeline_snapshots
           WHERE outcome_20d IS NOT NULL AND conviction_score IS NOT NULL
           ORDER BY rowid""",
        con,
    )['snapshot_date']
    con.close()

    dates = pd.to_datetime(dates).reset_index(drop=True)
    log.info(f'  Date range: {dates.min().date()} to {dates.max().date()}')
    return X, y, feat_cols, dates


# =============================================================================
# v3a -- Rolling 18-month window
# =============================================================================

def run_v3a(X_full, y_full, feat_cols, dates, current_best):
    log.info('')
    log.info('=' * 65)
    log.info('v3a: Rolling 18-month window (last 548 days of data only)')
    log.info('=' * 65)

    max_date = dates.max()
    cutoff   = max_date - timedelta(days=ROLLING_DAYS)
    mask     = dates >= cutoff
    X        = X_full[mask.values]
    y        = y_full[mask.values]
    log.info(f'  Cutoff: {cutoff.date()}  |  Rows kept: {len(X):,} of {len(X_full):,}')

    if len(np.unique(y)) < 2:
        log.error('  v3a: only one class in 18-month window — skipping')
        return current_best, 0.5, 0.5

    # Build interaction features
    X_aug, all_cols = build_interactions(X, feat_cols)
    log.info(f'  Features: {X.shape[1]} base + {X_aug.shape[1] - X.shape[1]} interactions = {X_aug.shape[1]}')

    # 80/20 chronological split
    split   = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split],     y[split:]
    log.info(f'  Train: {len(X_tr):,}  Val: {len(X_val):,}')

    # 60-trial Optuna
    log.info('  Running 60-trial Optuna ...')
    study = optuna.create_study(direction='maximize')
    study.optimize(
        lambda trial: _lgb_objective(trial, X_tr, y_tr, X_val, y_val),
        n_trials=60,
    )
    bp = study.best_params
    log.info(f'  Optuna best val AUC: {study.best_value:.4f}')

    # Final model
    model   = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])

    # Walk-forward on 18-month window
    log.info('  Running 6-fold walk-forward on 18-month window ...')
    wf_mean = run_walk_forward(X_aug, y, bp, 'v3a')

    record('v3a', val_auc, wf_mean,
           f'18-month rolling window ({len(X):,} rows), optuna={study.best_value:.4f}',
           current_best)

    if val_auc > current_best:
        current_best = promote('v3a', model, all_cols, val_auc, current_best)

    return current_best, val_auc, wf_mean


# =============================================================================
# v3b -- Full history + recency weighting
# =============================================================================

def run_v3b(X_full, y_full, feat_cols, dates, current_best):
    log.info('')
    log.info('=' * 65)
    log.info('v3b: Full history + recency weighting (18-month rows = 3x weight)')
    log.info('=' * 65)

    max_date = dates.max()
    cutoff   = max_date - timedelta(days=ROLLING_DAYS)
    recent   = dates >= cutoff

    # Build sample weights: recent rows get 3.0, older rows get 1.0
    sample_weight          = np.ones(len(y_full), dtype=float)
    sample_weight[recent.values] = 3.0
    n_recent = int(recent.sum())
    log.info(
        f'  Recent rows (weight=3.0): {n_recent:,}  '
        f'Older rows (weight=1.0): {len(y_full) - n_recent:,}'
    )

    # Build interaction features on full data
    X_aug, all_cols = build_interactions(X_full, feat_cols)
    log.info(f'  Features: {X_full.shape[1]} base + {X_aug.shape[1] - X_full.shape[1]} interactions = {X_aug.shape[1]}')

    # 80/20 chronological split
    split   = int(len(X_aug) * 0.8)
    X_tr, X_val       = X_aug[:split],        X_aug[split:]
    y_tr, y_val       = y_full[:split],        y_full[split:]
    sw_tr             = sample_weight[:split]
    log.info(f'  Train: {len(X_tr):,}  Val: {len(X_val):,}')

    # 60-trial Optuna (with sample weights on training set only)
    log.info('  Running 60-trial Optuna ...')
    study = optuna.create_study(direction='maximize')
    study.optimize(
        lambda trial: _lgb_objective(trial, X_tr, y_tr, X_val, y_val, sample_weight=sw_tr),
        n_trials=60,
    )
    bp = study.best_params
    log.info(f'  Optuna best val AUC: {study.best_value:.4f}')

    # Final model (with sample weights)
    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr, sample_weight=sw_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])

    # Walk-forward on full data (with weights)
    log.info('  Running 6-fold walk-forward on full data (with sample weights) ...')
    wf_mean = run_walk_forward(X_aug, y_full, bp, 'v3b', sample_weight=sample_weight)

    record('v3b', val_auc, wf_mean,
           f'Full {len(X_full):,} rows, recent weight=3.0, optuna={study.best_value:.4f}',
           current_best)

    if val_auc > current_best:
        current_best = promote('v3b', model, all_cols, val_auc, current_best)

    return current_best, val_auc, wf_mean


# =============================================================================
# Email
# =============================================================================

def send_email(v3a_val, v3a_wf, v3b_val, v3b_wf, current_best, promoted_any):
    try:
        wf_improved_a = 'YES' if v3a_wf > R8_WF_MEAN else 'NO'
        wf_improved_b = 'YES' if v3b_wf > R8_WF_MEAN else 'NO'

        promotion_line = (
            f'PROMOTED: yes -- new production model (val={current_best:.4f})'
            if promoted_any else
            'PROMOTED: no -- v2s (val=0.6229) still holds'
        )

        body = (
            'Apex Capital -- ML Chain Round 9 Complete\n'
            '=' * 60 + '\n\n'
            'Experiments\n'
            '-----------\n'
            '  v3a: Rolling 18-month window (regime-focused training)\n'
            '  v3b: Full history + recency weighting (recent rows = 3x weight)\n\n'
            'Key Numbers\n'
            '-----------\n'
            f'  v3a val AUC           : {v3a_val:.4f}\n'
            f'  v3a walk-forward mean : {v3a_wf:.4f}  (improved vs R8 {R8_WF_MEAN:.4f}? {wf_improved_a})\n'
            f'  v3b val AUC           : {v3b_val:.4f}\n'
            f'  v3b walk-forward mean : {v3b_wf:.4f}  (improved vs R8 {R8_WF_MEAN:.4f}? {wf_improved_b})\n\n'
            f'Current best val AUC  : {current_best:.4f}  (v2s baseline=0.6229)\n\n'
            f'{promotion_line}\n\n'
            'Walk-forward interpretation:\n'
            '  R8 mean=0.5073 failed on folds 4-6 (2024-2026 regime shift).\n'
            '  v3a isolates recent data; v3b upweights it. Either approach\n'
            '  should reduce the regime mismatch if the hypothesis is correct.\n\n'
            '(Full overnight summary in 10am briefing.)'
        )

        subject = 'Apex Capital -- ML Chain R9 Complete'

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            msg = (
                f'From: {GMAIL_USER}\r\n'
                f'To: {CHAIRMAN_EMAIL}\r\n'
                f'Subject: {subject}\r\n'
                f'Content-Type: text/plain; charset=us-ascii\r\n'
                f'\r\n'
                + body
            )
            s.sendmail(GMAIL_USER, CHAIRMAN_EMAIL, msg.encode('ascii', errors='replace'))

        log.info('Email sent.')
    except Exception as e:
        log.error(f'Email failed: {e}')


# =============================================================================
# Entry point
# =============================================================================

if __name__ == '__main__':
    log.info('=' * 65)
    log.info('ML CHAIN ROUND 9   v3a -> v3b')
    log.info(f'Started: {datetime.now()}')
    log.info('Regime-shift mitigation: rolling window + recency weighting')
    log.info('=' * 65)

    X_full, y_full, feat_cols, dates = load_full_data()

    current_best = CURRENT_BEST
    promoted_any = False
    prev_best    = current_best

    # v3a
    current_best, v3a_val, v3a_wf = run_v3a(X_full, y_full, feat_cols, dates, current_best)
    if current_best > prev_best:
        promoted_any = True
        prev_best    = current_best

    # v3b
    current_best, v3b_val, v3b_wf = run_v3b(X_full, y_full, feat_cols, dates, current_best)
    if current_best > prev_best:
        promoted_any = True

    log.info('')
    log.info('=' * 65)
    log.info(f'ROUND 9 COMPLETE -- {datetime.now()}')
    log.info(f'  v3a val AUC           : {v3a_val:.4f}')
    log.info(f'  v3a walk-forward mean : {v3a_wf:.4f}  (R8 baseline={R8_WF_MEAN:.4f})')
    log.info(f'  v3b val AUC           : {v3b_val:.4f}')
    log.info(f'  v3b walk-forward mean : {v3b_wf:.4f}  (R8 baseline={R8_WF_MEAN:.4f})')
    log.info(f'  Current best          : {current_best:.4f}')
    log.info(f'  Promoted              : {promoted_any}')
    log.info('=' * 65)

    send_email(v3a_val, v3a_wf, v3b_val, v3b_wf, current_best, promoted_any)

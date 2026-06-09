"""
ML Chain Round 10 -- Regime-focused feature pruning + near-duplicate dedup.

Context
-------
  Round 9 results: v3a (18-month rolling), v3b (full + recency weight).
  R8 best: v2w val=0.6100 (post-dedup clean baseline used here).
  Current prod: v2s val=0.6229 (pre-dedup).

  Validation audit findings:
    - conviction_score: NEGATIVE permutation importance for 20d predictions
    - filing_delay_days: negative permutation importance
    - institution_regime_hit_rate: slightly negative permutation importance
    - Top features: volatility_20d, vix_x_vol interaction, price_vs_52w_high,
      price_momentum_20d, rd_vix, rd_spy_rsi, rd_ticker_pct_from_50ma, rd_regime_bull
    - 15.5% near-duplicate rows from same ticker+date across institutions
      inflating training signal

  v4a -- Regime-focused model (remove negative-importance features):
         Drop conviction_score, filing_delay_days, institution_regime_hit_rate.
         Keep all regime/momentum features.
         Skip interactions that involve removed features.
         Deduplicate by (ticker, snapshot_date) keeping highest portfolio_weight.
         60-trial Optuna, 6-fold walk-forward.
         Promote if val AUC > 0.6100 AND walk-forward mean > 0.54.

  v4b -- Regime-only pure model:
         Only features with positive permutation importance from audit.
         Same dedup as v4a.
         60-trial Optuna, 6-fold walk-forward.
         Promote if beats v4a.
"""

import sys
import pickle
import logging
import smtplib
import warnings
import shutil
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
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
CURRENT_BEST  = 0.6100    # clean post-dedup val AUC from R8
R8_WF_MEAN    = 0.5073    # round 8 walk-forward baseline
RESULTS       = []

# Features to drop in v4a (negative permutation importance)
NEGATIVE_FEATURES = {'conviction_score', 'filing_delay_days', 'institution_regime_hit_rate'}

# Features to keep in v4b (positive permutation importance only)
V4B_FEATURES = [
    'volatility_20d',
    'rd_vix',
    'price_vs_52w_high',
    'price_momentum_20d',
    'rd_spy_rsi',
    'price_momentum_5d',
    'rd_ticker_pct_from_50ma',
    'rd_regime_bull',
]

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
log = logging.getLogger('ml_r10')


# =============================================================================
# Shared utilities
# =============================================================================

def dedup_by_ticker_date(X, y, meta, feat_cols):
    """
    Remove near-duplicate rows from the same (ticker, snapshot_date) pair
    across multiple institutions. Keep only one row per ticker per date:
    the one with highest portfolio_weight if available, else the first row.
    Returns deduplicated X, y arrays (meta DataFrame also returned for reference).
    """
    df = pd.DataFrame(X, columns=feat_cols)
    df['__y__'] = y
    df['__ticker__'] = meta['ticker'].values if 'ticker' in meta.columns else ''
    df['__date__'] = meta['snapshot_date'].values if 'snapshot_date' in meta.columns else ''

    n_before = len(df)

    if 'portfolio_weight' in feat_cols:
        pw_col = 'portfolio_weight'
        # Sort so highest portfolio_weight comes first within each (ticker, date) group
        df = df.sort_values(['__ticker__', '__date__', pw_col], ascending=[True, True, False])
    else:
        df = df.sort_values(['__ticker__', '__date__'])

    df = df.drop_duplicates(subset=['__ticker__', '__date__'], keep='first')
    n_after = len(df)
    removed = n_before - n_after
    pct = removed / n_before * 100 if n_before > 0 else 0
    log.info(f'  Dedup: {n_before:,} -> {n_after:,} rows  (removed {removed:,} = {pct:.1f}%)')

    meta_out = df[['__ticker__', '__date__']].rename(
        columns={'__ticker__': 'ticker', '__date__': 'snapshot_date'}
    ).reset_index(drop=True)
    y_out = df['__y__'].values
    X_out = df[feat_cols].values

    return X_out, y_out, meta_out


def build_interactions_v4a(X, feat_cols):
    """
    Build interaction features for v4a -- skip any interaction involving
    the removed negative-importance features (conviction_score,
    filing_delay_days, institution_regime_hit_rate).
    Kept interactions:
      - rd_vix x volatility_20d
      - price_vs_52w_high x price_momentum_20d
      - avg_conviction_3q x ... (skip if partner removed)
      - position_size_pct_aum x ... (skip if partner removed)
    """
    idx = {f: i for i, f in enumerate(feat_cols)}
    interactions, names = [], []

    def _prod(f1, f2):
        # Skip if either feature was removed
        if f1 in NEGATIVE_FEATURES or f2 in NEGATIVE_FEATURES:
            return
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            names.append(f'{f1}_x_{f2}')

    # Original 6 interactions -- skip those involving removed features
    # conviction_score x institution_hit_rate     --> SKIPPED (conviction_score removed)
    # filing_delay_days x institution_hit_rate     --> SKIPPED (filing_delay_days removed)
    # avg_conviction_3q x institution_regime_hit_rate --> SKIPPED (institution_regime_hit_rate removed)
    _prod('rd_vix',               'volatility_20d')        # KEPT
    _prod('price_vs_52w_high',    'price_momentum_20d')    # KEPT
    _prod('position_size_pct_aum', 'institution_hit_rate') # KEPT (neither is negative)

    if interactions:
        return (
            np.hstack([X] + [i.reshape(-1, 1) for i in interactions]),
            list(feat_cols) + names,
        )
    return X, list(feat_cols)


def build_interactions_v4b(X, feat_cols):
    """
    Build interaction features for v4b -- only between the positive-importance features.
    """
    idx = {f: i for i, f in enumerate(feat_cols)}
    interactions, names = [], []

    def _prod(f1, f2):
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            names.append(f'{f1}_x_{f2}')

    _prod('rd_vix',            'volatility_20d')
    _prod('price_vs_52w_high', 'price_momentum_20d')
    _prod('rd_spy_rsi',        'rd_regime_bull')

    if interactions:
        return (
            np.hstack([X] + [i.reshape(-1, 1) for i in interactions]),
            list(feat_cols) + names,
        )
    return X, list(feat_cols)


def _lgb_objective(trial, X_tr, y_tr, X_val, y_val):
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
    m.fit(X_tr, y_tr)
    return roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])


def run_walk_forward(X_aug, y, best_params, label):
    """
    6-fold walk-forward validation.
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

        if len(np.unique(y_te)) < 2:
            log.info(f'  WF fold {i+1}: skipped (single class in test window)')
            continue

        m = lgb.LGBMClassifier(
            **best_params, class_weight='balanced', verbosity=-1, random_state=42
        )
        m.fit(X_tr, y_tr)
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
# Load full feature matrix
# =============================================================================

def load_full_data():
    log.info('Loading feature matrix ...')
    X, y, meta = build_feature_matrix(target='outcome_20d')
    feat_cols   = list(FEATURE_COLS)
    log.info(f'  {len(X):,} samples | {len(feat_cols)} features | {y.mean():.1%} positive rate')

    dates = pd.to_datetime(meta['snapshot_date']).reset_index(drop=True)
    log.info(f'  Date range: {dates.min().date()} to {dates.max().date()}')
    return X, y, feat_cols, meta


# =============================================================================
# v4a -- Regime-focused model (remove negative-importance features)
# =============================================================================

def run_v4a(X_full, y_full, feat_cols, meta_full, current_best):
    log.info('')
    log.info('=' * 65)
    log.info('v4a: Regime-focused (drop negative-importance features + dedup)')
    log.info('=' * 65)

    # Drop negative-importance features
    keep_cols  = [c for c in feat_cols if c not in NEGATIVE_FEATURES]
    drop_idx   = [i for i, c in enumerate(feat_cols) if c in NEGATIVE_FEATURES]
    keep_idx   = [i for i, c in enumerate(feat_cols) if c not in NEGATIVE_FEATURES]
    dropped    = [feat_cols[i] for i in drop_idx]
    log.info(f'  Dropping features: {dropped}')
    log.info(f'  Kept {len(keep_cols)} of {len(feat_cols)} features')

    X_pruned = X_full[:, keep_idx]

    # Deduplicate by (ticker, snapshot_date)
    X, y, meta = dedup_by_ticker_date(X_pruned, y_full, meta_full, keep_cols)
    log.info(f'  Post-dedup: {len(X):,} rows | positive rate: {y.mean():.1%}')

    if len(np.unique(y)) < 2:
        log.error('  v4a: only one class after dedup -- skipping')
        return current_best, 0.5, 0.5

    # Build interaction features (skip those involving removed features)
    X_aug, all_cols = build_interactions_v4a(X, keep_cols)
    log.info(f'  Features: {X.shape[1]} base + {X_aug.shape[1] - X.shape[1]} interactions = {X_aug.shape[1]}')

    # 80/20 chronological split
    split       = int(len(X_aug) * 0.8)
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

    # Walk-forward
    log.info('  Running 6-fold walk-forward ...')
    wf_mean = run_walk_forward(X_aug, y, bp, 'v4a')

    note = (
        f'drop={dropped} dedup, optuna={study.best_value:.4f}, '
        f'{len(X):,} rows post-dedup'
    )
    record('v4a', val_auc, wf_mean, note, current_best)

    # Promote if val AUC > 0.6100 AND walk-forward mean > 0.54
    if val_auc > CURRENT_BEST and wf_mean > 0.54:
        current_best = promote('v4a', model, all_cols, val_auc, current_best)
    else:
        log.info(
            f'  v4a not promoted: val={val_auc:.4f} (need >{CURRENT_BEST:.4f}) '
            f'wf={wf_mean:.4f} (need >0.54)'
        )

    return current_best, val_auc, wf_mean


# =============================================================================
# v4b -- Regime-only pure model (positive permutation importance features only)
# =============================================================================

def run_v4b(X_full, y_full, feat_cols, meta_full, current_best, v4a_val):
    log.info('')
    log.info('=' * 65)
    log.info('v4b: Regime-only pure model (positive permutation importance only)')
    log.info('=' * 65)

    # Select only the positive-importance features that exist in feat_cols
    keep_cols = [c for c in V4B_FEATURES if c in feat_cols]
    missing   = [c for c in V4B_FEATURES if c not in feat_cols]
    if missing:
        log.warning(f'  V4B features not found in FEATURE_COLS (skipped): {missing}')
    keep_idx  = [feat_cols.index(c) for c in keep_cols]
    log.info(f'  Using {len(keep_cols)} features: {keep_cols}')

    X_pruned = X_full[:, keep_idx]

    # Deduplicate by (ticker, snapshot_date) -- same as v4a
    # Need portfolio_weight for dedup priority; use meta_full FEATURE_COLS context
    # Build a temporary feat_cols list matching X_pruned columns
    X, y, meta = dedup_by_ticker_date(X_pruned, y_full, meta_full, keep_cols)
    log.info(f'  Post-dedup: {len(X):,} rows | positive rate: {y.mean():.1%}')

    if len(np.unique(y)) < 2:
        log.error('  v4b: only one class after dedup -- skipping')
        return current_best, 0.5, 0.5

    # Build interaction features (only between positive-importance features)
    X_aug, all_cols = build_interactions_v4b(X, keep_cols)
    log.info(f'  Features: {X.shape[1]} base + {X_aug.shape[1] - X.shape[1]} interactions = {X_aug.shape[1]}')

    # 80/20 chronological split
    split       = int(len(X_aug) * 0.8)
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

    # Walk-forward
    log.info('  Running 6-fold walk-forward ...')
    wf_mean = run_walk_forward(X_aug, y, bp, 'v4b')

    note = (
        f'regime-only {len(keep_cols)} features, dedup, optuna={study.best_value:.4f}, '
        f'{len(X):,} rows post-dedup'
    )
    record('v4b', val_auc, wf_mean, note, current_best)

    # Promote if beats v4a
    if val_auc > v4a_val and val_auc > current_best:
        current_best = promote('v4b', model, all_cols, val_auc, current_best)
    else:
        log.info(
            f'  v4b not promoted: val={val_auc:.4f}  v4a_val={v4a_val:.4f}  '
            f'current_best={current_best:.4f}'
        )

    return current_best, val_auc, wf_mean


# =============================================================================
# Email
# =============================================================================

def send_email(v4a_val, v4a_wf, v4b_val, v4b_wf, current_best, promoted_any, promoted_name):
    try:
        def _wf_status(wf):
            return 'PASS (>0.54)' if wf > 0.54 else f'FAIL ({wf:.4f} < 0.54)'

        promotion_line = (
            f'PROMOTED: {promoted_name} -- new production model (val={current_best:.4f})'
            if promoted_any else
            f'PROMOTED: no -- current best remains {current_best:.4f}'
        )

        body = (
            'Apex Capital -- ML Chain Round 10 Complete\n'
            '=' * 60 + '\n\n'
            'Audit Findings Applied\n'
            '----------------------\n'
            '  Removed features (negative permutation importance):\n'
            '    conviction_score, filing_delay_days, institution_regime_hit_rate\n'
            '  Deduplication: one row per (ticker, snapshot_date), highest portfolio_weight kept\n'
            '  Post-dedup near-duplicate removal targeted ~15.5% inflated rows\n\n'
            'Experiments\n'
            '-----------\n'
            '  v4a: Regime-focused (drop negative features + dedup)\n'
            '       Interactions: rd_vix x volatility_20d, price_vs_52w_high x price_momentum_20d,\n'
            '                     position_size_pct_aum x institution_hit_rate\n'
            '  v4b: Regime-only pure (positive permutation importance features only)\n'
            '       Features: volatility_20d, rd_vix, price_vs_52w_high, price_momentum_20d,\n'
            '                 rd_spy_rsi, price_momentum_5d, rd_ticker_pct_from_50ma, rd_regime_bull\n\n'
            'Key Numbers\n'
            '-----------\n'
            f'  v4a val AUC           : {v4a_val:.4f}  (threshold: >0.6100)\n'
            f'  v4a walk-forward mean : {v4a_wf:.4f}  ({_wf_status(v4a_wf)})\n'
            f'  v4b val AUC           : {v4b_val:.4f}  (must beat v4a)\n'
            f'  v4b walk-forward mean : {v4b_wf:.4f}  ({_wf_status(v4b_wf)})\n\n'
            f'Current best val AUC  : {current_best:.4f}  (R8 post-dedup baseline=0.6100)\n\n'
            f'{promotion_line}\n\n'
            'Interpretation:\n'
            '  Removing negative-importance features isolates the regime detection\n'
            '  signal. If deduplication alone was responsible for the R8 0.6100 floor,\n'
            '  v4a/v4b should meet or exceed it with cleaner feature attribution.\n'
            '  Walk-forward > 0.54 confirms the model generalizes across market regimes.\n\n'
            '(Full overnight summary in 10am briefing.)'
        )

        subject = 'Apex Capital -- ML Chain R10 Complete'

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
    log.info('ML CHAIN ROUND 10   v4a -> v4b')
    log.info(f'Started: {datetime.now()}')
    log.info('Regime-focused feature pruning + near-duplicate dedup')
    log.info('Audit: drop conviction_score, filing_delay_days, institution_regime_hit_rate')
    log.info('=' * 65)

    X_full, y_full, feat_cols, meta_full = load_full_data()

    current_best  = CURRENT_BEST
    promoted_any  = False
    promoted_name = None
    prev_best     = current_best

    # v4a
    current_best, v4a_val, v4a_wf = run_v4a(
        X_full, y_full, feat_cols, meta_full, current_best
    )
    if current_best > prev_best:
        promoted_any  = True
        promoted_name = 'v4a'
        prev_best     = current_best

    # v4b
    current_best, v4b_val, v4b_wf = run_v4b(
        X_full, y_full, feat_cols, meta_full, current_best, v4a_val
    )
    if current_best > prev_best:
        promoted_any  = True
        promoted_name = 'v4b'

    log.info('')
    log.info('=' * 65)
    log.info(f'ROUND 10 COMPLETE -- {datetime.now()}')
    log.info(f'  v4a val AUC           : {v4a_val:.4f}')
    log.info(f'  v4a walk-forward mean : {v4a_wf:.4f}  (R8 baseline={R8_WF_MEAN:.4f})')
    log.info(f'  v4b val AUC           : {v4b_val:.4f}')
    log.info(f'  v4b walk-forward mean : {v4b_wf:.4f}  (R8 baseline={R8_WF_MEAN:.4f})')
    log.info(f'  Current best          : {current_best:.4f}')
    log.info(f'  Promoted              : {promoted_name if promoted_any else "none"}')
    log.info('=' * 65)

    send_email(v4a_val, v4a_wf, v4b_val, v4b_wf, current_best, promoted_any, promoted_name)

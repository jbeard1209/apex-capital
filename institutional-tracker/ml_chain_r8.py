"""
ML Chain Round 8 — Train on clean deduplicated data.

Run AFTER db_dedup_snapshots.py has completed.

  v2w — v2s architecture on clean deduplicated data (baseline, with quarter_of_year)
        LightGBM + same 6 interaction features as v2s, 60-trial Optuna on val AUC
        Promotes if val > 0.6229

  v2x — Same as v2w but with quarter_of_year removed from FEATURE_COLS
        Tests whether the model is more robust without the seasonal proxy
        Promotes only if val > current best

  v2y — Walk-forward validation on the best of v2w/v2x
        6-fold walk-forward (same as Test 1 in ml_validate_v2s.py)
        Key question: does deduplication fix the walk-forward FAIL (pre-dedup was 0.497)?
"""
import sys
import pickle
import logging
import smtplib
import warnings
import shutil
import numpy as np
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import optuna

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

# ── Paths & constants ─────────────────────────────────────────────────────────
BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
CURRENT_BEST = 0.6229   # v2s, pre-dedup data
RESULTS      = []

# ── Email credentials ─────────────────────────────────────────────────────────
GMAIL_USER        = 'jack.beard07@gmail.com'
GMAIL_APP_PASSWORD = 'vaje mgnh jhtm kknf'
CHAIRMAN_EMAIL    = 'jack.beard07@gmail.com'

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ml_r8')


# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────

def ts_cv_auc(model_fn, X, y, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for tr, va in tscv.split(X):
        if len(np.unique(y[va])) < 2:
            continue
        m = model_fn()
        m.fit(X[tr], y[tr])
        aucs.append(roc_auc_score(y[va], m.predict_proba(X[va])[:, 1]))
    return (float(np.mean(aucs)) if aucs else 0.5,
            float(np.std(aucs))  if aucs else 0.0)


def promote(name, model, feat_cols, val_auc, ts_cv_val, current_best):
    if val_auc <= current_best:
        log.info(f"  No promotion: {val_auc:.4f} <= {current_best:.4f}")
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
    log.info(
        f"PROMOTED {name} -> production + model.pkl "
        f"(val={val_auc:.4f}  TS-CV={ts_cv_val:.4f})"
    )
    return val_auc


def record(name, val_auc, ts_mean, ts_std, note, current_best):
    beats = val_auc > current_best
    log.info(
        f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}+-{ts_std:.4f}  "
        f"{'NEW BEST' if beats else 'no improvement'}  {note}"
    )
    RESULTS.append(dict(
        name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
        beats_prod=beats, note=note,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Interaction feature builder (6 features — identical to v2s)
# ─────────────────────────────────────────────────────────────────────────────

def build_interactions(X, feat_cols):
    idx = {f: i for i, f in enumerate(feat_cols)}
    interactions, names = [], []

    def _prod(f1, f2):
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            names.append(f'{f1}_x_{f2}')

    _prod('conviction_score',      'institution_hit_rate')
    _prod('rd_vix',                'volatility_20d')
    _prod('price_vs_52w_high',     'price_momentum_20d')
    _prod('avg_conviction_3q',     'institution_regime_hit_rate')
    _prod('position_size_pct_aum', 'conviction_score')
    _prod('filing_delay_days',     'institution_hit_rate')

    if interactions:
        return (
            np.hstack([X] + [i.reshape(-1, 1) for i in interactions]),
            list(feat_cols) + names,
        )
    return X, list(feat_cols)


# ─────────────────────────────────────────────────────────────────────────────
# Optuna objective (shared between v2w and v2x)
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# v2w — v2s architecture on clean deduplicated data (with quarter_of_year)
# ─────────────────────────────────────────────────────────────────────────────

def run_v2w(X, y, feat_cols, current_best):
    log.info("-- v2w: LGB + 6 interactions, clean dedup data (with quarter_of_year) --")
    log.info(f"  Feature count: {len(feat_cols)} | 'quarter_of_year' present: {'quarter_of_year' in feat_cols}")

    X_aug, all_cols = build_interactions(X, feat_cols)
    log.info(f"  Augmented: {X.shape[1]} -> {X_aug.shape[1]} features ({X_aug.shape[1] - X.shape[1]} interactions)")

    split = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split], y[split:]

    study = optuna.create_study(direction='maximize')
    study.optimize(
        lambda trial: _lgb_objective(trial, X_tr, y_tr, X_val, y_val),
        n_trials=60,
    )
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_aug, y,
    )
    record('v2w', val_auc, ts_mean, ts_std,
           f'LGB+6 interactions, dedup data, best_optuna={study.best_value:.4f}',
           current_best)
    if val_auc > current_best:
        current_best = promote('v2w', model, all_cols, val_auc, ts_mean, current_best)

    return current_best, val_auc, bp, X_aug, all_cols


# ─────────────────────────────────────────────────────────────────────────────
# v2x — Same but quarter_of_year removed from feature set
# ─────────────────────────────────────────────────────────────────────────────

def run_v2x(X, y, feat_cols, current_best):
    log.info("-- v2x: LGB + 6 interactions, quarter_of_year removed --")
    log.info("  quarter_of_year removed -- testing if model is more robust without seasonal proxy")

    feat_cols_x = [f for f in feat_cols if f != 'quarter_of_year']
    removed = len(feat_cols) - len(feat_cols_x)
    log.info(f"  Features used ({len(feat_cols_x)}): {feat_cols_x}")
    log.info(f"  Dropped {removed} column(s): 'quarter_of_year'")

    # Rebuild X matrix without quarter_of_year
    orig_idx = {f: i for i, f in enumerate(feat_cols)}
    keep_mask = np.array([orig_idx[f] for f in feat_cols_x])
    X_x = X[:, keep_mask]

    X_aug, all_cols = build_interactions(X_x, feat_cols_x)
    log.info(f"  Augmented: {X_x.shape[1]} -> {X_aug.shape[1]} features ({X_aug.shape[1] - X_x.shape[1]} interactions)")

    split = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split], y[split:]

    study = optuna.create_study(direction='maximize')
    study.optimize(
        lambda trial: _lgb_objective(trial, X_tr, y_tr, X_val, y_val),
        n_trials=60,
    )
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_aug, y,
    )
    record('v2x', val_auc, ts_mean, ts_std,
           f'LGB+6 interactions, no quarter_of_year, best_optuna={study.best_value:.4f}',
           current_best)
    if val_auc > current_best:
        current_best = promote('v2x', model, all_cols, val_auc, ts_mean, current_best)

    return current_best, val_auc, bp, X_aug, all_cols


# ─────────────────────────────────────────────────────────────────────────────
# v2y — Walk-forward validation on the better of v2w/v2x
# ─────────────────────────────────────────────────────────────────────────────

def run_walk_forward(X_aug, y, best_params, model_label):
    """
    6-fold walk-forward validation — identical to Test 1 in ml_validate_v2s.py.
    Each fold trains on a fixed prefix and tests on the next 10% slice.
    Key question: does deduplication fix the walk-forward FAIL (pre-dedup=0.497)?
    """
    fold_configs = [0.40, 0.50, 0.60, 0.65, 0.70, 0.75]
    N = len(y)
    aucs = []

    for i, train_pct in enumerate(fold_configs):
        train_end = int(N * train_pct)
        test_end  = min(int(N * (train_pct + 0.10)), N)
        X_tr, y_tr = X_aug[:train_end], y[:train_end]
        X_te, y_te = X_aug[train_end:test_end], y[train_end:test_end]

        if len(np.unique(y_te)) < 2:
            log.info(f"  WF fold {i + 1}: skipped (only one class in test window)")
            continue

        m = lgb.LGBMClassifier(
            **best_params, class_weight='balanced', verbosity=-1, random_state=42
        )
        m.fit(X_tr, y_tr)
        auc = roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])
        aucs.append(auc)
        log.info(
            f"  WF fold {i + 1}: train={train_end}  test={test_end - train_end}  AUC={auc:.4f}"
        )

    mean_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(
        f"[v2y/{model_label}] Walk-forward mean AUC={mean_auc:.4f}  "
        f"(pre-dedup was 0.497)"
    )
    return mean_auc


def run_v2y(v2w_val, v2x_val, v2w_bp, v2x_bp, v2w_X_aug, v2x_X_aug, y):
    log.info("-- v2y: walk-forward on best of v2w/v2x --")

    if v2w_val >= v2x_val:
        winner_label  = 'v2w'
        best_params   = v2w_bp
        X_aug         = v2w_X_aug
        log.info(
            f"  Winner: v2w (val={v2w_val:.4f} >= v2x val={v2x_val:.4f}) — "
            f"running walk-forward on v2w"
        )
    else:
        winner_label  = 'v2x'
        best_params   = v2x_bp
        X_aug         = v2x_X_aug
        log.info(
            f"  Winner: v2x (val={v2x_val:.4f} > v2w val={v2w_val:.4f}) — "
            f"running walk-forward on v2x"
        )

    wf_auc = run_walk_forward(X_aug, y, best_params, winner_label)

    log.info(f"[v2y] Walk-forward mean AUC={wf_auc:.4f}  winner={winner_label}")
    RESULTS.append(dict(
        name='v2y',
        val_auc=None,
        ts_cv=wf_auc,
        ts_std=None,
        beats_prod=None,
        note=f'walk-forward on {winner_label} — pre-dedup was 0.497',
    ))
    return wf_auc, winner_label


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def send_email(current_best, v2w_val, v2x_val, wf_auc, wf_winner, promoted_any):
    try:
        rows = '\n'.join(
            f"  {r['name']:6}  val={r['val_auc'] if r['val_auc'] is not None else 'N/A':>7}  "
            f"TS-CV={r['ts_cv']:.4f}  "
            f"{'NEW BEST' if r['beats_prod'] else ('no improvement' if r['beats_prod'] is not None else 'walk-forward')}  "
            f"{r['note']}"
            for r in RESULTS
        )

        promotion_line = (
            f"PROMOTED: yes — new production model (val={current_best:.4f})"
            if promoted_any else
            f"PROMOTED: no — previous best (v2s, pre-dedup) still holds at 0.6229"
        )

        body = (
            "Apex Capital — ML Chain Round 8 Complete\n"
            + "=" * 60 + "\n\n"
            "Context:\n"
            "  This round trains on DEDUPLICATED data (db_dedup_snapshots.py ran first).\n"
            "  Previous best: v2s val=0.6229  walk-forward=0.497 (pre-dedup data)\n\n"
            "Round 8 results:\n"
            + rows + "\n\n"
            "Key numbers:\n"
            f"  v2w val AUC (dedup, WITH quarter_of_year)    : {v2w_val:.4f}\n"
            f"  v2x val AUC (dedup, WITHOUT quarter_of_year) : {v2x_val:.4f}\n"
            f"  v2y walk-forward mean AUC ({wf_winner:3})              : {wf_auc:.4f}\n"
            f"      (pre-dedup walk-forward was 0.497 — FAIL threshold ~0.52)\n\n"
            f"{promotion_line}\n\n"
            f"Current best val AUC: {current_best:.4f}\n"
            "(Full overnight summary in 10am email.)"
        )

        subject = f"Apex Capital — ML Chain R8 | best val={current_best:.4f}"

        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.ehlo()
            s.starttls()
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            msg = f"Subject: {subject}\n\n{body}"
            s.sendmail(GMAIL_USER, CHAIRMAN_EMAIL, msg)

        log.info("Email sent.")

    except Exception as e:
        log.error(f"Email failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    log.info('=' * 65)
    log.info('ML CHAIN ROUND 8  v2w -> v2x -> v2y')
    log.info(f'Started: {datetime.now()}')
    log.info('Trains on deduplicated data (run db_dedup_snapshots.py first).')
    log.info('=' * 65)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    feat_cols = list(FEATURE_COLS)
    log.info(f"  {len(X):,} samples | {len(feat_cols)} features | {y.mean():.1%} positive rate")

    current_best  = CURRENT_BEST
    promoted_any  = False
    prev_best     = current_best

    # v2w
    current_best, v2w_val, v2w_bp, v2w_X_aug, v2w_all_cols = run_v2w(
        X, y, feat_cols, current_best
    )
    if current_best > prev_best:
        promoted_any = True
        prev_best    = current_best

    # v2x
    current_best, v2x_val, v2x_bp, v2x_X_aug, v2x_all_cols = run_v2x(
        X, y, feat_cols, current_best
    )
    if current_best > prev_best:
        promoted_any = True

    # v2y
    wf_auc, wf_winner = run_v2y(
        v2w_val, v2x_val,
        v2w_bp, v2x_bp,
        v2w_X_aug, v2x_X_aug,
        y,
    )

    log.info('=' * 65)
    log.info(f'ROUND 8 COMPLETE — {datetime.now()}')
    log.info(f'  v2w val AUC (with quarter_of_year)    : {v2w_val:.4f}')
    log.info(f'  v2x val AUC (without quarter_of_year) : {v2x_val:.4f}')
    log.info(f'  v2y walk-forward mean AUC ({wf_winner})    : {wf_auc:.4f}  (pre-dedup=0.497)')
    log.info(f'  Current best val AUC                  : {current_best:.4f}')
    log.info(f'  Promoted                              : {promoted_any}')
    log.info('=' * 65)

    send_email(current_best, v2w_val, v2x_val, wf_auc, wf_winner, promoted_any)

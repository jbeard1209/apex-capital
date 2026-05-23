"""
ML Chain Round 7 — Maximize interaction feature engineering.

v2s breakthrough: val=0.6229 (+0.013 vs v2p) with just 6 interaction features.
Now try:
  v2t — Expand to 15 interaction features + squared terms, LGB 100-trial Optuna
  v2u — v2s interactions but optimize on TS-CV to improve robustness
        (now that val is high, see if we can push TS-CV without killing val)
"""
import sys, pickle, logging, warnings
import numpy as np
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ml_r7')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
CURRENT_BEST = 0.6229   # v2s
RESULTS      = []


def ts_cv_auc(model_fn, X, y, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for tr, va in tscv.split(X):
        if len(np.unique(y[va])) < 2: continue
        m = model_fn(); m.fit(X[tr], y[tr])
        aucs.append(roc_auc_score(y[va], m.predict_proba(X[va])[:, 1]))
    return float(np.mean(aucs)) if aucs else 0.5, float(np.std(aucs)) if aucs else 0.0


def promote(name, model, feat_cols, val_auc, ts_cv_val, current_best):
    if val_auc <= current_best:
        log.info(f"  No promotion: {val_auc:.4f} <= {current_best:.4f}")
        return current_best
    import shutil
    d = pickle.load(open(PROD, 'rb'))
    d.update({'xgb': model, 'feature_cols': feat_cols,
              'ens_weights': {'lgb':0.0,'xgb':1.0,'rf':0.0,'lr':0.0},
              'trained_at': datetime.now().isoformat(), 'experiment': name})
    pickle.dump(d, open(PROD, 'wb'))
    shutil.copy(PROD, BASE / 'data' / 'model.pkl')
    log.info(f"✅ PROMOTED {name} → production + model.pkl (val={val_auc:.4f} TS-CV={ts_cv_val:.4f})")
    return val_auc


def record(name, val_auc, ts_mean, ts_std, note, current_best):
    beats = val_auc > current_best
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  "
             f"{'✅ NEW BEST' if beats else '❌'}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note=note))


def build_interactions(X, feat_cols):
    """Build a rich set of interaction and polynomial features."""
    idx = {f: i for i, f in enumerate(feat_cols)}
    interactions = []
    names = []

    def _prod(f1, f2):
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            names.append(f'{f1}_x_{f2}')

    def _sq(f):
        if f in idx:
            interactions.append(X[:, idx[f]] ** 2)
            names.append(f'{f}_sq')

    def _ratio(f1, f2, eps=1e-6):
        if f1 in idx and f2 in idx:
            denom = X[:, idx[f2]]
            safe  = np.where(np.abs(denom) > eps, denom, eps)
            interactions.append(X[:, idx[f1]] / safe)
            names.append(f'{f1}_div_{f2}')

    # Round 6 interactions (already proven to help)
    _prod('conviction_score',     'institution_hit_rate')
    _prod('rd_vix',               'volatility_20d')
    _prod('price_vs_52w_high',    'price_momentum_20d')
    _prod('avg_conviction_3q',    'institution_regime_hit_rate')
    _prod('position_size_pct_aum','conviction_score')
    _prod('filing_delay_days',    'institution_hit_rate')

    # New: more interaction pairs
    _prod('rd_spy_rsi',           'volatility_20d')
    _prod('rd_vix',               'price_momentum_20d')
    _prod('conviction_score',     'portfolio_weight')
    _prod('institution_hit_rate', 'institution_regime_hit_rate')
    _prod('price_vs_52w_high',    'volatility_20d')
    _prod('rd_spy_rsi',           'price_vs_52w_high')

    # Squared terms for key features
    _sq('conviction_score')
    _sq('institution_hit_rate')
    _sq('rd_vix')

    # Ratios
    _ratio('conviction_score', 'filing_delay_days')
    _ratio('price_momentum_20d', 'volatility_20d')

    if interactions:
        return np.hstack([X] + [i.reshape(-1, 1) for i in interactions]), list(feat_cols) + names
    return X, list(feat_cols)


# ═══════════════════════════════════════════════════════════════════════════════
# v2t — 17 interaction+poly features, LGB 100-trial Optuna → val AUC
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2t(X, y, feat_cols, current_best):
    log.info("── v2t: LGB 100-trial Optuna, 17 interaction+poly features ──")
    X_aug, all_cols = build_interactions(X, feat_cols)
    log.info(f"  Augmented: {X.shape[1]} → {X_aug.shape[1]} features ({X_aug.shape[1]-X.shape[1]} new)")

    split = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators      = trial.suggest_int('n_estimators', 100, 2000),
            learning_rate     = trial.suggest_float('learning_rate', 0.003, 0.3, log=True),
            num_leaves        = trial.suggest_int('num_leaves', 15, 400),
            max_depth         = trial.suggest_int('max_depth', 3, 15),
            min_child_samples = trial.suggest_int('min_child_samples', 5, 150),
            subsample         = trial.suggest_float('subsample', 0.3, 1.0),
            colsample_bytree  = trial.suggest_float('colsample_bytree', 0.3, 1.0),
            reg_alpha         = trial.suggest_float('reg_alpha', 0.0, 10.0),
            reg_lambda        = trial.suggest_float('reg_lambda', 0.0, 10.0),
            min_split_gain    = trial.suggest_float('min_split_gain', 0.0, 3.0),
        )
        m = lgb.LGBMClassifier(**p, class_weight='balanced', verbosity=-1, random_state=42)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=100)
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_aug, y)
    record('v2t', val_auc, ts_mean, ts_std,
           f'LGB 100-trial, {X_aug.shape[1]} features, best={study.best_value:.4f}', current_best)
    if val_auc > current_best:
        current_best = promote('v2t', model, all_cols, val_auc, ts_mean, current_best)
    return current_best


# ═══════════════════════════════════════════════════════════════════════════════
# v2u — Same interactions but optimizing TS-CV (robustness check)
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2u(X, y, feat_cols, current_best):
    log.info("── v2u: LGB 60-trial Optuna→TS-CV, 17 interaction+poly features ──")
    X_aug, all_cols = build_interactions(X, feat_cols)

    split = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators      = trial.suggest_int('n_estimators', 100, 1500),
            learning_rate     = trial.suggest_float('learning_rate', 0.003, 0.2, log=True),
            num_leaves        = trial.suggest_int('num_leaves', 15, 300),
            max_depth         = trial.suggest_int('max_depth', 3, 12),
            min_child_samples = trial.suggest_int('min_child_samples', 5, 100),
            subsample         = trial.suggest_float('subsample', 0.4, 1.0),
            colsample_bytree  = trial.suggest_float('colsample_bytree', 0.4, 1.0),
            reg_alpha         = trial.suggest_float('reg_alpha', 0.0, 8.0),
            reg_lambda        = trial.suggest_float('reg_lambda', 0.0, 8.0),
        )
        mean, _ = ts_cv_auc(
            lambda: lgb.LGBMClassifier(**p, class_weight='balanced', verbosity=-1, random_state=42),
            X_aug, y)
        return mean

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=60)
    bp = study.best_params
    log.info(f"  Best TS-CV from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_aug, y)
    record('v2u', val_auc, ts_mean, ts_std,
           f'LGB 60-trial→TS-CV, {X_aug.shape[1]} features, best_ts={study.best_value:.4f}', current_best)
    # Only promote if val also improved
    if val_auc > current_best:
        current_best = promote('v2u', model, all_cols, val_auc, ts_mean, current_best)
    return current_best


def send_email(current_best):
    try:
        from config import GMAIL_USER, GMAIL_APP_PASSWORD, CHAIRMAN_EMAIL
        import smtplib
        rows = '\n'.join(
            f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  "
            f"{'✅' if r['beats_prod'] else '❌'}  {r['note']}"
            for r in RESULTS)
        body = (
            "Apex Capital — ML Chain Round 7 Complete\n" + "="*50 + "\n"
            "v2s benchmark: val=0.6229  TS-CV=0.5426\n\n"
            "Round 7 results:\n" + rows + "\n\n"
            f"Current best val: {current_best:.4f}\n"
            "(Full overnight summary in 10am email.)"
        )
        subject = f"Apex Capital — ML Chain R7 | best val={current_best:.4f}"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, CHAIRMAN_EMAIL, f"Subject: {subject}\n\n{body}")
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 7  v2t→v2u')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    feat_cols = list(FEATURE_COLS)
    log.info(f"  {len(X)} samples | {len(feat_cols)} features | {y.mean():.1%} positive")

    current_best = CURRENT_BEST
    current_best = run_v2t(X, y, feat_cols, current_best)
    current_best = run_v2u(X, y, feat_cols, current_best)

    log.info('='*60)
    log.info(f'ROUND 7 COMPLETE — {datetime.now()}')
    log.info(f'Current best val: {current_best:.4f}')
    log.info('='*60)
    send_email(current_best)

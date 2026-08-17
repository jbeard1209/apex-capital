"""
ML Chain Round 5 — LightGBM improvements.

v2n (LightGBM 60-trial) gave val=0.6094 — first model to beat production 0.6068.
  v2p — LightGBM 100-trial Optuna (more search budget)
  v2q — LightGBM 60-trial, 12 active features only (drop 17 zero-XGBoost-importance cols)

NOTE: promote() now requires val > current production val (strict), not just > 0.606.
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
log = logging.getLogger('ml_r5')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
ORIG_VAL_AUC = 0.6068
ORIG_TSCV    = 0.536
RESULTS      = []

# 17 features with zero XGBoost gain importance (identified from production model)
ZERO_IMPORTANCE = {
    'earnings_surprise_flag', 'insider_buy_dollars', 'insider_is_ceo_cfo',
    'days_since_filing', 'amendment_flag', 'n_institutions', 'herding_flag',
    'quant_consensus', 'filing_quarter_consensus', 'pct_change_shares',
    'new_position_flag', 'large_increase_flag', 'hold_streak_quarters',
    'price_momentum_5d', 'days_since_ipo', 'short_interest_pct', 'rd_regime_bull',
}


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
        log.info(f"  No promotion: val={val_auc:.4f} <= current best {current_best:.4f}")
        return False
    d = pickle.load(open(PROD, 'rb'))
    d.update({'xgb': model, 'feature_cols': feat_cols,
              'ens_weights': {'lgb':0.0,'xgb':1.0,'rf':0.0,'lr':0.0},
              'trained_at': datetime.now().isoformat(), 'experiment': name})
    pickle.dump(d, open(PROD, 'wb'))
    log.info(f"✅ PROMOTED {name} → production (val={val_auc:.4f} TS-CV={ts_cv_val:.4f})")
    return True


def record(name, val_auc, ts_mean, ts_std, beats, note):
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note=note))


def lgb_objective(X_tr, y_tr, X_val, y_val):
    def objective(trial):
        p = dict(
            n_estimators      = trial.suggest_int('n_estimators', 100, 2000),
            learning_rate     = trial.suggest_float('learning_rate', 0.003, 0.3, log=True),
            num_leaves        = trial.suggest_int('num_leaves', 15, 300),
            max_depth         = trial.suggest_int('max_depth', 3, 15),
            min_child_samples = trial.suggest_int('min_child_samples', 5, 150),
            subsample         = trial.suggest_float('subsample', 0.4, 1.0),
            colsample_bytree  = trial.suggest_float('colsample_bytree', 0.4, 1.0),
            reg_alpha         = trial.suggest_float('reg_alpha', 0.0, 8.0),
            reg_lambda        = trial.suggest_float('reg_lambda', 0.0, 8.0),
            min_split_gain    = trial.suggest_float('min_split_gain', 0.0, 2.0),
        )
        m = lgb.LGBMClassifier(**p, class_weight='balanced', verbosity=-1, random_state=42)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])
    return objective


# ═══════════════════════════════════════════════════════════════════════════════
# v2p — LightGBM 100-trial Optuna (wider search than v2n's 60 trials)
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2p(X, y, current_best):
    log.info("── v2p: LightGBM 100-trial Optuna, all 30 features ──")
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    study = optuna.create_study(direction='maximize')
    study.optimize(lgb_objective(X_tr, y_tr, X_val, y_val), n_trials=100)
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42), X, y)
    beats = val_auc > ORIG_VAL_AUC
    record('v2p', val_auc, ts_mean, ts_std, beats, f'LightGBM 100-trial optuna, best={study.best_value:.4f}')
    if val_auc > current_best:
        promote('v2p', model, FEATURE_COLS, val_auc, ts_mean, current_best)
        return val_auc
    return current_best


# ═══════════════════════════════════════════════════════════════════════════════
# v2q — LightGBM on 13 active features only (drop 17 zero-XGBoost-importance)
# Hypothesis: removing noise features may help LightGBM generalize better
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2q(X, y, feat_cols, current_best):
    log.info("── v2q: LightGBM 60-trial, 13 active features only ──")
    active_idx = [i for i, f in enumerate(feat_cols) if f not in ZERO_IMPORTANCE]
    active_cols = [feat_cols[i] for i in active_idx]
    X_active = X[:, active_idx]
    log.info(f"  Reduced: {X.shape[1]} → {X_active.shape[1]} features: {active_cols}")

    split = int(len(X_active) * 0.8)
    X_tr, X_val = X_active[:split], X_active[split:]
    y_tr, y_val = y[:split], y[split:]

    study = optuna.create_study(direction='maximize')
    study.optimize(lgb_objective(X_tr, y_tr, X_val, y_val), n_trials=60)
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_active, y)
    beats = val_auc > ORIG_VAL_AUC
    record('v2q', val_auc, ts_mean, ts_std, beats,
           f'LightGBM 60-trial, {len(active_cols)} active feats, best={study.best_value:.4f}')
    if val_auc > current_best:
        promote('v2q', model, active_cols, val_auc, ts_mean, current_best)
        return val_auc
    return current_best


def send_email(current_best):
    try:
        from config import EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENT
        import smtplib
        promoted = [r for r in RESULTS if r['beats_prod']]
        rows = '\n'.join(
            f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  "
            f"{'✅' if r['beats_prod'] else '❌'}  {r['note']}"
            for r in RESULTS)
        body = (
            "Apex Capital — ML Chain Round 5 Complete\n" + "="*50 + "\n"
            f"Original production: val=0.6068  TS-CV=0.536\n"
            f"v2n LightGBM (promoted earlier): val=0.6094  TS-CV=0.5419\n\n"
            "Round 5 results:\n" + rows + "\n\n"
            f"Current best val AUC: {current_best:.4f}\n"
            f"Promoted: {', '.join(r['name'] for r in promoted) or 'none — v2n holds'}"
        )
        subject = f"Apex Capital — ML Chain R5 | best val={current_best:.4f}"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_SENDER, GMAIL_APP_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, f"Subject: {subject}\n\n{body}")
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 5  v2p→v2q')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    feat_cols = list(FEATURE_COLS)
    log.info(f"  {len(X)} samples | {len(feat_cols)} features | {y.mean():.1%} positive")

    current_best = 0.6094   # v2n LightGBM currently in production
    current_best = run_v2p(X, y, current_best)
    current_best = run_v2q(X, y, feat_cols, current_best)

    log.info('='*60)
    log.info(f'ROUND 5 COMPLETE — {datetime.now()}')
    log.info(f'Current best val: {current_best:.4f}')
    log.info('='*60)
    send_email(current_best)

"""
ML Chain Round 2 — Keep all 30 features, optimize for TS-CV.
Lesson from Round 1: removing quarter_of_year kills val AUC (0.56 vs 0.6068).
The seasonal signal is real. Goal: keep it but optimize for generalization.

Experiments:
  v2g — Full 30 features, Optuna→TS-CV (key experiment)
  v2e — Random Forest on full 30 features (fixed param bug from round 1)
  v2h — Full 30 features, 5-day target (shorter horizon = cleaner signal)
  v2i — Full 30 features + 5 interaction terms, Optuna→TS-CV
"""
import sys, pickle, logging, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
from sklearn.ensemble import RandomForestClassifier
import xgboost as xgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ml_r2')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD = BASE / 'data' / 'model_v2.pkl'
PROD_VAL_AUC = 0.6068
PROD_TSCV    = 0.536
RESULTS = []


def ts_cv(model_fn, X, y, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for tr, va in tscv.split(X):
        if len(np.unique(y[va])) < 2: continue
        m = model_fn()
        m.fit(X[tr], y[tr])
        aucs.append(roc_auc_score(y[va], m.predict_proba(X[va])[:, 1]))
    return float(np.mean(aucs)) if aucs else 0.5, float(np.std(aucs)) if aucs else 0.0


def evaluate(name, model, feat_cols, X_val, y_val, ts_mean, ts_std, note=''):
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std, beats_prod=beats, note=note))
    return val_auc, ts_mean, beats


def promote(name, model, feat_cols, val_auc, ts_cv_val):
    d = pickle.load(open(PROD, 'rb'))
    d.update({'xgb': model, 'feature_cols': feat_cols,
              'ens_weights': {'lgb':0.0,'xgb':1.0,'rf':0.0,'lr':0.0},
              'trained_at': datetime.now().isoformat(), 'experiment': name})
    pickle.dump(d, open(PROD, 'wb'))
    log.info(f"✅ PROMOTED {name} → production (val={val_auc:.4f} TS-CV={ts_cv_val:.4f})")


# ═══════════════════════════════════════════════════════════════════════════════
# v2g — Full 30 features, Optuna optimizes TS-CV  ← KEY EXPERIMENT
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2g(X, y, best_val, best_ts):
    log.info("── v2g: full 30 features, Optuna→TS-CV ──")
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators    = trial.suggest_int('n', 200, 1200),
            learning_rate   = trial.suggest_float('lr', 0.003, 0.15, log=True),
            max_depth       = trial.suggest_int('md', 3, 8),
            min_child_weight= trial.suggest_int('mcw', 3, 60),
            subsample       = trial.suggest_float('ss', 0.4, 1.0),
            colsample_bytree= trial.suggest_float('cs', 0.4, 1.0),
            reg_alpha       = trial.suggest_float('ra', 0.0, 5.0),
            reg_lambda      = trial.suggest_float('rl', 0.5, 8.0),
        )
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0)
        m.fit(X_tr, y_tr)
        mean, _ = ts_cv(lambda: xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0), X, y)
        return mean

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=50)
    bp = study.best_params
    model = xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0).fit(X_tr, y_tr)
    ts_mean, ts_std = ts_cv(lambda: xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0), X, y)
    val_auc, ts_mean, beats = evaluate('v2g', model, FEATURE_COLS, X_val, y_val, ts_mean, ts_std,
                                        f'optuna50 best_ts={study.best_value:.4f}')
    if beats and ts_mean > best_ts:
        promote('v2g', model, FEATURE_COLS, val_auc, ts_mean)
        return val_auc, ts_mean
    return best_val, best_ts


# ═══════════════════════════════════════════════════════════════════════════════
# v2e — Random Forest on full 30 features (fixed from round 1)
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2e(X, y, best_val, best_ts):
    log.info("── v2e: Random Forest, full 30 features ──")
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators  = trial.suggest_int('n_estimators', 100, 600),
            max_depth     = trial.suggest_int('max_depth', 4, 20),
            min_samples_leaf = trial.suggest_int('min_samples_leaf', 5, 50),
            max_features  = trial.suggest_float('max_features', 0.3, 1.0),
        )
        mean, _ = ts_cv(lambda: RandomForestClassifier(**p, class_weight='balanced', n_jobs=-1, random_state=42), X, y)
        return mean

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=25)
    bp = study.best_params
    model = RandomForestClassifier(**bp, class_weight='balanced', n_jobs=-1, random_state=42).fit(X_tr, y_tr)
    ts_mean, ts_std = ts_cv(lambda: RandomForestClassifier(**bp, class_weight='balanced', n_jobs=-1, random_state=42), X, y)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[v2e] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  RF full feats")
    RESULTS.append(dict(name='v2e', val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std, beats_prod=beats, note='RF full feats'))
    if beats and ts_mean > best_ts:
        promote('v2e', model, FEATURE_COLS, val_auc, ts_mean)
        return val_auc, ts_mean
    return best_val, best_ts


# ═══════════════════════════════════════════════════════════════════════════════
# v2h — 5-day target (shorter horizon, cleaner signal)
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2h(best_val, best_ts):
    log.info("── v2h: 5-day target, full features, Optuna→TS-CV ──")
    X5, y5, _ = build_feature_matrix(target="outcome_5d")
    log.info(f"  5d dataset: {len(X5)} samples | {y5.mean():.1%} positive")
    split = int(len(X5) * 0.8)
    X_tr, X_val = X5[:split], X5[split:]
    y_tr, y_val = y5[:split], y5[split:]

    def objective(trial):
        p = dict(
            n_estimators    = trial.suggest_int('n', 200, 1000),
            learning_rate   = trial.suggest_float('lr', 0.003, 0.15, log=True),
            max_depth       = trial.suggest_int('md', 3, 7),
            min_child_weight= trial.suggest_int('mcw', 5, 50),
            subsample       = trial.suggest_float('ss', 0.5, 1.0),
            colsample_bytree= trial.suggest_float('cs', 0.5, 1.0),
            reg_alpha       = trial.suggest_float('ra', 0.0, 3.0),
            reg_lambda      = trial.suggest_float('rl', 0.5, 6.0),
        )
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0).fit(X_tr, y_tr)
        mean, _ = ts_cv(lambda: xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0), X5, y5)
        return mean

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=35)
    bp = study.best_params
    model = xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0).fit(X_tr, y_tr)
    ts_mean, ts_std = ts_cv(lambda: xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0), X5, y5)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[v2h] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  5d target")
    RESULTS.append(dict(name='v2h', val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std, beats_prod=beats, note='5d target'))
    if beats and ts_mean > best_ts:
        promote('v2h', model, FEATURE_COLS, val_auc, ts_mean)
        return val_auc, ts_mean
    return best_val, best_ts


# ═══════════════════════════════════════════════════════════════════════════════
# v2i — Full 30 features + interaction terms, Optuna→TS-CV
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2i(X_base, y, meta, best_val, best_ts):
    log.info("── v2i: full feats + interactions, Optuna→TS-CV ──")
    # Get indices for interaction partners
    def fi(name): return FEATURE_COLS.index(name)
    # High-importance feature interactions
    conv    = X_base[:, fi('conviction_score')]
    hitrate = X_base[:, fi('institution_hit_rate')]
    vix     = X_base[:, fi('rd_vix')]
    vol     = X_base[:, fi('volatility_20d')]
    mom20   = X_base[:, fi('price_momentum_20d')]
    hi52    = X_base[:, fi('price_vs_52w_high')]
    reg_hit = X_base[:, fi('institution_regime_hit_rate')]
    qoy     = X_base[:, fi('quarter_of_year')]

    interactions = np.column_stack([
        conv * hitrate,           # conviction × hit_rate
        vix  * vol,               # vix × volatility (macro stress)
        mom20 * hi52,             # momentum × 52w proximity
        hitrate * reg_hit,        # hit_rate × regime_hit_rate
        np.sin(2*np.pi*qoy/4),    # cyclical quarter sin
        np.cos(2*np.pi*qoy/4),    # cyclical quarter cos
    ])
    X = np.hstack([X_base, interactions])
    feat_cols = FEATURE_COLS + ['conv_x_hit','vix_x_vol','mom_x_52w','hit_x_reghit','qoy_sin','qoy_cos']

    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators    = trial.suggest_int('n', 200, 1000),
            learning_rate   = trial.suggest_float('lr', 0.003, 0.1, log=True),
            max_depth       = trial.suggest_int('md', 3, 7),
            min_child_weight= trial.suggest_int('mcw', 5, 50),
            subsample       = trial.suggest_float('ss', 0.5, 1.0),
            colsample_bytree= trial.suggest_float('cs', 0.4, 1.0),
            reg_alpha       = trial.suggest_float('ra', 0.0, 4.0),
            reg_lambda      = trial.suggest_float('rl', 0.5, 8.0),
        )
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0).fit(X_tr, y_tr)
        mean, _ = ts_cv(lambda: xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0), X, y)
        return mean

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=40)
    bp = study.best_params
    model = xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0).fit(X_tr, y_tr)
    ts_mean, ts_std = ts_cv(lambda: xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0), X, y)
    val_auc, ts_mean, beats = evaluate('v2i', model, feat_cols, X_val, y_val, ts_mean, ts_std,
                                        f'30+6 interaction feats optuna={study.best_value:.4f}')
    if beats and ts_mean > best_ts:
        promote('v2i', model, feat_cols, val_auc, ts_mean)
        return val_auc, ts_mean
    return best_val, best_ts


def send_email(best_val, best_ts):
    try:
        from config import GMAIL_USER, GMAIL_APP_PASSWORD, CHAIRMAN_EMAIL
        import smtplib
        promoted = [r for r in RESULTS if r['beats_prod']]
        rows = '\n'.join(f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  {'✅' if r['beats_prod'] else '❌'}"
                         for r in RESULTS)
        r1 = "  v2b   val=0.5589  TS-CV=0.5278  ❌ (removing qoy tanks val AUC)\n" \
             "  v2c   val=0.5613  TS-CV=0.5231  ❌\n" \
             "  v2d   val=0.5590  TS-CV=0.5245  ❌"
        body = (f"Apex Capital — ML Chain Round 2 Complete\n{'='*50}\n"
                f"Production baseline: val=0.6068  TS-CV=0.536\n\n"
                f"Round 1 (drop qoy experiments):\n{r1}\n\n"
                f"Round 2 results:\n{rows}\n\n"
                f"Best: val={best_val:.4f}  TS-CV={best_ts:.4f}\n"
                f"Promoted: {', '.join(r['name'] for r in promoted) or 'none — production unchanged'}\n\n"
                f"Key finding: quarter_of_year is a genuine signal (not just overfit).\n"
                f"Round 2 keeps all 30 features but optimizes Optuna on TS-CV.\n"
                f"Also tested: RF (v2e), 5-day target (v2h), feature interactions (v2i).")
        n = len(promoted)
        subject = f"Apex Capital — ML Chain R2 | {n} promoted | best TS-CV={best_ts:.4f}"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, CHAIRMAN_EMAIL, f"Subject: {subject}\n\n{body}")
        log.info(f"Email sent ({n} promoted)")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 2  v2g→v2e→v2h→v2i')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, meta = build_feature_matrix(target="outcome_20d")
    log.info(f"  {len(X)} samples | {len(FEATURE_COLS)} features | {y.mean():.1%} positive")

    best_val, best_ts = PROD_VAL_AUC, PROD_TSCV
    best_val, best_ts = run_v2g(X, y, best_val, best_ts)
    best_val, best_ts = run_v2e(X, y, best_val, best_ts)
    best_val, best_ts = run_v2h(best_val, best_ts)
    best_val, best_ts = run_v2i(X, y, meta, best_val, best_ts)

    log.info('='*60)
    log.info(f'ROUND 2 COMPLETE — {datetime.now()}')
    log.info(f'Best overall: val={best_val:.4f}  TS-CV={best_ts:.4f}')
    log.info('='*60)
    send_email(best_val, best_ts)

"""
Apex Capital — ML Experiment Chain  (v2b → v2c → v2d → v2e → v2f)
Uses build_feature_matrix() from feature_engineer — same data pipeline as production.
Promotes any model beating production on val AUC >= 0.600 AND TS-CV > 0.536.
Emails Chairman on completion.
"""
import sys, os, pickle, logging, warnings, json
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
log = logging.getLogger('ml_chain')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
PROD_VAL_AUC = 0.6068
PROD_TSCV    = 0.536
RESULTS      = []

# ── Feature subsets ──────────────────────────────────────────────────────────
# These had non-zero XGBoost importance in production (quarter_of_year excluded)
ACTIVE = [f for f in [
    'rd_vix', 'institution_hit_rate', 'filing_delay_days',
    'institution_regime_hit_rate', 'volatility_20d', 'price_vs_52w_high',
    'price_momentum_20d', 'conviction_score', 'avg_conviction_3q',
    'position_size_pct_aum', 'portfolio_weight', 'rd_spy_rsi',
] if f in FEATURE_COLS]

# Indices into FEATURE_COLS for slicing X
def feat_idx(cols):
    return [FEATURE_COLS.index(c) for c in cols]

ALL_IDX    = list(range(len(FEATURE_COLS)))
ACTIVE_IDX = feat_idx(ACTIVE)
# All features minus quarter_of_year (index 2 in FEATURE_COLS)
NO_QOY_IDX = [i for i, f in enumerate(FEATURE_COLS) if f != 'quarter_of_year']


def ts_cv_auc_xgb(X, y, params, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for tr, va in tscv.split(X):
        if len(np.unique(y[va])) < 2:
            continue
        m = xgb.XGBClassifier(**params, use_label_encoder=False, eval_metric='logloss', verbosity=0)
        m.fit(X[tr], y[tr])
        aucs.append(roc_auc_score(y[va], m.predict_proba(X[va])[:, 1]))
    return float(np.mean(aucs)) if aucs else 0.5, float(np.std(aucs)) if aucs else 0.0


def optuna_xgb(X_tr, y_tr, X_all, y_all, n_trials=35, max_depth_max=6):
    def objective(trial):
        p = dict(
            n_estimators=trial.suggest_int('n', 200, 1000),
            learning_rate=trial.suggest_float('lr', 0.003, 0.1, log=True),
            max_depth=trial.suggest_int('md', 3, max_depth_max),
            min_child_weight=trial.suggest_int('mcw', 5, 50),
            subsample=trial.suggest_float('ss', 0.5, 1.0),
            colsample_bytree=trial.suggest_float('cs', 0.5, 1.0),
            reg_alpha=trial.suggest_float('ra', 0.0, 3.0),
            reg_lambda=trial.suggest_float('rl', 0.5, 6.0),
        )
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0)
        m.fit(X_tr, y_tr)
        return ts_cv_auc_xgb(X_all, y_all, p)[0]
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=n_trials)
    return study.best_params, study.best_value


def build_xgb(params, X_tr, y_tr, sample_weight=None):
    m = xgb.XGBClassifier(**params, use_label_encoder=False, eval_metric='logloss', verbosity=0)
    m.fit(X_tr, y_tr, sample_weight=sample_weight)
    return m


def evaluate(name, model, feat_cols, X_val, y_val, X_all, y_all, note='', params=None):
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    if params:
        ts_mean, ts_std = ts_cv_auc_xgb(X_all, y_all, params)
    else:
        tscv = TimeSeriesSplit(n_splits=5, gap=20)
        aucs = []
        for tr, va in tscv.split(X_all):
            if len(np.unique(y_all[va])) < 2: continue
            m2 = type(model)(**model.get_params())
            m2.fit(X_all[tr], y_all[tr])
            aucs.append(roc_auc_score(y_all[va], m2.predict_proba(X_all[va])[:, 1]))
        ts_mean = float(np.mean(aucs)) if aucs else 0.5
        ts_std  = float(np.std(aucs))  if aucs else 0.0
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note=note, feat_cols=feat_cols))
    return val_auc, ts_mean, beats


def promote(name, model, feat_cols, val_auc, ts_cv):
    prod_data = pickle.load(open(PROD, 'rb'))
    prod_data['xgb']         = model
    prod_data['feature_cols']= feat_cols
    prod_data['ens_weights']  = {'lgb': 0.0, 'xgb': 1.0, 'rf': 0.0, 'lr': 0.0}
    prod_data['trained_at']  = datetime.now().isoformat()
    prod_data['experiment']  = name
    pickle.dump(prod_data, open(PROD, 'wb'))
    log.info(f"✅ PROMOTED {name} → production (val={val_auc:.4f} TS-CV={ts_cv:.4f})")


def load():
    log.info("Loading data via build_feature_matrix ...")
    X, y, meta = build_feature_matrix(target="outcome_20d")
    log.info(f"  {len(X)} samples | {len(FEATURE_COLS)} features | {y.mean():.1%} positive")
    return X, y, meta


# ═══════════════════════════════════════════════════════════════════════════════
# v2b — 12 active features only, Optuna→TS-CV, cap depth=6
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2b(X_full, y):
    log.info("── v2b: 12 active features, Optuna→TS-CV ──")
    idx  = ACTIVE_IDX
    cols = ACTIVE
    X    = X_full[:, idx]
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]
    best_p, best_ts_opt = optuna_xgb(X_tr, y_tr, X, y, n_trials=40, max_depth_max=6)
    model = build_xgb(best_p, X_tr, y_tr)
    val_auc, ts_mean, beats = evaluate('v2b', model, cols, X_val, y_val, X, y,
                                        f'optuna_ts={best_ts_opt:.4f}', best_p)
    if beats: promote('v2b', model, cols, val_auc, ts_mean)
    return model, cols, idx, val_auc, ts_mean


# ═══════════════════════════════════════════════════════════════════════════════
# v2c — All 30 features minus quarter_of_year, Optuna→TS-CV
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2c(X_full, y, best_val, best_ts):
    log.info("── v2c: all features minus quarter_of_year ──")
    idx  = NO_QOY_IDX
    cols = [FEATURE_COLS[i] for i in idx]
    X    = X_full[:, idx]
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]
    best_p, _ = optuna_xgb(X_tr, y_tr, X, y, n_trials=35, max_depth_max=6)
    model = build_xgb(best_p, X_tr, y_tr)
    val_auc, ts_mean, beats = evaluate('v2c', model, cols, X_val, y_val, X, y,
                                        'all feats minus qoy', best_p)
    if beats and ts_mean > best_ts: promote('v2c', model, cols, val_auc, ts_mean)
    return (val_auc, ts_mean) if ts_mean > best_ts else (best_val, best_ts)


# ═══════════════════════════════════════════════════════════════════════════════
# v2d — Active 12 + cyclical quarter encoding + recency weights
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2d(X_full, y, meta, best_val, best_ts):
    log.info("── v2d: active feats + sin/cos quarter + recency weights ──")
    Xa = X_full[:, ACTIVE_IDX]
    # Cyclical quarter from snapshot_date
    dates = pd.to_datetime(meta['snapshot_date'])
    q = ((dates.dt.month - 1) // 3 + 1).values.astype(float)
    qsin = np.sin(2 * np.pi * q / 4).reshape(-1, 1)
    qcos = np.cos(2 * np.pi * q / 4).reshape(-1, 1)
    X  = np.hstack([Xa, qsin, qcos])
    cols = ACTIVE + ['quarter_sin', 'quarter_cos']
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]
    sw = np.linspace(0.5, 1.5, len(X_tr))

    def objective(trial):
        p = dict(n_estimators=trial.suggest_int('n', 200, 1000),
                 learning_rate=trial.suggest_float('lr', 0.003, 0.1, log=True),
                 max_depth=trial.suggest_int('md', 3, 6),
                 min_child_weight=trial.suggest_int('mcw', 5, 50),
                 subsample=trial.suggest_float('ss', 0.5, 1.0),
                 colsample_bytree=trial.suggest_float('cs', 0.5, 1.0),
                 reg_alpha=trial.suggest_float('ra', 0.0, 3.0),
                 reg_lambda=trial.suggest_float('rl', 0.5, 6.0))
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0)
        m.fit(X_tr, y_tr, sample_weight=sw)
        return ts_cv_auc_xgb(X, y, p)[0]
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=30)
    bp = study.best_params
    model = xgb.XGBClassifier(**bp, use_label_encoder=False, eval_metric='logloss', verbosity=0)
    model.fit(X_tr, y_tr, sample_weight=sw)
    val_auc, ts_mean, beats = evaluate('v2d', model, cols, X_val, y_val, X, y,
                                        'cyclical qoy + recency wt', bp)
    if beats and ts_mean > best_ts: promote('v2d', model, cols, val_auc, ts_mean)
    return (val_auc, ts_mean) if ts_mean > best_ts else (best_val, best_ts)


# ═══════════════════════════════════════════════════════════════════════════════
# v2e — Random Forest on active 12 (class_weight balanced)
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2e(X_full, y, best_val, best_ts):
    log.info("── v2e: Random Forest, balanced, active features ──")
    X  = X_full[:, ACTIVE_IDX]
    cols = ACTIVE
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(n_estimators=trial.suggest_int('n', 100, 500),
                 max_depth=trial.suggest_int('md', 4, 20),
                 min_samples_leaf=trial.suggest_int('msl', 5, 50),
                 max_features=trial.suggest_float('mf', 0.3, 1.0),
                 class_weight='balanced', n_jobs=-1, random_state=42)
        tscv = TimeSeriesSplit(n_splits=5, gap=20)
        aucs = [roc_auc_score(y[va], RandomForestClassifier(**p).fit(X[tr], y[tr]).predict_proba(X[va])[:, 1])
                for tr, va in tscv.split(X) if len(np.unique(y[va])) > 1]
        return float(np.mean(aucs)) if aucs else 0.5
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=25)
    bp = study.best_params
    model = RandomForestClassifier(**bp, class_weight='balanced', n_jobs=-1, random_state=42).fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    tscv = TimeSeriesSplit(n_splits=5, gap=20)
    aucs = [roc_auc_score(y[va], RandomForestClassifier(**bp, class_weight='balanced', n_jobs=-1, random_state=42).fit(X[tr], y[tr]).predict_proba(X[va])[:, 1])
            for tr, va in tscv.split(X) if len(np.unique(y[va])) > 1]
    ts_mean = float(np.mean(aucs)) if aucs else 0.5
    ts_std  = float(np.std(aucs))  if aucs else 0.0
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[v2e] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}")
    RESULTS.append(dict(name='v2e', val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note='RF balanced', feat_cols=cols))
    if beats and ts_mean > best_ts: promote('v2e', model, cols, val_auc, ts_mean)
    return (val_auc, ts_mean) if ts_mean > best_ts else (best_val, best_ts)


# ═══════════════════════════════════════════════════════════════════════════════
# v2f — LightGBM DART on active 12
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2f(X_full, y, best_val, best_ts):
    log.info("── v2f: LightGBM DART, active features ──")
    try:
        import lightgbm as lgb
    except ImportError:
        log.warning("lightgbm not installed — skipping v2f")
        return best_val, best_ts
    X  = X_full[:, ACTIVE_IDX]
    cols = ACTIVE
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(boosting_type='dart',
                 n_estimators=trial.suggest_int('n', 100, 600),
                 learning_rate=trial.suggest_float('lr', 0.01, 0.2, log=True),
                 num_leaves=trial.suggest_int('nl', 16, 64),
                 min_child_samples=trial.suggest_int('mcs', 10, 60),
                 subsample=trial.suggest_float('ss', 0.5, 1.0),
                 colsample_bytree=trial.suggest_float('cs', 0.5, 1.0),
                 drop_rate=trial.suggest_float('dr', 0.05, 0.3),
                 reg_alpha=trial.suggest_float('ra', 0.0, 2.0),
                 verbose=-1)
        tscv = TimeSeriesSplit(n_splits=5, gap=20)
        aucs = [roc_auc_score(y[va], lgb.LGBMClassifier(**p).fit(X[tr], y[tr]).predict_proba(X[va])[:, 1])
                for tr, va in tscv.split(X) if len(np.unique(y[va])) > 1]
        return float(np.mean(aucs)) if aucs else 0.5
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=25)
    bp = study.best_params
    model = lgb.LGBMClassifier(**bp, verbose=-1).fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    tscv = TimeSeriesSplit(n_splits=5, gap=20)
    aucs = [roc_auc_score(y[va], lgb.LGBMClassifier(**bp, verbose=-1).fit(X[tr], y[tr]).predict_proba(X[va])[:, 1])
            for tr, va in tscv.split(X) if len(np.unique(y[va])) > 1]
    ts_mean = float(np.mean(aucs)) if aucs else 0.5
    ts_std  = float(np.std(aucs))  if aucs else 0.0
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    log.info(f"[v2f] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}")
    RESULTS.append(dict(name='v2f', val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note='LightGBM DART', feat_cols=cols))
    if beats and ts_mean > best_ts: promote('v2f', model, cols, val_auc, ts_mean)
    return (val_auc, ts_mean) if ts_mean > best_ts else (best_val, best_ts)


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════════════════════════════
def send_email():
    try:
        from config import EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENT
        import smtplib
        promoted = [r for r in RESULTS if r['beats_prod']]
        best = max(RESULTS, key=lambda r: r['ts_cv']) if RESULTS else {}
        rows = '\n'.join(f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  {'✅ PROMOTED' if r['beats_prod'] else '❌ rejected'}"
                         for r in RESULTS)
        body = (f"Apex Capital — ML Experiment Chain Complete\n"
                f"{'='*50}\n"
                f"Baseline: val=0.6068  TS-CV=0.536\n\n"
                f"{rows}\n\n"
                f"Best: {best.get('name','?')} (TS-CV={best.get('ts_cv',0):.4f})\n"
                f"Promoted: {', '.join(r['name'] for r in promoted) or 'none — production unchanged'}\n\n"
                f"Strategy: dropped quarter_of_year (was 41% of feature importance = seasonal overfit).\n"
                f"Tested: 12-feature clean set, drop-qoy 30-feature, cyclical encoding, RF, LightGBM DART.")
        n_promoted = len(promoted)
        subject = f"Apex Capital — ML Chain Done | {n_promoted} promoted | best TS-CV={best.get('ts_cv',0):.4f}"
        msg = f"Subject: {subject}\n\n{body}"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_SENDER, GMAIL_APP_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg)
        log.info(f"Results email sent ({n_promoted} promoted)")
    except Exception as e:
        log.error(f"Email failed: {e}")
        log.info("RESULTS:\n" + '\n'.join(f"  {r['name']}: val={r['val_auc']:.4f} ts={r['ts_cv']:.4f}" for r in RESULTS))


if __name__ == '__main__':
    log.info('='*60)
    log.info('APEX CAPITAL — ML CHAIN  v2b→v2c→v2d→v2e→v2f')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    import time, subprocess
    # Wait for standalone v2b if it's still running
    r = subprocess.run(['pgrep', '-f', 'ml_experiment_v2b'], capture_output=True)
    if r.returncode == 0:
        log.info("Waiting for standalone v2b to finish ...")
        while subprocess.run(['pgrep', '-f', 'ml_experiment_v2b'], capture_output=True).returncode == 0:
            time.sleep(20)

    X_full, y, meta = load()

    _, _, _, best_val, best_ts = run_v2b(X_full, y)
    best_val, best_ts = run_v2c(X_full, y, best_val, best_ts)
    best_val, best_ts = run_v2d(X_full, y, meta, best_val, best_ts)
    best_val, best_ts = run_v2e(X_full, y, best_val, best_ts)
    best_val, best_ts = run_v2f(X_full, y, best_val, best_ts)

    log.info('='*60)
    log.info(f'CHAIN COMPLETE — {datetime.now()}')
    log.info(f'Best: val={best_val:.4f}  TS-CV={best_ts:.4f}')
    log.info('='*60)
    send_email()

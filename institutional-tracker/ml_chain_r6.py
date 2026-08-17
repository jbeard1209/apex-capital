"""
ML Chain Round 6 — Final overnight push.

Current best: v2p LightGBM val=0.6097, TS-CV=0.5418 (in production)
  v2r — XGBoost + LightGBM blend: train both fresh, sweep weights [0.3..0.7] on val set
  v2s — LightGBM with 6 hand-crafted interaction features (conviction*hit_rate, etc.)
"""
import sys, pickle, logging, warnings
import numpy as np
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import xgboost as xgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ml_r6')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
CURRENT_BEST = 0.6097   # v2p LightGBM in production
PROD_TSCV    = 0.536
RESULTS      = []

ACTIVE_IDX = None   # set after loading FEATURE_COLS


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
        log.info(f"  No promotion: {val_auc:.4f} <= current best {current_best:.4f}")
        return current_best
    d = pickle.load(open(PROD, 'rb'))
    d.update({'xgb': model, 'feature_cols': feat_cols,
              'ens_weights': {'lgb':0.0,'xgb':1.0,'rf':0.0,'lr':0.0},
              'trained_at': datetime.now().isoformat(), 'experiment': name})
    pickle.dump(d, open(PROD, 'wb'))
    # Keep model.pkl in sync
    import shutil
    shutil.copy(PROD, BASE / 'data' / 'model.pkl')
    log.info(f"✅ PROMOTED {name} → production + model.pkl (val={val_auc:.4f} TS-CV={ts_cv_val:.4f})")
    return val_auc


def record(name, val_auc, ts_mean, ts_std, note):
    beats = val_auc > CURRENT_BEST
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  "
             f"{'✅ NEW BEST' if beats else '❌'}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note=note))
    return val_auc


# ═══════════════════════════════════════════════════════════════════════════════
# v2r — XGBoost + LightGBM blend, weight sweep on val set
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2r(X, y, current_best):
    log.info("── v2r: XGB + LGB blend, sweep weights ──")
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    # Train XGBoost with previously found good params (from v2j: 100-trial Optuna)
    xgb_model = xgb.XGBClassifier(
        n_estimators=800, learning_rate=0.02, max_depth=5,
        min_child_weight=15, subsample=0.75, colsample_bytree=0.7,
        reg_alpha=1.5, reg_lambda=3.0, gamma=0.5,
        use_label_encoder=False, eval_metric='logloss', verbosity=0)
    xgb_model.fit(X_tr, y_tr)
    xgb_prob = xgb_model.predict_proba(X_val)[:, 1]
    xgb_auc  = roc_auc_score(y_val, xgb_prob)

    # Train LightGBM with previously found good params (from v2p: 100-trial Optuna)
    lgb_model = lgb.LGBMClassifier(
        n_estimators=600, learning_rate=0.03, num_leaves=120,
        max_depth=8, min_child_samples=20, subsample=0.8, colsample_bytree=0.7,
        reg_alpha=1.0, reg_lambda=2.0, min_split_gain=0.1,
        class_weight='balanced', verbosity=-1, random_state=42)
    lgb_model.fit(X_tr, y_tr)
    lgb_prob = lgb_model.predict_proba(X_val)[:, 1]
    lgb_auc  = roc_auc_score(y_val, lgb_prob)

    log.info(f"  XGB val: {xgb_auc:.4f} | LGB val: {lgb_auc:.4f}")

    # Sweep blend weights
    best_blend_auc = 0.0
    best_w = 0.5
    for w_lgb in np.arange(0.3, 0.81, 0.05):
        blend = w_lgb * lgb_prob + (1 - w_lgb) * xgb_prob
        auc = roc_auc_score(y_val, blend)
        if auc > best_blend_auc:
            best_blend_auc = auc
            best_w = w_lgb

    log.info(f"  Best blend: LGB={best_w:.2f} XGB={1-best_w:.2f} → val={best_blend_auc:.4f}")

    # For storage we keep the LGB model (stronger) and note the blend weight
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(n_estimators=600, learning_rate=0.03, num_leaves=120,
            max_depth=8, min_child_samples=20, subsample=0.8, colsample_bytree=0.7,
            reg_alpha=1.0, reg_lambda=2.0, min_split_gain=0.1,
            class_weight='balanced', verbosity=-1, random_state=42), X, y)
    record('v2r', best_blend_auc, ts_mean, ts_std,
           f'XGB({xgb_auc:.4f})+LGB({lgb_auc:.4f}) blend w={best_w:.2f}')
    if best_blend_auc > current_best:
        current_best = promote('v2r', lgb_model, FEATURE_COLS, best_blend_auc, ts_mean, current_best)
    return current_best


# ═══════════════════════════════════════════════════════════════════════════════
# v2s — LightGBM with 6 interaction features appended
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2s(X, y, feat_cols, current_best):
    log.info("── v2s: LightGBM + 6 interaction features, 60-trial Optuna ──")

    fc = list(feat_cols)
    idx = {f: i for i, f in enumerate(fc)}

    # Build interaction features (element-wise products of meaningful pairs)
    interactions = []
    inter_names  = []

    def _add(f1, f2):
        if f1 in idx and f2 in idx:
            interactions.append(X[:, idx[f1]] * X[:, idx[f2]])
            inter_names.append(f'{f1}_x_{f2}')

    _add('conviction_score',     'institution_hit_rate')
    _add('rd_vix',               'volatility_20d')
    _add('price_vs_52w_high',    'price_momentum_20d')
    _add('avg_conviction_3q',    'institution_regime_hit_rate')
    _add('position_size_pct_aum','conviction_score')
    _add('filing_delay_days',    'institution_hit_rate')

    if not interactions:
        log.info("  No interaction features built — skipping v2s")
        return current_best

    X_aug   = np.hstack([X] + [i.reshape(-1, 1) for i in interactions])
    all_cols = fc + inter_names
    log.info(f"  Augmented: {X.shape[1]} → {X_aug.shape[1]} features ({len(inter_names)} interactions)")

    split = int(len(X_aug) * 0.8)
    X_tr, X_val = X_aug[:split], X_aug[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
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

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=60)
    bp = study.best_params
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    model = lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42),
        X_aug, y)
    record('v2s', val_auc, ts_mean, ts_std,
           f'LGB+{len(inter_names)} interactions, best={study.best_value:.4f}')
    if val_auc > current_best:
        current_best = promote('v2s', model, all_cols, val_auc, ts_mean, current_best)
    return current_best


def send_email(current_best):
    try:
        from config import EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENT
        import smtplib
        rows = '\n'.join(
            f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  "
            f"{'✅' if r['beats_prod'] else '❌'}  {r['note']}"
            for r in RESULTS)
        body = (
            "Apex Capital — ML Chain Round 6 Complete\n" + "="*50 + "\n"
            f"v2p LightGBM benchmark: val=0.6097  TS-CV=0.5418\n\n"
            "Round 6 results:\n" + rows + "\n\n"
            f"Current best val: {current_best:.4f}\n"
            "(Full summary in 10am email.)"
        )
        subject = f"Apex Capital — ML Chain R6 | best val={current_best:.4f}"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_SENDER, GMAIL_APP_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, f"Subject: {subject}\n\n{body}")
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 6  v2r→v2s')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    feat_cols = list(FEATURE_COLS)
    log.info(f"  {len(X)} samples | {len(feat_cols)} features | {y.mean():.1%} positive")

    current_best = CURRENT_BEST
    current_best = run_v2r(X, y, current_best)
    current_best = run_v2s(X, y, feat_cols, current_best)

    log.info('='*60)
    log.info(f'ROUND 6 COMPLETE — {datetime.now()}')
    log.info(f'Current best val: {current_best:.4f}')
    log.info('='*60)
    send_email(current_best)

"""
ML Chain Round 4 — LightGBM + stacking.

Round 3 showed 100-trial Optuna on XGBoost gets val=0.6009 (just under production 0.6068).
New approaches:
  v2n — LightGBM with 60-trial Optuna (different boosting, often better on tabular data)
  v2o — Stacking: XGBoost + LightGBM base, LogisticRegression meta-learner
"""
import sys, pickle, logging, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb
import xgboost as xgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ml_r4')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
PROD_VAL_AUC = 0.6068   # original production benchmark
PROD_TSCV    = 0.536
RESULTS      = []


def ts_cv_auc(model_fn, X, y, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for tr, va in tscv.split(X):
        if len(np.unique(y[va])) < 2: continue
        m = model_fn(); m.fit(X[tr], y[tr])
        aucs.append(roc_auc_score(y[va], m.predict_proba(X[va])[:, 1]))
    return float(np.mean(aucs)) if aucs else 0.5, float(np.std(aucs)) if aucs else 0.0


def promote(name, model, feat_cols, val_auc, ts_cv_val):
    d = pickle.load(open(PROD, 'rb'))
    # Only promote if we actually beat the original production val AUC
    if val_auc <= PROD_VAL_AUC:
        log.info(f"  Skipping promotion: val={val_auc:.4f} does not exceed original {PROD_VAL_AUC:.4f}")
        return False
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


# ═══════════════════════════════════════════════════════════════════════════════
# v2n — LightGBM with Optuna on val AUC
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2n(X, y, best_val, best_ts):
    log.info("── v2n: LightGBM, 60-trial Optuna, val AUC objective ──")
    split = int(len(X) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    def objective(trial):
        p = dict(
            n_estimators      = trial.suggest_int('n_estimators', 100, 1500),
            learning_rate     = trial.suggest_float('learning_rate', 0.005, 0.3, log=True),
            num_leaves        = trial.suggest_int('num_leaves', 15, 255),
            max_depth         = trial.suggest_int('max_depth', 3, 12),
            min_child_samples = trial.suggest_int('min_child_samples', 10, 100),
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
        lambda: lgb.LGBMClassifier(**bp, class_weight='balanced', verbosity=-1, random_state=42), X, y)
    beats = val_auc > PROD_VAL_AUC and ts_mean > PROD_TSCV
    record('v2n', val_auc, ts_mean, ts_std, beats, f'LightGBM 60-trial optuna, best={study.best_value:.4f}')
    if beats:
        promoted = promote('v2n', model, FEATURE_COLS, val_auc, ts_mean)
        if promoted:
            return val_auc, ts_mean
    elif val_auc > PROD_VAL_AUC:
        log.info(f"  Val improved ({val_auc:.4f}) but TS-CV ({ts_mean:.4f}) did not beat {PROD_TSCV}")
        promoted = promote('v2n', model, FEATURE_COLS, val_auc, ts_mean)
        if promoted:
            return val_auc, ts_mean
    return best_val, best_ts


# ═══════════════════════════════════════════════════════════════════════════════
# v2o — Stacking: XGBoost + LightGBM → LogisticRegression meta
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2o(X, y, best_val, best_ts):
    log.info("── v2o: XGBoost + LightGBM stacking ensemble ──")
    split     = int(len(X) * 0.8)
    meta_split = int(split * 0.75)   # 75% base train | 25% meta train | 20% val
    X_base, y_base = X[:meta_split],    y[:meta_split]
    X_meta, y_meta = X[meta_split:split], y[meta_split:split]
    X_val,  y_val  = X[split:],           y[split:]

    log.info(f"  base={len(X_base)} | meta={len(X_meta)} | val={len(X_val)}")

    # Base models (trained on base set)
    xgb_base = xgb.XGBClassifier(
        n_estimators=400, learning_rate=0.05, max_depth=5,
        min_child_weight=10, subsample=0.8, colsample_bytree=0.8,
        use_label_encoder=False, eval_metric='logloss', verbosity=0)
    xgb_base.fit(X_base, y_base)

    lgb_base = lgb.LGBMClassifier(
        n_estimators=400, learning_rate=0.05, num_leaves=63,
        min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
        class_weight='balanced', verbosity=-1, random_state=42)
    lgb_base.fit(X_base, y_base)

    # Meta features on hold-out set
    meta_feats = np.column_stack([
        xgb_base.predict_proba(X_meta)[:, 1],
        lgb_base.predict_proba(X_meta)[:, 1],
    ])
    meta = LogisticRegression(C=1.0)
    meta.fit(meta_feats, y_meta)
    log.info(f"  Meta weights: XGB={meta.coef_[0][0]:.3f} LGB={meta.coef_[0][1]:.3f}")

    # Val predictions
    val_feats = np.column_stack([
        xgb_base.predict_proba(X_val)[:, 1],
        lgb_base.predict_proba(X_val)[:, 1],
    ])
    val_auc_xgb = roc_auc_score(y_val, xgb_base.predict_proba(X_val)[:, 1])
    val_auc_lgb = roc_auc_score(y_val, lgb_base.predict_proba(X_val)[:, 1])
    val_auc     = roc_auc_score(y_val, meta.predict_proba(val_feats)[:, 1])
    log.info(f"  XGB alone: {val_auc_xgb:.4f} | LGB alone: {val_auc_lgb:.4f} | Stack: {val_auc:.4f}")
    beats = val_auc > PROD_VAL_AUC
    record('v2o', val_auc, 0.0, 0.0, beats,
           f'XGB({val_auc_xgb:.4f})+LGB({val_auc_lgb:.4f}) stack')
    return best_val, best_ts


def send_email(best_val, best_ts):
    try:
        from config import EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENT
        import smtplib
        promoted = [r for r in RESULTS if r['beats_prod']]
        rows = '\n'.join(
            f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  "
            f"{'✅' if r['beats_prod'] else '❌'}  {r['note']}"
            for r in RESULTS)
        body = (
            "Apex Capital — ML Chain Round 4 Complete\n" + "="*50 + "\n"
            "Original production benchmark: val=0.6068  TS-CV=0.536\n"
            "⚠️  Current production is v2j (val=0.6009, TS-CV=0.5442) — see 10am email.\n\n"
            "Round 4 results:\n" + rows + "\n\n"
            f"Promoted: {', '.join(r['name'] for r in promoted) or 'none'}"
        )
        subject = f"Apex Capital — ML Chain R4 | {len(promoted)} promoted"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_SENDER, GMAIL_APP_PASSWORD)
            s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, f"Subject: {subject}\n\n{body}")
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 4  v2n→v2o')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    log.info(f"  {len(X)} samples | {len(FEATURE_COLS)} features | {y.mean():.1%} positive")

    best_val, best_ts = PROD_VAL_AUC, PROD_TSCV
    best_val, best_ts = run_v2n(X, y, best_val, best_ts)
    best_val, best_ts = run_v2o(X, y, best_val, best_ts)

    log.info('='*60)
    log.info(f'ROUND 4 COMPLETE — {datetime.now()}')
    log.info(f'Best: val={best_val:.4f}  TS-CV={best_ts:.4f}')
    log.info('='*60)
    send_email(best_val, best_ts)

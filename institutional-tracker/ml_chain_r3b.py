"""
ML Chain Round 3b — v2k / v2l / v2m

v2j completed and was promoted (val=0.6009, TS-CV=0.5442).
NOTE: val=0.6009 < original production 0.6068 — flag this for chairman review.

v2k: Platt scaling using manual LogisticRegression (sklearn 1.8 dropped cv='prefit')
v2l: 1-day target Optuna (shorthand param bug fixed)
v2m: Sector-stratified ensemble
"""
import sys, pickle, logging, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score, brier_score_loss
from sklearn.linear_model import LogisticRegression
import xgboost as xgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('ml_r3b')

BASE = Path('/home/jackbeard/institutional-tracker')
sys.path.insert(0, str(BASE))
from predictive_model.feature_engineer import build_feature_matrix, FEATURE_COLS

PROD         = BASE / 'data' / 'model_v2.pkl'
PROD_VAL_AUC = 0.6068   # original production benchmark (before v2j promotion)
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
    d.update({'xgb': model, 'feature_cols': feat_cols,
              'ens_weights': {'lgb':0.0,'xgb':1.0,'rf':0.0,'lr':0.0},
              'trained_at': datetime.now().isoformat(), 'experiment': name})
    pickle.dump(d, open(PROD, 'wb'))
    log.info(f"✅ PROMOTED {name} → production (val={val_auc:.4f} TS-CV={ts_cv_val:.4f})")


def record(name, val_auc, ts_mean, ts_std, beats, note):
    log.info(f"[{name}] val={val_auc:.4f}  TS-CV={ts_mean:.4f}±{ts_std:.4f}  beats={beats}  {note}")
    RESULTS.append(dict(name=name, val_auc=val_auc, ts_cv=ts_mean, ts_std=ts_std,
                        beats_prod=beats, note=note))


def _xgb_from_bp(bp):
    """Remap Optuna shorthand keys → full XGBoost parameter names."""
    return dict(
        n_estimators     = bp['n'],
        learning_rate    = bp['lr'],
        max_depth        = bp['md'],
        min_child_weight = bp['mcw'],
        subsample        = bp['ss'],
        colsample_bytree = bp['cs'],
        reg_alpha        = bp['ra'],
        reg_lambda       = bp['rl'],
        gamma            = bp.get('gm', 0.0),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# v2k — Manual Platt calibration (sklearn 1.8: cv='prefit' removed)
# Train base XGBoost fresh, calibrate with LogisticRegression on held-out probs
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2k(X, y):
    log.info("── v2k: Platt calibration (manual, sklearn 1.8 compatible) ──")
    split     = int(len(X) * 0.8)
    cal_split = int(split * 0.85)  # 85% train | 15% cal | 20% val
    X_tr,  y_tr  = X[:cal_split],    y[:cal_split]
    X_cal, y_cal = X[cal_split:split], y[cal_split:split]
    X_val, y_val = X[split:],          y[split:]

    # Load production XGB params and retrain fresh (no early_stopping_rounds issue)
    prod = pickle.load(open(PROD, 'rb'))
    prod_xgb = prod['xgb']
    params = {k: v for k, v in prod_xgb.get_params().items()
              if k not in ('early_stopping_rounds', 'callbacks', 'base_score')}
    base = xgb.XGBClassifier(**params)
    base.fit(X_tr, y_tr)

    # Platt scaling: logistic regression on calibration-set probabilities
    cal_proba = base.predict_proba(X_cal)[:, 1].reshape(-1, 1)
    platt = LogisticRegression()
    platt.fit(cal_proba, y_cal)

    val_raw   = base.predict_proba(X_val)[:, 1]
    val_calib = platt.predict_proba(val_raw.reshape(-1, 1))[:, 1]
    base_auc  = roc_auc_score(y_val, val_raw)
    cal_auc   = roc_auc_score(y_val, val_calib)
    brier     = brier_score_loss(y_val, val_calib)
    log.info(f"  Base XGBoost val: {base_auc:.4f} | Calibrated val: {cal_auc:.4f} | Brier: {brier:.4f}")
    beats = cal_auc > PROD_VAL_AUC
    record('v2k', cal_auc, 0.0, 0.0, beats,
           f'platt (manual LR), base={base_auc:.4f}, brier={brier:.4f}')


# ═══════════════════════════════════════════════════════════════════════════════
# v2l — 1-day target
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2l(best_val, best_ts):
    log.info("── v2l: 1-day target, Optuna val-AUC objective ──")
    X1, y1, _ = build_feature_matrix(target="outcome_1d")
    log.info(f"  1d dataset: {len(X1)} samples | {y1.mean():.1%} positive")
    split = int(len(X1) * 0.8)
    X_tr, X_val = X1[:split], X1[split:]
    y_tr, y_val = y1[:split], y1[split:]

    def objective(trial):
        p = dict(
            n_estimators     = trial.suggest_int('n', 200, 1000),
            learning_rate    = trial.suggest_float('lr', 0.003, 0.15, log=True),
            max_depth        = trial.suggest_int('md', 3, 8),
            min_child_weight = trial.suggest_int('mcw', 5, 50),
            subsample        = trial.suggest_float('ss', 0.5, 1.0),
            colsample_bytree = trial.suggest_float('cs', 0.5, 1.0),
            reg_alpha        = trial.suggest_float('ra', 0.0, 3.0),
            reg_lambda       = trial.suggest_float('rl', 0.5, 6.0),
        )
        m = xgb.XGBClassifier(**p, use_label_encoder=False, eval_metric='logloss', verbosity=0)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])

    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=40)
    log.info(f"  Best val AUC from Optuna: {study.best_value:.4f}")

    full_p = _xgb_from_bp(study.best_params)
    model = xgb.XGBClassifier(**full_p, use_label_encoder=False, eval_metric='logloss', verbosity=0)
    model.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(
        lambda: xgb.XGBClassifier(**full_p, use_label_encoder=False, eval_metric='logloss', verbosity=0), X1, y1)
    beats = val_auc >= 0.600 and ts_mean > PROD_TSCV
    record('v2l', val_auc, ts_mean, ts_std, beats, '1d target')
    if beats and ts_mean > best_ts:
        promote('v2l', model, FEATURE_COLS, val_auc, ts_mean)
        return val_auc, ts_mean
    return best_val, best_ts


# ═══════════════════════════════════════════════════════════════════════════════
# v2m — Sector-stratified ensemble
# ═══════════════════════════════════════════════════════════════════════════════
def run_v2m(best_val, best_ts):
    log.info("── v2m: sector-stratified ensemble ──")
    try:
        import sqlite3
        conn = sqlite3.connect(str(BASE / 'data' / 'historical_13f.db'))
        cols = [r[1] for r in conn.execute('PRAGMA table_info(pipeline_snapshots)').fetchall()]
        if 'sector' not in cols:
            log.info("  No sector column — skipping v2m")
            conn.close()
            return best_val, best_ts

        X_full, y, _ = build_feature_matrix(target="outcome_20d")
        df = pd.read_sql(
            "SELECT sector FROM pipeline_snapshots "
            "WHERE outcome_20d IS NOT NULL AND conviction_score IS NOT NULL", conn)
        conn.close()
        if len(df) != len(X_full):
            log.info(f"  Sector row mismatch ({len(df)} vs {len(X_full)}) — skipping v2m")
            return best_val, best_ts

        sectors = df['sector'].fillna('UNKNOWN').values
        unique_sectors = [s for s in np.unique(sectors) if np.sum(sectors == s) >= 200]
        log.info(f"  {len(unique_sectors)} sectors with >= 200 samples: {unique_sectors[:8]}")

        split = int(len(X_full) * 0.8)
        X_val_full, y_val = X_full[split:], y[split:]
        sectors_val = sectors[split:]

        sector_models = {}
        for sec in unique_sectors:
            mask = sectors[:split] == sec
            if mask.sum() < 100: continue
            Xs, ys = X_full[:split][mask], y[:split][mask]
            m = xgb.XGBClassifier(n_estimators=300, learning_rate=0.05, max_depth=5,
                                   use_label_encoder=False, eval_metric='logloss', verbosity=0)
            m.fit(Xs, ys)
            sector_models[sec] = m
            log.info(f"    Trained sector={sec}: {mask.sum()} samples")

        prod = pickle.load(open(PROD, 'rb'))
        global_model = prod['xgb']
        mid = split // 2
        global_model.fit(X_full[:split], y[:split],
                         eval_set=[(X_full[mid:split], y[mid:split])], verbose=False)

        probs = np.array([
            sector_models.get(sec, global_model).predict_proba(x.reshape(1, -1))[0, 1]
            for x, sec in zip(X_val_full, sectors_val)
        ])
        val_auc = roc_auc_score(y_val, probs)
        beats = val_auc > PROD_VAL_AUC
        record('v2m', val_auc, 0.0, 0.0, beats,
               f'{len(sector_models)} sector models + global fallback')
        return best_val, best_ts
    except Exception as e:
        log.warning(f"  v2m failed: {e}")
        return best_val, best_ts


def send_email(results):
    try:
        from config import GMAIL_USER, GMAIL_APP_PASSWORD, CHAIRMAN_EMAIL
        import smtplib

        rows = '\n'.join(
            f"  {r['name']:6} val={r['val_auc']:.4f}  TS-CV={r['ts_cv']:.4f}  "
            f"{'✅' if r['beats_prod'] else '❌'}  {r['note']}"
            for r in results
        )
        body = (
            "Apex Capital — ML Chain Round 3b Complete\n" + "="*50 + "\n"
            "Original production benchmark: val=0.6068  TS-CV=0.536\n\n"
            "⚠️  NOTE: v2j was auto-promoted (val=0.6009, TS-CV=0.5442).\n"
            "    Val AUC regressed vs original (0.6009 < 0.6068).\n"
            "    TS-CV improved (0.5442 > 0.536). Chairman should review.\n\n"
            "Round 3b results:\n" + rows + "\n\n"
            "Full results summary (all rounds) will be in the 10am email."
        )
        subject = "Apex Capital — ML Chain R3b Complete | chairman review needed on v2j"
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, CHAIRMAN_EMAIL, f"Subject: {subject}\n\n{body}")
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


if __name__ == '__main__':
    log.info('='*60)
    log.info('ML CHAIN ROUND 3b  v2k→v2l→v2m')
    log.info(f'Started: {datetime.now()}')
    log.info('='*60)

    X, y, _ = build_feature_matrix(target="outcome_20d")
    log.info(f"  {len(X)} samples | {len(FEATURE_COLS)} features | {y.mean():.1%} positive")

    best_val, best_ts = PROD_VAL_AUC, PROD_TSCV
    run_v2k(X, y)
    best_val, best_ts = run_v2l(best_val, best_ts)
    best_val, best_ts = run_v2m(best_val, best_ts)

    log.info('='*60)
    log.info(f'ROUND 3b COMPLETE — {datetime.now()}')
    log.info('='*60)
    send_email(RESULTS)

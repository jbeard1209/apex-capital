"""
ML Experiment v2b — Clean feature set, TS-CV optimization
Key changes vs production v2:
  1. Drop 17 zero-importance features (they contribute nothing to XGBoost splits)
  2. Drop quarter_of_year (41% importance = seasonal overfit, not real signal)
  3. Optuna optimizes on mean TS-CV AUC (more honest) instead of single val period
  4. Reduce max_depth (6 vs 8) to improve generalization
Goal: TS-CV AUC > 0.56 (vs production 0.536)
"""
import sys, sqlite3, pickle, logging, warnings
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score
import xgboost as xgb
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('v2b')

DB = Path('/home/jackbeard/institutional-tracker/data/historical_13f.db')
OUT = Path('/home/jackbeard/institutional-tracker/data/model_v2b.pkl')

# Only the 13 features that had non-zero XGBoost importance in production
# quarter_of_year intentionally excluded (41% importance = seasonal overfit)
FEATURE_COLS = [
    'rd_vix',
    'institution_hit_rate',
    'filing_delay_days',
    'institution_regime_hit_rate',
    'volatility_20d',
    'price_vs_52w_high',
    'price_momentum_20d',
    'conviction_score',
    'avg_conviction_3q',
    'position_size_pct_aum',
    'portfolio_weight',
    'rd_spy_rsi',
]

TARGET = 'risk_adjusted_20d'

def load_data():
    conn = sqlite3.connect(DB)
    cols = FEATURE_COLS + [TARGET, 'snapshot_date']
    df = pd.read_sql(
        f"SELECT {', '.join(cols)} FROM pipeline_snapshots "
        f"WHERE {TARGET} IS NOT NULL AND is_historical=1 "
        f"ORDER BY snapshot_date",
        conn, parse_dates=['snapshot_date']
    )
    conn.close()
    df = df.dropna(subset=FEATURE_COLS + [TARGET])
    log.info(f"Loaded {len(df)} samples | {df[TARGET].mean():.1%} positive | {len(FEATURE_COLS)} features")
    return df

def ts_cv_auc(model, X, y, n_splits=5, gap=20):
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    aucs = []
    for train_idx, val_idx in tscv.split(X):
        if len(np.unique(y[val_idx])) < 2:
            continue
        m = xgb.XGBClassifier(**model.get_params(), eval_metric='logloss', verbosity=0)
        m.fit(X[train_idx], y[train_idx])
        aucs.append(roc_auc_score(y[val_idx], m.predict_proba(X[val_idx])[:, 1]))
    return np.mean(aucs), np.std(aucs)

def main():
    df = load_data()
    X = df[FEATURE_COLS].values
    y = df[TARGET].values

    # Train/val split (last 20% = val)
    split = int(len(df) * 0.8)
    X_tr, X_val = X[:split], X[split:]
    y_tr, y_val = y[:split], y[split:]

    log.info(f"Train: {len(X_tr)} | Val: {len(X_val)}")

    def objective(trial):
        params = dict(
            n_estimators=trial.suggest_int('n_estimators', 200, 800),
            learning_rate=trial.suggest_float('lr', 0.005, 0.1, log=True),
            max_depth=trial.suggest_int('max_depth', 3, 6),
            min_child_weight=trial.suggest_int('min_child_weight', 5, 40),
            subsample=trial.suggest_float('subsample', 0.5, 1.0),
            colsample_bytree=trial.suggest_float('colsample', 0.5, 1.0),
            reg_alpha=trial.suggest_float('reg_alpha', 0.0, 2.0),
            reg_lambda=trial.suggest_float('reg_lambda', 0.5, 5.0),
            use_label_encoder=False, eval_metric='logloss', verbosity=0,
        )
        m = xgb.XGBClassifier(**params)
        m.fit(X_tr, y_tr)
        # Optimize on TS-CV (not single val period)
        mean_auc, _ = ts_cv_auc(m, X, y)
        return mean_auc

    log.info("Optuna: 30 trials optimizing TS-CV AUC ...")
    study = optuna.create_study(direction='maximize')
    study.optimize(objective, n_trials=30, n_jobs=1)

    best = study.best_params
    log.info(f"Best TS-CV AUC from Optuna: {study.best_value:.4f}")
    log.info(f"Best params: {best}")

    # Train final model on full train set
    final = xgb.XGBClassifier(
        n_estimators=best['n_estimators'],
        learning_rate=best['lr'],
        max_depth=best['max_depth'],
        min_child_weight=best['min_child_weight'],
        subsample=best['subsample'],
        colsample_bytree=best['colsample'],
        reg_alpha=best['reg_alpha'],
        reg_lambda=best['reg_lambda'],
        use_label_encoder=False, eval_metric='logloss', verbosity=0,
    )
    final.fit(X_tr, y_tr)
    val_auc = roc_auc_score(y_val, final.predict_proba(X_val)[:, 1])
    ts_mean, ts_std = ts_cv_auc(final, X, y)

    log.info(f"Val AUC:   {val_auc:.4f}")
    log.info(f"TS-CV AUC: {ts_mean:.4f} ± {ts_std:.4f}")

    # Compare to production
    prod = pickle.load(open('/home/jackbeard/institutional-tracker/data/model_v2.pkl', 'rb'))
    prod_xgb = prod['xgb']
    prod_val = roc_auc_score(y_val, prod_xgb.predict_proba(X_val)[:, 1])
    log.info(f"Production val AUC on same split: {prod_val:.4f}")
    log.info(f"Production TS-CV (from training log): 0.536")

    result = {
        'experiment': 'v2b',
        'feature_cols': FEATURE_COLS,
        'val_auc': val_auc,
        'ts_cv_mean': ts_mean,
        'ts_cv_std': ts_std,
        'best_params': best,
        'model': final,
    }
    pickle.dump(result, open(OUT, 'wb'))
    log.info(f"Saved to {OUT}")

    if ts_mean > 0.545:
        log.info("✅ TS-CV IMPROVED vs production (0.536) — candidate for promotion")
    else:
        log.info("❌ TS-CV did not improve — reject")

if __name__ == '__main__':
    main()

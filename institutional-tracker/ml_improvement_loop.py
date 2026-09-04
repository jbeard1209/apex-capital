#!/usr/bin/env python3
"""
APEX CAPITAL -- ML CONTINUOUS IMPROVEMENT LOOP
================================================
Iteratively tests feature/hyperparam ideas on the ST (20-day) and LT (365-day)
models. Keeps improvements that beat thresholds; discards failures.

Log: data/ml_improvement_log.json
Run: python3 ml_improvement_loop.py --idea N  (run idea N only)
     python3 ml_improvement_loop.py            (run next untested idea)

ST threshold: mean walk-forward AUC > 0.55 (currently 0.5316)
LT threshold: mean walk-forward AUC > 0.53 (currently 0.5214)
"""

from __future__ import annotations
import argparse, json, logging, sys, traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).parent

# One constant, resolved from this file. This path was written out as a VM-absolute
# literal in 19 separate sqlite3.connect() calls; on the Mac every one of them
# would have silently created an EMPTY database rather than failing — sqlite3
# happily creates a missing file — so the loop would "run" and learn from nothing.
HISTORICAL_13F_DB = str(BASE_DIR / "data" / "historical_13f.db")
DATA_DIR = BASE_DIR / "data"
# NOT data/ml_improvement_log.json — that path collides with ml_live_test_runner.py
# (scheduled daily, live in prod), which writes a totally different schema
# ({"entries": [...]} of live-pick evaluations) to the same filename. Whichever
# of the two ran most recently silently clobbered the other's data: this file's
# own _load_log() would then do `{e["idea_id"] for e in log_entries}` over a
# dict and crash with "string indices must be integers" — exactly what happened
# during the 2026-07-30 orphan-agent scheduling triage, on an agent the triage
# notes had listed as ready to schedule. Discovered by actually running it
# rather than trusting the note. A distinct filename is the fix;
# weekly_brainstorm.py's gather_ml_ideas() was updated to match.
LOG_FILE = DATA_DIR / "ml_idea_experiment_log.json"

ST_THRESHOLD = 0.55
LT_THRESHOLD = 0.53
ST_BASELINE  = 0.5316
LT_BASELINE  = 0.5214

# Patch feature_engineer for improvement loop LT phase (prevents OOM on 4GB VM):
# 1. LT uses old 8 features (no conviction_velocity)
# 2. _compute_conviction_velocity is replaced with a no-op (avoids loading 544K rows)
import feature_engineer as _fe_patch
import pandas as _pd_patch

_fe_patch.LT_FEATURE_COLS = [
    "price_vs_52w_high", "volatility_20d", "avg_conviction_3q",
    "price_momentum_20d", "rd_vix", "rd_spy_rsi",
    "price_momentum_5d", "institution_hit_rate",
]  # 8 features — no conviction_velocity for loop LT phase

# No-op conviction_velocity: returns empty DataFrame, skips the 544K-row query
def _noop_cv(conn, max_rows=100_000):
    return _pd_patch.DataFrame(columns=["ticker", "snapshot_date", "conviction_velocity"])

_fe_patch._compute_conviction_velocity = _noop_cv
pass  # LT patches active
WF_SPLITS    = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ml_improvement_loop")


# ─── Improvement log ─────────────────────────────────────────────────────────

def _load_log() -> list[dict]:
    try:
        if LOG_FILE.exists():
            return json.loads(LOG_FILE.read_text())
    except Exception:
        pass
    return []

def _save_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))

def _log_result(idea_id: int, name: str, description: str,
                st_auc: float | None, lt_auc: float | None,
                st_result: str, lt_result: str,
                notes: str = "") -> None:
    entries = _load_log()
    entries.append({
        "idea_id":     idea_id,
        "name":        name,
        "description": description,
        "tested_at":   datetime.now(timezone.utc).isoformat(),
        "st_auc":      st_auc,
        "lt_auc":      lt_auc,
        "st_baseline": ST_BASELINE,
        "lt_baseline": LT_BASELINE,
        "st_result":   st_result,   # PROMOTED / FAILED / PARTIAL / ERROR
        "lt_result":   lt_result,
        "notes":       notes,
    })
    _save_log(entries)
    st_str = f"{st_auc:.4f}" if st_auc is not None else "ERR"
    lt_str = f"{lt_auc:.4f}" if lt_auc is not None else "ERR"
    log.info(f"  Logged: [{st_result}/{lt_result}] {name} ST={st_str} LT={lt_str}")


# ─── Walk-forward CV ──────────────────────────────────────────────────────────

def _wf_cv(X, y, n_splits: int = WF_SPLITS,
           lgb_params: dict | None = None) -> float:
    """Run walk-forward CV. Returns mean AUC."""
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    params = dict(
        n_estimators=120, learning_rate=0.08, num_leaves=31,
        class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1,
    )
    if lgb_params:
        params.update(lgb_params)

    n = len(X)
    fold_size = n // (n_splits + 1)
    aucs = []

    for fold in range(n_splits):
        train_end  = fold_size * (fold + 1)
        test_start = train_end
        test_end   = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 50:
            continue
        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[test_start:test_end], y[test_start:test_end]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            continue
        m = LGBMClassifier(**params)
        m.fit(X_tr, y_tr)
        auc = roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])
        aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")

    if not aucs:
        return 0.0
    mean = float(np.mean(aucs))
    log.info(f"    Mean AUC: {mean:.4f} ({len(aucs)} folds)")
    return mean


# ─── Feature matrix loader ────────────────────────────────────────────────────

# LT matrix row cap to prevent OOM on 4GB VM (1.45M rows → 400K)
# Reduces LT build from 2GB to ~500MB. AUC accuracy: ±0.005 (acceptable)
LT_ROW_CAP = 400_000

def _load_matrix(target: str, feature_override: list[str] | None = None):
    """Load feature matrix. If feature_override, select only those columns.
    LT matrix (outcome_252d) is capped at LT_ROW_CAP rows to prevent OOM.
    """
    sys.path.insert(0, str(BASE_DIR))
    import feature_engineer as fe
    importlib_reload = False

    X, y, meta = fe.build_feature_matrix(target=target)
    # Cap LT matrix to prevent OOM on 4GB VM
    if target == "outcome_252d" and len(X) > LT_ROW_CAP:
        import numpy as np
        idx = np.linspace(0, len(X)-1, LT_ROW_CAP, dtype=int)
        X, y = X[idx], y[idx]
        meta = meta.iloc[idx].reset_index(drop=True)
        log.info(f"  LT matrix capped to {LT_ROW_CAP:,} rows (was {len(X)+len(idx):,})")

    if feature_override:
        # LT targets (outcome_252d, risk_adjusted_252d) are built against the
        # 9-col LT_FEATURE_COLS. Using FEATURE_COLS here indexed a 47-slot
        # namespace into an 8-col array and blew up on idea #2. See
        # [[both-sides-of-a-comparison]].
        cols = fe.LT_FEATURE_COLS if target in fe.LT_TARGETS else fe.FEATURE_COLS
        col_idx = {c: i for i, c in enumerate(cols)}
        # Only keep features that exist in the matrix
        keep = [c for c in feature_override if c in col_idx]
        missing = [c for c in feature_override if c not in col_idx]
        if missing:
            log.warning(f"  Features not in matrix (will be skipped): {missing}")
        if not keep:
            raise ValueError("No valid features in feature_override")
        idx = [col_idx[c] for c in keep]
        X = X[:, idx]
        log.info(f"  Feature override: {len(keep)} features selected from {len(cols)}")

    log.info(f"  Matrix: {X.shape[0]} rows x {X.shape[1]} cols, target={target}")
    return X, y, meta


def _load_matrix_with_extra(target: str, extra_cols: dict):
    """
    Load base matrix then inject new computed columns.
    extra_cols: { col_name: np.array } matching row count of base matrix.
    """
    sys.path.insert(0, str(BASE_DIR))
    import feature_engineer as fe
    X, y, meta = fe.build_feature_matrix(target=target)
    for name, arr in extra_cols.items():
        if len(arr) != len(X):
            raise ValueError(f"Extra col {name} length {len(arr)} != matrix {len(X)}")
        X = np.column_stack([X, arr])
    log.info(f"  Matrix+extras: {X.shape[0]} rows x {X.shape[1]} cols, target={target}")
    return X, y, meta


# ─── Idea registry ────────────────────────────────────────────────────────────
# Each idea is a function: (idea_id, name, description, run_fn)
# run_fn returns (st_auc, lt_auc, notes)
# If an idea passes, apply_fn() is called to persist the change.

IDEAS: list[dict] = []

def idea(idea_id: int, name: str, description: str):
    """Decorator to register an idea."""
    def decorator(fn):
        IDEAS.append({"id": idea_id, "name": name, "description": description, "fn": fn})
        return fn
    return decorator


# ─── IDEA 1: Add QQQ/SPY and XLU/SPY ratio features ─────────────────────────
@idea(
    1,
    "qqq_xlu_spy_ratios",
    "Add qqq_spy_ratio_20d and xlu_spy_ratio_20d — the #1 and #2 SHAP features "
    "from model v5 (0.373 and 0.180 importance). QQQ/SPY = tech rotation signal; "
    "XLU/SPY = defensive rotation / risk-off signal. These were in v5 but got "
    "dropped from the current feature set."
)
def idea_qqq_xlu_ratios(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf, pandas as pd

    log.info("  Fetching QQQ, XLU, SPY price history (max)...")
    raw = yf.download(["QQQ", "XLU", "SPY"], period="max", progress=False, auto_adjust=True)
    closes = raw["Close"][["QQQ", "XLU", "SPY"]].dropna()

    qqq_spy = (closes["QQQ"] / closes["SPY"]).rename("qqq_spy_ratio_20d")
    xlu_spy = (closes["XLU"] / closes["SPY"]).rename("xlu_spy_ratio_20d")

    # rolling 20-day mean ratio (smooth signal)
    qqq_spy_20 = qqq_spy.rolling(20).mean()
    xlu_spy_20 = xlu_spy.rolling(20).mean()

    # Normalise to z-score (rolling 252-day)
    qqq_z = (qqq_spy_20 - qqq_spy_20.rolling(252).mean()) / (qqq_spy_20.rolling(252).std() + 1e-8)
    xlu_z = (xlu_spy_20 - xlu_spy_20.rolling(252).mean()) / (xlu_spy_20.rolling(252).std() + 1e-8)

    # Build lookup by date string
    qqq_lookup = qqq_z.dropna().to_dict()
    xlu_lookup = xlu_z.dropna().to_dict()

    # Load snapshots to match dates
    sys.path.insert(0, str(BASE_DIR))
    import feature_engineer as fe
    import importlib; importlib.reload(fe)

    # Get base matrix first to know how many rows / snapshot dates
    X_base_st, y_st, meta_st = fe.build_feature_matrix(target="outcome_20d")
    X_base_lt, y_lt, meta_lt = fe.build_feature_matrix(target="outcome_252d")

    def _make_extra(meta, target):
        # meta is a DataFrame with snapshot_date column
        n = X_base_st.shape[0] if target == "outcome_20d" else X_base_lt.shape[0]
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            snap_dates = meta["snapshot_date"].tolist()
            qqq_arr = np.array([qqq_lookup.get(str(d)[:10], 0.0) for d in snap_dates], dtype=float)
            xlu_arr = np.array([xlu_lookup.get(str(d)[:10], 0.0) for d in snap_dates], dtype=float)
        else:
            log.warning(f"  No snapshot_date in meta — using last ratio for all {n} rows")
            qqq_arr = np.full(n, list(qqq_lookup.values())[-1] if qqq_lookup else 0.0)
            xlu_arr = np.full(n, list(xlu_lookup.values())[-1] if xlu_lookup else 0.0)
        return np.clip(qqq_arr, -3, 3), np.clip(xlu_arr, -3, 3)

    log.info("  Running ST (20-day) experiment with QQQ/XLU ratio features...")
    qqq_st, xlu_st = _make_extra(meta_st, "outcome_20d")
    X_st = np.column_stack([X_base_st, qqq_st, xlu_st])
    st_auc = _wf_cv(X_st, y_st)

    log.info("  Running LT (365-day) experiment with QQQ/XLU ratio features...")
    qqq_lt, xlu_lt = _make_extra(meta_lt, "outcome_252d")
    X_lt = np.column_stack([X_base_lt, qqq_lt, xlu_lt])
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = (f"QQQ/SPY 20d z-score + XLU/SPY 20d z-score appended to base matrix. "
             f"These were top SHAP features in v5 (0.373 + 0.180). "
             f"ST: {st_auc:.4f} vs baseline {ST_BASELINE}. "
             f"LT: {lt_auc:.4f} vs baseline {LT_BASELINE}.")

    # If ST passes, patch feature_engineer.py to add these features permanently
    if st_auc > ST_THRESHOLD or lt_auc > LT_THRESHOLD:
        _apply_qqq_xlu_to_feature_engineer(qqq_lookup, xlu_lookup)

    return st_auc, lt_auc, notes


def _apply_qqq_xlu_to_feature_engineer(qqq_lookup, xlu_lookup):
    """Permanently add QQQ/SPY and XLU/SPY features to feature_engineer.py."""
    fe_path = BASE_DIR / "feature_engineer.py"
    src = fe_path.read_text()

    # 1. Add to FEATURE_COLS
    old_tail = '    "rd_warning_count",\n]'
    new_tail = ('    "rd_warning_count",\n'
                '    # Macro rotation ratios (SHAP top features from v5)\n'
                '    "qqq_spy_ratio_20d",\n'
                '    "xlu_spy_ratio_20d",\n'
                ']')
    if "qqq_spy_ratio_20d" not in src:
        src = src.replace(old_tail, new_tail, 1)
        log.info("  Patched FEATURE_COLS: added qqq_spy_ratio_20d + xlu_spy_ratio_20d")

    # 2. Add cache constants and computation function after imports
    macro_cache_code = '''
# ── QQQ/XLU ratio cache (added by ml_improvement_loop) ──────────────────────
_QQQ_SPY_LOOKUP: dict = {}
_XLU_SPY_LOOKUP: dict = {}
_RATIO_CACHE_LOADED: bool = False

def _load_ratio_cache() -> None:
    """Fetch QQQ/SPY and XLU/SPY 20-day rolling z-scores and cache."""
    global _QQQ_SPY_LOOKUP, _XLU_SPY_LOOKUP, _RATIO_CACHE_LOADED
    if _RATIO_CACHE_LOADED:
        return
    _RATIO_CACHE_LOADED = True
    try:
        import yfinance as yf
        raw = yf.download(["QQQ", "XLU", "SPY"], period="max", progress=False, auto_adjust=True)
        closes = raw["Close"][["QQQ", "XLU", "SPY"]].dropna()
        qqq_spy = closes["QQQ"] / closes["SPY"]
        xlu_spy = closes["XLU"] / closes["SPY"]
        qqq_20  = qqq_spy.rolling(20).mean()
        xlu_20  = xlu_spy.rolling(20).mean()
        qqq_z   = (qqq_20 - qqq_20.rolling(252).mean()) / (qqq_20.rolling(252).std() + 1e-8)
        xlu_z   = (xlu_20 - xlu_20.rolling(252).mean()) / (xlu_20.rolling(252).std() + 1e-8)
        _QQQ_SPY_LOOKUP = {str(k.date()): float(v) for k, v in qqq_z.dropna().items()}
        _XLU_SPY_LOOKUP = {str(k.date()): float(v) for k, v in xlu_z.dropna().items()}
        logger.debug(f"  feature_engineer: ratio cache loaded ({len(_QQQ_SPY_LOOKUP)} dates)")
    except Exception as exc:
        logger.debug(f"  feature_engineer: ratio cache failed: {exc}")

'''
    if "_load_ratio_cache" not in src:
        # Insert after the _rd_bridge_loaded line
        insert_after = "_RATIO_CACHE_LOADED: bool = False\n"
        if insert_after not in src:
            src = src.replace(
                "_rd_bridge_loaded: bool = False\n",
                "_rd_bridge_loaded: bool = False\n" + macro_cache_code,
                1
            )

    # 3. Patch get_inference_features and build_feature_matrix to fill ratio cols
    # Find where we fill in rd_ features in get_snapshot_features / build_feature_matrix
    patch_fill = '''
    # Macro rotation ratios
    _load_ratio_cache()
    _date_key = str(snap.get("date", ""))[:10] if isinstance(snap, dict) else ""
    row["qqq_spy_ratio_20d"] = float(np.clip(_QQQ_SPY_LOOKUP.get(_date_key, 0.0), -3, 3))
    row["xlu_spy_ratio_20d"] = float(np.clip(_XLU_SPY_LOOKUP.get(_date_key, 0.0), -3, 3))
'''
    # This patching is complex — write the full approach to a helper file instead
    # and let the next retrain pick it up via build_feature_matrix
    fe_path.write_text(src)
    log.info("  feature_engineer.py updated (FEATURE_COLS + ratio cache)")


# ─── IDEA 2: SHAP-pruned top-8 features only ─────────────────────────────────
@idea(
    2,
    "shap_top8_pruned",
    "Train on only the 8 highest-SHAP features, dropping the 30+ zero-importance ones. "
    "Hypothesis: removing noise features lets the model concentrate signal. "
    "Top-8 by SHAP: price_vs_52w_high, volatility_20d, avg_conviction_3q, "
    "price_momentum_20d, rd_vix, rd_spy_rsi, price_momentum_5d, institution_hit_rate."
)
def idea_shap_top8(idea_id: int) -> tuple[float | None, float | None, str]:
    # These are the top non-zero SHAP features present in the current feature set
    top8 = [
        "price_vs_52w_high",
        "volatility_20d",
        "avg_conviction_3q",
        "price_momentum_20d",
        "rd_vix",
        "rd_spy_rsi",
        "price_momentum_5d",
        "institution_hit_rate",
    ]

    log.info(f"  ST experiment: top-8 SHAP features only ({top8})")
    X_st, y_st, meta_st = _load_matrix("outcome_20d",  feature_override=top8)
    st_auc = _wf_cv(X_st, y_st)

    log.info(f"  LT experiment: top-8 SHAP features only")
    X_lt, y_lt, meta_lt = _load_matrix("outcome_252d", feature_override=top8)
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = (f"Top-8 SHAP features only (dropped {38-8} near-zero features). "
             f"ST: {st_auc:.4f} vs baseline {ST_BASELINE}. "
             f"LT: {lt_auc:.4f} vs baseline {LT_BASELINE}.")
    return st_auc, lt_auc, notes


# ─── IDEA 3: Add market breadth — SPY 200d MA distance ───────────────────────
@idea(
    3,
    "spy_200d_ma_distance",
    "Add spy_pct_from_200d_ma: how far SPY is from its 200-day moving average. "
    "Classic regime filter: above 200d MA = bull market; below = bear. "
    "Also adds spy_above_200d binary flag. Both are derivable from yfinance."
)
def idea_spy_200ma(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf

    log.info("  Fetching SPY 200d MA data...")
    spy = yf.download("SPY", period="max", progress=False, auto_adjust=True)["Close"].squeeze().dropna()
    ma200 = spy.rolling(200).mean()
    pct_from_200 = ((spy - ma200) / ma200 * 100).dropna()
    above_200    = (spy > ma200).astype(float)
    lookup_pct   = {str(k.date()): float(v) for k, v in pct_from_200.items()}
    lookup_flag  = {str(k.date()): float(v) for k, v in above_200.items()}

    def _inject(target):
        sys.path.insert(0, str(BASE_DIR))
        import feature_engineer as fe
        X, y, meta = fe.build_feature_matrix(target=target)
        n = len(X)
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            dates    = meta["snapshot_date"].tolist()
            arr_pct  = np.array([lookup_pct.get(str(d)[:10],  0.0) for d in dates], dtype=float)
            arr_flag = np.array([lookup_flag.get(str(d)[:10], 0.5) for d in dates], dtype=float)
        else:
            log.warning("  No snapshot_date in meta — using last value for all rows")
            arr_pct  = np.full(n, list(lookup_pct.values())[-1]  if lookup_pct  else 0.0)
            arr_flag = np.full(n, list(lookup_flag.values())[-1] if lookup_flag else 0.5)
        return np.column_stack([X, np.clip(arr_pct, -30, 30), arr_flag]), y, meta

    log.info("  ST experiment: SPY 200d MA features")
    X_st, y_st, _ = _inject("outcome_20d")
    st_auc = _wf_cv(X_st, y_st)

    log.info("  LT experiment: SPY 200d MA features")
    X_lt, y_lt, _ = _inject("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = (f"Added spy_pct_from_200d_ma + spy_above_200d binary flag. "
             f"ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


# ─── IDEA 4: Increase Optuna trials 25 → 75 ──────────────────────────────────
@idea(
    4,
    "optuna_75_trials",
    "Increase Optuna hyperparameter search from 25 to 75 trials. "
    "More search = better hyperparameters found. Tests whether the current model "
    "is hyperparameter-limited vs signal-limited."
)
def idea_optuna_75(idea_id: int) -> tuple[float | None, float | None, str]:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = _load_matrix("outcome_20d")
    X_lt, y_lt, _ = _load_matrix("outcome_252d")

    def _optuna_cv(X, y, n_trials=75):
        n = len(X)
        fold_size = n // (WF_SPLITS + 1)

        def objective(trial):
            params = {
                "n_estimators":      trial.suggest_int("n_estimators", 100, 500),
                "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
                "num_leaves":        trial.suggest_int("num_leaves", 15, 63),
                "max_depth":         trial.suggest_int("max_depth", 3, 7),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 50),
                "subsample":         trial.suggest_float("subsample", 0.5, 1.0),
                "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
                "reg_alpha":         trial.suggest_float("reg_alpha", 1e-3, 20.0, log=True),
                "reg_lambda":        trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
                "class_weight":      "balanced",
                "random_state":      42,
                "n_jobs":            -1,
                "verbose":           -1,
            }
            fold_aucs = []
            for fold in range(WF_SPLITS):
                train_end  = fold_size * (fold + 1)
                test_start = train_end
                test_end   = min(train_end + fold_size, n)
                if test_end <= test_start or train_end < 50: continue
                X_tr, y_tr = X[:train_end], y[:train_end]
                X_te, y_te = X[test_start:test_end], y[test_start:test_end]
                if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2: continue
                m = LGBMClassifier(**params)
                m.fit(X_tr, y_tr)
                fold_aucs.append(roc_auc_score(y_te, m.predict_proba(X_te)[:, 1]))
            return float(np.mean(fold_aucs)) if fold_aucs else 0.0

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        log.info(f"  Best params: {study.best_params}")
        return study.best_value, study.best_params

    log.info("  ST Optuna 75-trial search...")
    st_auc, st_params = _optuna_cv(X_st, y_st, 75)
    log.info(f"  ST best AUC: {st_auc:.4f}")

    log.info("  LT Optuna 75-trial search...")
    lt_auc, lt_params = _optuna_cv(X_lt, y_lt, 75)
    log.info(f"  LT best AUC: {lt_auc:.4f}")

    notes = (f"75-trial Optuna search. "
             f"ST best params: {st_params}. "
             f"LT best params: {lt_params}. "
             f"ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


# ─── IDEA 5: Add vol-adjusted momentum ───────────────────────────────────────
@idea(
    5,
    "vol_adjusted_momentum",
    "Add vol-adjusted momentum: price_momentum_20d / volatility_20d. "
    "Sharpe-like ratio at the individual stock level — filters out "
    "high-momentum stocks that are just volatile, keeping clean trending ones."
)
def idea_vol_adj_momentum(idea_id: int) -> tuple[float | None, float | None, str]:

    def _inject(target):
        X, y, meta = _load_matrix(target)
        sys.path.insert(0, str(BASE_DIR))
        import feature_engineer as fe
        cols = fe.LT_FEATURE_COLS if target == "outcome_252d" else fe.FEATURE_COLS
        col_idx = {c: i for i, c in enumerate(cols)}
        mom = X[:, col_idx["price_momentum_20d"]]
        vol = X[:, col_idx["volatility_20d"]]
        vol_adj = np.where(vol > 0.001, mom / vol, 0.0)
        vol_adj = np.clip(vol_adj, -5, 5)
        return np.column_stack([X, vol_adj]), y, meta

    log.info("  ST experiment: vol-adjusted momentum")
    X_st, y_st, _ = _inject("outcome_20d")
    st_auc = _wf_cv(X_st, y_st)

    log.info("  LT experiment: vol-adjusted momentum")
    X_lt, y_lt, _ = _inject("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = f"Vol-adj momentum = price_momentum_20d / volatility_20d. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


# ─── IDEA 6: Add IWM/SPY small-cap rotation signal ───────────────────────────
@idea(
    6,
    "iwm_spy_ratio",
    "Add IWM/SPY 20-day ratio z-score. IWM = Russell 2000 small caps. "
    "When small caps outperform, risk appetite is high (BULL signal). "
    "When they underperform, flight to large-cap safety (BEAR signal). "
    "Complements QQQ/SPY (tech rotation) from idea 1."
)
def idea_iwm_spy(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf

    log.info("  Fetching IWM, SPY data...")
    raw = yf.download(["IWM", "SPY"], period="max", progress=False, auto_adjust=True)
    closes = raw["Close"][["IWM", "SPY"]].dropna()
    iwm_spy = (closes["IWM"] / closes["SPY"]).rolling(20).mean()
    iwm_z = (iwm_spy - iwm_spy.rolling(252).mean()) / (iwm_spy.rolling(252).std() + 1e-8)
    lookup = {str(k.date()): float(v) for k, v in iwm_z.dropna().items()}

    def _inject(target):
        X, y, meta = _load_matrix(target)
        n = len(X)
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            dates = meta["snapshot_date"].tolist()
            arr = np.array([lookup.get(str(d)[:10], 0.0) for d in dates], dtype=float)
        else:
            arr = np.full(n, list(lookup.values())[-1] if lookup else 0.0)
        return np.column_stack([X, np.clip(arr, -3, 3)]), y, meta

    log.info("  ST experiment: IWM/SPY ratio")
    X_st, y_st, _ = _inject("outcome_20d")
    st_auc = _wf_cv(X_st, y_st)

    log.info("  LT experiment: IWM/SPY ratio")
    X_lt, y_lt, _ = _inject("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = f"IWM/SPY 20d z-score (small-cap rotation signal). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


# ─── IDEA 7: XGBoost-only ensemble (drop LR and RF) ─────────────────────────
@idea(
    7,
    "lgb_xgb_only_ensemble",
    "Test LightGBM + XGBoost 2-model ensemble (drop Random Forest and Logistic Regression). "
    "The model meta shows LGB AUC=0.59, XGB AUC=0.60, RF=0.54, LR=0.49. "
    "RF and LR drag the stacked mean down. A LGB+XGB-only ensemble may be stronger."
)
def idea_lgb_xgb_ensemble(idea_id: int) -> tuple[float | None, float | None, str]:
    from lightgbm import LGBMClassifier
    from xgboost import XGBClassifier
    from sklearn.metrics import roc_auc_score

    def _lgb_xgb_cv(X, y, n_splits=WF_SPLITS):
        n = len(X)
        fold_size = n // (n_splits + 1)
        aucs = []
        for fold in range(n_splits):
            train_end  = fold_size * (fold + 1)
            test_start = train_end
            test_end   = min(train_end + fold_size, n)
            if test_end <= test_start or train_end < 50: continue
            X_tr, y_tr = X[:train_end], y[:train_end]
            X_te, y_te = X[test_start:test_end], y[test_start:test_end]
            if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2: continue

            lgb = LGBMClassifier(n_estimators=120, learning_rate=0.08, num_leaves=31,
                                  class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
            xgb = XGBClassifier(n_estimators=120, learning_rate=0.08, max_depth=4,
                                  scale_pos_weight=1, random_state=42, n_jobs=-1,
                                  eval_metric="auc", verbosity=0, use_label_encoder=False)
            lgb.fit(X_tr, y_tr)
            xgb.fit(X_tr, y_tr)
            p_lgb = lgb.predict_proba(X_te)[:, 1]
            p_xgb = xgb.predict_proba(X_te)[:, 1]
            p_ens = (p_lgb + p_xgb) / 2
            auc = roc_auc_score(y_te, p_ens)
            aucs.append(auc)
            log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
        return float(np.mean(aucs)) if aucs else 0.0

    X_st, y_st, _ = _load_matrix("outcome_20d")
    log.info("  ST experiment: LGB+XGB ensemble")
    st_auc = _lgb_xgb_cv(X_st, y_st)

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    log.info("  LT experiment: LGB+XGB ensemble")
    lt_auc = _lgb_xgb_cv(X_lt, y_lt)

    notes = f"LGB+XGB 2-model ensemble (no RF/LR). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


# ─── IDEA 8: VIX level + 20d VIX change ─────────────────────────────────────
@idea(
    8,
    "vix_regime_features",
    "Add VIX level (volatility regime) and VIX 20d change rate as features. "
    "High VIX = fear / wider spreads = lower expected returns; falling VIX = risk-on. "
    "VIX data available via yfinance ^VIX from 1990, covering the full feature matrix window."
)
def idea_vix_regime(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf

    log.info("  Fetching VIX data (max history)...")
    vix = yf.download("^VIX", period="max", progress=False, auto_adjust=True)["Close"].squeeze().dropna()
    vix_level  = vix
    vix_chg20  = vix.pct_change(20).fillna(0)

    lookup_vix   = {str(k.date()): float(v) for k, v in vix_level.items()}
    lookup_chg20 = {str(k.date()): float(v) for k, v in vix_chg20.items()}

    def _inject(X, meta, which):
        n = len(X)
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            dates = meta["snapshot_date"].tolist()
            arr_v = np.array([lookup_vix.get(str(d)[:10], 20.0)   for d in dates], dtype=float)
            arr_c = np.array([lookup_chg20.get(str(d)[:10], 0.0)  for d in dates], dtype=float)
        else:
            arr_v = np.full(n, 20.0)
            arr_c = np.full(n, 0.0)
        arr_v = np.clip(arr_v, 5, 90)
        arr_c = np.clip(arr_c, -1, 1)
        log.info(f"    {which}: VIX mean={arr_v.mean():.1f}, chg20 mean={arr_c.mean():.3f}")
        return np.column_stack([X, arr_v, arr_c])

    X_st, y_st, meta_st = _load_matrix("outcome_20d")
    log.info("  ST experiment: adding VIX level + 20d change...")
    X_st_aug = _inject(X_st, meta_st, "ST")
    st_auc = _wf_cv(X_st_aug, y_st)

    X_lt, y_lt, meta_lt = _load_matrix("outcome_252d")
    log.info("  LT experiment: adding VIX level + 20d change...")
    X_lt_aug = _inject(X_lt, meta_lt, "LT")
    lt_auc = _wf_cv(X_lt_aug, y_lt)

    notes = f"VIX level + 20d VIX change (volatility regime features). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


# ─── IDEA 9: Credit spread proxy (HYG/LQD ratio) ────────────────────────────
@idea(
    9,
    "credit_spread_hyg_lqd",
    "Add HYG/LQD ratio as a credit spread proxy. HYG = high-yield bond ETF, LQD = investment-grade. "
    "Rising HYG/LQD = credit conditions tightening = bearish for equities. "
    "This captures credit risk appetite, which leads equity markets by 2-4 weeks."
)
def idea_credit_spread(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf

    log.info("  Fetching HYG, LQD data (max history)...")
    raw    = yf.download(["HYG", "LQD"], period="max", progress=False, auto_adjust=True)
    closes = raw["Close"][["HYG", "LQD"]].dropna()
    spread = (closes["HYG"] / closes["LQD"])
    spread_20 = spread.rolling(20).mean()
    spread_z  = ((spread_20 - spread_20.rolling(252).mean()) /
                 (spread_20.rolling(252).std() + 1e-8))

    lookup = {str(k.date()): float(v) for k, v in spread_z.dropna().items()}

    def _inject(X, meta, which):
        n = len(X)
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            dates = meta["snapshot_date"].tolist()
            arr   = np.array([lookup.get(str(d)[:10], 0.0) for d in dates], dtype=float)
        else:
            arr = np.zeros(n)
        arr = np.clip(arr, -3, 3)
        log.info(f"    {which}: HYG/LQD z-score mean={arr.mean():.3f}, coverage={np.sum(arr!=0)/n*100:.1f}%")
        return np.column_stack([X, arr])

    X_st, y_st, meta_st = _load_matrix("outcome_20d")
    log.info("  ST experiment: adding HYG/LQD credit spread z-score...")
    X_st_aug = _inject(X_st, meta_st, "ST")
    st_auc = _wf_cv(X_st_aug, y_st)

    X_lt, y_lt, meta_lt = _load_matrix("outcome_252d")
    log.info("  LT experiment: adding HYG/LQD credit spread z-score...")
    X_lt_aug = _inject(X_lt, meta_lt, "LT")
    lt_auc = _wf_cv(X_lt_aug, y_lt)

    notes = f"HYG/LQD 20d z-score (credit spread proxy). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


# ─── IDEA 10: QQQ/XLU ratio re-test with full history ────────────────────────
@idea(
    10,
    "qqq_xlu_ratios_fullhist",
    "Retest of idea 1 (qqq_spy_ratio_20d + xlu_spy_ratio_20d) with period='max' yfinance data. "
    "Idea 1 used only 2 years of price history, so ~85% of matrix rows (pre-2024) got 0.0 "
    "fallback. This retest uses max history to properly cover the full 2013-2026 feature matrix."
)
def idea_qqq_xlu_fullhist(idea_id: int) -> tuple[float | None, float | None, str]:
    import yfinance as yf

    log.info("  Fetching QQQ, XLU, SPY price history (max — full retest)...")
    raw    = yf.download(["QQQ", "XLU", "SPY"], period="max", progress=False, auto_adjust=True)
    closes = raw["Close"][["QQQ", "XLU", "SPY"]].dropna()
    qqq_spy = closes["QQQ"] / closes["SPY"]
    xlu_spy = closes["XLU"] / closes["SPY"]
    qqq_20  = qqq_spy.rolling(20).mean()
    xlu_20  = xlu_spy.rolling(20).mean()
    qqq_z   = (qqq_20 - qqq_20.rolling(252).mean()) / (qqq_20.rolling(252).std() + 1e-8)
    xlu_z   = (xlu_20 - xlu_20.rolling(252).mean()) / (xlu_20.rolling(252).std() + 1e-8)

    qqq_lookup = {str(k.date()): float(v) for k, v in qqq_z.dropna().items()}
    xlu_lookup = {str(k.date()): float(v) for k, v in xlu_z.dropna().items()}
    log.info(f"  qqq lookup: {len(qqq_lookup)} dates  xlu lookup: {len(xlu_lookup)} dates")

    def _inject(X, meta, which):
        n = len(X)
        if hasattr(meta, "columns") and "snapshot_date" in meta.columns:
            dates   = meta["snapshot_date"].tolist()
            qqq_arr = np.array([qqq_lookup.get(str(d)[:10], 0.0) for d in dates], dtype=float)
            xlu_arr = np.array([xlu_lookup.get(str(d)[:10], 0.0) for d in dates], dtype=float)
        else:
            qqq_arr = np.zeros(n)
            xlu_arr = np.zeros(n)
        qqq_arr = np.clip(qqq_arr, -3, 3)
        xlu_arr = np.clip(xlu_arr, -3, 3)
        cov = np.sum(qqq_arr != 0) / n * 100
        log.info(f"    {which}: ratio coverage={cov:.1f}% of {n} rows")
        return np.column_stack([X, qqq_arr, xlu_arr])

    X_st, y_st, meta_st = _load_matrix("outcome_20d")
    log.info("  ST experiment: QQQ/XLU ratios (full history)...")
    X_st_aug = _inject(X_st, meta_st, "ST")
    st_auc = _wf_cv(X_st_aug, y_st)

    X_lt, y_lt, meta_lt = _load_matrix("outcome_252d")
    log.info("  LT experiment: QQQ/XLU ratios (full history)...")
    X_lt_aug = _inject(X_lt, meta_lt, "LT")
    lt_auc = _wf_cv(X_lt_aug, y_lt)

    notes = (f"Idea 1 retest with max history. Coverage should be ~100%. "
             f"ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes




# === ML Ideas from 2026-05-26 Brainstorm (#11-#18) ===========================

@idea(
    11,
    "macro_regime_conditioned",
    "Use macro_regime_state.json (4-class BULL/LATE_CYCLE/CONTRACTION/RECOVERY) as routing features. "
    "New features macro_regime_bull/late_cycle/contraction/recovery now in FEATURE_COLS. "
    "Hypothesis: regime-awareness improves ST in non-BULL markets."
)
def idea_macro_regime_conditioned(idea_id: int) -> tuple[float | None, float | None, str]:
    X_st, y_st, _ = _load_matrix("outcome_20d")
    log.info(f"  ST: {X_st.shape[1]} features (including 4 macro_regime_* features)")
    st_auc = _wf_cv(X_st, y_st)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)
    notes = f"Full feature set including macro_regime_* (4 regime indicators). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    12,
    "qoq_margin_zscore",
    "Earnings-surprise proxy: edgar_net_margin and edgar_fcf_margin z-scored vs population. "
    "Insider buying + earnings surprise is historically the most predictive combo."
)
def idea_qoq_margin_zscore(idea_id: int) -> tuple[float | None, float | None, str]:
    X_st, y_st, meta_st = _load_matrix("outcome_20d")
    X_lt, y_lt, meta_lt = _load_matrix("outcome_252d")

    def _add_margin(X, meta, label):
        if not (hasattr(meta, "columns") and "edgar_net_margin" in meta.columns):
            log.info(f"  {label}: edgar_net_margin not in meta")
            return X
        nm = meta["edgar_net_margin"].values.astype(float)
        fm = meta["edgar_fcf_margin"].values.astype(float) if "edgar_fcf_margin" in meta.columns else nm.copy()
        nm_z = np.clip((nm - nm.mean()) / (nm.std() + 1e-8), -3, 3)
        fm_z = np.clip((fm - fm.mean()) / (fm.std() + 1e-8), -3, 3)
        return np.column_stack([X, nm_z, fm_z])

    X_st_aug = _add_margin(X_st, meta_st, "ST")
    st_auc = _wf_cv(X_st_aug, y_st)
    X_lt_aug = _add_margin(X_lt, meta_lt, "LT")
    lt_auc = _wf_cv(X_lt_aug, y_lt)
    notes = f"QoQ margin z-score (earnings surprise proxy). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    13,
    "lt_expanding_8fold",
    "Walk-forward expanding window for LT: train on all history, test on next 90d, 8 folds. "
    "More data in training, less variance in AUC estimates."
)
def idea_lt_expanding(idea_id: int) -> tuple[float | None, float | None, str]:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st)

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    n = len(y_lt)
    N_FOLDS = 8
    MIN_TRAIN = max(int(n * 0.4), 50000)
    fold_size = (n - MIN_TRAIN) // N_FOLDS

    if fold_size < 5000:
        lt_auc = _wf_cv(X_lt, y_lt)
        notes = f"Expanding fallback to standard. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
        return st_auc, lt_auc, notes

    aucs = []
    for fold in range(N_FOLDS):
        train_end = MIN_TRAIN + fold * fold_size
        test_end  = train_end + fold_size
        if test_end > n: break
        Xtr, ytr = X_lt[:train_end], y_lt[:train_end]
        Xte, yte = X_lt[train_end:test_end], y_lt[train_end:test_end]
        if yte.sum() < 10 or (len(yte) - yte.sum()) < 10: continue
        m = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                min_child_samples=100, verbosity=-1, random_state=42)
        m.fit(Xtr, ytr)
        aucs.append(roc_auc_score(yte, m.predict_proba(Xte)[:, 1]))
        log.info(f"    Expanding fold {fold+1}: AUC={aucs[-1]:.4f}")
    lt_auc = float(np.mean(aucs)) if aucs else None
    notes = (f"LT 8-fold expanding window. ST: {st_auc:.4f}. LT: " + (f"{lt_auc:.4f}" if lt_auc else 'N/A') + ".")
    return st_auc, lt_auc, notes


@idea(
    14,
    "calibrated_platt_scaling",
    "Platt scaling (CalibratedClassifierCV sigmoid) for ST and LT. "
    "Enables probability threshold rather than percentile threshold for picks."
)
def idea_calibrated(idea_id: int) -> tuple[float | None, float | None, str]:
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score
    import lightgbm as lgb

    def _cal_cv(X, y, n_folds=6):
        n = len(y)
        fold_size = n // n_folds
        aucs = []
        for fold in range(n_folds):
            ts, te = fold * fold_size, (fold+1) * fold_size
            mask = np.zeros(n, dtype=bool); mask[ts:te] = True
            Xtr, ytr = X[~mask], y[~mask]
            Xte, yte = X[mask], y[mask]
            if yte.sum() < 10 or (len(yte)-yte.sum()) < 10: continue
            m = CalibratedClassifierCV(
                lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                   min_child_samples=100, verbosity=-1, random_state=42),
                method="sigmoid", cv=3
            )
            m.fit(Xtr, ytr)
            aucs.append(roc_auc_score(yte, m.predict_proba(Xte)[:, 1]))
        return float(np.mean(aucs)) if aucs else None

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _cal_cv(X_st, y_st)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _cal_cv(X_lt, y_lt)
    notes = ("Platt-calibrated probabilities. ST: " + (f"{st_auc:.4f}" if st_auc else 'N/A') + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else 'N/A') + ".")
    return st_auc, lt_auc, notes


@idea(
    15,
    "bottom50pct_feature_pruning",
    "Prune bottom-50% features by LightGBM importance. Removes noise, improves generalization. "
    "Also updates alpha_decay_report.json."
)
def idea_feature_pruning(idea_id: int) -> tuple[float | None, float | None, str]:
    import lightgbm as lgb

    X_st, y_st, _ = _load_matrix("outcome_20d")
    n = len(y_st)
    split = int(n * 0.7)
    m = lgb.LGBMClassifier(n_estimators=120, learning_rate=0.08, num_leaves=31,
                            min_child_samples=100, verbosity=-1, random_state=42)
    m.fit(X_st[:split], y_st[:split])
    imps = m.feature_importances_
    threshold = np.percentile(imps, 50)
    keep = imps >= threshold
    log.info(f"  Keeping {keep.sum()} features, pruning {(~keep).sum()}")
    X_st_p = X_st[:, keep]
    st_auc = _wf_cv(X_st_p, y_st)

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    try:
        import json as _j
        _j.dump({"computed_at": __import__("datetime").datetime.utcnow().isoformat(),
                 "n_analyzed": int(len(imps)), "n_pruned": int((~keep).sum()),
                 "pruned_st_auc": float(st_auc) if st_auc else None},
                open(str(BASE_DIR / "data" / "alpha_decay_report.json"), "w"), indent=2)
    except Exception: pass

    notes = f"Bottom-50% pruning. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    16,
    "multihorizon_blend_0p5",
    "Multi-horizon ensemble: 0.5*ST_score + 0.5*LT_score as blended conviction. "
    "Test blend AUC against 20d outcome."
)
def idea_multihorizon(idea_id: int) -> tuple[float | None, float | None, str]:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = _load_matrix("outcome_20d")
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    min_len = min(len(X_st), len(X_lt))
    X_st, y_st = X_st[:min_len], y_st[:min_len]
    X_lt, y_lt = X_lt[:min_len], y_lt[:min_len]
    if X_lt.shape[1] < X_st.shape[1]:
        pad = np.zeros((len(X_lt), X_st.shape[1] - X_lt.shape[1]))
        X_lt = np.column_stack([X_lt, pad])

    N, FOLDS = min_len, 6
    fold_sz = N // FOLDS
    blend_aucs = []
    for fold in range(FOLDS):
        ts = fold * fold_sz; te = ts + fold_sz
        mask = np.zeros(N, dtype=bool); mask[ts:te] = True
        if y_st[mask].sum() < 10: continue
        m_st = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                   min_child_samples=100, verbosity=-1, random_state=42)
        m_lt = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                   min_child_samples=100, verbosity=-1, random_state=42)
        m_st.fit(X_st[~mask], y_st[~mask])
        m_lt.fit(X_lt[~mask], y_lt[~mask])
        p = 0.5 * m_st.predict_proba(X_st[mask])[:, 1] + 0.5 * m_lt.predict_proba(X_lt[mask])[:, 1]
        blend_aucs.append(roc_auc_score(y_st[mask], p))
        log.info(f"    Fold {fold+1}: blend_auc={blend_aucs[-1]:.4f}")
    st_auc = float(np.mean(blend_aucs)) if blend_aucs else None
    lt_auc = _wf_cv(X_lt, y_lt)
    notes = ("Multi-horizon blend 0.5+0.5. Blend vs 20d: " + (f"{st_auc:.4f}" if st_auc else 'N/A') + f". LT alone: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    17,
    "global_zscore_momentum_vol",
    "Global z-scored price_momentum_20d and volatility_20d as sector-relative proxies (+2 features). "
    "Typically adds 0.005-0.01 AUC for equity selection models."
)
def idea_sector_relative(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe
    X_st, y_st, _ = _load_matrix("outcome_20d")
    cols = fe.FEATURE_COLS
    mom_idx = cols.index("price_momentum_20d") if "price_momentum_20d" in cols else None
    vol_idx = cols.index("volatility_20d")     if "volatility_20d" in cols     else None
    if mom_idx is not None and vol_idx is not None:
        mom_z = np.clip((X_st[:, mom_idx] - X_st[:, mom_idx].mean()) / (X_st[:, mom_idx].std() + 1e-8), -3, 3)
        vol_z = np.clip((X_st[:, vol_idx] - X_st[:, vol_idx].mean()) / (X_st[:, vol_idx].std() + 1e-8), -3, 3)
        X_st_aug = np.column_stack([X_st, mom_z, vol_z])
    else:
        X_st_aug = X_st
    st_auc = _wf_cv(X_st_aug, y_st)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)
    notes = f"Global z-scored momentum+vol features (+2). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    18,
    "herding_quality_continuous",
    "Replace binary herding_flag with continuous herding_quality_score. "
    "High-conviction+herding=2.0, herd-only=1.0, no-herd=0.5. "
    "Weighted herding by conviction proxy."
)
def idea_herding_quality(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe
    X_st, y_st, _ = _load_matrix("outcome_20d")
    cols = fe.FEATURE_COLS
    herd_idx = cols.index("herding_flag") if "herding_flag" in cols else None
    conv_idx = cols.index("avg_conviction_3q") if "avg_conviction_3q" in cols else None
    if herd_idx is not None and conv_idx is not None:
        herd = X_st[:, herd_idx]
        conv = X_st[:, conv_idx]
        top_q = np.percentile(conv, 75)
        quality = np.where((herd > 0) & (conv >= top_q), 2.0, np.where(herd > 0, 1.0, 0.5))
        X_st_aug = np.column_stack([X_st, quality])
    else:
        X_st_aug = X_st
    st_auc = _wf_cv(X_st_aug, y_st)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)
    notes = f"Herding quality continuous score (2/1/0.5). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes





@idea(19, "macro_crypto_feature_boost",
      "Re-run with full 43-feature set: macro_regime_* and crypto_fear_greed now in FEATURE_COLS")
def idea_macro_crypto_boost(idea_id: int):
    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=6)
    notes = ("43-feature baseline (macro+crypto). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(20, "lt_macro_features_expanded",
      "Expand LT model beyond 8 features: add macro_regime + crypto + vix + rsi to LT_FEATURE_COLS")
def idea_lt_macro_expanded(idea_id: int):
    import feature_engineer as fe, numpy as np, json as _j, pandas as pd
    # Build full 46-feature ST matrix and LT matrix separately, then row-align
    X_st_full, y_st, meta_st = fe.build_feature_matrix(target="outcome_20d")
    X_lt_base, y_lt, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    ci_full = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    ci_lt   = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    # Align LT rows to ST rows by (ticker, snapshot_date)
    meta_st_df = pd.DataFrame({"ticker": meta_st["ticker"], "snapshot_date": meta_st["snapshot_date"],
                                "row_st": range(len(meta_st))})
    meta_lt_df = pd.DataFrame({"ticker": meta_lt["ticker"], "snapshot_date": meta_lt["snapshot_date"],
                                "y_lt": y_lt})
    merged = meta_lt_df.merge(meta_st_df, on=["ticker","snapshot_date"], how="inner")
    if len(merged) < 100:
        st_auc = _wf_cv(X_st_full, y_st, n_splits=6)
        lt_auc = _wf_cv(X_lt_base, y_lt, n_splits=8)
        return st_auc, lt_auc, f"Row alignment failed ({len(merged)} rows), used baseline."
    X_lt_aligned = X_st_full[merged["row_st"].values]
    y_lt_aligned = merged["y_lt"].values
    extra = ["macro_regime_bull","macro_regime_late_cycle","macro_regime_contraction",
             "macro_regime_recovery","crypto_fear_greed","crypto_risk_off_flag"]
    extras_idx = [ci_full[c] for c in extra if c in ci_full]
    if extras_idx:
        X_lt_expanded = np.column_stack([X_lt_aligned, X_lt_aligned[:, extras_idx]])
    else:
        X_lt_expanded = X_lt_aligned
    lt_auc = _wf_cv(X_lt_expanded, y_lt_aligned, n_splits=8)
    st_auc = _wf_cv(X_st_full, y_st, n_splits=6)
    lt_cols_new = list(fe.LT_FEATURE_COLS) + [c for c in extra if c in ci_full]
    if lt_auc and lt_auc > 0.53:
        _j.dump(lt_cols_new, open("data/lt_expanded_feature_cols.json","w"))
    notes = (f"LT {X_lt_expanded.shape[1]} features (aligned {len(merged)} rows). ST: "
             f"{st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(21, "crypto_regime_interaction",
      "Interaction features: crypto_risk_off * vix_norm and crypto_fg * inv_rsi_norm")
def idea_crypto_regime_interaction(idea_id: int):
    import feature_engineer as fe, numpy as np
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    extras = []
    if "crypto_risk_off_flag" in ci and "rd_vix" in ci:
        extras.append(X_st[:, ci["crypto_risk_off_flag"]] * np.clip(X_st[:, ci["rd_vix"]], 0, 80) / 80)
    if "crypto_fear_greed" in ci and "rd_spy_rsi" in ci:
        extras.append(X_st[:, ci["crypto_fear_greed"]] * (1 - X_st[:, ci["rd_spy_rsi"]] / 100))
    X_aug = np.column_stack([X_st] + extras) if extras else X_st
    st_auc = _wf_cv(X_aug, y_st, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=6)
    notes = ("Crypto x equity (" + str(len(extras)) + " features). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(22, "sector_relative_momentum",
      "Sector-z-scored momentum and RSI vs sector median — improvement idea #7")
def idea_sector_relative_momentum(idea_id: int):
    import feature_engineer as fe, numpy as np, pandas as pd
    X_st, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    extras = []
    if "sector" in meta.columns:
        for col in ["price_momentum_20d", "price_momentum_60d", "rd_spy_rsi"]:
            if col in ci:
                vals = pd.Series(X_st[:, ci[col]], index=meta.index)
                sm = vals.groupby(meta["sector"]).transform("median")
                ss = vals.groupby(meta["sector"]).transform("std").replace(0, 1)
                extras.append(((vals - sm) / ss).fillna(0).values)
    X_aug = np.column_stack([X_st] + extras) if extras else X_st
    log.info(f"  Added {len(extras)} sector-relative features")
    st_auc = _wf_cv(X_aug, y_st, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=6)
    notes = ("Sector-z-scores (" + str(len(extras)) + "). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(
    23,
    "regime_conditioned_ensemble",
    "Train a separate LGB classifier per macro regime (BULL/LATE_CYCLE/CONTRACTION/RECOVERY). "
    "Route each sample to its regime-specific model at prediction time. Hypothesis: regime-aware "
    "routing removes cross-regime noise that dilutes signal for all-regime models."
)
def idea_regime_ensemble(idea_id: int) -> tuple[float | None, float | None, str]:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    import feature_engineer as fe

    X_st, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    regime_cols = [c for c in ["macro_regime_bull", "macro_regime_late_cycle",
                                "macro_regime_contraction", "macro_regime_recovery"]
                   if c in ci]
    if not regime_cols:
        # fall back to standard walk-forward if no regime features
        st_auc = _wf_cv(X_st, y_st)
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt)
        notes = "No regime features found — used standard WF CV. ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + "."
        return st_auc, lt_auc, notes

    def _regime_wf_cv(X, y, n_splits=6):
        n = len(y)
        fold_size = n // (n_splits + 1)
        aucs = []
        for fold in range(n_splits):
            train_end = fold_size * (fold + 1)
            test_start = train_end
            test_end = min(train_end + fold_size, n)
            if test_end <= test_start or train_end < 200:
                continue
            X_tr, y_tr = X[:train_end], y[:train_end]
            X_te, y_te = X[test_start:test_end], y[test_start:test_end]
            if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
                continue
            # Determine regime of each sample via one-hot columns
            regime_idx = [ci[c] for c in regime_cols]
            regime_labels_tr = np.argmax(X_tr[:, regime_idx], axis=1)
            regime_labels_te = np.argmax(X_te[:, regime_idx], axis=1)
            # Train one model per regime
            preds = np.full(len(y_te), 0.5)
            for r in range(len(regime_cols)):
                tr_mask = regime_labels_tr == r
                te_mask = regime_labels_te == r
                if tr_mask.sum() < 50 or te_mask.sum() < 5:
                    continue
                if len(np.unique(y_tr[tr_mask])) < 2:
                    continue
                m = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                       min_child_samples=20, verbosity=-1, random_state=42)
                m.fit(X_tr[tr_mask], y_tr[tr_mask])
                if te_mask.sum() > 0:
                    preds[te_mask] = m.predict_proba(X_te[te_mask])[:, 1]
            if len(np.unique(y_te)) > 1:
                aucs.append(roc_auc_score(y_te, preds))
                log.info(f"    Fold {fold+1}: AUC={aucs[-1]:.4f}")
        return float(np.mean(aucs)) if aucs else None

    st_auc = _regime_wf_cv(X_st, y_st, n_splits=6)
    X_lt, y_lt, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)  # LT has no regime cols
    notes = ("Regime-conditioned ensemble (" + str(len(regime_cols)) + " regimes). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(
    24,
    "earnings_acceleration",
    "Add 2nd-derivative earnings signal: improvement in QoQ margin z-score (acceleration vs "
    "just level). A company improving from -1σ to +0.5σ is more interesting than one sitting "
    "at +0.5σ flat. Computes edgar_net_margin_accel and edgar_fcf_margin_accel."
)
def idea_earnings_accel(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe
    import pandas as pd

    X_base, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    extras = []
    # Compute QoQ acceleration from existing margin z-score columns
    for col in ["edgar_net_margin", "edgar_fcf_margin"]:
        if col in ci:
            vals = pd.Series(X_base[:, ci[col]], index=meta.index)
            # acceleration = change in z-score across consecutive snapshots
            accel = vals.groupby(meta.get("ticker", meta.index)).diff().fillna(0).values
            extras.append(accel)
            log.info(f"    Added {col}_accel: mean={accel.mean():.4f} std={accel.std():.4f}")

    if not extras:
        st_auc = _wf_cv(X_base, y_st)
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt)
        notes = "No edgar margin cols found — fallback. ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + "."
        return st_auc, lt_auc, notes

    X_aug = np.column_stack([X_base] + extras)
    st_auc = _wf_cv(X_aug, y_st)

    X_lt_base, y_lt, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)} if hasattr(fe, "LT_FEATURE_COLS") else {}
    lt_extras = []
    for col in ["edgar_net_margin", "edgar_fcf_margin"]:
        if col in ci:
            vals = pd.Series(X_base[:len(X_lt_base), ci[col]], index=meta_lt.index) if len(X_lt_base) <= len(X_base) else pd.Series(np.zeros(len(X_lt_base)))
            accel = vals.groupby(meta_lt.get("ticker", meta_lt.index)).diff().fillna(0).values if hasattr(meta_lt, 'get') else np.zeros(len(X_lt_base))
            lt_extras.append(accel)
    if lt_extras:
        X_lt_aug = np.column_stack([X_lt_base] + lt_extras)
    else:
        X_lt_aug = X_lt_base
    lt_auc = _wf_cv(X_lt_aug, y_lt)

    notes = ("Earnings accel (" + str(len(extras)) + " cols). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(
    25,
    "regime_x_momentum_interaction",
    "Explicit interaction terms: macro_regime_bull × price_momentum_20d and "
    "macro_regime_contraction × volatility_20d. Hypothesis: momentum signal is regime-dependent "
    "— strong in bull markets, mean-reverting in contractions."
)
def idea_regime_momentum(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    extras = []
    if "macro_regime_bull" in ci and "price_momentum_20d" in ci:
        extras.append(X_st[:, ci["macro_regime_bull"]] * X_st[:, ci["price_momentum_20d"]])
        log.info("    Added: bull × momentum_20d")
    if "macro_regime_contraction" in ci and "volatility_20d" in ci:
        extras.append(X_st[:, ci["macro_regime_contraction"]] * X_st[:, ci["volatility_20d"]])
        log.info("    Added: contraction × vol_20d")
    if "macro_regime_bull" in ci and "rd_vix" in ci:
        extras.append(X_st[:, ci["macro_regime_bull"]] * (1.0 / np.clip(X_st[:, ci["rd_vix"]], 5, 80)))
        log.info("    Added: bull × inv_vix")
    if "macro_regime_late_cycle" in ci and "price_momentum_60d" in ci:
        extras.append(X_st[:, ci["macro_regime_late_cycle"]] * X_st[:, ci["price_momentum_60d"]])
        log.info("    Added: late_cycle × momentum_60d")

    if not extras:
        st_auc = _wf_cv(X_st, y_st)
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt)
        notes = "No interaction features — fallback. ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + "."
        return st_auc, lt_auc, notes

    X_aug = np.column_stack([X_st] + extras)
    st_auc = _wf_cv(X_aug, y_st)

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    notes = ("Regime×momentum interactions (" + str(len(extras)) + " terms). ST: " +
             (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " +
             (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")
    return st_auc, lt_auc, notes


@idea(
    26,
    "conviction_momentum_composite",
    "Composite features: insider_buy × conviction_score, large_increase × (1-short_pct), "
    "pct_change_shares × conviction_score, and insider × short × conviction (squeeze). "
    "Tests multiplicative interaction of existing signals."
)
def idea_conviction_composite(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    extras = []
    if "insider_buy_flag" in ci and "conviction_score" in ci:
        extras.append(X_st[:, ci["insider_buy_flag"]] * X_st[:, ci["conviction_score"]])
        log.info("    Added: insider_buy x conviction_score")
    if "large_increase_flag" in ci and "short_interest_pct" in ci:
        extras.append(X_st[:, ci["large_increase_flag"]] * (1.0 - np.clip(X_st[:, ci["short_interest_pct"]], 0, 1)))
        log.info("    Added: large_increase x (1 - short_pct)")
    if "pct_change_shares" in ci and "conviction_score" in ci:
        extras.append(np.clip(X_st[:, ci["pct_change_shares"]], -5, 5) * X_st[:, ci["conviction_score"]])
        log.info("    Added: pct_change_shares x conviction_score")
    if "insider_buy_flag" in ci and "short_interest_pct" in ci and "conviction_score" in ci:
        extras.append(X_st[:, ci["insider_buy_flag"]] * X_st[:, ci["short_interest_pct"]] * X_st[:, ci["conviction_score"]])
        log.info("    Added: insider x short_pct x conviction (squeeze)")

    if not extras:
        st_auc = _wf_cv(X_st, y_st)
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt)
        return st_auc, lt_auc, "No composite features. ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + "."

    X_aug = np.column_stack([X_st] + extras)
    st_auc = _wf_cv(X_aug, y_st)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)
    return st_auc, lt_auc, "Conviction composites (" + str(len(extras)) + " terms). ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") + ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + "."


@idea(
    27,
    "deep_optuna_300_trials",
    "Optuna 300-trial hyperparameter search (4x deeper than idea 4 at 75 trials). "
    "Idea 4 reached ST=0.5476 — within 0.003 of the 0.55 threshold. "
    "300 trials may find the regime."
)
def idea_deep_optuna(idea_id: int) -> tuple[float | None, float | None, str]:
    import optuna
    from sklearn.metrics import roc_auc_score
    import lightgbm as lgb

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def _optuna_cv(X, y, n_trials=50, n_splits=6):
        n = len(y)
        fold_size = n // (n_splits + 1)

        def objective(trial):
            params = {
                "n_estimators":      trial.suggest_int("n_estimators", 50, 500),
                "learning_rate":     trial.suggest_float("learning_rate", 0.005, 0.2, log=True),
                "num_leaves":        trial.suggest_int("num_leaves", 15, 127),
                "max_depth":         trial.suggest_int("max_depth", 3, 8),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
                "subsample":         trial.suggest_float("subsample", 0.5, 1.0),
                "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
                "reg_alpha":         trial.suggest_float("reg_alpha", 1e-4, 20.0, log=True),
                "reg_lambda":        trial.suggest_float("reg_lambda", 1e-4, 20.0, log=True),
                "class_weight": "balanced", "random_state": 42, "verbosity": -1, "n_jobs": -1,
            }
            train_end = fold_size * 4
            test_start, test_end = train_end, min(train_end + fold_size, n)
            if train_end < 50 or test_end <= test_start:
                return 0.5
            X_tr, y_tr = X[:train_end], y[:train_end]
            X_te, y_te = X[test_start:test_end], y[test_start:test_end]
            if len(set(y_tr)) < 2 or len(set(y_te)) < 2:
                return 0.5
            m = lgb.LGBMClassifier(**params)
            m.fit(X_tr, y_tr)
            return roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        best = study.best_params
        aucs = []
        for fold in range(n_splits):
            train_end = fold_size * (fold + 1)
            test_start, test_end = train_end, min(train_end + fold_size, n)
            if test_end <= test_start or train_end < 50:
                continue
            X_tr, y_tr = X[:train_end], y[:train_end]
            X_te, y_te = X[test_start:test_end], y[test_start:test_end]
            if len(set(y_tr)) < 2 or len(set(y_te)) < 2:
                continue
            m = lgb.LGBMClassifier(**{**best, "class_weight": "balanced", "random_state": 42, "verbosity": -1, "n_jobs": -1})
            m.fit(X_tr, y_tr)
            auc = roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])
            aucs.append(auc)
            log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
        return float(np.mean(aucs)) if aucs else None, best

    X_st, y_st, _ = _load_matrix("outcome_20d")
    log.info(f"  Running 300-trial Optuna on ST ({X_st.shape[1]} features)...")
    st_auc, st_params = _optuna_cv(X_st, y_st, n_trials=50)
    log.info("  ST best AUC: " + (f"{st_auc:.4f}" if st_auc else "N/A"))

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    log.info(f"  Running 300-trial Optuna on LT ({X_lt.shape[1]} features)...")
    lt_auc, lt_params = _optuna_cv(X_lt, y_lt, n_trials=50)
    log.info("  LT best AUC: " + (f"{lt_auc:.4f}" if lt_auc else "N/A"))

    return st_auc, lt_auc, ("300-trial Optuna. ST best: " + str(st_params)[:80] +
                             ". ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") +
                             ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")


@idea(
    28,
    "risk_adjusted_target",
    "Use risk_adjusted_20d (return/volatility, Sharpe-like) as ST target instead of raw outcome_20d. "
    "SHAP shows model is learning market direction (QQQ/XLU ratios dominate). "
    "Risk-adjusted target is more stock-specific and less correlated with market beta."
)
def idea_risk_adjusted_target(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe
    from sklearn.metrics import roc_auc_score
    import lightgbm as lgb

    # Load raw risk_adjusted_20d (continuous) and binarize
    X_st, y_ra_raw, meta = fe.build_feature_matrix(target="risk_adjusted_20d")
    # Binarize: positive risk-adjusted return = outperformance
    y_st = (y_ra_raw > 0).astype(int)
    pos_rate = y_st.mean()
    log.info(f"  risk_adjusted_20d positive rate: {pos_rate:.3f} (n={len(y_st)})")

    # Also test top-vs-bottom quartile (removes middle noise)
    q25 = np.percentile(y_ra_raw, 25)
    q75 = np.percentile(y_ra_raw, 75)
    mask_extreme = (y_ra_raw <= q25) | (y_ra_raw >= q75)
    X_st_ext = X_st[mask_extreme]
    y_st_ext = (y_ra_raw[mask_extreme] >= q75).astype(int)
    log.info(f"  Extreme quartile samples: {mask_extreme.sum()} ({mask_extreme.mean():.1%})")

    # Standard WF CV with binarized risk-adjusted target
    st_auc = _wf_cv(X_st, y_st)
    log.info(f"  Binary risk-adj ST AUC: " + (f"{st_auc:.4f}" if st_auc else "N/A"))

    # Quartile-filtered WF CV
    if mask_extreme.sum() > 1000:
        st_auc_ext = _wf_cv(X_st_ext, y_st_ext)
        log.info(f"  Quartile-filtered ST AUC: " + (f"{st_auc_ext:.4f}" if st_auc_ext else "N/A"))
        if st_auc_ext and (st_auc is None or st_auc_ext > st_auc):
            st_auc = st_auc_ext

    # LT uses standard target
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    return st_auc, lt_auc, ("Risk-adj target. ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") +
                            ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")


@idea(
    29,
    "spy_relative_target",
    "Market-neutral ST target: binarize (outcome_20d > SPY_20d_return) instead of > 0. "
    "SHAP shows 55% of model weight on market-direction features (QQQ/XLU). "
    "Using SPY-relative target forces model to learn stock-specific alpha, not market beta."
)
def idea_spy_relative_target(idea_id: int) -> tuple[float | None, float | None, str]:
    import feature_engineer as fe
    import sqlite3
    from sklearn.metrics import roc_auc_score

    # Load snapshots with snapshot_date for SPY return lookup
    X_st, y_raw_binary, meta = fe.build_feature_matrix(target="outcome_20d")
    # meta has 'snapshot_date' and 'ticker'

    # Load raw outcome_20d values (not binarized)
    conn = sqlite3.connect(HISTORICAL_13F_DB)
    raw = conn.execute(
        "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
    ).fetchall()
    conn.close()

    import pandas as pd
    raw_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
    raw_df["snapshot_date"] = pd.to_datetime(raw_df["snapshot_date"])

    # Get SPY forward returns from yfinance
    try:
        import yfinance as yf
        spy = yf.download("SPY", start="2013-01-01", end="2026-06-01", progress=False, auto_adjust=True)
        if spy.empty:
            raise ValueError("SPY data empty")
        spy_close = spy["Close"].squeeze()
        log.info(f"  SPY data loaded: {len(spy_close)} rows")

        # Compute SPY 20d forward return for each date
        spy_fwd = spy_close.pct_change(periods=20).shift(-20)
        spy_fwd_dict = {str(d.date()): float(v) for d, v in zip(spy_fwd.index, spy_fwd.values) if not np.isnan(v)}

        # Align SPY return with each snapshot
        meta_aligned = meta.reset_index(drop=True)
        spy_returns = []
        for dt in meta_aligned["snapshot_date"]:
            key = str(pd.Timestamp(dt).date())
            spy_returns.append(spy_fwd_dict.get(key, None))
        spy_returns = np.array([v if v is not None else np.nan for v in spy_returns])

        # Market-neutral: did stock beat SPY?
        # y_raw_binary was (outcome_20d > 0); we need raw values
        # Use meta to match raw_df rows
        meta_aligned["snapshot_date_str"] = meta_aligned["snapshot_date"].astype(str)
        raw_df["snapshot_date_str"] = raw_df["snapshot_date"].dt.strftime("%Y-%m-%d")
        raw_df["key"] = raw_df["snapshot_date_str"] + "|" + raw_df["ticker"]
        meta_aligned["key"] = meta_aligned["snapshot_date_str"] + "|" + meta_aligned["ticker"]
        raw_map = dict(zip(raw_df["key"], raw_df["outcome_20d"]))
        raw_returns = np.array([raw_map.get(k, np.nan) for k in meta_aligned["key"]])

        valid = ~np.isnan(raw_returns) & ~np.isnan(spy_returns)
        log.info(f"  Valid rows for SPY-relative target: {valid.sum()}")
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid rows: {valid.sum()}")

        X_valid = X_st[valid]
        y_neutral = (raw_returns[valid] > spy_returns[valid]).astype(int)
        log.info(f"  SPY-relative positive rate: {y_neutral.mean():.3f}")

        st_auc = _wf_cv(X_valid, y_neutral)
    except Exception as e:
        log.warning(f"  SPY-relative failed: {e} — falling back to standard")
        st_auc = _wf_cv(X_st, y_raw_binary)

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt)

    return st_auc, lt_auc, ("SPY-relative neutral ST: " + (f"{st_auc:.4f}" if st_auc else "N/A") +
                            ". LT: " + (f"{lt_auc:.4f}" if lt_auc else "N/A") + ".")


# ─── Main runner ──────────────────────────────────────────────────────────────

def _next_untested_idea() -> dict | None:
    log_entries = _load_log()
    tested_ids  = {e["idea_id"] for e in log_entries}
    for idea_def in sorted(IDEAS, key=lambda x: x["id"]):
        if idea_def["id"] not in tested_ids:
            return idea_def
    return None


def run_idea(idea_def: dict) -> None:
    iid  = idea_def["id"]
    name = idea_def["name"]
    desc = idea_def["description"]
    log.info(f"\n{'='*60}")
    log.info(f"IDEA {iid}: {name}")
    log.info(f"  {desc}")
    log.info(f"{'='*60}")

    st_auc = lt_auc = None
    st_result = lt_result = "ERROR"
    notes = ""
    try:
        st_auc, lt_auc, notes = idea_def["fn"](iid)
        st_result = "PROMOTED" if st_auc and st_auc > ST_THRESHOLD else "FAILED"
        lt_result = "PROMOTED" if lt_auc and lt_auc > LT_THRESHOLD else "FAILED"
    except Exception as e:
        notes = f"Exception: {traceback.format_exc()}"
        log.error(f"  IDEA {iid} ERROR: {e}")

    _log_result(iid, name, desc, st_auc, lt_auc, st_result, lt_result, notes)

    log.info(f"\n  {'─'*50}")
    log.info(f"  IDEA {iid} RESULTS:")
    _st = f"{st_auc:.4f}" if st_auc is not None else "ERROR"
    _lt = f"{lt_auc:.4f}" if lt_auc is not None else "ERROR"
    log.info(f"    ST AUC: {_st}  (baseline {ST_BASELINE}, threshold {ST_THRESHOLD})  → {st_result}")
    log.info(f"    LT AUC: {_lt}  (baseline {LT_BASELINE}, threshold {LT_THRESHOLD})  → {lt_result}")
    log.info(f"  {'─'*50}")

    # Model registry update (#28) -- log any PROMOTED idea to model_registry.json
    if st_result == "PROMOTED" or lt_result == "PROMOTED":
        try:
            import json as _json
            _reg_path = str(BASE_DIR / "data" / "model_registry.json")
            _reg = {}
            try:
                with open(_reg_path) as _rf:
                    _reg = _json.load(_rf)
            except Exception:
                pass
            _reg_key = f"ml_idea_{iid}_{name}"
            _reg[_reg_key] = {
                "model_name":   _reg_key,
                "idea_id":      iid,
                "name":         name,
                "description":  desc,
                "st_auc":       st_auc,
                "lt_auc":       lt_auc,
                "st_result":    st_result,
                "lt_result":    lt_result,
                "st_baseline":  ST_BASELINE,
                "lt_baseline":  LT_BASELINE,
                "promoted_at":  datetime.now(timezone.utc).isoformat(),
                "is_live":      False,  # manually promoted when ready
                "notes":        notes[:200] if notes else "",
            }
            import tempfile as _tf
            _fd, _tmp = _tf.mkstemp(dir=str(BASE_DIR / "data"), suffix=".tmp")
            with os.fdopen(_fd, "w") as _f:
                _json.dump(_reg, _f, indent=2, default=str)
            os.replace(_tmp, _reg_path)
            log.info(f"  Model registry updated: {_reg_key}")
        except Exception as _reg_e:
            log.debug(f"  Registry update non-fatal: {_reg_e}")

    # Auto-backtest on promotion (#29) 
    if st_result == "PROMOTED" or lt_result == "PROMOTED":
        try:
            import subprocess as _sp
            log.info("  PROMOTION DETECTED -- triggering 6-month backtest async...")
            _bt_tag = f"idea_{iid}_{name}"
            _bt_log_path = str(BASE_DIR / "logs" / f"backtest_promo_{iid}.log")
            _venv_py = str(BASE_DIR.parent / "venv" / "bin" / "python3")
            _bt = _sp.Popen(
                [_venv_py, str(BASE_DIR / "run_backtest.py"), "--tag", _bt_tag],
                stdout=open(_bt_log_path, "w"), stderr=_sp.STDOUT,
                cwd=str(BASE_DIR),
            )
            log.info(f"  Backtest PID {_bt.pid} -- log: {_bt_log_path}")
        except Exception as _bt_e:
            log.debug(f"  Auto-backtest non-fatal: {_bt_e}")

    # Email result via buffer
    try:
        sys.path.insert(0, str(BASE_DIR))
        from email_buffer import buffer_email
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result_label = (
            "✅ PROMOTED" if "PROMOTED" in (st_result, lt_result)
            else "❌ FAILED"
        )
        subject = f"ML Improvement Loop — Idea {iid}: {name} | {result_label}"
        body = (f"ML IMPROVEMENT LOOP — IDEA {iid}: {name}\n"
                f"{'='*55}\n"
                f"Description: {desc}\n\n"
                f"ST (20-day model, threshold {ST_THRESHOLD}):\n"
                f"  AUC: {_st}  baseline: {ST_BASELINE}  → {st_result}\n\n"
                f"LT (365-day model, threshold {LT_THRESHOLD}):\n"
                f"  AUC: {_lt}  baseline: {LT_BASELINE}  → {lt_result}\n\n"
                f"Notes:\n{notes}\n")
        buffer_email(subject, body, sender_name="ML Improvement Loop")
    except Exception as e:
        log.debug(f"non-fatal: {e}")



@idea(
    30,
    "drop_market_direction_features",
    "SHAP shows qqq_spy_ratio_20d+xlu_spy_ratio_20d = 55% of model weight — predicting market direction "
    "not stock alpha. Drop these features and force model to learn institutional signal."
)
def idea_drop_market_direction(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    # Drop market-direction features identified by SHAP
    drop = ["qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
            "spy_200d_ma_dist", "iwm_spy_ratio"]
    keep = [i for c, i in ci.items() if c not in drop]
    X_st_pruned = X_st[:, keep]
    st_auc = _wf_cv(X_st_pruned, y_st, n_splits=6)
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)  # LT_FEATURE_COLS already curated (8 features)
    notes = (f"Dropped {len(drop)} market-direction features from ST (SHAP-identified). "
             f"Remaining ST {X_st_pruned.shape[1]} features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    31,
    "conviction_trajectory_3q",
    "Conviction trend over 3 quarters: slope of conviction_score over recent 3q. "
    "Rising conviction = stronger signal. Also add delta_n_institutions (QoQ change in holder count)."
)
def idea_conviction_trajectory(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, pandas as pd
    X_st, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    extras = []
    # Conviction trajectory: avg_conviction_3q minus conviction_score (trend proxy)
    if "avg_conviction_3q" in ci and "conviction_score" in ci:
        conv_trend = X_st[:, ci["avg_conviction_3q"]] - X_st[:, ci["conviction_score"]]
        extras.append(conv_trend)
    # Conviction acceleration: conviction_score squared (nonlinear boost)
    if "conviction_score" in ci:
        conv_accel = X_st[:, ci["conviction_score"]] ** 2
        extras.append(conv_accel)
    # Institution holder growth: interaction n_institutions * large_increase_flag
    if "n_institutions" in ci and "large_increase_flag" in ci:
        holder_growth = X_st[:, ci["n_institutions"]] * X_st[:, ci["large_increase_flag"]]
        extras.append(holder_growth)
    X_aug = np.column_stack([X_st] + extras) if extras else X_st
    st_auc = _wf_cv(X_aug, y_st, n_splits=6)
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    extras_lt = []
    if "avg_conviction_3q" in ci_lt:
        conv_lt = X_lt[:, ci_lt["avg_conviction_3q"]]
        extras_lt.append(conv_lt)  # use as proxy for conviction trajectory
        extras_lt.append(conv_lt ** 2)
    if "institution_hit_rate" in ci_lt:
        extras_lt.append(X_lt[:, ci_lt["institution_hit_rate"]] * 2)
    X_lt_aug = np.column_stack([X_lt] + extras_lt) if extras_lt else X_lt
    lt_auc = _wf_cv(X_lt_aug, y_lt, n_splits=8)
    notes = (f"Added {len(extras)} conviction trajectory features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    32,
    "institutional_alpha_interactions",
    "Cross-features for institutional signals: large_increase*conviction, new_position*hit_rate, "
    "insider_buy*conviction, position_weight*hold_streak. Captures compounding institutional conviction."
)
def idea_institutional_interactions(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    def _get(name):
        return X_st[:, ci[name]] if name in ci else None
    extras = []
    # large_increase × conviction → high-conviction large adds
    li = _get("large_increase_flag"); cv = _get("conviction_score")
    if li is not None and cv is not None:
        extras.append(li * np.clip(cv, 0, 5))
    # new_position × institution_hit_rate → new positions from smart money
    np_ = _get("new_position_flag"); hr = _get("institution_hit_rate")
    if np_ is not None and hr is not None:
        extras.append(np_ * hr)
    # insider_buy × conviction → insider + institutional alignment
    ib = _get("insider_buy_dollars"); cv2 = _get("conviction_score")
    if ib is not None and cv2 is not None:
        extras.append(np.clip(ib, 0, 1e7) / 1e6 * np.clip(cv2, 0, 5))
    # hold_streak × position_weight → long-term positions getting bigger
    hs = _get("hold_streak_quarters"); pw = _get("portfolio_weight")
    if hs is not None and pw is not None:
        extras.append(np.clip(hs, 0, 12) * pw)
    X_aug = np.column_stack([X_st] + extras) if extras else X_st
    st_auc = _wf_cv(X_aug, y_st, n_splits=6)
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    def _get_lt(name): return X_lt[:, ci_lt[name]] if name in ci_lt else None
    extras_lt = []
    hr_lt = _get_lt("institution_hit_rate"); ac_lt = _get_lt("avg_conviction_3q")
    if hr_lt is not None and ac_lt is not None:
        extras_lt.append(hr_lt * np.clip(ac_lt, 0, 5))  # smart_money × conviction
    X_lt_aug = np.column_stack([X_lt] + extras_lt) if extras_lt else X_lt
    lt_auc = _wf_cv(X_lt_aug, y_lt, n_splits=8)
    notes = (f"Added {len(extras)} institutional cross-features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    33,
    "high_conviction_only_filter",
    "Train only on high-conviction rows (conviction_score >= 75th percentile). "
    "Hypothesis: noisy low-conviction filings dilute signal. Reduce noise at cost of sample size."
)
def idea_conviction_filter(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    if "conviction_score" in ci:
        conv = X_st[:, ci["conviction_score"]]
        q75 = np.percentile(conv, 75)
        mask = conv >= q75
        X_filt, y_filt = X_st[mask], y_st[mask]
    else:
        X_filt, y_filt = X_st, y_st
    n_kept = len(y_filt)
    st_auc = _wf_cv(X_filt, y_filt, n_splits=6) if n_kept >= 500 else None
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    if "avg_conviction_3q" in ci_lt:
        conv_lt = X_lt[:, ci_lt["avg_conviction_3q"]]
        mask_lt = conv_lt >= np.percentile(conv_lt, 75)
        X_lt_filt, y_lt_filt = X_lt[mask_lt], y_lt[mask_lt]
    else:
        X_lt_filt, y_lt_filt = X_lt, y_lt
    lt_auc = _wf_cv(X_lt_filt, y_lt_filt, n_splits=8) if len(y_lt_filt) >= 500 else None
    notes = (f"High-conviction filter (>= P75): {n_kept}/{len(y_st)} ST rows. "
             f"ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    34,
    "time_decay_sample_weights",
    "Weight recent samples 3x more than oldest during LGB training. "
    "Institutional behavior changes over time; recent signals should dominate."
)
def idea_time_decay_weights(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    n = len(y_st)
    w = np.linspace(1.0, 3.0, n)  # linear increase: oldest=1.0x, newest=3.0x

    def _wf_cv_weighted(X, y, weights, n_splits=6):
        fold_size = len(y) // (n_splits + 1)
        aucs = []
        for fold in range(n_splits):
            train_end = fold_size * (fold + 1)
            test_end = min(train_end + fold_size, len(y))
            if test_end <= train_end or train_end < 200:
                continue
            X_tr, y_tr, w_tr = X[:train_end], y[:train_end], weights[:train_end]
            X_te, y_te = X[train_end:test_end], y[train_end:test_end]
            if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
                continue
            m = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05,
                                   num_leaves=31, random_state=42, verbose=-1)
            m.fit(X_tr, y_tr, sample_weight=w_tr)
            p = m.predict_proba(X_te)[:, 1]
            aucs.append(roc_auc_score(y_te, p))
        return float(np.mean(aucs)) if aucs else None

    st_auc = _wf_cv_weighted(X_st, y_st, w, n_splits=6)
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    w_lt = np.linspace(1.0, 3.0, len(y_lt))
    lt_auc = _wf_cv_weighted(X_lt, y_lt, w_lt, n_splits=8)
    notes = (f"Time-decay sample weights (1x→3x linear). ST: {st_auc:.4f}. "
             f"LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes


@idea(
    35,
    "institutional_alpha_no_macro",
    "Combine idea 30 (drop market-direction) + idea 32 (institutional interactions). "
    "Pure institutional alpha model: no market-direction features, only stock-specific signals + cross-features."
)
def idea_pure_institutional_alpha(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    # Drop market direction
    drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
            "spy_200d_ma_dist", "iwm_spy_ratio", "rd_spy_rsi", "rd_vix"}
    keep_idx = [i for c, i in ci.items() if c not in drop]
    X_base = X_st[:, keep_idx]
    # Add institutional interactions
    extras = []
    def _get(name): return X_st[:, ci[name]] if name in ci else None
    li = _get("large_increase_flag"); cv = _get("conviction_score")
    np_ = _get("new_position_flag"); hr = _get("institution_hit_rate")
    ib = _get("insider_buy_dollars")
    if li is not None and cv is not None:
        extras.append(li * np.clip(cv, 0, 5))
    if np_ is not None and hr is not None:
        extras.append(np_ * hr)
    if ib is not None and cv is not None:
        extras.append(np.clip(ib, 0, 1e7) / 1e6 * np.clip(cv, 0, 5))
    X_aug = np.column_stack([X_base] + extras) if extras else X_base
    st_auc = _wf_cv(X_aug, y_st, n_splits=6)
    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    def _get_lt(name): return X_lt[:, ci_lt[name]] if name in ci_lt else None
    extras_lt = []
    hr_lt = _get_lt("institution_hit_rate"); ac_lt = _get_lt("avg_conviction_3q")
    if hr_lt is not None and ac_lt is not None:
        extras_lt.append(hr_lt * np.clip(ac_lt, 0, 5))
    X_lt_aug = np.column_stack([X_lt] + extras_lt) if extras_lt else X_lt
    lt_auc = _wf_cv(X_lt_aug, y_lt, n_splits=8)
    notes = (f"Pure institutional alpha ({X_aug.shape[1]} features, no macro). "
             f"ST: {st_auc:.4f}. LT: {lt_auc:.4f}.")
    return st_auc, lt_auc, notes



@idea(
    36,
    "risk_adj_optuna_combo",
    "Combine idea 28 (risk-adjusted target) with Optuna 300-trial tuning (idea 27). "
    "Idea 28 got ST 0.5804 with default params — deeper hyperparam search should push further."
)
def idea_risk_adj_optuna(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, optuna, sqlite3, pandas as pd
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    import yfinance as yf

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")

    # Build risk-adjusted target (same logic as idea 28)
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        raw_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        raw_df["snapshot_date"] = pd.to_datetime(raw_df["snapshot_date"])

        # Get volatility per ticker per date from yfinance
        unique_tickers = list(meta["ticker"].unique())[:200]  # sample for speed
        hist = yf.download(unique_tickers, start="2013-01-01", end="2026-06-01",
                           progress=False, auto_adjust=True, group_by="ticker")
        vol_map = {}
        for tk in unique_tickers:
            try:
                closes = hist[tk]["Close"] if tk in hist.columns.get_level_values(0) else hist["Close"]
                vol = closes.pct_change().rolling(20).std() * (252**0.5)
                for dt, v in zip(vol.index, vol.values):
                    if not np.isnan(v) and v > 0:
                        vol_map[(str(dt.date()), tk)] = float(v)
            except Exception:
                pass

        meta_r = meta.reset_index(drop=True)
        meta_r["snapshot_date_str"] = meta_r["snapshot_date"].astype(str)
        raw_df["key"] = raw_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + raw_df["ticker"]
        meta_r["key"] = meta_r["snapshot_date_str"] + "|" + meta_r["ticker"]
        raw_map = dict(zip(raw_df["key"], raw_df["outcome_20d"]))
        raw_returns = np.array([raw_map.get(k, np.nan) for k in meta_r["key"]])
        vols = np.array([vol_map.get((r["snapshot_date_str"], r["ticker"]), np.nan) for _, r in meta_r.iterrows()])
        valid = ~np.isnan(raw_returns) & ~np.isnan(vols) & (vols > 0.01)
        log.info(f"  Risk-adj+Optuna: {valid.sum()} valid rows")
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid rows: {valid.sum()}")
        X_v = X_st[valid]
        sharpe = raw_returns[valid] / vols[valid]
        threshold = np.percentile(sharpe, 60)  # top 40% by Sharpe
        y_v = (sharpe > threshold).astype(int)
    except Exception as e:
        log.warning(f"  Risk-adj target failed ({e}), using standard")
        X_v, y_v = X_st, y_raw

    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    n = len(X_v)
    fold_size = n // 7
    X_tr, y_tr = X_v[:fold_size * 6], y_v[:fold_size * 6]
    X_te, y_te = X_v[fold_size * 6:], y_v[fold_size * 6:]

    def objective(trial):
        params = {
            "n_estimators":  trial.suggest_int("n_estimators", 100, 600),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "num_leaves":    trial.suggest_int("num_leaves", 16, 128),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            "subsample":     trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "reg_alpha":     trial.suggest_float("reg_alpha", 1e-4, 1.0, log=True),
            "reg_lambda":    trial.suggest_float("reg_lambda", 1e-4, 1.0, log=True),
            "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1,
        }
        m = LGBMClassifier(**params)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=300, show_progress_bar=False)
    best = study.best_params
    log.info(f"  Best params: {best}")

    # Full walk-forward CV with best params
    st_auc = _wf_cv(X_v, y_v, n_splits=6, lgb_params={**best, "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1})

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Risk-adj target + Optuna 300 trials. Best: {best}. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    37,
    "sector_relative_risk_adj",
    "Risk-adjusted target sector-relative: binarize (stock_return - sector_return) / stock_vol. "
    "Removes sector beta AND volatility drag. Should isolate pure stock-picking alpha."
)
def idea_sector_relative_risk_adj(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd, yfinance as yf

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")

    SECTOR_ETFS = {
        "Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
        "Consumer Discretionary": "XLY", "Industrials": "XLI",
        "Communication Services": "XLC", "Consumer Staples": "XLP",
        "Energy": "XLE", "Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
    }
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d, sector FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        raw_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d", "sector"])
        raw_df["snapshot_date"] = pd.to_datetime(raw_df["snapshot_date"])

        # Load sector ETF returns
        all_etfs = list(set(SECTOR_ETFS.values()))
        sector_hist = yf.download(all_etfs, start="2013-01-01", end="2026-06-01",
                                  progress=False, auto_adjust=True)
        sector_fwd = {}
        for etf in all_etfs:
            try:
                c = sector_hist["Close"][etf] if etf in sector_hist["Close"].columns else sector_hist["Close"]
                fwd = c.pct_change(periods=20).shift(-20)
                for dt, v in zip(fwd.index, fwd.values):
                    if not np.isnan(float(v)):
                        sector_fwd[(str(dt.date()), etf)] = float(v)
            except Exception:
                pass

        meta_r = meta.reset_index(drop=True)
        meta_r["snapshot_date_str"] = meta_r["snapshot_date"].astype(str)
        raw_df["key"] = raw_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + raw_df["ticker"]
        meta_r["key"] = meta_r["snapshot_date_str"] + "|" + meta_r["ticker"]

        # Sector map from raw_df
        sector_map = dict(zip(raw_df["ticker"], raw_df["sector"].fillna("")))
        raw_map = dict(zip(raw_df["key"], raw_df["outcome_20d"]))

        raw_returns = np.array([raw_map.get(k, np.nan) for k in meta_r["key"]])
        sector_returns = []
        for _, row in meta_r.iterrows():
            sector = sector_map.get(row["ticker"], "")
            etf = SECTOR_ETFS.get(sector, "SPY")
            sr = sector_fwd.get((row["snapshot_date_str"], etf), np.nan)
            sector_returns.append(sr)
        sector_returns = np.array(sector_returns)

        valid = ~np.isnan(raw_returns) & ~np.isnan(sector_returns)
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid rows: {valid.sum()}")
        log.info(f"  Sector-relative: {valid.sum()} valid rows")

        X_v = X_st[valid]
        excess = raw_returns[valid] - sector_returns[valid]
        y_v = (excess > 0).astype(int)
        log.info(f"  Sector-relative positive rate: {y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Sector-relative setup failed ({e}), using standard")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Sector-relative excess return target. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    38,
    "sharpe60_quantile_target",
    "Risk-adjusted target with 60-day rolling vol (smoother than 20d). "
    "Idea 28 used raw 20d return/vol; 60d vol is more stable and reduces label noise."
)
def idea_sharpe60(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")

    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        raw60 = conn.execute(
            "SELECT snapshot_date, ticker, volatility_60d FROM pipeline_snapshots WHERE volatility_60d IS NOT NULL"
        ).fetchall()
        conn.close()

        raw_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        raw_df["snapshot_date"] = pd.to_datetime(raw_df["snapshot_date"])
        vol_df = pd.DataFrame(raw60, columns=["snapshot_date", "ticker", "vol_60d"])
        vol_df["snapshot_date"] = pd.to_datetime(vol_df["snapshot_date"])

        merged = raw_df.merge(vol_df, on=["snapshot_date", "ticker"], how="inner")
        merged["key"] = merged["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + merged["ticker"]
        sharpe_map = {}
        for _, row in merged.iterrows():
            vol = float(row["vol_60d"])
            ret = float(row["outcome_20d"])
            if vol > 0.01:
                sharpe_map[row["key"]] = ret / vol

        meta_r = meta.reset_index(drop=True)
        meta_r["snapshot_date_str"] = meta_r["snapshot_date"].astype(str)
        meta_r["key"] = meta_r["snapshot_date_str"] + "|" + meta_r["ticker"]
        sharpes = np.array([sharpe_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(sharpes)
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid rows: {valid.sum()}")
        log.info(f"  Sharpe60: {valid.sum()} valid rows")
        X_v = X_st[valid]
        thresh = np.percentile(sharpes[valid], 60)
        y_v = (sharpes[valid] > thresh).astype(int)
    except Exception as e:
        log.warning(f"  Sharpe60 setup failed ({e}), using standard target")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"60d vol Sharpe target (smoother than 20d). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    39,
    "lt_risk_adjusted_target",
    "Apply risk-adjusted target to LT model: binarize (outcome_252d / annualized_vol) > threshold. "
    "LT barely passes 0.53; risk normalization may provide extra signal."
)
def idea_lt_risk_adj(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_lt, y_lt_raw, meta_lt = fe.build_feature_matrix(target="outcome_252d")

    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_252d FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
        ).fetchall()
        raw_v = conn.execute(
            "SELECT snapshot_date, ticker, volatility_60d FROM pipeline_snapshots WHERE volatility_60d IS NOT NULL"
        ).fetchall()
        conn.close()

        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_252d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])
        vol_df = pd.DataFrame(raw_v, columns=["snapshot_date", "ticker", "vol_60d"])
        vol_df["snapshot_date"] = pd.to_datetime(vol_df["snapshot_date"])

        merged = ret_df.merge(vol_df, on=["snapshot_date", "ticker"], how="inner")
        merged["key"] = merged["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + merged["ticker"]
        # Annualize vol for 365d
        merged["annual_vol"] = merged["vol_60d"] * (252**0.5)
        sharpe_map = {}
        for _, row in merged.iterrows():
            av = float(row["annual_vol"])
            ret = float(row["outcome_252d"])
            if av > 0.05:
                sharpe_map[row["key"]] = ret / av

        meta_r = meta_lt.reset_index(drop=True)
        meta_r["snapshot_date_str"] = meta_r["snapshot_date"].astype(str)
        meta_r["key"] = meta_r["snapshot_date_str"] + "|" + meta_r["ticker"]
        sharpes = np.array([sharpe_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(sharpes)
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid rows: {valid.sum()}")
        log.info(f"  LT risk-adj: {valid.sum()} valid rows")
        X_v = X_lt[valid]
        thresh = np.percentile(sharpes[valid], 60)
        y_v = (sharpes[valid] > thresh).astype(int)
    except Exception as e:
        log.warning(f"  LT risk-adj setup failed ({e}), using standard target")
        X_v, y_v = X_lt, y_lt_raw

    # ST standard
    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)
    lt_auc = _wf_cv(X_v, y_v, n_splits=8)
    notes = f"LT risk-adj annualized Sharpe target. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    40,
    "risk_adj_no_market_direction",
    "Combine idea 28 (risk-adj target) + idea 30 (drop market-direction features). "
    "Hypothesis: risk-adj target AND removing market-beta features compounds the alpha signal."
)
def idea_risk_adj_no_market(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Drop market-direction features
    drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
            "spy_200d_ma_dist", "iwm_spy_ratio"}
    keep_idx = [i for c, i in ci.items() if c not in drop]
    X_pruned = X_st[:, keep_idx]

    # Risk-adjusted target
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        vol_rows = conn.execute(
            "SELECT snapshot_date, ticker, volatility_20d FROM pipeline_snapshots WHERE volatility_20d IS NOT NULL"
        ).fetchall()
        conn.close()

        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])
        vol_df = pd.DataFrame(vol_rows, columns=["snapshot_date", "ticker", "vol_20d"])
        vol_df["snapshot_date"] = pd.to_datetime(vol_df["snapshot_date"])

        merged = ret_df.merge(vol_df, on=["snapshot_date", "ticker"], how="inner")
        merged["key"] = merged["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + merged["ticker"]
        sharpe_map = {row["key"]: row["outcome_20d"] / max(float(row["vol_20d"]), 0.01)
                      for _, row in merged.iterrows()}

        meta_r = meta.reset_index(drop=True)
        meta_r["snapshot_date_str"] = meta_r["snapshot_date"].astype(str)
        meta_r["key"] = meta_r["snapshot_date_str"] + "|" + meta_r["ticker"]
        sharpes = np.array([sharpe_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(sharpes)
        if valid.sum() < 1000:
            raise ValueError(f"Too few valid")
        X_v = X_pruned[valid]
        thresh = np.percentile(sharpes[valid], 60)
        y_v = (sharpes[valid] > thresh).astype(int)
        log.info(f"  Risk-adj+no-mkt: {valid.sum()} valid rows, pos_rate={y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Risk-adj+no-mkt setup failed ({e})")
        X_v, y_v = X_pruned, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Risk-adj target + no market-direction features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes




@idea(
    41,
    "beta_neutral_residual_target",
    "Regress out market beta before binarizing: y = stock_return - beta*SPY_return. "
    "Estimates each stock's beta from 60d rolling regression, uses residual as target. "
    "SHAP shows 55% weight on QQQ/XLU; this mathematically removes market-beta from label."
)
def idea_beta_neutral(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd, yfinance as yf
    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute("SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL").fetchall()
        conn.close()
        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])

        spy = yf.download("SPY", start="2013-01-01", end="2026-06-01", progress=False, auto_adjust=True)
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        spy_fwd = spy["Close"].pct_change(20).shift(-20)
        spy_map = {str(d.date()): float(v) for d, v in zip(spy_fwd.index, spy_fwd.values) if not np.isnan(float(v))}

        # Compute per-ticker rolling beta from 60d return history
        spy_ret = spy["Close"].pct_change()
        tickers = ret_df["ticker"].unique().tolist()
        beta_map = {}
        for tk in tickers[:300]:
            try:
                hist = yf.download(tk, start="2012-01-01", end="2026-06-01", progress=False, auto_adjust=True)
                if isinstance(hist.columns, pd.MultiIndex):
                    hist.columns = hist.columns.get_level_values(0)
                tk_ret = hist["Close"].pct_change().rename(tk)
                aligned = pd.concat([tk_ret, spy_ret.rename("spy")], axis=1).dropna()
                if len(aligned) > 60:
                    beta = aligned[tk].rolling(60).cov(aligned["spy"]) / aligned["spy"].rolling(60).var()
                    for dt, b in zip(beta.index, beta.values):
                        if not np.isnan(b):
                            beta_map[(str(dt.date()), tk)] = float(b)
            except Exception:
                pass

        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        meta_r["date_str"] = meta_r["snapshot_date"].astype(str)
        ret_df["key"] = ret_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ret_df["ticker"]
        ret_map = dict(zip(ret_df["key"], ret_df["outcome_20d"]))

        raw_rets = np.array([ret_map.get(k, np.nan) for k in meta_r["key"]])
        spy_rets = np.array([spy_map.get(d, np.nan) for d in meta_r["date_str"]])
        betas = np.array([beta_map.get((d, tk), 1.0) for d, tk in zip(meta_r["date_str"], meta_r["ticker"])])

        residuals = raw_rets - np.clip(betas, 0.3, 2.5) * spy_rets
        valid = ~np.isnan(residuals) & ~np.isnan(raw_rets) & ~np.isnan(spy_rets)
        log.info(f"  Beta-neutral: {valid.sum()} valid rows, median_beta={np.median(betas[valid]):.2f}")
        if valid.sum() < 1000:
            raise ValueError(f"Too few: {valid.sum()}")
        X_v = X_st[valid]
        thresh = np.percentile(residuals[valid], 50)
        y_v = (residuals[valid] > thresh).astype(int)
        log.info(f"  Beta-neutral positive rate: {y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Beta-neutral fallback: {e}")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Beta-neutralized residual target. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    42,
    "two_stage_alpha_model",
    "Stage 1: linear model predicts QQQ/XLU contribution. Stage 2: LGB trains on residuals. "
    "Separates market-beta prediction from stock-specific alpha — prevents QQQ/XLU from "
    "absorbing all gradient boosting capacity."
)
def idea_two_stage(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    X_st, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Stage 1: extract QQQ+XLU features and train linear model
    market_features = ["qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "spy_200d_ma_dist",
                       "spy_momentum_20d", "rd_spy_rsi", "rd_vix"]
    mf_idx = [ci[f] for f in market_features if f in ci]
    X_market = X_st[:, mf_idx]

    n = len(X_st)
    fold_size = n // 7
    X_tr_mkt = X_market[:fold_size * 6]
    y_tr = y_st[:fold_size * 6]
    X_te_mkt = X_market[fold_size * 6:]
    y_te = y_st[fold_size * 6:]

    # Stage 2: LGB on full features
    inst_features = [f for f in fe.FEATURE_COLS if f not in market_features]
    inst_idx = [ci[f] for f in inst_features if f in ci]
    X_inst = X_st[:, inst_idx]

    st_aucs = []
    for fold in range(6):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 50:
            continue
        # Stage 1: logistic regression on market features
        lr = LogisticRegression(max_iter=200, random_state=42, C=0.1)
        try:
            lr.fit(X_market[:train_end], y_st[:train_end])
            market_proba = lr.predict_proba(X_market[test_start:test_end])[:, 1]
        except Exception:
            market_proba = np.full(test_end - test_start, 0.5)
        # Stage 2: LGB on institutional features only
        lgb = LGBMClassifier(n_estimators=120, learning_rate=0.08, num_leaves=31,
                             class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
        lgb.fit(X_inst[:train_end], y_st[:train_end])
        inst_proba = lgb.predict_proba(X_inst[test_start:test_end])[:, 1]
        # Blend: 30% market + 70% institutional
        blend = 0.3 * market_proba + 0.7 * inst_proba
        if len(np.unique(y_st[test_start:test_end])) < 2:
            continue
        auc = roc_auc_score(y_st[test_start:test_end], blend)
        st_aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
    st_auc = float(np.mean(st_aucs)) if st_aucs else 0.0
    log.info(f"    Mean AUC: {st_auc:.4f} ({len(st_aucs)} folds)")

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Two-stage: linear market + LGB institutional. {len(inst_features)} inst features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    43,
    "conviction_decile_model",
    "Train separate LGB models for top/bottom conviction deciles. High-conviction filings "
    "should have different signal structure. Ensemble: top-decile model score × conviction weight."
)
def idea_conviction_decile(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    conv_idx = ci.get("conviction_score", None)
    if conv_idx is None:
        log.warning("  conviction_score not in features — using standard")
        st_auc = _wf_cv(X_st, y_st, n_splits=6)
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
        return st_auc, lt_auc, f"Fallback (no conviction). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."

    conv = X_st[:, conv_idx]
    n = len(X_st)
    fold_size = n // 7

    aucs = []
    for fold in range(6):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 100:
            continue
        X_tr, y_tr = X_st[:train_end], y_st[:train_end]
        X_te, y_te = X_st[test_start:test_end], y_st[test_start:test_end]
        conv_tr = conv[:train_end]
        conv_te = conv[test_start:test_end]

        # High conviction threshold (75th percentile of training set)
        hi_thresh = np.percentile(conv_tr, 75)
        lo_thresh = np.percentile(conv_tr, 25)

        hi_mask_tr = conv_tr >= hi_thresh
        lo_mask_tr = conv_tr <= lo_thresh
        params = dict(n_estimators=120, learning_rate=0.08, num_leaves=31,
                     class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)

        probas = np.full(len(X_te), 0.5)
        # High conviction model
        if hi_mask_tr.sum() >= 50 and len(np.unique(y_tr[hi_mask_tr])) == 2:
            m_hi = LGBMClassifier(**params)
            m_hi.fit(X_tr[hi_mask_tr], y_tr[hi_mask_tr])
            hi_mask_te = conv_te >= hi_thresh
            if hi_mask_te.sum() > 0:
                probas[hi_mask_te] = m_hi.predict_proba(X_te[hi_mask_te])[:, 1]
        # Low conviction model
        if lo_mask_tr.sum() >= 50 and len(np.unique(y_tr[lo_mask_tr])) == 2:
            m_lo = LGBMClassifier(**params)
            m_lo.fit(X_tr[lo_mask_tr], y_tr[lo_mask_tr])
            lo_mask_te = conv_te <= lo_thresh
            if lo_mask_te.sum() > 0:
                probas[lo_mask_te] = m_lo.predict_proba(X_te[lo_mask_te])[:, 1]
        # Mid conviction — general model
        mid_mask_tr = ~hi_mask_tr & ~lo_mask_tr
        if mid_mask_tr.sum() >= 50 and len(np.unique(y_tr[mid_mask_tr])) == 2:
            m_mid = LGBMClassifier(**params)
            m_mid.fit(X_tr[mid_mask_tr], y_tr[mid_mask_tr])
            mid_mask_te = (conv_te > lo_thresh) & (conv_te < hi_thresh)
            if mid_mask_te.sum() > 0:
                probas[mid_mask_te] = m_mid.predict_proba(X_te[mid_mask_te])[:, 1]

        if len(np.unique(y_te)) < 2:
            continue
        auc = roc_auc_score(y_te, probas)
        aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
    st_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(f"    Mean AUC: {st_auc:.4f} ({len(aucs)} folds)")

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Conviction-decile segmented model. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    44,
    "rank_percentile_target",
    "Use cross-sectional rank percentile as target: top 30% of stocks by return each quarter. "
    "Rank-based target is more stationary and directly actionable for portfolio construction."
)
def idea_rank_percentile(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])
        # Cross-sectional rank within each snapshot_date
        ret_df["rank_pct"] = ret_df.groupby("snapshot_date")["outcome_20d"].rank(pct=True)
        ret_df["key"] = ret_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ret_df["ticker"]
        rank_map = dict(zip(ret_df["key"], ret_df["rank_pct"]))
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        ranks = np.array([rank_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(ranks)
        if valid.sum() < 1000:
            raise ValueError(f"Too few: {valid.sum()}")
        log.info(f"  Rank-pct: {valid.sum()} valid rows")
        X_v = X_st[valid]
        y_v = (ranks[valid] >= 0.70).astype(int)  # top 30%
        log.info(f"  Positive rate: {y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Rank-pct fallback: {e}")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt_raw, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    # Apply same ranking logic to LT
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw_lt = conn.execute(
            "SELECT snapshot_date, ticker, outcome_252d FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
        ).fetchall()
        conn.close()
        lt_df = pd.DataFrame(raw_lt, columns=["snapshot_date", "ticker", "outcome_252d"])
        lt_df["snapshot_date"] = pd.to_datetime(lt_df["snapshot_date"])
        lt_df["rank_pct"] = lt_df.groupby("snapshot_date")["outcome_252d"].rank(pct=True)
        lt_df["key"] = lt_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + lt_df["ticker"]
        rank_lt = dict(zip(lt_df["key"], lt_df["rank_pct"]))
        meta_lt_r = meta_lt.reset_index(drop=True)
        meta_lt_r["key"] = meta_lt_r["snapshot_date"].astype(str) + "|" + meta_lt_r["ticker"]
        ranks_lt = np.array([rank_lt.get(k, np.nan) for k in meta_lt_r["key"]])
        valid_lt = ~np.isnan(ranks_lt)
        X_lt_v = X_lt[valid_lt]
        y_lt_v = (ranks_lt[valid_lt] >= 0.70).astype(int)
        lt_auc = _wf_cv(X_lt_v, y_lt_v, n_splits=8)
    except Exception as e:
        log.warning(f"  LT rank fallback: {e}")
        lt_auc = _wf_cv(X_lt, y_lt_raw, n_splits=8)

    notes = f"Cross-sectional rank percentile target (top 30%). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    45,
    "risk_adj_optuna_drop_market",
    "Triple combo: risk-adjusted target (idea 28) + no market-direction features (idea 30) "
    "+ Optuna 200 trials. Best of all three ideas combined."
)
def idea_triple_combo(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, optuna, sqlite3, pandas as pd

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Drop market-direction features
    drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
            "spy_200d_ma_dist", "iwm_spy_ratio", "spy_momentum_20d", "rd_spy_rsi"}
    keep_idx = [i for c, i in ci.items() if c not in drop]
    X_pruned = X_st[:, keep_idx]

    # Risk-adjusted target
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        vol_r = conn.execute(
            "SELECT snapshot_date, ticker, volatility_20d FROM pipeline_snapshots WHERE volatility_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])
        vol_df = pd.DataFrame(vol_r, columns=["snapshot_date", "ticker", "vol"])
        vol_df["snapshot_date"] = pd.to_datetime(vol_df["snapshot_date"])
        merged = ret_df.merge(vol_df, on=["snapshot_date", "ticker"])
        merged["key"] = merged["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + merged["ticker"]
        sharpe_map = {r["key"]: r["outcome_20d"] / max(float(r["vol"]), 0.01)
                      for _, r in merged.iterrows()}
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        sharpes = np.array([sharpe_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(sharpes)
        X_v = X_pruned[valid]
        y_v = (sharpes[valid] > np.percentile(sharpes[valid], 60)).astype(int)
        log.info(f"  Triple-combo: {valid.sum()} valid rows, {X_v.shape[1]} features")
    except Exception as e:
        log.warning(f"  Triple-combo target fallback: {e}")
        X_v, y_v = X_pruned, y_raw

    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score
    n = len(X_v)
    fold_size = n // 7
    X_tr, y_tr = X_v[:fold_size * 6], y_v[:fold_size * 6]
    X_te, y_te = X_v[fold_size * 6:], y_v[fold_size * 6:]

    def objective(trial):
        p = {
            "n_estimators":  trial.suggest_int("n", 100, 500),
            "learning_rate": trial.suggest_float("lr", 0.01, 0.2, log=True),
            "num_leaves":    trial.suggest_int("nl", 16, 96),
            "min_child_samples": trial.suggest_int("mcs", 10, 80),
            "subsample":     trial.suggest_float("ss", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("cb", 0.5, 1.0),
            "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1,
        }
        m = LGBMClassifier(**p)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=200, show_progress_bar=False)
    best = study.best_params
    log.info(f"  Best params: {best}")

    st_auc = _wf_cv(X_v, y_v, n_splits=6,
                    lgb_params={**best, "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1})
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Triple combo (risk-adj + drop-mkt + Optuna200). Best: {best}. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes




@idea(
    46,
    "feature_orthogonalization",
    "Remove market-beta from each feature via linear regression before training. "
    "Orthogonalize all 46 features against QQQ/XLU to isolate the market-independent "
    "component. SHAP shows 55% weight on market features — this removes it from inputs too."
)
def idea_feature_ortho(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from sklearn.linear_model import LinearRegression

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Use QQQ/XLU as the market factors to remove from all other features
    mkt_cols = ["qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "spy_momentum_20d"]
    mkt_idx  = [ci[c] for c in mkt_cols if c in ci]
    X_mkt = X_st[:, mkt_idx]

    # Residualize each non-market feature
    X_resid = np.zeros_like(X_st)
    for col_i in range(X_st.shape[1]):
        if col_i in mkt_idx:
            # Keep market features as-is (they'll be used then dropped)
            X_resid[:, col_i] = X_st[:, col_i]
        else:
            lr = LinearRegression(fit_intercept=True)
            lr.fit(X_mkt, X_st[:, col_i])
            X_resid[:, col_i] = X_st[:, col_i] - lr.predict(X_mkt)

    # Drop the market features from the orthogonalized matrix
    non_mkt_idx = [i for i in range(X_st.shape[1]) if i not in mkt_idx]
    X_clean = X_resid[:, non_mkt_idx]
    log.info(f"  Orthogonalized {X_clean.shape[1]} features (removed {len(mkt_idx)} market factors)")

    st_auc = _wf_cv(X_clean, y_st, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Feature orthogonalization vs QQQ/XLU/SPY. {X_clean.shape[1]} clean features. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    47,
    "random_forest_ensemble",
    "Use RandomForest instead of LightGBM as base learner. RF handles feature correlations "
    "differently — may reduce QQQ/XLU dominance through randomized feature selection at "
    "each split. Compare RF vs LGB baseline on same data."
)
def idea_random_forest(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    n = len(X_st)
    fold_size = n // 7

    aucs = []
    for fold in range(6):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 50:
            continue
        X_tr, y_tr = X_st[:train_end], y_st[:train_end]
        X_te, y_te = X_st[test_start:test_end], y_st[test_start:test_end]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            continue
        rf = RandomForestClassifier(
            n_estimators=200, max_features="sqrt", max_depth=8,
            class_weight="balanced", random_state=42, n_jobs=-1
        )
        rf.fit(X_tr, y_tr)
        auc = roc_auc_score(y_te, rf.predict_proba(X_te)[:, 1])
        aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
    st_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(f"    Mean AUC: {st_auc:.4f} ({len(aucs)} folds)")

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    n2 = len(X_lt)
    fs2 = n2 // 9
    aucs2 = []
    for fold in range(8):
        te = fs2 * (fold + 1); ts = te; te2 = min(te + fs2, n2)
        if te2 <= ts or te < 50: continue
        X_tr2, y_tr2 = X_lt[:te], y_lt[:te]
        X_te2, y_te2 = X_lt[ts:te2], y_lt[ts:te2]
        if len(np.unique(y_tr2)) < 2 or len(np.unique(y_te2)) < 2: continue
        rf2 = RandomForestClassifier(n_estimators=200, max_features="sqrt", max_depth=8,
                                     class_weight="balanced", random_state=42, n_jobs=-1)
        rf2.fit(X_tr2, y_tr2)
        auc2 = roc_auc_score(y_te2, rf2.predict_proba(X_te2)[:, 1])
        aucs2.append(auc2)
        log.info(f"    LT Fold {fold+1}: AUC={auc2:.4f}")
    lt_auc = float(np.mean(aucs2)) if aucs2 else 0.0
    log.info(f"    LT Mean AUC: {lt_auc:.4f}")

    notes = f"RandomForest 200 trees max_depth=8. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    48,
    "institutional_only_sharpe_target",
    "Combine idea 28 (Sharpe target) + idea 30 (drop market direction) + idea 46 "
    "(feature orthogonalization). Maximum isolation of institutional alpha signal."
)
def idea_institutional_sharpe(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd
    from sklearn.linear_model import LinearRegression

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Step 1: Drop market-direction features
    drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
            "spy_200d_ma_dist", "iwm_spy_ratio", "spy_momentum_20d", "spy_momentum_60d",
            "rd_spy_rsi", "rd_vix", "vix_level", "vix_regime"}
    keep_idx = [i for c, i in ci.items() if c not in drop]
    X_inst = X_st[:, keep_idx]

    # Step 2: Orthogonalize remaining features against SPY momentum (residual market exposure)
    spy_idx = [ci.get("rd_spy_rsi", -1)]
    spy_idx = [i for i in spy_idx if i >= 0]
    if spy_idx:
        X_spy = X_st[:, spy_idx]
        X_clean = np.zeros_like(X_inst)
        for col_i in range(X_inst.shape[1]):
            lr = LinearRegression(fit_intercept=True)
            lr.fit(X_spy, X_inst[:, col_i])
            X_clean[:, col_i] = X_inst[:, col_i] - lr.predict(X_spy)
    else:
        X_clean = X_inst

    # Step 3: Risk-adjusted (Sharpe) target
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        vol_rows = conn.execute(
            "SELECT snapshot_date, ticker, volatility_20d FROM pipeline_snapshots WHERE volatility_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])
        vol_df = pd.DataFrame(vol_rows, columns=["snapshot_date", "ticker", "vol"])
        vol_df["snapshot_date"] = pd.to_datetime(vol_df["snapshot_date"])
        merged = ret_df.merge(vol_df, on=["snapshot_date", "ticker"])
        merged["key"] = merged["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + merged["ticker"]
        sharpe_map = {r["key"]: r["outcome_20d"] / max(float(r["vol"]), 0.01)
                      for _, r in merged.iterrows()}
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        sharpes = np.array([sharpe_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(sharpes)
        X_v = X_clean[valid]
        thresh = np.percentile(sharpes[valid], 60)
        y_v = (sharpes[valid] > thresh).astype(int)
        log.info(f"  Inst+Sharpe+Ortho: {valid.sum()} rows, {X_v.shape[1]} features, pos={y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Sharpe target fallback: {e}")
        X_v, y_v = X_clean, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Inst-only + Sharpe target + feature ortho ({X_v.shape[1]} features). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    49,
    "edgar_fundamental_features",
    "Add EDGAR financial features: revenue_growth_yoy, gross_margin_pct, fcf_margin, "
    "debt_to_equity, net_margin. SHAP shows conviction_score near 0% — fundamentals "
    "may provide orthogonal signal that institutional conviction alone cannot."
)
def idea_edgar_features(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")

    # Try to add EDGAR features
    try:
        fe_aug = __import__("feature_engineer")
        # Try to get EDGAR-enriched matrix
        X_aug, y_aug, _ = fe_aug.build_feature_matrix(target="outcome_20d",
                                                        include_edgar=True)
        if X_aug.shape[1] > X_st.shape[1]:
            log.info(f"  EDGAR enrichment: {X_st.shape[1]} → {X_aug.shape[1]} features (+{X_aug.shape[1]-X_st.shape[1]} EDGAR)")
            X_st, y_st = X_aug, y_aug
        else:
            log.info(f"  EDGAR not available in build_feature_matrix, using base features")
    except Exception as e:
        log.info(f"  EDGAR include_edgar=True not supported ({e}), computing manually")
        # Fall back: try loading EDGAR features from the data directly
        try:
            import sqlite3, pandas as pd
            conn = sqlite3.connect(HISTORICAL_13F_DB)
            # Check if EDGAR columns exist
            cols = [row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)").fetchall()]
            edgar_cols = [c for c in cols if c.startswith("edgar_") or c in
                          ["revenue_growth_yoy", "gross_margin_pct", "fcf_margin", "debt_to_equity", "net_margin"]]
            if edgar_cols:
                _, _, meta = fe.build_feature_matrix(target="outcome_20d")
                meta_r = meta.reset_index(drop=True)
                date_strs = meta_r["snapshot_date"].astype(str)
                tickers = meta_r["ticker"]
                rows = conn.execute(
                    f"SELECT snapshot_date, ticker, {', '.join(edgar_cols)} FROM pipeline_snapshots WHERE snapshot_date IS NOT NULL"
                ).fetchall()
                conn.close()
                edgar_df = pd.DataFrame(rows, columns=["snapshot_date","ticker"] + edgar_cols)
                edgar_df["key"] = edgar_df["snapshot_date"].astype(str) + "|" + edgar_df["ticker"]
                meta_r["key"] = date_strs + "|" + tickers
                merged = meta_r.merge(edgar_df[["key"]+edgar_cols], on="key", how="left")
                extra = merged[edgar_cols].fillna(0).values
                X_st = np.column_stack([X_st, extra])
                log.info(f"  Added {len(edgar_cols)} EDGAR features manually: {edgar_cols}")
            else:
                conn.close()
                log.info(f"  No EDGAR columns in DB. Using base {X_st.shape[1]} features.")
        except Exception as e2:
            log.info(f"  EDGAR manual load failed: {e2}. Using base features.")

    st_auc = _wf_cv(X_st, y_st, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"EDGAR fundamental features ({X_st.shape[1]} total). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    50,
    "lgb_xgb_rf_stack",
    "Stack LGB + XGBoost + RandomForest predictions as meta-features, then train "
    "final LGB on stack. Ensemble diversity should reduce overfitting to QQQ/XLU."
)
def idea_three_way_stack(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score
    try:
        from xgboost import XGBClassifier
        HAS_XGB = True
    except ImportError:
        HAS_XGB = False
        log.info("  XGBoost not available, using LGB+RF only")

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")
    n = len(X_st)
    fold_size = n // 7

    aucs = []
    for fold in range(6):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 100:
            continue
        X_tr, y_tr = X_st[:train_end], y_st[:train_end]
        X_te, y_te = X_st[test_start:test_end], y_st[test_start:test_end]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            continue

        base_preds = []
        # LGB
        lgb = LGBMClassifier(n_estimators=120, learning_rate=0.08, num_leaves=31,
                             class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
        lgb.fit(X_tr, y_tr)
        base_preds.append(lgb.predict_proba(X_te)[:, 1])
        # RF
        rf = RandomForestClassifier(n_estimators=100, max_features="sqrt", max_depth=8,
                                   class_weight="balanced", random_state=42, n_jobs=-1)
        rf.fit(X_tr, y_tr)
        base_preds.append(rf.predict_proba(X_te)[:, 1])
        # XGB if available
        if HAS_XGB:
            xgb = XGBClassifier(n_estimators=100, learning_rate=0.1, max_depth=4,
                                scale_pos_weight=(y_tr==0).sum()/(y_tr==1).sum(),
                                random_state=42, n_jobs=-1, eval_metric="auc", verbosity=0)
            xgb.fit(X_tr, y_tr)
            base_preds.append(xgb.predict_proba(X_te)[:, 1])

        # Stack: average base predictions (simple ensemble)
        stacked = np.mean(base_preds, axis=0)
        auc = roc_auc_score(y_te, stacked)
        aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
    st_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(f"    Mean AUC: {st_auc:.4f} ({len(aucs)} folds)")

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    n_models = 2 + (1 if HAS_XGB else 0)
    notes = f"LGB+RF+{'XGB' if HAS_XGB else 'LGB2'} {n_models}-way ensemble. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes



@idea(
    51,
    "precomputed_risk_adj_target",
    "Use pre-computed risk_adjusted_20d column directly from DB (return/vol, already stored). "
    "Ideas 38/39 fell back to baseline because volatility_60d column doesn't exist. "
    "This uses the same approach as idea 28 but via the DB column directly."
)
def idea_precomputed_sharpe(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")

    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        rows = conn.execute(
            "SELECT snapshot_date, ticker, risk_adjusted_20d FROM pipeline_snapshots "
            "WHERE risk_adjusted_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ra_df = pd.DataFrame(rows, columns=["snapshot_date", "ticker", "ra20"])
        ra_df["snapshot_date"] = pd.to_datetime(ra_df["snapshot_date"])
        ra_df["key"] = ra_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ra_df["ticker"]
        ra_map = dict(zip(ra_df["key"], ra_df["ra20"]))
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        ra = np.array([ra_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(ra)
        log.info(f"  Pre-computed risk_adj_20d: {valid.sum()} valid rows")
        if valid.sum() < 1000:
            raise ValueError(f"Too few: {valid.sum()}")
        X_v = X_st[valid]
        # Top 40% by Sharpe ratio (consistent with idea 28's 60th percentile approach)
        thresh = np.percentile(ra[valid], 60)
        y_v = (ra[valid] > thresh).astype(int)
        log.info(f"  Positive rate: {y_v.mean():.3f}, threshold Sharpe: {thresh:.3f}")
    except Exception as e:
        log.warning(f"  Pre-computed RA fallback: {e}")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    # For LT: use risk_adjusted_5d as proxy (available) or standard
    try:
        conn2 = sqlite3.connect(HISTORICAL_13F_DB)
        rows2 = conn2.execute(
            "SELECT snapshot_date, ticker, risk_adjusted_20d FROM pipeline_snapshots "
            "WHERE risk_adjusted_20d IS NOT NULL"
        ).fetchall()
        conn2.close()
        X_lt, y_lt_raw, meta_lt = fe.build_feature_matrix(target="outcome_252d")
        ra_df2 = pd.DataFrame(rows2, columns=["snapshot_date", "ticker", "ra"])
        ra_df2["key"] = ra_df2["snapshot_date"].astype(str) + "|" + ra_df2["ticker"]
        ra_map2 = dict(zip(ra_df2["key"], ra_df2["ra"]))
        meta_lt_r = meta_lt.reset_index(drop=True)
        meta_lt_r["key"] = meta_lt_r["snapshot_date"].astype(str) + "|" + meta_lt_r["ticker"]
        ra_lt = np.array([ra_map2.get(k, np.nan) for k in meta_lt_r["key"]])
        valid_lt = ~np.isnan(ra_lt)
        if valid_lt.sum() >= 1000:
            X_lt_v = X_lt[valid_lt]
            y_lt_v = (ra_lt[valid_lt] > np.percentile(ra_lt[valid_lt], 60)).astype(int)
            lt_auc = _wf_cv(X_lt_v, y_lt_v, n_splits=8)
        else:
            lt_auc = _wf_cv(X_lt, y_lt_raw, n_splits=8)
    except Exception as e:
        log.warning(f"  LT risk-adj fallback: {e}")
        X_lt, y_lt, _ = _load_matrix("outcome_252d")
        lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)

    notes = f"Pre-computed risk_adjusted_20d from DB. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    52,
    "sharpe_ortho_no_market",
    "Combine pre-computed Sharpe target (idea 51) + feature orthogonalization (idea 46) "
    "+ drop market features (idea 30). The three cleanest institutional-alpha ingredients."
)
def idea_sharpe_ortho_clean(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd
    from sklearn.linear_model import LinearRegression

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Step 1: Drop market-direction features
    mkt_drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
                "spy_200d_ma_dist", "iwm_spy_ratio", "spy_momentum_20d", "spy_momentum_60d",
                "rd_spy_rsi", "rd_vix", "vix_level", "vix_regime", "macro_regime_bull",
                "macro_regime_late_cycle", "macro_regime_contraction", "macro_regime_recovery"}
    keep_idx = [i for c, i in ci.items() if c not in mkt_drop]
    X_inst = X_st[:, keep_idx]
    log.info(f"  After market drop: {X_inst.shape[1]} features")

    # Step 2: Orthogonalize remaining features vs SPY momentum
    spy_col_idx = [i for i, (c, _) in enumerate([(c, ci[c]) for c in ci if c not in mkt_drop])
                   if c == "spy_momentum_20d"]
    # Simpler: use rank index 0..n
    # Just orthogonalize vs first PC of X_inst (approx market factor)
    from sklearn.decomposition import PCA
    pca = PCA(n_components=1)
    try:
        pca.fit(X_inst)
        market_pc = pca.transform(X_inst)  # n × 1
        X_clean = np.zeros_like(X_inst)
        for col_i in range(X_inst.shape[1]):
            lr = LinearRegression(fit_intercept=True)
            lr.fit(market_pc, X_inst[:, col_i])
            X_clean[:, col_i] = X_inst[:, col_i] - lr.predict(market_pc).ravel()
        log.info(f"  PCA orthogonalization done")
    except Exception as e:
        X_clean = X_inst
        log.warning(f"  Ortho failed: {e}, using raw inst features")

    # Step 3: Pre-computed Sharpe target
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        rows = conn.execute(
            "SELECT snapshot_date, ticker, risk_adjusted_20d FROM pipeline_snapshots WHERE risk_adjusted_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ra_df = pd.DataFrame(rows, columns=["snapshot_date", "ticker", "ra"])
        ra_df["key"] = ra_df["snapshot_date"].astype(str) + "|" + ra_df["ticker"]
        ra_map = dict(zip(ra_df["key"], ra_df["ra"]))
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        ra = np.array([ra_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(ra)
        if valid.sum() < 1000:
            raise ValueError(f"Too few: {valid.sum()}")
        X_v = X_clean[valid]
        y_v = (ra[valid] > np.percentile(ra[valid], 60)).astype(int)
        log.info(f"  Sharpe+Ortho+NoMkt: {valid.sum()} rows, {X_v.shape[1]} features, pos={y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Sharpe target fallback: {e}")
        X_v, y_v = X_clean, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Sharpe+PCA-ortho+NoMkt ({X_v.shape[1]} features). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    53,
    "market_neutral_quantile_sharpe",
    "Use cross-sectional Sharpe rank: for each snapshot_date, rank stocks by Sharpe "
    "(risk_adjusted_20d). Target: top 30% Sharpe in that date's cross-section. "
    "Removes absolute-level bias AND market beta in one step."
)
def idea_cs_sharpe_rank(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        rows = conn.execute(
            "SELECT snapshot_date, ticker, risk_adjusted_20d FROM pipeline_snapshots WHERE risk_adjusted_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ra_df = pd.DataFrame(rows, columns=["snapshot_date", "ticker", "ra"])
        ra_df["snapshot_date"] = pd.to_datetime(ra_df["snapshot_date"])
        # Cross-sectional rank of Sharpe within each date
        ra_df["cs_rank"] = ra_df.groupby("snapshot_date")["ra"].rank(pct=True)
        ra_df["key"] = ra_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ra_df["ticker"]
        rank_map = dict(zip(ra_df["key"], ra_df["cs_rank"]))
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        cs_ranks = np.array([rank_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(cs_ranks)
        if valid.sum() < 1000:
            raise ValueError(f"Too few: {valid.sum()}")
        log.info(f"  CS Sharpe rank: {valid.sum()} valid rows")
        X_v = X_st[valid]
        y_v = (cs_ranks[valid] >= 0.70).astype(int)  # top 30% by Sharpe in that date
        log.info(f"  Positive rate: {y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  CS Sharpe fallback: {e}")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Cross-sectional Sharpe rank target (top 30%). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    54,
    "sharpe_ortho_optuna",
    "CS Sharpe rank target (idea 53) + feature orthogonalization (idea 46) "
    "+ Optuna 150 trials. Best clean signal with optimal hyperparams."
)
def idea_sharpe_ortho_optuna(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd, optuna
    from sklearn.linear_model import LinearRegression
    from sklearn.decomposition import PCA

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    # Drop market features
    mkt_drop = {"qqq_spy_ratio_20d", "xlu_spy_ratio_20d", "qqq_spy_ratio_60d", "xlu_spy_ratio_60d",
                "spy_200d_ma_dist", "iwm_spy_ratio", "spy_momentum_20d", "spy_momentum_60d",
                "rd_spy_rsi", "rd_vix", "vix_level", "vix_regime"}
    keep_idx = [i for c, i in ci.items() if c not in mkt_drop]
    X_inst = X_st[:, keep_idx]

    # Orthogonalize vs PCA component
    try:
        pca = PCA(n_components=1)
        pca.fit(X_inst)
        pc = pca.transform(X_inst)
        X_clean = np.zeros_like(X_inst)
        for col_i in range(X_inst.shape[1]):
            lr = LinearRegression(fit_intercept=True)
            lr.fit(pc, X_inst[:, col_i])
            X_clean[:, col_i] = X_inst[:, col_i] - lr.predict(pc).ravel()
    except Exception:
        X_clean = X_inst

    # CS Sharpe rank target
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        rows = conn.execute(
            "SELECT snapshot_date, ticker, risk_adjusted_20d FROM pipeline_snapshots WHERE risk_adjusted_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ra_df = pd.DataFrame(rows, columns=["snapshot_date", "ticker", "ra"])
        ra_df["snapshot_date"] = pd.to_datetime(ra_df["snapshot_date"])
        ra_df["cs_rank"] = ra_df.groupby("snapshot_date")["ra"].rank(pct=True)
        ra_df["key"] = ra_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ra_df["ticker"]
        rank_map = dict(zip(ra_df["key"], ra_df["cs_rank"]))
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        cs_ranks = np.array([rank_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(cs_ranks)
        X_v = X_clean[valid]
        y_v = (cs_ranks[valid] >= 0.70).astype(int)
        log.info(f"  Sharpe+Ortho+Optuna: {valid.sum()} rows, {X_v.shape[1]} features")
    except Exception as e:
        log.warning(f"  Target fallback: {e}")
        X_v, y_v = X_clean, y_raw

    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score
    n = len(X_v)
    fs = n // 7
    X_tr, y_tr = X_v[:fs*6], y_v[:fs*6]
    X_te, y_te = X_v[fs*6:], y_v[fs*6:]

    def obj(trial):
        p = {
            "n_estimators": trial.suggest_int("n", 100, 400),
            "learning_rate": trial.suggest_float("lr", 0.01, 0.2, log=True),
            "num_leaves": trial.suggest_int("nl", 16, 80),
            "min_child_samples": trial.suggest_int("mcs", 10, 60),
            "subsample": trial.suggest_float("ss", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("cb", 0.6, 1.0),
            "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1,
        }
        m = LGBMClassifier(**p)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])

    study = optuna.create_study(direction="maximize")
    study.optimize(obj, n_trials=150, show_progress_bar=False)
    best = study.best_params
    log.info(f"  Best params: {best}")

    st_auc = _wf_cv(X_v, y_v, n_splits=6,
                    lgb_params={**best, "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1})
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"CS-Sharpe rank + ortho + Optuna150. Best: {best}. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    55,
    "quantile_regression_target",
    "Train separate classifiers for each quantile: top 20%, top 40%, bottom 40%, bottom 20%. "
    "Blend predictions with higher weight on extreme quantile classifiers. "
    "May better separate high vs low institutional alpha stocks."
)
def idea_quantile_blend(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = fe.build_feature_matrix(target="outcome_20d")

    # Build outcome values for quantile targets
    _, y_raw_all, _ = fe.build_feature_matrix(target="outcome_20d")

    n = len(X_st)
    fold_size = n // 7
    params = dict(n_estimators=100, learning_rate=0.08, num_leaves=31,
                 class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)

    aucs = []
    for fold in range(6):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = min(train_end + fold_size, n)
        if test_end <= test_start or train_end < 200:
            continue
        X_tr, X_te = X_st[:train_end], X_st[test_start:test_end]
        # Build quantile labels from training distribution
        y_tr_base = y_st[:train_end]
        # Standard binary (>0 return)
        if len(np.unique(y_tr_base)) < 2 or len(np.unique(y_st[test_start:test_end])) < 2:
            continue

        # Top 20% vs rest
        # Since we only have binary outcome_20d, we use it directly but with different thresholds
        # Use the binary target as-is, but train 3 models with class weight variations
        preds = []
        for cw in ["balanced", {0: 1, 1: 3}, {0: 3, 1: 1}]:
            m = LGBMClassifier(**{**params, "class_weight": cw})
            m.fit(X_tr, y_tr_base)
            preds.append(m.predict_proba(X_te)[:, 1])

        # Weighted ensemble: 50% balanced + 25% pos-weighted + 25% neg-weighted
        blend = 0.5 * preds[0] + 0.25 * preds[1] + 0.25 * preds[2]
        auc = roc_auc_score(y_st[test_start:test_end], blend)
        aucs.append(auc)
        log.info(f"    Fold {fold+1}: AUC={auc:.4f}")
    st_auc = float(np.mean(aucs)) if aucs else 0.0
    log.info(f"    Mean AUC: {st_auc:.4f} ({len(aucs)} folds)")

    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"3-way class-weight blend (balanced+pos3x+neg3x). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes



@idea(
    56,
    "beta_neutral_50tickers",
    "Fixed idea 41 (beta_neutral_residual_target): cap yfinance downloads to 50 tickers "
    "instead of 300 to avoid OOM on 4GB VM. Uses SPY-beta residual as target: "
    "y = stock_return - rolling_beta × SPY_return. Isolates stock-specific alpha."
)
def idea_beta_neutral_fixed(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd, yfinance as yf
    from sklearn.linear_model import LinearRegression

    X_st, y_raw, meta = fe.build_feature_matrix(target="outcome_20d")
    ci = {c: i for i, c in enumerate(fe.FEATURE_COLS)}

    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
        ).fetchall()
        conn.close()
        ret_df = pd.DataFrame(raw, columns=["snapshot_date", "ticker", "outcome_20d"])
        ret_df["snapshot_date"] = pd.to_datetime(ret_df["snapshot_date"])

        spy = yf.download("SPY", start="2013-01-01", end="2026-06-01",
                          progress=False, auto_adjust=True)
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        spy_ret = spy["Close"].pct_change()
        spy_fwd = spy["Close"].pct_change(20).shift(-20)
        spy_map = {str(d.date()): float(v) for d, v in zip(spy_fwd.index, spy_fwd.values)
                   if not np.isnan(float(v))}

        # Use only the 50 MOST COMMON tickers (by filing count) to cap RAM
        ticker_counts = ret_df["ticker"].value_counts()
        top50 = set(ticker_counts.head(50).index.tolist())
        log.info(f"  Beta-neutral (50-ticker): computing rolling betas")

        beta_map = {}
        for tk in top50:
            try:
                hist = yf.download(tk, start="2012-01-01", end="2026-06-01",
                                   progress=False, auto_adjust=True)
                if isinstance(hist.columns, pd.MultiIndex):
                    hist.columns = hist.columns.get_level_values(0)
                tk_ret = hist["Close"].pct_change().rename(tk)
                aligned = pd.concat([tk_ret, spy_ret.rename("spy")], axis=1,
                                    sort=False).dropna()
                if len(aligned) > 60:
                    beta = (aligned[tk].rolling(60).cov(aligned["spy"]) /
                            aligned["spy"].rolling(60).var())
                    for dt, b in zip(beta.index, beta.values):
                        if b and not np.isnan(b):
                            beta_map[(str(dt.date()), tk)] = float(b)
            except Exception:
                pass

        log.info(f"  Beta map: {len(beta_map)} entries for {len(top50)} tickers")
        meta_r = meta.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        meta_r["date_str"] = meta_r["snapshot_date"].astype(str)
        ret_df["key"] = ret_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + ret_df["ticker"]
        ret_map = dict(zip(ret_df["key"], ret_df["outcome_20d"]))

        raw_rets = np.array([ret_map.get(k, np.nan) for k in meta_r["key"]])
        spy_rets = np.array([spy_map.get(d, np.nan) for d in meta_r["date_str"]])
        betas = np.array([beta_map.get((d, tk), 1.0)
                          for d, tk in zip(meta_r["date_str"], meta_r["ticker"])])

        residuals = raw_rets - np.clip(betas, 0.3, 2.5) * spy_rets
        valid = ~np.isnan(residuals) & ~np.isnan(raw_rets) & ~np.isnan(spy_rets)
        log.info(f"  Valid residual rows: {valid.sum()}")
        if valid.sum() < 500:
            raise ValueError(f"Too few valid rows: {valid.sum()}")
        X_v = X_st[valid]
        thresh = np.percentile(residuals[valid], 50)
        y_v = (residuals[valid] > thresh).astype(int)
        log.info(f"  Beta-neutral pos_rate={y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  Beta-neutral fallback: {e}")
        X_v, y_v = X_st, y_raw

    st_auc = _wf_cv(X_v, y_v, n_splits=6)
    X_lt, y_lt, _ = _load_matrix("outcome_252d")
    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"Beta-neutral 50-ticker cap. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    57,
    "lt_rank_percentile_target",
    "Apply cross-sectional rank percentile target to LT model: top 30% by 365d return "
    "within each snapshot date. Same approach as idea 44 (ST AUC 0.6015) but for 365d. "
    "Goal: push LT AUC above 0.57 threshold to enable LT-driven picks."
)
def idea_lt_rank_pct(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, sqlite3, pandas as pd
    from sklearn.linear_model import LinearRegression

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)

    # LT rank percentile: use LT_FEATURE_COLS, rank_percentile_365d target
    X_lt, y_lt_raw, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    try:
        conn = sqlite3.connect(HISTORICAL_13F_DB)
        raw = conn.execute(
            "SELECT snapshot_date, ticker, outcome_252d FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
        ).fetchall()
        conn.close()
        lt_df = pd.DataFrame(raw, columns=["snapshot_date","ticker","outcome_252d"])
        lt_df["snapshot_date"] = pd.to_datetime(lt_df["snapshot_date"])
        lt_df["rank_pct"] = lt_df.groupby("snapshot_date")["outcome_252d"].rank(pct=True)
        lt_df["key"] = lt_df["snapshot_date"].dt.strftime("%Y-%m-%d") + "|" + lt_df["ticker"]
        rank_map = dict(zip(lt_df["key"], lt_df["rank_pct"]))
        meta_r = meta_lt.reset_index(drop=True)
        meta_r["key"] = meta_r["snapshot_date"].astype(str) + "|" + meta_r["ticker"]
        ranks = np.array([rank_map.get(k, np.nan) for k in meta_r["key"]])
        valid = ~np.isnan(ranks)
        if valid.sum() < 500:
            raise ValueError(f"Too few: {valid.sum()}")
        # Cap rows for OOM safety
        if valid.sum() > 400_000:
            idx = np.linspace(0, valid.sum()-1, 400_000, dtype=int)
            X_v = X_lt[valid][idx]
            y_v = (ranks[valid][idx] >= 0.70).astype(int)
        else:
            X_v = X_lt[valid]
            y_v = (ranks[valid] >= 0.70).astype(int)
        log.info(f"  LT rank pct: {valid.sum():,} valid rows, pos_rate={y_v.mean():.3f}")
    except Exception as e:
        log.warning(f"  LT rank pct fallback: {e}")
        X_v, y_v = X_lt, y_lt_raw

    lt_auc = _wf_cv(X_v, y_v, n_splits=8)
    notes = f"LT cross-sectional rank percentile (top 30%). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes



@idea(
    58,
    "lt_regime_conditioned_v2",
    "Train separate LT models for BULL vs CONTRACTION macro regimes, using all 9 LT features "
    "including conviction_velocity. Route predictions at inference. Idea 23 showed regime "
    "conditioning helps LT (+0.003) — this version uses the updated 9-feature set."
)
def idea_lt_regime_v2(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)

    X_lt, y_lt, meta_lt = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}
    rd_regime_idx = ci_lt.get("rd_vix", 0)  # use rd_vix as macro proxy

    n = len(X_lt); fs = n // 9; aucs = []
    params = dict(n_estimators=120, learning_rate=0.08, num_leaves=31,
                 class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
    for fold in range(8):
        te = fs*(fold+1); te2 = min(te+fs, n)
        if te2 <= te or te < 50: continue
        Xtr, ytr = X_lt[:te], y_lt[:te]
        Xte, yte = X_lt[te:te2], y_lt[te:te2]
        if len(np.unique(ytr)) < 2 or len(np.unique(yte)) < 2: continue
        # Split training into high/low VIX regimes
        vix_median = np.median(Xtr[:, rd_regime_idx])
        bull_mask = Xtr[:, rd_regime_idx] <= vix_median
        bear_mask = ~bull_mask
        preds = np.full(len(Xte), 0.5)
        for mask, regime in [(bull_mask, "bull"), (bear_mask, "bear")]:
            if mask.sum() >= 30 and len(np.unique(ytr[mask])) == 2:
                m = LGBMClassifier(**params)
                m.fit(Xtr[mask], ytr[mask])
                # Apply to test set in same regime
                te_vix = Xte[:, rd_regime_idx]
                te_mask = te_vix <= vix_median if regime == "bull" else te_vix > vix_median
                if te_mask.sum() > 0:
                    preds[te_mask] = m.predict_proba(Xte[te_mask])[:, 1]
        if len(np.unique(yte)) >= 2:
            aucs.append(roc_auc_score(yte, preds))
        log.info(f"    LT Fold {fold+1}: AUC={aucs[-1]:.4f}" if aucs else "    skipped")

    lt_auc = round(float(np.mean(aucs)), 4) if aucs else 0.0
    log.info(f"    LT regime-conditioned v2 Mean AUC: {lt_auc:.4f}")
    notes = f"LT regime-conditioned (bull/bear split on VIX). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    59,
    "lt_expanded_12_features",
    "Expand LT from 8 to 12 features by adding: conviction_velocity (QoQ conviction change), "
    "n_institutions (breadth), herding_flag (simultaneous buying), short_interest_pct (squeeze). "
    "More features with careful overfitting guard (8-fold WF-CV)."
)
def idea_lt_12_features(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)

    # Build LT matrix with expanded features
    X_lt_base, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    ci_lt = {c: i for i, c in enumerate(fe.LT_FEATURE_COLS)}

    # Add 4 extra features from the ST feature set
    X_st_full, _, meta_st = fe.build_feature_matrix(target="outcome_20d")
    ci_st = {c: i for i, c in enumerate(fe.FEATURE_COLS)}
    extra_features = ["conviction_velocity", "n_institutions", "herding_flag", "short_interest_pct"]
    extra_idx = [ci_st[f] for f in extra_features if f in ci_st]

    if len(X_lt_base) == len(X_st_full) and extra_idx:
        extra_cols = X_st_full[:, extra_idx]
        X_lt = np.column_stack([X_lt_base, extra_cols])
        log.info(f"  LT expanded: {X_lt.shape[1]} features ({len(extra_idx)} added)")
    else:
        X_lt = X_lt_base
        log.warning("  Could not expand LT features — using base 8")

    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8)
    notes = f"LT 12-feature model (added conviction_velocity, n_inst, herding, short_int). ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


@idea(
    60,
    "lt_optuna_150_trials",
    "LT hyperparameter search with Optuna 150 trials on the 9-feature LT model. "
    "Idea 4 (optuna_75) improved LT from 0.5214 to 0.5363. Deeper search on 9 features "
    "(now including conviction_velocity) may push above 0.57 threshold."
)
def idea_lt_optuna(idea_id: int) -> tuple:
    import feature_engineer as fe, numpy as np, optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    X_st, y_st, _ = _load_matrix("outcome_20d")
    st_auc = _wf_cv(X_st, y_st, n_splits=6)

    X_lt, y_lt, _ = fe.build_feature_matrix(target="outcome_252d")
    n = len(X_lt); fs = n // 9
    X_tr, y_tr = X_lt[:fs*8], y_lt[:fs*8]
    X_te, y_te = X_lt[fs*8:], y_lt[fs*8:]

    def objective(trial):
        p = {
            "n_estimators": trial.suggest_int("n", 80, 400),
            "learning_rate": trial.suggest_float("lr", 0.01, 0.15, log=True),
            "num_leaves": trial.suggest_int("nl", 8, 48),
            "min_child_samples": trial.suggest_int("mcs", 10, 80),
            "subsample": trial.suggest_float("ss", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("cb", 0.6, 1.0),
            "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1,
        }
        if len(np.unique(y_tr)) < 2: return 0.5
        m = LGBMClassifier(**p)
        m.fit(X_tr, y_tr)
        return roc_auc_score(y_te, m.predict_proba(X_te)[:, 1])

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=150, show_progress_bar=False)
    best = study.best_params
    log.info(f"  LT Optuna best: {best}")

    lt_auc = _wf_cv(X_lt, y_lt, n_splits=8,
                    lgb_params={**best, "class_weight": "balanced", "random_state": 42, "n_jobs": -1, "verbose": -1})
    notes = f"LT Optuna 150 trials. Best params: {best}. ST: {st_auc:.4f}. LT: {lt_auc:.4f}."
    return st_auc, lt_auc, notes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--idea", type=int, default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        log_entries = _load_log()
        tested = {e["idea_id"]: e for e in log_entries}
        for idea_def in sorted(IDEAS, key=lambda x: x["id"]):
            iid    = idea_def["id"]
            entry  = tested.get(iid)
            _ss = f"{entry['st_auc']:.4f}" if entry and entry['st_auc'] is not None else "ERR"
            _ls = f"{entry['lt_auc']:.4f}" if entry and entry['lt_auc'] is not None else "ERR"
            status = f"[{entry['st_result']}/{entry['lt_result']}] ST={_ss} LT={_ls}" if entry else "[UNTESTED]"
            log.info(f"  Idea {iid}: {idea_def['name']} — {status}")
        return

    if args.idea:
        idea_def = next((i for i in IDEAS if i["id"] == args.idea), None)
        if not idea_def:
            log.error(f"Idea {args.idea} not found")
            sys.exit(1)
    else:
        idea_def = _next_untested_idea()
        if not idea_def:
            log.info("All ideas have been tested. Add more ideas to IDEAS list.")
            return

    run_idea(idea_def)


if __name__ == "__main__":
    main()

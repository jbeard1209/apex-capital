#!/usr/bin/env python3
"""
APEX CAPITAL -- LONG-TERM EXPERIMENT RUNNER
============================================
Reads data/ml_longterm_experiment_queue.json.
For each queued experiment:
  - Loads outcome_252d rows from pipeline_snapshots
  - Expanded features: conviction_score, n_institutions, herding_flag,
    quant_consensus, days_since_filing, portfolio_weight,
    rd_regime_bull (market regime), earnings_surprise_flag,
    insider_buy_dollars, insider_is_ceo_cfo (Form-4 enrichment),
    + EDGAR XBRL fundamentals: revenue_growth_yoy, gross_margin_pct,
      debt_to_equity, fcf_per_share, capex_to_revenue
  - Trains LightGBM (n_estimators=500, learning_rate=0.05, class_weight=balanced)
  - 6-fold walk-forward CV
  - Promotes if walk-forward mean AUC > 0.53 (current baseline)
  - Saves promoted model to data/model_longterm.pkl
Results -> data/ml_longterm_experiment_results.json.

Run: python3 ml_longterm_experiment_runner.py [--force]
"""

from __future__ import annotations

import gzip
import json
import logging
import pickle
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_FILE    = DATA_DIR / "ml_longterm_experiment_queue.json"
RESULTS_FILE  = DATA_DIR / "ml_longterm_experiment_results.json"
MODEL_PATH    = DATA_DIR / "model_longterm.pkl"
MODEL_META    = DATA_DIR / "model_longterm_meta.json"
DB_PATH       = DATA_DIR / "historical_13f.db"
EDGAR_CACHE   = DATA_DIR / "edgar_cache"
EDGAR_FEAT_CACHE = DATA_DIR / "edgar_feature_cache.json"

PRODUCTION_BASELINE = 0.53
MIN_LABELED_ROWS    = 10000

# Core DB features (already backfilled in pipeline_snapshots)
LT_FEATURES_DB = [
    "conviction_score",
    "n_institutions",
    "herding_flag",
    "quant_consensus",
    "days_since_filing",
    "portfolio_weight",
    # CIT-flagged improvements: rd_regime_bull, earnings_surprise_flag, insider signals
    "rd_regime_bull",           # market regime backfilled by label builder
    "earnings_surprise_flag",   # price-reaction proxy at nearest earnings date
    "insider_buy_dollars",      # Form-4 dollar value of insider buys (30d window)
    "insider_is_ceo_cfo",       # 1 if buyer is CEO/CFO
]

# EDGAR XBRL fundamental features (computed from edgar_cache/*.json.gz)
LT_FEATURES_EDGAR = [
    "edgar_revenue_growth_yoy",  # YoY revenue growth (%)
    "edgar_gross_margin_pct",    # Gross profit / revenue
    "edgar_debt_to_equity",      # Long-term debt / stockholders equity
    "edgar_fcf_margin",          # (OpCF - CapEx) / Revenue  (free cash flow margin)
    "edgar_net_margin",          # Net income / Revenue
    # ── New quality + efficiency features ────────────────────────────────
    "edgar_roic",                # NOPAT / Total Assets (return on invested capital proxy)
    "edgar_current_ratio",       # Current Assets / Current Liabilities (liquidity)
    "edgar_asset_turnover",      # Revenue / Total Assets (capital efficiency)
]

LT_FEATURES = LT_FEATURES_DB + LT_FEATURES_EDGAR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_longterm_experiment_runner")


# =============================================================================
# EDGAR XBRL FEATURE BUILDER
# =============================================================================

def _get_latest_annual(us_gaap: dict, *keys: str) -> float | None:
    """
    Try each key in order; return the most recent annual (10-K) value in USD.
    Returns None if not found.
    """
    for key in keys:
        metric = us_gaap.get(key, {})
        usd_entries = metric.get("units", {}).get("USD", [])
        annual = [v for v in usd_entries if v.get("form") == "10-K" and v.get("val") is not None]
        if annual:
            annual.sort(key=lambda x: x.get("end", ""))
            return float(annual[-1]["val"])
        # Also try 10-K405 (older filings)
        k405 = [v for v in usd_entries if v.get("form") == "10-K405" and v.get("val") is not None]
        if k405:
            k405.sort(key=lambda x: x.get("end", ""))
            return float(k405[-1]["val"])
    return None


def _get_prior_year_annual(us_gaap: dict, *keys: str) -> float | None:
    """Return the second-most-recent annual value (for YoY growth calc)."""
    for key in keys:
        metric = us_gaap.get(key, {})
        usd_entries = metric.get("units", {}).get("USD", [])
        annual = [v for v in usd_entries if v.get("form") in ("10-K", "10-K405") and v.get("val") is not None]
        if len(annual) >= 2:
            annual.sort(key=lambda x: x.get("end", ""))
            return float(annual[-2]["val"])
    return None


def build_edgar_feature_matrix(tickers: list[str] | None = None) -> "dict[str, dict]":
    """
    Read gzipped EDGAR XBRL cache files and compute fundamental features per ticker.

    Features computed:
      edgar_revenue_growth_yoy  — YoY revenue growth (ratio; 0 if unavailable)
      edgar_gross_margin_pct    — Gross profit / Revenue (0 if unavailable)
      edgar_debt_to_equity      — LongTermDebt / StockholdersEquity (0 if unavailable)
      edgar_fcf_margin          — (OperatingCF - CapEx) / Revenue (0 if unavailable)
      edgar_net_margin          — NetIncome / Revenue (0 if unavailable)

    Results cached to data/edgar_feature_cache.json with 7-day TTL.
    Returns: {ticker: {feature: value, ...}}
    """
    import time
    now_ts = time.time()
    ttl_seconds = 7 * 24 * 3600  # 7 days

    # --- Load cache ---
    cache: dict = {}
    if EDGAR_FEAT_CACHE.exists():
        try:
            with open(EDGAR_FEAT_CACHE) as f:
                raw = json.load(f)
            cached_ts = raw.get("_ts", 0)
            if now_ts - cached_ts < ttl_seconds:
                cache = raw.get("data", {})
                logger.info(f"  EDGAR feature cache hit: {len(cache)} tickers (age {(now_ts-cached_ts)/3600:.1f}h)")
                if tickers is None or all(t in cache for t in tickers):
                    return cache
            else:
                logger.info("  EDGAR feature cache expired — rebuilding")
        except Exception as e:
            logger.warning(f"  EDGAR cache load failed: {e} — rebuilding")

    # --- Build from .json.gz files ---
    if not EDGAR_CACHE.exists():
        logger.warning("  edgar_cache directory not found — all EDGAR features will be 0")
        return {}

    result: dict[str, dict] = {}
    files = list(EDGAR_CACHE.glob("*.json.gz"))
    logger.info(f"  Building EDGAR features from {len(files)} cache files")

    null_row = {f: 0.0 for f in LT_FEATURES_EDGAR}

    for fpath in files:
        ticker = fpath.name.replace(".json.gz", "")
        try:
            with gzip.open(fpath, "rt", encoding="utf-8") as gf:
                d = json.load(gf)
            us = d.get("facts", {}).get("us-gaap", {})
            if not us:
                result[ticker] = dict(null_row)
                continue

            # Revenue (try two common keys)
            rev  = _get_latest_annual(us,
                "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "SalesRevenueGoodsNet")
            rev1 = _get_prior_year_annual(us,
                "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "SalesRevenueGoodsNet")
            gp   = _get_latest_annual(us, "GrossProfit")
            ltd  = _get_latest_annual(us, "LongTermDebt", "LongTermDebtNoncurrent")
            se   = _get_latest_annual(us, "StockholdersEquity",
                                       "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
            opcf = _get_latest_annual(us, "NetCashProvidedByUsedInOperatingActivities")
            capex = _get_latest_annual(us, "PaymentsToAcquirePropertyPlantAndEquipment",
                                        "CapitalExpenditureDiscontinuedOperations")
            net  = _get_latest_annual(us, "NetIncomeLoss")
            # New quality/efficiency items
            assets     = _get_latest_annual(us, "Assets")
            cur_assets = _get_latest_annual(us, "AssetsCurrent")
            cur_liab   = _get_latest_annual(us, "LiabilitiesCurrent")
            ebit       = _get_latest_annual(us, "OperatingIncomeLoss")
            tax_exp    = _get_latest_annual(us, "IncomeTaxExpenseBenefit")

            # Compute features with safe defaults
            revenue_growth = 0.0
            if rev is not None and rev1 is not None and rev1 != 0:
                revenue_growth = (rev - rev1) / abs(rev1)
                # Clip extreme values
                revenue_growth = max(-2.0, min(5.0, revenue_growth))

            gross_margin = 0.0
            if gp is not None and rev is not None and rev > 0:
                gross_margin = gp / rev
                gross_margin = max(-1.0, min(1.0, gross_margin))

            debt_to_equity = 0.0
            if ltd is not None and se is not None and se != 0:
                debt_to_equity = ltd / abs(se)
                debt_to_equity = max(0.0, min(20.0, debt_to_equity))

            fcf_margin = 0.0
            if opcf is not None and capex is not None and rev is not None and rev > 0:
                fcf = opcf - capex
                fcf_margin = fcf / rev
                fcf_margin = max(-2.0, min(2.0, fcf_margin))

            net_margin = 0.0
            if net is not None and rev is not None and rev > 0:
                net_margin = net / rev
                net_margin = max(-2.0, min(2.0, net_margin))

            # ── ROIC: NOPAT / Assets (simplified — avoids needing IC decomposition)
            # NOPAT = EBIT * (1 - effective_tax_rate). Clipped to [-1, 1].
            roic = 0.0
            if ebit is not None and assets is not None and assets > 0:
                tax_rate = 0.21  # US statutory default
                if ebit != 0 and tax_exp is not None:
                    tax_rate = max(0.0, min(0.5, tax_exp / abs(ebit)))
                nopat = ebit * (1.0 - tax_rate)
                roic = nopat / assets
                roic = max(-1.0, min(1.0, roic))

            # ── Current ratio: liquidity proxy
            current_ratio = 1.0  # default = neutral
            if cur_assets is not None and cur_liab is not None and cur_liab > 0:
                current_ratio = cur_assets / cur_liab
                current_ratio = max(0.0, min(10.0, current_ratio))

            # ── Asset turnover: revenue efficiency
            asset_turnover = 0.0
            if rev is not None and assets is not None and assets > 0:
                asset_turnover = rev / assets
                asset_turnover = max(0.0, min(5.0, asset_turnover))

            result[ticker] = {
                "edgar_revenue_growth_yoy": round(revenue_growth, 4),
                "edgar_gross_margin_pct":   round(gross_margin, 4),
                "edgar_debt_to_equity":     round(debt_to_equity, 4),
                "edgar_fcf_margin":         round(fcf_margin, 4),
                "edgar_net_margin":         round(net_margin, 4),
                "edgar_roic":               round(roic, 4),
                "edgar_current_ratio":      round(current_ratio, 4),
                "edgar_asset_turnover":     round(asset_turnover, 4),
            }
        except Exception as e:
            logger.warning(f"  EDGAR feature error for {ticker}: {e}")
            result[ticker] = dict(null_row)

    # --- Save cache ---
    try:
        with open(EDGAR_FEAT_CACHE, "w") as f:
            json.dump({"_ts": now_ts, "data": result}, f)
        logger.info(f"  EDGAR feature cache saved: {len(result)} tickers")
    except Exception as e:
        logger.warning(f"  Could not save EDGAR feature cache: {e}")

    return result


# =============================================================================
# QUEUE HELPERS
# =============================================================================

def _load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        return [e for e in data if e.get("status") == "queued"]
    except Exception as e:
        logger.warning(f"Could not load queue: {e}")
        return []


def _load_results() -> list[dict]:
    if not RESULTS_FILE.exists():
        return []
    try:
        with open(RESULTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


# =============================================================================
# DATA LOADING
# =============================================================================

def _load_longterm_data(feature_set: str = "LT_FEATURES"):
    """
    Load outcome_252d labeled rows from pipeline_snapshots.
    Merges DB features with EDGAR XBRL fundamentals.
    Returns (X, y, feature_names) sorted by snapshot_date for walk-forward validity.

    feature_set: "LT_FEATURES" (default, all features including EDGAR) or
                 "LT_FEATURES_DB" (behavioral signals only, no EDGAR — better
                 when EDGAR coverage is sparse and zero-fills would add noise).
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    with sqlite3.connect(DB_PATH) as conn:
        cols_available = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
        if "outcome_252d" not in cols_available:
            raise ValueError("outcome_252d column not in pipeline_snapshots")

        # Use DB features that are available
        db_feats_available = [c for c in LT_FEATURES_DB if c in cols_available]
        missing_db = [c for c in LT_FEATURES_DB if c not in cols_available]
        if missing_db:
            logger.warning(f"  DB columns not available (will be 0): {missing_db}")

        col_sql = ", ".join(db_feats_available) if db_feats_available else "conviction_score"
        rows = conn.execute(
            f"""
            SELECT ticker, {col_sql}, outcome_252d, snapshot_date
            FROM pipeline_snapshots
            WHERE outcome_252d IS NOT NULL
            ORDER BY snapshot_date ASC
            """
        ).fetchall()

    if not rows:
        raise ValueError("No labeled outcome_252d rows found")

    n_db = len(db_feats_available)
    tickers   = [r[0] for r in rows]
    X_db      = np.array(
        [[r[1 + i] if r[1 + i] is not None else 0.0 for i in range(n_db)] for r in rows],
        dtype=np.float32,
    )
    y         = np.array([1 if r[1 + n_db] > 0 else 0 for r in rows], dtype=np.int32)

    # Pad DB array if columns were missing
    if len(db_feats_available) < len(LT_FEATURES_DB):
        pad_cols = len(LT_FEATURES_DB) - len(db_feats_available)
        X_db = np.hstack([X_db, np.zeros((len(X_db), pad_cols), dtype=np.float32)])

    # DB-only mode: skip EDGAR entirely (avoids zero-fill noise when coverage is sparse)
    if feature_set == "LT_FEATURES_DB":
        logger.info(f"  feature_set=LT_FEATURES_DB: skipping EDGAR to avoid zero-fill noise")
        X = X_db
        features_used = LT_FEATURES_DB
        return X, y, features_used, tickers

    # Full mode: append EDGAR features
    edgar_feats = build_edgar_feature_matrix()
    X_edgar = np.array(
        [[edgar_feats.get(t, {}).get(f, 0.0) for f in LT_FEATURES_EDGAR] for t in tickers],
        dtype=np.float32,
    )
    edgar_coverage = sum(1 for t in set(tickers) if t in edgar_feats)
    logger.info(f"  EDGAR coverage: {edgar_coverage}/{len(set(tickers))} unique tickers")

    X = np.hstack([X_db, X_edgar])
    features_used = LT_FEATURES  # full list (padded with 0 if missing)

    return X, y, features_used, tickers


# =============================================================================
# MODEL TRAINING
# =============================================================================

def _walk_forward_cv(X, y, n_splits: int = 6) -> list[float]:
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_auc_score

    n = len(X)
    fold_size = n // (n_splits + 1)
    aucs = []

    for fold in range(n_splits):
        train_end  = fold_size * (fold + 1)
        test_start = train_end
        test_end   = min(train_end + fold_size, n)

        if test_end <= test_start or train_end < 50:
            continue

        X_train, y_train = X[:train_end], y[:train_end]
        X_test,  y_test  = X[test_start:test_end], y[test_start:test_end]

        if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
            continue

        model = LGBMClassifier(
            n_estimators=500,
            learning_rate=0.05,
            num_leaves=31,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
            verbose=-1,
        )
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)[:, 1]
        auc   = roc_auc_score(y_test, proba)
        aucs.append(auc)
        logger.info(f"  Fold {fold+1}: AUC={auc:.4f}")

    return aucs


def _train_final(X, y):
    from lightgbm import LGBMClassifier
    model = LGBMClassifier(
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=31,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X, y)
    return model


# =============================================================================
# MAIN RUN LOOP
# =============================================================================

def run(force: bool = False) -> None:

    # inbox wiring
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("MLLTExpRunner")
    except Exception:
        pass
    logger.info("ml_longterm_experiment_runner starting")
    now_str = datetime.now(timezone.utc).isoformat()

    queue = _load_queue()
    if not queue and not force:
        logger.info("Long-term experiment queue is empty -- nothing to run")
        return

    if force and not queue:
        # Inject a synthetic experiment so the runner has something to execute
        queue = [{
            "id":     f"lt-fundamental-retrain-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}",
            "title":  "Retrain with rd_regime_bull + insider + earnings_surprise + EDGAR XBRL fundamentals",
            "status": "queued",
        }]
        logger.info("  --force: injecting synthetic experiment entry")

    logger.info(f"Running {len(queue)} long-term experiments")

    # Load data — check if any experiment requests a specific feature set
    # If multiple experiments are queued, we load data fresh per experiment
    # (they may request different feature sets). For single-experiment queues,
    # this is the same as before.
    first_exp   = queue[0] if queue else {}
    feature_set = first_exp.get("feature_override", "LT_FEATURES")
    try:
        X, y, features_used, tickers = _load_longterm_data(feature_set=feature_set)
    except Exception as e:
        logger.error(f"Failed to load long-term data: {e}")
        return

    n_labeled = len(y)
    logger.info(f"Loaded {n_labeled} labeled rows, {len(features_used)} features: {features_used}")

    if n_labeled < MIN_LABELED_ROWS:
        logger.warning(
            f"Only {n_labeled} labeled rows, minimum is {MIN_LABELED_ROWS} -- skipping experiments"
        )
        return

    existing_results = _load_results()
    existing_ids     = {r["id"] for r in existing_results}
    # Load queue file data (may not exist if we injected a synthetic entry)
    if QUEUE_FILE.exists():
        queue_data = json.loads(QUEUE_FILE.read_text())
    else:
        queue_data = []

    newly_promoted   = 0

    for exp in queue:
        exp_id    = exp.get("id", "unknown")
        exp_title = exp.get("title", "")
        logger.info(f"Running LT experiment: {exp_id} -- {exp_title}")

        if exp_id in existing_ids and not force:
            logger.info(f"  Already have results for {exp_id} -- skipping")
            continue

        try:
            aucs     = _walk_forward_cv(X, y, n_splits=6)
            mean_auc = float(np.mean(aucs)) if aucs else 0.0
            promoted = mean_auc > PRODUCTION_BASELINE

            result = {
                "id":                exp_id,
                "title":             exp_title,
                "run_at":            now_str,
                "fold_aucs":         [round(a, 4) for a in aucs],
                "mean_auc":          round(mean_auc, 4),
                "baseline_auc":      PRODUCTION_BASELINE,
                "promoted":          promoted,
                "promotion_threshold": PRODUCTION_BASELINE,
                "n_folds":           len(aucs),
                "n_labeled":         n_labeled,
                "features_used":     features_used,
                "new_features":      ["rd_regime_bull", "earnings_surprise_flag",
                                      "insider_buy_dollars", "insider_is_ceo_cfo"] + LT_FEATURES_EDGAR,
            }

            if promoted:
                logger.info(f"  PROMOTED: mean AUC {mean_auc:.4f} > {PRODUCTION_BASELINE}")
                final_model = _train_final(X, y)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with open(MODEL_PATH, "wb") as f:
                    pickle.dump(final_model, f)
                # Write metadata
                meta = {
                    "trained_at":      now_str,
                    "n_labeled":       n_labeled,
                    "mean_cv_auc":     round(mean_auc, 4),
                    "fold_aucs":       [round(a, 4) for a in aucs],
                    "features":        features_used,
                    "n_features":      len(features_used),
                    "baseline_auc":    PRODUCTION_BASELINE,
                    "edgar_tickers":   len([t for t in set(tickers)]),
                    "source":          "ml_longterm_experiment_runner v2 (with EDGAR + regime + insider + earnings)",
                }
                with open(MODEL_META, "w") as f:
                    json.dump(meta, f, indent=2)
                result["model_saved"] = str(MODEL_PATH)
                newly_promoted += 1
                logger.info(f"  Model saved to {MODEL_PATH}")
                logger.info(f"  Metadata saved to {MODEL_META}")
            else:
                logger.info(f"  Not promoted: mean AUC {mean_auc:.4f} <= {PRODUCTION_BASELINE}")
                # Still save even if not above baseline (user specifically requested retrain)
                if force:
                    logger.info("  --force: saving model anyway (user-requested retrain)")
                    final_model = _train_final(X, y)
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    with open(MODEL_PATH, "wb") as f:
                        pickle.dump(final_model, f)
                    meta = {
                        "trained_at":   now_str,
                        "n_labeled":    n_labeled,
                        "mean_cv_auc":  round(mean_auc, 4),
                        "fold_aucs":    [round(a, 4) for a in aucs],
                        "features":     features_used,
                        "n_features":   len(features_used),
                        "baseline_auc": PRODUCTION_BASELINE,
                        "promoted":     False,
                        "force_saved":  True,
                        "source":       "ml_longterm_experiment_runner v2 (with EDGAR + regime + insider + earnings)",
                    }
                    with open(MODEL_META, "w") as f:
                        json.dump(meta, f, indent=2)
                    result["model_saved"]  = str(MODEL_PATH)
                    result["force_saved"]  = True

            existing_results.append(result)

            for entry in queue_data:
                if entry.get("id") == exp_id:
                    entry["status"]       = "completed"
                    entry["completed_at"] = now_str
                    entry["mean_auc"]     = round(mean_auc, 4)
                    entry["promoted"]     = promoted

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"  LT experiment {exp_id} failed: {e}\n{tb}")
            existing_results.append({
                "id":       exp_id,
                "title":    exp_title,
                "run_at":   now_str,
                "error":    str(e),
                "promoted": False,
            })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(existing_results, f, indent=2)

    if QUEUE_FILE.exists():
        with open(QUEUE_FILE, "w") as f:
            json.dump(queue_data, f, indent=2)

    logger.info(f"LT experiment runner done. Ran {len(queue)}, promoted {newly_promoted}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Run even if queue is empty; save model even if below baseline")
    args = parser.parse_args()
    run(force=args.force)


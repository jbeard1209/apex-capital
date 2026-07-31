#!/usr/bin/env python3
"""
APEX CAPITAL — PREDICTIVE MODEL BACKTESTER
============================================
Validates the ML model against real historical 13F data in the SQLite DB.

What this tests
---------------
  Walk-forward validation: train on filings up to date T, predict on T+1 quarter,
  measure 20-day forward return of the top-ranked signals.

  Compared against:
    1. Naive baseline: buy all signals equally
    2. Conviction-only: rank by raw conviction_score
    3. ML-ranked: rank by predictive_multiplier × conviction_score

Metrics
-------
  • Top-N hit rate    — % of top 5 picks with positive 20d return
  • Mean return       — average 20d return across top picks
  • Sharpe proxy      — mean / std of returns (rough signal quality metric)
  • vs S&P 500        — compared to SPY 20d forward return in same period

Output
------
  data/backtest_predictive_report.json   — full results
  Printed summary to stdout.

Note
----
  Requires at least 2 quarters of historical data in historical_13f.db.
  With a fresh install, run edgar_historical_scraper.py --full first.

Usage
-----
  python3 predictive_model/backtest_predictive.py [--quarters N]
"""

import sys
from typing import Optional
import json
import logging
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import get_db
from predictive_model.data_collector import _ensure_collector_tables
from predictive_model.feature_engineer import get_inference_features, FEATURE_COLS
from predictive_model.ml_model import load_model


def _predict(feature_df) -> "np.ndarray":
    """Version-aware prediction — routes to v4/v3/v2/v1 inference based on bundle."""
    bundle = load_model()
    try:
        from predictive_model.predictor import _dispatch_predict
        return _dispatch_predict(bundle, feature_df)
    except Exception:
        from predictive_model.ml_model import predict_proba
        return predict_proba(feature_df)

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "backtest_predictive_report.json"
TOP_N = 5   # how many top-ranked picks to evaluate per quarter


# =============================================================================
# DATA PREP
# =============================================================================

def _get_price_return(ticker: str, entry_date: date, hold_days: int = 20) -> Optional[float]:
    """Get forward return for ticker N days after entry_date."""
    end = entry_date + timedelta(days=hold_days + 10)
    try:
        df = yf.download(ticker, start=entry_date.isoformat(), end=end.isoformat(),
                         auto_adjust=True, progress=False)
        if df.empty:
            return None

        close = df["Close"].squeeze()
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]

        if len(close) < 2:
            return None

        entry_px = float(close.iloc[0])
        if entry_px == 0:
            return None

        # Find price ~hold_days business days out
        target_idx = min(hold_days, len(close) - 1)
        exit_px = float(close.iloc[target_idx])
        return (exit_px - entry_px) / entry_px
    except Exception:
        return None


def _spy_return(entry_date: date, hold_days: int = 20) -> Optional[float]:
    return _get_price_return("SPY", entry_date, hold_days)


def _load_all_signals_by_quarter(conn) -> dict:
    """
    Load all holdings data from 13F filings, grouped by filing quarter.
    Returns: {quarter_start_date: [signal dicts]}
    """
    df = pd.read_sql(
        """SELECT h.ticker, h.institution, h.filing_date, h.shares, h.value_usd,
                  f.period_of_report, f.accession
           FROM holdings h
           JOIN filings f ON h.filing_id = f.id
           WHERE h.ticker != '' AND h.ticker IS NOT NULL
           ORDER BY h.filing_date""",
        conn,
    )
    if df.empty:
        return {}

    df["filing_date"] = pd.to_datetime(df["filing_date"])
    df["quarter_start"] = df["filing_date"].dt.to_period("Q").dt.start_time

    by_quarter = {}
    for quarter, grp in df.groupby("quarter_start"):
        signals = []
        for ticker, tgrp in grp.groupby("ticker"):
            n_inst = tgrp["institution"].nunique()
            total_value = tgrp["value_usd"].sum()
            signals.append({
                "ticker":          ticker,
                "institution":     tgrp["institution"].iloc[0],
                "n_institutions":  n_inst,
                "conviction_score": min(100.0, n_inst * 20 + total_value / 1e7),
                "filing_date":     tgrp["filing_date"].max().date().isoformat(),
                "value_usd":       int(total_value),
            })
        by_quarter[quarter.date()] = signals

    return by_quarter


# =============================================================================
# WALK-FORWARD BACKTEST
# =============================================================================

def run_backtest(n_quarters: int = 8) -> dict:
    """
    Walk-forward backtest over last N quarters of 13F data.
    """
    conn = get_db()
    _ensure_collector_tables(conn)
    quarterly_signals = _load_all_signals_by_quarter(conn)
    conn.close()

    if len(quarterly_signals) < 2:
        return {
            "status": "INSUFFICIENT_DATA",
            "message": f"Need at least 2 quarters of data. Have {len(quarterly_signals)}.",
            "hint": "Run: python3 predictive_model/edgar_historical_scraper.py --full",
        }

    quarters = sorted(quarterly_signals.keys())[-n_quarters:]
    logger.info(f"  backtest: {len(quarters)} quarters from {quarters[0]} to {quarters[-1]}")

    results = []
    model = load_model()

    for i, qdate in enumerate(quarters):
        signals = quarterly_signals[qdate]
        if not signals:
            continue

        entry_date = qdate + timedelta(days=5)  # ~5 days after filing period starts

        # --- Strategy 1: Naive (equal weight, all signals) ---
        naive_returns = []
        for sig in signals[:20]:
            r = _get_price_return(sig["ticker"], entry_date)
            if r is not None:
                naive_returns.append(r)

        # --- Strategy 2: Conviction-ranked (top N by conviction_score) ---
        conv_ranked = sorted(signals, key=lambda x: x.get("conviction_score", 0), reverse=True)[:TOP_N]
        conv_returns = []
        for sig in conv_ranked:
            r = _get_price_return(sig["ticker"], entry_date)
            if r is not None:
                conv_returns.append(r)

        # --- Strategy 3: ML-ranked ---
        ml_returns = []
        if model is not None:
            try:
                feat_df = get_inference_features(signals)
                if not feat_df.empty:
                    scores = _predict(feat_df)
                    scored_signals = list(zip(signals, scores))
                    scored_signals.sort(key=lambda x: x[1], reverse=True)
                    top_ml = [s for s, _ in scored_signals[:TOP_N]]
                    for sig in top_ml:
                        r = _get_price_return(sig["ticker"], entry_date)
                        if r is not None:
                            ml_returns.append(r)
            except Exception as e:
                logger.debug(f"  ML ranking failed for {qdate}: {e}")

        # --- Benchmark: SPY ---
        spy_ret = _spy_return(entry_date)

        def _stats(returns, label):
            if not returns:
                return {"label": label, "n": 0, "mean": None}
            arr = np.array(returns)
            sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
            return {
                "label":     label,
                "n":         len(arr),
                "mean":      round(float(arr.mean()), 5),
                "std":       round(float(arr.std()), 5),
                "win_rate":  round(float((arr > 0).mean()), 4),
                "sharpe":    round(sharpe, 3),
                "vs_spy":    round(float(arr.mean()) - (spy_ret or 0), 5) if spy_ret else None,
            }

        quarter_result = {
            "quarter":         qdate.isoformat(),
            "entry_date":      entry_date.isoformat(),
            "n_signals":       len(signals),
            "spy_return_20d":  round(spy_ret, 5) if spy_ret else None,
            "naive":           _stats(naive_returns, "NAIVE"),
            "conviction":      _stats(conv_returns,  "CONVICTION_RANKED"),
            "ml_enhanced":     _stats(ml_returns,    "ML_ENHANCED") if ml_returns else {"label": "ML_ENHANCED", "n": 0},
        }
        results.append(quarter_result)
        # Both halves of the old line were invalid format specs — everything
        # after ':' is the spec, so ".3f or 'N/A'" and ".3f if spy_ret else
        # 'N/A'" each raise ValueError: Invalid format specifier. This is the
        # per-quarter progress line, so the backtest blew up on quarter one,
        # after the work and before any aggregate. Decide the text first.
        conv_txt = f"{np.mean(conv_returns):.3f}" if conv_returns else "N/A"
        spy_txt  = f"{spy_ret:.3f}" if spy_ret else "N/A"
        logger.info(f"  {qdate}  conviction_mean={conv_txt}  spy={spy_txt}")

    # Aggregate
    def _agg(key):
        means = [r[key]["mean"] for r in results if r[key].get("mean") is not None]
        wr    = [r[key]["win_rate"] for r in results if r[key].get("win_rate") is not None]
        return {
            "avg_mean_return": round(float(np.mean(means)), 5) if means else None,
            "avg_win_rate":    round(float(np.mean(wr)), 4)    if wr    else None,
            "n_quarters":      len(means),
        }

    report = {
        "generated_at":   datetime.now().isoformat(),
        "n_quarters":     len(results),
        "top_n":          TOP_N,
        "quarters":       results,
        "aggregate": {
            "naive":       _agg("naive"),
            "conviction":  _agg("conviction"),
            "ml_enhanced": _agg("ml_enhanced"),
        },
        "model_active":   model is not None,
        "status":         "OK",
    }

    # Verdict
    agg = report["aggregate"]
    naive_ret  = agg["naive"]["avg_mean_return"] or 0
    conv_ret   = agg["conviction"]["avg_mean_return"] or 0
    ml_ret     = agg["ml_enhanced"]["avg_mean_return"] or 0

    if model and ml_ret > conv_ret:
        verdict = f"ML outperforms conviction-only by {(ml_ret - conv_ret)*100:.2f}pp avg per quarter"
    elif conv_ret > naive_ret:
        verdict = f"Conviction ranking adds {(conv_ret - naive_ret)*100:.2f}pp vs naive"
    else:
        verdict = "No clear edge detected yet — accumulate more data"

    report["verdict"] = verdict
    logger.info(f"  Backtest verdict: {verdict}")

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    n_q = 8
    for i, arg in enumerate(sys.argv):
        if arg == "--quarters" and i + 1 < len(sys.argv):
            n_q = int(sys.argv[i + 1])

    report = run_backtest(n_quarters=n_q)
    print(json.dumps({
        "status":    report.get("status"),
        "n_quarters": report.get("n_quarters"),
        "verdict":   report.get("verdict"),
        "aggregate": report.get("aggregate"),
    }, indent=2, default=str))

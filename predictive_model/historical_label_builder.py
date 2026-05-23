#!/usr/bin/env python3
"""
APEX CAPITAL — HISTORICAL LABEL BUILDER
=========================================
One-time (and incremental) job that turns the 759 scraped 13F filings into
labeled training examples for the ML model.

What it does
------------
For each filing in the `filings` table:
  1. Downloads and parses the InfoTable XML to get stock holdings
  2. Looks up the stock price on the filing_date using yfinance
  3. Looks up the price 20 days later (1d and 5d too)
  4. Computes forward returns and risk-adjusted return
  5. Inserts a row into pipeline_snapshots with is_historical=1

This gives us 26 years of labeled data immediately rather than waiting
~50 trading days for the live pipeline to accumulate snapshots.

Expected output: 20,000–100,000 labeled rows depending on how many
holdings parse successfully.

Runtime: ~4–8 hours (SEC rate limit + yfinance calls). Run via nohup.

Progress is checkpointed — safe to interrupt and resume.

Usage
-----
  python3 predictive_model/historical_label_builder.py [--limit N] [--fund NAME]
  --limit N    only process N filings (for testing)
  --fund NAME  only process one fund
"""

import sys
import os
import sqlite3
import time
import logging
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import (
    get_db, parse_holdings_from_accession, resolve_ticker, _SEC_DELAY
)
from predictive_model.data_collector import _ensure_collector_tables

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("label_builder")

PROGRESS_PATH = Path(__file__).parent.parent / "data" / "label_builder_progress.json"

# Batch price fetching cache — avoid re-downloading same ticker/date combo
_price_cache: dict = {}


# =============================================================================
# SCHEMA MIGRATION
# =============================================================================

def _migrate_schema(conn: sqlite3.Connection):
    """Add new columns to pipeline_snapshots if they don't exist yet."""
    new_cols = [
        ("is_historical",        "INTEGER DEFAULT 0"),
        ("price_momentum_20d",   "REAL"),
        ("amendment_flag",       "INTEGER DEFAULT 0"),
        ("position_size_pct_aum","REAL"),
        ("sector_rotation_score","REAL"),
        ("days_since_ipo",       "INTEGER"),
        ("short_interest_pct",   "REAL"),
        ("price_vs_52w_high",    "REAL"),
        ("filing_quarter_consensus", "REAL"),
        ("insider_buy_flag",     "INTEGER DEFAULT 0"),
        ("insider_net_shares_30d","INTEGER"),
        ("risk_adjusted_20d",    "REAL"),
        ("risk_adjusted_5d",     "REAL"),
        ("volatility_20d",       "REAL"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
    for col_name, col_def in new_cols:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE pipeline_snapshots ADD COLUMN {col_name} {col_def}")
                logger.info(f"  Schema: added column {col_name}")
            except Exception as e:
                logger.debug(f"  Schema: {col_name} already exists or error: {e}")
    conn.commit()


# =============================================================================
# PRICE FETCHING
# =============================================================================

def _fetch_price_history(ticker: str, start: date, end: date):
    """Fetch and cache price history for a ticker over a date range."""
    key = f"{ticker}_{start}_{end}"
    if key in _price_cache:
        return _price_cache[key]

    try:
        import yfinance as yf
        import pandas as pd
        # Add buffer days to account for weekends/holidays
        end_buf = end + timedelta(days=10)
        df = yf.download(ticker, start=start.isoformat(), end=end_buf.isoformat(),
                         auto_adjust=True, progress=False)
        if df.empty:
            _price_cache[key] = None
            return None
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        _price_cache[key] = close
        return close
    except Exception:
        _price_cache[key] = None
        return None


def _get_price_on_date(ticker: str, target_date: date) -> Optional[float]:
    """Get closing price on or just after target_date."""
    end = target_date + timedelta(days=30)
    series = _fetch_price_history(ticker, target_date, end)
    if series is None or len(series) == 0:
        return None
    try:
        import pandas as pd
        # Find first trading day on or after target_date
        for offset in range(10):
            d = target_date + timedelta(days=offset)
            matches = series[series.index >= pd.Timestamp(d)]
            if len(matches) > 0:
                return float(matches.iloc[0])
    except Exception:
        pass
    return None


def _get_forward_return(ticker: str, entry_date: date, hold_days: int) -> Optional[float]:
    """Get forward return N trading days after entry_date."""
    end = entry_date + timedelta(days=hold_days + 15)
    series = _fetch_price_history(ticker, entry_date, end)
    if series is None or len(series) < 2:
        return None
    try:
        import pandas as pd
        entry_series = series[series.index >= pd.Timestamp(entry_date)]
        if len(entry_series) < 2:
            return None
        entry_px = float(entry_series.iloc[0])
        # Find price ~hold_days trading days out
        target_idx = min(hold_days, len(entry_series) - 1)
        exit_px = float(entry_series.iloc[target_idx])
        if entry_px == 0:
            return None
        return (exit_px - entry_px) / entry_px
    except Exception:
        return None


def _get_price_momentum(ticker: str, filing_date: date, lookback_days: int = 20) -> Optional[float]:
    """% price change in lookback_days before filing_date."""
    start = filing_date - timedelta(days=lookback_days + 10)
    series = _fetch_price_history(ticker, start, filing_date + timedelta(days=2))
    if series is None or len(series) < 2:
        return None
    try:
        import pandas as pd
        before = series[series.index <= pd.Timestamp(filing_date)]
        if len(before) < 2:
            return None
        old_px = float(before.iloc[-lookback_days]) if len(before) > lookback_days else float(before.iloc[0])
        new_px = float(before.iloc[-1])
        if old_px == 0:
            return None
        return (new_px - old_px) / old_px
    except Exception:
        return None


def _get_price_vs_52w_high(ticker: str, filing_date: date) -> Optional[float]:
    """How far current price is below its 52-week high (0=at high, 1=totally collapsed)."""
    start = filing_date - timedelta(days=365)
    series = _fetch_price_history(ticker, start, filing_date + timedelta(days=2))
    if series is None or len(series) < 5:
        return None
    try:
        import pandas as pd
        window = series[series.index <= pd.Timestamp(filing_date)]
        if len(window) < 5:
            return None
        high_52w = float(window.max())
        current  = float(window.iloc[-1])
        if high_52w == 0:
            return None
        return 1.0 - (current / high_52w)  # 0 = at 52w high, closer to 1 = far below
    except Exception:
        return None


def _get_volatility(ticker: str, filing_date: date, days: int = 20) -> Optional[float]:
    """Annualised volatility of daily returns over last N days."""
    start = filing_date - timedelta(days=days + 10)
    series = _fetch_price_history(ticker, start, filing_date + timedelta(days=2))
    if series is None or len(series) < 5:
        return None
    try:
        import pandas as pd
        import numpy as np
        window = series[series.index <= pd.Timestamp(filing_date)].tail(days)
        if len(window) < 5:
            return None
        daily_rets = window.pct_change().dropna()
        return float(daily_rets.std() * (252 ** 0.5))
    except Exception:
        return None


# =============================================================================
# PROGRESS TRACKING
# =============================================================================

def _load_progress() -> set:
    """Load set of already-processed accession numbers."""
    if PROGRESS_PATH.exists():
        try:
            with open(PROGRESS_PATH) as f:
                return set(json.load(f).get("done", []))
        except Exception:
            pass
    return set()


def _save_progress(done: set):
    PROGRESS_PATH.parent.mkdir(exist_ok=True)
    with open(PROGRESS_PATH, "w") as f:
        json.dump({"done": list(done), "updated": datetime.now().isoformat()}, f)


# =============================================================================
# CORE BUILDER
# =============================================================================

def process_filing(filing: sqlite3.Row, conn: sqlite3.Connection,
                   fund_aum_map: dict) -> int:
    """
    Parse holdings for one filing, compute labels, insert into pipeline_snapshots.
    Returns number of rows inserted.
    """
    institution = filing["institution"]
    cik         = filing["cik"]
    accession   = filing["accession"]
    filing_date_str = filing["filing_date"]
    form        = filing["form"]

    try:
        filing_date = date.fromisoformat(filing_date_str)
    except Exception:
        return 0

    # Parse holdings from XML
    holdings = parse_holdings_from_accession(cik, accession)
    if not holdings:
        return 0

    total_value = sum(h.get("value_usd", 0) for h in holdings)
    fund_aum_map[institution] = max(fund_aum_map.get(institution, 0), total_value)

    amendment_flag = 1 if form == "13F-HR/A" else 0
    inserted = 0
    now_ts = datetime.now().isoformat()

    for h in holdings:
        # parse_holdings_from_accession returns name+cusip but no ticker — resolve it
        ticker = h.get("ticker", "")
        if not ticker:
            ticker = resolve_ticker(h.get("cusip", ""), h.get("name", ""), conn)
        cusip  = h.get("cusip", "")
        if not ticker or len(ticker) > 6:
            continue

        # Skip if already have a historical snapshot for this ticker on this date
        exists = conn.execute(
            "SELECT id FROM pipeline_snapshots WHERE snapshot_date=? AND ticker=? AND is_historical=1",
            (filing_date_str, ticker)
        ).fetchone()
        if exists:
            continue

        shares    = h.get("shares", 0)
        value_usd = h.get("value_usd", 0)
        total_aum = fund_aum_map.get(institution, 1) or 1
        pos_pct   = value_usd / total_aum if total_aum > 0 else 0.0

        # Price features
        entry_px     = _get_price_on_date(ticker, filing_date)
        momentum_20d = _get_price_momentum(ticker, filing_date)
        vs_52w_high  = _get_price_vs_52w_high(ticker, filing_date)
        vol_20d      = _get_volatility(ticker, filing_date)

        # Forward returns
        ret_1d  = _get_forward_return(ticker, filing_date, 1)
        ret_5d  = _get_forward_return(ticker, filing_date, 5)
        ret_20d = _get_forward_return(ticker, filing_date, 20)

        # Risk-adjusted returns
        risk_adj_20d = None
        risk_adj_5d  = None
        if ret_20d is not None and vol_20d and vol_20d > 0:
            risk_adj_20d = ret_20d / (vol_20d / (252 ** 0.5) * (20 ** 0.5))
        if ret_5d is not None and vol_20d and vol_20d > 0:
            risk_adj_5d = ret_5d / (vol_20d / (252 ** 0.5) * (5 ** 0.5))

        # Conviction score proxy (same formula as backtest_predictive)
        n_inst_same_quarter = conn.execute(
            """SELECT COUNT(DISTINCT h2.institution)
               FROM holdings h2
               JOIN filings f2 ON h2.filing_id = f2.id
               WHERE h2.ticker = ? AND f2.filing_date BETWEEN ? AND ?""",
            (ticker,
             (filing_date - timedelta(days=95)).isoformat(),
             (filing_date + timedelta(days=5)).isoformat())
        ).fetchone()[0] or 1

        conviction = min(100.0, n_inst_same_quarter * 20 + value_usd / 1e7)

        # Days since filing quarter end
        try:
            period = filing.get("period_of_report", "") or ""
            if period:
                period_date = date.fromisoformat(period)
                days_since = (filing_date - period_date).days
            else:
                days_since = 45
        except Exception:
            days_since = 45

        try:
            conn.execute(
                """INSERT OR IGNORE INTO pipeline_snapshots
                   (snapshot_date, snapshot_ts, ticker, institution, conviction_score,
                    n_institutions, signal_type, days_since_filing, herding_flag,
                    quant_consensus, portfolio_weight, entry_price,
                    outcome_1d, outcome_5d, outcome_20d, outcome_filled,
                    is_historical, price_momentum_20d, amendment_flag,
                    position_size_pct_aum, price_vs_52w_high,
                    risk_adjusted_20d, risk_adjusted_5d, volatility_20d)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    filing_date_str, now_ts, ticker, institution, conviction,
                    n_inst_same_quarter, "HISTORICAL_13F", days_since,
                    0, 0,  # herding_flag, quant_consensus — computed in feature_engineer
                    pos_pct, entry_px,
                    ret_1d, ret_5d, ret_20d,
                    1 if ret_20d is not None else 0,  # outcome_filled
                    1,  # is_historical
                    momentum_20d, amendment_flag,
                    pos_pct, vs_52w_high,
                    risk_adj_20d, risk_adj_5d, vol_20d,
                )
            )
            inserted += 1
        except Exception as e:
            logger.debug(f"  Insert error {ticker} {filing_date_str}: {e}")

        time.sleep(0.05)  # gentle yfinance rate limit

    if inserted:
        conn.commit()

    return inserted


def run_label_builder(limit: int = None, fund_filter: str = None) -> dict:
    logger.info("=" * 60)
    logger.info("  HISTORICAL LABEL BUILDER")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    conn = get_db()
    _ensure_collector_tables(conn)
    _migrate_schema(conn)

    # Load all filings from DB
    query = "SELECT * FROM filings ORDER BY filing_date DESC"
    params = []
    if fund_filter:
        query = "SELECT * FROM filings WHERE institution LIKE ? ORDER BY filing_date DESC"
        params = [f"%{fund_filter}%"]

    filings = conn.execute(query, params).fetchall()
    if limit:
        filings = filings[:limit]

    logger.info(f"  {len(filings)} filing(s) to process")

    done = _load_progress()
    fund_aum_map: dict = {}
    total_inserted = 0
    total_filings_processed = 0

    for i, filing in enumerate(filings):
        acc = filing["accession"]
        if acc in done:
            continue

        inst = filing["institution"]
        fdate = filing["filing_date"]
        logger.info(f"  [{i+1}/{len(filings)}] {inst}  {fdate}  {acc[:20]}...")

        try:
            n = process_filing(filing, conn, fund_aum_map)
            total_inserted += n
            total_filings_processed += 1
            logger.info(f"    → {n} holdings labeled")
        except Exception as e:
            logger.warning(f"    Filing error: {e}")

        done.add(acc)

        # Checkpoint every 10 filings
        if len(done) % 10 == 0:
            _save_progress(done)
            readiness = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL AND is_historical=1"
            ).fetchone()[0]
            logger.info(f"  Checkpoint: {total_inserted} rows, {readiness} labeled")

    _save_progress(done)
    conn.close()

    summary = {
        "completed_at":       datetime.now().isoformat(),
        "filings_processed":  total_filings_processed,
        "rows_inserted":      total_inserted,
        "status":             "DONE",
    }
    logger.info(f"\n  Done — {total_inserted} labeled rows inserted from {total_filings_processed} filings")
    return summary


def get_label_builder_status() -> dict:
    """Quick status check for monitoring."""
    conn = get_db()
    _ensure_collector_tables(conn)
    _migrate_schema(conn)

    historical = conn.execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE is_historical=1"
    ).fetchone()[0]
    labeled = conn.execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL AND is_historical=1"
    ).fetchone()[0]
    live = conn.execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE is_historical=0"
    ).fetchone()[0]
    conn.close()

    done = _load_progress()
    return {
        "filings_processed": len(done),
        "historical_rows":   historical,
        "labeled_rows":      labeled,
        "live_rows":         live,
        "total_rows":        historical + live,
        "ready_to_train":    labeled >= 50,
    }


if __name__ == "__main__":
    import sys
    limit_arg  = None
    fund_arg   = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit_arg = int(sys.argv[i + 1])
        if arg == "--fund" and i + 1 < len(sys.argv):
            fund_arg = sys.argv[i + 1]

    result = run_label_builder(limit=limit_arg, fund_filter=fund_arg)
    print(json.dumps(result, indent=2, default=str))

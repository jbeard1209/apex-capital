#!/usr/bin/env python3
"""
APEX CAPITAL — PREDICTIVE MODEL DATA COLLECTOR
================================================
Runs after every main.py pipeline execution and logs a snapshot of:
  - Which signals fired (institution, ticker, conviction_score)
  - Current portfolio positions
  - Subsequent price outcomes (filled in on next run, 1/5/20 days later)

This is the real-time training data pipeline. Each run appends one
snapshot record. Over time this accumulates the ground-truth data needed
to train the ML model on what actually works.

Storage: data/historical_13f.db  (table: pipeline_snapshots, price_outcomes)

Usage
-----
  Called automatically at end of main.py:
      from predictive_model.data_collector import collect_snapshot
      collect_snapshot()

  Can also run standalone:
      python3 predictive_model/data_collector.py
"""

import sys
from typing import Optional
import os
import json
import sqlite3
import logging
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import get_db

logger = logging.getLogger(__name__)

DATA_DIR    = Path(__file__).parent.parent / "data"
SIGNALS_PATH = DATA_DIR / "signals.json"
POSITIONS_PATH = DATA_DIR / "positions.json"


# =============================================================================
# SCHEMA EXTENSION (added on top of edgar_historical_scraper schema)
# =============================================================================

def _ensure_collector_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date   TEXT    NOT NULL,
            snapshot_ts     TEXT    NOT NULL,
            ticker          TEXT    NOT NULL,
            institution     TEXT,
            conviction_score REAL,
            n_institutions  INTEGER,
            signal_type     TEXT,
            days_since_filing INTEGER,
            herding_flag    INTEGER DEFAULT 0,
            quant_consensus INTEGER DEFAULT 0,
            portfolio_weight REAL,
            entry_price     REAL,
            outcome_1d      REAL,
            outcome_5d      REAL,
            outcome_20d     REAL,
            outcome_filled  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS price_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id     INTEGER REFERENCES pipeline_snapshots(id),
            ticker          TEXT    NOT NULL,
            entry_date      TEXT    NOT NULL,
            entry_price     REAL,
            price_1d        REAL,
            price_5d        REAL,
            price_20d       REAL,
            return_1d       REAL,
            return_5d       REAL,
            return_20d      REAL,
            filled_at       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON pipeline_snapshots(ticker);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date   ON pipeline_snapshots(snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_outcomes_ticker  ON price_outcomes(ticker);
    """)
    conn.commit()


# =============================================================================
# SNAPSHOT COLLECTION
# =============================================================================

def _load_json(path: Path) -> object:
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _get_current_price(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2d", auto_adjust=True, progress=False)
        if df.empty:
            return None
        close = df["Close"]
        if hasattr(close, "iloc"):
            val = close.iloc[-1]
            if hasattr(val, "item"):
                return float(val.item())
            return float(val)
    except Exception:
        pass
    return None


def collect_snapshot() -> int:
    """
    Log current pipeline signals as a snapshot record.
    Returns number of rows inserted.
    """
    conn = get_db()
    _ensure_collector_tables(conn)

    today = date.today().isoformat()
    now   = datetime.now().isoformat()

    signals = _load_json(SIGNALS_PATH)
    if isinstance(signals, dict):
        signals = signals.get("signals", [])
    if not signals:
        logger.info("  data_collector: no signals to snapshot today")
        conn.close()
        return 0

    # Load position weights if available
    positions = _load_json(POSITIONS_PATH)
    weight_map = {}
    if isinstance(positions, list):
        for pos in positions:
            if isinstance(pos, dict):
                weight_map[pos.get("symbol", "")] = pos.get("market_value_pct", 0.0)

    inserted = 0
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        ticker = sig.get("ticker", "")
        if not ticker:
            continue

        # Check if we already snapshotted this ticker today
        existing = conn.execute(
            "SELECT id FROM pipeline_snapshots WHERE snapshot_date = ? AND ticker = ?",
            (today, ticker)
        ).fetchone()
        if existing:
            continue

        # Compute days_since_filing
        filing_date_str = sig.get("filing_date", "")
        days_since = None
        if filing_date_str:
            try:
                fd = date.fromisoformat(filing_date_str)
                days_since = (date.today() - fd).days
            except Exception:
                pass

        entry_price = _get_current_price(ticker)

        conn.execute(
            """INSERT INTO pipeline_snapshots
               (snapshot_date, snapshot_ts, ticker, institution, conviction_score,
                n_institutions, signal_type, days_since_filing, herding_flag,
                quant_consensus, portfolio_weight, entry_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                today, now, ticker,
                sig.get("institution", ""),
                sig.get("conviction_score", sig.get("score", 0.0)),
                sig.get("n_institutions", 1),
                sig.get("signal_type", sig.get("type", "")),
                days_since,
                1 if sig.get("herding", False) else 0,
                1 if sig.get("quant_consensus", False) else 0,
                weight_map.get(ticker, 0.0),
                entry_price,
            )
        )
        inserted += 1

    conn.commit()

    # Fill in price outcomes for old snapshots
    filled = _fill_price_outcomes(conn)
    conn.close()

    logger.info(f"  data_collector: {inserted} new snapshot(s), {filled} outcome(s) filled")
    return inserted


def _fill_price_outcomes(conn: sqlite3.Connection) -> int:
    """Fill price outcome columns for snapshots old enough to have 1/5/20-day data."""
    today = date.today()
    cutoff_1d  = (today - timedelta(days=1)).isoformat()
    cutoff_5d  = (today - timedelta(days=5)).isoformat()
    cutoff_20d = (today - timedelta(days=20)).isoformat()

    unfilled = conn.execute(
        "SELECT id, ticker, snapshot_date, entry_price FROM pipeline_snapshots WHERE outcome_filled = 0"
    ).fetchall()

    filled = 0
    tickers_to_fetch = set()
    eligible = []

    for row in unfilled:
        snap_date = row["snapshot_date"]
        if snap_date <= cutoff_1d:
            eligible.append(row)
            tickers_to_fetch.add(row["ticker"])

    if not tickers_to_fetch:
        return 0

    # Batch fetch prices
    price_cache = {}
    for ticker in tickers_to_fetch:
        try:
            import yfinance as yf
            df = yf.download(ticker, period="30d", auto_adjust=True, progress=False)
            if not df.empty:
                price_cache[ticker] = df["Close"]
                if hasattr(price_cache[ticker], "squeeze"):
                    price_cache[ticker] = price_cache[ticker].squeeze()
        except Exception:
            pass

    for row in eligible:
        snap_id    = row["id"]
        ticker     = row["ticker"]
        snap_date  = date.fromisoformat(row["snapshot_date"])
        entry_px   = row["entry_price"]

        if ticker not in price_cache or entry_px is None or entry_px == 0:
            continue

        series = price_cache[ticker]

        def _price_on_or_after(target: date) -> Optional[float]:
            for offset in range(5):
                d = target + timedelta(days=offset)
                try:
                    idx = d.isoformat()
                    if idx in series.index.astype(str):
                        val = series[series.index.astype(str) == idx].iloc[0]
                        return float(val)
                except Exception:
                    pass
            return None

        p1  = _price_on_or_after(snap_date + timedelta(days=1))
        p5  = _price_on_or_after(snap_date + timedelta(days=5))
        p20 = _price_on_or_after(snap_date + timedelta(days=20))

        if snap_date > date.fromisoformat(cutoff_20d):
            p20 = None
        if snap_date > date.fromisoformat(cutoff_5d):
            p5 = None

        def _ret(p):
            if p and entry_px:
                return round((p - entry_px) / entry_px, 6)
            return None

        conn.execute(
            """UPDATE pipeline_snapshots
               SET outcome_1d=?, outcome_5d=?, outcome_20d=?,
                   outcome_filled=?
               WHERE id=?""",
            (_ret(p1), _ret(p5), _ret(p20),
             1 if (p1 is not None) else 0,
             snap_id)
        )
        filled += 1

    conn.commit()
    return filled


def get_training_readiness() -> dict:
    """Return how many labeled training rows we have — used by ml_model.py to decide if training is viable."""
    conn = get_db()
    _ensure_collector_tables(conn)

    total = conn.execute("SELECT COUNT(*) FROM pipeline_snapshots").fetchone()[0]
    labeled_1d  = conn.execute("SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_1d IS NOT NULL").fetchone()[0]
    labeled_20d = conn.execute("SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL").fetchone()[0]
    conn.close()

    return {
        "total_snapshots": total,
        "labeled_1d":      labeled_1d,
        "labeled_20d":     labeled_20d,
        "ready_to_train":  labeled_20d >= 50,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    n = collect_snapshot()
    readiness = get_training_readiness()
    print(f"Snapshots inserted: {n}")
    print(f"Training readiness: {readiness}")

#!/usr/bin/env python3
"""
price_feature_backfill.py

Backfills price-derived features in pipeline_snapshots:
  - price_momentum_20d
  - price_momentum_5d
  - volatility_20d
  - price_vs_52w_high

Reads all tickers with NULL features, downloads price history via yfinance
in batches of 50, computes features per row, and updates in batches of 1000.

Usage:
    python3 price_feature_backfill.py [--dry-run]
"""

import argparse
import logging
import sqlite3
import sys
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path("/home/jackbeard/institutional-tracker")
DB_PATH    = BASE_DIR / "data" / "historical_13f.db"
CACHE_PATH = BASE_DIR / "data" / "price_cache.pkl"

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_START  = "2012-01-01"
DOWNLOAD_END    = datetime.today().strftime("%Y-%m-%d")
TICKER_BATCH    = 50    # tickers per yf.download() call
DB_BATCH        = 1000  # rows per executemany
LOG_EVERY_ROWS  = 500

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("price_backfill")


# ── Price-feature helpers ─────────────────────────────────────────────────────

def _closest_price(series: pd.Series, target_date: pd.Timestamp,
                   window: int, max_slop: int = 5) -> float | None:
    """
    Return the closing price on or just before `target_date - window calendar days`.
    Looks up to max_slop trading days earlier if the exact date is missing.
    Returns None if no price is found within the tolerance.
    """
    ref = target_date - timedelta(days=window)
    # Search from ref backwards up to max_slop trading days
    for delta in range(0, max_slop + 1):
        d = ref - timedelta(days=delta)
        if d in series.index:
            return float(series.loc[d])
    return None


def compute_features(series: pd.Series, snap_date_str: str) -> dict | None:
    """
    Given a Close price Series (DatetimeIndex) and a snapshot date string,
    return a dict with the four price features, or None if insufficient data.
    """
    try:
        snap_date = pd.Timestamp(snap_date_str)
    except Exception:
        return None

    if series.empty:
        return None

    # Price at snapshot date (allow up to 5 trading days slop)
    price_now = None
    for delta in range(0, 6):
        d = snap_date - timedelta(days=delta)
        if d in series.index:
            price_now = float(series.loc[d])
            break
    if price_now is None or price_now <= 0:
        return None

    # ── price_momentum_20d: 28 calendar days back ─────────────────────────────
    price_28d_ago = _closest_price(series, snap_date, 28)
    momentum_20d  = ((price_now / price_28d_ago) - 1.0) if price_28d_ago else None

    # ── price_momentum_5d: 7 calendar days back ───────────────────────────────
    price_7d_ago = _closest_price(series, snap_date, 7)
    momentum_5d  = ((price_now / price_7d_ago) - 1.0) if price_7d_ago else None

    # ── volatility_20d: annualised std of 30-day daily returns ───────────────
    vol_20d = None
    end_idx   = snap_date
    start_idx = snap_date - timedelta(days=35)  # a bit extra for weekends
    window_prices = series.loc[(series.index >= start_idx) &
                                (series.index <= end_idx)]
    if len(window_prices) >= 5:
        daily_returns = window_prices.pct_change().dropna()
        if len(daily_returns) >= 4:
            vol_20d = float(daily_returns.std() * np.sqrt(252))

    # ── price_vs_52w_high: (price - 52w_high) / 52w_high ─────────────────────
    price_vs_52w = None
    w52_start = snap_date - timedelta(days=365)
    w52_prices = series.loc[(series.index >= w52_start) &
                             (series.index <= snap_date)]
    if not w52_prices.empty:
        high_52w = float(w52_prices.max())
        if high_52w > 0:
            price_vs_52w = (price_now - high_52w) / high_52w

    return {
        "price_momentum_20d": momentum_20d,
        "price_momentum_5d":  momentum_5d,
        "volatility_20d":     vol_20d,
        "price_vs_52w_high":  price_vs_52w,
    }


# ── Download helpers ──────────────────────────────────────────────────────────

def download_batch(tickers: list[str]) -> dict[str, pd.Series]:
    """
    Download Close prices for a list of tickers in one yf.download() call.
    Returns {ticker: pd.Series(Close, DatetimeIndex)}.
    Handles MultiIndex columns from multi-ticker downloads.
    Skips tickers that fail or return empty data.
    """
    result: dict[str, pd.Series] = {}
    if not tickers:
        return result

    try:
        raw = yf.download(
            tickers,
            start=DOWNLOAD_START,
            end=DOWNLOAD_END,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        log.warning("yf.download failed for batch of %d tickers: %s", len(tickers), exc)
        return result

    if raw.empty:
        log.warning("Empty download for batch: %s …", tickers[:5])
        return result

    if raw.columns.nlevels == 2:
        # MultiIndex: ('Close', 'TICKER')
        if "Close" not in raw.columns.get_level_values(0):
            log.warning("No 'Close' in multi-ticker download columns.")
            return result
        close_df = raw["Close"]
        for ticker in tickers:
            if ticker not in close_df.columns:
                continue
            s = close_df[ticker].dropna()
            if not s.empty:
                result[ticker] = s
    else:
        # Single ticker — raw is a flat DataFrame
        if "Close" not in raw.columns:
            return result
        ticker = tickers[0]
        s = raw["Close"].dropna()
        if not s.empty:
            result[ticker] = s

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill price features in pipeline_snapshots")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute features but do not write to DB")
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        log.info("DRY-RUN mode — no DB writes will occur.")

    # ── Load existing price cache ─────────────────────────────────────────────
    price_cache: dict[str, pd.Series] = {}
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "rb") as fh:
                price_cache = pickle.load(fh)
            log.info("Loaded existing price_cache.pkl with %d tickers.", len(price_cache))
        except Exception as exc:
            log.warning("Could not load price_cache.pkl: %s — starting fresh.", exc)

    # ── Connect to DB ─────────────────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()

    # ── Fetch all rows needing backfill ───────────────────────────────────────
    log.info("Querying rows with NULL price_momentum_20d …")
    cur.execute("""
        SELECT id, ticker, snapshot_date
        FROM pipeline_snapshots
        WHERE price_momentum_20d IS NULL
        ORDER BY ticker, snapshot_date
    """)
    rows = cur.fetchall()
    total_null = len(rows)
    log.info("Found %d rows to backfill across all tickers.", total_null)

    if total_null == 0:
        log.info("Nothing to do — all rows already have price features.")
        conn.close()
        return

    # Group rows by ticker
    from collections import defaultdict
    ticker_rows: dict[str, list[tuple]] = defaultdict(list)
    for row_id, ticker, snap_date in rows:
        ticker_rows[ticker].append((row_id, snap_date))

    all_tickers = sorted(ticker_rows.keys())
    log.info("Distinct tickers needing backfill: %d", len(all_tickers))
    log.info("Downloading price history from %s to %s in batches of %d …",
             DOWNLOAD_START, DOWNLOAD_END, TICKER_BATCH)

    total_updated  = 0
    total_skipped  = 0
    pending_writes: list[tuple] = []   # (mom20d, mom5d, vol20d, vs52w, row_id)

    def flush_writes() -> None:
        nonlocal total_updated
        if dry_run or not pending_writes:
            if dry_run and pending_writes:
                log.info("[DRY-RUN] Would update %d rows (sample: id=%s mom20d=%.4f)",
                         len(pending_writes), pending_writes[0][4], pending_writes[0][0] or 0)
            pending_writes.clear()
            return
        conn.executemany("""
            UPDATE pipeline_snapshots
            SET price_momentum_20d = ?,
                price_momentum_5d  = ?,
                volatility_20d     = ?,
                price_vs_52w_high  = ?
            WHERE id = ?
        """, pending_writes)
        conn.commit()
        total_updated += len(pending_writes)
        pending_writes.clear()

    # ── Process tickers in batches ────────────────────────────────────────────
    for batch_start in range(0, len(all_tickers), TICKER_BATCH):
        batch_tickers = all_tickers[batch_start: batch_start + TICKER_BATCH]

        # Separate tickers already in cache vs needing download
        need_download = [t for t in batch_tickers if t not in price_cache]
        log.info("Batch %d–%d: %d tickers (%d to download, %d from cache)",
                 batch_start + 1,
                 min(batch_start + TICKER_BATCH, len(all_tickers)),
                 len(batch_tickers),
                 len(need_download),
                 len(batch_tickers) - len(need_download))

        # Download missing tickers
        if need_download:
            downloaded = download_batch(need_download)
            price_cache.update(downloaded)
            log.info("  Downloaded %d/%d tickers successfully.",
                     len(downloaded), len(need_download))

        # Compute features for all rows belonging to this batch's tickers
        batch_row_count = 0
        for ticker in batch_tickers:
            series = price_cache.get(ticker)
            if series is None or series.empty:
                skipped = len(ticker_rows[ticker])
                log.debug("  Skipping %s — no price data (%d rows).", ticker, skipped)
                total_skipped += skipped
                continue

            for row_id, snap_date in ticker_rows[ticker]:
                feats = compute_features(series, snap_date)
                if feats is None:
                    total_skipped += 1
                    continue

                pending_writes.append((
                    feats["price_momentum_20d"],
                    feats["price_momentum_5d"],
                    feats["volatility_20d"],
                    feats["price_vs_52w_high"],
                    row_id,
                ))
                batch_row_count += 1

                if len(pending_writes) >= DB_BATCH:
                    flush_writes()
                    if total_updated % LOG_EVERY_ROWS < DB_BATCH:
                        log.info("  Progress: %d rows updated so far, %d skipped.",
                                 total_updated, total_skipped)

        log.info("  Batch done — %d feature rows queued/written for this batch.",
                 batch_row_count)

    # Flush any remaining writes
    if pending_writes:
        flush_writes()

    log.info("─" * 60)
    log.info("Backfill complete.")
    log.info("  Total rows updated : %d", total_updated)
    log.info("  Total rows skipped : %d", total_skipped)
    log.info("  Total NULL rows    : %d", total_null)

    # ── Verify ────────────────────────────────────────────────────────────────
    if not dry_run:
        cur.execute("SELECT COUNT(*) FROM pipeline_snapshots WHERE price_momentum_20d IS NULL")
        remaining = cur.fetchone()[0]
        log.info("  Remaining NULLs    : %d", remaining)

        # Save updated cache
        try:
            with open(CACHE_PATH, "wb") as fh:
                pickle.dump(price_cache, fh)
            log.info("  price_cache.pkl updated with %d tickers.", len(price_cache))
        except Exception as exc:
            log.warning("Could not save price_cache.pkl: %s", exc)

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()

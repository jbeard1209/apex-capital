"""
build_365d_labels.py — Backfill outcome_365d into pipeline_snapshots.

For each snapshot row, fetches the stock price 365 days after snapshot_date
and computes: outcome_365d = (price_365d - entry_price) / entry_price

Uses yfinance. Batches by ticker to minimize API calls.
Skips rows where snapshot_date > today - 365 days (future label unavailable).
Writes progress to logs/build_365d_labels.log
"""
import sqlite3, json, logging, sys, time
from datetime import datetime, timedelta
from pathlib import Path

BASE    = Path('/home/jackbeard/institutional-tracker')
DB_PATH = BASE / 'data' / 'historical_13f.db'
LOG_DIR = BASE / 'logs'
LOG_DIR.mkdir(exist_ok=True)

fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('label365')
log.setLevel(logging.INFO)
for h in [logging.FileHandler(LOG_DIR / 'build_365d_labels.log'),
          logging.StreamHandler(sys.stdout)]:
    h.setFormatter(fmt)
    log.addHandler(h)

import yfinance as yf
import pandas as pd

CUTOFF = datetime.today() - timedelta(days=365)  # can only label snapshots before this date

def main():
    log.info('='*60)
    log.info('BUILD outcome_365d LABELS')
    log.info(f'Label cutoff date: {CUTOFF.date()} (snapshots after this cannot be labeled yet)')
    log.info('='*60)

    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")

    # Add column if missing
    cols = [r[1] for r in con.execute("PRAGMA table_info(pipeline_snapshots)").fetchall()]
    if 'outcome_365d' not in cols:
        con.execute("ALTER TABLE pipeline_snapshots ADD COLUMN outcome_365d REAL")
        con.commit()
        log.info("Added outcome_365d column.")
    else:
        log.info("outcome_365d column already exists.")

    # Count eligible rows
    eligible = con.execute(
        "SELECT COUNT(*) FROM pipeline_snapshots WHERE snapshot_date <= ? AND outcome_365d IS NULL",
        (CUTOFF.strftime('%Y-%m-%d'),)
    ).fetchone()[0]
    log.info(f"Eligible rows to label: {eligible:,}")

    # Get distinct tickers with eligible snapshots
    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM pipeline_snapshots WHERE snapshot_date <= ? AND outcome_365d IS NULL",
        (CUTOFF.strftime('%Y-%m-%d'),)
    ).fetchall()]
    log.info(f"Distinct tickers: {len(tickers)}")

    labeled_total = 0
    failed_tickers = []

    for i, ticker in enumerate(tickers):
        try:
            # Get all snapshot dates + entry prices for this ticker
            rows = con.execute(
                """SELECT rowid, snapshot_date, entry_price FROM pipeline_snapshots
                   WHERE ticker=? AND snapshot_date <= ? AND outcome_365d IS NULL""",
                (ticker, CUTOFF.strftime('%Y-%m-%d'))
            ).fetchall()

            if not rows:
                continue

            dates = [r[1] for r in rows]
            min_date = min(dates)
            max_date_365 = (datetime.strptime(max(dates), '%Y-%m-%d') + timedelta(days=400)).strftime('%Y-%m-%d')

            # Fetch full price history for this ticker
            hist = yf.download(ticker, start=min_date, end=max_date_365,
                               progress=False, auto_adjust=True)
            if hist.empty:
                failed_tickers.append(ticker)
                continue

            prices = hist['Close'].squeeze()
            prices.index = pd.to_datetime(prices.index).normalize()

            updates = []
            for rowid, snap_date, entry_price in rows:
                target_date = pd.Timestamp(snap_date) + timedelta(days=365)
                # Find nearest trading day at or after target
                future = prices[prices.index >= target_date]
                if future.empty:
                    continue
                price_365d = float(future.iloc[0])

                # Use entry_price if available, else use price on snap_date
                if entry_price and entry_price > 0:
                    base_price = float(entry_price)
                else:
                    snap_prices = prices[prices.index <= pd.Timestamp(snap_date)]
                    if snap_prices.empty:
                        continue
                    base_price = float(snap_prices.iloc[-1])

                outcome = (price_365d - base_price) / base_price
                updates.append((outcome, rowid))

            if updates:
                con.executemany("UPDATE pipeline_snapshots SET outcome_365d=? WHERE rowid=?", updates)
                con.commit()
                labeled_total += len(updates)

            if (i+1) % 20 == 0:
                log.info(f"  Progress: {i+1}/{len(tickers)} tickers | {labeled_total:,} rows labeled so far")

            time.sleep(0.1)  # be gentle with yfinance

        except Exception as e:
            log.warning(f"  {ticker}: {e}")
            failed_tickers.append(ticker)
            continue

    # Final count
    final_count = con.execute("SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_365d IS NOT NULL").fetchone()[0]
    pos_rate = con.execute("SELECT AVG(outcome_365d > 0) FROM pipeline_snapshots WHERE outcome_365d IS NOT NULL").fetchone()[0]

    log.info('='*60)
    log.info(f'DONE. Rows labeled: {labeled_total:,}')
    log.info(f'Total outcome_365d non-null: {final_count:,}')
    log.info(f'Positive rate (went up over 1yr): {pos_rate:.1%}')
    log.info(f'Failed tickers: {len(failed_tickers)} — {failed_tickers[:10]}')
    log.info('='*60)

    report = {
        'run_at': datetime.now().isoformat(),
        'eligible_rows': eligible,
        'labeled': labeled_total,
        'total_labeled': final_count,
        'positive_rate': pos_rate,
        'failed_tickers': failed_tickers,
        'cutoff_date': CUTOFF.strftime('%Y-%m-%d'),
    }
    json.dump(report, open(BASE / 'data' / 'build_365d_report.json', 'w'), indent=2)
    con.close()

if __name__ == '__main__':
    main()

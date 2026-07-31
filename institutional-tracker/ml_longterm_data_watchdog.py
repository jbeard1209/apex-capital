#!/usr/bin/env python3
"""
APEX CAPITAL -- ML LONG-TERM DATA WATCHDOG
==========================================
Reports on outcome_252d data quality:
  - Total labeled rows
  - % coverage (labeled / total snapshots)
  - Tickers with NULL outcome_252d despite old snapshot dates (stale, should be labeled by now)
  - Last backfill date

Writes data/ml_longterm_data_quality_report.json.

Run: python3 ml_longterm_data_watchdog.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
LOGS_DIR    = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH     = DATA_DIR / "historical_13f.db"
REPORT_PATH = DATA_DIR / "ml_longterm_data_quality_report.json"

# Snapshots older than this many days should have outcome_252d filled in
STALE_THRESHOLD_DAYS = 400

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_longterm_data_watchdog")


def run() -> dict:
    logger.info("ml_longterm_data_watchdog starting")

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }

    if not DB_PATH.exists():
        report["status"] = "skipped"
        report["reason"] = "Database not found"
        logger.warning("Database not found")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        return report

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
            if "outcome_252d" not in cols:
                report["status"] = "skipped"
                report["reason"] = "outcome_252d column not present"
                logger.warning("outcome_252d column missing")
                with open(REPORT_PATH, "w") as f:
                    json.dump(report, f, indent=2)
                return report

            # Total rows
            total_row = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots"
            ).fetchone()
            total_snapshots = total_row[0] if total_row else 0

            # Labeled rows
            labeled_row = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
            ).fetchone()
            total_labeled = labeled_row[0] if labeled_row else 0

            coverage_pct = round(100.0 * total_labeled / total_snapshots, 2) if total_snapshots > 0 else 0.0

            # Stale nulls: snapshots old enough that outcome_252d should exist
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS)).strftime("%Y-%m-%d")
            stale_rows = conn.execute(
                """
                SELECT ticker, snapshot_date
                FROM pipeline_snapshots
                WHERE outcome_252d IS NULL
                  AND snapshot_date < ?
                ORDER BY snapshot_date ASC
                LIMIT 50
                """,
                (cutoff_date,),
            ).fetchall()
            stale_tickers = [{"ticker": r[0], "snapshot_date": r[1]} for r in stale_rows]

            stale_count_row = conn.execute(
                """
                SELECT COUNT(DISTINCT ticker)
                FROM pipeline_snapshots
                WHERE outcome_252d IS NULL AND snapshot_date < ?
                """,
                (cutoff_date,),
            ).fetchone()
            stale_ticker_count = stale_count_row[0] if stale_count_row else 0

            # Last backfill date: most recent snapshot_date that has outcome_252d filled
            last_backfill_row = conn.execute(
                "SELECT MAX(snapshot_date) FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
            ).fetchone()
            last_backfill_date = last_backfill_row[0] if last_backfill_row else None

            report.update({
                "total_snapshots": total_snapshots,
                "total_labeled": total_labeled,
                "coverage_pct": coverage_pct,
                "stale_null_ticker_count": stale_ticker_count,
                "stale_null_cutoff_date": cutoff_date,
                "stale_null_sample": stale_tickers[:20],
                "last_backfill_date": last_backfill_date,
            })

            logger.info(
                f"Snapshots: {total_snapshots}, Labeled: {total_labeled} "
                f"({coverage_pct}%), Stale nulls: {stale_ticker_count}, "
                f"Last backfill: {last_backfill_date}"
            )

    except Exception as e:
        report["status"] = "error"
        report["error"]  = str(e)
        logger.error(f"Data watchdog error: {e}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Data quality report written to {REPORT_PATH}")
    return report


if __name__ == "__main__":
    run()

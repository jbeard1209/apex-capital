#!/usr/bin/env python3
"""
APEX CAPITAL -- ML LONG-TERM DRIFT MONITOR
===========================================
Checks outcome_252d positive rate over time.
Splits data pre/post 2020 and pre/post 2022 to detect label distribution shift.
Writes data/ml_longterm_drift_report.json.
Handles missing outcome_252d gracefully.

Run: python3 ml_longterm_drift_monitor.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH     = DATA_DIR / "historical_13f.db"
REPORT_PATH = DATA_DIR / "ml_longterm_drift_report.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_longterm_drift_monitor")


def _query_period(conn: sqlite3.Connection, start: str, end: str) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome_252d > 0 THEN 1 ELSE 0 END) as positive
        FROM pipeline_snapshots
        WHERE outcome_252d IS NOT NULL
          AND snapshot_date >= ? AND snapshot_date < ?
        """,
        (start, end),
    ).fetchone()
    total    = row[0] if row else 0
    positive = row[1] if row else 0
    pos_rate = round(positive / total, 4) if total > 0 else None
    return {"total": total, "positive": positive, "positive_rate": pos_rate}


def run() -> dict:
    logger.info("ml_longterm_drift_monitor starting")

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "periods": {},
        "warnings": [],
    }

    if not DB_PATH.exists():
        report["status"] = "skipped"
        report["reason"] = "Database not found"
        logger.warning("Database not found -- skipping drift monitor")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        return report

    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Check if outcome_252d column exists
            cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
            if "outcome_252d" not in cols:
                report["status"] = "skipped"
                report["reason"] = "outcome_252d column not present"
                logger.warning("outcome_252d column missing -- skipping")
                with open(REPORT_PATH, "w") as f:
                    json.dump(report, f, indent=2)
                return report

            total_row = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
            ).fetchone()
            total_labeled = total_row[0] if total_row else 0
            report["total_labeled"] = total_labeled

            if total_labeled == 0:
                report["status"] = "no_data"
                report["reason"] = "No labeled outcome_252d rows"
                logger.warning("No labeled outcome_252d rows")
                with open(REPORT_PATH, "w") as f:
                    json.dump(report, f, indent=2)
                return report

            periods = {
                "pre_2020":    _query_period(conn, "2000-01-01", "2020-01-01"),
                "post_2020":   _query_period(conn, "2020-01-01", "2030-01-01"),
                "pre_2022":    _query_period(conn, "2000-01-01", "2022-01-01"),
                "post_2022":   _query_period(conn, "2022-01-01", "2030-01-01"),
                "all_time":    _query_period(conn, "2000-01-01", "2030-01-01"),
            }
            report["periods"] = periods

            # Drift warning: if post-2022 positive rate deviates >15pp from pre-2022
            pre_rate  = periods["pre_2022"]["positive_rate"]
            post_rate = periods["post_2022"]["positive_rate"]
            if pre_rate is not None and post_rate is not None:
                drift = abs(post_rate - pre_rate)
                if drift > 0.15:
                    msg = (
                        f"Label drift detected: pre-2022 positive rate={pre_rate:.2%}, "
                        f"post-2022={post_rate:.2%}, delta={drift:.2%}"
                    )
                    report["warnings"].append(msg)
                    logger.warning(msg)

            # Drift warning: pre/post 2020
            pre20_rate  = periods["pre_2020"]["positive_rate"]
            post20_rate = periods["post_2020"]["positive_rate"]
            if pre20_rate is not None and post20_rate is not None:
                drift20 = abs(post20_rate - pre20_rate)
                if drift20 > 0.15:
                    msg = (
                        f"Label drift detected: pre-2020 positive rate={pre20_rate:.2%}, "
                        f"post-2020={post20_rate:.2%}, delta={drift20:.2%}"
                    )
                    report["warnings"].append(msg)
                    logger.warning(msg)

            logger.info(f"Drift report: {periods}")

    except Exception as e:
        report["status"] = "error"
        report["error"]  = str(e)
        logger.error(f"Drift monitor error: {e}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Drift report written to {REPORT_PATH}")
    return report


if __name__ == "__main__":
    run()

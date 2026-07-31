#!/usr/bin/env python3
"""
APEX CAPITAL -- ML LONG-TERM LABEL CHECKER
==========================================
Checks COUNT of outcome_252d IS NOT NULL vs yesterday's baseline.
If >5000 new rows have been labeled since last run: adds a "retrain"
suggestion to ml_longterm_suggestions.json.
Writes updated status to data/ml_longterm_label_status.json.

Run: python3 ml_longterm_label_checker.py
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

DB_PATH          = DATA_DIR / "historical_13f.db"
STATUS_FILE      = DATA_DIR / "ml_longterm_label_status.json"
SUGGESTIONS_FILE = DATA_DIR / "ml_longterm_suggestions.json"

NEW_ROWS_RETRAIN_THRESHOLD = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_longterm_label_checker")


def _load_status() -> dict:
    if not STATUS_FILE.exists():
        return {"last_count": 0, "last_checked": None}
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_count": 0, "last_checked": None}


def _load_suggestions() -> list[dict]:
    if not SUGGESTIONS_FILE.exists():
        return []
    try:
        with open(SUGGESTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _get_labeled_count() -> int:
    if not DB_PATH.exists():
        return 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}
            if "outcome_252d" not in cols:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"DB error: {e}")
        return 0


def run() -> None:
    logger.info("ml_longterm_label_checker starting")

    now_str    = datetime.now(timezone.utc).isoformat()
    status     = _load_status()
    last_count = status.get("last_count", 0)
    current    = _get_labeled_count()
    new_rows   = current - last_count

    logger.info(f"Last count: {last_count}, Current: {current}, New rows: {new_rows}")

    if new_rows >= NEW_ROWS_RETRAIN_THRESHOLD:
        logger.info(f"{new_rows} new labeled rows -- adding retrain suggestion")
        suggestions = _load_suggestions()
        # Avoid duplicate retrain suggestions
        existing_ids = {s["id"] for s in suggestions}
        retrain_id = f"lt_retrain_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        if retrain_id not in existing_ids:
            suggestions.append({
                "id": retrain_id,
                "title": f"Retrain long-term model ({new_rows} new labeled rows)",
                "rationale": (
                    f"{new_rows} new outcome_252d rows were labeled since last check "
                    f"(total: {current}). Retraining will incorporate fresh ground truth."
                ),
                "category": "retrain",
                "estimated_auc_delta": 0.005,
                "effort": "low",
                "status": "pending",
                "created_at": now_str,
                "new_rows_trigger": new_rows,
                "total_labeled_at_trigger": current,
            })
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(SUGGESTIONS_FILE, "w") as f:
                json.dump(suggestions, f, indent=2)
            logger.info(f"Retrain suggestion {retrain_id} added")
        else:
            logger.info(f"Retrain suggestion {retrain_id} already exists -- skipping")
    else:
        logger.info(f"New rows ({new_rows}) below threshold ({NEW_ROWS_RETRAIN_THRESHOLD}) -- no retrain suggestion")

    # Update status
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump({
            "last_count":   current,
            "last_checked": now_str,
            "new_rows":     new_rows,
        }, f, indent=2)

    logger.info("ml_longterm_label_checker done")


if __name__ == "__main__":
    run()

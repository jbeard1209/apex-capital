#!/usr/bin/env python3
"""
APEX CAPITAL -- ML LONG-TERM IMPROVEMENT AGENT
===============================================
Generates experiment ideas for the 1-year horizon model (outcome_252d).
Focuses on fundamental features: P/B ratio, FCF yield, revenue growth rate,
ROIC, net debt/EBITDA (all via yfinance fundamentals).
Also generates structural ideas: remove short-term features, train on
post-2018 data only, weight value stocks 2x, etc.

Writes ideas to data/ml_longterm_suggestions.json with status "pending".

Run: python3 ml_longterm_improvement_agent.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

SUGGESTIONS_FILE = DATA_DIR / "ml_longterm_suggestions.json"
DB_PATH          = DATA_DIR / "historical_13f.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_longterm_improvement_agent")


IDEA_TEMPLATES = [
    {
        "id": "lt_feature_pb_ratio",
        "title": "Add P/B ratio as fundamental feature",
        "rationale": (
            "Price-to-book ratio is a core value signal for 1-year horizons. "
            "Institutions accumulating low P/B stocks tend to outperform over 365d. "
            "Source: yfinance ticker.info['priceToBook']."
        ),
        "category": "feature_engineering",
        "estimated_auc_delta": 0.012,
        "effort": "medium",
    },
    {
        "id": "lt_feature_fcf_yield",
        "title": "Add FCF yield as fundamental feature",
        "rationale": (
            "Free cash flow yield (FCF / market cap) captures quality at a price. "
            "Strong predictor of institutional accumulation payoff over 365d. "
            "Source: yfinance financials + market cap."
        ),
        "category": "feature_engineering",
        "estimated_auc_delta": 0.015,
        "effort": "medium",
    },
    {
        "id": "lt_feature_revenue_growth",
        "title": "Add revenue growth rate (YoY) as fundamental feature",
        "rationale": (
            "Revenue growth rate (YoY) captures business momentum that institutions "
            "track for multi-year conviction positions. "
            "Source: yfinance quarterly financials."
        ),
        "category": "feature_engineering",
        "estimated_auc_delta": 0.010,
        "effort": "medium",
    },
    {
        "id": "lt_feature_roic",
        "title": "Add ROIC as fundamental feature",
        "rationale": (
            "Return on invested capital (ROIC) identifies durable competitive advantage. "
            "Stocks with ROIC > WACC compound over 365d. "
            "Source: yfinance financials + balance sheet."
        ),
        "category": "feature_engineering",
        "estimated_auc_delta": 0.013,
        "effort": "high",
    },
    {
        "id": "lt_feature_net_debt_ebitda",
        "title": "Add net debt / EBITDA as fundamental feature",
        "rationale": (
            "Leverage ratio filters out financially stressed positions. "
            "Low net debt/EBITDA tickers accumulate safely by institutions for 1-year holds. "
            "Source: yfinance balance sheet + earnings."
        ),
        "category": "feature_engineering",
        "estimated_auc_delta": 0.009,
        "effort": "medium",
    },
    {
        "id": "lt_remove_short_rsi",
        "title": "Remove short-term RSI features from long-term model",
        "rationale": (
            "RSI (14d, 20d) is noise at the 365-day horizon. "
            "Including it may cause overfitting to short-term momentum patterns "
            "that have no predictive value over 1 year."
        ),
        "category": "feature_pruning",
        "estimated_auc_delta": 0.006,
        "effort": "low",
    },
    {
        "id": "lt_post2018_only",
        "title": "Train only on post-2018 data",
        "rationale": (
            "Pre-2018 market structure (low rates, pre-passive-investing dominance) "
            "may introduce distributional shift. "
            "Restricting training to 2018+ aligns with current institutional behavior."
        ),
        "category": "data_filtering",
        "estimated_auc_delta": 0.007,
        "effort": "low",
    },
    {
        "id": "lt_weight_value_2x",
        "title": "Weight value stocks 2x in training",
        "rationale": (
            "Long-term institutional mandates (First Eagle, Dodge & Cox style) "
            "are concentrated in value stocks. Overweighting value tickers "
            "aligns the loss function with how long-horizon managers actually operate."
        ),
        "category": "training_strategy",
        "estimated_auc_delta": 0.008,
        "effort": "medium",
    },
]


def _load_existing() -> list[dict]:
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
            row = conn.execute(
                "SELECT COUNT(*) FROM pipeline_snapshots WHERE outcome_252d IS NOT NULL"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def run() -> None:
    logger.info("ml_longterm_improvement_agent starting")
    existing = _load_existing()
    existing_ids = {e["id"] for e in existing}

    labeled_count = _get_labeled_count()
    logger.info(f"outcome_252d labeled rows: {labeled_count}")

    now_str = datetime.now(timezone.utc).isoformat()
    added = 0

    for idea in IDEA_TEMPLATES:
        if idea["id"] in existing_ids:
            logger.info(f"Skipping existing idea: {idea['id']}")
            continue
        entry = {
            **idea,
            "status": "pending",
            "created_at": now_str,
            "labeled_rows_at_creation": labeled_count,
        }
        existing.append(entry)
        added += 1
        logger.info(f"Added idea: {idea['id']}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUGGESTIONS_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    logger.info(f"ml_longterm_improvement_agent done. Added {added} new ideas. Total: {len(existing)}")


if __name__ == "__main__":
    run()

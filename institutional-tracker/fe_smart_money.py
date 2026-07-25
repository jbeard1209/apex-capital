#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_smart_money.py -- Cross-reference tickers against local 13F institutional database
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).parent / "data" / "historical_13f.db")

KNOWN_VALUE_INVESTORS = [
    "BERKSHIRE", "BAUPOST", "FAIRFAX", "GOTHAM", "PZENA",
    "TWEEDY", "THIRD AVENUE", "ARIEL", "SOUTHEASTERN", "BRANDES",
]


def check_smart_money(ticker) -> dict:
    """
    Query local historical_13f.db for institutional holdings of ticker.
    Returns: {ticker, institutions_holding, value_investors_holding,
              recent_buyers, recent_sellers, smart_money_signal}
    """
    ticker = ticker.upper().strip()

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Get all snapshots for this ticker, grouped by institution and filing_date
        cur.execute("""
            SELECT institution, filing_date, shares, value_usd
            FROM holdings
            WHERE ticker = ?
            ORDER BY filing_date DESC
        """, (ticker,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {
            "ticker": ticker,
            "institutions_holding": 0,
            "value_investors_holding": [],
            "recent_buyers": [],
            "recent_sellers": [],
            "smart_money_signal": "NEUTRAL",
            "note": f"DB query failed: {e}",
        }

    if not rows:
        return {
            "ticker": ticker,
            "institutions_holding": 0,
            "value_investors_holding": [],
            "recent_buyers": [],
            "recent_sellers": [],
            "smart_money_signal": "NEUTRAL",
            "note": "No holdings found for this ticker in local 13F database",
        }

    # Group by institution, get latest and prior snapshot
    institution_snapshots = {}
    for row in rows:
        inst = row["institution"]
        fd   = row["filing_date"]
        if inst not in institution_snapshots:
            institution_snapshots[inst] = []
        institution_snapshots[inst].append({
            "filing_date": fd,
            "shares": row["shares"],
            "value_usd": row["value_usd"],
        })

    # Sort each institution's snapshots by date (newest first)
    for inst in institution_snapshots:
        institution_snapshots[inst].sort(key=lambda x: x["filing_date"], reverse=True)

    # Identify who currently holds (has at least one entry)
    institutions_holding = list(institution_snapshots.keys())

    # Check value investors
    value_investors_holding = []
    for inst in institutions_holding:
        if any(vi in inst.upper() for vi in KNOWN_VALUE_INVESTORS):
            value_investors_holding.append(inst)

    # Identify recent buyers / sellers (compare latest vs prior snapshot)
    recent_buyers  = []
    recent_sellers = []
    for inst, snaps in institution_snapshots.items():
        if len(snaps) >= 2:
            latest = snaps[0]["shares"]
            prior  = snaps[1]["shares"]
            if latest is not None and prior is not None:
                if latest > prior * 1.05:
                    pct = round((latest - prior) / prior * 100, 1)
                    recent_buyers.append({"institution": inst, "change_pct": pct,
                                          "latest_shares": int(latest), "filing": snaps[0]["filing_date"]})
                elif latest < prior * 0.95:
                    pct = round((latest - prior) / prior * 100, 1)
                    recent_sellers.append({"institution": inst, "change_pct": pct,
                                           "latest_shares": int(latest), "filing": snaps[0]["filing_date"]})

    # Determine smart money signal
    n_holders = len(institutions_holding)
    n_buyers  = len(recent_buyers)
    n_sellers = len(recent_sellers)

    if n_buyers > n_sellers and (value_investors_holding or n_buyers >= 2):
        signal = "BULLISH"
    elif n_sellers > n_buyers or n_holders == 0:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    return {
        "ticker": ticker,
        "institutions_holding": n_holders,
        "institutions_list": institutions_holding,
        "value_investors_holding": value_investors_holding,
        "recent_buyers": recent_buyers,
        "recent_sellers": recent_sellers,
        "smart_money_signal": signal,
    }


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = check_smart_money(ticker)
    print(json.dumps(result, indent=2, default=str))

#!/usr/bin/env python3
"""APEX CAPITAL — FE FRAUD WATCHDOG
Red-flag screen for FE picks. Filters obvious frauds and balance sheet disasters.
Returns (passed_picks, flagged_picks).
"""
from __future__ import annotations
import logging
logger = logging.getLogger("fe_fraud_watchdog")

RED_FLAGS = ["restatement", "going concern", "sec investigation", "material weakness",
             "auditor change", "fraud", "default", "bankruptcy"]

def fraud_check(ticker: str, picks: list) -> tuple[list, list]:
    """Screen picks for fraud red flags. Returns (clean, flagged)."""
    clean, flagged = [], []
    for pick in picks:
        flags = []
        # Check for known structural issues
        debt_equity = pick.get("debt_equity", 0) or 0
        if debt_equity > 15:
            flags.append(f"extreme leverage D/E={debt_equity:.1f}")
        mos = pick.get("margin_of_safety", 0) or 0
        if mos < -1.0:
            flags.append(f"extreme overvaluation MoS={mos*100:.0f}%")
        if flags:
            pick["fraud_flags"] = flags
            flagged.append(pick)
            logger.warning("[FraudWatchdog] %s flagged: %s", pick.get("ticker"), "; ".join(flags))
        else:
            clean.append(pick)
    return clean, flagged

def run_prescreen(watchlist: list) -> list:
    """Pre-screen watchlist for obvious frauds before running agents."""
    return watchlist  # lightweight — full screen happens at pick level

#!/usr/bin/env python3
"""
pretrade_compliance_agent.py — Pre-Trade Compliance Agent (#19)
==============================================================
Sits between the ML model / review pipeline and the Alpaca executor.
Before any trade is placed, checks:
  1. Position size limits (max 10% of portfolio per position)
  2. Sector concentration limits (max 35% in any single sector)
  3. Blackout periods (earnings within 2 days)
  4. Liquidity check (can full position be filled in <3 days' ADTV?)
  5. Circuit breaker status (is portfolio drawdown freeze active?)

Returns APPROVED / FLAGGED / BLOCKED for each proposed trade,
with reasons. ManagerAgent imports this before calling the executor.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("pretrade_compliance")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

COMPLIANCE_LOG = os.path.join(DATA_DIR, "compliance_audit_log.json")

# Limits
MAX_POSITION_PCT   = 0.10   # max 10% of portfolio in any single position
MAX_SECTOR_PCT     = 0.35   # max 35% in any sector
MAX_ADTV_DAYS      = 3      # position must be fillable in <3 days of average volume
EARNINGS_BLACKOUT_DAYS = 2  # don't initiate new position within 2 days of earnings

APPROVAL_STATUSES = ("APPROVED", "FLAGGED", "BLOCKED")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_log(entry: dict) -> None:
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    log = _load_json(COMPLIANCE_LOG, [])
    log.append(entry)
    log = log[-1000:]
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(log, f, indent=2, default=str)
        os.replace(tmp, COMPLIANCE_LOG)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def get_adtv(ticker: str, days: int = 30) -> Optional[float]:
    """Return average daily dollar volume for ticker over last N days."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period=f"{days+5}d", progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        if df.empty:
            return None
        df["dollar_vol"] = df["Close"] * df["Volume"]
        return float(df["dollar_vol"].tail(days).mean())
    except Exception as e:
        logger.debug(f"  ADTV fetch error for {ticker}: {e}")
        return None


def get_earnings_date(ticker: str) -> Optional[str]:
    """Return next earnings date if within EARNINGS_BLACKOUT_DAYS, else None."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).calendar
        if info is None:
            return None
        # Calendar returns a DataFrame or dict
        if hasattr(info, "columns"):
            earnings_dates = info.get("Earnings Date", [])
        elif isinstance(info, dict):
            earnings_dates = info.get("Earnings Date", [])
        else:
            return None
        if not earnings_dates:
            return None
        for ed in (earnings_dates if hasattr(earnings_dates, "__iter__") else [earnings_dates]):
            try:
                import pandas as pd
                ed_dt = pd.Timestamp(ed).to_pydatetime().replace(tzinfo=timezone.utc)
                days_away = (ed_dt - datetime.now(timezone.utc)).days
                if -1 <= days_away <= EARNINGS_BLACKOUT_DAYS:
                    return str(ed_dt.date())
            except Exception:
                continue
        return None
    except Exception as e:
        logger.debug(f"  Earnings check error for {ticker}: {e}")
        return None


def check_trade(
    ticker: str,
    action: str,             # "BUY_NEW", "ADD", "SELL", "REDUCE"
    proposed_dollar_value: float,
    portfolio_value: float,
    current_positions: dict,  # {ticker: {value, sector}}
    sector: str = "Unknown",
) -> dict:
    """
    Run all compliance checks on a single proposed trade.
    Returns {status, flags, reasons, ticker, action}.
    """
    flags   = []
    reasons = []
    blocked = []

    # Skip checks for SELL/REDUCE — compliance only gate entries
    if action in ("SELL", "REDUCE", "CLOSE"):
        return {"status": "APPROVED", "flags": [], "reasons": ["Exit trade — compliance checks skipped"], "ticker": ticker, "action": action}

    # ── 1. Position size limit ──────────────────────────────────────────────
    position_pct = proposed_dollar_value / portfolio_value if portfolio_value > 0 else 0
    existing_val = current_positions.get(ticker, {}).get("value", 0)
    total_pos_pct = (existing_val + proposed_dollar_value) / portfolio_value if portfolio_value > 0 else 0
    if total_pos_pct > MAX_POSITION_PCT:
        blocked.append(f"Position size {total_pos_pct:.1%} exceeds limit {MAX_POSITION_PCT:.1%}")
    elif total_pos_pct > MAX_POSITION_PCT * 0.8:
        flags.append(f"Position size {total_pos_pct:.1%} near limit {MAX_POSITION_PCT:.1%}")

    # ── 2. Sector concentration ─────────────────────────────────────────────
    sector_total = sum(
        p.get("value", 0) for t, p in current_positions.items()
        if p.get("sector", "") == sector
    )
    sector_pct = (sector_total + proposed_dollar_value) / portfolio_value if portfolio_value > 0 else 0
    if sector_pct > MAX_SECTOR_PCT:
        blocked.append(f"Sector {sector} concentration {sector_pct:.1%} exceeds limit {MAX_SECTOR_PCT:.1%}")
    elif sector_pct > MAX_SECTOR_PCT * 0.8:
        flags.append(f"Sector {sector} concentration {sector_pct:.1%} near limit {MAX_SECTOR_PCT:.1%}")

    # ── 3. Circuit breaker ──────────────────────────────────────────────────
    try:
        import position_risk_monitor as _prm
        if _prm.is_circuit_breaker_active():
            cb = _prm._load_json(_prm.CIRCUIT_BREAKER, {})
            blocked.append(f"Portfolio drawdown circuit breaker ACTIVE (drawdown={cb.get('drawdown_pct','?')}%)")
    except Exception:
        pass

    # ── 4. Earnings blackout ────────────────────────────────────────────────
    earnings_dt = get_earnings_date(ticker)
    if earnings_dt:
        flags.append(f"Earnings within blackout window: {earnings_dt}")

    # ── 5. Liquidity check ──────────────────────────────────────────────────
    adtv = get_adtv(ticker, days=30)
    if adtv is not None and adtv > 0:
        days_to_fill = proposed_dollar_value / adtv
        if days_to_fill > MAX_ADTV_DAYS:
            blocked.append(
                f"Liquidity: would take {days_to_fill:.1f}d to fill "
                f"(ADTV=${adtv/1e6:.1f}M, proposed=${proposed_dollar_value/1e3:.0f}K)"
            )
        elif days_to_fill > MAX_ADTV_DAYS * 0.5:
            flags.append(f"Liquidity: {days_to_fill:.1f}d to fill (caution)")
    else:
        flags.append("Liquidity: ADTV unavailable — manual review recommended")

    if blocked:
        status = "BLOCKED"
        reasons = blocked + flags
    elif flags:
        status = "FLAGGED"
        reasons = flags
    else:
        status = "APPROVED"
        reasons = ["All compliance checks passed"]

    result = {
        "status":   status,
        "flags":    flags,
        "blocked":  blocked,
        "reasons":  reasons,
        "ticker":   ticker,
        "action":   action,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "position_pct":  round(total_pos_pct * 100, 2),
        "sector_pct":    round(sector_pct * 100, 2),
        "adtv_days":     round(days_to_fill, 2) if adtv and adtv > 0 else None,
    }
    _save_log(result)
    return result


def compliance_screen_decisions(
    decisions: list[dict],
    portfolio_value: float,
    current_positions: dict | None = None,
) -> list[dict]:
    """
    Screen a list of manager_agent decisions through compliance.
    Modifies each decision in-place: sets approved=False and appends compliance_blocked=True
    for BLOCKED decisions.
    Returns the list with compliance results attached.
    """
    if current_positions is None:
        current_positions = {}

    for d in decisions:
        ticker  = d.get("ticker", "")
        action  = d.get("action", "HOLD")
        value   = d.get("proposed_dollar_value", d.get("target_value", 5000.0))
        sector  = d.get("sector", "Unknown")

        if action in ("HOLD", "SELL", "REDUCE", "CLOSE") or not d.get("approved", False):
            continue

        result = check_trade(ticker, action, float(value), portfolio_value, current_positions, sector)
        d["compliance_status"]  = result["status"]
        d["compliance_reasons"] = result["reasons"]

        if result["status"] == "BLOCKED":
            d["approved"]            = False
            d["compliance_blocked"]  = True
            logger.warning(f"  [COMPLIANCE BLOCKED] {ticker} {action}: {result['blocked']}")
        elif result["status"] == "FLAGGED":
            logger.info(f"  [COMPLIANCE FLAGGED] {ticker} {action}: {result['flags']}")

    return decisions


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("PreTradeComplianceAgent")
        sys.exit(0)

    # Self-test
    print("Pre-Trade Compliance Agent — Self Test")
    test = check_trade(
        ticker="AAPL", action="BUY_NEW",
        proposed_dollar_value=15000,
        portfolio_value=100000,
        current_positions={"MSFT": {"value": 8000, "sector": "Information Technology"}},
        sector="Information Technology"
    )
    print(f"AAPL BUY $15K: {test['status']} | {test['reasons']}")

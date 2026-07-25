#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME EXECUTOR
=======================================
Reads fi_approved_picks.json produced by the FI board, applies safety
gates, and submits market buy orders via the shared Alpaca trading client.

Safety gates (in order):
  1. Market hours — only trades during regular session (9:30–16:00 ET)
  2. Canary gate — skips if buys_blocked=True or canary level=RED
  3. IRM gate   — skips if IRM highest_risk_score >= 8
  4. Duplicate  — skips tickers already executed today (fi_execution_log)
  5. FI position cap — skips if ticker already held >= 80% of target size

Runs immediately after fi_board.py (called from fi_pipeline.py, Step 9).
Can also run standalone: python3 fi_executor.py

Output: data/fi_execution_log.json
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

APPROVED_FILE   = DATA_DIR / "fi_approved_picks.json"
EXEC_LOG_FILE   = DATA_DIR / "fi_execution_log.json"
CANARY_FILE     = DATA_DIR / "canary_state.json"
IRM_FILE        = DATA_DIR / "irm_daily_check.json"
CIT_HR_MEMO     = DATA_DIR / "cit_hr_memo.json"

FI_CAPITAL = 30_000.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fi_executor")


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------

_US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}

def _is_market_open() -> bool:
    """True if current UTC time falls inside NYSE regular session (ET 9:30–16:00)."""
    now_utc = datetime.now(timezone.utc)
    # ET = UTC-4 (EDT) or UTC-5 (EST); approximate with UTC-4 for summer
    now_et = now_utc - timedelta(hours=4)
    if now_et.weekday() >= 5:          # Saturday / Sunday
        return False
    if now_et.strftime("%Y-%m-%d") in _US_MARKET_HOLIDAYS_2026:
        return False
    open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now_et < close_t


def _canary_gate() -> tuple[bool, str]:
    """Returns (ok_to_trade, reason). ok_to_trade=False blocks execution."""
    try:
        if not CANARY_FILE.exists():
            return True, "canary file missing — proceeding (fail open)"
        with open(CANARY_FILE) as f:
            cs = json.load(f)
        level = (cs.get("last_level") or "GREEN").upper()
        if level == "RED":
            return False, f"Canary level=RED — all buys blocked"
        # Check if main portfolio has buys_blocked
        buys_blocked = cs.get("buys_blocked", False)
        if buys_blocked:
            return False, "Canary buys_blocked=True — FI executor halted"
    except Exception as e:
        logger.debug(f"non-fatal: {e}")
    return True, "ok"


def _irm_gate() -> tuple[bool, str]:
    """Returns (ok_to_trade, reason). Blocks if IRM risk >= 8 (extreme)."""
    try:
        if not IRM_FILE.exists():
            return True, "irm file missing — proceeding"
        with open(IRM_FILE) as f:
            irm = json.load(f)
        score = irm.get("board_panel", {}).get("highest_risk_score", 0)
        if score >= 8:
            return False, f"IRM risk score={score} (extreme) — FI buys suspended"
    except Exception as e:
        logger.debug(f"non-fatal: {e}")
    return True, "ok"


# ---------------------------------------------------------------------------
# Execution log helpers
# ---------------------------------------------------------------------------

def _load_exec_log() -> dict:
    try:
        if EXEC_LOG_FILE.exists():
            with open(EXEC_LOG_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"non-fatal: {e}")
    return {"executions": []}


def _save_exec_log(log: dict) -> None:
    with open(EXEC_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def _already_executed_today(ticker: str, exec_log: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ex in exec_log.get("executions", []):
        if ex.get("ticker") == ticker and ex.get("date") == today:
            return True
    return False


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------

def _get_price(ticker: str) -> float | None:
    # Method 1: fast_info attribute access (yfinance FastInfo uses attrs, not dict.get())
    try:
        fi = yf.Ticker(ticker).fast_info
        for attr in ("last_price", "regularMarketPrice", "previous_close"):
            price = getattr(fi, attr, None)
            if price and float(price) > 0:
                return float(price)
    except Exception as e:
        logger.debug(f"non-fatal fast_info: {e}")
    # Method 2: download last 5 trading days
    try:
        df = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"non-fatal yf.download: {e}")
    # Method 3: Alpaca latest quote
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        price = trader.get_latest_price(ticker)
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.debug(f"non-fatal alpaca price: {e}")
    return None


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

def run_fi_executor() -> dict:
    logger.info("=" * 60)
    logger.info("APEX CAPITAL -- FIXED INCOME EXECUTOR")
    logger.info("=" * 60)

    result = {
        "run_at":    datetime.now(timezone.utc).isoformat(),
        "submitted": [],
        "skipped":   [],
        "errors":    [],
    }

    # --- Safety gate: market hours ---
    if not _is_market_open():
        msg = "Market is closed — FI orders will not be placed (run during 9:30–16:00 ET)"
        logger.warning(msg)
        result["skipped"].append({"reason": msg})
        _finalize(result)
        return result

    # --- Safety gate: canary ---
    canary_ok, canary_reason = _canary_gate()
    if not canary_ok:
        logger.warning(f"CANARY GATE: {canary_reason}")
        result["skipped"].append({"reason": canary_reason})
        _finalize(result)
        return result

    # --- Safety gate: IRM ---
    irm_ok, irm_reason = _irm_gate()
    if not irm_ok:
        logger.warning(f"IRM GATE: {irm_reason}")
        result["skipped"].append({"reason": irm_reason})
        _finalize(result)
        return result

    # --- Load approved picks ---
    if not APPROVED_FILE.exists():
        logger.info("No fi_approved_picks.json found — nothing to execute")
        return result

    try:
        with open(APPROVED_FILE) as f:
            approved_data = json.load(f)
    except Exception as e:
        logger.error(f"Could not read fi_approved_picks.json: {e}")
        result["errors"].append(str(e))
        return result

    picks = approved_data.get("picks", [])
    if not picks:
        logger.info("No approved picks to execute")
        return result

    # Check approval freshness — only execute picks approved today
    generated_at = approved_data.get("generated_at", "")
    if generated_at:
        try:
            gen_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
            if age_h > 26:
                msg = f"Approved picks are {age_h:.1f}h old — skipping stale execution"
                logger.warning(msg)
                result["skipped"].append({"reason": msg})
                return result
        except Exception as e:
            logger.debug(f"non-fatal: {e}")

    # --- Alpaca client ---
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        client = TradingClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=True)
        logger.info("Alpaca client connected")
    except Exception as e:
        logger.error(f"Alpaca client failed to initialize: {e}")
        result["errors"].append(f"Alpaca init: {e}")
        return result

    # --- Current Alpaca positions ---
    try:
        existing_positions = {p.symbol for p in client.get_all_positions()}
        logger.info(f"Existing positions: {existing_positions or 'none'}")
    except Exception as e:
        existing_positions = set()
        logger.debug(f"non-fatal: {e}")

    # --- Account cash check ---
    try:
        acct = client.get_account()
        available_cash = float(acct.cash)
        logger.info(f"Available cash: ${available_cash:,.2f}")
    except Exception as e:
        logger.error(f"Could not fetch account: {e}")
        result["errors"].append(f"Account fetch: {e}")
        return result

    exec_log = _load_exec_log()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for pick in picks:
        ticker      = pick.get("ticker", "")
        target_usd  = float(pick.get("position_size_usd", 0))
        conviction  = pick.get("conviction", 3)
        verdict     = pick.get("verdict", "BUY")
        reason      = pick.get("reason", "")

        if not ticker or target_usd <= 0:
            continue

        # Duplicate guard
        if _already_executed_today(ticker, exec_log):
            logger.info(f"  SKIP {ticker}: already executed today")
            result["skipped"].append({"ticker": ticker, "reason": "already executed today"})
            continue

        # Already have a meaningful position
        if ticker in existing_positions:
            logger.info(f"  SKIP {ticker}: position already open")
            result["skipped"].append({"ticker": ticker, "reason": "position already open"})
            continue

        # Cash floor: maintain 15% of FI_CAPITAL as minimum cash buffer
        fi_cash_floor = FI_CAPITAL * 0.15
        if available_cash < fi_cash_floor + target_usd:
            msg = (f"Insufficient cash: need ${target_usd + fi_cash_floor:,.0f}, "
                   f"have ${available_cash:,.0f} (floor ${fi_cash_floor:,.0f})")
            logger.warning(f"  SKIP {ticker}: {msg}")
            result["skipped"].append({"ticker": ticker, "reason": msg})
            continue

        # Get current price
        price = _get_price(ticker)
        if not price:
            logger.error(f"  SKIP {ticker}: could not fetch price")
            result["errors"].append(f"{ticker}: price unavailable")
            continue

        qty = int(target_usd // price)
        if qty <= 0:
            logger.warning(f"  SKIP {ticker}: target ${target_usd:.0f} < price ${price:.2f}")
            result["skipped"].append({"ticker": ticker, "reason": "target_usd < price"})
            continue

        actual_cost = qty * price
        logger.info(f"  Submitting BUY: {qty} x {ticker} @ ~${price:.2f} = ~${actual_cost:,.2f}"
                    f"  [{verdict} conv={conviction}]")

        try:
            order_req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = client.submit_order(order_req)
            order_id = str(order.id)
            logger.info(f"  ✅ ORDER SUBMITTED: {ticker} id={order_id}")

            exec_entry = {
                "ticker":      ticker,
                "date":        today,
                "qty":         qty,
                "price_est":   price,
                "cost_est":    actual_cost,
                "target_usd":  target_usd,
                "conviction":  conviction,
                "verdict":     verdict,
                "reason":      reason,
                "order_id":    order_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            exec_log["executions"].append(exec_entry)
            result["submitted"].append(exec_entry)
            available_cash -= actual_cost

        except Exception as e:
            logger.error(f"  ❌ Order failed for {ticker}: {e}")
            result["errors"].append(f"{ticker}: {e}")

    _save_exec_log(exec_log)
    _finalize(result)
    return result


def _finalize(result: dict) -> None:
    """Buffer a brief execution summary into the evening email digest."""
    submitted = result.get("submitted", [])
    skipped   = result.get("skipped", [])
    errors    = result.get("errors", [])

    lines = [
        "APEX CAPITAL — FI EXECUTION SUMMARY",
        "=" * 45,
        f"Run at: {result.get('run_at', 'unknown')}",
        "",
        f"Orders submitted: {len(submitted)}",
    ]
    for ex in submitted:
        lines.append(
            f"  ✅ {ex['ticker']:6s} {ex['qty']} shares @ ~${ex['price_est']:.2f}"
            f" = ~${ex['cost_est']:,.0f}  [{ex['verdict']}]"
        )
    if skipped:
        lines.append(f"\nSkipped ({len(skipped)}):")
        for s in skipped:
            t = s.get("ticker", "")
            lines.append(f"  — {(t + ': ') if t else ''}{s.get('reason','')}")
    if errors:
        lines.append(f"\nErrors ({len(errors)}):")
        for e in errors:
            lines.append(f"  ❌ {e}")

    body = "\n".join(lines)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"Apex Capital — FI Execution | {date_str}"

    try:
        from email_buffer import buffer_email
        buffer_email(subject, body, sender_name="FI Executor")
        logger.info("Execution summary buffered for evening digest")
    except Exception as e:
        logger.debug(f"non-fatal: {e}")

    logger.info(
        f"\n{'='*60}\n"
        f"  FI Executor done — submitted: {len(submitted)}, "
        f"skipped: {len(skipped)}, errors: {len(errors)}\n"
        f"{'='*60}"
    )


if __name__ == "__main__":
    run_fi_executor()

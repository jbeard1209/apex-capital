#!/usr/bin/env python3
"""
position_risk_monitor.py — Per-Position Stop-Loss Tracker (#15)
================================================================
Monitors each open position for drawdown from entry price.
Fires alerts to ManagerAgent and CROAgent when a position
breaches its stop-loss threshold (default: sector-adjusted).

Thresholds by sector volatility:
  Low-vol sectors  (utilities, staples):  -6%
  Mid-vol sectors  (financials, health):  -8%
  High-vol sectors (tech, energy):        -12%

Runs alongside the canary (every 3 min market hours, 30 min otherwise).
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
logger = logging.getLogger("position_risk_monitor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

POSITION_RISK_LOG = os.path.join(DATA_DIR, "position_risk_log.json")
CIRCUIT_BREAKER   = os.path.join(DATA_DIR, "circuit_breaker_state.json")

# Sector → stop-loss threshold (negative %)
SECTOR_STOP_LOSS = {
    "Utilities":            -6.0,
    "Consumer Staples":     -6.0,
    "Real Estate":          -7.0,
    "Financials":           -8.0,
    "Health Care":          -8.0,
    "Industrials":          -8.0,
    "Materials":            -9.0,
    "Communication Services": -10.0,
    "Consumer Discretionary": -10.0,
    "Information Technology": -12.0,
    "Energy":               -12.0,
    "DEFAULT":              -8.0,
}

# Portfolio-level drawdown circuit breaker
CIRCUIT_BREAKER_THRESHOLD = -8.0   # -8% in rolling 30 days triggers freeze


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_positions_with_entry() -> list[dict]:
    """
    Read open positions from Alpaca with their average entry cost.
    Returns list of {ticker, qty, current_price, entry_price, pnl_pct, sector}.
    """
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        pos_df = trader.get_positions()
        if pos_df is None or pos_df.empty:
            return []
        positions = []
        for _, row in pos_df.iterrows():
            ticker = str(row.get("symbol", "")).upper()
            try:
                current = float(row.get("current_price", 0))
                entry   = float(row.get("avg_entry_price", current))
                qty     = float(row.get("qty", 0))
                pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0.0
                sector  = str(row.get("sector", "DEFAULT"))
            except (ValueError, TypeError):
                continue
            positions.append({
                "ticker":        ticker,
                "qty":           qty,
                "current_price": current,
                "entry_price":   entry,
                "pnl_pct":       round(pnl_pct, 2),
                "sector":        sector,
            })
        return positions
    except Exception as e:
        logger.debug(f"Could not fetch positions: {e}")
        return []


def check_portfolio_drawdown() -> Optional[float]:
    """
    Check rolling 30-day portfolio drawdown from performance_ledger.
    Returns the drawdown % (negative) or None if unavailable.
    """
    try:
        ledger_path = os.path.join(DATA_DIR, "performance_ledger.json")
        ledger = _load_json(ledger_path, [])
        if not ledger:
            return None
        # Sort by date
        ledger = sorted(ledger, key=lambda x: x.get("date", ""))
        if len(ledger) < 2:
            return None
        # Find 30-day-ago entry
        latest = ledger[-1]
        cutoff = ledger[max(0, len(ledger) - 22)]   # ~30 calendar days ≈ 22 trading days
        latest_val  = latest.get("portfolio_value") or latest.get("total_value")
        cutoff_val  = cutoff.get("portfolio_value") or cutoff.get("total_value")
        if not latest_val or not cutoff_val or cutoff_val <= 0:
            return None
        return round((latest_val - cutoff_val) / cutoff_val * 100, 2)
    except Exception as e:
        logger.debug(f"Drawdown check error: {e}")
        return None


def is_circuit_breaker_active() -> bool:
    """Return True if the circuit breaker (hard stop on new entries) is currently active."""
    state = _load_json(CIRCUIT_BREAKER, {})
    return state.get("active", False)


def _activate_circuit_breaker(drawdown_pct: float, triggered_by: str = "position_risk_monitor") -> None:
    """Activate the circuit breaker — freeze new position entries until board vote."""
    state = {
        "active":           True,
        "activated_at":     datetime.now(timezone.utc).isoformat(),
        "triggered_by":     triggered_by,
        "drawdown_pct":     drawdown_pct,
        "reason":           f"Portfolio drawdown {drawdown_pct:.1f}% exceeded {CIRCUIT_BREAKER_THRESHOLD}% threshold in rolling 30d. New position entries FROZEN pending Board vote.",
        "cleared_at":       None,
        "cleared_by":       None,
    }
    _save_json(CIRCUIT_BREAKER, state)
    logger.warning(f"⛔ CIRCUIT BREAKER ACTIVATED — drawdown {drawdown_pct:.1f}%")


def clear_circuit_breaker(cleared_by: str, reason: str = "") -> None:
    """
    Clear the circuit breaker (call after Board vote approves resumption).
    cleared_by: the agent or board member authorizing the reset.
    """
    state = _load_json(CIRCUIT_BREAKER, {})
    if not state.get("active"):
        logger.info("Circuit breaker was not active — nothing to clear.")
        return
    state["active"]     = False
    state["cleared_at"] = datetime.now(timezone.utc).isoformat()
    state["cleared_by"] = cleared_by
    state["clear_reason"] = reason
    _save_json(CIRCUIT_BREAKER, state)
    logger.info(f"✅ Circuit breaker cleared by {cleared_by}")


def run_position_risk_check() -> dict:
    """
    Main entrypoint: check all positions for stop-loss violations,
    check portfolio drawdown circuit breaker.
    Returns summary dict.
    """
    positions = get_positions_with_entry()
    drawdown_30d = check_portfolio_drawdown()

    breaches  = []
    ok_count  = 0

    for pos in positions:
        ticker  = pos["ticker"]
        pnl_pct = pos["pnl_pct"]
        sector  = pos.get("sector", "DEFAULT")
        threshold = SECTOR_STOP_LOSS.get(sector, SECTOR_STOP_LOSS["DEFAULT"])

        if pnl_pct <= threshold:
            breaches.append({
                "ticker":    ticker,
                "pnl_pct":  pnl_pct,
                "threshold": threshold,
                "sector":    sector,
                "entry":     pos["entry_price"],
                "current":   pos["current_price"],
            })
            logger.warning(f"  ⚠️ STOP-LOSS BREACH: {ticker} {pnl_pct:.1f}% (threshold {threshold}%) sector={sector}")
        else:
            ok_count += 1

    # ── Circuit breaker check ─────────────────────────────────────────────
    cb_activated_now = False
    if drawdown_30d is not None and drawdown_30d <= CIRCUIT_BREAKER_THRESHOLD:
        if not is_circuit_breaker_active():
            _activate_circuit_breaker(drawdown_30d)
            cb_activated_now = True
    elif drawdown_30d is not None and drawdown_30d > CIRCUIT_BREAKER_THRESHOLD + 2.0:
        # Auto-clear if recovered by 2%+ above threshold (e.g. >-6% after -8% triggered)
        state = _load_json(CIRCUIT_BREAKER, {})
        if state.get("active") and state.get("cleared_at") is None:
            logger.info(f"  Portfolio recovered to {drawdown_30d:.1f}% — circuit breaker can be manually cleared.")

    # ── Persist risk log ──────────────────────────────────────────────────
    log_entry = {
        "checked_at":       datetime.now(timezone.utc).isoformat(),
        "positions_checked":len(positions),
        "ok_count":         ok_count,
        "breaches":         breaches,
        "drawdown_30d":     drawdown_30d,
        "circuit_breaker":  is_circuit_breaker_active(),
        "cb_activated_now": cb_activated_now,
    }
    log = _load_json(POSITION_RISK_LOG, [])
    log.append(log_entry)
    log = log[-500:]    # keep last 500 checks
    _save_json(POSITION_RISK_LOG, log)

    # ── Alert via agent_meeting if breaches or CB triggered ──────────────
    if breaches or cb_activated_now:
        try:
            from agent_meeting import request_info, post_bulletin
            alert_lines = ["⚠️ POSITION RISK ALERT"]
            if cb_activated_now:
                alert_lines.append(f"  🛑 CIRCUIT BREAKER ACTIVATED: portfolio {drawdown_30d:.1f}% 30d drawdown")
                alert_lines.append(f"     New position entries FROZEN until Board vote.")
            for b in breaches:
                alert_lines.append(
                    f"  {b['ticker']}: {b['pnl_pct']:.1f}% (stop at {b['threshold']}%, sector={b['sector']})"
                )
            alert_body = "\n".join(alert_lines)

            # Notify Manager Agent and CROAgent
            for target in ["ManagerAgent", "CROAgent"]:
                request_info(
                    from_agent   = "PositionRiskMonitor",
                    to_agent     = target,
                    topic        = "Stop-Loss Breach Alert" if breaches else "Circuit Breaker Activated",
                    context      = alert_body,
                    urgency      = "HIGH" if breaches else "CRITICAL",
                    callback_hint= "Review and act on stop-loss breaches immediately",
                )

            if cb_activated_now:
                post_bulletin(
                    author    = "PositionRiskMonitor",
                    topic     = "risk",
                    title     = "⛔ CIRCUIT BREAKER ACTIVATED",
                    body      = alert_body,
                    urgency   = "CRITICAL",
                    ttl_hours = 168.0,
                )
        except Exception as e:
            logger.error(f"  Alert dispatch failed: {e}")

    return log_entry


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s  %(levelname)-8s  %(message)s")
        from agent_inbox import main_inbox_only
        main_inbox_only("PositionRiskMonitor")
        sys.exit(0)

    if "--clear-cb" in sys.argv:
        who = sys.argv[sys.argv.index("--clear-cb") + 1] if len(sys.argv) > sys.argv.index("--clear-cb") + 1 else "manual"
        clear_circuit_breaker(who, "Manual clear via CLI")
        sys.exit(0)

    if "--status" in sys.argv:
        state = _load_json(CIRCUIT_BREAKER, {})
        log   = _load_json(POSITION_RISK_LOG, [])
        print(f"Circuit breaker: {'ACTIVE' if state.get('active') else 'INACTIVE'}")
        if state.get("active"):
            print(f"  Activated: {state.get('activated_at')} ({state.get('drawdown_pct')}%)")
        if log:
            last = log[-1]
            print(f"Last check: {last['checked_at']}")
            print(f"  Positions: {last['positions_checked']} | Breaches: {len(last['breaches'])} | Drawdown: {last['drawdown_30d']}%")
        sys.exit(0)

    result = run_position_risk_check()
    logger.info(
        f"Risk check complete: {result['positions_checked']} positions, "
        f"{len(result['breaches'])} breaches, "
        f"drawdown={result['drawdown_30d']}%, "
        f"CB={'ACTIVE' if result['circuit_breaker'] else 'inactive'}"
    )

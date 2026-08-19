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

SHADOW BY DEFAULT (2026-08-19). This module is now wired into fi_pipeline as
Step 9 and runs every session, but it does not transmit unless the Chairman has
armed it. Unarmed, it does every gate, every sizing calculation and every price
lookup, then writes the order it WOULD have placed to data/fi_shadow_orders.json
instead of sending it. Same gate design as kalshi_client.place_order(): a file
the Chairman creates, never a flag an agent can flip.

    arm:    echo '{"armed": true, "account": "paper"}' > data/.fi_armed
    disarm: rm data/.fi_armed

Wiring and arming were deliberately separated, because they had been conflated.
Until 2026-08-14 this docstring claimed "called from fi_pipeline.py, Step 9";
fi_pipeline had 8 steps and had never imported this module, and no crontab on
either host ran it. With the wrong picks key, that was three independent reasons
the FI pod placed no orders — fi_execution_log.json ends 2026-06-16. "Should we
trade the FI book?" was left unanswered for two months by machinery that was
merely broken, which is the worst way to not make a decision.

So the wiring is fixed and the decision is now a real one, taken on evidence:
run shadow, read fi_shadow_orders.json, and arm when the orders it proposes look
like orders you would have placed.

That evidence matters more than it sounds. On 2026-08-18 the credit monitor
published a 10-year Treasury yield of 100% from a vendor placeholder, with three
BUY signals computed off it, and the pipeline abstained only because a different
feed happened to fail in the same minute. An executor armed that morning would
have traded on it. The FI book's inputs earn arming; they are not owed it.

Output: data/fi_execution_log.json  (live)
        data/fi_shadow_orders.json  (unarmed — what it would have done)
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
SHADOW_FILE     = DATA_DIR / "fi_shadow_orders.json"
ARM_FILE        = DATA_DIR / ".fi_armed"
CANARY_FILE     = DATA_DIR / "canary_state.json"
IRM_FILE        = DATA_DIR / "irm_daily_check.json"
CIT_HR_MEMO     = DATA_DIR / "cit_hr_memo.json"

FI_CAPITAL = 30_000.0


class FINotArmed(RuntimeError):
    """Raised if a transmit path is reached without the Chairman's arm file."""


def is_armed() -> bool:
    """True only if data/.fi_armed exists and explicitly authorises the account.

    Deliberately a file the Chairman creates, and deliberately account-specific,
    so that arming the paper book can never silently arm a live one. Any parse
    failure, any missing key, any unexpected value reads as NOT armed: the
    default has to be the safe one, because this is the branch that spends money.

    Note the shape of the check. It asks whether authorisation is present, not
    whether it is absent — `presence-is-not-currency` cuts the other way here,
    and the file is the currency.
    """
    try:
        d = json.loads(ARM_FILE.read_text())
    except Exception:
        return False
    return bool(d.get("armed")) and d.get("account") == "paper"

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

    armed = is_armed()
    result = {
        "run_at":    datetime.now(timezone.utc).isoformat(),
        "armed":     armed,
        "submitted": [],
        "shadow":    [],
        "skipped":   [],
        "errors":    [],
    }
    logger.info("  mode: %s", "ARMED — orders will be transmitted" if armed else
                "SHADOW — proposed orders recorded, nothing transmitted "
                f"(arm with {ARM_FILE})")

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

    # fi_board writes the approved list under "approved". This read used
    # "picks", so it resolved to [] for every board session that has ever run,
    # logged "No approved picks to execute", and returned — which is why the FI
    # execution log ends on 2026-06-16. Every reader now goes through the one
    # loader that knows the key, and that loader raises on a shape it does not
    # recognise rather than handing back an empty list.
    from fi_data_layer import load_approved_envelope, load_approved_picks
    try:
        picks    = load_approved_picks(APPROVED_FILE)
        envelope = load_approved_envelope(APPROVED_FILE)
    except Exception as e:
        logger.error(f"Could not read fi_approved_picks.json: {e}")
        result["errors"].append(str(e))
        return result

    if not picks:
        logger.info("No approved picks to execute")
        return result

    # Check approval freshness — only execute picks approved today. Undatable is
    # treated as stale: this used to be `if generated_at:` over a debug-swallowed
    # parse, so a missing or malformed timestamp meant no freshness check at all
    # and an arbitrarily old board mandate executed at full size. An executor
    # that cannot date its mandate must not act on it.
    generated_at = envelope.get("generated_at", "")
    try:
        gen_dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
    except Exception as e:
        msg = (f"Approved picks carry no usable 'generated_at' "
               f"({generated_at!r}: {e}) — refusing to execute an undatable mandate")
        logger.error(msg)
        result["skipped"].append({"reason": msg})
        return result

    if age_h > 26:
        msg = f"Approved picks are {age_h:.1f}h old — skipping stale execution"
        logger.warning(msg)
        result["skipped"].append({"reason": msg})
        return result

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

        # ── The arm gate ─────────────────────────────────────────────────────
        # Everything above this line ran: market hours, canary, IRM, duplicate,
        # position cap, cash floor, sizing, live price. That is the point — the
        # shadow record is only worth reading if it is the decision the armed
        # executor would genuinely have made, not a sketch of one.
        if not armed:
            shadow_entry = {
                "ticker": ticker, "date": today, "qty": qty,
                "price_est": price, "cost_est": actual_cost,
                "target_usd": target_usd, "conviction": conviction,
                "verdict": verdict, "reason": reason,
                "would_submit_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(f"  [SHADOW] would BUY {qty} x {ticker} @ ~${price:.2f} "
                        f"= ~${actual_cost:,.2f}  [{verdict} conv={conviction}]")
            result["shadow"].append(shadow_entry)
            available_cash -= actual_cost      # so later picks size honestly
            continue

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
    _save_shadow(result)
    _finalize(result)
    return result


def _save_shadow(result: dict) -> None:
    """Append this session's proposed orders to the shadow ledger.

    Appended, never overwritten. The whole value of shadow mode is the run of
    sessions: one day's proposal says nothing, twenty days of them say whether
    the FI pod's judgement is worth arming. An overwriting writer would leave
    the Chairman deciding on a sample of one.
    """
    proposed = result.get("shadow", [])
    if not proposed:
        return
    try:
        with open(SHADOW_FILE) as f:
            ledger = json.load(f)
        if not isinstance(ledger, list):
            ledger = []
    except Exception:
        ledger = []

    ledger.append({
        "run_at": result.get("run_at"),
        "armed":  False,
        "orders": proposed,
        "notional": round(sum(o.get("cost_est", 0.0) for o in proposed), 2),
    })
    tmp = SHADOW_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(ledger[-500:], f, indent=2)
    tmp.replace(SHADOW_FILE)
    logger.info("  %d proposed order(s) recorded to %s", len(proposed), SHADOW_FILE.name)


def _finalize(result: dict) -> None:
    """Buffer a brief execution summary into the evening email digest."""
    submitted = result.get("submitted", [])
    shadow    = result.get("shadow", [])
    skipped   = result.get("skipped", [])
    errors    = result.get("errors", [])
    armed     = result.get("armed", False)

    # The mode goes in the first line the Chairman reads. A summary saying
    # "Orders submitted: 0" is true in both modes and means opposite things —
    # nothing to do, versus nothing allowed. `verdict-with-no-consequence`.
    lines = [
        "APEX CAPITAL — FI EXECUTION SUMMARY",
        "=" * 45,
        f"Run at: {result.get('run_at', 'unknown')}",
        f"Mode:   {'ARMED (live paper orders)' if armed else 'SHADOW (nothing transmitted)'}",
        "",
        f"Orders submitted: {len(submitted)}",
    ]
    for ex in submitted:
        lines.append(
            f"  ✅ {ex['ticker']:6s} {ex['qty']} shares @ ~${ex['price_est']:.2f}"
            f" = ~${ex['cost_est']:,.0f}  [{ex['verdict']}]"
        )
    if shadow:
        notional = sum(o.get("cost_est", 0.0) for o in shadow)
        lines.append(f"\nProposed but NOT sent ({len(shadow)}, ~${notional:,.0f} notional):")
        for o in shadow:
            lines.append(
                f"  ○ {o['ticker']:6s} {o['qty']} shares @ ~${o['price_est']:.2f}"
                f" = ~${o['cost_est']:,.0f}  [{o['verdict']}]"
            )
        lines.append("\n  Arming is a Chairman action:")
        lines.append("      echo '{\"armed\": true, \"account\": \"paper\"}' > data/.fi_armed")
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
        f"  FI Executor done [{'ARMED' if armed else 'SHADOW'}] — "
        f"submitted: {len(submitted)}, proposed: {len(shadow)}, "
        f"skipped: {len(skipped)}, errors: {len(errors)}\n"
        f"{'='*60}"
    )


if __name__ == "__main__":
    run_fi_executor()

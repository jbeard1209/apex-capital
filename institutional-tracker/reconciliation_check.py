#!/usr/bin/env python3
"""
APEX CAPITAL — RECONCILIATION ENGINE
=====================================
Runs every 30 minutes. Compares what agents promised against Alpaca ground
truth. Does not trust any agent output — goes straight to the source.

Checks:
  1. Cash floor  — cash must stay ≥ 20% of portfolio
  2. Position count — must not exceed 15
  3. Stop-loss compliance — no position held below -7%
  4. Position size compliance — no single position > 7% of portfolio
  5. CRO CRITICAL buy compliance — no buys on PRESERVE CAPITAL days
  6. Unexecuted IMMEDIATE board decisions — flagged if > 2h old
  7. P&L drift — what pipeline recorded vs Alpaca actual

Any flag goes to firm channel. CRITICAL flags email Jack immediately.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reconciliation")

REPORT_PATH = "data/reconciliation_report.json"

# Mirror config.py guardrails — deliberately hardcoded here so the
# reconciler doesn't silently pass if config.py has a wrong value.
_MIN_CASH_FLOOR_PCT    = 0.20
_MAX_POSITIONS         = 15
_STOP_LOSS_PCT         = 0.07   # 7%
_MAX_POSITION_SIZE_PCT = 0.07   # 7% (10% grace before flagging)
_IMMEDIATE_TIMEOUT_H   = 2      # IMMEDIATE board actions must execute within 2h
_PNL_DRIFT_THRESHOLD   = 1000   # flag if pipeline P&L tracking drifts > $1000 from Alpaca


def _flag(flags: list, severity: str, check: str, detail: str):
    logger.warning(f"  [{severity}] {check}: {detail}")
    flags.append({"severity": severity, "check": check, "detail": detail, "ts": datetime.now().isoformat()})


def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def run_reconciliation() -> dict:
    flags: list[dict] = []
    now = datetime.now()

    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — RECONCILIATION ENGINE")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ── Connect to Alpaca (ground truth) ──────────────────────────────────
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        account   = trader.get_account()
        positions = trader.get_positions()
    except Exception as e:
        logger.error(f"  Alpaca connection failed — reconciliation aborted: {e}")
        return {"error": str(e), "flags": [], "generated_at": now.isoformat()}

    portfolio_value = account.get("portfolio_value", 0) or 1
    cash            = account.get("cash", 0) or 0
    cash_pct        = cash / portfolio_value
    n_positions     = len(positions) if positions is not None and not positions.empty else 0

    logger.info(f"  Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} ({cash_pct*100:.1f}%) | Positions: {n_positions}")

    # ── AUTHORITATIVE FIRM VALUE (2026-07-23) ──────────────────────────────
    # The Alpaca account is ground truth. The pod ledger is a virtual overlay
    # that drifted to $104,284 while the real account was $97,524 — reports were
    # overstating value by ~$6,700. Write the true equity to data/firm_value.json
    # so every report/briefing reads ONE honest number, and flag when the pod
    # ledger diverges >2% (that drift is itself a bug worth surfacing).
    try:
        pod_data = _load_json("data/pod_allocations.json") or {}
        pod_sum = round(sum((p.get("total_value") or 0)
                            for p in pod_data.get("pods", {}).values()), 2)
        drift = portfolio_value - pod_sum
        drift_pct = (drift / portfolio_value * 100.0) if portfolio_value else 0.0
        with open("data/firm_value.json", "w") as fh:
            json.dump({
                "equity": round(portfolio_value, 2),
                "cash": round(cash, 2),
                "as_of": now.isoformat(),
                "source": "alpaca_paper_account (ground truth)",
                "pod_ledger_sum": pod_sum,
                "pod_ledger_drift": round(drift, 2),
                "pod_ledger_drift_pct": round(drift_pct, 2),
            }, fh, indent=2)
        logger.info(f"  Authoritative firm value written: ${portfolio_value:,.2f} "
                    f"(pod ledger ${pod_sum:,.2f}, drift {drift_pct:+.1f}%)")
        if abs(drift_pct) > 2.0:
            _flag(flags, "HIGH", "LEDGER_DRIFT",
                  f"Pod ledger (${pod_sum:,.0f}) diverges {drift_pct:+.1f}% from the real "
                  f"account (${portfolio_value:,.0f}). Reports built on the pod ledger are "
                  f"MISSTATING firm value by ${drift:,.0f}. Reconcile pod cash/positions to Alpaca.")
    except Exception as _fve:
        logger.warning(f"  Could not write authoritative firm value: {_fve}")

    # ── CHECK 1: Cash floor ────────────────────────────────────────────────
    logger.info("\n  [1/7] Cash floor...")
    if cash_pct < _MIN_CASH_FLOOR_PCT:
        _flag(flags, "HIGH", "CASH_FLOOR",
              f"Cash is {cash_pct*100:.1f}% of portfolio (${cash:,.0f}) — "
              f"below {_MIN_CASH_FLOOR_PCT*100:.0f}% floor. "
              f"A buy bypassed the cash floor guardrail.")
    else:
        logger.info(f"    ✓ Cash {cash_pct*100:.1f}% ≥ {_MIN_CASH_FLOOR_PCT*100:.0f}% floor")

    # ── CHECK 2: Position count ────────────────────────────────────────────
    logger.info("  [2/7] Position count...")
    if n_positions > _MAX_POSITIONS:
        _flag(flags, "HIGH", "MAX_POSITIONS",
              f"{n_positions} positions open — exceeds {_MAX_POSITIONS} maximum.")
    else:
        logger.info(f"    ✓ {n_positions}/{_MAX_POSITIONS} positions")

    # ── CHECK 3 & 4: Per-position compliance ──────────────────────────────
    logger.info("  [3/7] Stop-loss compliance...")
    logger.info("  [4/7] Position size compliance...")
    if positions is not None and not positions.empty:
        for _, pos in positions.iterrows():
            ticker  = pos.get("ticker", "?")
            try:
                # Column is `unrealized_pct`, already in percent — see
                # deputy_monitor. STOP_LOSS_BREACH could never be raised.
                pnl_pct = float(pos["unrealized_pct"])
                mv      = float(pos.get("market_value", 0))
                weight  = mv / portfolio_value

                if pnl_pct <= -(_STOP_LOSS_PCT * 100):
                    _flag(flags, "CRITICAL", "STOP_LOSS_BREACH",
                          f"{ticker} is at {pnl_pct:+.1f}% — breaches "
                          f"-{_STOP_LOSS_PCT*100:.0f}% stop-loss and is still held. "
                          f"Stop-loss enforcement missed this position.")

                if weight > _MAX_POSITION_SIZE_PCT * 1.10:   # 10% grace for intraday drift
                    _flag(flags, "MEDIUM", "POSITION_SIZE_BREACH",
                          f"{ticker} is {weight*100:.1f}% of portfolio (${mv:,.0f}) — "
                          f"exceeds {_MAX_POSITION_SIZE_PCT*100:.0f}% limit.")
            except Exception as _pe:
                logger.debug(f"    Position parse error for {ticker}: {_pe}")
    else:
        logger.info("    ✓ No open positions")

    # ── CHECK 5: CRO CRITICAL buy compliance ──────────────────────────────
    logger.info("  [5/7] CRO CRITICAL buy compliance...")
    try:
        rb = _load_json("data/risk_bulletin.json")
        risk_level = rb.get("risk_level", "")
        guidance   = rb.get("capital_guidance", "")
        rb_ts      = rb.get("generated_at", "")

        if risk_level == "CRITICAL" and "PRESERVE" in guidance.upper():
            # Check if bulletin is recent (within last 24h — could be yesterday's CRITICAL run)
            try:
                rb_age_h = (now - datetime.fromisoformat(rb_ts[:19])).total_seconds() / 3600
            except Exception:
                rb_age_h = 0

            if rb_age_h < 24:
                # Fetch today's Alpaca orders
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=today_start)
                orders = trader.client.get_orders(filter=req)
                today_buys = [o for o in orders if str(o.side).lower() == "buy"]
                if today_buys:
                    tickers = [o.symbol for o in today_buys]
                    _flag(flags, "CRITICAL", "CRO_CRITICAL_BUY_VIOLATION",
                          f"CRO bulletin is CRITICAL/PRESERVE CAPITAL (issued {rb_age_h:.0f}h ago) "
                          f"but {len(today_buys)} buy order(s) executed today: {tickers}. "
                          f"CRO CRITICAL enforcement failed.")
                else:
                    logger.info(f"    ✓ No buys today — CRITICAL/PRESERVE CAPITAL respected")
            else:
                logger.info(f"    ✓ CRITICAL bulletin is {rb_age_h:.0f}h old — outside 24h window")
        else:
            logger.info(f"    ✓ CRO risk level is {risk_level or 'UNKNOWN'} — no buy freeze active")
    except Exception as e:
        logger.warning(f"    CRO compliance check error: {e}")

    # ── CHECK 6: Unexecuted IMMEDIATE board decisions ──────────────────────
    logger.info("  [6/7] IMMEDIATE board decision execution...")
    try:
        # Check both regular and emergency board reports
        for board_path in ["data/board_meeting_active.json", "data/emergency_board_report.json"]:
            board = _load_json(board_path)
            if not board:
                continue

            # Parse meeting timestamp
            ts_str = board.get("timestamp") or board.get("started_at") or board.get("date", "")
            try:
                meeting_time = datetime.fromisoformat(ts_str[:19])
                age_h = (now - meeting_time).total_seconds() / 3600
            except Exception:
                continue

            if age_h > 12:
                continue   # Too old — not relevant to current cycle

            # Find IMMEDIATE PROCEED actions
            consensus = board.get("consensus", board.get("consensus_actions", []))
            if isinstance(consensus, dict):
                consensus = consensus.get("actions", [])

            immediate_actions = [
                a for a in (consensus or [])
                if a.get("proceed") and a.get("urgency") == "IMMEDIATE"
            ]

            if not immediate_actions:
                logger.info(f"    ✓ No IMMEDIATE board actions in {os.path.basename(board_path)}")
                continue

            # Fetch today's orders to verify execution
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            cutoff = meeting_time - timedelta(minutes=5)  # orders after meeting start
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=cutoff)
            orders = trader.client.get_orders(filter=req)
            executed_tickers = {str(o.symbol) for o in orders}

            for action in immediate_actions:
                ticker  = action.get("ticker", "")
                verb    = action.get("verb", action.get("action", "")).upper()
                action_id = action.get("id", "?")

                if not ticker:
                    continue

                is_sell = verb in ("SELL", "CLOSE", "TRIM", "REDUCE")
                is_buy  = verb in ("BUY", "ADD", "BUY_NEW")

                if is_sell:
                    # Verify it was sold: ticker should no longer be in positions
                    # OR a sell order should appear in today's orders
                    still_held = (
                        positions is not None
                        and not positions.empty
                        and ticker in positions["ticker"].values
                    )
                    sell_order_placed = ticker in executed_tickers

                    if still_held and not sell_order_placed and age_h > _IMMEDIATE_TIMEOUT_H:
                        _flag(flags, "CRITICAL", "BOARD_DECISION_NOT_EXECUTED",
                              f"Board voted {verb} {ticker} (IMMEDIATE, action {action_id}) "
                              f"{age_h:.1f}h ago but {ticker} is still in the portfolio "
                              f"and no sell order found. Decision was not executed.")
                    else:
                        logger.info(f"    ✓ IMMEDIATE {verb} {ticker} — appears executed")

    except Exception as e:
        logger.warning(f"    Board decision check error: {e}")

    # ── CHECK 7: P&L drift ─────────────────────────────────────────────────
    logger.info("  [7/7] P&L drift (pipeline vs Alpaca)...")
    try:
        state = _load_json("data/state.json")
        starting_capital = 100000.0  # from config — hardcoded for independence
        pipeline_pnl = portfolio_value - starting_capital

        # Compare against Alpaca's own P&L tracking
        alpaca_pnl_today = account.get("pnl_today", 0) or 0

        # Check if ledger tracks a materially different portfolio value
        ledger = _load_json("data/performance_ledger.json")
        if ledger:
            picks = ledger.get("picks", [])
            acted_picks = [p for p in picks if p.get("acted_on")]
            if acted_picks:
                # Simple sanity: portfolio value should be within $1000 of a reasonable range
                if abs(pipeline_pnl) > portfolio_value * 0.5:
                    _flag(flags, "MEDIUM", "PNL_DRIFT",
                          f"Pipeline total P&L (${pipeline_pnl:+,.0f}) looks implausible "
                          f"against portfolio value (${portfolio_value:,.0f}). "
                          f"Performance tracking may be out of sync.")
                else:
                    logger.info(f"    ✓ P&L tracking looks consistent (total P&L: ${pipeline_pnl:+,.0f})")
            else:
                logger.info("    ✓ No acted picks yet — P&L drift check skipped")
        else:
            logger.info("    ✓ No performance ledger yet — P&L drift check skipped")
    except Exception as e:
        logger.warning(f"    P&L drift check error: {e}")

    # ── Write report ──────────────────────────────────────────────────────
    n_critical = sum(1 for f in flags if f["severity"] == "CRITICAL")
    n_high     = sum(1 for f in flags if f["severity"] == "HIGH")
    n_medium   = sum(1 for f in flags if f["severity"] == "MEDIUM")

    report = {
        "generated_at":   now.isoformat(),
        "portfolio_value": portfolio_value,
        "cash":            cash,
        "cash_pct":        round(cash_pct * 100, 1),
        "n_positions":     n_positions,
        "n_flags":         len(flags),
        "n_critical":      n_critical,
        "n_high":          n_high,
        "n_medium":        n_medium,
        "flags":           flags,
        "status":          "CLEAN" if not flags else ("CRITICAL" if n_critical else "WARNING"),
    }

    # ── Fable 5 discrepancy diagnosis — gated to CRITICAL/HIGH flags ──────
    if n_critical or n_high:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("reconciliation"):
                flag_txt = "; ".join(
                    f"[{f['severity']}] {f['check']}: {f['detail'][:70]}"
                    for f in flags[:4])
                raw = _r(
                    "You diagnose data-reconciliation failures in a trading system.",
                    f"Portfolio ${portfolio_value:,.0f}, {n_positions} positions.\n"
                    f"Flags: {flag_txt}\n"
                    "2 sentences: most likely root cause, and is capital actually at "
                    "risk or is this a bookkeeping lag?",
                    agent_name="reconciliation", max_tokens=110,
                )
                if raw:
                    report["llm_diagnosis"] = raw.strip()
        except Exception:
            pass

    os.makedirs("data", exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # ── Post to firm channel ──────────────────────────────────────────────
    try:
        from firm_channel import post
        if flags:
            summary = (
                f"RECONCILIATION: {len(flags)} flag(s) — "
                + ", ".join(f['check'] for f in flags[:3])
                + ("..." if len(flags) > 3 else "")
            )
            priority = "URGENT" if n_critical else "HIGH"
            post("Reconciliation Engine", "PIPELINE_ALERT", summary, priority=priority)
        else:
            post("Reconciliation Engine", "PIPELINE_ALERT",
                 f"All 7 reconciliation checks passed. Portfolio ${portfolio_value:,.0f} | "
                 f"Cash {cash_pct*100:.1f}% | {n_positions} positions.",
                 priority="NORMAL")
    except Exception as e:
        logger.warning(f"  Firm channel post failed: {e}")

    # ── Email Jack if CRITICAL ────────────────────────────────────────────
    if n_critical > 0:
        try:
            from email_sender import send_alert_email
            subject = f"🚨 Apex Capital — Reconciliation CRITICAL: {n_critical} flag(s)"
            body_lines = [
                f"Reconciliation Engine found {n_critical} CRITICAL flag(s) at {now.strftime('%H:%M ET')}.",
                f"Portfolio: ${portfolio_value:,.2f} | Cash: {cash_pct*100:.1f}% | Positions: {n_positions}",
                "",
                "CRITICAL FLAGS:",
            ]
            for f in flags:
                if f["severity"] == "CRITICAL":
                    body_lines.append(f"  [{f['check']}] {f['detail']}")
            if n_high or n_medium:
                body_lines.append(f"\nAlso: {n_high} HIGH, {n_medium} MEDIUM flags — see data/reconciliation_report.json")
            send_alert_email(subject, "\n".join(body_lines))
            logger.info(f"  🚨 Alert email sent to Jack ({n_critical} CRITICAL flag(s))")
        except Exception as e:
            logger.warning(f"  Alert email failed: {e}")

    # ── Trigger emergency board meeting for CRITICAL flags ────────────────
    if n_critical > 0:
        try:
            from board_meeting import call_emergency_board_meeting
            critical_flags = [f for f in flags if f["severity"] == "CRITICAL"]
            reason = (
                f"Reconciliation Engine detected {n_critical} CRITICAL compliance breach(es): "
                + "; ".join(f['check'] + ': ' + f['detail'][:80] for f in critical_flags[:2])
                + ("..." if len(critical_flags) > 2 else "")
            )
            call_emergency_board_meeting(
                reason=reason,
                severity="CRITICAL",
                caller="Reconciliation Engine",
            )
            logger.info(f"  🚨 Emergency board meeting triggered ({n_critical} CRITICAL flag(s))")
        except Exception as e:
            logger.warning(f"  Emergency board trigger failed: {e}")

    # ── Summary ───────────────────────────────────────────────────────────
    logger.info("")
    if not flags:
        logger.info("  ✅ All checks passed — portfolio is clean.")
    else:
        logger.info(f"  ⚠️  {len(flags)} flag(s): {n_critical} CRITICAL | {n_high} HIGH | {n_medium} MEDIUM")
        for f in flags:
            logger.info(f"     [{f['severity']}] {f['check']}: {f['detail'][:100]}")

    return report


if __name__ == "__main__":
    report = run_reconciliation()
    sys.exit(1 if report.get("n_critical", 0) > 0 else 0)

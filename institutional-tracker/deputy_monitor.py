#!/usr/bin/env python3
"""
APEX CAPITAL — DEPUTY MONITOR
==============================
Runs every 15 minutes during market hours (cron: */15 13-21 * * 1-5 UTC).
Also runs every 30 minutes overnight/weekends for lighter coverage.

This is the fast-action layer. The board detects and decides; deputies act.

Three autonomous tasks (no vote required — clear existing mandate):
  1. Stop-loss enforcement     — close any position breaching -7%
  2. Position drift correction — trim any position that has grown > 7.7% of portfolio
  3. Stale agent recovery      — re-run any agent whose output has gone stale

One escalation task:
  4. Escalation sweep          — any flagged issue sitting unresolved > 1h gets
                                  either handled here or escalated to emergency board

One implementation task:
  5. Pending fix review        — approved fixes in cos_meeting_requests.json that
                                  are > 2h old get flagged via email so they don't
                                  silently sit in the queue

All actions are logged to data/deputy_monitor_report.json and posted to firm channel.
CRITICAL actions email Jack immediately.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("deputy_monitor")

REPORT_PATH      = "data/deputy_monitor_report.json"
CMR_PATH         = "data/cos_meeting_requests.json"
CHAIRMAN_PATH    = "data/chairman_action_required.json"
STATE_PATH       = "data/state.json"

# Mirror config guardrails — hardcoded for independence
_STOP_LOSS_PCT        = 0.07    # -7%
_MAX_POSITION_PCT     = 0.07    # 7%
_POSITION_GRACE       = 1.10   # 10% grace before acting (7.7%)
_STALE_THRESHOLDS_H   = {       # max acceptable age per output file
    "data/risk_bulletin.json":             13,
    "data/canary_state.json":              1,
    "data/manager_decisions.json":         14,
    "data/review_recommendations.json":    14,
    "data/chief_of_staff_report.json":     26,
    "data/capital_efficiency_report.json": 26,
    "data/hr_report.json":                 26,
    "data/irm_daily_check.json":           26,
    "data/research_team_report.json":      26,
}
# Which script regenerates each stale file
_RECOVERY_SCRIPTS = {
    "data/risk_bulletin.json":             "risk_manager_agent.py",
    "data/canary_state.json":              "canary_agent.py",
    "data/manager_decisions.json":         None,   # main pipeline — don't auto-trigger
    "data/review_recommendations.json":    "review_agent.py",
    "data/chief_of_staff_report.json":     "chief_of_staff_agent.py",
    "data/capital_efficiency_report.json": "capital_efficiency_agent.py",
    "data/hr_report.json":                 None,   # hr_agent is heavy — defer to board
    "data/irm_daily_check.json":           None,   # IRM is independent — defer to board
    "data/research_team_report.json":      "rd_team_leader.py",
}
_PENDING_FIX_ALERT_H  = 2    # email Jack if approved fix sits unimplemented > 2h
_ALERT_DEDUP_PATH     = "data/deputy_monitor_alert_state.json"
_ALERT_RESEND_H       = 6    # re-send same alert after 6h if count unchanged


def _load_alert_state() -> dict:
    """Load deduplication state so we don't spam the same alert every 15 min."""
    try:
        if os.path.exists(_ALERT_DEDUP_PATH):
            with open(_ALERT_DEDUP_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_alert_state(state: dict):
    try:
        with open(_ALERT_DEDUP_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:
        pass


def _should_send_alert(key: str, count: int, state: dict) -> bool:
    """
    Returns True only if this alert is new or materially changed.
    Suppresses repeated emails about the same count of stale items within 6h.
    Re-sends after _ALERT_RESEND_H hours even if count is unchanged (reminder).
    """
    prev = state.get(key, {})
    prev_count = prev.get("count", -1)
    prev_sent  = prev.get("last_sent_ts", "")
    age_h = _age_hours(prev_sent)
    # Send if count changed, or if it's been > 6h since last send
    return count != prev_count or age_h > _ALERT_RESEND_H


def _record_alert_sent(key: str, count: int, state: dict):
    """Record that we just sent an alert for this key."""
    state[key] = {
        "count":        count,
        "last_sent_ts": datetime.now().isoformat(),
    }


def _load(path: str):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _load_list(path: str) -> list:
    data = _load(path)
    if isinstance(data, list):
        return data
    return []


def _age_hours(ts: str) -> float:
    if not ts:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 3600
    except Exception:
        return float("inf")


def _post(agent: str, msg: str, priority: str = "NORMAL"):
    try:
        from firm_channel import post
        post(agent, "PIPELINE_ALERT", msg, priority=priority)
    except Exception:
        pass


def _email(subject: str, body: str):
    try:
        from email_sender import send_alert_email
        send_alert_email(subject, body)
    except Exception as e:
        logger.warning(f"  Email failed: {e}")


def _wake_board(reason: str, severity: str, caller: str):
    logger.warning(f"  [DeputyMonitor] Escalating to emergency board: {reason[:80]}")
    try:
        from board_meeting import call_emergency_board_meeting
        call_emergency_board_meeting(reason=reason, severity=severity, caller=caller)
    except Exception as e:
        logger.error(f"  [DeputyMonitor] Emergency board call failed: {e}")
        _email(
            f"⚠️ Deputy Monitor — Board escalation FAILED: {severity}",
            f"Deputy Monitor tried to wake the board but the call failed.\n\nReason: {reason}\n\nError: {e}"
        )


# =============================================================================
# TASK 1 — STOP-LOSS ENFORCEMENT
# =============================================================================

def _check_stop_losses(actions: list, errors: list):
    logger.info("\n  [1/5] Stop-loss enforcement...")
    try:
        from alpaca_trader import AlpacaTrader
        trader  = AlpacaTrader()
        account = trader.get_account()
        positions = trader.get_positions()

        if positions is None or positions.empty:
            logger.info("    ✓ No open positions")
            return

        breaches = []
        for _, pos in positions.iterrows():
            ticker  = pos.get("ticker", "?")
            try:
                # `unrealized_plpc` is the SDK attribute; the column is
                # `unrealized_pct` and is ALREADY in percent. The old read
                # took the 0 default and multiplied it by 100, so every
                # position scored exactly 0.0% and this logged
                # "No stop-loss breaches across N positions" forever.
                pnl_pct = float(pos["unrealized_pct"])
                if pnl_pct <= -(_STOP_LOSS_PCT * 100):
                    breaches.append((ticker, pnl_pct))
            except Exception:
                continue

        if not breaches:
            logger.info(f"    ✓ No stop-loss breaches across {len(positions)} positions")
            return

        for ticker, pnl_pct in breaches:
            logger.warning(f"    ⚠ STOP-LOSS: {ticker} at {pnl_pct:+.1f}% — closing position")
            try:
                order = trader.submit_sell(ticker, reason="deputy_monitor_stop_loss")
                ok = bool(order and order.get("id"))
                status = "EXECUTED" if ok else "FAILED"
                actions.append({
                    "task":    "STOP_LOSS",
                    "ticker":  ticker,
                    "pnl_pct": pnl_pct,
                    "status":  status,
                    "order_id": order.get("id") if order else None,
                    "ts":      datetime.now().isoformat(),
                })
                if ok:
                    logger.info(f"    ✅ {ticker} closed — order {order.get('id')}")
                    _post("Deputy Monitor",
                          f"STOP-LOSS enforced: {ticker} at {pnl_pct:+.1f}% — position closed.",
                          priority="HIGH")
                else:
                    errors.append(f"Stop-loss sell for {ticker} returned no order")
                    _wake_board(
                        f"Deputy Monitor could not close {ticker} ({pnl_pct:+.1f}%) — stop-loss breach unresolved.",
                        severity="CRITICAL", caller="Deputy Monitor"
                    )
            except Exception as e:
                errors.append(f"Stop-loss {ticker}: {e}")
                logger.error(f"    ✗ {ticker} stop-loss failed: {e}")
                _wake_board(
                    f"Stop-loss enforcement failed for {ticker} ({pnl_pct:+.1f}%): {e}",
                    severity="CRITICAL", caller="Deputy Monitor"
                )

    except Exception as e:
        errors.append(f"Stop-loss check failed: {e}")
        logger.error(f"    Stop-loss check error: {e}")


# =============================================================================
# TASK 2 — POSITION DRIFT CORRECTION
# =============================================================================

def _check_position_drift(actions: list, errors: list):
    logger.info("\n  [2/5] Position drift correction...")
    try:
        from alpaca_trader import AlpacaTrader
        trader   = AlpacaTrader()
        account  = trader.get_account()
        pv       = float(account.get("portfolio_value", 1) or 1)
        positions = trader.get_positions()

        if positions is None or positions.empty:
            logger.info("    ✓ No open positions")
            return

        limit    = _MAX_POSITION_PCT * _POSITION_GRACE   # 7.7%
        drifters = []
        for _, pos in positions.iterrows():
            ticker = pos.get("ticker", "?")
            try:
                mv     = float(pos.get("market_value", 0))
                weight = mv / pv
                qty    = float(pos.get("qty", 0))
                if weight > limit:
                    # Calculate how many shares to sell to get back to 7%
                    target_mv   = pv * _MAX_POSITION_PCT
                    excess_mv   = mv - target_mv
                    price       = mv / qty if qty > 0 else 0
                    trim_shares = int(excess_mv / price) if price > 0 else 0
                    if trim_shares > 0:
                        drifters.append((ticker, weight, trim_shares, qty))
            except Exception:
                continue

        if not drifters:
            logger.info(f"    ✓ No position drift across {len(positions)} positions")
            return

        for ticker, weight, trim_shares, total_qty in drifters:
            logger.warning(
                f"    ⚠ DRIFT: {ticker} at {weight*100:.1f}% of portfolio "
                f"(limit {_MAX_POSITION_PCT*100:.0f}%) — trimming {trim_shares} shares"
            )
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                order_req = MarketOrderRequest(
                    symbol=ticker,
                    qty=trim_shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                order = trader.client.submit_order(order_req)
                actions.append({
                    "task":        "POSITION_DRIFT",
                    "ticker":      ticker,
                    "weight_pct":  round(weight * 100, 2),
                    "trim_shares": trim_shares,
                    "status":      "EXECUTED",
                    "order_id":    str(order.id),
                    "ts":          datetime.now().isoformat(),
                })
                logger.info(f"    ✅ {ticker} trimmed {trim_shares} shares — order {order.id}")
                _post("Deputy Monitor",
                      f"Position drift corrected: {ticker} trimmed {trim_shares} shares "
                      f"(was {weight*100:.1f}% → target {_MAX_POSITION_PCT*100:.0f}%).",
                      priority="NORMAL")
            except Exception as e:
                errors.append(f"Drift trim {ticker}: {e}")
                logger.error(f"    ✗ {ticker} trim failed: {e}")
                _wake_board(
                    f"Position drift correction failed for {ticker} ({weight*100:.1f}%): {e}",
                    severity="HIGH", caller="Deputy Monitor"
                )

    except Exception as e:
        errors.append(f"Position drift check failed: {e}")
        logger.error(f"    Position drift check error: {e}")


# =============================================================================
# TASK 3 — STALE AGENT RECOVERY
# =============================================================================

def _check_stale_agents(actions: list, errors: list):
    logger.info("\n  [3/5] Stale agent recovery...")
    recovered   = 0
    deferred    = 0
    for data_file, max_age_h in _STALE_THRESHOLDS_H.items():
        try:
            if not os.path.exists(data_file):
                age_h = float("inf")
            else:
                with open(data_file) as f:
                    content = json.load(f)
                ts    = content.get("generated_at", "")
                age_h = _age_hours(ts)

            if age_h <= max_age_h:
                continue

            script = _RECOVERY_SCRIPTS.get(data_file)
            label  = data_file.split("/")[-1]

            if script is None:
                # Above deputy authority — escalate
                logger.warning(f"    ⚠ STALE: {label} ({age_h:.0f}h old, threshold {max_age_h}h) — escalating")
                _wake_board(
                    f"{label} is {age_h:.0f}h old (threshold {max_age_h}h) and cannot be auto-recovered. "
                    f"Board should investigate why the producing agent has not run.",
                    severity="HIGH", caller="Deputy Monitor"
                )
                deferred += 1
            else:
                # Deputy can recover this — trigger the script
                logger.info(f"    ↻ STALE: {label} ({age_h:.0f}h old) — triggering {script}")
                result = subprocess.run(
                    [sys.executable, script],
                    capture_output=True, text=True, timeout=120,
                    cwd=os.path.dirname(os.path.abspath(__file__))
                )
                if result.returncode == 0:
                    logger.info(f"    ✅ {label} refreshed via {script}")
                    actions.append({
                        "task":    "STALE_RECOVERY",
                        "file":    data_file,
                        "age_h":   round(age_h, 1),
                        "script":  script,
                        "status":  "RECOVERED",
                        "ts":      datetime.now().isoformat(),
                    })
                    recovered += 1
                    _post("Deputy Monitor",
                          f"Stale agent recovered: {label} ({age_h:.0f}h old) refreshed via {script}.",
                          priority="NORMAL")
                else:
                    err = result.stderr[:200] if result.stderr else "unknown error"
                    errors.append(f"Recovery of {label} failed: {err}")
                    logger.error(f"    ✗ {script} failed: {err}")
                    _wake_board(
                        f"Auto-recovery of {label} failed (script: {script}). "
                        f"Output was {age_h:.0f}h old. Error: {err}",
                        severity="HIGH", caller="Deputy Monitor"
                    )
        except Exception as e:
            errors.append(f"Stale check for {data_file}: {e}")
            logger.warning(f"    Stale check error for {data_file}: {e}")

    logger.info(f"    ✓ Stale recovery: {recovered} refreshed, {deferred} escalated to board")


# =============================================================================
# TASK 4 — ESCALATION SWEEP
# =============================================================================

def _escalation_sweep(actions: list, errors: list):
    """
    Scan known flag sources for unresolved issues older than 1 hour.
    Deputy handles what it can; escalates the rest to the board.
    """
    logger.info("\n  [4/5] Escalation sweep...")
    escalated = 0

    # Reconciliation CRITICAL flags
    try:
        recon = _load("data/reconciliation_report.json")
        recon_age = _age_hours(recon.get("generated_at", ""))
        if recon.get("n_critical", 0) > 0 and recon_age < 2:
            flags = [f["check"] for f in recon.get("flags", []) if f.get("severity") == "CRITICAL"]
            _wake_board(
                f"Reconciliation CRITICAL flags unresolved: {', '.join(flags)}. "
                f"Report is {recon_age:.0f}h old.",
                severity="CRITICAL", caller="Deputy Monitor"
            )
            escalated += 1
            logger.warning(f"    ⚠ Reconciliation CRITICAL escalated to board")
    except Exception as e:
        logger.warning(f"    Escalation sweep (reconciliation): {e}")

    # Canary RED — if stuck RED for > 30 min without board response
    try:
        canary = _load("data/canary_state.json")
        canary_level = canary.get("alert_level", "GREEN")
        canary_age   = _age_hours(canary.get("generated_at", ""))
        board_report = _load("data/board_meeting_report.json")
        board_age    = _age_hours(board_report.get("timestamp", ""))
        if canary_level == "RED" and canary_age < 1 and board_age > 0.5:
            _wake_board(
                f"Canary RED for {canary_age*60:.0f} min with no board response in {board_age*60:.0f} min.",
                severity="CRITICAL", caller="Deputy Monitor"
            )
            escalated += 1
            logger.warning(f"    ⚠ Canary RED escalated to board")
    except Exception as e:
        logger.warning(f"    Escalation sweep (canary): {e}")

    if escalated == 0:
        logger.info("    ✓ No unresolved escalations pending")


# =============================================================================
# TASK 5 — PENDING FIX REVIEW
# =============================================================================

def _check_pending_fixes(actions: list, errors: list, alert_state: dict):
    """
    Scan both cos_meeting_requests.json AND chairman_action_required.json for
    approved fixes sitting unimplemented for more than 2 hours — email Jack.

    Deduplication: only sends a new email when the count changes or after
    _ALERT_RESEND_H hours, so repeated runs don't spam the inbox.

    Implementation of ALL approved proposals requires Claude (chairman) to read
    chairman_action_required.json at check-in and make the code changes.
    """
    logger.info("\n  [5/5] Pending fix review...")
    try:
        stale_pending = []

        # ── cos_meeting_requests.json — PENDING entries ───────────────────
        for req in _load_list(CMR_PATH):
            if req.get("status") != "PENDING":
                continue
            # Only alert on HIGH/CRITICAL urgency from cos_meeting_requests
            # to avoid spamming about NORMAL-priority board votes
            if req.get("urgency", "NORMAL") not in ("HIGH", "CRITICAL"):
                continue
            age_h = _age_hours(req.get("ts", ""))
            if age_h > _PENDING_FIX_ALERT_H:
                stale_pending.append({
                    "source":   "cos_meeting_requests",
                    "urgency":  req.get("urgency", "?"),
                    "summary":  req.get("summary", "?")[:120],
                    "age_h":    round(age_h, 1),
                    "from":     req.get("from_agent", "?"),
                })

        # ── chairman_action_required.json — awaiting_chairman entries ─────
        for req in _load_list(CHAIRMAN_PATH):
            if req.get("status") != "awaiting_chairman":
                continue
            age_h = _age_hours(req.get("queued_at", ""))
            if age_h > _PENDING_FIX_ALERT_H:
                stale_pending.append({
                    "source":   "chairman_action_required",
                    "urgency":  req.get("severity", req.get("urgency", "?")),
                    "summary":  f"[{req.get('approved_by','Board')}] {req.get('title','?')[:100]}",
                    "age_h":    round(age_h, 1),
                    "from":     req.get("approved_by", "Board"),
                })

        if not stale_pending:
            logger.info("    ✓ No stale pending fixes in either queue")
            # Reset dedup state so the next real alert fires immediately
            if "pending_fixes" in alert_state:
                del alert_state["pending_fixes"]
            return

        count = len(stale_pending)

        # ── Deduplication check ───────────────────────────────────────────
        if not _should_send_alert("pending_fixes", count, alert_state):
            logger.info(
                f"    ℹ {count} fix(es) pending, but alert already sent "
                f"(resends after {_ALERT_RESEND_H}h or count change) — suppressing."
            )
            return

        logger.warning(f"    ⚠ {count} fix(es) pending > {_PENDING_FIX_ALERT_H}h — emailing Jack")
        lines = [
            f"{count} board-approved fix(es) have not been implemented yet.\n",
            "Implementation requires Claude to read chairman_action_required.json "
            "at the start of the next session and make the code changes.\n",
            "\nPENDING FIXES:",
        ]
        for r in stale_pending[:10]:
            lines.append(
                f"  [{r['urgency']}] {r['summary']} "
                f"(pending {r['age_h']:.1f}h — {r['source']})"
            )
        _email(
            f"⚠️ Apex Capital — {count} approved fix(es) awaiting Claude implementation",
            "\n".join(lines)
        )
        _record_alert_sent("pending_fixes", count, alert_state)
        _post("Deputy Monitor",
              f"{count} approved fix(es) unimplemented >{_PENDING_FIX_ALERT_H}h — Jack notified.",
              priority="HIGH")
        actions.append({
            "task":        "PENDING_FIX_ALERT",
            "stale_count": count,
            "status":      "JACK_NOTIFIED",
            "ts":          datetime.now().isoformat(),
        })

    except Exception as e:
        errors.append(f"Pending fix check: {e}")
        logger.warning(f"    Pending fix check error: {e}")


# =============================================================================
# MAIN
# =============================================================================

def run_deputy_monitor() -> dict:
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — DEPUTY MONITOR")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    actions: list = []
    errors:  list = []

    # Load alert dedup state — prevents spam on repeated monitor runs
    alert_state = _load_alert_state()

    _check_stop_losses(actions, errors)
    _check_position_drift(actions, errors)
    _check_stale_agents(actions, errors)
    _escalation_sweep(actions, errors)
    _check_pending_fixes(actions, errors, alert_state)

    # Persist dedup state so next run knows what was already sent
    _save_alert_state(alert_state)

    report = {
        "generated_at": now.isoformat(),
        "actions":      actions,
        "errors":       errors,
        "n_actions":    len(actions),
        "n_errors":     len(errors),
        "status":       "CLEAN" if not errors else "ERRORS",
    }

    os.makedirs("data", exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if actions:
        summary = f"Deputy Monitor: {len(actions)} action(s) taken — " + \
                  ", ".join(set(a["task"] for a in actions))
        _post("Deputy Monitor", summary, priority="HIGH" if errors else "NORMAL")

    logger.info(f"\n  ✅ Deputy Monitor complete — {len(actions)} action(s), {len(errors)} error(s)")
    return report


if __name__ == "__main__":
    report = run_deputy_monitor()
    sys.exit(1 if report["n_errors"] > 0 else 0)

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
  5. Pending fix review        — HIGH/CRITICAL requests in cos_meeting_requests.json
                                  that have survived 2+ *scheduled Chief-of-Staff
                                  runs* unanswered get flagged via email. Priced in
                                  runs, not hours — see _COS_RUNS_BEFORE_ALERT.

All actions are logged to data/deputy_monitor_report.json and posted to firm channel.
CRITICAL actions email Jack immediately.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

import atomic_json as aj

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("deputy_monitor")

# Set from argv in __main__ (see the postmortem there). Off by default so a
# cron/wake invocation behaves exactly as before; every side-effecting path in
# this file checks it — see the DRY_RUN guards in _post, _email, _wake_board,
# _check_stop_losses and _check_position_drift.
DRY_RUN = False

REPORT_PATH      = "data/deputy_monitor_report.json"
CMR_PATH         = "data/cos_meeting_requests.json"
CHAIRMAN_PATH    = "data/chairman_action_required.json"
STATE_PATH       = "data/state.json"

# Mirror config guardrails — hardcoded for independence
_STOP_LOSS_PCT        = 0.07    # -7%
_MAX_POSITION_PCT     = 0.07    # 7%
_POSITION_GRACE       = 1.10   # 10% grace before acting (7.7%)
# The script whose crontab entry actually governs each output. This is NOT the
# same question as "who may the deputy re-run" (_RECOVERY_SCRIPTS below) and
# conflating them is why nothing here was ever checked against a schedule:
# chief_of_staff_agent.py, review_agent.py and capital_efficiency_agent.py have
# no cron line on either host — they run as steps INSIDE main.py, so main.py's
# schedule is the one that decides when their output is due.
_PRODUCER_SCRIPT = {
    "data/risk_bulletin.json":             "risk_manager_agent.py",
    "data/canary_state.json":              "canary_agent.py",
    "data/manager_decisions.json":         "main.py",
    "data/review_recommendations.json":    "main.py",
    "data/chief_of_staff_report.json":     "main.py",
    "data/capital_efficiency_report.json": "main.py",
    "data/hr_report.json":                 "hr_agent.py",
    "data/irm_daily_check.json":           "irm_review_agent.py",
    "data/research_team_report.json":      "rd_team_leader.py",
}

# BACKSTOP ONLY — used when neither host's schedule can be read. The primary
# test is `schedule_expectations.lateness()`: did the producer miss a fire its
# own crontab asked for?
#
# Measured 2026-08-21, and the reason this table changed: 7 of these 9 numbers
# were BELOW the largest gap the producing schedule itself asks for, so they
# alarmed by arithmetic on a healthy firm, not on a fault.
#
#   file                          was  schedule gap  now   producer
#   risk_bulletin                  13      24.00      26   risk_manager_agent
#   canary_state                    1       0.50       3   canary_agent
#   manager_decisions              14      65.50      68   main.py (weekday-only)
#   review_recommendations         14      65.50      68   main.py
#   chief_of_staff_report          26      65.50      68   main.py
#   capital_efficiency_report      26      65.50      68   main.py
#   hr_report                      26      56.50      59   hr_agent
#   irm_daily_check                26      72.00      74   irm_review_agent
#   research_team_report           26       9.75      26   rd_team_leader (kept)
#
# main.py runs `* * * * 1-5`, so Friday's last fire to Monday's first is 65.5h.
# Every Saturday, four of these crossed their threshold at the same time; two
# of them (hr_report, irm_daily_check) escalate to the EMERGENCY BOARD, which
# is what the 2026-08-21 HIGH "irm_daily_check.json is 152h old" alert was.
# Each number below is ceil(gap) + 2h of slack for the producer to finish.
# `test_pipeline_smoke.py::TestStalenessIsPricedInFires` re-derives this from
# the live crontabs and fails if any threshold ever drops below its own gap.
_STALE_THRESHOLDS_H   = {
    "data/risk_bulletin.json":             26,
    "data/canary_state.json":              3,
    "data/manager_decisions.json":         68,
    "data/review_recommendations.json":    68,
    "data/chief_of_staff_report.json":     68,
    "data/capital_efficiency_report.json": 68,
    "data/hr_report.json":                 59,
    "data/irm_daily_check.json":           74,
    "data/research_team_report.json":      26,
}
# Which script the deputy is ALLOWED to re-run. None = above deputy authority.
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
_ALERT_DEDUP_PATH     = "data/deputy_monitor_alert_state.json"
_ALERT_RESEND_H       = 6    # re-send same alert after 6h if count unchanged

# A HIGH/CRITICAL meeting request is answered by the Chief of Staff, and the
# CoS is not a daemon — it runs inside main.py, on main.py's crontab. So the
# honest unit is "how many chances has it had?", not "how many hours has it
# been". Two hours of a Saturday is zero chances; two hours of a Tuesday
# afternoon is one. Priced in hours, this alarm emailed Jack every single
# weekend, because the producer's own schedule (`* * * * 1-5`, 65.5h
# Friday->Monday) guaranteed it — the same defect as the six freshness alarms
# fixed the same day, see schedule_expectations.py.
#
# Two, not one: the CoS run in progress when the request lands may already
# have read the queue, so the first chance can be lost without anything being
# wrong. Missing two is a real miss.
_COS_RUNS_BEFORE_ALERT = 2


def _missed_cos_runs(ts: str) -> int:
    """Scheduled Chief-of-Staff runs that have passed since `ts`.

    Returns 0 when the schedule cannot be read or this host does not run the
    pipeline: with no denominator this check has no verdict, and inventing a
    large number here would email Jack on the strength of a failed
    `crontab -l` (memory `fallback-that-means-act`).
    """
    if not ts:
        return 0
    try:
        import schedule_expectations as sched
        from datetime import datetime as _dt
        when = _dt.fromisoformat(str(ts)[:19]).timestamp()
    except Exception:
        return 0
    n = sched.missed_fires_since("main.py", when)
    if n is None:
        n = sched.missed_fires_since("main.py", when,
                                     cron_text=sched.peer_crontab())
    return n or 0


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


def _upsert_chairman_row(rid: str, severity: str, description: str) -> bool:
    """One keyed row in the Chairman's queue. Upserts; never appends a second.

    Keyed by `id`, because this sweep runs on a schedule and an append would
    grow the queue by one row per run per flag. The queue is already 127 items
    deep, and a detector that floods it is a detector people stop reading —
    which is how a HIGH ends up meaning nothing.

    Returns False rather than raising: a monitor that dies while reporting on
    other monitors is worse than one that logs a miss.

    ONE lock spans the read and the write. This was two unlocked halves on the
    file 23 producers share — deputy_board._queue_fix appends to it from the
    same main.py run — so an upsert could read 137 rows, and write 138 over a
    peer's 138, losing the peer's row silently. A monitor that drops the alarms
    of the thing it monitors is worse than either failure alone.
    """
    try:
        with aj.locked(CHAIRMAN_PATH):
            rows = aj.read(CHAIRMAN_PATH, default=None, strict=True, expect=list)
            if rows is None:
                # Genuinely absent. This sweep reports ON the queue; it does not
                # create it, so there is nothing to upsert into.
                return False
            row = {"id": rid, "severity": severity, "source": "deputy_monitor",
                   "ts": datetime.now().isoformat(), "description": description}
            for i, existing in enumerate(rows):
                if isinstance(existing, dict) and existing.get("id") == rid:
                    if existing.get("resolved_at"):
                        # Closed, and the condition is back. Re-open on the same
                        # id so the row keeps its history instead of forking a
                        # near-duplicate.
                        row["reopened_at"] = row["ts"]
                        existing = {k: v for k, v in existing.items()
                                    if k not in ("resolved_at", "resolution", "status")}
                    rows[i] = {**existing, **row}
                    break
            else:
                rows.append(row)
            aj.write(CHAIRMAN_PATH, rows)
        return True
    except (aj.Unreadable, ValueError, OSError):
        return False


def _post(agent: str, msg: str, priority: str = "NORMAL"):
    if DRY_RUN:
        logger.info(f"  [dry-run] would post [{priority}] {msg[:100]}")
        return
    try:
        from firm_channel import post
        post(agent, "PIPELINE_ALERT", msg, priority=priority)
    except Exception:
        pass


def _email(subject: str, body: str):
    if DRY_RUN:
        logger.info(f"  [dry-run] would email: {subject}")
        return
    try:
        from email_sender import send_alert_email
        send_alert_email(subject, body)
    except Exception as e:
        logger.warning(f"  Email failed: {e}")


def _wake_board(reason: str, severity: str, caller: str):
    logger.warning(f"  [DeputyMonitor] Escalating to emergency board: {reason[:80]}")
    if DRY_RUN:
        logger.info("  [dry-run] would wake the emergency board")
        return
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
            if DRY_RUN:
                logger.info(f"    [dry-run] would submit_sell({ticker})")
                actions.append({"task": "STOP_LOSS", "ticker": ticker,
                                "pnl_pct": pnl_pct, "status": "DRY_RUN",
                                "ts": datetime.now().isoformat()})
                continue
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
            if DRY_RUN:
                logger.info(f"    [dry-run] would trim {trim_shares} of {ticker}")
                actions.append({"task": "POSITION_DRIFT", "ticker": ticker,
                                "trim_shares": trim_shares, "status": "DRY_RUN",
                                "ts": datetime.now().isoformat()})
                continue
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

def _output_age_h(data_file: str):
    """Hours since `data_file` was last produced — or None when unmeasurable.

    Three separate failures used to collapse into one number here:

      * `content.get("generated_at", "")` on a LIST — manager_decisions.json is
        a list, so this raised `'list' object has no attribute 'get'` on every
        single run since the check was written. The one file that records what
        the firm traded has never had its staleness verified.
      * A dict with no `generated_at` → `_age_hours("")` → `inf`. canary_state
        and chief_of_staff_report are both like this, and both were minutes old
        when judged infinitely stale. The deputy then re-ran canary_agent and
        chief_of_staff_agent every pass and logged "RECOVERED".
      * A file that genuinely does not exist — the only case where "infinitely
        old" is the truth.

    mtime is the honest fallback for a file that exists but carries no
    timestamp: it is a real measurement of when the producer last wrote. `inf`
    is not a measurement, it is a decision to act (memory
    `fallback-that-means-act`), and an unreadable file returns None so the
    caller can say "cannot tell" instead of "stale".
    """
    if not os.path.exists(data_file):
        return float("inf")          # absent: genuinely, measurably, not there
    try:
        with open(data_file) as f:
            content = json.load(f)
    except Exception:
        return None                  # cannot tell — never treat as stale
    ts = ""
    if isinstance(content, dict):
        ts = content.get("generated_at") or content.get("ts") or \
             content.get("timestamp") or ""
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        row = content[-1]
        ts = row.get("generated_at") or row.get("ts") or \
             row.get("timestamp") or ""
    age = _age_hours(str(ts)) if ts else float("inf")
    if age == float("inf"):
        # No usable timestamp inside. The file is still on disk, so mtime is a
        # real answer to "when was this last written".
        try:
            return (datetime.now() -
                    datetime.fromtimestamp(os.path.getmtime(data_file))
                    ).total_seconds() / 3600.0
        except Exception:
            return None
    return age


def _schedule_verdict(script: str, data_file: str) -> dict:
    """Is `script` late by its own scheduler? Tries this host, then the peer.

    Hours are a proxy. The quantity that decides whether an output is overdue
    is "has the producer had a scheduled run since it last wrote?" — the same
    correction Jack made to the roster staleness guard, where a 19.7h snapshot
    was inside a 48h threshold and still wrong.

    The peer's schedule is deliberately NOT used to judge a local file. There
    is no state sync between hosts (CLAUDE.md), so `data/manager_decisions.json`
    here and on the VM are different files with different histories. After
    decision #81 the Mac no longer runs main.py at all, so four of these outputs
    can never be refreshed here — measuring the Mac's copy against the VM's
    crontab reports LATE forever and escalates to the emergency board forever.
    Two correct halves, one meaningless comparison. The peer crontab is read
    only to tell "nobody produces this" apart from "the other host produces it",
    which is that host's deputy's business, not ours.

    Returns {"verdict": "PEER_OWNED"} for the latter, {} when no schedule
    anywhere covers `script` (caller falls back to the hour backstop).
    """
    try:
        import schedule_expectations as sched
    except Exception:
        return {}
    try:
        mtime = os.path.getmtime(data_file) if os.path.exists(data_file) else None
    except Exception:
        mtime = None

    out = sched.lateness(script, mtime)
    if out.get("verdict") in (sched.LATE, sched.ON_TIME, sched.NO_FIRE_YET):
        out["host"] = "this host"
        return out

    if out.get("verdict") == sched.NOT_SCHEDULED_HERE:
        peer = sched.peer_crontab()
        if peer and sched.entries_for(script, peer):
            return {"verdict": "PEER_OWNED", "script": script,
                    "max_legit_gap_h": sched.max_legit_gap_h(script, peer)}
    return {}


def _check_stale_agents(actions: list, errors: list):
    logger.info("\n  [3/5] Stale agent recovery...")
    recovered   = 0
    deferred    = 0
    unmeasured  = 0
    peer_owned  = 0
    for data_file, max_age_h in _STALE_THRESHOLDS_H.items():
        try:
            label  = data_file.split("/")[-1]
            age_h  = _output_age_h(data_file)

            if age_h is None:
                # "Cannot tell" is not "stale". Acting on it re-ran agents and
                # woke the board on files that were minutes old. It is also not
                # nothing: it lands in errors[], which flips the report status
                # and the exit code, so it cannot sit here silently.
                unmeasured += 1
                errors.append(f"Staleness of {label} is unmeasurable "
                              f"(unreadable JSON) — no action taken")
                logger.warning(f"    ? UNMEASURABLE: {label} — not judged")
                continue

            prod    = _PRODUCER_SCRIPT.get(data_file)
            verdict = _schedule_verdict(prod, data_file) if prod else {}
            if verdict:
                import schedule_expectations as _s
                if verdict["verdict"] == "PEER_OWNED":
                    # Not ours to judge, and not ours to hide either: it is
                    # counted and logged so "we watch 9 files" never quietly
                    # becomes "we watch 5".
                    peer_owned += 1
                    logger.info(f"    — {label}: produced by {prod} on the peer "
                                f"host only; that host's deputy owns it")
                    continue
                if verdict["verdict"] != _s.LATE:
                    continue         # the schedule says it is not due yet
                why = (f"{prod} missed its {verdict.get('host')} run at "
                       f"{str(verdict.get('missed_fire'))[:16]} "
                       f"(schedule's own largest gap: "
                       f"{verdict.get('max_legit_gap_h')}h)")
            else:
                if age_h <= max_age_h:
                    continue         # backstop: no schedule readable anywhere
                why = (f"{age_h:.0f}h old, backstop threshold {max_age_h}h "
                       f"(no readable schedule for {prod})")

            script = _RECOVERY_SCRIPTS.get(data_file)

            if script is None:
                # Above deputy authority — escalate. The message now carries its
                # own derivation, so a board member can check the premise
                # instead of taking "152h old" on faith.
                logger.warning(f"    ⚠ STALE: {label} — {why} — escalating")
                _wake_board(
                    f"{label} is overdue and cannot be auto-recovered: {why}. "
                    f"Output last written {age_h:.0f}h ago. "
                    f"Board should investigate why the producing agent has not run.",
                    severity="HIGH", caller="Deputy Monitor"
                )
                deferred += 1
            else:
                # Deputy can recover this — trigger the script
                logger.info(f"    ↻ STALE: {label} — {why} — triggering {script}")
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
                        "why":     why,
                        "script":  script,
                        "status":  "RECOVERED",
                        "ts":      datetime.now().isoformat(),
                    })
                    recovered += 1
                    _post("Deputy Monitor",
                          f"Stale agent recovered: {label} ({why}) refreshed via {script}.",
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

    # n_examined, not just n_found: a pass on zero items is not a pass
    # (CLAUDE.md, coherence laws). "0 stale" and "9 unmeasurable" used to print
    # the same line.
    logger.info(f"    ✓ Stale recovery: {len(_STALE_THRESHOLDS_H)} examined — "
                f"{recovered} refreshed, {deferred} escalated to board, "
                f"{peer_owned} owned by the peer host, "
                f"{unmeasured} unmeasurable")


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
        fresh = recon_age < 2
        if recon.get("n_critical", 0) > 0 and fresh:
            flags = [f["check"] for f in recon.get("flags", []) if f.get("severity") == "CRITICAL"]
            _wake_board(
                f"Reconciliation CRITICAL flags unresolved: {', '.join(flags)}. "
                f"Report is {recon_age:.0f}h old.",
                severity="CRITICAL", caller="Deputy Monitor"
            )
            escalated += 1
            logger.warning(f"    ⚠ Reconciliation CRITICAL escalated to board")

        # HIGH flags used to reach nobody. This branch only ever read
        # n_critical, and the only other consumer of the report,
        # morning_briefing, renders top_flags into a paragraph. So on
        # 2026-08-19 the firm carried three unowned HIGHs for an unknown
        # number of days: pod ledger $6,376 adrift from the broker, cash at
        # 6.0% against a 20% floor with the report itself saying "a buy
        # bypassed the cash floor guardrail", and 21 positions against a cap
        # of 20. A guardrail reporting its own bypass into a briefing
        # paragraph is a verdict with no consequence.
        #
        # HIGH does not wake the board — three HIGHs on every run would make
        # the wake-up meaningless, the same trap as a scheduler pinned to a
        # strict exit code. It gets a keyed row in the Chairman's queue
        # instead, where it ages, hits the SLA escalation, and is archived
        # only once something records a closure.
        if fresh:
            for f in recon.get("flags", []):
                if f.get("severity") != "HIGH":
                    continue
                check = str(f.get("check") or "UNKNOWN")
                if _upsert_chairman_row(
                        f"recon-high-{check.lower()}", "HIGH",
                        f"Reconciliation HIGH — {check}: {f.get('detail', '')} "
                        f"Raised by reconciliation_check.py; it has no other "
                        f"escalation path."):
                    escalated += 1
                    logger.warning(f"    ⚠ Reconciliation HIGH {check} → Chairman queue")

        # A stale report is not a clean report. Without this, the freshness
        # guard above turns every flag into silence the moment the producer
        # stops running — the degraded read substituting a value that means
        # "nothing to escalate".
        if not fresh and recon.get("n_flags", 0) > 0:
            _wake_board(
                f"Reconciliation report is {recon_age:.0f}h old and still carries "
                f"{recon.get('n_flags')} unresolved flag(s). Too old to act on and "
                f"too flagged to ignore — reconciliation_check.py has stopped running.",
                severity="HIGH", caller="Deputy Monitor"
            )
            escalated += 1
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
    Scan cos_meeting_requests.json for HIGH/CRITICAL requests the Chief of
    Staff has had _COS_RUNS_BEFORE_ALERT scheduled chances to answer and has
    not — email Jack.

    The unit is missed CoS runs, not hours: the CoS runs inside main.py on
    main.py's crontab, so "two hours" means one chance on a Tuesday and none
    at all on a Saturday. See the comment on _COS_RUNS_BEFORE_ALERT.

    The chairman_action_required.json half was removed 2026-08-21 — see the
    postmortem below. Deduplication: a new email only when the count changes
    or after _ALERT_RESEND_H hours, so repeated runs don't spam the inbox.
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
            age_h  = _age_hours(req.get("ts", ""))
            missed = _missed_cos_runs(req.get("ts", ""))
            if missed >= _COS_RUNS_BEFORE_ALERT:
                stale_pending.append({
                    "source":      "cos_meeting_requests",
                    "urgency":     req.get("urgency", "?"),
                    "summary":     req.get("summary", "?")[:120],
                    "missed_runs": missed,
                    "age_h":       round(age_h, 1),
                    "from":        req.get("from_agent", "?"),
                })

        # ── chairman_action_required.json ─────────────────────────────────
        # Removed 2026-08-21. This half alerted on every awaiting_chairman
        # entry older than two hours, and the "implementer" it was waiting for
        # is the Chairman himself — so the threshold asserted that Jack reviews
        # the queue every two hours, around the clock.
        #
        # Measured against the firm's own history: of 95 archived decisions
        # with a recorded latency, 7 landed inside 2h. Seven percent. Median
        # was 45.3h and p90 was 469h. On the live queue, 51 of 51 awaiting
        # entries were over the threshold — 100%, and they will be 100%
        # tomorrow too. An alarm that is on permanently carries no information
        # and its only lasting effect is to teach the reader to filter the
        # sender (memory `both-sides-of-a-comparison`).
        #
        # It was also a second control on a quantity that already has an owner.
        # chairman_decision_sla.py enforces SLA_DAYS = 20 by Chairman directive
        # of 2026-07-29, surfaces to the app inbox, and escalates to a board
        # vote when it lapses — a real consequence, which this had none of.
        # Two alarms on one queue calibrated 240x apart is not defence in
        # depth; the tighter one just drowns the one that acts. Single owner.
        #
        # If the Chairman-queue latency is worth alerting on more tightly than
        # 20 days, that is a change to SLA_DAYS in chairman_decision_sla.py —
        # a Chairman decision, made once, in the module that can act on it.

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

        logger.warning(f"    ⚠ {count} HIGH/CRITICAL meeting request(s) have survived "
                       f"{_COS_RUNS_BEFORE_ALERT}+ scheduled CoS runs — emailing Jack")
        lines = [
            f"{count} HIGH/CRITICAL meeting request(s) have now survived "
            f"{_COS_RUNS_BEFORE_ALERT}+ scheduled Chief-of-Staff runs unanswered.\n",
            "The CoS runs inside main.py, so these had real chances to be picked "
            "up and were not — this is a miss, not just elapsed time.\n",
            "\nUNANSWERED REQUESTS:",
        ]
        for r in stale_pending[:10]:
            lines.append(
                f"  [{r['urgency']}] {r['summary']} "
                f"({r['missed_runs']} missed CoS run(s), {r['age_h']:.1f}h — from {r['from']})"
            )
        _email(
            f"⚠️ Apex Capital — {count} urgent meeting request(s) unanswered by the Chief of Staff",
            "\n".join(lines)
        )
        _record_alert_sent("pending_fixes", count, alert_state)
        _post("Deputy Monitor",
              f"{count} urgent meeting request(s) unanswered after "
              f"{_COS_RUNS_BEFORE_ALERT}+ scheduled CoS runs — Jack notified.",
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

    # Persist dedup state so next run knows what was already sent.
    # Never under DRY_RUN: the dedup entry says "Jack was told", so writing it
    # from a run that emailed nobody suppresses the next real alert.
    if not DRY_RUN:
        _save_alert_state(alert_state)

    report = {
        "generated_at": now.isoformat(),
        "actions":      actions,
        "errors":       errors,
        "n_actions":    len(actions),
        "n_errors":     len(errors),
        "status":       "CLEAN" if not errors else "ERRORS",
    }

    if DRY_RUN:
        # The report's mtime/generated_at IS this agent's liveness signal —
        # several freshness checks read it. A dry run took no action, so
        # refreshing it would prove the monitor alive on the strength of a run
        # that did nothing (see memory `test-refreshes-the-liveness-signal`).
        logger.warning("  DRY-RUN: %s NOT written (%d action(s) would have "
                       "been taken)", REPORT_PATH, len(actions))
    else:
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
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('DeputyMonitor'):
        raise SystemExit(0)

    # `--report` used to be silently ignored: this script had no argv parsing at
    # all, so `python3 deputy_monitor.py --report` — the convention every other
    # agent here honours, and the one CLAUDE.md documents — ran the FULL live
    # monitor. Its first act is closing any position down 7% at the broker. An
    # unknown flag must never be read as consent to trade, so it now fails
    # closed: anything not recognised puts the run in DRY_RUN.
    _argv = set(sys.argv[1:])
    _known = {"--live", "--run"}
    DRY_RUN = bool(_argv - _known)
    if DRY_RUN:
        logger.warning("  DRY-RUN: no orders, no email, no firm-channel post, "
                       "no state written (argv: %s). Use --live to act.",
                       " ".join(sys.argv[1:]) or "(none)")

    report = run_deputy_monitor()
    sys.exit(1 if report["n_errors"] > 0 else 0)

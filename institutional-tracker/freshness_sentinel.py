#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — FRESHNESS SENTINEL
# =============================================================================
# Chairman directive 2026-06-12: "data is often stale or something didn't run
# and it didn't get flagged."
#
# One cheap watchdog, run hourly by cron, that:
#   1. Knows the expected max age of every critical data file (manifest below)
#   2. Detects "pipeline started but never completed" via pipeline_heartbeat
#      (the exact failure mode that went unnoticed for 2 days in June 2026:
#       libomp crash killed main.py mid-run, heartbeat had start but no finish)
#   3. Alerts ONLY on transitions (fresh→stale), with a 12h cooldown per item —
#      no alert spam, but nothing goes silently dark either
#   4. Auto-remediates: safe producers are re-run once per cooldown window
#   5. Writes data/freshness_report.json (read by morning briefing + dashboards)
#
# No LLM calls — this must be bulletproof and free.
#
# Usage:  python3 freshness_sentinel.py [--no-fix] [--no-email]
# =============================================================================

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("freshness_sentinel")

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
STATE_PATH  = DATA_DIR / "freshness_state.json"     # sentinel's own memory
REPORT_PATH = DATA_DIR / "freshness_report.json"    # for briefing/dashboard

#: Latest-state freshness snapshot. Each scan rewrites the full picture of
#: tracked artifacts; the morning briefing reads the current state. There is
#: no missed-generation harm — a sentinel reads the newest state, not a log.
_WIRING_SNAPSHOT = {"freshness_report.json"}

ALERT_COOLDOWN_H = 12     # one alert + one auto-fix attempt per item per 12h
MARKET_DAYS_ONLY = {0, 1, 2, 3, 4}   # Mon-Fri

# =============================================================================
# MANIFEST — every critical artifact, its freshness budget, and (optionally)
# the command that regenerates it. `fix` commands must be idempotent and safe
# to run at any hour. `weekdays_only` files aren't expected to update weekends.
# =============================================================================
# `owner` is the host whose scheduler actually PRODUCES the artifact. It is
# derived, not assumed: the Mac's launchd runs exactly two jobs, and
# `catch_up.py`'s JOB_REGISTRY (post decision #81, which removed
# pipeline_0945/1200/1400/1615) runs only premarket_check, morning_briefing,
# board_meeting, rd_team_leader, cit_director ×2, cit_pipeline_integrity,
# coherence_laws and internal_audit. Everything the daily pipeline writes is
# therefore produced on the VM alone, and the Mac's untouched copy is a
# leftover, not a stale artifact.
#
# This mattered more than the queues did. Measured 2026-08-28 the Mac reported
# 16 stale items — 5 of them CRITICAL — and 13 of those were 0.4–1.2h old on
# the VM at the same instant. The sentinel had been red every hour since #81
# narrowed the Mac, which is the failure Jack named: an alarm nobody can act on
# is an alarm everybody learns to close, and it was burying the one entry below
# that is genuinely dead on BOTH hosts.
VM = "apexcapitalc"

MANIFEST = {
    # ── Core pipeline ──────────────────────────────────────────────────────
    "data/manager_decisions.json":       {"max_age_h": 26, "critical": True,  "weekdays_only": True,
                                          "owner": VM},
    "data/review_recommendations.json":  {"max_age_h": 26, "critical": True,  "weekdays_only": True,
                                          "owner": VM},
    "data/state.json":                   {"max_age_h": 26, "critical": True,  "weekdays_only": True,
                                          "owner": VM},
    # ── Risk stack ─────────────────────────────────────────────────────────
    "data/risk_bulletin.json":           {"max_age_h": 26, "critical": True, "owner": VM,
                                          "fix": "risk_manager_agent.py"},
    "data/canary_state.json":            {"max_age_h": 14, "critical": True, "owner": VM,
                                          "fix": "canary_agent.py"},
    "data/irm_daily_check.json":         {"max_age_h": 30, "critical": True, "owner": VM,
                                          "fix": "irm_review_agent.py --no-email"},
    # No owner: catch_up runs premarket_check on the Mac and cron runs it on the VM.
    "data/premarket_alerts.json":        {"max_age_h": 30, "critical": False, "weekdays_only": True},
    # ── Governance / oversight ─────────────────────────────────────────────
    "data/hr_report.json":               {"max_age_h": 30, "critical": False, "owner": VM,
                                          "fix": "hr_agent.py"},
    "data/chief_of_staff_report.json":   {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "owner": VM},
    "data/logic_team_findings.json":     {"max_age_h": 16, "critical": False, "owner": VM,
                                          "fix": "logic_team.py"},
    "data/cit_pending_proposals.json":   {"max_age_h": 50, "critical": False},
    "data/morning_briefing.json":        {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "fix": "morning_briefing.py"},
    # ── Pods ───────────────────────────────────────────────────────────────
    "data/fi_pipeline_report.json":      {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "owner": VM, "fix": "fi_pipeline.py"},
    "data/fe_pipeline_log.json":         {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "owner": VM},
    "data/etf_research_brief.json":      {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "owner": VM, "fix": "etf_research_agent.py"},
    "data/etf_pod_orders.json":          {"max_age_h": 54, "critical": False, "weekdays_only": True,
                                          "owner": VM},
    # ── Research ───────────────────────────────────────────────────────────
    "data/research_team_report.json":    {"max_age_h": 100, "critical": False},
    "data/sector_assignments.json":      {"max_age_h": 100, "critical": False, "owner": VM},
    "data/missed_winners_report.json":   {"max_age_h": 200, "critical": False, "owner": VM,
                                          "fix": "missed_winners_agent.py"},
    # ── ML ─────────────────────────────────────────────────────────────────
    "data/ml_live_picks_log.json":       {"max_age_h": 54, "critical": False, "weekdays_only": True,
                                          "owner": VM},
    # 2320h (96 days) old on the VM as well as the Mac — the one entry in this
    # block that is a REAL dead producer rather than an ownership artifact. It
    # was indistinguishable from the 13 false alarms until they were removed.
    "data/data_quality_report.json":     {"max_age_h": 54, "critical": False, "owner": VM},
    # Slow gate (full test suite dual-zone, ~4.5h). Chairman directive
    # 2026-09-08 required a scheduled runner off the push path AND wiring the
    # report into this manifest, so a red slow gate escalates through the same
    # freshness path as any other production failure. VM cron runs it at 03:30
    # UTC on Sun + Tue-Sat (skipping Monday's ML experiment window). Budget
    # 30h gives a nightly cadence one full miss before firing.
    "data/slow_gate_report.json":        {"max_age_h": 30, "critical": False, "owner": VM,
                                          "fix": "slow_gate.py", "verdict_key": "ok"},

    # ── Queues ─────────────────────────────────────────────────────────────
    # Added 2026-08-28. Until then this manifest watched 21 reports and exactly
    # ONE queue (cit_pending_proposals), which left the firm's whole class of
    # "the drainer died" failures unwatched: a report going stale means a
    # producer stopped, but a QUEUE going stale while it holds items means work
    # was accepted and never done, and nothing was looking for that.
    #
    # TWO THINGS HAD TO BE RIGHT BEFORE THESE COULD BE ADDED AT ALL.
    #
    # 1. `owner`. There is no state sync between the Mac and the VM, so each
    #    host has its own private copy of every one of these files, and the
    #    copy belonging to a host that does not run the drainer is not stale —
    #    it is IRRELEVANT, and it has been sitting untouched since the day it
    #    was last written. Measured 2026-08-28, same instant, both hosts:
    #
    #        queue                        Mac        VM
    #        fe_picks_queue            1331.8h     14.2h
    #        ml_pod_pending             805.4h     13.4h
    #        hr_action_queue            633.1h     89.7h
    #        cit_implementation_queue   209.8h      0.5h
    #        fi_picks_queue             164.1h     14.9h
    #        rd_pod_pending             100.0h     13.3h
    #
    #    Adding these flat would have fired six permanent CRITICALs on the Mac
    #    that are six healthy queues on the VM. An alarm that is always on is
    #    not a control; it trains everyone to close the tab, and it would have
    #    buried the three below that are REAL. So each entry names the host
    #    that owns it and is only measured there. See `two_brains_one_account`:
    #    host identity is persisted (`single_writer.this_host()`), never
    #    inferred from what happens to be present on disk.
    #
    # 2. `queue`. For a queue, age alone is the wrong quantity — an empty queue
    #    that nobody has touched for a week is a queue with nothing to do, and
    #    alarming on it measures the arrival rate of work rather than the
    #    health of the worker. What matters is age AND depth: items accepted
    #    and not drained. The other half of the pair — a queue that is empty
    #    because its PRODUCER died — is `wiring_audit.py`'s empty-queue sweep,
    #    which is why this file does not try to cover it and why neither is
    #    sufficient alone.
    #
    # Three were already failing when this was written, which is the point:
    #   fe_research_queue          2427.4h with 126 items  (101 days)
    #   ml_shortterm_experiment_q  1347.4h with   6 items
    #   ml_longterm_experiment_q   1346.4h with  13 items
    # The two ML queues are the ones the Chairman-approved crontab batch of
    # 2026-08-28 scheduled runners for; if those Monday jobs work, these clear
    # themselves, and if they do not, this manifest is now what says so.
    #
    # Budgets are ~3x the observed healthy interval on the owning host, which
    # is a cadence measurement rather than a round number, and they are meant
    # to be re-derived once each drainer's schedule is pinned.
    "data/cit_implementation_queue.json": {"max_age_h": 12, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/rd_pod_pending.json":           {"max_age_h": 48, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/ml_pod_pending.json":           {"max_age_h": 48, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/fi_picks_queue.json":           {"max_age_h": 48, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/fe_picks_queue.json":           {"max_age_h": 48, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/hr_action_queue.json":          {"max_age_h": 200, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/fe_research_queue.json":        {"max_age_h": 200, "critical": False,
                                           "owner": "apexcapitalc", "queue": True},
    "data/ml_shortterm_experiment_queue.json": {"max_age_h": 200, "critical": False,
                                               "owner": "apexcapitalc", "queue": True},
    "data/ml_longterm_experiment_queue.json":  {"max_age_h": 200, "critical": False,
                                               "owner": "apexcapitalc", "queue": True},
    # Written on both hosts, so no owner.
    "data/digest_queue.json":             {"max_age_h": 72, "critical": False, "queue": True},
    "data/chairman_decision_queue.json":  {"max_age_h": 26, "critical": True,  "queue": True},
    # DELIBERATELY ABSENT: data/fi_pod_pending.json. It is 1826.5h old on BOTH
    # hosts, which is not a stale queue — it is a queue with no consumer at
    # all, already reported by wiring_audit and routed away by
    # `cit_director._effective_pod()`. Listing it here would add a permanent
    # alarm for a known, decided condition, and a permanently-red row is how a
    # panel stops being read.
}


def _owner_of(rel: str) -> Optional[str]:
    return MANIFEST.get(rel, {}).get("owner")


def _this_host() -> str:
    """Persisted host identity, never derived from what is on disk.

    Falls back to "" — which owns nothing and therefore DEFERS everything
    rather than measuring everything. A degraded read of "which host am I"
    must not resolve to "I am the owner of all of it"; that is the
    `fallback-that-means-act` shape, where the substituted constant is itself
    the decision.
    """
    try:
        from single_writer import this_host
        return this_host() or ""
    except Exception:
        return ""


def _queue_depth(path: Path) -> Optional[int]:
    """Rows held by a queue file, or None if it cannot be counted.

    None is NOT zero. It means "I could not count this", and the caller keeps
    the staleness rather than resolving to the reassuring answer.

    The dict branch returns None when no list-shaped payload is found, instead
    of falling back to `len(d)`. A wrapper like fi_picks_queue.json —
    {generated_at, capital, regime, spreads, market_signals, picks} — has six
    top-level keys and that number is not a depth; reporting it would give the
    gate a value that is never 0 and therefore never right.
    """
    try:
        d = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        for k in ("items", "queue", "pending", "entries", "picks",
                  "proposals", "requests", "experiments"):
            v = d.get(k)
            if isinstance(v, list):
                return len(v)
        return None
    return None


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _age_hours(path: Path) -> float | None:
    try:
        return (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _check_heartbeat() -> dict | None:
    """Detect 'pipeline started but never completed' — the silent-death mode.

    Host-scoped for the same reason the manifest is. The heartbeat is written
    by main.py, so it belongs to whichever host runs main.py — and that is not
    a second hardcoded fact here, it is read off `data/state.json`'s declared
    owner, main.py's other output. On a host that no longer runs the pipeline
    the local heartbeat is frozen at the last run it ever did (the Mac's still
    reads 2026-08-14, the crash that decision #81 was the answer to), so a
    naive check reports a two-week-old death as today's news, forever.
    """
    owner = _owner_of("data/state.json")
    if owner and owner != _this_host():
        return None
    hb = _load(DATA_DIR / "pipeline_heartbeat.json", {})
    started   = hb.get("last_started")
    completed = hb.get("last_completed")
    pid       = hb.get("pid")
    if not started:
        return None
    try:
        t_start = datetime.fromisoformat(str(started)[:19])
    except Exception:
        return None
    # Completed run that finished after it started → healthy
    if completed:
        try:
            if datetime.fromisoformat(str(completed)[:19]) >= t_start:
                return None
        except Exception:
            pass
    # Started >2h ago, no completion, and the process is gone → it died mid-run
    if datetime.now() - t_start > timedelta(hours=2) and not _pid_alive(pid):
        return {
            "item":    "pipeline_heartbeat (mid-run death)",
            "status":  "DEAD_MID_RUN",
            "detail":  (f"main.py started {started} (pid {pid}) but never wrote "
                        f"a completion marker and the process is gone. The pipeline "
                        f"crashed mid-run — agents scheduled after the crash point "
                        f"did NOT run this cycle."),
            "critical": True,
        }
    return None


def _attempt_fix(script: str) -> str:
    """Re-run a producer in a subprocess. Returns outcome string."""
    try:
        cmd = [sys.executable] + script.split()
        logger.info("    AUTO-FIX: running %s …", script)
        r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True,
                           text=True, timeout=900)
        return "fixed (exit 0)" if r.returncode == 0 else f"fix failed (exit {r.returncode})"
    except subprocess.TimeoutExpired:
        return "fix timed out (15m)"
    except Exception as e:
        return f"fix error: {e}"


def run(auto_fix: bool = True, send_email: bool = True) -> dict:
    now      = datetime.now()
    weekday  = now.weekday() in MARKET_DAYS_ONLY
    sentinel = _load(STATE_PATH, {})          # item → {status, last_alert, last_fix}
    issues, fixed, ok_count = [], [], 0
    host     = _this_host()
    deferred = []          # artifacts another host owns — NOT measured here
    measured = 0           # what this run actually looked at (never len(MANIFEST))

    # ── 1. File freshness ──────────────────────────────────────────────────
    for rel, spec in MANIFEST.items():
        if spec.get("weekdays_only") and not weekday:
            continue

        # Owned by the other host: this host's copy is a private, unsynced
        # leftover (no state sync between hosts), so its age measures nothing.
        # Record it by name so the denominator stays visible — an item that
        # silently vanishes from the sweep is indistinguishable from one that
        # passed, which is how a control ends up reporting on fewer things
        # than anyone believes it covers.
        owner = spec.get("owner")
        if owner and owner != host:
            deferred.append({"item": rel, "owner": owner,
                             "measured_here": False,
                             "reason": f"owned by {owner}; this host is {host or 'UNKNOWN'}"})
            continue

        measured += 1
        path    = BASE_DIR / rel
        age     = _age_hours(path)
        max_age = spec["max_age_h"]
        stale   = age is None or age > max_age

        # A queue is stale only if it is old AND still holding work. An old
        # EMPTY queue is a queue with nothing to do — alarming on it measures
        # how often work arrives, not whether the worker is alive. (The
        # opposite failure, a queue empty because its PRODUCER died, is
        # wiring_audit's empty-queue sweep.) An unreadable queue is NOT empty:
        # depth None keeps the staleness, because "I could not count it" must
        # not resolve to the reassuring answer.
        depth = None
        if spec.get("queue"):
            depth = _queue_depth(path)
            if stale and depth == 0:
                stale = False

        # A fresh report can still say "I failed". Until 2026-09-30 this loop
        # read only the mtime, so slow_gate_report.json — wired in here so "a
        # red slow gate escalates" — was counted OK every night it was red.
        # `verdict_key` names a field that must be literally True; a missing or
        # unreadable verdict is not a pass.
        red = False
        if not stale and spec.get("verdict_key"):
            payload = _load(path, None)
            red = not (isinstance(payload, dict)
                       and payload.get(spec["verdict_key"]) is True)

        prev = sentinel.get(rel, {})
        if not stale and not red:
            ok_count += 1
            if prev.get("status") in ("STALE", "RED"):
                logger.info("  RECOVERED: %s (%.0fh old)", rel, age)
            sentinel[rel] = {"status": "OK"}
            continue

        status = "RED" if red else "STALE"
        if red:
            detail = (f"fresh ({age:.0f}h) but reports "
                      f"{spec['verdict_key']}={payload.get(spec['verdict_key']) if isinstance(payload, dict) else 'unreadable'}")
        else:
            detail = (f"missing" if age is None else f"{age:.0f}h old (budget {max_age}h)")
        if spec.get("queue"):
            detail += (" — holding an uncountable number of items" if depth is None
                       else f" — still holding {depth} item(s)")
        issue = {"item": rel, "status": status, "detail": detail,
                 "critical": spec.get("critical", False)}
        if spec.get("queue"):
            issue["queue_depth"] = depth

        # Cooldown: only alert/fix once per window per item
        last_action = prev.get("last_action")
        in_cooldown = False
        if last_action:
            try:
                in_cooldown = (now - datetime.fromisoformat(last_action)) < timedelta(hours=ALERT_COOLDOWN_H)
            except Exception:
                pass

        if not in_cooldown:
            sentinel[rel] = {"status": status, "last_action": now.isoformat()}
            issue["new_alert"] = True
            # Re-running the producer cures staleness, not a red verdict.
            if auto_fix and spec.get("fix") and not red:
                outcome = _attempt_fix(spec["fix"])
                issue["auto_fix"] = outcome
                if outcome.startswith("fixed"):
                    fixed.append(rel)
        else:
            sentinel[rel] = {**prev, "status": status}

        issues.append(issue)
        logger.warning("  %s%s: %s — %s%s", status,
                       " [CRITICAL]" if issue["critical"] else "",
                       rel, detail,
                       f" | auto-fix: {issue.get('auto_fix','-')}" if issue.get("auto_fix") else "")

    # ── 2. Mid-run pipeline death ──────────────────────────────────────────
    hb_issue = _check_heartbeat()
    if hb_issue:
        prev = sentinel.get("heartbeat", {})
        last_action = prev.get("last_action")
        in_cooldown = False
        if last_action:
            try:
                in_cooldown = (now - datetime.fromisoformat(last_action)) < timedelta(hours=ALERT_COOLDOWN_H)
            except Exception:
                pass
        if not in_cooldown:
            hb_issue["new_alert"] = True
            sentinel["heartbeat"] = {"status": "DEAD", "last_action": now.isoformat()}
        issues.append(hb_issue)
        logger.error("  %s", hb_issue["detail"])
    else:
        sentinel["heartbeat"] = {"status": "OK"}

    # ── 3. Report + escalation ─────────────────────────────────────────────
    new_alerts = [i for i in issues if i.get("new_alert")]
    crit_new   = [i for i in new_alerts if i.get("critical")]

    report = {
        "generated_at": now.isoformat(),
        "agent":        "Freshness Sentinel",
        # `checked` used to be len(MANIFEST), which counted the weekday-skipped
        # items as if they had passed. It is now what was actually measured on
        # THIS host, with the skipped population named separately, so the
        # denominator can never quietly exceed the work done.
        "checked":      measured,
        "in_manifest":  len(MANIFEST),
        "host":         host or "UNKNOWN",
        "deferred":     deferred,
        "deferred_n":   len(deferred),
        "ok":           ok_count,
        "stale":        len(issues),
        "auto_fixed":   fixed,
        "issues":       issues,
        "overall":      "CRITICAL" if any(i.get("critical") for i in issues)
                         else ("DEGRADED" if issues else "HEALTHY"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))
    STATE_PATH.write_text(json.dumps(sentinel, indent=2))

    # Firm channel: post only when there is something new to say
    if new_alerts or fixed:
        try:
            from firm_channel import post
            parts = []
            if fixed:
                parts.append(f"auto-fixed {len(fixed)}: {', '.join(Path(f).name for f in fixed)}")
            if new_alerts:
                parts.append("newly stale: " + ", ".join(
                    f"{Path(i['item']).name} ({i['detail'][:30]})" for i in new_alerts[:5]))
            post("Freshness Sentinel", "OPS_ALERT",
                 f"🕐 FRESHNESS: {'; '.join(parts)}",
                 priority="URGENT" if crit_new else "NORMAL")
        except Exception:
            pass

    # Email: ONLY for new CRITICAL transitions (heartbeat death or critical file)
    if send_email and crit_new:
        try:
            from email_sender import send_alert_email
            rows = "".join(
                f"<li><b>{i['item']}</b>: {i['detail']}"
                + (f" — auto-fix: {i.get('auto_fix','not attempted')}" if 'auto_fix' in i or True else "")
                + "</li>"
                for i in crit_new)
            send_alert_email(
                subject=f"🕐 Freshness Sentinel: {len(crit_new)} CRITICAL stale item(s)",
                body=(f"<h3>Critical data freshness failures</h3><ul>{rows}</ul>"
                      f"<p>Auto-fixes were attempted where safe. Full report: "
                      f"data/freshness_report.json</p>"
                      f"<p><i>You will not be re-alerted for these items for "
                      f"{ALERT_COOLDOWN_H}h.</i></p>"))
            logger.info("  CRITICAL alert email sent (%d items)", len(crit_new))
        except Exception as e:
            logger.warning("  Alert email failed: %s", e)

    # Denominator is what was measured here, not len(MANIFEST) — "6/32 fresh"
    # reads as 26 failures when it is 26 artifacts this host does not own.
    logger.info("Sentinel[%s]: %d/%d fresh | %d stale (%d new) | %d auto-fixed "
                "| %d deferred to the owning host | %s",
                host or "UNKNOWN", ok_count, measured, len(issues),
                len(new_alerts), len(fixed), len(deferred), report["overall"])
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fix",   action="store_true")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    run(auto_fix=not args.no_fix, send_email=not args.no_email)

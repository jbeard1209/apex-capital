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

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("freshness_sentinel")

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
STATE_PATH  = DATA_DIR / "freshness_state.json"     # sentinel's own memory
REPORT_PATH = DATA_DIR / "freshness_report.json"    # for briefing/dashboard

ALERT_COOLDOWN_H = 12     # one alert + one auto-fix attempt per item per 12h
MARKET_DAYS_ONLY = {0, 1, 2, 3, 4}   # Mon-Fri

# =============================================================================
# MANIFEST — every critical artifact, its freshness budget, and (optionally)
# the command that regenerates it. `fix` commands must be idempotent and safe
# to run at any hour. `weekdays_only` files aren't expected to update weekends.
# =============================================================================
MANIFEST = {
    # ── Core pipeline ──────────────────────────────────────────────────────
    "data/manager_decisions.json":       {"max_age_h": 26, "critical": True,  "weekdays_only": True},
    "data/review_recommendations.json":  {"max_age_h": 26, "critical": True,  "weekdays_only": True},
    "data/state.json":                   {"max_age_h": 26, "critical": True,  "weekdays_only": True},
    # ── Risk stack ─────────────────────────────────────────────────────────
    "data/risk_bulletin.json":           {"max_age_h": 26, "critical": True,
                                          "fix": "risk_manager_agent.py"},
    "data/canary_state.json":            {"max_age_h": 14, "critical": True,
                                          "fix": "canary_agent.py"},
    "data/irm_daily_check.json":         {"max_age_h": 30, "critical": True,
                                          "fix": "irm_review_agent.py --no-email"},
    "data/premarket_alerts.json":        {"max_age_h": 30, "critical": False, "weekdays_only": True},
    # ── Governance / oversight ─────────────────────────────────────────────
    "data/hr_report.json":               {"max_age_h": 30, "critical": False,
                                          "fix": "hr_agent.py"},
    "data/chief_of_staff_report.json":   {"max_age_h": 30, "critical": False, "weekdays_only": True},
    "data/logic_team_findings.json":     {"max_age_h": 16, "critical": False,
                                          "fix": "logic_team.py"},
    "data/cit_pending_proposals.json":   {"max_age_h": 50, "critical": False},
    "data/morning_briefing.json":        {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "fix": "morning_briefing.py"},
    # ── Pods ───────────────────────────────────────────────────────────────
    "data/fi_pipeline_report.json":      {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "fix": "fi_pipeline.py"},
    "data/fe_pipeline_log.json":         {"max_age_h": 30, "critical": False, "weekdays_only": True},
    "data/etf_research_brief.json":      {"max_age_h": 30, "critical": False, "weekdays_only": True,
                                          "fix": "etf_research_agent.py"},
    "data/etf_pod_orders.json":          {"max_age_h": 54, "critical": False, "weekdays_only": True},
    # ── Research ───────────────────────────────────────────────────────────
    "data/research_team_report.json":    {"max_age_h": 100, "critical": False},
    "data/sector_assignments.json":      {"max_age_h": 100, "critical": False},
    "data/missed_winners_report.json":   {"max_age_h": 200, "critical": False,
                                          "fix": "missed_winners_agent.py"},
    # ── ML ─────────────────────────────────────────────────────────────────
    "data/ml_live_picks_log.json":       {"max_age_h": 54, "critical": False, "weekdays_only": True},
    "data/data_quality_report.json":     {"max_age_h": 54, "critical": False},
}


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
    """Detect 'pipeline started but never completed' — the silent-death mode."""
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

    # ── 1. File freshness ──────────────────────────────────────────────────
    for rel, spec in MANIFEST.items():
        if spec.get("weekdays_only") and not weekday:
            continue
        path    = BASE_DIR / rel
        age     = _age_hours(path)
        max_age = spec["max_age_h"]
        stale   = age is None or age > max_age

        prev = sentinel.get(rel, {})
        if not stale:
            ok_count += 1
            if prev.get("status") == "STALE":
                logger.info("  RECOVERED: %s (%.0fh old)", rel, age)
            sentinel[rel] = {"status": "OK"}
            continue

        detail = (f"missing" if age is None else f"{age:.0f}h old (budget {max_age}h)")
        issue = {"item": rel, "status": "STALE", "detail": detail,
                 "critical": spec.get("critical", False)}

        # Cooldown: only alert/fix once per window per item
        last_action = prev.get("last_action")
        in_cooldown = False
        if last_action:
            try:
                in_cooldown = (now - datetime.fromisoformat(last_action)) < timedelta(hours=ALERT_COOLDOWN_H)
            except Exception:
                pass

        if not in_cooldown:
            sentinel[rel] = {"status": "STALE", "last_action": now.isoformat()}
            issue["new_alert"] = True
            if auto_fix and spec.get("fix"):
                outcome = _attempt_fix(spec["fix"])
                issue["auto_fix"] = outcome
                if outcome.startswith("fixed"):
                    fixed.append(rel)
        else:
            sentinel[rel] = {**prev, "status": "STALE"}

        issues.append(issue)
        logger.warning("  STALE%s: %s — %s%s",
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
        "checked":      len(MANIFEST),
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

    logger.info("Sentinel: %d/%d fresh | %d stale (%d new) | %d auto-fixed | %s",
                ok_count, len(MANIFEST), len(issues), len(new_alerts),
                len(fixed), report["overall"])
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fix",   action="store_true")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    run(auto_fix=not args.no_fix, send_email=not args.no_email)

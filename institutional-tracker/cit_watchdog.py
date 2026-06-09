#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — CIT WATCHDOG
# =============================================================================
# Dedicated scanner: its only job is to catch problems that were observed but
# went nowhere — stuck issues, ignored task requests, log warnings that were
# never formally filed.
#
# WHAT IT SCANS
# ─────────────
#   1. data/issue_tracker.json
#      • OPEN/ASSIGNED/IN_PROGRESS issues with no update for >12h → STALE
#      • STALE issues with no update for >24h → request CoS meeting (EMERGENCY)
#
#   2. data/firm_channel.json
#      • TASK_REQUEST posts older than 6h with no follow-up from the target agent
#        → escalate to the target agent's supervisor
#      • TASK_REQUEST posts older than 24h still unacknowledged
#        → request CoS meeting (HIGH urgency)
#
#   3. Log files (logs/*.log)
#      • Lines containing ERROR or WARNING that mention a specific agent or file
#        but have no matching issue in issue_tracker → file a new OPEN issue
#
# SCHEDULE
# ────────
#   Run every 3 hours via GCP cron:
#   0 */3 * * * ~/venv/bin/python3 ~/institutional-tracker/cit_watchdog.py
#
# OUTPUT
# ──────
#   data/cit_watchdog_report.json — written every run
#   firm_channel — PIPELINE_ALERT post with summary
#   cos_meeting_requests.json — escalations for items stuck >24h
# =============================================================================

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cit_watchdog")

WATCHDOG_REPORT = ROOT / "data" / "cit_watchdog_report.json"
ISSUE_TRACKER   = ROOT / "data" / "issue_tracker.json"
FIRM_CHANNEL    = ROOT / "data" / "firm_channel.json"
LOG_DIR         = ROOT / "logs"

# Thresholds
STALE_HOURS        = 12   # no update in 12h → mark STALE
EMERGENCY_HOURS    = 24   # no update in 24h → CoS emergency
TASK_ESCALATE_HOURS = 6   # task request unacknowledged 6h → escalate to supervisor
TASK_COS_HOURS     = 24   # task request unacknowledged 24h → CoS meeting request

# Log lines with these patterns AND a specific file/agent reference get filed as issues
_LOG_ERROR_RE  = re.compile(r'\b(ERROR|CRITICAL)\b')
_LOG_AGENT_RE  = re.compile(
    r'(alpaca_trader|firm_channel|chief_of_staff|cit_director|risk_manager|'
    r'manager_agent|board_meeting|ml_model|predictor|signal_engine|'
    r'main\.py|trade_review|earnings_predictor)\b',
    re.IGNORECASE,
)


# =============================================================================
# HELPERS
# =============================================================================

def _load_json(path: Path):
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def _now() -> datetime:
    return datetime.now()


def _age_hours(ts_str: str) -> float:
    """Return age of an ISO timestamp in hours. Returns 9999 on parse failure."""
    try:
        dt = datetime.fromisoformat(str(ts_str)[:19])
        return (_now() - dt).total_seconds() / 3600
    except Exception:
        return 9999.0


def _get_supervisor(agent_name: str) -> str:
    try:
        from firm_channel import get_supervisor
        return get_supervisor(agent_name)
    except Exception:
        return "Chief of Staff"


# =============================================================================
# SCAN 1 — ISSUE TRACKER
# =============================================================================

def scan_issue_tracker() -> dict:
    """
    Find issues that have gone stale (no progress for too long).
    Returns a dict with lists of newly_stale, escalated, and already_stale issues.
    """
    issues = _load_json(ISSUE_TRACKER)
    if not isinstance(issues, list):
        return {"newly_stale": [], "escalated": [], "already_stale": []}

    newly_stale  = []
    escalated    = []
    already_stale = []
    updated_ids  = set()

    for issue in issues:
        status   = issue.get("status", "OPEN")
        if status in ("RESOLVED", "STALE", "ESCALATED"):
            # Check if STALE/ESCALATED ones have been sitting too long
            if status in ("STALE", "ESCALATED"):
                updated_at = issue.get("updated_at") or issue.get("created_at", "")
                age = _age_hours(updated_at)
                if age > EMERGENCY_HOURS:
                    already_stale.append(issue)
            continue

        # Active statuses: OPEN, ASSIGNED, IN_PROGRESS
        updated_at = issue.get("updated_at") or issue.get("created_at", "")
        age = _age_hours(updated_at)

        if age > EMERGENCY_HOURS:
            escalated.append(issue)
            updated_ids.add(issue.get("issue_id"))
        elif age > STALE_HOURS:
            newly_stale.append(issue)
            updated_ids.add(issue.get("issue_id"))

    # Persist status updates to issue_tracker
    if updated_ids:
        _update_issue_statuses(issues, updated_ids, newly_stale, escalated)

    return {
        "newly_stale":  newly_stale,
        "escalated":    escalated,
        "already_stale": already_stale,
    }


def _update_issue_statuses(issues: list, updated_ids: set,
                            newly_stale: list, escalated: list):
    """Write STALE / ESCALATED status back to issue_tracker.json."""
    import fcntl
    stale_ids    = {i.get("issue_id") for i in newly_stale}
    escalate_ids = {i.get("issue_id") for i in escalated}
    now_iso = _now().isoformat()

    for issue in issues:
        iid = issue.get("issue_id")
        if iid in escalate_ids and issue.get("status") not in ("RESOLVED", "ESCALATED"):
            issue["status"]     = "ESCALATED"
            issue["updated_at"] = now_iso
            issue["watchdog_note"] = f"Auto-escalated by CITWatchdog at {now_iso} (>{EMERGENCY_HOURS}h stale)"
        elif iid in stale_ids and issue.get("status") not in ("RESOLVED", "STALE", "ESCALATED"):
            issue["status"]     = "STALE"
            issue["updated_at"] = now_iso
            issue["watchdog_note"] = f"Marked STALE by CITWatchdog at {now_iso} (>{STALE_HOURS}h no progress)"

    try:
        path = str(ISSUE_TRACKER)
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0); f.truncate()
                json.dump(issues, f, indent=2, default=str)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        logger.warning(f"  [Watchdog] Could not update issue_tracker: {e}")


# =============================================================================
# SCAN 2 — FIRM CHANNEL TASK REQUESTS
# =============================================================================

def scan_task_requests() -> dict:
    """
    Find TASK_REQUEST posts that have sat unacknowledged too long.
    Uses a simple heuristic: if the target agent posted ANYTHING to the firm
    channel after the task was posted, treat it as acknowledged.
    """
    channel = _load_json(FIRM_CHANNEL)
    if not isinstance(channel, list):
        return {"to_escalate_supervisor": [], "to_escalate_cos": []}

    task_requests = [e for e in channel if e.get("category") == "TASK_REQUEST"]
    to_escalate_supervisor = []
    to_escalate_cos        = []

    for task in task_requests:
        ts      = task.get("ts", "")
        age     = _age_hours(ts)
        content = task.get("content", "")

        # Parse target agent from "TASK REQUEST → <Agent>: ..." pattern
        m = re.search(r'TASK REQUEST\s*→\s*([^:]+):', content)
        if not m:
            continue
        target_agent = m.group(1).strip()

        # Check if the target agent has posted anything AFTER this task was requested
        task_dt = datetime.fromisoformat(ts[:19]) if ts else None
        acknowledged = False
        if task_dt:
            for entry in channel:
                if entry.get("agent", "") == target_agent:
                    try:
                        entry_dt = datetime.fromisoformat(entry.get("ts", "")[:19])
                        if entry_dt > task_dt:
                            acknowledged = True
                            break
                    except Exception:
                        pass

        if acknowledged:
            continue

        if age > TASK_COS_HOURS:
            to_escalate_cos.append({
                "task": task,
                "target_agent": target_agent,
                "age_hours": round(age, 1),
            })
        elif age > TASK_ESCALATE_HOURS:
            supervisor = _get_supervisor(target_agent)
            to_escalate_supervisor.append({
                "task": task,
                "target_agent": target_agent,
                "supervisor": supervisor,
                "age_hours": round(age, 1),
            })

    return {
        "to_escalate_supervisor": to_escalate_supervisor,
        "to_escalate_cos":        to_escalate_cos,
    }


# =============================================================================
# SCAN 3 — LOG FILES
# =============================================================================

def scan_log_files() -> list:
    """
    Scan the most recent log file for ERROR/CRITICAL lines that mention a
    specific agent/file but have no matching issue in issue_tracker.
    Returns a list of candidate issues to file.
    """
    if not LOG_DIR.exists():
        return []

    # Load existing issue descriptions to avoid duplicates
    existing_issues = _load_json(ISSUE_TRACKER)
    existing_descs  = {
        i.get("description", "").lower()[:80]
        for i in (existing_issues if isinstance(existing_issues, list) else [])
    }

    # Find the most recent log file(s) — only scan logs from the past 6h
    cutoff = _now() - timedelta(hours=6)
    recent_logs = []
    for log_file in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        if datetime.fromtimestamp(log_file.stat().st_mtime) > cutoff:
            recent_logs.append(log_file)

    candidates = []
    seen_lines = set()

    for log_file in recent_logs:
        try:
            with open(log_file, errors="replace") as f:
                for line in f:
                    if not _LOG_ERROR_RE.search(line):
                        continue
                    agent_match = _LOG_AGENT_RE.search(line)
                    if not agent_match:
                        continue
                    # Deduplicate by a short fingerprint
                    fingerprint = line.strip()[:100].lower()
                    if fingerprint in seen_lines:
                        continue
                    seen_lines.add(fingerprint)

                    # Check if an issue for this already exists
                    if fingerprint[:80] in existing_descs:
                        continue

                    agent_name = agent_match.group(0)
                    # Extract timestamp from log line if present
                    ts_match = re.search(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', line)
                    log_ts = ts_match.group(0) if ts_match else _now().isoformat()

                    candidates.append({
                        "source_file": str(log_file.name),
                        "log_ts":      log_ts,
                        "agent":       agent_name,
                        "line":        line.strip()[:200],
                        "severity":    "HIGH" if "CRITICAL" in line else "MEDIUM",
                    })
        except Exception as e:
            logger.warning(f"  [Watchdog] Could not read {log_file}: {e}")

    return candidates[:10]  # cap at 10 new issues per run


def _file_log_issues(candidates: list) -> list:
    """File new issues in issue_tracker for untracked log errors."""
    filed = []
    try:
        from issue_tracker import log_issue
        for c in candidates:
            iid = log_issue(
                reporter="CIT Watchdog",
                title=f"Log error in {c['agent']}: {c['line'][:60]}",
                description=f"{c['line']}\n\nSource: {c['source_file']} @ {c['log_ts']}",
                category="PIPELINE",
                severity=c["severity"],
                auto_fix_hint=f"Check {c['agent']} at {c['log_ts']}",
            )
            if iid:
                filed.append(iid)
                logger.info(f"  [Watchdog] Filed issue {iid} for log error in {c['agent']}")
    except Exception as e:
        logger.warning(f"  [Watchdog] Could not file log issues: {e}")
    return filed


# =============================================================================
# ESCALATION ACTIONS
# =============================================================================

def escalate_to_supervisor(items: list):
    """Post firm channel task requests to the supervisor of the unresponsive agent."""
    try:
        from firm_channel import post_task_request
        for item in items:
            target = item["target_agent"]
            supervisor = item["supervisor"]
            age = item["age_hours"]
            original_task = item["task"].get("content", "")[:100]
            post_task_request(
                from_agent="CIT Watchdog",
                to_agent=supervisor,
                task=(
                    f"{target} has not acknowledged a task request for {age}h. "
                    f"Original: {original_task}"
                ),
                priority="HIGH",
            )
            logger.info(f"  [Watchdog] Escalated to {supervisor}: {target} unresponsive ({age}h)")
    except Exception as e:
        logger.warning(f"  [Watchdog] Supervisor escalation failed: {e}")


def request_emergency_cos(issues: list, task_items: list):
    """Request CoS meetings for items stuck >24h."""
    try:
        from firm_channel import request_cos_meeting
        for issue in issues:
            iid   = issue.get("issue_id", "?")
            title = issue.get("title", "Unknown issue")
            owner = issue.get("assigned_to", "unassigned")
            age   = _age_hours(issue.get("updated_at") or issue.get("created_at", ""))
            request_cos_meeting(
                from_agent="CIT Watchdog",
                summary=(
                    f"STALE ISSUE {iid}: '{title}' — assigned to {owner}, "
                    f"no progress for {age:.0f}h. Needs board attention."
                ),
                urgency="EMERGENCY",
            )
            logger.warning(f"  [Watchdog] CoS meeting requested for {iid} ({age:.0f}h stale)")

        for item in task_items:
            target = item["target_agent"]
            age    = item["age_hours"]
            snippet = item["task"].get("content", "")[:80]
            request_cos_meeting(
                from_agent="CIT Watchdog",
                summary=(
                    f"UNACKNOWLEDGED TASK: {target} has not responded in {age}h. "
                    f"Task: {snippet}"
                ),
                urgency="HIGH",
            )
            logger.warning(f"  [Watchdog] CoS meeting requested — {target} unresponsive ({age}h)")
    except Exception as e:
        logger.warning(f"  [Watchdog] CoS meeting request failed: {e}")


# =============================================================================
# REPORT + FIRM CHANNEL POST
# =============================================================================

def _save_report(report: dict):
    (ROOT / "data").mkdir(exist_ok=True)
    try:
        with open(WATCHDOG_REPORT, "w") as f:
            json.dump(report, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"  [Watchdog] Could not save report: {e}")


def _post_summary(report: dict):
    try:
        from firm_channel import post
        n_stale     = len(report.get("newly_stale_issues", []))
        n_escalated = len(report.get("escalated_issues", []))
        n_task_esc  = len(report.get("task_escalations_to_supervisor", []))
        n_cos       = len(report.get("cos_meetings_requested", []))
        n_log       = len(report.get("log_issues_filed", []))

        total_flags = n_stale + n_escalated + n_task_esc + n_cos + n_log
        if total_flags == 0:
            logger.info("  [Watchdog] All clear — no stuck issues.")
            return

        summary = (
            f"Watchdog scan: {n_stale} new STALE, {n_escalated} ESCALATED, "
            f"{n_task_esc} task-requests escalated to supervisor, "
            f"{n_cos} CoS meetings requested, {n_log} log errors filed."
        )
        priority = "HIGH" if (n_escalated + n_cos) > 0 else "NORMAL"
        post("CIT Watchdog", "PIPELINE_ALERT", summary, priority=priority)
        logger.info(f"  [Watchdog] {summary}")
    except Exception as e:
        logger.warning(f"  [Watchdog] Could not post to firm channel: {e}")


# =============================================================================
# MAIN
# =============================================================================

def run() -> dict:
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — CIT WATCHDOG")
    logger.info(f"  Scan started: {_now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    report = {
        "run_at": _now().isoformat(),
        "newly_stale_issues":             [],
        "escalated_issues":               [],
        "already_stale_issues":           [],
        "task_escalations_to_supervisor": [],
        "cos_meetings_requested":         [],
        "log_issues_filed":               [],
    }

    # ── Scan 1: Issue tracker ────────────────────────────────────────────────
    logger.info("  Scanning issue tracker…")
    it_result = scan_issue_tracker()
    report["newly_stale_issues"]   = [i.get("issue_id") for i in it_result["newly_stale"]]
    report["escalated_issues"]     = [i.get("issue_id") for i in it_result["escalated"]]
    report["already_stale_issues"] = [i.get("issue_id") for i in it_result["already_stale"]]

    logger.info(f"    newly stale: {len(it_result['newly_stale'])}  |  "
                f"escalated: {len(it_result['escalated'])}  |  "
                f"already stale: {len(it_result['already_stale'])}")

    # ── Scan 2: Firm channel task requests ────────────────────────────────────
    logger.info("  Scanning firm channel task requests…")
    tr_result = scan_task_requests()
    sup_items = tr_result["to_escalate_supervisor"]
    cos_items = tr_result["to_escalate_cos"]

    report["task_escalations_to_supervisor"] = [
        {"target": i["target_agent"], "age_h": i["age_hours"]} for i in sup_items
    ]
    logger.info(f"    to-supervisor: {len(sup_items)}  |  to-CoS: {len(cos_items)}")

    # ── Scan 3: Log files ────────────────────────────────────────────────────
    logger.info("  Scanning recent log files for untracked errors…")
    log_candidates = scan_log_files()
    filed_ids = _file_log_issues(log_candidates)
    report["log_issues_filed"] = filed_ids
    logger.info(f"    filed {len(filed_ids)} new issue(s) from log errors")

    # ── Actions ──────────────────────────────────────────────────────────────
    if sup_items:
        escalate_to_supervisor(sup_items)

    cos_issue_items = it_result["escalated"]
    cos_all = cos_issue_items + cos_items
    if cos_all:
        request_emergency_cos(cos_issue_items, cos_items)
        report["cos_meetings_requested"] = (
            [i.get("issue_id") for i in cos_issue_items] +
            [i["target_agent"] for i in cos_items]
        )

    # ── Report + channel post ─────────────────────────────────────────────────
    _save_report(report)
    _post_summary(report)

    logger.info(f"  Watchdog report → {WATCHDOG_REPORT}")
    logger.info("=" * 60)
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, default=str))

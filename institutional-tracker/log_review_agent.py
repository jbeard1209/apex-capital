#!/usr/bin/env python3
"""
LOG REVIEW AGENT
================
Runs weekly (Sunday 2 AM).  Before logs are rotated out:

1. Scans all log files, extracts meaningful signals
2. Scores findings by severity + recurrence
3. Posts a board bulletin with the week's highlights
4. Sends a PRIVATE memo to IRM covering:
   - IRM's own log activity
   - Any vetoes cast and their outcomes
   - Risk threshold events and escalations
5. Exits — cron then runs log_rotate.sh at 2:30 AM

Key design choices:
  - "Interesting" = ERROR/CRITICAL, repeated WARNINGs (>=3 occurrences),
    VETOED/PROMOTED/ESCALATED events, anomalous runtimes
  - Recurring patterns (same message 3+ days in a row) flagged as HIGH priority
  - Agent health scored: runs_ok / total_runs
  - IRM report is filed as a private INFO_REQUEST (private=True flag in meeting)
"""

import os, re, glob, json, sys, logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, Counter

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("LogReviewAgent")

sys.path.insert(0, str(BASE_DIR))

# ── Inbox wiring ──────────────────────────────────────────────────────────────
try:
    from agent_inbox import run_inbox as _run_inbox
    _run_inbox("LogReviewAgent")
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
REVIEW_WINDOW_DAYS  = 7     # look at logs from the past 7 days
ROTATE_OLDER_DAYS   = 30    # rotation deletes logs older than this
RECURRENCE_THRESHOLD = 3    # same message on 3+ distinct days = HIGH priority
MAX_FINDINGS_BOARD  = 25    # cap board report length
MAX_FINDINGS_IRM    = 30

# Patterns that make a log line "interesting"
CRITICAL_RE = re.compile(
    r'\b(ERROR|CRITICAL|Traceback|Exception|FATAL|OOM|killed|crashed)\b', re.I
)
WARNING_RE = re.compile(r'\bWARNING\b', re.I)
EVENT_RE   = re.compile(
    r'\b(VETOED|PROMOTED|ESCALAT|VETO|PIP|DEMOTION|CIRCUIT.BREAKER|'
    r'STOP.LOSS|RED.ZONE|drawdown|breach|threshold|critical|severe)\b', re.I
)
IRM_AGENTS = {"irm_review_agent", "IndependentRiskManager", "irm"}

# Agents mapped to their log file prefix(es)
AGENT_LOG_MAP = {
    "Chief of Staff":       ["run", "cron"],
    "Board":                ["board"],
    "Canary":               ["canary"],
    "CROAgent":             ["portfolio_risk_dashboard"],
    "CITDirector":          ["cit"],
    "IndependentRiskManager": ["irm"],  # may not have dedicated log yet
    "HRPerformanceOfficer": ["hr"],
    "MLShortTermBoard":     ["ml_shortterm_board"],
    "MLLongTermBoard":      ["ml_longterm_board"],
    "FEBoard":              ["fe_board"],
    "FIBoard":              ["fi_board"],
    "RDTeamLeader":         ["rd"],
    "ManagerAgent":         ["run", "main"],
}


# ─────────────────────────────────────────────────────────────────────────────
# LOG PARSING
# ─────────────────────────────────────────────────────────────────────────────

def _parse_log_file(path: Path, since: datetime) -> list[dict]:
    """Return list of {level, message, line, timestamp, file} for interesting lines."""
    findings = []
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < since:
            return []
        with open(path, errors="replace") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    continue
                level = "INFO"
                if CRITICAL_RE.search(line):
                    level = "ERROR"
                elif WARNING_RE.search(line):
                    level = "WARNING"
                elif EVENT_RE.search(line):
                    level = "EVENT"
                else:
                    continue  # skip plain INFO lines

                findings.append({
                    "level":   level,
                    "message": line[:200],
                    "lineno":  lineno,
                    "file":    path.name,
                    "mtime":   mtime.isoformat(),
                })
    except Exception as e:
        log.warning(f"  Could not parse {path.name}: {e}")
    return findings


def scan_logs(since: datetime) -> dict:
    """
    Scan all log files modified since `since`.
    Returns {
        "by_file": {filename: [findings]},
        "errors":  [finding, ...],
        "warnings": [...],
        "events":  [...],
        "recurring": {message_key: count},   # messages on 3+ distinct days
        "agent_health": {agent_prefix: {"ok": n, "err": n}},
    }
    """
    by_file    = defaultdict(list)
    all_errors = []
    all_warnings = []
    all_events  = []
    msg_by_day  = defaultdict(set)   # msg_key -> set of dates it appeared
    agent_runs  = defaultdict(lambda: {"ok": 0, "err": 0})

    for path in sorted(LOG_DIR.glob("*.log")):
        file_findings = _parse_log_file(path, since)
        if not file_findings:
            continue

        prefix = re.sub(r'_\d{8}.*', '', path.stem)  # strip date suffix
        has_error = False

        for f in file_findings:
            by_file[path.name].append(f)
            day = f["mtime"][:10]
            # Build recurrence map using first 80 chars of message as key
            key = f["message"][:80]
            msg_by_day[key].add(day)

            if f["level"] == "ERROR":
                all_errors.append(f)
                has_error = True
            elif f["level"] == "WARNING":
                all_warnings.append(f)
            elif f["level"] == "EVENT":
                all_events.append(f)

        # Agent health: count runs vs errors per log file prefix
        if has_error:
            agent_runs[prefix]["err"] += 1
        else:
            agent_runs[prefix]["ok"] += 1

    # Recurring: messages seen on 3+ distinct days
    recurring = {
        msg: len(days) for msg, days in msg_by_day.items()
        if len(days) >= RECURRENCE_THRESHOLD
    }

    return {
        "by_file":      dict(by_file),
        "errors":       all_errors,
        "warnings":     all_warnings,
        "events":       all_events,
        "recurring":    recurring,
        "agent_health": dict(agent_runs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# IRM-SPECIFIC EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_irm_findings(scan: dict) -> list[dict]:
    """
    Pull everything relevant to IRM:
    - Errors/events from IRM's own log files
    - Any VETO lines across all logs
    - Risk threshold / circuit breaker events
    - Escalations that landed with IRM
    """
    irm_findings = []

    # IRM's own log files
    for fname, findings in scan["by_file"].items():
        if any(kw in fname.lower() for kw in ["irm", "risk_manager", "risk_dashboard"]):
            irm_findings.extend(findings)

    # Veto / risk events from ANY log
    veto_re = re.compile(r'\b(VETO|vetoed|risk.limit|threshold.breach|circuit.breaker|'
                         r'ESCALAT.*IRM|to.*IRM|IndependentRisk)\b', re.I)
    for finding in scan["events"] + scan["warnings"] + scan["errors"]:
        if veto_re.search(finding["message"]) and finding not in irm_findings:
            irm_findings.append(finding)

    # Recurring patterns that overlap with risk topics
    risk_re = re.compile(r'\b(risk|var|drawdown|volatil|margin|exposure|concentration)\b', re.I)
    for msg, count in scan["recurring"].items():
        if risk_re.search(msg):
            irm_findings.append({
                "level": "RECURRING",
                "message": f"[Appeared {count} days] {msg}",
                "file": "multiple",
                "lineno": 0,
                "mtime": "",
            })

    # Deduplicate by first 100 chars of message
    seen = set()
    deduped = []
    for f in irm_findings:
        key = f["message"][:100]
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


# ─────────────────────────────────────────────────────────────────────────────
# REPORT GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_findings(findings: list[dict], max_n: int) -> str:
    lines = []
    seen = set()
    for f in findings[:max_n]:
        key = f["message"][:60]
        if key in seen:
            continue
        seen.add(key)
        tag = f["level"]
        lines.append(f"  [{tag}] {f['file']} — {f['message'][:120]}")
    return "\n".join(lines) if lines else "  (none)"


def build_board_report(scan: dict, week_start: str, week_end: str) -> str:
    n_err  = len(scan["errors"])
    n_warn = len(scan["warnings"])
    n_evt  = len(scan["events"])
    n_rec  = len(scan["recurring"])

    # Agent health table
    health_lines = []
    for prefix, counts in sorted(scan["agent_health"].items()):
        total = counts["ok"] + counts["err"]
        if total == 0:
            continue
        pct = 100 * counts["ok"] // total
        icon = "✅" if pct == 100 else ("⚠️" if pct >= 70 else "❌")
        health_lines.append(f"  {icon} {prefix:<30} {counts['ok']}/{total} runs OK ({pct}%)")

    # Top recurring issues
    rec_lines = []
    for msg, cnt in sorted(scan["recurring"].items(), key=lambda x: -x[1])[:8]:
        rec_lines.append(f"  ⟳ [{cnt} days] {msg[:100]}")

    report = f"""WEEKLY LOG REVIEW — {week_start} to {week_end}
{'='*60}

SUMMARY
  Errors / Criticals : {n_err}
  Warnings           : {n_warn}
  Notable events     : {n_evt}
  Recurring patterns : {n_rec}

AGENT HEALTH (past 7 days)
{chr(10).join(health_lines) if health_lines else '  (no runs detected)'}

RECURRING ISSUES (appeared 3+ days)
{chr(10).join(rec_lines) if rec_lines else '  (none — clean week!)'}

ERRORS & CRITICALS
{_fmt_findings(scan['errors'], 15)}

NOTABLE EVENTS (vetoes, promotions, escalations, risk events)
{_fmt_findings(scan['events'], 10)}

TOP WARNINGS
{_fmt_findings(scan['warnings'], 10)}

---
Full logs available on GCP at ~/institutional-tracker/logs/
This report was generated before log rotation. Logs older than 30 days will be deleted.
"""
    return report


def build_irm_report(irm_findings: list[dict], scan: dict, week_start: str) -> str:
    if not irm_findings:
        return (
            f"IRM PRIVATE LOG REVIEW — week of {week_start}\n"
            "{'='*60}\n\n"
            "Nothing noteworthy found in your domain this week. "
            "Risk logs clean, no threshold breaches, no veto-related anomalies.\n"
        )

    veto_lines = [f for f in irm_findings if "veto" in f["message"].lower()]
    risk_lines = [f for f in irm_findings if f not in veto_lines]

    report = f"""IRM PRIVATE LOG REVIEW — week of {week_start}
{'='*60}
CONFIDENTIAL — For IndependentRiskManager only.

This report covers: your own run logs, all veto-related entries across
the system, risk threshold events, and escalations involving you.
Use it to identify anything you may want to revisit or improve.

VETO / GOVERNANCE ACTIVITY ({len(veto_lines)} entries)
{_fmt_findings(veto_lines, 15)}

RISK EVENTS & THRESHOLD ACTIVITY ({len(risk_lines)} entries)
{_fmt_findings(risk_lines, 15)}

RECURRING RISK PATTERNS
"""
    risk_re = re.compile(r'\b(risk|var|drawdown|volatil|margin|exposure)\b', re.I)
    rec_risk = [(m, c) for m, c in scan["recurring"].items() if risk_re.search(m)]
    if rec_risk:
        for msg, cnt in sorted(rec_risk, key=lambda x: -x[1])[:8]:
            report += f"  ⟳ [{cnt} days] {msg[:100]}\n"
    else:
        report += "  (none)\n"

    report += "\n---\nThis memo is not shared with the board or any other agent.\n"
    return report


# ─────────────────────────────────────────────────────────────────────────────
# LOG ROTATION
# ─────────────────────────────────────────────────────────────────────────────

def rotate_logs(older_than_days: int) -> dict:
    cutoff   = datetime.now() - timedelta(days=older_than_days)
    deleted  = []
    kept     = []
    skipped  = []   # non-dated files we never auto-delete

    for path in LOG_DIR.glob("*.log"):
        # Only rotate files with a date stamp in the name (YYYYMMDD)
        if not re.search(r'_\d{8}', path.name):
            skipped.append(path.name)
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < cutoff:
            try:
                path.unlink()
                deleted.append(path.name)
            except Exception as e:
                log.warning(f"  Could not delete {path.name}: {e}")
        else:
            kept.append(path.name)

    log.info(f"  Log rotation: deleted {len(deleted)}, kept {len(kept)}, skipped {len(skipped)}")
    return {"deleted": deleted, "kept": kept, "skipped": skipped}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run_log_review(dry_run: bool = False) -> dict:
    now        = datetime.now()
    week_start = (now - timedelta(days=REVIEW_WINDOW_DAYS)).strftime("%Y-%m-%d")
    week_end   = now.strftime("%Y-%m-%d")
    since      = now - timedelta(days=REVIEW_WINDOW_DAYS)

    log.info(f"LOG REVIEW AGENT starting — scanning {week_start} → {week_end}")

    # 1. Scan logs
    scan = scan_logs(since)
    n_files = len(scan["by_file"])
    n_total = sum(len(v) for v in scan["by_file"].values())
    log.info(f"  Scanned {n_files} log files, {n_total} interesting lines found")

    # 2. Build reports
    board_report = build_board_report(scan, week_start, week_end)
    irm_findings = extract_irm_findings(scan)
    irm_report   = build_irm_report(irm_findings, scan, week_start)

    log.info(f"  Board report: {len(board_report)} chars | IRM findings: {len(irm_findings)}")

    if not dry_run:
        from agent_meeting import (
            post_bulletin, request_info, broadcast,
            update_agent_status,
        )

        # 3a. Board bulletin (visible to all)
        severity = "WARNING" if (scan["errors"] or scan["recurring"]) else "INFO"
        bullet_id = post_bulletin(
            agent   = "LogReviewAgent",
            message = board_report,
            topic   = "ops",
            ttl_hours = 168.0,   # keep for 1 week
        )
        log.info(f"  Board bulletin posted: {bullet_id}")

        # Also broadcast a short summary so it hits everyone's inbox
        n_issues = len(scan["errors"]) + len(scan["recurring"])
        summary = (
            f"Weekly log review complete ({week_start} → {week_end}). "
            f"{len(scan['errors'])} errors, {len(scan['recurring'])} recurring patterns, "
            f"{len(scan['events'])} notable events. "
            + ("⚠️ Action may be required — see ops bulletin." if n_issues > 0
               else "✅ Clean week — no action required.")
        )
        broadcast(
            "LogReviewAgent",
            summary,
            severity  = severity,
            category  = "ops",
            recipients = ["Board", "Chief of Staff", "CITDirector", "CROAgent"],
        )

        # 3b. Private IRM memo — filed as an INFO_REQUEST with private flag
        # We use request_info and immediately self-fulfill it with the report
        # so IRM sees it in their inbox without it appearing on shared bulletin
        irm_req_id = request_info(
            from_agent = "LogReviewAgent",
            to_agent   = "IndependentRiskManager",
            topic      = f"[PRIVATE] IRM Log Review — week of {week_start}",
            urgency    = "NORMAL" if not irm_findings else "HIGH",
            context    = "Weekly private memo for IRM — not shared with board.",
        )
        # Immediately fulfill with the full report
        from agent_meeting import fulfill_info_request
        fulfill_info_request(
            request_id = irm_req_id,
            fulfilling_agent = "LogReviewAgent",
            result = irm_report,
            confidence = "HIGH",
        )
        log.info(f"  IRM private memo sent: {irm_req_id}")

        # 3c. Update agent status
        update_agent_status(
            "LogReviewAgent", "OK",
            f"Weekly review done: {len(scan['errors'])} errors, "
            f"{len(scan['recurring'])} recurring issues",
            metrics={
                "errors": len(scan["errors"]),
                "warnings": len(scan["warnings"]),
                "events":  len(scan["events"]),
                "recurring": len(scan["recurring"]),
                "irm_findings": len(irm_findings),
            }
        )

    # 4. Rotate logs (after reports are filed)
    rotation = {}
    if not dry_run:
        log.info(f"  Rotating logs older than {ROTATE_OLDER_DAYS} days...")
        rotation = rotate_logs(ROTATE_OLDER_DAYS)
        log.info(f"  Deleted {len(rotation['deleted'])} old log files")

    result = {
        "week":           f"{week_start} → {week_end}",
        "files_scanned":  n_files,
        "errors":         len(scan["errors"]),
        "warnings":       len(scan["warnings"]),
        "events":         len(scan["events"]),
        "recurring":      len(scan["recurring"]),
        "irm_findings":   len(irm_findings),
        "logs_deleted":   len(rotation.get("deleted", [])),
    }
    log.info(f"  Done. Result: {result}")
    return result


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Weekly log review + rotation agent")
    p.add_argument("--dry-run", action="store_true",
                   help="Scan and print reports without posting or rotating")
    args = p.parse_args()

    if args.dry_run:
        log.info("DRY RUN — no writes, no rotation")
        now        = datetime.now()
        week_start = (now - timedelta(days=REVIEW_WINDOW_DAYS)).strftime("%Y-%m-%d")
        week_end   = now.strftime("%Y-%m-%d")
        since      = now - timedelta(days=REVIEW_WINDOW_DAYS)
        scan       = scan_logs(since)
        board_rpt  = build_board_report(scan, week_start, week_end)
        irm_f      = extract_irm_findings(scan)
        irm_rpt    = build_irm_report(irm_f, scan, week_start)
        print("\n" + "="*60)
        print("BOARD REPORT PREVIEW")
        print("="*60)
        print(board_rpt[:3000])
        print("\n" + "="*60)
        print(f"IRM PRIVATE MEMO PREVIEW ({len(irm_f)} findings)")
        print("="*60)
        print(irm_rpt[:2000])
        print("\n" + "="*60)
        print(f"Would delete: {len([p for p in LOG_DIR.glob('*.log') if re.search(r'_d{8}', p.name)])} dated log files older than {ROTATE_OLDER_DAYS} days")
    else:
        run_log_review()

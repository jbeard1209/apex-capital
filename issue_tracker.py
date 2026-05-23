# =============================================================================
# APEX CAPITAL — ISSUE TRACKER
# =============================================================================
# Central resolution tracking system. Every identified problem — code bugs,
# data quality issues, security findings, pipeline failures, strategy gaps,
# risk flags, performance issues, and team concerns — gets a tracked record
# here so nothing stalls permanently at "proposal".
#
# Status lifecycle:
#   OPEN → ASSIGNED → IN_PROGRESS → RESOLVED | STALE | ESCALATED
#
# Categories:
#   CODE_BUG, DATA_QUALITY, SECURITY, PIPELINE, STRATEGY, RISK, PERFORMANCE, TEAM
#
# File: data/issue_tracker.json
# =============================================================================

import fcntl
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TRACKER_PATH = "data/issue_tracker.json"

VALID_STATUSES   = {"OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "STALE", "ESCALATED"}
VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
VALID_CATEGORIES = {
    "CODE_BUG", "DATA_QUALITY", "SECURITY", "PIPELINE",
    "STRATEGY", "RISK", "PERFORMANCE", "TEAM",
}


# =============================================================================
# FILE I/O WITH LOCKING
# =============================================================================

def _load() -> list:
    """Load all issues from disk. Returns list."""
    if not os.path.exists(TRACKER_PATH):
        return []
    try:
        with open(TRACKER_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                # Lock must be released in finally so json.load exceptions never leave it held.
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[IssueTracker] Load error: {e}")
        return []


def _save(issues: list) -> bool:
    """Save issues list to disk with exclusive lock."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(TRACKER_PATH, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                # Lock must be released in finally so json.dump exceptions never leave it held.
                json.dump(issues, f, indent=2, default=str)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except Exception as e:
        logger.warning(f"[IssueTracker] Save error: {e}")
        return False


def _next_id(issues: list) -> str:
    """Generate next sequential issue ID (e.g. ISS-0042)."""
    used = set()
    for issue in issues:
        iid = issue.get("issue_id", "")
        if iid.startswith("ISS-"):
            try:
                used.add(int(iid[4:]))
            except ValueError:
                pass
    n = 1
    while n in used:
        n += 1
    return f"ISS-{n:04d}"


# =============================================================================
# PUBLIC API
# =============================================================================

def log_issue(
    reporter: str,
    title: str,
    description: str,
    severity: str,
    category: str,
    auto_fix_hint: str = None,
) -> str:
    """
    Log a new issue. Returns the issue_id (e.g. 'ISS-0042').
    Deduplicates by title + reporter to avoid flooding the tracker.
    """
    severity = severity.upper() if severity else "MEDIUM"
    category = category.upper() if category else "PIPELINE"

    if severity not in VALID_SEVERITIES:
        severity = "MEDIUM"
    if category not in VALID_CATEGORIES:
        category = "PIPELINE"

    issues = _load()

    # Deduplicate: skip if same title+reporter already OPEN or IN_PROGRESS
    title_lower = title.lower().strip()
    for existing in issues:
        if (existing.get("title", "").lower().strip() == title_lower
                and existing.get("reporter") == reporter
                and existing.get("status") in ("OPEN", "ASSIGNED", "IN_PROGRESS")):
            logger.debug(f"[IssueTracker] Duplicate skipped: {existing['issue_id']} — {title[:60]}")
            return existing["issue_id"]

    now = datetime.now().isoformat()
    issue_id = _next_id(issues)

    record = {
        "issue_id":         issue_id,
        "reporter":         reporter,
        "title":            title,
        "description":      description,
        "severity":         severity,
        "category":         category,
        "status":           "OPEN",
        "assigned_to":      None,
        "created_at":       now,
        "updated_at":       now,
        "resolved_at":      None,
        "resolution":       None,
        "auto_fix_hint":    auto_fix_hint,
        "escalation_count": 0,
    }

    issues.append(record)
    _save(issues)
    logger.info(f"[IssueTracker] Logged {issue_id}: [{severity}] {title[:70]}")
    return issue_id


def update_issue(
    issue_id: str,
    status: str,
    resolution: str = None,
    assigned_to: str = None,
    notes: str = None,
) -> bool:
    """
    Update issue status.
    Status: OPEN → ASSIGNED → IN_PROGRESS → RESOLVED | STALE | ESCALATED
    """
    status = status.upper() if status else "OPEN"
    if status not in VALID_STATUSES:
        logger.warning(f"[IssueTracker] Invalid status '{status}' for {issue_id}")
        return False

    issues = _load()
    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["status"]     = status
            issue["updated_at"] = datetime.now().isoformat()
            if resolution is not None:
                issue["resolution"] = resolution
            if assigned_to is not None:
                issue["assigned_to"] = assigned_to
            if notes is not None:
                existing_notes = issue.get("notes", "")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                issue["notes"] = f"{existing_notes}\n[{ts}] {notes}".strip() if existing_notes else f"[{ts}] {notes}"
            if status == "RESOLVED" and not issue.get("resolved_at"):
                issue["resolved_at"] = datetime.now().isoformat()
            _save(issues)
            logger.info(f"[IssueTracker] Updated {issue_id} → {status}"
                        + (f" (assigned: {assigned_to})" if assigned_to else ""))
            return True

    logger.warning(f"[IssueTracker] Issue not found: {issue_id}")
    return False


def get_open_issues(severity: str = None, category: str = None) -> list:
    """Return all open issues, optionally filtered by severity and/or category."""
    issues = _load()
    result = [
        i for i in issues
        if i.get("status") in ("OPEN", "ASSIGNED", "IN_PROGRESS")
    ]
    if severity:
        result = [i for i in result if i.get("severity") == severity.upper()]
    if category:
        result = [i for i in result if i.get("category") == category.upper()]
    return result


def get_stale_issues(hours: int = 48) -> list:
    """Return issues that have been OPEN/ASSIGNED for too long without resolution."""
    cutoff = datetime.now() - timedelta(hours=hours)
    issues = _load()
    stale  = []
    for i in issues:
        if i.get("status") not in ("OPEN", "ASSIGNED", "IN_PROGRESS"):
            continue
        try:
            updated = datetime.fromisoformat(str(i.get("updated_at", i.get("created_at", "")))[:19])
            if updated < cutoff:
                stale.append(i)
        except Exception:
            pass
    return stale


def get_issue(issue_id: str) -> dict:
    """Get a specific issue by ID. Returns {} if not found."""
    for issue in _load():
        if issue.get("issue_id") == issue_id:
            return issue
    return {}


def resolve_issue(issue_id: str, resolution: str, resolved_by: str) -> bool:
    """Mark issue as resolved with explanation."""
    issues = _load()
    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["status"]      = "RESOLVED"
            issue["resolution"]  = resolution
            issue["assigned_to"] = resolved_by
            issue["resolved_at"] = datetime.now().isoformat()
            issue["updated_at"]  = datetime.now().isoformat()
            _save(issues)
            logger.info(f"[IssueTracker] Resolved {issue_id} by {resolved_by}: {resolution[:80]}")
            return True
    logger.warning(f"[IssueTracker] Cannot resolve — issue not found: {issue_id}")
    return False


def increment_escalation(issue_id: str) -> int:
    """Increment escalation_count for an issue. Returns new count."""
    issues = _load()
    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["escalation_count"] = issue.get("escalation_count", 0) + 1
            issue["updated_at"] = datetime.now().isoformat()
            _save(issues)
            return issue["escalation_count"]
    return 0


def get_stats() -> dict:
    """Return summary statistics for the issue tracker."""
    issues = _load()
    by_status   = {}
    by_severity = {}
    by_category = {}
    for i in issues:
        s = i.get("status", "UNKNOWN")
        sv = i.get("severity", "UNKNOWN")
        c  = i.get("category", "UNKNOWN")
        by_status[s]   = by_status.get(s, 0) + 1
        by_severity[sv] = by_severity.get(sv, 0) + 1
        by_category[c]  = by_category.get(c, 0) + 1
    return {
        "total":       len(issues),
        "by_status":   by_status,
        "by_severity": by_severity,
        "by_category": by_category,
        "open_count":  sum(1 for i in issues if i.get("status") in ("OPEN", "ASSIGNED", "IN_PROGRESS")),
        "resolved_count": sum(1 for i in issues if i.get("status") == "RESOLVED"),
    }

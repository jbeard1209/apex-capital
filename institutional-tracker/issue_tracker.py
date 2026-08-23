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
import re
import tempfile
from contextlib import contextmanager
from functools import wraps
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

# Three different questions get asked about a status, and they are NOT the same
# set. Conflating them is what made the dedupe leak:
#   ACTIVE     — "is somebody working this?"  (what get_open_issues() means)
#   TERMINAL   — "is this finished?"          (nothing more to do)
#   UNRESOLVED — "does this condition still stand?"  (ACTIVE + STALE + ESCALATED)
# The dedupe key must span UNRESOLVED, not ACTIVE: a STALE row is an unfixed
# problem, and matching only ACTIVE let the same condition be re-filed the
# moment its predecessor aged out.  tracker_hygiene.py writes lowercase
# terminal statuses ("resolved", "closed_stale_unverified"), so every
# comparison here is case-folded — an uppercase-only check would read a
# hygiene-closed row as still unresolved forever.
ACTIVE_STATUSES   = {"open", "assigned", "in_progress"}
TERMINAL_STATUSES = {"resolved", "closed", "closed_stale_unverified", "wont_fix"}


def _status_of(issue: dict) -> str:
    return str(issue.get("status") or "").strip().lower()


def is_terminal(issue: dict) -> bool:
    return _status_of(issue) in TERMINAL_STATUSES


def is_active(issue: dict) -> bool:
    return _status_of(issue) in ACTIVE_STATUSES


def is_unresolved(issue: dict) -> bool:
    """The condition still stands. Anything not finished — including STALE."""
    return not is_terminal(issue)


# =============================================================================
# CONDITION IDENTITY
# =============================================================================
# A detector is a pure function of the world: it reports what it sees right now
# and keeps no memory. So it writes the measurement into the title —
# "Severely stale output: data/x.json (49h old)". Six hours later the same
# unfixed condition reports "(55h old)".
#
# Dedupe used to compare titles exactly, which meant the dedupe key moved every
# time the measurement moved. The key could therefore never repeat, and one
# unfixed condition became one new issue per run: data/capital_efficiency_report.json
# alone produced 29 of them. Measured across the live tracker, 2284 of 4423 rows
# (52%) were re-reports of a condition already on file.
#
# The fix is to separate identity from measurement. The signature normalises
# measurement-shaped text and nothing else — file:line stays intact, because
# agent_roster.py:693 and :701 are genuinely different conditions. Validated
# against all 4423 live rows: 0 groups merged distinct .py:LINE conditions.
_MEASUREMENT_PATTERNS = [
    (re.compile(r"\(\s*\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours|d|days|m|mins?|minutes)\s+old\s*\)", re.I), "(<age>)"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours|d|days)\s+old\b", re.I), "<age> old"),
    (re.compile(r"\bis\s+\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours|d|days)\b", re.I), "is <age>"),
    (re.compile(r"\bfor\s+\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours|d|days)\b", re.I), "for <age>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*%"), "<pct>%"),
    (re.compile(r"\(\s*\d+\s*/\s*\d+([^)]*)\)"), r"(<ratio>\1)"),
    (re.compile(r"\bn\s*=\s*\d+", re.I), "n=<n>"),
    (re.compile(r"\bISS-\d{3,}\b"), "ISS-<id>"),
    (re.compile(r"\b(CMR|PROP)-[0-9A-Fa-f]{4,}\b"), r"\1-<id>"),
]


def condition_signature(title: str) -> str:
    """Stable identity for the condition a title describes.

    Deliberately narrow: it rewrites ages, percentages, ratios, sample sizes
    and generated ids, and leaves every other number alone. Widening this to
    "strip all digits" would merge distinct file:line findings into one.
    """
    s = str(title or "").strip()
    for rx, rep in _MEASUREMENT_PATTERNS:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip().lower()


# =============================================================================
# FILE I/O WITH LOCKING
# =============================================================================

class TrackerUnreadable(Exception):
    """The tracker file exists but could not be parsed.

    Raised only on the mutating paths. A reader may degrade to "no issues"; a
    writer may not, because the next thing a writer does is save.
    """


def _dirname() -> str:
    # NOT the literal "data": TRACKER_PATH is patched to a tmpdir in tests, and
    # os.makedirs("data") would then create a stray directory in the cwd while
    # the real write went somewhere else entirely.
    return os.path.dirname(TRACKER_PATH) or "."


@contextmanager
def _exclusive():
    """Hold ONE exclusive lock for an entire read-modify-write.

    Every mutation here is load → mutate → save, and the old code locked the
    load and the save separately. A lock that does not span the modify grants
    no mutual exclusion at all: two agents both read 2000 rows, both append
    their own row, and the second save silently discards the first agent's.
    main.py runs 6x/day alongside cit_watchdog, tracker_hygiene and pod_health,
    so this raced constantly.

    The lock lives in its own file. It cannot live in the tracker itself,
    because acquiring it would mean opening the tracker — and the write path
    opens it with "w", which truncates.
    """
    os.makedirs(_dirname(), exist_ok=True)
    f = open(TRACKER_PATH + ".lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _locked(fn):
    """Serialise a whole load-mutate-save against every other writer.

    Applied as a decorator rather than by indenting six function bodies, so the
    change is one line per call site and nothing else moves. flock is per open
    file description, so a decorated function calling another decorated
    function WOULD self-deadlock — there are none today, and a smoke test pins
    that (test_no_locked_function_calls_another).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _exclusive():
            return fn(*args, **kwargs)
    return wrapper


def _load(strict: bool = False) -> list:
    """Load all issues from disk.

    `strict=True` on the mutating paths. The old version returned [] for BOTH
    "there are no issues" and "I could not read the file", and the caller could
    not tell the difference — it appended one row to that [] and saved, which
    erased the tracker. Row counts in git are not monotonic (2414 → 1303 →
    2266 → 3886) and no row older than 2026-08-06 survives, which is what that
    looks like from the outside. The trigger was the write path below, which
    truncated the file BEFORE taking its exclusive lock: a concurrent reader
    got an empty or half-written file, json.load raised, and the except handler
    turned a failed read into a confident "the tracker is empty". Measured on a
    2000-row file: 14 of 300 concurrent reads came back empty.
    """
    if not os.path.exists(TRACKER_PATH):
        return []                     # genuinely empty — safe to build on
    try:
        with open(TRACKER_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"tracker is {type(data).__name__}, expected list")
        return data
    except Exception as e:
        if strict:
            raise TrackerUnreadable(f"{TRACKER_PATH}: {e}") from e
        logger.warning(f"[IssueTracker] Load error: {e}")
        return []


def _load_for_write() -> list:
    """Load on a mutating path, never silently substituting [] for a bad read.

    If the file is present but unparseable the bytes are moved aside with a
    timestamp instead of being overwritten by the save that follows. Losing the
    tracker is the outcome this whole change exists to prevent, and a file
    nobody can parse is still evidence. Archive, never delete.
    """
    try:
        return _load(strict=True)
    except TrackerUnreadable as e:
        quarantine = f"{TRACKER_PATH}.corrupt-{datetime.now():%Y%m%dT%H%M%S}"
        try:
            os.replace(TRACKER_PATH, quarantine)
            logger.error(f"[IssueTracker] UNREADABLE tracker quarantined to "
                         f"{quarantine} — starting a new file. {e}")
        except Exception as move_err:                  # noqa: BLE001
            logger.error(f"[IssueTracker] UNREADABLE tracker and it could not be "
                         f"moved aside ({move_err}) — REFUSING to write. {e}")
            raise
        return []


def _save(issues: list) -> bool:
    """Write the tracker atomically. The live file is never truncated in place.

    os.replace() is atomic within a filesystem, so a reader either sees the
    whole old file or the whole new one — never the empty window that the
    previous open(path, "w") opened up on every single save.
    """
    os.makedirs(_dirname(), exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=_dirname(), prefix=".issue_tracker.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(issues, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, TRACKER_PATH)
        return True
    except Exception as e:
        logger.warning(f"[IssueTracker] Save error: {e}")
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


ARCHIVE_PATH = "data/issue_tracker_archive.json"


def _load_archive() -> list:
    if not os.path.exists(ARCHIVE_PATH):
        return []
    try:
        with open(ARCHIVE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[IssueTracker] Archive load error: {e}")
        return []


def _next_id(issues: list) -> str:
    """Generate the next issue ID (e.g. ISS-0042).

    Highest-plus-one, never lowest-unused: compaction moves rows to the
    archive, and refilling the gaps they leave would mint an ID that already
    names a different issue in the archive — every historical reference to it
    would then resolve to the wrong problem.
    """
    highest = 0
    for issue in list(issues) + _load_archive():
        iid = str(issue.get("issue_id", ""))
        if iid.startswith("ISS-"):
            try:
                highest = max(highest, int(iid[4:]))
            except ValueError:
                pass
    return f"ISS-{highest + 1:04d}"


# =============================================================================
# PUBLIC API
# =============================================================================

@_locked
def log_issue(
    reporter: str,
    title: str,
    description: str,
    severity: str,
    category: str,
    auto_fix_hint: str = None,
    signature: str = None,
) -> str:
    """
    Log a new issue. Returns the issue_id (e.g. 'ISS-0042').

    Deduplicates by (condition signature, reporter) across every UNRESOLVED
    row. Callers that know their own stable identity may pass `signature=`
    explicitly; otherwise it is derived from the title.
    """
    severity = severity.upper() if severity else "MEDIUM"
    category = category.upper() if category else "PIPELINE"

    if severity not in VALID_SEVERITIES:
        severity = "MEDIUM"
    if category not in VALID_CATEGORIES:
        category = "PIPELINE"

    issues = _load_for_write()
    sig = signature or condition_signature(title)
    now = datetime.now().isoformat()

    # Match on the condition, not on the wording, and across the whole
    # unresolved lifecycle. Rows written before signatures existed are matched
    # by deriving theirs on the fly, so the backlog dedupes too.
    for existing in issues:
        if existing.get("reporter") != reporter or not is_unresolved(existing):
            continue
        existing_sig = existing.get("signature") or condition_signature(existing.get("title", ""))
        if existing_sig != sig:
            continue

        # Re-observation of a condition already on file.
        #
        # `updated_at` is deliberately NOT touched. get_stale_issues() measures
        # staleness from it, so bumping it on every sighting would mean a
        # condition observed 6x/day could never age past the 48h threshold —
        # the escalation path would go quiet precisely for the problems that
        # recur most. Freshness of the *observation* and age of the *unfixed
        # problem* are different clocks; keep them apart.
        existing["last_seen_at"] = now
        existing["occurrences"]  = int(existing.get("occurrences") or 1) + 1
        existing["signature"]    = existing_sig
        # Keep the newest measurement visible without moving the identity.
        existing["latest_title"] = title
        existing["description"]  = description
        _save(issues)
        logger.debug(f"[IssueTracker] Re-observed {existing['issue_id']} "
                     f"(x{existing['occurrences']}) — {title[:60]}")
        return existing["issue_id"]

    issue_id = _next_id(issues)

    record = {
        "issue_id":         issue_id,
        "reporter":         reporter,
        "title":            title,
        "signature":        sig,
        "description":      description,
        "severity":         severity,
        "category":         category,
        "status":           "OPEN",
        "assigned_to":      None,
        "created_at":       now,
        "updated_at":       now,
        "last_seen_at":     now,
        "occurrences":      1,
        "resolved_at":      None,
        "resolution":       None,
        "auto_fix_hint":    auto_fix_hint,
        "escalation_count": 0,
    }

    issues.append(record)
    _save(issues)
    logger.info(f"[IssueTracker] Logged {issue_id}: [{severity}] {title[:70]}")
    return issue_id


@_locked
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

    issues = _load_for_write()
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
    result = [i for i in issues if is_active(i)]
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
        if not is_active(i):
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


REGISTRY_PATH = "data/resolution_registry.json"

# How many entries the registry keeps. It is a rolling record of recent fixes,
# not an archive — the archive of closed issues is issue_tracker_archive.json.
REGISTRY_MAX = 500


def _registry_path() -> str:
    """Anchored to this file, NOT to cwd like TRACKER_PATH.

    The reader, tracker_hygiene.py, resolves the registry as
    `Path(__file__).parent / "data"`. A cwd-relative writer and a
    __file__-relative reader agree only while every caller happens to run from
    institutional-tracker/ — the moment one doesn't, the writer writes a file
    the reader will never see, and the rule silently reports zero again. Anchor
    the new file the way its reader already does.
    (os.path.join discards the prefix when REGISTRY_PATH is absolute, so tests
    can patch the constant with a tmpdir path.)
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), REGISTRY_PATH)


def load_resolution_registry() -> list:
    """Read data/resolution_registry.json. Returns [] when it does not exist."""
    try:
        with open(_registry_path()) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_resolution(note: str, by: str, signature: str = None,
                      match: str = None) -> bool:
    """Record that a fix happened, so other consumers can stop re-raising it.

    The registry had TWO readers — tracker_hygiene.py's R1 auto-close rule and
    board_meeting.py's "don't resurrect an already-fixed proposal" gate — and
    zero writers. data/resolution_registry.json had never existed on either
    host, so `_load(..., [])` handed R1 an empty list every run and it reported
    "0 verified-closed" forever, which is indistinguishable from having nothing
    to do. board_meeting's `_fixed` was likewise pinned to False, so an issue
    that HAD been fixed still got flagged "unresolved after N approvals" and
    went back to the Chairman — a live contributor to the queue this whole
    cluster is about.

    Two kinds of entry, and the difference matters:
      signature — exact match against condition_signature(title). Precise. This
                  is what the automatic closers below write.
      match     — a case-insensitive substring of the title. Blunt, for
                  hand-written entries; a careless one like "stale" would match
                  hundreds of unrelated rows, so R1 caps its blast radius
                  rather than trusting it.

    Returns True if a new entry was written, False if it was already present.
    """
    if not signature and not match:
        return False
    reg = load_resolution_registry()
    for r in reg:
        if (signature and r.get("signature") == signature) or \
           (match and r.get("match") == match):
            return False
    reg.append({
        "signature": signature,
        "match":     match,
        "note":      str(note)[:300],
        "by":        by,
        "date":      datetime.now().isoformat(),
    })
    # Newest-last, so trimming from the front drops the oldest fixes — those are
    # the ones whose matching issues have already been closed by earlier runs.
    reg = reg[-REGISTRY_MAX:]
    try:
        with open(_registry_path(), "w") as f:
            json.dump(reg, f, indent=2)
        return True
    except Exception as e:                            # noqa: BLE001
        logger.warning(f"[IssueTracker] Could not write resolution registry: {e}")
        return False


@_locked
def resolve_issue(issue_id: str, resolution: str, resolved_by: str) -> bool:
    """Mark issue as resolved with explanation."""
    issues = _load_for_write()
    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["status"]      = "RESOLVED"
            issue["resolution"]  = resolution
            issue["assigned_to"] = resolved_by
            issue["resolved_at"] = datetime.now().isoformat()
            issue["updated_at"]  = datetime.now().isoformat()
            _save(issues)
            record_resolution(
                note=resolution, by=resolved_by,
                signature=issue.get("signature") or condition_signature(issue.get("title", "")),
            )
            logger.info(f"[IssueTracker] Resolved {issue_id} by {resolved_by}: {resolution[:80]}")
            return True
    logger.warning(f"[IssueTracker] Cannot resolve — issue not found: {issue_id}")
    return False


@_locked
def resolve_by_signature(
    reporter: str,
    signature: str,
    resolution: str,
    resolved_by: str = "auto",
) -> list:
    """Close every unresolved issue from `reporter` whose condition has cleared.

    This is the half that was missing. Detectors could open an issue when a
    condition appeared but had no way to say "it is gone now", so nothing ever
    closed: across 4423 live rows, `resolved_at` was set on exactly zero. The
    tracker therefore recorded that a file was stale in perpetuity — 56 open
    issues said data/canary_state.json was stale while the file on disk was
    three hours old, and each kept generating meeting requests and escalations.

    An issue is a claim about the world. When the world stops matching it, the
    claim has to be retired by the same detector that made it — nobody else
    knows enough to.

    Returns the list of issue_ids closed (empty when nothing matched).
    """
    issues = _load_for_write()
    now = datetime.now().isoformat()
    closed = []
    for issue in issues:
        if issue.get("reporter") != reporter or not is_unresolved(issue):
            continue
        existing_sig = issue.get("signature") or condition_signature(issue.get("title", ""))
        if existing_sig != signature:
            continue
        issue["status"]      = "RESOLVED"
        issue["resolution"]  = resolution
        issue["resolved_at"] = now
        issue["updated_at"]  = now
        issue["assigned_to"] = issue.get("assigned_to") or resolved_by
        closed.append(issue["issue_id"])

    if closed:
        _save(issues)
        record_resolution(note=resolution, by=resolved_by, signature=signature)
        logger.info(f"[IssueTracker] Condition cleared — resolved {len(closed)} "
                    f"issue(s) for {reporter}: {signature[:60]}")
    return closed


@_locked
def increment_escalation(issue_id: str) -> int:
    """Increment escalation_count for an issue. Returns new count.

    `updated_at` is deliberately NOT bumped. It used to be, and that made the
    approval-loop breaker in tracker_hygiene.py structurally unable to fire:
    the breaker closes issues "untouched 21+ days", but escalating counts as
    touching, so an issue escalated every single day was never untouched — the
    loop-breaker could only ever fire on the issues that were NOT looping.
    One row here has been escalated 19 times and read as freshly updated.

    Escalation is not progress; it is evidence of the absence of progress.
    `last_escalated_at` records that it happened without claiming it was work.
    """
    issues = _load_for_write()
    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["escalation_count"] = issue.get("escalation_count", 0) + 1
            issue["last_escalated_at"] = datetime.now().isoformat()
            _save(issues)
            return issue["escalation_count"]
    return 0


_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@_locked
def compact(dry_run: bool = False) -> dict:
    """Fold historical re-reports of one condition into a single issue.

    The dedupe fix stops new duplicates; this retires the ones already on file.
    Rows are moved to data/issue_tracker_archive.json with a `folded_into`
    pointer — never deleted. A row that vanishes with no record is
    indistinguishable from a decision nobody made, and the surviving issue
    would silently lose the history that justified its severity.

    Rules:
      * only UNRESOLVED rows are folded; terminal rows are left untouched
      * the OLDEST row survives, because created_at is what the staleness and
        escalation clocks measure from — keeping the newest would reset them
      * escalation_count carries the MAX, not the sum: the sum would count one
        condition's escalations once per duplicate and instantly over-escalate
      * severity carries the HIGHEST ever seen
      * idempotent — a second run finds nothing left to fold

    Must be run on each host separately: the two hosts' data/ directories are
    fully independent (see CLAUDE.md).
    """
    issues = _load(strict=True)
    groups = {}
    for issue in issues:
        if not is_unresolved(issue):
            continue
        sig = issue.get("signature") or condition_signature(issue.get("title", ""))
        groups.setdefault((sig, issue.get("reporter")), []).append(issue)

    folded, survivors_touched = [], 0
    for (sig, _reporter), rows in groups.items():
        for r in rows:
            r["signature"] = sig
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: str(r.get("created_at") or ""))
        keep, drop = rows[0], rows[1:]

        # Every aggregate is computed BEFORE anything is written back. `keep` is
        # rows[0], so assigning to it mid-scan changes an input the later
        # aggregates still have to read — setting escalation_count to the max
        # first made the following sum count that max instead of the original.
        agg_occurrences = sum(int(r.get("occurrences") or 1) for r in rows)
        agg_esc_max     = max(int(r.get("escalation_count") or 0) for r in rows)
        agg_esc_sum     = sum(int(r.get("escalation_count") or 0) for r in rows)
        agg_severity    = max((r.get("severity", "MEDIUM") for r in rows),
                              key=lambda s: _SEVERITY_RANK.get(str(s).upper(), 1))
        agg_latest      = max(rows, key=lambda r: str(r.get("created_at") or "")).get("title")
        agg_last_seen   = max(str(r.get("last_seen_at") or r.get("created_at") or "")
                              for r in rows)
        agg_folded_from = sorted(str(r.get("issue_id")) for r in drop)

        keep["occurrences"]        = agg_occurrences
        keep["escalation_count"]   = agg_esc_max
        keep["folded_escalations"] = agg_esc_sum
        keep["folded_from"]        = agg_folded_from
        keep["severity"]           = agg_severity
        keep["latest_title"]       = agg_latest
        keep["last_seen_at"]       = agg_last_seen
        survivors_touched += 1

        for r in drop:
            r["status"] = "closed"
            r["resolution"] = f"folded into {keep['issue_id']} — same condition"
            r["folded_into"] = keep["issue_id"]
            r["resolved_at"] = datetime.now().isoformat()
            folded.append(r)

    result = {
        "rows_before":        len(issues),
        "distinct_conditions": len(groups),
        "folded":             len(folded),
        "survivors_updated":  survivors_touched,
        "rows_after":         len(issues) - len(folded),
    }
    if dry_run or not folded:
        return result

    folded_ids = {id(r) for r in folded}
    remaining = [r for r in issues if id(r) not in folded_ids]
    archive = _load_archive() + folded
    try:
        os.makedirs("data", exist_ok=True)
        with open(ARCHIVE_PATH, "w") as f:
            json.dump(archive, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"[IssueTracker] Archive write failed — NOT compacting: {e}")
        result["error"] = str(e)
        return result           # never drop rows we could not archive first

    _save(remaining)
    logger.info(f"[IssueTracker] Compacted {len(issues)} → {len(remaining)} rows "
                f"({len(folded)} folded into {survivors_touched} conditions)")
    return result


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
        # Case-folded via is_active/is_terminal: tracker_hygiene.py closes rows
        # with lowercase statuses ("resolved", "closed_stale_unverified"), and
        # an `== "RESOLVED"` comparison counted every one of them as unresolved.
        "open_count":       sum(1 for i in issues if is_active(i)),
        "resolved_count":   sum(1 for i in issues if is_terminal(i)),
        "unresolved_count": sum(1 for i in issues if is_unresolved(i)),
    }


# =============================================================================
# CLI
# =============================================================================

def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Apex Capital issue tracker")
    ap.add_argument("--report", action="store_true", help="print tracker stats")
    ap.add_argument("--compact", action="store_true",
                    help="fold historical re-reports of one condition together")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --compact: report what would change, write nothing")
    args = ap.parse_args()

    if args.compact:
        r = compact(dry_run=args.dry_run)
        print(f"{'DRY RUN — ' if args.dry_run else ''}compact: "
              f"{r['rows_before']} rows -> {r['rows_after']} "
              f"({r['folded']} folded into {r['survivors_updated']} conditions; "
              f"{r['distinct_conditions']} distinct unresolved conditions)")
        if r.get("error"):
            print(f"  ERROR: {r['error']}")
        return

    s = get_stats()
    print(f"total {s['total']} | open {s['open_count']} | "
          f"unresolved {s['unresolved_count']} | resolved {s['resolved_count']}")
    for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1]):
        print(f"  {v:6d}  {k}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _main()

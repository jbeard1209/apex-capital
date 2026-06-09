#!/usr/bin/env python3
"""
APEX CAPITAL — CIT PIPELINE INTEGRITY AGENT
============================================
Runs every 2 hours (cron: 50 */2 * * *) alongside the R&D cycle.

PURPOSE
-------
The CIT director identifies code improvement opportunities but does not
monitor active pipeline WIRING bugs in real time. The deputy_monitor detects
stale files but cannot distinguish "file stale because VM is down" from "file
stale because of a code bug." This agent closes that gap.

MOTIVATION
----------
A 17-day bug: manager_decisions.json was never written by main.py because the
code called evaluate_recommendation() directly instead of manager_agent.run().
The dead-man's switch fired "Manager Agent missed a cycle" 4× per day. The
VM was up. The pipeline was running. A cross-file wiring check would have
caught this on day 1.

CHECKS
------
  1. Dead-man's switch sanity — heartbeat fresh but manager_decisions stale
     → distinguishes code wiring bug from genuine VM outage
  2. Email loop detection — repeated subjects in email_buffer.json
  3. Agent file freshness — lighter version of deputy staleness check
  4. ML model health — AUC regression detection

OUTPUT
------
  data/cit_pipeline_integrity.json   — full findings every run
  data/chairman_action_required.json — appended on CRITICAL/HIGH (last 20)

LOCKING
-------
  data/.pipeline_integrity.lock      — fcntl.flock, prevents concurrent runs
"""

import fcntl
import json
import logging
import os
import sys
from datetime import datetime, timedelta

# ── Working directory ─────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cit_pipeline_integrity")

# ── File paths ────────────────────────────────────────────────────────────────
_HEARTBEAT_PATH    = "data/pipeline_heartbeat.json"
_MANAGER_PATH      = "data/manager_decisions.json"
_EMAIL_BUFFER_PATH = "data/email_buffer.json"
_RESEARCH_PATH     = "data/research_team_report.json"
_RISK_PATH         = "data/risk_bulletin.json"
_CANARY_PATH       = "data/canary_state.json"
_MODEL_META_PATH   = "data/model_v2_meta.json"
_OUTPUT_PATH       = "data/cit_pipeline_integrity.json"
_CHAIRMAN_PATH     = "data/chairman_action_required.json"
_LOCK_PATH         = "data/.pipeline_integrity.lock"

# ── Thresholds ────────────────────────────────────────────────────────────────
_HEARTBEAT_FRESH_H      = 4    # pipeline_heartbeat age below this = pipeline running
_HEARTBEAT_STALE_H      = 26   # pipeline_heartbeat age above this = genuine outage candidate
                               # (raised from 16h: Mac pipeline runs once at ~4:15 PM;
                               # 26h covers a full missed-day with ~2h buffer before alarming)
_MANAGER_STALE_H        = 14   # manager_decisions age above this = suspect
_AGENT_STALE_H          = 26   # research_team, risk_bulletin, canary max age
_EMAIL_LOOP_THRESHOLD   = 5    # same subject N+ times = loop
                               # (raised from 3: R&D runs 4×/day; IRM REVIEW copies now excluded
                               # so threshold of 5 catches genuine loops while not flagging R&D)
_MODEL_AUC_FLOOR        = 0.58 # below this = serious regression
_CHAIRMAN_MAX_ENTRIES   = 20   # keep only the most recent N entries


# =============================================================================
# HELPERS
# =============================================================================

def _load_json(path: str):
    """Load JSON from path; return None on any error."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"  Could not load {path}: {e}")
    return None


def _file_age_hours(path: str) -> float:
    """Return file mtime age in hours; inf if missing."""
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except OSError:
        return float("inf")


def _ts_age_hours(ts: str) -> float:
    """Return age of an ISO timestamp in hours; inf if blank/invalid."""
    if not ts:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 3600
    except Exception:
        return float("inf")


def _get_timestamp_from_json(data, *keys) -> str:
    """Try multiple key names to find a timestamp in a dict."""
    if not isinstance(data, dict):
        return ""
    for k in keys:
        v = data.get(k, "")
        if v:
            return str(v)
    return ""


def _is_weekday() -> bool:
    return datetime.now().weekday() < 5  # Mon=0 … Fri=4


def _now_iso() -> str:
    return datetime.now().isoformat()


def _run_id() -> str:
    return f"pip-integrity-{datetime.now().strftime('%Y%m%d-%H')}"


# =============================================================================
# CHECK 1 — DEAD-MAN'S SWITCH SANITY
# =============================================================================

def check_deadmans_switch(issues: list):
    logger.info("\n  [1/4] Dead-man's switch sanity check...")

    # --- pipeline_heartbeat.json ---
    hb_data = _load_json(_HEARTBEAT_PATH)
    if hb_data is None:
        hb_age_h = float("inf")
        logger.info("    pipeline_heartbeat.json missing — treating as infinitely stale")
    else:
        hb_ts = _get_timestamp_from_json(hb_data, "last_started", "generated_at", "timestamp")
        hb_age_h = _ts_age_hours(hb_ts) if hb_ts else _file_age_hours(_HEARTBEAT_PATH)

    # --- manager_decisions.json ---
    # manager_decisions is a list; use file mtime as the freshness signal
    # (each entry has a "timestamp" field but the file mtime is authoritative)
    mgr_data = _load_json(_MANAGER_PATH)
    if mgr_data is None:
        mgr_age_h = float("inf")
        mgr_ts_age_h = float("inf")
    else:
        mgr_file_age_h = _file_age_hours(_MANAGER_PATH)
        # Also try the most recent entry's timestamp as a cross-check
        if isinstance(mgr_data, list) and mgr_data:
            last_entry = mgr_data[-1]
            entry_ts = _get_timestamp_from_json(last_entry, "timestamp", "generated_at", "ts")
            mgr_ts_age_h = _ts_age_hours(entry_ts) if entry_ts else float("inf")
        else:
            mgr_ts_age_h = float("inf")
        # Use the younger of file mtime vs last entry timestamp
        mgr_age_h = min(mgr_file_age_h, mgr_ts_age_h)

    logger.info(f"    pipeline_heartbeat age: {hb_age_h:.1f}h")
    logger.info(f"    manager_decisions age:  {mgr_age_h:.1f}h")

    # If BOTH files are missing this agent is running on GCP (main.py runs on Mac).
    # pipeline_heartbeat and manager_decisions only exist on the Mac host.
    # Skip this check entirely — it cannot be evaluated from GCP.
    if hb_age_h == float("inf") and mgr_age_h == float("inf"):
        logger.info("    Both files missing — running on GCP/remote host where Mac pipeline "
                    "files are not present. DMS check skipped (not applicable).")
        return

    # If manager_decisions.json has NEVER been written (missing, not just stale),
    # this is the GCP host. main.py on Mac writes manager_decisions.json; GCP only
    # runs ML agents and the integrity check. A stale heartbeat + absent manager file
    # is the normal GCP steady-state, not a pipeline outage. Skip the check.
    if not os.path.exists(_MANAGER_PATH):
        logger.info("    manager_decisions.json does not exist on this host — running on "
                    "GCP/remote context. DMS check skipped (pipeline files are Mac-only).")
        return

    # Pattern 1: heartbeat fresh, manager stale → wiring bug
    if hb_age_h < _HEARTBEAT_FRESH_H and mgr_age_h > _MANAGER_STALE_H:
        msg = (
            f"Pipeline running (heartbeat {hb_age_h:.1f}h old) but "
            f"manager_decisions.json is {mgr_age_h:.1f}h old (threshold {_MANAGER_STALE_H}h) — "
            f"likely code wiring bug, not VM issue. "
            f"Check that main.py writes manager_decisions.json after every manager block."
        )
        logger.warning(f"    CRITICAL: {msg}")
        issues.append({
            "severity":   "CRITICAL",
            "check":      "deadmans_switch_sanity",
            "message":    msg,
            "auto_fixed": False,
        })
        return

    # Pattern 2: heartbeat stale on a weekday → genuine pipeline issue
    if hb_age_h > _HEARTBEAT_STALE_H and _is_weekday():
        msg = (
            f"pipeline_heartbeat.json is {hb_age_h:.1f}h old on a weekday "
            f"(threshold {_HEARTBEAT_STALE_H}h) — possible genuine pipeline outage or VM issue."
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      "deadmans_switch_sanity",
            "message":    msg,
            "auto_fixed": False,
        })
        return

    logger.info("    OK — heartbeat and manager_decisions are consistent.")


# =============================================================================
# CHECK 2 — EMAIL LOOP DETECTION
# =============================================================================

def check_email_loop(issues: list):
    logger.info("\n  [2/4] Email loop detection...")

    buffer = _load_json(_EMAIL_BUFFER_PATH)
    if not isinstance(buffer, list) or not buffer:
        logger.info("    email_buffer.json is empty or missing — no loop possible.")
        return

    logger.info(f"    {len(buffer)} email(s) in buffer.")

    # Normalise subjects: strip leading emoji/whitespace, lower for comparison
    def _norm(s: str) -> str:
        # Drop leading non-alpha characters (emoji, spaces) then lowercase
        stripped = s.lstrip()
        # Remove common emoji prefixes by stripping non-ASCII at the start
        while stripped and ord(stripped[0]) > 127:
            stripped = stripped[1:]
        return stripped.strip().lower()

    subject_counts: dict[str, list[str]] = {}
    for entry in buffer:
        raw_subject = entry.get("subject", "") if isinstance(entry, dict) else ""
        if not raw_subject:
            continue
        # Skip IRM REVIEW shadow copies — every buffered email gets one automatically.
        # Counting them would double every subject count and fire false loop alerts.
        if raw_subject.strip().startswith("[IRM REVIEW]"):
            continue
        key = _norm(raw_subject)
        subject_counts.setdefault(key, []).append(raw_subject)

    loop_subjects = {k: v for k, v in subject_counts.items() if len(v) >= _EMAIL_LOOP_THRESHOLD}

    if loop_subjects:
        examples = "; ".join(
            f'"{list(v)[0]}" ×{len(v)}' for k, v in list(loop_subjects.items())[:3]
        )
        msg = (
            f"Repeated email subjects in buffer ({len(loop_subjects)} unique subject(s) "
            f"appear {_EMAIL_LOOP_THRESHOLD}+ times): {examples}"
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      "email_loop_detection",
            "message":    msg,
            "auto_fixed": False,
        })

    # False-alarm pattern: DEAD-MAN'S SWITCH email but pipeline is actually fresh
    hb_data = _load_json(_HEARTBEAT_PATH)
    if hb_data is not None:
        hb_ts = _get_timestamp_from_json(hb_data, "last_started", "generated_at", "timestamp")
        hb_age_h = _ts_age_hours(hb_ts) if hb_ts else _file_age_hours(_HEARTBEAT_PATH)
        if hb_age_h < _HEARTBEAT_FRESH_H:
            for entry in buffer:
                subj = entry.get("subject", "") if isinstance(entry, dict) else ""
                if "DEAD" in subj.upper() and "MAN" in subj.upper():
                    msg = (
                        f"Dead-man's switch alert in email buffer but pipeline heartbeat is "
                        f"only {hb_age_h:.1f}h old — this is the false-alarm pattern (wiring "
                        f"bug causes missed-cycle emails even though the VM is running fine)."
                    )
                    logger.warning(f"    HIGH: {msg}")
                    issues.append({
                        "severity":   "HIGH",
                        "check":      "email_loop_detection",
                        "message":    msg,
                        "auto_fixed": False,
                    })
                    break

    if not loop_subjects:
        logger.info("    OK — no repeated subjects detected.")


# =============================================================================
# CHECK 3 — AGENT FILE FRESHNESS
# =============================================================================

def check_agent_freshness(issues: list):
    logger.info("\n  [3/4] Agent file freshness check...")

    watch_files = {
        _RESEARCH_PATH: "research_team_report.json",
        _RISK_PATH:     "risk_bulletin.json",
        _CANARY_PATH:   "canary_state.json",
    }

    stale = []
    for path, label in watch_files.items():
        data = _load_json(path)
        if data is None:
            stale.append((label, float("inf")))
            logger.warning(f"    {label}: MISSING")
            continue

        ts = _get_timestamp_from_json(
            data, "generated_at", "timestamp", "last_level_time", "ts"
        )
        age_h = _ts_age_hours(ts) if ts else _file_age_hours(path)
        logger.info(f"    {label}: {age_h:.1f}h old")
        if age_h > _AGENT_STALE_H:
            stale.append((label, age_h))

    if stale:
        details = "; ".join(f"{lbl} ({age:.0f}h)" for lbl, age in stale)
        msg = (
            f"{len(stale)} agent output file(s) are >{_AGENT_STALE_H}h old "
            f"and no recovery appears in progress: {details}"
        )
        logger.warning(f"    MEDIUM: {msg}")
        issues.append({
            "severity":   "MEDIUM",
            "check":      "agent_file_freshness",
            "message":    msg,
            "auto_fixed": False,
        })
    else:
        logger.info("    OK — all watched files are fresh.")


# =============================================================================
# CHECK 4 — ML MODEL HEALTH
# =============================================================================

def check_ml_model_health(issues: list):
    logger.info("\n  [4/4] ML model health check...")

    meta = _load_json(_MODEL_META_PATH)
    if meta is None:
        logger.info("    model_v2_meta.json not found — skipping (model not yet trained).")
        return

    auc = meta.get("auc") or meta.get("val_auc") or meta.get("best_auc")
    if auc is None:
        logger.info("    No AUC field found in model_v2_meta.json — skipping AUC check.")
        return

    try:
        auc = float(auc)
    except (ValueError, TypeError):
        logger.warning(f"    AUC value not numeric: {auc!r} — skipping.")
        return

    logger.info(f"    Model AUC: {auc:.4f} (floor: {_MODEL_AUC_FLOOR})")

    if auc < _MODEL_AUC_FLOOR:
        trained_at = meta.get("trained_at", meta.get("timestamp", "unknown"))
        msg = (
            f"ML model AUC is {auc:.4f}, below serious-regression floor "
            f"({_MODEL_AUC_FLOOR}). Model trained at: {trained_at}. "
            f"Review training data quality and feature pipeline before deploying signals."
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      "ml_model_health",
            "message":    msg,
            "auto_fixed": False,
        })
    else:
        logger.info("    OK — model AUC above regression floor.")


# =============================================================================
# OUTPUT — write findings + update chairman queue
# =============================================================================

def _write_output(issues: list):
    now_iso = _now_iso()
    has_critical_or_high = any(i["severity"] in ("CRITICAL", "HIGH") for i in issues)
    healthy = len(issues) == 0

    report = {
        "checked_at": now_iso,
        "issues":     issues,
        "healthy":    healthy,
    }

    os.makedirs("data", exist_ok=True)
    with open(_OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"\n  Wrote findings to {_OUTPUT_PATH}")

    if has_critical_or_high:
        _update_chairman_queue(issues, now_iso)


def _update_chairman_queue(issues: list, now_iso: str):
    """Append CRITICAL/HIGH issues to chairman_action_required.json (keep last 20)."""
    existing: list = []
    raw = _load_json(_CHAIRMAN_PATH)
    if isinstance(raw, list):
        existing = raw

    run_id = _run_id()
    new_entries = []
    for iss in issues:
        if iss["severity"] not in ("CRITICAL", "HIGH"):
            continue
        entry = {
            "id":          f"{run_id}-{iss['check'][:20]}",
            "severity":    iss["severity"],
            "description": iss["message"],
            "source":      "cit_pipeline_integrity_agent",
            "ts":          now_iso,
            "status":      "awaiting_chairman",
        }
        new_entries.append(entry)
        logger.info(f"  Queued to chairman: [{iss['severity']}] {iss['check']}")

    combined = existing + new_entries
    # Keep last 20
    combined = combined[-_CHAIRMAN_MAX_ENTRIES:]

    with open(_CHAIRMAN_PATH, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    logger.info(f"  Updated {_CHAIRMAN_PATH} ({len(combined)} entries)")


# =============================================================================
# MAIN
# =============================================================================

def main():
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — CIT PIPELINE INTEGRITY AGENT")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ── Single-instance lock ──────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another instance is already running — exiting.")
        lock_fh.close()
        sys.exit(0)

    try:
        issues: list = []

        check_deadmans_switch(issues)
        check_email_loop(issues)
        check_agent_freshness(issues)
        check_ml_model_health(issues)

        _write_output(issues)

        if not issues:
            logger.info("\n  ALL CLEAR — pipeline integrity looks healthy.")
        else:
            severities = [i["severity"] for i in issues]
            logger.info(
                f"\n  DONE — {len(issues)} issue(s) found: "
                + ", ".join(f"{s}×{severities.count(s)}" for s in dict.fromkeys(severities))
            )

        sys.exit(0)

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()

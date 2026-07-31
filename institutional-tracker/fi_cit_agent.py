#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME CIT AGENT
========================================
Daily pipeline integrity and improvement agent for the FI pod.
Checks: did fi_pipeline.py run? Are data files fresh? fi_picks_queue.json valid?
Writes improvement proposals to chairman_action_required.json.

Run: python3 fi_cit_agent.py
"""

from __future__ import annotations

import json
import logging
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
import chairman_queue

from config import EMAIL_SENDER, GMAIL_APP_PASSWORD
EMAIL_RECIPIENT = "jack.beard07@gmail.com"
APP_PASSWORD    = GMAIL_APP_PASSWORD
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 465

# The shared Chairman queue, same file the other seven CIT producers write.
# This used to be BASE_DIR / "chairman_action_required.json" -- the repo root,
# one directory above the real queue. Nothing reads that path, so every finding
# this agent has ever raised (including a HIGH "fi_pipeline.py has not run")
# went into a private file and died there. It was doubly broken: the real queue
# is a JSON *list*, and _update_chairman_file did existing.get("fi_pod") on it,
# which raises AttributeError outside the try -- so pointing the old code at the
# right path would have crashed rather than worked.
CHAIRMAN_FILE = DATA_DIR / "chairman_action_required.json"
_CHAIRMAN_MAX_ENTRIES = 20
STALE_HOURS   = 25   # file is stale if older than this

# Live credit spreads, written by fi_credit_monitor.py every pipeline run.
# This used to read data/fi_spread_cache.json -- a file NOTHING in the repo
# writes. Its last update was 2026-07-03; the reader was left behind when the
# producer was renamed. So for four weeks the spread-regime and duration
# improvement checks ran against frozen July 3 numbers (HY 274bps, "TIGHT")
# while the live book was at HY 333bps, "NORMAL", and the staleness alarm they
# raised about it could never be cleared by any code that exists.
SPREADS_PATH = DATA_DIR / "fi_credit_conditions.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fi_cit_agent")


def _file_age_hours(path: Path) -> float:
    """Return file age in hours, or 999 if not found."""
    if not path.exists():
        return 999.0
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600


def _load_credit_conditions():
    """Return (spreads_dict, age_hours) from the live credit-conditions file.

    Age comes from the payload's own `fetched_at` when present, not the file's
    mtime: deploy/rsync rewrites mtimes, so mtime can report a four-week-old
    snapshot as minutes fresh. Falls back to mtime, then to the 999.0 sentinel.
    """
    if not SPREADS_PATH.exists():
        return {}, 999.0
    try:
        with open(SPREADS_PATH) as f:
            payload = json.load(f)
    except Exception as exc:
        logger.warning("Could not read %s: %s", SPREADS_PATH.name, exc)
        return {}, 999.0

    spreads = payload.get("spreads") or {}
    age = _file_age_hours(SPREADS_PATH)
    stamp = payload.get("fetched_at") or payload.get("as_of")
    if stamp:
        try:
            dt = datetime.fromisoformat(str(stamp))
            if dt.tzinfo is None:                 # writer stores naive UTC
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            pass
    return spreads, age


def _host_runs_fi_pipeline():
    """Does THIS host schedule fi_pipeline.py? True / False / None if unknown.

    Every check below measures files in *this* host's data/ directory, but the
    FI pipeline runs on exactly one host (the GCP VM, `30 14 * * 1-5`). Run
    anywhere else, the identical code reads a data/ that nobody feeds and files
    "fi_pipeline.py has not run in 47.6 hours" about a pipeline that ran on
    time somewhere else. That is the same defect as the GCP IP check that filed
    the operator's home IP as a VM alert -- a host-specific question asked by a
    process that cannot tell which host it is on.

    Asking the local crontab rather than hardcoding a hostname keeps the answer
    correct if the pipeline ever moves, and needs no second source of truth.
    """
    try:
        r = subprocess.run(["crontab", "-l"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:      # no crontab at all -> this host schedules nothing
        return False
    return "fi_pipeline.py" in r.stdout


def run_fi_cit_agent() -> dict:
    logger.info("FI CIT Agent starting...")
    now     = datetime.now(timezone.utc)
    issues  = []
    improvements = []

    on_fi_host = _host_runs_fi_pipeline()
    logger.info("Host schedules fi_pipeline.py: %s",
                {True: "yes", False: "no", None: "unknown"}[on_fi_host])

    # 1. Did fi_pipeline.py run today?
    pipeline_log = DATA_DIR / "fi_pipeline_log.json"
    age_h = _file_age_hours(pipeline_log)
    if age_h > STALE_HOURS:
        issues.append({
            "severity": "HIGH",
            "type":     "PIPELINE_NOT_RUN",
            "message":  f"fi_pipeline.py has not run in {age_h:.1f} hours (threshold {STALE_HOURS}h). "
                        "Check the fi_pipeline cron on the pipeline host.",
        })
    else:
        logger.info("fi_pipeline.py ran %.1f hours ago -- OK", age_h)

    # 2. Are credit spread data files fresh?
    # FI runs once a day; the FRED cache has a 4h TTL, so a few hours of age is
    # expected behaviour, not a bug. Only flag MEDIUM past 26 hours (a whole day
    # missed).
    spreads, cache_age = _load_credit_conditions()
    if cache_age > 26:
        issues.append({
            "severity": "MEDIUM",
            "type":     "STALE_SPREAD_DATA",
            "message":  f"{SPREADS_PATH.name} is {cache_age:.1f} hours old "
                        "(threshold 26h). FI credit monitor may not have run today.",
        })

    # 3. fi_picks_queue.json valid?
    picks_file = DATA_DIR / "fi_picks_queue.json"
    picks_age  = _file_age_hours(picks_file)
    if picks_age > STALE_HOURS:
        issues.append({
            "severity": "MEDIUM",
            "type":     "STALE_PICKS_QUEUE",
            "message":  f"fi_picks_queue.json is {picks_age:.1f} hours old. No fresh picks.",
        })
    elif picks_file.exists():
        try:
            with open(picks_file) as f:
                picks_data = json.load(f)
            n_picks = picks_data.get("total_picks", 0)
            regime  = picks_data.get("regime", "UNKNOWN")
            logger.info("fi_picks_queue.json: %d picks, regime=%s", n_picks, regime)
            if n_picks == 0 and regime in ("WIDE", "DISTRESSED"):
                issues.append({
                    "severity": "MEDIUM",
                    "type":     "NO_PICKS_IN_WIDE_REGIME",
                    "message":  f"Zero picks in fi_picks_queue.json despite {regime} regime. "
                                "Agents may be misconfigured or thresholds too high.",
                })
        except Exception as exc:
            issues.append({
                "severity": "HIGH",
                "type":     "CORRUPT_PICKS_FILE",
                "message":  f"fi_picks_queue.json parse error: {exc}",
            })

    # 4. Any FI-specific email loops (check logs for repeated errors)
    for log_name in ["fi_pipeline", "fi_board", "fi_risk_monitor"]:
        log_file = LOGS_DIR / f"{log_name}.log"
        if log_file.exists():
            try:
                text = log_file.read_text(errors="replace")[-5000:]  # last 5KB
                error_count = text.count("ERROR")
                if error_count > 20:
                    issues.append({
                        "severity": "HIGH",
                        "type":     "ERROR_LOOP",
                        "message":  f"{log_name}.log has {error_count} ERROR lines in last 5KB. "
                                    "Possible error loop. Investigate immediately.",
                    })
            except Exception as _e:
                logger.debug("non-fatal: %s", _e)

    # 5. Improvement proposals
    if spreads:
        try:
            regime  = spreads.get("spread_regime", "NORMAL")
            hy      = spreads.get("hy_spread", 350)
            ig      = spreads.get("ig_spread", 120)
            t10     = spreads.get("treasury_10yr", 4.0)
            slope   = spreads.get("yield_curve_slope", 0)

            if regime == "WIDE" and hy > 550:
                improvements.append({
                    "type":        "OPPORTUNITY",
                    "priority":    "HIGH",
                    "description": (
                        f"HY spread at {hy:.0f}bps (WIDE regime). "
                        "Historical buying opportunity. Consider increasing HY allocation "
                        "beyond default targets if approved picks are available."
                    ),
                })
            if regime == "DISTRESSED":
                improvements.append({
                    "type":        "OPPORTUNITY",
                    "priority":    "CRITICAL",
                    "description": (
                        f"HY spread at {hy:.0f}bps -- DISTRESSED regime. "
                        "Maximum deployment in HY and IG credit warranted. "
                        "Consider emergency board meeting."
                    ),
                })
            if slope < -0.5 and t10 > 4.5:
                improvements.append({
                    "type":        "DURATION",
                    "priority":    "HIGH",
                    "description": (
                        f"Yield curve steeply inverted (slope={slope:.2f}%) with 10yr={t10:.2f}%. "
                        "Consider maximizing TLT/IEF allocation (duration target: 8yr)."
                    ),
                })
        except Exception as exc:
            logger.warning("Improvement analysis failed: %s", exc)

    # Raise findings into the shared Chairman queue (only from the pipeline host)
    raised = _update_chairman_file(issues, improvements, on_fi_host)

    result = {
        "run_at":            now.isoformat(),
        "on_fi_pipeline_host": on_fi_host,
        "issues":            issues,
        "issue_count":       len(issues),
        "improvements":      improvements,
        "improvement_count": len(improvements),
        "raised_to_chairman": raised,
    }

    cit_file = DATA_DIR / "fi_cit_report.json"
    try:
        with open(cit_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Could not write CIT report: %s", exc)

    logger.info(
        "FI CIT Agent complete. Issues: %d | Improvements: %d",
        len(issues), len(improvements),
    )
    return result


def _to_queue_entries(issues: list, improvements: list) -> list:
    """Render FI findings in the shape the Chairman queue's consumers read.

    `dedup_key` is the finding *type*, not the rendered message: the messages
    embed a growing age in hours, so keying on text would re-raise the same
    open issue every single run and bury the queue.
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    entries = []
    for iss in issues:
        typ = iss.get("type", "FI_ISSUE")
        entries.append({
            "id":          "fi-cit-%s-%s" % (run_id, typ.lower()),
            "severity":    iss.get("severity", "MEDIUM"),
            "category":    "BUG_FIX",
            "description": iss.get("message", ""),
            "source":      "fi_cit_agent",
            "ts":          datetime.now(timezone.utc).isoformat(),
            "status":      "awaiting_chairman",
            "dedup_key":   "fi_cit:%s" % typ,
        })
    for imp in improvements:
        typ = imp.get("type", "OPPORTUNITY")
        entries.append({
            "id":          "fi-cit-%s-imp-%s" % (run_id, typ.lower()),
            "severity":    imp.get("priority", "MEDIUM"),
            "category":    "IMPROVEMENT",
            "description": imp.get("description", ""),
            "source":      "fi_cit_agent",
            "ts":          datetime.now(timezone.utc).isoformat(),
            "status":      "awaiting_chairman",
            "dedup_key":   "fi_cit:improvement:%s" % typ,
        })
    return entries


def _update_chairman_file(issues: list, improvements: list, on_fi_host=None):
    """Raise FI pod findings into the shared Chairman queue.

    Delegates to chairman_queue.submit(), which owns the three rules this agent
    used to break: only evict your OWN entries, don't re-raise an already-open
    finding, and write atomically.

    `on_fi_host` gates escalation, not measurement. Off the host that schedules
    fi_pipeline.py these findings describe a data/ directory nobody feeds, so
    they are logged but not filed. Unknown (None) is treated as off-host and
    says so at WARNING: a false HIGH in the Chairman queue costs more than one
    day's delay in catching a stale pipeline, which the right host re-detects
    on its next run either way.
    """
    entries = _to_queue_entries(issues, improvements)
    if not entries:
        logger.info("No FI findings to raise.")
        return 0

    if on_fi_host is not True:
        why = ("this host does not schedule fi_pipeline.py"
               if on_fi_host is False else
               "could not read this host's crontab, so cannot confirm it runs "
               "fi_pipeline.py")
        logger.warning(
            "Not raising %d FI finding(s) to the Chairman queue -- %s. They "
            "describe this host's data/ directory, not the pipeline host's.",
            len(entries), why)
        for e in entries:
            logger.warning("  [local only] [%s] %s", e["severity"],
                           e["description"][:100])
        return 0

    try:
        added, suppressed = chairman_queue.submit(
            entries, "fi_cit_agent",
            path=str(CHAIRMAN_FILE),
            max_entries=_CHAIRMAN_MAX_ENTRIES)
        logger.info("Chairman queue: +%d FI finding(s), %d already open.",
                    added, suppressed)
        return added
    except Exception as exc:
        logger.error("Could not update chairman queue: %s", exc)
        return 0


if __name__ == "__main__":
    result = run_fi_cit_agent()
    logger.info(json.dumps(result, indent=2, default=str))

#!/usr/bin/env python3
"""
APEX CAPITAL — WAKEUP RECOVERY AGENT
======================================
Automatically detects when the GCP VM has been down and reruns all missed
pipeline jobs in dependency order.  Fires on @reboot (via crontab) and also
as a periodic check every 30 minutes to catch partial outages.

Recovery sequence (dependency order):
  1. morning_briefing      — context for everything else
  2. rd_team_leader        — market research baseline
  3. canary_agent          — risk gate before trading
  4. main.py               — main portfolio pipeline (up to 4x)
  5. fe_pipeline           — First Eagle lane
  6. fi_pipeline           — Fixed Income lane
  7. board_meeting         — investment decisions
  8. cit_director          — trade execution proposals
  9. overnight_ml_runner   — ML retraining (if after 1 AM and log stale)
  10. email_buffer          — digest flush

Usage:
  python3 wakeup_recovery.py [--force] [--dry-run] [--check-only]
  @reboot: cd ~/institutional-tracker && ~/venv/bin/python3 wakeup_recovery.py >> logs/recovery.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
LOGS_DIR   = BASE_DIR / "logs"
PYTHON     = str(Path(sys.executable))   # same venv python
RECOVERY_LOG = LOGS_DIR / "recovery.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("wakeup_recovery")

# ─── Job registry ─────────────────────────────────────────────────────────────
# Each entry: (script, log_prefix, max_stale_hours, weekday_only, args)
# max_stale_hours: if the most recent log for today/yesterday is older than this,
#                  the job is considered missed.

def _today() -> str:
    return datetime.now().strftime("%Y%m%d")

def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

JOBS = [
    # (script_name, log_prefix, max_stale_hours, weekday_only, extra_args)
    ("morning_briefing.py",          "briefing",          25, False, []),
    # rd_team_leader.py's own cron (05:00, 08:00, 16:00, 19:15 UTC daily) has a
    # natural max gap of ~9h45m (19:15 -> next day 05:00); 3h falsely flagged it
    # as missed on almost every 6h wakeup_recovery check, forcing redundant full
    # pipeline reruns (duplicate R&D Briefing + IRM review emails, 2026-07-17/18).
    ("rd_team_leader.py",            "rd",                11, False, []),
    ("canary_agent.py",              "canary",             1, True,  []),
    # log_prefix is "cron", not "main": the crontab's 4 scheduled main.py runs
    # write to logs/cron_YYYYMMDD.log (see crontab: `>> logs/cron_$(date...)`).
    # This entry read "main_YYYYMMDD.log" for over a month, a file only THIS
    # script itself ever wrote (_run_job() names the log after log_prefix) —
    # so none of the 4 real scheduled runs ever counted as freshness evidence,
    # and every 6h cron check saw its own last recovery run age past 7h and
    # fired again, forever. Confirmed 2026-08-22: main.py ran 6x/day (4
    # scheduled + phantom recovery runs at 00:00 and 12:00 UTC, both outside
    # market hours) every day since deploy, incl. one full DEFENSIVE-MODE run
    # on stale data 8pm and 8am ET daily. Pointing this at "cron" makes the
    # freshness check see the real crontab runs, so it only fires on a
    # genuine gap.
    ("main.py",                      "cron",               7, True,  []),
    ("fe_pipeline.py",               "fe_pipeline",       25, False, []),
    ("fi_pipeline.py",               "fi_pipeline",       25, False, []),
    ("board_meeting.py",             "board",             25, True,  []),
    ("cit_director.py",              "cit",                7, False, ["--session", "morning"]),
    ("overnight_ml_runner.py",       "ml_runner_daily",   25, False, []),
    # Staleness here is measured from logs/digest_YYYYMMDD.log, which is
    # written by whoever RAN the script — not by whoever sent mail. So this
    # entry cannot tell a missed 22:00 cron from one that ran and correctly
    # sent nothing. It re-runs on both, and that is fine: since 2026-08-17
    # `email_buffer.flush_digest()` refuses a second send on the same day, so
    # the redundant run is a no-op rather than Jack's second digest. Do not
    # delete this line to fix a duplicate — the duplicate is fixed at the
    # choke point, and deleting it would lose the genuinely-missed case.
    ("email_buffer.py",              "digest",            25, False, []),
    ("logic_team.py",                "logic_team",        13, False, []),
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_weekday() -> bool:
    return datetime.now().weekday() < 5  # Mon=0 … Fri=4

def _get_uptime_minutes() -> float:
    """Return VM uptime in minutes (Linux /proc/uptime)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0]) / 60.0
    except Exception:
        return 9999.0

def _log_age_hours(log_prefix: str) -> float | None:
    """
    Return age in hours of the most recent log file matching the prefix,
    searching today's and yesterday's date-stamped log files.
    Returns None if no log found.
    """
    for date_str in [_today(), _yesterday()]:
        log_path = LOGS_DIR / f"{log_prefix}_{date_str}.log"
        if log_path.exists():
            mtime = log_path.stat().st_mtime
            age   = (time.time() - mtime) / 3600.0
            return age
    return None

def _run_job(script: str, log_prefix: str, extra_args: list[str], dry_run: bool = False) -> bool:
    """
    Run a pipeline job and write output to that job's own dated log file
    (e.g. logs/fi_pipeline_20260524.log).  This is important: the staleness
    check in _log_age_hours() reads that same file, so if output went to
    recovery.log instead, the job would be considered stale on the very next
    30-minute check and run again — causing an infinite loop.
    """
    cmd = [PYTHON, str(BASE_DIR / script)] + extra_args
    logger.info(f"[Recovery] Running: {' '.join(cmd)}")

    if dry_run:
        logger.info("[Recovery] DRY RUN — skipping actual execution")
        return True

    # Write to the job's own dated log file (same naming as crontab)
    job_log  = LOGS_DIR / f"{log_prefix}_{_today()}.log"
    recovery_log = LOGS_DIR / f"recovery_{_today()}.log"
    try:
        with open(job_log, "a") as jf, open(recovery_log, "a") as rf:
            header = f"\n{'='*60}\n[{datetime.now().isoformat()}] RECOVERY RUN: {' '.join(cmd)}\n{'='*60}\n"
            jf.write(header)
            rf.write(header)
            jf.flush()
            rf.flush()
            # Use subprocess.PIPE and tee manually to both files
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for line in proc.stdout:
                jf.write(line.decode("utf-8", errors="replace"))
                rf.write(line.decode("utf-8", errors="replace"))
                jf.flush()
                rf.flush()
            proc.wait(timeout=600)
            returncode = proc.returncode
        success = returncode == 0
        status  = "✓ OK" if success else f"✗ exit {returncode}"
        logger.info(f"[Recovery] {script} — {status}")
        return success
    except subprocess.TimeoutExpired:
        logger.warning(f"[Recovery] {script} timed out (10 min) — skipping")
        try:
            proc.kill()
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error(f"[Recovery] {script} failed: {e}")
        return False

def _send_recovery_email(ran: list[str], skipped: list[str], failed: list[str]) -> None:
    """Send a recovery summary email using the project's email infrastructure."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from email_buffer import buffer_email  # type: ignore
        date_str  = datetime.now().strftime("%B %d, %Y")
        time_str  = datetime.now().strftime("%H:%M ET")
        body_lines = [
            f"Apex Capital — Auto-Recovery Report | {date_str} {time_str}",
            "",
            "The GCP VM detected missed pipeline jobs and ran automatic recovery.",
            "",
            f"✅ Jobs recovered ({len(ran)}):",
        ] + [f"  • {j}" for j in ran] + [
            "",
            f"⏭ Jobs skipped — already fresh ({len(skipped)}):",
        ] + [f"  • {j}" for j in skipped]

        if failed:
            body_lines += [
                "",
                f"❌ Jobs that failed during recovery ({len(failed)}):",
            ] + [f"  • {j}" for j in failed] + [
                "",
                "Manual intervention may be required for failed jobs.",
            ]

        body = "\n".join(body_lines)
        subject = f"[Auto-Recovery] Apex Capital Pipeline Restored | {date_str}"
        buffer_email("wakeup_recovery", subject, body)
        logger.info(f"[Recovery] Recovery email buffered: {subject}")
    except Exception as e:
        logger.warning(f"[Recovery] Could not send recovery email: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def run_recovery(force: bool = False, dry_run: bool = False, check_only: bool = False) -> None:
    uptime_min = _get_uptime_minutes()
    now        = datetime.now()
    is_weekday = _is_weekday()

    logger.info(f"[Recovery] === Wakeup Recovery Agent starting ===")
    logger.info(f"[Recovery] VM uptime: {uptime_min:.1f} min | Weekday: {is_weekday} | {now.strftime('%Y-%m-%d %H:%M')}")

    # Decide if recovery is warranted:
    # - Force flag overrides everything
    # - Fresh reboot (uptime < 15 min) always triggers full recovery
    # - Periodic check (not a reboot): only recover if jobs are stale
    is_fresh_reboot = uptime_min < 15

    if not force and not is_fresh_reboot:
        # Periodic check mode — only run if something is stale
        stale_count = 0
        for script, log_prefix, max_stale_h, weekday_only, _ in JOBS:
            if weekday_only and not is_weekday:
                continue
            age = _log_age_hours(log_prefix)
            if age is None or age > max_stale_h:
                stale_count += 1
        if stale_count == 0:
            logger.info("[Recovery] All jobs fresh — nothing to do.")
            return
        logger.info(f"[Recovery] Periodic check: {stale_count} stale job(s) detected — running recovery")
    elif is_fresh_reboot:
        logger.info(f"[Recovery] Fresh reboot detected — running full recovery sequence")
    else:
        logger.info("[Recovery] Forced recovery — running all stale jobs")

    if check_only:
        logger.info("[Recovery] CHECK ONLY mode — reporting stale jobs without running:")
        for script, log_prefix, max_stale_h, weekday_only, _ in JOBS:
            if weekday_only and not is_weekday:
                continue
            age = _log_age_hours(log_prefix)
            status = f"{age:.1f}h old" if age is not None else "NO LOG"
            stale  = age is None or age > max_stale_h
            flag   = "⚠ STALE" if stale else "✓ OK"
            logger.info(f"  {flag:12s} {log_prefix:<30s} (max={max_stale_h}h, actual={status})")
        return

    # Wait for network if fresh reboot
    if is_fresh_reboot:
        logger.info("[Recovery] Waiting 45s for network to stabilize...")
        if not dry_run:
            time.sleep(45)

    ran: list[str]     = []
    skipped: list[str] = []
    failed: list[str]  = []

    for script, log_prefix, max_stale_h, weekday_only, extra_args in JOBS:
        # Skip weekday-only jobs on weekends
        if weekday_only and not is_weekday:
            skipped.append(f"{script} (weekend)")
            logger.info(f"[Recovery] SKIP {script} — weekday only, today is weekend")
            continue

        # Check freshness
        age = _log_age_hours(log_prefix)
        if age is not None and age <= max_stale_h and not force:
            skipped.append(f"{script} ({age:.1f}h old, ok)")
            logger.info(f"[Recovery] SKIP {script} — fresh ({age:.1f}h old, limit={max_stale_h}h)")
            continue

        age_str = f"{age:.1f}h old" if age is not None else "no log"
        logger.info(f"[Recovery] STALE {script} ({age_str}, limit={max_stale_h}h) — running now")

        success = _run_job(script, log_prefix, extra_args, dry_run=dry_run)
        if success:
            ran.append(script)
        else:
            failed.append(script)

        # Small gap between jobs to avoid rate-limit cascades
        if not dry_run:
            time.sleep(5)

    logger.info(f"[Recovery] === Recovery complete ===")
    logger.info(f"[Recovery] Ran: {len(ran)} | Skipped: {len(skipped)} | Failed: {len(failed)}")

    if ran and not dry_run:
        _send_recovery_email(ran, skipped, failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Capital Wakeup Recovery Agent")
    parser.add_argument("--force",      action="store_true", help="Run all jobs regardless of log freshness")
    parser.add_argument("--dry-run",    action="store_true", help="Log what would run without executing")
    parser.add_argument("--check-only", action="store_true", help="Report stale jobs without running")
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run_recovery(force=args.force, dry_run=args.dry_run, check_only=args.check_only)

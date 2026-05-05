#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — WAKE-UP CATCH-UP RUNNER
# =============================================================================
# macOS cron jobs are SILENTLY SKIPPED when the Mac is asleep or off.
# This script runs automatically whenever the Mac wakes from sleep or logs in
# (via a LaunchAgent — see catch_up.plist).
#
# It checks the age of key data files and runs any scheduled jobs that were
# missed. This is not a perfect substitute for the Oracle 24/7 server, but
# it ensures the pipeline catches up quickly whenever the Mac is available.
#
# Jobs managed:
#   - Morning briefing   (if missed and it's a weekday 9:15–11:00 AM EDT)
#   - Board meeting      (if missed and it's a weekday 9:30–11:00 AM EDT)
#   - Pipeline           (if missed and it's a weekday 9:45 AM–5:30 PM EDT)
#   - R&D team           (if last run >3h ago and it's 6 AM–midnight EDT)
#   - CIT                (if last run >6h ago, any day)
# =============================================================================

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("catch_up")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
PYTHON   = sys.executable
LOCKFILE = BASE_DIR / "data" / ".catch_up_running"


def _age_hours(path: str) -> float:
    """How many hours old is a file? Returns 999 if missing."""
    p = DATA_DIR / path if not os.path.isabs(path) else Path(path)
    if not p.exists():
        return 999
    try:
        with open(p) as f:
            d = json.load(f)
        ts = (d[-1] if isinstance(d, list) and d else d).get("generated_at", "")
        if ts:
            return (datetime.now() - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 3600
    except Exception:
        pass
    # Fall back to file mtime
    return (datetime.now().timestamp() - p.stat().st_mtime) / 3600


def _is_weekday() -> bool:
    return datetime.now().weekday() < 5


def _edt_hour() -> float:
    """Current hour in EDT (UTC-4). Approximate — ignores DST edge cases."""
    return (datetime.utcnow().hour - 4) % 24 + datetime.utcnow().minute / 60


def _run(script: str, args: list = None, log_suffix: str = "") -> bool:
    """Run a script and log output. Returns True on success."""
    cmd  = [PYTHON, str(BASE_DIR / script)] + (args or [])
    log  = LOG_DIR / f"catchup_{log_suffix or script.replace('.py','')}_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
    LOG_DIR.mkdir(exist_ok=True)
    logger.info(f"  ▶ Running {script} {' '.join(args or [])}")
    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True,
                                text=True, timeout=300)
        with open(log, "w") as f:
            f.write(result.stdout + "\n" + result.stderr)
        if result.returncode == 0:
            logger.info(f"  ✅ {script} completed OK")
            return True
        else:
            logger.warning(f"  ⚠ {script} exited {result.returncode} — see {log.name}")
            return False
    except subprocess.TimeoutExpired:
        logger.error(f"  ❌ {script} timed out after 5 min")
        return False
    except Exception as e:
        logger.error(f"  ❌ {script} failed: {e}")
        return False


def run_catch_up():
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — WAKE-UP CATCH-UP RUNNER")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    # ── Prevent overlapping runs ──────────────────────────────────────────
    if LOCKFILE.exists():
        lock_age = (datetime.now().timestamp() - LOCKFILE.stat().st_mtime) / 60
        if lock_age < 30:
            logger.info("  Catch-up already running (lockfile present) — skipping.")
            return
        LOCKFILE.unlink(missing_ok=True)   # stale lock

    LOCKFILE.touch()
    ran = []

    try:
        now_edt  = _edt_hour()
        weekday  = _is_weekday()
        briefing_age = _age_hours("morning_briefing.json")
        rd_age       = _age_hours("research_team_report.json")
        cit_age      = min(
            _age_hours("cit_morning_report.json"),
            _age_hours("cit_evening_report.json"),
        )

        logger.info(f"  EDT: {now_edt:.1f}h | Weekday: {weekday}")
        logger.info(f"  Morning briefing: {briefing_age:.1f}h old")
        logger.info(f"  R&D report: {rd_age:.1f}h old")
        logger.info(f"  CIT last run: {cit_age:.1f}h ago")

        # ── Morning Briefing (weekdays 9:15–11:00 AM EDT) ─────────────────
        if weekday and 9.25 <= now_edt <= 11.0 and briefing_age > 1.0:
            logger.info("  → Morning briefing was missed — running now...")
            if _run("morning_briefing.py"):
                ran.append("morning_briefing")

        # ── Board Meeting (weekdays 9:30–11:00 AM EDT) ────────────────────
        board_age = _age_hours("board_meeting_report.json")
        if weekday and 9.5 <= now_edt <= 11.0 and board_age > 20:
            logger.info("  → Board meeting was missed — running now...")
            if _run("board_meeting.py"):
                ran.append("board_meeting")

        # ── Main Pipeline (weekdays 9:45 AM–5:30 PM EDT) ─────────────────
        # Check if any pipeline run was missed — look for today's cron log
        today_log  = LOG_DIR / f"cron_{datetime.now().strftime('%Y%m%d')}.log"
        pipeline_ran_today = today_log.exists() and today_log.stat().st_size > 1000
        if weekday and 9.75 <= now_edt <= 17.5 and not pipeline_ran_today:
            logger.info("  → Pipeline hasn't run today — running catch-up pipeline...")
            log_path = str(LOG_DIR / f"cron_{datetime.now().strftime('%Y%m%d')}.log")
            if _run("main.py"):
                ran.append("main_pipeline")

        # ── R&D Team (6 AM–midnight EDT, any day, if >3h stale) ──────────
        if 6.0 <= now_edt <= 24.0 and rd_age > 3.0:
            logger.info(f"  → R&D report is {rd_age:.1f}h old — running R&D team...")
            if _run("rd_team_leader.py"):
                ran.append("rd_team_leader")

        # ── CIT (any day, if >6h since last run) ─────────────────────────
        if cit_age > 6.0:
            session = "morning" if now_edt < 15 else "evening"
            logger.info(f"  → CIT last ran {cit_age:.1f}h ago — running {session} session...")
            if _run("cit_director.py", ["--session", session], "cit"):
                ran.append(f"cit_{session}")

        if ran:
            logger.info(f"\n  ✅ Catch-up complete. Ran: {', '.join(ran)}")
        else:
            logger.info("\n  ✓ All jobs are current — nothing to catch up.")

    finally:
        LOCKFILE.unlink(missing_ok=True)

    return ran


if __name__ == "__main__":
    run_catch_up()

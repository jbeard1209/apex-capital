#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — WAKE-UP CATCH-UP RUNNER
# =============================================================================
# macOS cron jobs are SILENTLY SKIPPED when the Mac is asleep or off.
# This script runs automatically every 20 min via LaunchAgent so that
# whenever the Mac wakes up, any missed jobs are caught up in the exact
# order they were originally scheduled to fire.
#
# HOW IT WORKS
# ─────────────
# 1. Walk every job in the JOB_REGISTRY (mirrors the full crontab).
# 2. For each job, find the most recent scheduled fire time that has passed.
# 3. Check whether the job's output file is older than that fire time
#    (meaning the job either didn't run, or ran before that slot).
# 4. If stale → add to the "missed" queue with the scheduled fire time.
# 5. Sort the entire missed queue by scheduled fire time (oldest first).
# 6. Run jobs one-by-one in that order — CRO before IRM, briefing before
#    board, board before pipeline, etc.
#
# ORACLE / 24-7 SERVER COMPATIBILITY
# ────────────────────────────────────
# When the Oracle server is running, it keeps every output file fresh.
# Step 3 will always find files younger than the last scheduled time →
# nothing gets added to the missed queue → catch_up exits immediately
# without running anything. No conflicts, no double-running.
# You can leave this script installed permanently; it becomes a no-op
# the moment Oracle takes over.
#
# CANARY EXCEPTION
# ─────────────────
# The canary fires every 5–30 minutes. Catching up a 3-hour-old canary
# reading is useless — market conditions have completely changed. It is
# excluded from catch-up; it self-recovers on the next cron tick.
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
LOCKFILE = DATA_DIR / ".catch_up_running"

# ── UTC offset for EDT ────────────────────────────────────────────────────────
# EDT = UTC-4.  We use a fixed offset; the 1-hour DST difference is acceptable.
EDT_OFFSET_H = -4


# Per-job wall clock. 600s was applied to every job, including main.py, and
# main.py outgrew it: 211 of 437 pipeline runs were SIGKILLed at exactly 10:00,
# in every hour of the day, ~50/50 with no time-of-day pattern.
#
# It is worth recording how that was misread, because the numbers invite it. The
# median SUCCESSFUL run is 48s and p95 is 404s, which reads as "half the runs
# hang" — and the fix for a hang is not a bigger budget. main.py's own log
# (which survives the kill; the per-job log below does not) settles it: the
# killed run logged 790 lines in its 10 minutes with no gap longer than 27s. It
# was working the whole time — dozens of FE agents at ~40 candidates each, then
# a large issue backlog. The fast runs are early exits (market closed, CRO
# block, already ran). So the work really has outgrown the budget, and it keeps
# growing: 155 agents now.
#
# The cost was never "the run restarts". main.py places orders near the END of
# its work, so a kill at 10:00 lands AFTER trading and BEFORE the state save —
# which is how data/state.json comes to show 0 positions while the broker holds
# 16. Since 2026-08-07 the Mac is also the firm's only exit-capable host while
# the VM is unreachable (see single_writer.py), and an exit path that is killed
# half the time is not a safety net.
#
# 2700s is >4x the point at which runs were being truncated, and stays under
# main.py's own 90-min hang watchdog so that inner, self-releasing bound stays
# the real ceiling for a genuine hang. Overlap is safe: main.py holds an flock,
# so a duplicate started by the next launchd tick exits immediately.
DEFAULT_JOB_TIMEOUT_S = 600
PIPELINE_TIMEOUT_S    = 2700


# =============================================================================
# JOB REGISTRY — mirrors the full crontab
# =============================================================================
# Each entry describes one cron job:
#
#   name            — human label used in logs
#   script          — filename relative to BASE_DIR
#   args            — extra CLI args (list)
#   output_file     — data/ file written by this job (used for freshness check)
#                     Use None for the main pipeline (checked via log file instead)
#   ttl_hours       — how long before output is considered stale
#                     Must be slightly LESS than the interval between fires so
#                     we only catch up genuinely missed runs, not just slow ones.
#   fire_times_edt  — list of scheduled EDT hours (float) this job fires each day
#                     e.g. 8.5 = 8:30 AM, 13.75 = 1:45 PM
#   run_days        — "weekdays" or "all"
#   catchup_window  — (start_edt, end_edt) — only worth running catch-up within
#                     this window. Premarket at 8:30 AM is useless at 9 PM.
#                     Use (0, 24) to always catch up regardless of time.
#   once_per_wakeup — if True, run this job at most once per catch-up cycle
#                     even if multiple fire slots were missed (e.g. pipeline).
#
# ─────────────────────────────────────────────────────────────────────────────
JOB_REGISTRY = [

    # ── Pre-market check ─────────────────────────────────────────────────────
    # Scans positions for overnight gaps. Only useful before market opens.
    {
        "name":            "premarket_check",
        "script":          "premarket_check.py",
        "args":            [],
        "output_file":     "data/premarket_alerts.json",
        "ttl_hours":       22,
        "fire_times_edt":  [8.5],           # 8:30 AM
        "run_days":        "weekdays",
        "catchup_window":  (8.5, 10.5),     # pointless to run after 10:30 AM
        "once_per_wakeup": True,
    },

    # ── Morning briefing ──────────────────────────────────────────────────────
    # Collects state from all 15 agents, writes shared morning_briefing.json.
    # Must run before board meeting and pipeline.
    {
        "name":            "morning_briefing",
        "script":          "morning_briefing.py",
        "args":            [],
        "output_file":     "data/morning_briefing.json",
        "ttl_hours":       6,
        "fire_times_edt":  [9.25],          # 9:15 AM
        "run_days":        "weekdays",
        "catchup_window":  (9.25, 14.0),    # still useful up to 2 PM
        "once_per_wakeup": True,
    },

    # ── Board meeting ─────────────────────────────────────────────────────────
    # Daily board deliberation. Reads morning briefing. Runs before pipeline.
    {
        "name":            "board_meeting",
        "script":          "board_meeting.py",
        "args":            [],
        "output_file":     "data/board_meeting_report.json",
        "ttl_hours":       22,
        "fire_times_edt":  [9.5],           # 9:30 AM
        "run_days":        "weekdays",
        "catchup_window":  (9.5, 14.0),
        "once_per_wakeup": True,
    },

    # ── R&D Team Leader ───────────────────────────────────────────────────────
    # Sector scan + research picks. Every 2h, 6 AM–midnight, 7 days.
    # Fire times: 6,8,10,12,14,16,18,20,22,0 EDT
    {
        "name":            "rd_team_leader",
        "script":          "rd_team_leader.py",
        "args":            [],
        "output_file":     "data/research_team_report.json",
        "ttl_hours":       2.5,             # just over 2h interval
        "fire_times_edt":  [0, 6, 8, 10, 12, 14, 16, 18, 20, 22],
        "run_days":        "all",
        "catchup_window":  (0, 24),
        "once_per_wakeup": True,            # run once, get fresh data
    },

    # ── CIT — morning session (6 AM, 12 PM EDT) ───────────────────────────────
    {
        "name":            "cit_morning",
        "script":          "cit_director.py",
        "args":            ["--session", "morning"],
        "output_file":     "data/cit_morning_report.json",
        "ttl_hours":       7,
        "fire_times_edt":  [6.0, 12.0],
        "run_days":        "all",
        "catchup_window":  (0, 24),
        "once_per_wakeup": True,
    },

    # ── Main pipeline — run 1 (9:45 AM) ──────────────────────────────────────
    # These four entries keep the ordering correct: pipeline run 1 fires
    # between R&D@10AM and R&D@12PM, so it needs its own slot in the queue.
    # once_per_wakeup=False here so all missed slots are discovered and
    # sorted — but we de-duplicate to a single actual execution below.
    {
        "name":            "pipeline_0945",
        "script":          "main.py",
        "timeout_s":       PIPELINE_TIMEOUT_S,   # see _run(): 600s truncated ~half of all runs
        "args":            [],
        "output_file":     None,            # checked via today's cron log
        "ttl_hours":       None,
        "fire_times_edt":  [9.75],          # 9:45 AM
        "run_days":        "weekdays",
        "catchup_window":  (9.75, 17.5),
        "once_per_wakeup": False,           # see de-dup logic below
    },

    # ── Main pipeline — run 2 (12:00 PM) ─────────────────────────────────────
    {
        "name":            "pipeline_1200",
        "script":          "main.py",
        "timeout_s":       PIPELINE_TIMEOUT_S,   # see _run(): 600s truncated ~half of all runs
        "args":            [],
        "output_file":     None,
        "ttl_hours":       None,
        "fire_times_edt":  [12.0],
        "run_days":        "weekdays",
        "catchup_window":  (12.0, 17.5),
        "once_per_wakeup": False,
    },

    # ── Main pipeline — run 3 (2:00 PM) ──────────────────────────────────────
    {
        "name":            "pipeline_1400",
        "script":          "main.py",
        "timeout_s":       PIPELINE_TIMEOUT_S,   # see _run(): 600s truncated ~half of all runs
        "args":            [],
        "output_file":     None,
        "ttl_hours":       None,
        "fire_times_edt":  [14.0],
        "run_days":        "weekdays",
        "catchup_window":  (14.0, 17.5),
        "once_per_wakeup": False,
    },

    # ── Main pipeline — run 4 (4:15 PM) ──────────────────────────────────────
    {
        "name":            "pipeline_1615",
        "script":          "main.py",
        "timeout_s":       PIPELINE_TIMEOUT_S,   # see _run(): 600s truncated ~half of all runs
        "args":            [],
        "output_file":     None,
        "ttl_hours":       None,
        "fire_times_edt":  [16.25],
        "run_days":        "weekdays",
        "catchup_window":  (16.25, 18.0),
        "once_per_wakeup": False,
    },

    # ── CIT — evening session (6 PM, 9 PM EDT) ────────────────────────────────
    {
        "name":            "cit_evening",
        "script":          "cit_director.py",
        "args":            ["--session", "evening"],
        "output_file":     "data/cit_evening_report.json",
        "ttl_hours":       7,
        "fire_times_edt":  [18.0, 21.0],
        "run_days":        "all",
        "catchup_window":  (0, 24),
        "once_per_wakeup": True,
    },

    # ── Daily digest ──────────────────────────────────────────────────────────
    # Flush buffered emails. If missed, send as soon as Mac wakes — user
    # shouldn't go without their digest just because Mac was off at 6 PM.
    {
        "name":            "email_digest",
        "script":          "email_buffer.py",
        "args":            [],
        "output_file":     None,            # no output file — check buffer size
        "ttl_hours":       None,
        "fire_times_edt":  [18.0],          # 6:00 PM
        "run_days":        "all",
        "catchup_window":  (18.0, 24),      # only after 6 PM
        "once_per_wakeup": True,
    },

    # ── CIT pipeline integrity (dead-man's switch / peer host liveness) ───────
    # VM cron: "50 */2 * * *" — every 2h, round the clock, every day. This is
    # host-agnostic (local file ages + an ssh probe to the peer), unlike
    # apex_system_cit_agent.py's GCP-specific disk/memory checks, which is why
    # only this one is mirrored here. Found 2026-08-11: this was the ONLY
    # producer of deadmans_switch_sanity / peer_host_liveness / single_writer
    # findings, it is VM-only in the crontab, and the VM had been unreachable
    # for 5 days with zero Mac-side fallback — chairman_action_required.json
    # took zero new entries in that entire window despite the outage.
    {
        "name":            "cit_pipeline_integrity",
        "script":          "cit_pipeline_integrity_agent.py",
        "args":            [],
        "output_file":     "data/cit_pipeline_integrity.json",
        "ttl_hours":       1.75,            # just under the 2h cron interval
        "fire_times_edt":  [0.83, 2.83, 4.83, 6.83, 8.83, 10.83, 12.83,
                             14.83, 16.83, 18.83, 20.83, 22.83],
        "run_days":        "all",
        "catchup_window":  (0, 24),
        "once_per_wakeup": True,
    },
]


# =============================================================================
# HELPERS
# =============================================================================

def _now_edt() -> datetime:
    """Current time expressed as EDT (UTC-4)."""
    return datetime.utcnow() + timedelta(hours=EDT_OFFSET_H)


def _is_weekday_edt() -> bool:
    return _now_edt().weekday() < 5


def _output_age_hours(output_file: str) -> float:
    """
    How many hours old is a job's output file?
    Returns 999 if the file doesn't exist or can't be read.
    Tries to read 'generated_at' from JSON first; falls back to file mtime.
    """
    path = BASE_DIR / output_file
    if not path.exists():
        return 999
    try:
        with open(path) as f:
            d = json.load(f)
        # Handle both dict and list-of-dict formats
        record = (d[-1] if isinstance(d, list) and d else d) if d else {}
        ts = record.get("generated_at", "")
        if ts:
            age = (datetime.now() - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 3600
            return max(age, 0.0)
    except Exception:
        pass
    # Fall back to filesystem mtime
    return (datetime.now().timestamp() - path.stat().st_mtime) / 3600


def _pipeline_ran_today() -> bool:
    """True if main.py produced a non-trivial log entry today."""
    log = LOG_DIR / f"cron_{_now_edt().strftime('%Y%m%d')}.log"
    return log.exists() and log.stat().st_size > 500


def _pipeline_last_run_hours() -> float:
    """How many hours ago did main.py last produce output?"""
    log = LOG_DIR / f"cron_{_now_edt().strftime('%Y%m%d')}.log"
    if not log.exists() or log.stat().st_size < 500:
        return 999
    return (datetime.now().timestamp() - log.stat().st_mtime) / 3600


def _buffer_has_emails() -> bool:
    """True if the email buffer has unsent emails."""
    p = DATA_DIR / "email_buffer.json"
    if not p.exists():
        return False
    try:
        with open(p) as f:
            buf = json.load(f)
        return bool(buf)
    except Exception:
        return False


def _most_recent_fire_time(job: dict) -> object:
    """
    Find the most recent scheduled fire time that has already passed (within 24h).
    Returns None if no scheduled time has passed yet today.
    """
    now_edt  = _now_edt()
    now_h    = now_edt.hour + now_edt.minute / 60.0
    best = None

    for fire_h in job["fire_times_edt"]:
        # Build a candidate datetime for today at fire_h EDT
        candidate = now_edt.replace(
            hour   = int(fire_h),
            minute = int((fire_h % 1) * 60),
            second = 0,
            microsecond = 0,
        )
        # If the fire time is in the future today, try yesterday
        if candidate > now_edt:
            candidate = candidate - timedelta(days=1)

        # Don't look back more than 24h
        if (now_edt - candidate).total_seconds() > 86400:
            continue

        if best is None or candidate > best:
            best = candidate

    return best


def _is_job_missed(job: dict) -> tuple:
    """
    Returns (missed: bool, scheduled_at: datetime|None).
    A job is "missed" if:
      - Its most recent fire time has passed
      - Its output file is older than that fire time (or doesn't exist)
      - We're inside the job's catchup_window
      - It's the right day type (weekdays / all)
    """
    now_edt = _now_edt()
    now_h   = now_edt.hour + now_edt.minute / 60.0

    # Day-of-week filter
    if job["run_days"] == "weekdays" and not _is_weekday_edt():
        return False, None

    # Catch-up window filter
    win_start, win_end = job["catchup_window"]
    if not (win_start <= now_h < win_end):
        return False, None

    scheduled_at = _most_recent_fire_time(job)
    if scheduled_at is None:
        return False, None

    # Special handling: pipeline uses today's log file
    if job["script"] == "main.py":
        last_run_h = _pipeline_last_run_hours()
        hours_since_scheduled = (now_edt - scheduled_at).total_seconds() / 3600
        # Missed if: pipeline hasn't run since the scheduled time (+ 10 min grace)
        missed = last_run_h > hours_since_scheduled + 0.17
        return missed, scheduled_at

    # Special handling: digest uses buffer contents
    if job["script"] == "email_buffer.py":
        missed = _buffer_has_emails()
        return missed, scheduled_at

    # Standard: check output file age vs scheduled time
    if job["output_file"] is None:
        return False, None

    output_age_h = _output_age_hours(job["output_file"])
    hours_since_scheduled = (now_edt - scheduled_at).total_seconds() / 3600

    # Missed if output is older than time-since-scheduled + small grace window (15 min)
    missed = output_age_h > (hours_since_scheduled + 0.25)
    return missed, scheduled_at


def _run(job: dict) -> bool:
    """Execute one job. Returns True on success."""
    cmd  = [PYTHON, str(BASE_DIR / job["script"])] + job.get("args", [])
    suffix = job["name"].replace(" ", "_")
    log  = LOG_DIR / f"catchup_{suffix}_{_now_edt().strftime('%Y%m%d_%H%M')}.log"
    LOG_DIR.mkdir(exist_ok=True)
    timeout_s = job.get("timeout_s", DEFAULT_JOB_TIMEOUT_S)

    logger.info(f"  ▶ [{job['name']}]  {job['script']} {' '.join(job.get('args', []))}")
    try:
        result = subprocess.run(
            cmd, cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=timeout_s,
        )
        with open(log, "w") as f:
            f.write(result.stdout + "\n" + result.stderr)
        if result.returncode == 0:
            logger.info(f"  ✅ [{job['name']}] completed OK")
            return True
        else:
            logger.warning(f"  ⚠ [{job['name']}] exited {result.returncode} — see {log.name}")
            return False
    except subprocess.TimeoutExpired as e:
        # Write what the job managed to say before it was killed. This branch
        # used to discard it: the log file is only written in the success path
        # above, so 968 timeouts produced no per-job log at all and the one
        # question worth asking — where was it when the clock ran out? — had no
        # artifact to answer it. TimeoutExpired carries the partial output when
        # capture_output is set; keeping it costs nothing and makes the next
        # timeout diagnosable instead of merely counted.
        def _text(v):
            # TimeoutExpired.stdout arrives as bytes or str depending on which
            # branch of communicate() raised — coerce rather than assume, or the
            # diagnostic is lost to a TypeError in the handler that exists to
            # save it.
            if v is None:
                return ""
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)

        try:
            with open(log, "w") as f:
                f.write(_text(e.stdout) + "\n" + _text(e.stderr))
        except Exception:
            pass
        logger.error(f"  ❌ [{job['name']}] timed out after {timeout_s}s — see {log.name}")
        return False
    except Exception as e:
        logger.error(f"  ❌ [{job['name']}] failed to launch: {e}")
        return False


# =============================================================================
# MAIN
# =============================================================================

def run_catch_up():
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — WAKE-UP CATCH-UP RUNNER")
    logger.info(f"  {_now_edt().strftime('%Y-%m-%d %H:%M')} EDT")
    logger.info("=" * 60)

    # ── Prevent overlapping catch-up runs ────────────────────────────────────
    if LOCKFILE.exists():
        lock_age_min = (datetime.now().timestamp() - LOCKFILE.stat().st_mtime) / 60
        if lock_age_min < 45:
            logger.info(f"  Catch-up already running ({lock_age_min:.0f} min ago) — skipping.")
            return []
        LOCKFILE.unlink(missing_ok=True)  # stale lock from a crashed run

    LOCKFILE.touch()

    try:
        # ── Step 1: Discover missed jobs ─────────────────────────────────────
        missed_queue: list[tuple[datetime, dict]] = []

        for job in JOB_REGISTRY:
            is_missed, scheduled_at = _is_job_missed(job)
            if is_missed:
                missed_queue.append((scheduled_at, job))
                logger.info(
                    f"  MISSED: [{job['name']}] — was scheduled "
                    f"{scheduled_at.strftime('%H:%M')} EDT, output is stale"
                )

        if not missed_queue:
            logger.info("  ✓ All agents are current — nothing to catch up.")
            return []

        # ── Step 2: Sort by scheduled time (oldest first = correct order) ────
        missed_queue.sort(key=lambda x: x[0])

        # ── Step 3: De-duplicate pipeline runs ───────────────────────────────
        # Multiple pipeline slots (9:45, 12:00, 2:00, 4:15) may all be missed.
        # Running main.py 4× in a row is wasteful. Keep only the LATEST missed
        # pipeline slot (most up-to-date run), but preserve its position in the
        # sorted order so it runs after earlier dependencies.
        pipeline_entries = [(i, t, j) for i, (t, j) in enumerate(missed_queue)
                            if j["script"] == "main.py"]
        if len(pipeline_entries) > 1:
            # Keep only the last one (latest scheduled time)
            keep_idx = pipeline_entries[-1][0]
            remove_indices = {e[0] for e in pipeline_entries[:-1]}
            missed_queue = [(t, j) for i, (t, j) in enumerate(missed_queue)
                            if i not in remove_indices]
            logger.info(
                f"  De-duplicated {len(pipeline_entries)} missed pipeline slots → "
                f"running once at latest missed slot "
                f"({pipeline_entries[-1][1].strftime('%H:%M')} EDT)"
            )

        # ── Step 4: Log the run plan ──────────────────────────────────────��──
        logger.info(f"\n  Run plan ({len(missed_queue)} job(s) in scheduled order):")
        for i, (sched, job) in enumerate(missed_queue, 1):
            logger.info(f"    {i}. [{sched.strftime('%H:%M')}] {job['name']}")

        # ── Step 5: Execute in order ─────────────────────────────────────────
        ran       = []
        failed    = []
        logger.info("")

        for sched_time, job in missed_queue:
            ok = _run(job)
            if ok:
                ran.append(job["name"])
            else:
                failed.append(job["name"])

        # ── Summary ──────────────────────────────────────────────────────────
        logger.info(f"\n  {'='*55}")
        logger.info(f"  CATCH-UP COMPLETE")
        logger.info(f"  ✅ Ran:    {', '.join(ran) if ran else 'none'}")
        if failed:
            logger.info(f"  ❌ Failed: {', '.join(failed)}")
        logger.info(f"  {'='*55}")

        return ran

    finally:
        LOCKFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    run_catch_up()

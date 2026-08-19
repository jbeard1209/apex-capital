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
from typing import Optional

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

    # ── Main pipeline — REMOVED 2026-08-17 (decision #81) ────────────────────
    # Four entries used to live here (pipeline_0945/1200/1400/1615), running
    # main.py on the Mac. They are gone, and the Mac no longer runs the
    # pipeline at all.
    #
    # The Mac's own crontab header has said "NO trading, NO agents on cron"
    # since 2026-07-03 — the firm runs on the GCP VM, and the Mac is a
    # development machine. launchd ran the full pipeline anyway, four times a
    # day by design and 12-22 times a day in practice until 47ee16e. Every
    # entry it produced was refused at the broker: `single_writer.py` is in
    # enforce mode with the VM designated, so the Mac's work came back
    # BLOCK_ENTRY 132 times across two sampled days and zero of its buys
    # reached Alpaca. What it bought was compute, API spend, and a `data/`
    # directory that diverges from the host that actually trades — there is no
    # state sync between the two.
    #
    # The one thing it did contribute was exits (5 ALLOW_EXIT on 08-14), and
    # those were a side effect of the runaway rather than designed coverage.
    # That capability is kept deliberately below, in `_mac_exit_guard()`, and
    # gated on the VM having actually gone quiet — which is the only condition
    # under which the Mac's exit path is worth anything. A permanent parallel
    # exit path on a clamshell-sleeping laptop is not a safety net; a fallback
    # that arms itself when the primary dies is.

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

    # ── Daily digest — REMOVED 2026-08-17, and the removal is the fix ─────────
    # This entry read: "If missed, send as soon as Mac wakes — user shouldn't go
    # without their digest just because Mac was off at 6 PM." That reasoning was
    # sound when the Mac was the firm. It stopped being sound when the VM became
    # the designated writer, because the two hosts share no state: each keeps
    # its own data/email_buffer.json, so this job did not "catch up" the VM's
    # digest, it composed and sent a SECOND one out of the Mac's own buffer.
    # That is the two-digests-in-a-row Jack received.
    #
    # The failover it was reaching for is real and is kept — it just belongs at
    # the choke point, not in one of four schedulers. `email_buffer
    # ._may_send_digest()` lets a non-designated host send only when the
    # designated one has gone quiet, judged by the same vm_state_monitor.json
    # that gates `_mac_exit_guard` below. Leaving this entry AND adding that
    # guard would have been harmless-looking and wrong: the job would fire
    # nightly, no-op, and read in the logs as if the Mac were still covering.

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

    # ── Coherence laws (Mac-side, and only Mac-side) ──────────────────────────
    # The pipeline runs these on the VM too, but one of the seven can ONLY run
    # here: `deployed_set_matches_repo` compares the git checkout against the
    # trading host, and the VM is not a git checkout — deploy.sh rsyncs with
    # --exclude=.git/. On the VM that law returns CANNOT_TELL naming this
    # machine as the owner, so if the Mac never runs it, the one law that would
    # have caught "the fix never reached production" is answered by nobody.
    #
    # This is now the substantive reason the Mac's scheduler exists at all
    # (#81 removed the pipeline). Read-only: it places no orders and writes
    # only data/coherence_report.json plus, on a violation, at most one row per
    # law in the Chairman's queue.
    {
        "name":            "coherence_laws",
        "script":          "coherence_laws.py",
        "args":            ["--quiet", "--exit-on-violation-only"],
        "output_file":     "data/coherence_report.json",
        "ttl_hours":       7,
        "fire_times_edt":  [9.0, 15.0, 21.0],
        "run_days":        "all",
        "catchup_window":  (0, 24),
        "once_per_wakeup": True,
    },

    # ── Internal audit (the third line) ───────────────────────────────────────
    # Added 2026-08-19 because #81 broke it here without anyone deciding to.
    # `internal_audit_agent` has exactly one caller — main.py — and #81 removed
    # main.py from this host. The module stayed deployed, its report stayed on
    # disk, and a frozen "0 blind, 0 uncovered" is indistinguishable from a
    # fresh one, so nothing looked wrong; wiring_audit caught it only because it
    # measures the report's AGE rather than its contents. The third line has no
    # fourth line, so a host where it silently stopped is a host whose controls
    # are unaudited.
    #
    # It runs 15 minutes after the coherence laws on purpose: its denominator
    # check grades THAT report, and grading yesterday's would be the same fossil
    # in a different file. catch_up orders by scheduled fire time, so the offset
    # is what sequences them.
    #
    # Escalation is left ON. Both hosts keep their own Chairman queue (there is
    # no state sync), so this cannot duplicate a row the VM wrote — and the ids
    # are deterministic (`internal-audit-blind-<control>`), so within a queue it
    # upserts and retires rather than appends. The alternative, --no-escalate,
    # would drop any finding only this host can see: `empty_success` is computed
    # from THIS host's coherence report.
    #
    # Exits 1 when it has findings, which catch_up logs as a warning. That is
    # the intended reading — the report is written before the exit.
    {
        "name":            "internal_audit",
        "script":          "internal_audit_agent.py",
        "args":            ["--report"],
        "output_file":     "data/internal_audit_report.json",
        "ttl_hours":       7,
        "fire_times_edt":  [9.25, 15.25, 21.25],
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


# The file main.py actually writes when it starts. Both this and the
# completion heartbeat go to the same path, so its mtime/`last_started` is the
# firm's real answer to "has the pipeline run recently on THIS host".
PIPELINE_HEARTBEAT = DATA_DIR / "pipeline_heartbeat.json"


def _pipeline_last_run_hours() -> Optional[float]:
    """
    Hours since main.py last STARTED on this host, or None when we cannot tell.

    This used to read logs/cron_<today>.log — the redirect target of the Mac's
    crontab. Two things were wrong with that, and the second hid behind the first.

    The check was self-defeating from the start: `_run()` captures the child's
    output and writes it to logs/catchup_<job>_<ts>.log, so a pipeline run that
    THIS module launched never touched cron_<today>.log. The runner could not
    see its own work. While the Mac crontab was live its own runs refreshed the
    file often enough to mask it — but the Mac still logged 18-22 pipeline runs
    on days like 2026-05-25 and 2026-06-02, against a design of 4.

    Then on 2026-07-03 the Mac crontab was deliberately emptied ("the firm RUNS
    ON THE GCP VM ... the Mac is a development machine only"), and the last
    writer of cron_*.log went away. From that day the file simply did not exist
    and this function returned its "cannot read it" constant, 999. 999 hours ago
    is not a neutral value here: `_is_job_missed` compares it to
    hours-since-scheduled, so 999 means "definitely missed" and every launchd
    tick inside the catch-up window re-launched the full trading pipeline. That
    is what 2026-08-14 looks like on disk — pipeline_1400 executed at 14:19,
    14:44, 15:07, 15:31 and 15:55, each a complete run that reaches the Manager
    and can place orders, on the same Alpaca account the VM trades. Nothing
    reported it, because a job that runs too often never fails.

    Two rules come out of that and are kept here deliberately:
      1. Read a file something still writes. main.py writes this one itself,
         at the top of every run — see main.py's "Pipeline heartbeat" block.
      2. Never return a number that means "act". When the signal cannot be
         read, return None and let the caller refuse; a monitor that cannot
         tell must not be able to authorise a trade. (Same shape as the FI
         credit feed's fabricated 350bp spread, and memory `nan-is-not-none`.)
    """
    if not PIPELINE_HEARTBEAT.exists():
        return None
    try:
        with open(PIPELINE_HEARTBEAT) as f:
            hb = json.load(f)
        ts = hb.get("last_started") or hb.get("last_completed")
        if ts:
            age = (datetime.now() - datetime.fromisoformat(str(ts)[:26])).total_seconds() / 3600
            return max(age, 0.0)
    except Exception:
        pass
    # The file exists but carries no usable timestamp — its mtime is still a
    # real observation of when main.py last wrote it.
    try:
        return (datetime.now().timestamp() - PIPELINE_HEARTBEAT.stat().st_mtime) / 3600
    except OSError:
        return None


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

    # Special handling: the pipeline has no output_file — ask its heartbeat.
    if job["script"] == "main.py":
        last_run_h = _pipeline_last_run_hours()
        if last_run_h is None:
            # Fail closed. "I don't know when the pipeline last ran" is not a
            # reason to run the pipeline — it is a reason to say so loudly and
            # do nothing. Running main.py places orders; the cost of a wrong
            # "yes" here is real trades, the cost of a wrong "no" is a skipped
            # catch-up that the next real run covers.
            logger.error(
                f"  ✗ [{job['name']}] NOT run: cannot tell when main.py last "
                f"started — {PIPELINE_HEARTBEAT} is missing or unreadable. "
                f"Fix the heartbeat; do not relax this check."
            )
            return False, scheduled_at
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

VM_STATE_FILE = DATA_DIR / "vm_state_monitor.json"

# How stale the VM's state check may be before the Mac stops believing it.
# The LaunchAgent runs vm_state_monitor every 15 minutes, so anything past an
# hour means that monitor is itself not running.
VM_STATE_MAX_AGE_H = 1.0

# Where the guard records its last run, and how long it waits between runs when
# the VM's state is merely UNKNOWN rather than known-bad.
EXIT_GUARD_STAMP = DATA_DIR / ".mac_exit_guard_last"
UNCERTAIN_GUARD_COOLDOWN_H = 4.0


def _mac_exit_guard() -> bool:
    """Run the canary's exit enforcement, but only if the VM has gone quiet.

    Decision #81. The Mac stopped running main.py; this is what replaced it.

    `canary_agent.py` carries `_enforce_canary_exits()` — the loop-breaker that
    submits direct market-close orders for positions past their stop or take-
    profit, bypassing deliberation entirely. On the VM it fires every few
    minutes. The Mac needs it only when the VM cannot: on 2026-08-06 the free
    trial ended, the instance sat STOPPED, and the firm ran exit-only for eight
    days while nothing said so.

    The gate is `vm_state_monitor.json`, which asks the two questions no single
    check can answer alone — GCP for "is the box on", the broker for "is the
    writer writing". Reusing it matters: a second, hand-rolled liveness test
    here would be a third opinion about whether the VM is alive, and the firm
    has been bitten by two definitions of one fact more than once.

    ON AN UNREADABLE GATE THIS RUNS THE CANARY ANYWAY, AND SAYS SO.
    That is deliberate and it is NOT a contradiction of the rule that a monitor
    which cannot read its signal must not act (memory `fallback-that-means-act`,
    and the 999 that launched 20 pipelines a day). The asymmetry is reversed
    here. That constant authorised ENTRIES: a wrong "yes" opens positions with
    real money behind it. This authorises EXITS, and only of positions already
    breaching their own stop — a wrong "yes" closes something that was going to
    be closed anyway, or no-ops because the canary re-checks Alpaca and finds
    the position already gone. A wrong "no" leaves a -8% position bleeding for
    days with nobody watching. Do not "fix" this into failing closed by
    pattern-matching to the entry case; the direction of the risk is the whole
    argument, and `single_writer.py` permits exits from a non-designated host
    for exactly this reason.
    """
    reason, certain = None, False
    try:
        raw = json.loads(VM_STATE_FILE.read_text())
        age_h = (datetime.now().timestamp() - VM_STATE_FILE.stat().st_mtime) / 3600
        power = str(raw.get("power", "")).upper()
        writer = str(raw.get("writer", "")).upper()
        if age_h > VM_STATE_MAX_AGE_H:
            reason = (f"{VM_STATE_FILE.name} is {age_h:.1f}h old (max "
                      f"{VM_STATE_MAX_AGE_H}h) — the monitor that watches the VM "
                      f"is not itself running, so the VM's state is unknown")
        elif writer in ("HIGH", "CRITICAL"):
            # Broker-sourced and authoritative: the writer has stopped writing.
            reason = f"designated writer silent — {raw.get('writer_msg') or writer}"
            certain = True
        elif power == "CRITICAL":
            reason = f"GCP reports the instance {raw.get('gcp_status')!r}, not RUNNING"
            certain = True
        elif power == "UNKNOWN" and writer in ("PASS", "SKIP"):
            # gcloud could not answer, but the broker could, and it says the
            # writer is trading. That is the question that actually matters;
            # power state is a proxy for it. Standing down here is what keeps
            # an unauthenticated gcloud from arming this guard every 20
            # minutes forever — see the cooldown below for the case where
            # NOTHING can answer.
            logger.info(f"  Exit guard: GCP unreachable ({raw.get('detail')!r}) but "
                        f"the broker says the writer is live — Mac stands down.")
            return False
        elif power == "UNKNOWN":
            reason = (f"neither GCP nor the broker can confirm the writer "
                      f"(power UNKNOWN: {raw.get('detail')!r}, writer {writer})")
    except FileNotFoundError:
        reason = (f"{VM_STATE_FILE.name} does not exist — the Mac has never "
                  f"successfully checked on the VM")
    except (ValueError, OSError) as e:
        reason = f"{VM_STATE_FILE.name} unreadable: {type(e).__name__}: {e}"

    if reason is None:
        logger.info("  Exit guard: VM is up and writing — Mac stands down.")
        return False

    # Cooldown, and it is only for the uncertain case. When the VM is KNOWN
    # down the guard runs on every tick, because that is the emergency it was
    # built for. When we merely cannot tell — an unauthenticated gcloud, a
    # missing state file — running the canary every 20 minutes forever is how
    # a safety net turns into the thing it was meant to catch. Every detector
    # in this firm looks for absence; nothing would ever report this.
    if not certain:
        try:
            last = EXIT_GUARD_STAMP.stat().st_mtime
            since_h = (datetime.now().timestamp() - last) / 3600
            if since_h < UNCERTAIN_GUARD_COOLDOWN_H:
                logger.warning(
                    f"  Exit guard would arm ({reason}) but ran {since_h:.1f}h ago "
                    f"and the VM state is UNCERTAIN, not known-bad — holding off "
                    f"for {UNCERTAIN_GUARD_COOLDOWN_H}h. Owner: whoever restores "
                    f"the VM state check (gcloud auth on this Mac).")
                return False
        except OSError:
            pass

    logger.warning(f"  ⚠ Exit guard ARMED ({'certain' if certain else 'uncertain'}): {reason}")
    logger.warning("    Running canary_agent.py for stop/take-profit enforcement. "
                   "This host places no entries; single_writer blocks those.")
    ok, _out = _run({"name": "mac_exit_guard", "script": "canary_agent.py",
                     "args": [], "timeout_s": 300})
    try:
        EXIT_GUARD_STAMP.touch()
    except OSError:
        pass
    logger.warning(f"    Exit guard finished: {'ok' if ok else 'FAILED'}")
    return True


def run_catch_up():
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — WAKE-UP CATCH-UP RUNNER")
    logger.info(f"  {_now_edt().strftime('%Y-%m-%d %H:%M')} EDT")
    logger.info("=" * 60)

    # Before anything else: is the firm's writer alive? This runs on every
    # 20-minute launchd tick, ahead of the missed-job sweep, because an exit
    # that waits for a catch-up queue is not a safety net.
    try:
        _mac_exit_guard()
    except Exception as e:
        logger.error(f"  Exit guard raised {type(e).__name__}: {e} — continuing")

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

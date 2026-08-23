#!/usr/bin/env python3
"""
APEX CAPITAL — WAKEUP RECOVERY AGENT
======================================
Automatically detects when the GCP VM has been down and reruns all missed
pipeline jobs in dependency order.  Fires on @reboot (via crontab) and also
as a periodic check every 6 hours to catch partial outages.

WHICH DAYS and WHICH LOG a job uses are DERIVED from the crontab, never
declared here — see the note above JOBS. The declared copy is kept only as a
second measurement so drift can be alarmed on.

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
  @reboot:   cd ~/institutional-tracker && ~/venv/bin/python3 wakeup_recovery.py >> logs/wakeup_$(date +%Y%m%d).log 2>&1
  periodic:  0 */6 * * *  (same command)
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
# Each entry: (script, log_prefix, max_stale_hours, declared_dow, args)
# max_stale_hours: if the most recent log for today/yesterday is older than this,
#                  the job is considered missed.

def _today() -> str:
    return datetime.now().strftime("%Y%m%d")

def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

# ── Why two of these five fields are only a CROSS-CHECK, not the authority ────
# `log_prefix` and `weekday_only` restate, by hand, two facts the crontab
# already states — which log proves a job ran, and which days it runs. Both
# had drifted, silently, and in the same shape:
#
#   * fe_pipeline / fi_pipeline / overnight_ml_runner declared log prefixes
#     ("fe_pipeline", "fi_pipeline", "ml_runner_daily") that NO scheduled job
#     writes. The crontab writes fe_/fi_/ml_overnight_. `_run_job()` names its
#     log after log_prefix, so each of those three was reading a file only THIS
#     SCRIPT had ever written — identical to the main.py "main" vs "cron" bug
#     fixed in 8e4f97b, left standing on three sibling rows. Effect: the real
#     cron run never counted as freshness evidence, the self-written log aged
#     past max_stale_hours, recovery fired, wrote the log again, and repeated
#     forever. Measured 2026-08-23: fe_pipeline ran EVERY day (12:12, 18:02,
#     00:14, 06:08, 12:08) against a real cron of 15:04 Mon-Fri.
#   * weekday_only said False for three weekday-only jobs and True for
#     canary_agent, which the crontab runs on weekends too (`*/30 * * * 0,6`).
#     So recovery "restored" the weekend and skipped the one job that really
#     was scheduled — wrong in BOTH directions, from one hand-kept flag.
#
# The fix is not better values. Values drift; a derivation does not. Both
# fields are now DERIVED from the crontab and the declared value is kept only
# so a disagreement can be alarmed on (two independent sources for one fact —
# the coherence-law shape in CONTROLS.md). Only max_stale_hours and extra_args
# are genuinely ours: cron cannot express "how late is too late".
#
# `declared_dow` is a cron day-of-week LITERAL, not a bool. The old field was
# `weekday_only: bool`, which cannot express overnight_ml_runner's real
# schedule (`2-6`, Tue-Sat) at all: no value of that flag is correct for it,
# so the drift there was unfixable without changing the type. A field whose
# domain is smaller than the thing it describes will be wrong eventually.
JOBS = [
    # (script_name, log_prefix, max_stale_hours, declared_dow, extra_args)
    ("morning_briefing.py",          "briefing",          25, "1-5", []),
    # rd_team_leader.py's own cron (05:00, 08:00, 16:00, 19:15 UTC daily) has a
    # natural max gap of ~9h45m (19:15 -> next day 05:00); 3h falsely flagged it
    # as missed on almost every 6h wakeup_recovery check, forcing redundant full
    # pipeline reruns (duplicate R&D Briefing + IRM review emails, 2026-07-17/18).
    ("rd_team_leader.py",            "rd",                11, "*",   []),
    # Daily, not weekday-only: `*/30 * * * 0,6` runs canary all weekend. The
    # old True skipped the weekend check on the one agent whose whole job is
    # noticing that something broke while nobody was looking.
    ("canary_agent.py",              "canary",             1, "*",   []),
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
    ("main.py",                      "cron",               7, "1-5", []),
    ("fe_pipeline.py",               "fe",                25, "1-5", []),
    ("fi_pipeline.py",               "fi",                25, "1-5", []),
    ("board_meeting.py",             "board",             25, "1-5", []),
    ("cit_director.py",              "cit",                7, "*",   ["--session", "morning"]),
    # "2-6" = Tue-Sat: the 03:00 UTC run trains on the PREVIOUS weekday's close,
    # so Saturday's run is Friday's data and Monday has none to train on. This
    # is the schedule the old bool could not represent.
    ("overnight_ml_runner.py",       "ml_overnight",      25, "2-6", []),
    # Staleness here is measured from logs/digest_YYYYMMDD.log, which is
    # written by whoever RAN the script — not by whoever sent mail. So this
    # entry cannot tell a missed 22:00 cron from one that ran and correctly
    # sent nothing. It re-runs on both, and that is fine: since 2026-08-17
    # `email_buffer.flush_digest()` refuses a second send on the same day, so
    # the redundant run is a no-op rather than Jack's second digest. Do not
    # delete this line to fix a duplicate — the duplicate is fixed at the
    # choke point, and deleting it would lose the genuinely-missed case.
    ("email_buffer.py",              "digest",            25, "*",   []),
    ("logic_team.py",                "logic_team",        13, "*",   []),
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

# ─── Schedule derivation (the crontab is the authority) ───────────────────────
# Ground truth is data/vm_crontab_snapshot.txt, mirrored from the VM's live
# crontab by scripts/refresh_crontab_snapshot.sh (11:10 daily on the VM, and by
# deploy.sh before every rsync). wiring_audit.py and hr_agent.py already treat
# that file as production truth; reading a second, hand-kept copy here is what
# produced the drift documented above. `crontab -l` is the fallback for a host
# running its own scheduler.

CRONTAB_SNAPSHOT = BASE_DIR / "data" / "vm_crontab_snapshot.txt"
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4,
              "fri": 5, "sat": 6}


def _crontab_text() -> tuple:
    """(text, source). Empty text means the schedule could not be read."""
    if CRONTAB_SNAPSHOT.exists():
        try:
            txt = CRONTAB_SNAPSHOT.read_text()
            if txt.strip():
                return txt, f"snapshot({CRONTAB_SNAPSHOT.name})"
        except Exception:
            pass
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout, "crontab -l"
    except Exception:
        pass
    return "", "unavailable"


def _expand_dow(field: str):
    """Cron day-of-week field -> set of Python weekdays (Mon=0 … Sun=6).

    Returns None when the field cannot be parsed. None is NOT "runs never" —
    every caller must read it as "unknown", and the only safe reading of
    unknown is "assume it runs today". Skipping a scheduled job is a missed
    agent; running an unscheduled one is waste. Those costs are not equal, so
    the degraded path resolves toward the cheap error, loudly.
    """
    field = field.strip().lower()
    if not field:
        return None
    days = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                return None
            step = int(step_s)
        if part in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in part.strip("-"):
            lo_s, _, hi_s = part.partition("-")
            lo = _DOW_NAMES.get(lo_s.strip(), None)
            hi = _DOW_NAMES.get(hi_s.strip(), None)
            if lo is None:
                lo = int(lo_s) if lo_s.strip().isdigit() else None
            if hi is None:
                hi = int(hi_s) if hi_s.strip().isdigit() else None
            if lo is None or hi is None:
                return None
        else:
            v = _DOW_NAMES.get(part.strip(), None)
            if v is None:
                v = int(part.strip()) if part.strip().isdigit() else None
            if v is None:
                return None
            lo = hi = v
        if not (0 <= lo <= 7 and 0 <= hi <= 7) or hi < lo:
            return None
        # cron: 0 and 7 both mean Sunday. Python: Mon=0 … Sun=6.
        for cron_d in range(lo, hi + 1, step):
            days.add(((cron_d % 7) + 6) % 7)
    return days or None


def _crontab_schedule() -> tuple:
    """({script: {'dow': set, 'prefixes': set}}, source)."""
    text, source = _crontab_text()
    sched: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        if line.startswith("@"):
            continue          # @reboot has no day-of-week to derive
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        _minute, _hour, dom, month, dow_f, command = fields
        dow = _expand_dow(dow_f)
        # Cron ORs day-of-month against day-of-week when both are restricted,
        # and a month restriction makes "which weekday" meaningless for our
        # purpose. Either way we cannot narrow the day from the DOW field
        # alone, so we widen to every day rather than risk skipping.
        if dom.strip() not in ("*", "?") or month.strip() != "*":
            dow = set(range(7))
        if dow is None:
            dow = set(range(7))

        prefix = None
        marker = ">> logs/"
        if marker in command:
            tail = command.split(marker, 1)[1]
            name = tail.split("_$(date", 1)[0].split(".log", 1)[0]
            prefix = name.strip().strip('"').strip("'") or None

        for token in command.split():
            token = token.strip('"').strip("'")
            if not token.endswith(".py"):
                continue
            script = os.path.basename(token)
            entry = sched.setdefault(script, {"dow": set(), "prefixes": set()})
            entry["dow"] |= dow
            if prefix:
                entry["prefixes"].add(prefix)
    return sched, source


def _resolve_job(script: str, declared_prefix: str, declared_dow: str,
                 sched: dict) -> tuple:
    """(dow, prefixes, disagreements) — crontab wins, the table is the check.

    The declared values are not authority and not decoration: they are the
    second independent measurement. When they disagree with production, that
    disagreement is the finding — somebody edited the crontab without
    revisiting what recovery believes, or the reverse.
    """
    notes: list = []
    entry = sched.get(script)
    if not entry:
        notes.append(f"{script}: named in JOBS but absent from the crontab — "
                     f"recovery is the only thing that runs it")
        want = _expand_dow(declared_dow) or set(range(7))
        return want, {declared_prefix}, declared_prefix, notes

    dow = entry["dow"] or set(range(7))
    declared_set = _expand_dow(declared_dow)
    if declared_set is not None and declared_set != dow:
        notes.append(f"{script}: JOBS declares dow={declared_dow} "
                     f"({sorted(declared_set)}) but the crontab schedules "
                     f"{sorted(dow)} — using the crontab")

    prefixes = set(entry["prefixes"])
    if prefixes and declared_prefix not in prefixes:
        notes.append(f"{script}: JOBS declares log prefix "
                     f"'{declared_prefix}' but the crontab writes "
                     f"{sorted(prefixes)} — a prefix no scheduled job writes "
                     f"means recovery is reading its own output")
    # Recovery WRITES to the prefix the crontab uses, so its own run becomes
    # freshness evidence for the same job the scheduler is tracking, instead
    # of accumulating a private log nobody else reads. _run_job() appends and
    # stamps a "RECOVERY RUN" header, so nothing is overwritten or disguised.
    write_prefix = (declared_prefix if declared_prefix in prefixes
                    else (sorted(prefixes)[0] if prefixes else declared_prefix))
    # Keep the declared prefix in the search set even when it disagrees: a
    # previous recovery run's own log is still evidence the job ran.
    prefixes.add(declared_prefix)
    return dow, prefixes, write_prefix, notes

DRIFT_REPORT = BASE_DIR / "data" / "wakeup_schedule_drift.json"


def _record_drift(notes: list, source: str) -> None:
    """Persist schedule disagreements where a sweep can find them.

    A warning in a log nobody greps is not a detector. wiring_audit.py's
    empty-file tell reads data/*.json, so an EMPTY drift report is the healthy
    state and a populated one is a finding with a timestamp on it.
    """
    try:
        DRIFT_REPORT.parent.mkdir(parents=True, exist_ok=True)
        DRIFT_REPORT.write_text(json.dumps({
            "generated_at": datetime.now().isoformat(),
            "schedule_source": source,
            "host": os.uname().nodename,
            "n_disagreements": len(notes),
            "disagreements": notes,
        }, indent=2))
    except Exception as e:
        logger.warning(f"[Recovery] Could not write drift report: {e}")


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


def _freshest_log_age(prefixes) -> float | None:
    """Age of the most recent log across every prefix this job may write.

    A job proven to have run by ANY of its logs has run. Taking the minimum
    (freshest) is what makes the real cron log count again: before this,
    fe_pipeline's freshness came from one hand-named prefix, and if that prefix
    was wrong the fifteen-o'clock cron run was invisible no matter how recent.
    """
    ages = [a for a in (_log_age_hours(p) for p in sorted(prefixes))
            if a is not None]
    return min(ages) if ages else None

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
    today_dow  = now.weekday()

    sched, sched_source = _crontab_schedule()
    plan: list = []
    drift: list = []
    for script, declared_prefix, max_stale_h, declared_dow, extra_args in JOBS:
        dow, prefixes, write_prefix, notes = _resolve_job(
            script, declared_prefix, declared_dow, sched)
        drift.extend(notes)
        plan.append((script, prefixes, max_stale_h, dow, extra_args,
                     write_prefix))

    logger.info(f"[Recovery] === Wakeup Recovery Agent starting ===")
    logger.info(f"[Recovery] VM uptime: {uptime_min:.1f} min | "
                f"{now.strftime('%Y-%m-%d %H:%M')} (weekday {today_dow}) | "
                f"schedule source: {sched_source}")
    if sched_source == "unavailable":
        # Fail toward running, not toward silence — but never silently. An
        # unreadable schedule that quietly reverted to "weekdays" would be the
        # same class of bug this function was written to remove.
        logger.warning("[Recovery] Could not read any crontab — falling back "
                       "to the declared JOBS schedule, which is exactly the "
                       "hand-kept table that drifted. Fix the snapshot.")
    for note in drift:
        logger.warning(f"[Recovery] SCHEDULE DRIFT — {note}")
    # Written every run, clean or not. Writing only on failure leaves the last
    # bad report sitting there after the drift is fixed, and a reader cannot
    # tell a current finding from a fossil ([[presence-is-not-currency]]).
    _record_drift(drift, sched_source)

    # Decide if recovery is warranted:
    # - Force flag overrides everything
    # - Fresh reboot (uptime < 15 min) always triggers full recovery
    # - Periodic check (not a reboot): only recover if jobs are stale
    is_fresh_reboot = uptime_min < 15

    if not force and not is_fresh_reboot:
        # Periodic check mode — only run if something is stale
        stale_count = 0
        for script, prefixes, max_stale_h, dow, _, _wp in plan:
            if today_dow not in dow:
                continue
            age = _freshest_log_age(prefixes)
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
        for script, prefixes, max_stale_h, dow, _, _wp in plan:
            if today_dow not in dow:
                logger.info(f"  {'– NOT TODAY':12s} {script:<30s} "
                            f"(scheduled {sorted(dow)}, today {today_dow})")
                continue
            age = _freshest_log_age(prefixes)
            status = f"{age:.1f}h old" if age is not None else "NO LOG"
            stale  = age is None or age > max_stale_h
            flag   = "⚠ STALE" if stale else "✓ OK"
            logger.info(f"  {flag:12s} {script:<30s} (max={max_stale_h}h, "
                        f"actual={status}, logs={sorted(prefixes)})")
        return

    # Wait for network if fresh reboot
    if is_fresh_reboot:
        logger.info("[Recovery] Waiting 45s for network to stabilize...")
        if not dry_run:
            time.sleep(45)

    ran: list[str]     = []
    skipped: list[str] = []
    failed: list[str]  = []

    for script, prefixes, max_stale_h, dow, extra_args, write_prefix in plan:
        # Not scheduled today -> there is nothing to recover. A job that was
        # never due cannot be missed, and "restoring" it produced both the
        # phantom "Pipeline Restored" mail and a full weekend FE/FI run.
        if today_dow not in dow:
            skipped.append(f"{script} (not scheduled today)")
            logger.info(f"[Recovery] SKIP {script} — crontab schedules "
                        f"{sorted(dow)}, today is {today_dow}")
            continue

        # Check freshness
        age = _freshest_log_age(prefixes)
        if age is not None and age <= max_stale_h and not force:
            skipped.append(f"{script} ({age:.1f}h old, ok)")
            logger.info(f"[Recovery] SKIP {script} — fresh ({age:.1f}h old, limit={max_stale_h}h)")
            continue

        age_str = f"{age:.1f}h old" if age is not None else "no log"
        logger.info(f"[Recovery] STALE {script} ({age_str}, limit={max_stale_h}h) — running now")

        success = _run_job(script, write_prefix, extra_args, dry_run=dry_run)
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

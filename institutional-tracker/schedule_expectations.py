#!/usr/bin/env python3
"""
APEX CAPITAL — WHAT THE SCHEDULE ITSELF SAYS A GAP MAY BE
=========================================================
One shared answer to "is this output late?", derived from the producer's own
schedule instead of a constant somebody picked.

WHY THIS EXISTS
---------------
Six agents each answered that question with a hardcoded number of hours:

    fi_cit_agent.STALE_HOURS                   = 25
    fe_cit_agent._PIPELINE_LOG_STALE_HOURS     = 25
    cit_pipeline_integrity_agent._AGENT_STALE_H= 26
    cit_watchdog.STALE_HOURS / EMERGENCY_HOURS = 12 / 24
    kalshi_watchdog.STALL_HOURS                = 26

Every one of those reads as reasonable: "the job is daily, so a day and a bit
of slack." And every one of them is describing a producer scheduled
`* * 1-5` — weekdays only. Expand that crontab and the largest gap the
schedule itself asks for is 72.0h (Friday's run to Monday's), or 65.5h for
main.py. A 25h threshold against a 72h legitimate gap is not a strict
standard. It is an alarm that must fire every weekend, on a firm that is
behaving exactly as designed, and an alarm that fires every weekend is one
people learn to scroll past (`rd_system_performance.ALERT_FATIGUE_THRESHOLD`
exists because of this). Worse, it means the Monday morning where a producer
genuinely died looks identical to the fifty Mondays where it did not.

This is the same defect as `hr_agent.ROSTER_MAX_AGE_H = 48` (commit 63084f3),
and it fails the same way: **staleness was priced in wall-clock hours when the
quantity that matters is "did it miss a scheduled run?"** Hours are a proxy.
The scheduled-fire time is the real quantity, it is already written down in the
host's own crontab, and reading it needs no threshold at all.

`catch_up.py` has been doing it correctly since it was written — step 2 of its
header is literally "for each job, find the most recent scheduled fire time
that has passed." This module is that logic, extracted so the checkers can
share it, rather than a seventh independent opinion.

WHAT IT MAY NOT DO
------------------
It never substitutes a default for a schedule it could not read. A degraded
read and "the producer is fine" are different states with opposite
consequences, and collapsing them is memory `fallback-that-means-act` — the
same bug `_scheduled_pipeline_runs_today` was fixed for on 2026-08-17. Hence
four verdicts, not two:

    LATE                — a scheduled fire time has passed with grace and the
                          output predates it. The producer missed a run.
    ON_TIME             — the output is newer than the last scheduled fire.
    NOT_SCHEDULED_HERE  — the scheduler read fine and does not run this script
                          on this host. The caller MUST NOT judge; there is no
                          state sync between hosts and this host's data/ is not
                          the producer's data/.
    SCHEDULE_UNREADABLE — `crontab -l` failed. A finding, never a pass.

It reads the LIVE scheduler, never a doc or a snapshot. A doc is a third
party's claim about the schedule; `crontab -l` is the schedule (memory
`auditor_ground_truth`).

Usage:
    python3 schedule_expectations.py            # what this host schedules
    python3 schedule_expectations.py --check fi_pipeline.py data/fi_pipeline_log.json

Guarded by test_pipeline_smoke.py::TestLatenessComesFromTheSchedule.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# ── Verdicts ─────────────────────────────────────────────────────────────────
LATE                = "LATE"
ON_TIME             = "ON_TIME"
NOT_SCHEDULED_HERE  = "NOT_SCHEDULED_HERE"
SCHEDULE_UNREADABLE = "SCHEDULE_UNREADABLE"
NO_FIRE_YET         = "NO_FIRE_YET"     # scheduled here; nothing due yet

#: Verdicts that mean "no evidence of a missed run" -- but for three different
#: reasons, and only the first is a clean bill of health. The other three are
#: findings in their own right and must not be silently treated as passes.
NOT_LATE = (ON_TIME, NOT_SCHEDULED_HERE, SCHEDULE_UNREADABLE, NO_FIRE_YET)

# ── Grace: how long after a fire time the output may still legitimately be old
# Not a guess and not tuned. It is the firm's own largest configured job
# budget, `catch_up.PIPELINE_TIMEOUT_S = 2700` (45 min) — the longest the firm
# has already decided any single job may legitimately take. A job that started
# on its fire time and is still inside its own budget has not missed anything.
# Imported rather than copied so it cannot drift from the number it quotes.
try:
    from catch_up import PIPELINE_TIMEOUT_S as _LONGEST_JOB_BUDGET_S
except Exception:                                    # pragma: no cover
    _LONGEST_JOB_BUDGET_S = 2700
DEFAULT_GRACE_MIN = _LONGEST_JOB_BUDGET_S / 60.0

# How far back to look for the previous scheduled fire. Must exceed the largest
# gap any schedule in the crontab can produce; the observed maximum is 72.0h
# (Friday -> Monday on a `1-5` job), so 14 days is >4x headroom and the search
# is 14 cheap iterations.
_LOOKBACK_DAYS = 14


# =============================================================================
# Cron parsing
# =============================================================================

def _expand(field: str, lo: int, hi: int) -> List[int]:
    """Every value a single cron field covers, e.g. "*/5" or "13-20" or "0,30"."""
    out: set = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            try:
                step = max(int(s), 1)
            except ValueError:
                step = 1
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            try:
                a_s, b_s = part.split("-")[:2]
                a, b = int(a_s), int(b_s)
            except ValueError:
                continue
        else:
            try:
                a = b = int(part)
            except ValueError:
                continue
        if b < a:                    # cron wrap ("22-2"); expand both halves
            out.update(range(a, hi + 1, step))
            out.update(range(lo, b + 1, step))
            continue
        out.update(range(max(a, lo), min(b, hi) + 1, step))
    return sorted(out)


def _dow_matches(dow_field: str, when: datetime) -> bool:
    """cron day-of-week (0=Sun..6=Sat, 7 also Sun) against a datetime."""
    vals = set(_expand(dow_field, 0, 7))
    if 7 in vals:
        vals.add(0)
    return ((when.weekday() + 1) % 7) in vals      # Mon=0 -> cron 1


def _day_matches(dom_field: str, mon_field: str, dow_field: str,
                 when: datetime) -> bool:
    """Cron's day rule, including the dom/dow OR.

    POSIX cron ORs day-of-month and day-of-week when BOTH are restricted, and
    ANDs when only one is. Every Apex entry uses `*` for day-of-month so this
    branch is never exercised in production — it is here because a rule that is
    only correct for the inputs you happened to look at is the misspecified
    comparison this module exists to stop.
    """
    if when.month not in _expand(mon_field, 1, 12):
        return False
    dom_star = str(dom_field).strip() == "*"
    dow_star = str(dow_field).strip() in ("*", "0-6", "0-7")
    dom_ok = when.day in _expand(dom_field, 1, 31)
    dow_ok = _dow_matches(dow_field, when)
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow_ok
    if dow_star:
        return dom_ok
    return dom_ok or dow_ok


def read_crontab(cron_text: Optional[str] = None) -> Optional[str]:
    """This host's live crontab, or None if it could not be read.

    None means "could not look". It must never be turned into an empty
    schedule by a caller: an unreadable scheduler and a host that schedules
    nothing are different facts.
    """
    if cron_text is not None:
        return cron_text
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        # A host with no crontab at all exits non-zero with "no crontab for X".
        # That is a readable answer — it schedules nothing — and is distinct
        # from the OSError above, where the command itself could not run.
        if "no crontab" in (r.stderr or "").lower():
            return ""
        return None
    return r.stdout


_PEER_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "vm_crontab_snapshot.txt")
_CAPTURED_RE = re.compile(r"^#\s*Captured\s+(\S+?)Z?\s", re.M)
_SNAPSHOT_REFRESH = "refresh_crontab_snapshot.sh"


def peer_crontab(path: Optional[str] = None) -> Optional[str]:
    """The OTHER host's schedule, or None if it cannot be trusted.

    There is no state sync between the Mac and the VM, so a check running on
    one host cannot read the other's `crontab -l`. What it can read is
    `data/vm_crontab_snapshot.txt` — a verbatim mirror the VM refreshes daily
    and ships through git, which is how `wiring_audit` and `hr_agent` already
    measure reality.

    A snapshot is only worth trusting while it is current, and its own
    freshness is derived rather than guessed: the snapshot contains the cron
    line that refreshes it, so the longest gap that line can legitimately leave
    IS the staleness threshold. If the capture is older than that, the mirror
    has stopped being a mirror and this returns None — "could not look", never
    an empty schedule.
    """
    p = path or _PEER_SNAPSHOT
    try:
        with open(p) as fh:
            text = fh.read()
    except OSError:
        return None

    m = _CAPTURED_RE.search(text)
    if not m:
        return None                       # a mirror with no capture time is
                                          # not datable, so not trustable
    try:
        captured = datetime.fromisoformat(m.group(1)[:19])
    except ValueError:
        return None

    # The refresher's own line, read out of the thing it refreshes.
    own = entries_for(_SNAPSHOT_REFRESH, cron_text=text)
    if not own:
        return None                       # nothing refreshes it -> it is a
                                          # frozen document, not a mirror
    budget_h = max_legit_gap_h(_SNAPSHOT_REFRESH, cron_text=text)
    if budget_h is None:
        return None
    # The capture header is UTC; compare in UTC so a Mac on America/New_York
    # does not read a 4-hour-old snapshot as 4 hours in the future.
    age_h = (datetime.utcnow() - captured).total_seconds() / 3600.0
    if age_h > budget_h + DEFAULT_GRACE_MIN / 60.0:
        return None
    return text


def entries_for(script: str, cron_text: Optional[str] = None
                ) -> Optional[List[Tuple[str, str, str, str, str]]]:
    """The (min, hour, dom, mon, dow) of every crontab line that runs `script`.

    None      -> the scheduler could not be read.
    []        -> read fine; this host does not schedule `script`.
    """
    text = read_crontab(cron_text)
    if text is None:
        return None
    # Word-boundary match so `hr_agent.py` does not match
    # `hr_retirement_audit.py`, and a bare filename does not match a longer one
    # that merely ends with it.
    pat = re.compile(r"(?:^|[/\s\"'])" + re.escape(script) + r"(?:[\s\"';|>&]|$)")
    out: List[Tuple[str, str, str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split()[0]:          # crontab env assignment (SHELL=...)
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        if not pat.search(parts[5]):
            continue
        out.append(tuple(parts[:5]))        # type: ignore[arg-type]
    return out


# =============================================================================
# The two questions worth asking
# =============================================================================

def last_scheduled_fire(script: str, now: Optional[datetime] = None,
                        cron_text: Optional[str] = None
                        ) -> Tuple[str, Optional[datetime]]:
    """The most recent moment this host's schedule asked `script` to run.

    Local naive time throughout, deliberately: cron fires in the host's local
    zone, and file mtimes come back in the same zone via
    `datetime.fromtimestamp`. Comparing those two needs no timezone at all,
    which is what keeps this correct on a VM running Etc/UTC and a Mac running
    America/New_York without either one converting (memory
    `host_divergence_is_a_bug_class`).
    """
    ents = entries_for(script, cron_text)
    if ents is None:
        return SCHEDULE_UNREADABLE, None
    if not ents:
        return NOT_SCHEDULED_HERE, None
    now = now or datetime.now()
    for back in range(_LOOKBACK_DAYS + 1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0,
                                                   second=0, microsecond=0)
        fires: List[datetime] = []
        for mi, ho, dom, mon, dow in ents:
            if not _day_matches(dom, mon, dow, day):
                continue
            for h in _expand(ho, 0, 23):
                for m in _expand(mi, 0, 59):
                    fires.append(day.replace(hour=h, minute=m))
        past = [f for f in fires if f <= now]
        if past:
            return ON_TIME, max(past)       # status refined by lateness()
    # Scheduled here, but nothing has come due inside the lookback window (a
    # `0 0 1 1 *` job, or a line added this morning). That is NOT the same fact
    # as "this host does not schedule it" -- folding the two together would let
    # a yearly job report NOT_SCHEDULED_HERE for 364 days, which every caller
    # reads as "not my problem". Own verdict, so a caller has to decide.
    return NO_FIRE_YET, None


def max_legit_gap_h(script: str, cron_text: Optional[str] = None
                    ) -> Optional[float]:
    """The largest gap between consecutive fires the schedule itself asks for.

    This is the number every hardcoded STALE_HOURS should have been compared
    against before it was written down. Returns None when the schedule cannot
    be read or does not run `script` here.
    """
    ents = entries_for(script, cron_text)
    if not ents:
        return None
    # One synthetic week is enough: cron schedules repeat weekly unless a
    # day-of-month field is restricted, and none of ours are. Anchor on a
    # Monday so the week is whole.
    base = datetime(2026, 1, 5)             # a Monday
    fires: List[datetime] = []
    for d in range(7):
        day = base + timedelta(days=d)
        for mi, ho, dom, mon, dow in ents:
            if not _day_matches(dom, mon, dow, day):
                continue
            for h in _expand(ho, 0, 23):
                for m in _expand(mi, 0, 59):
                    fires.append(day.replace(hour=h, minute=m))
    fires = sorted(set(fires))
    if len(fires) < 2:
        return None
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(fires, fires[1:])]
    gaps.append(((fires[0] + timedelta(days=7)) - fires[-1]).total_seconds() / 3600.0)
    return max(gaps)


def lateness(script: str, mtime_epoch: Optional[float],
             now: Optional[datetime] = None,
             grace_min: float = DEFAULT_GRACE_MIN,
             cron_text: Optional[str] = None) -> Dict:
    """Did `script` miss a scheduled run, judged by this host's own scheduler?

    `mtime_epoch=None` means the output does not exist. That is a LATE verdict
    once a fire time has actually passed its grace window, and NO_FIRE_YET
    before that — an absent output is not evidence against a producer that has
    not yet been asked to run.
    """
    now = now or datetime.now()
    status, fire = last_scheduled_fire(script, now, cron_text)
    gap = max_legit_gap_h(script, cron_text)
    out: Dict = {
        "script": script, "verdict": status, "last_fire": None,
        "deadline": None, "output_age_h": None,
        "max_legit_gap_h": gap, "grace_min": grace_min,
    }
    if status in (SCHEDULE_UNREADABLE, NOT_SCHEDULED_HERE,
                  NO_FIRE_YET) or fire is None:
        return out

    deadline = fire + timedelta(minutes=grace_min)
    out["last_fire"] = fire.isoformat()
    out["deadline"] = deadline.isoformat()
    if mtime_epoch is not None:
        out["output_age_h"] = (now - datetime.fromtimestamp(mtime_epoch)
                               ).total_seconds() / 3600.0

    if now < deadline:
        # Inside the grace window the job may still be running. Judging it here
        # would report a false LATE on every fire time; the previous fire is
        # the one that can be judged, and it already was on the last run.
        out["verdict"] = ON_TIME
        out["note"] = "inside the grace window of the most recent fire"
        return out

    if mtime_epoch is None:
        out["verdict"] = LATE
        out["missed_fire"] = fire.isoformat()
        return out

    written = datetime.fromtimestamp(mtime_epoch)
    if written < fire:
        out["verdict"] = LATE
        out["missed_fire"] = fire.isoformat()
    else:
        out["verdict"] = ON_TIME
    return out


def missed_fires_since(script: str, since_epoch: Optional[float],
                       now: Optional[datetime] = None,
                       cron_text: Optional[str] = None) -> Optional[int]:
    """How many scheduled runs of `script` fell between `since` and now.

    The honest unit for "how bad is this outage". Hours are not comparable
    across schedules — 10 hours overnight costs the firm nothing and 10 hours
    across a trading afternoon costs it four pipeline runs — so a severity
    ladder priced in hours says different things on different days for no
    reason. Counted runs say the same thing every day.

    None = the schedule could not be read, or the host does not run it. Both
    are "cannot tell", and the caller must not read them as zero.
    """
    ents = entries_for(script, cron_text)
    if not ents:
        return None
    now = now or datetime.now()
    if since_epoch is None:
        return None
    since = datetime.fromtimestamp(since_epoch)
    if since >= now:
        return 0
    n = 0
    span = (now - since).days + 1
    for back in range(min(span, _LOOKBACK_DAYS * 2) + 1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0,
                                                   second=0, microsecond=0)
        for mi, ho, dom, mon, dow in ents:
            if not _day_matches(dom, mon, dow, day):
                continue
            for h in _expand(ho, 0, 23):
                for m in _expand(mi, 0, 59):
                    f = day.replace(hour=h, minute=m)
                    if since < f <= now:
                        n += 1
    return n


def fires_per_busiest_day(script: str, cron_text: Optional[str] = None
                          ) -> Optional[int]:
    """Runs on the day the schedule asks for the most of them.

    Used as the "a whole day's work is gone" mark, so a severity ladder can be
    expressed in the firm's own units instead of a round number of hours.
    """
    ents = entries_for(script, cron_text)
    if not ents:
        return None
    best = 0
    base = datetime(2026, 1, 5)                  # a Monday, so a full week
    for back in range(7):
        day = base + timedelta(days=back)
        n = 0
        for mi, ho, dom, mon, dow in ents:
            if not _day_matches(dom, mon, dow, day):
                continue
            n += len(_expand(ho, 0, 23)) * len(_expand(mi, 0, 59))
        best = max(best, n)
    return best or None


def describe(script: str, cron_text: Optional[str] = None) -> str:
    """One line a finding can quote, so the alarm carries its own derivation."""
    gap = max_legit_gap_h(script, cron_text)
    ents = entries_for(script, cron_text)
    if ents is None:
        return f"{script}: this host's crontab could not be read"
    if not ents:
        return f"{script}: not scheduled on this host"
    return (f"{script}: {len(ents)} crontab entr(y/ies), largest gap the schedule "
            f"itself asks for = {gap:.1f}h")


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", nargs=2, metavar=("SCRIPT", "OUTPUT"),
                    help="verdict for one producer/output pair")
    ap.add_argument("--report", action="store_true",
                    help="agent convention; same as no args")
    args = ap.parse_args()

    if args.check:
        script, out_path = args.check
        mt = os.path.getmtime(out_path) if os.path.exists(out_path) else None
        res = lateness(script, mt)
        for k, v in res.items():
            print(f"  {k:<18} {v}")
        return 0 if res["verdict"] in (ON_TIME, NOT_SCHEDULED_HERE) else 1

    text = read_crontab()
    if text is None:
        print("crontab unreadable on this host — no schedule-derived verdict "
              "is available here.")
        return 1
    scripts = sorted({m.group(1) for m in
                      re.finditer(r"([A-Za-z0-9_]+\.py)", text)})
    if not scripts:
        print("this host schedules nothing.")
        return 0
    print(f"{'script':<36}{'entries':>8}{'max legit gap (h)':>20}")
    for s in scripts:
        ents = entries_for(s, text) or []
        gap = max_legit_gap_h(s, text)
        print(f"{s:<36}{len(ents):>8}{(f'{gap:.1f}' if gap else '-'):>20}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

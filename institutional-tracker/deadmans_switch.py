#!/usr/bin/env python3
"""
APEX CAPITAL — DEAD-MAN'S SWITCH POLICY
========================================
The firm's ONE definition of "the Manager stopped deciding".

WHY THIS MODULE EXISTS
----------------------
Three modules implemented this alarm independently and disagreed about the same
file on the same morning (2026-08-06):

    main.py                       > 20h, weekend-adjusted, emails the Chairman
    morning_briefing.py           > 30h, raw age, adds a briefing alert
    cit_pipeline_integrity_agent  > 14h, min(file mtime, content ts), queues a
                                    HIGH to the Chairman decision queue

At 09:15 that morning `manager_decisions.json` had not gained a decision since
Aug 4 17:49 — a 39h gap covering the whole of Wednesday Aug 5, a trading day.
`morning_briefing` said "39h old — DEAD-MAN'S SWITCH risk" and was RIGHT.
`cit_pipeline_integrity_agent` said "OK — heartbeat and manager_decisions are
consistent" and was WRONG, because `min(mtime, content_ts)` takes the *younger*
of the two, so any process that rewrites the file resets the apparent age
without a single new decision behind it. `main.py` said nothing, because its
weekend subtraction had eaten the gap (see `weekend_hours_between`).

Three alarms on one fact, two wrong, and the one wired to the Chairman queue
wrong in the reassuring direction. This is the same shape as the three
spellings of "retired" (`hr_retirement_audit.is_retired`) and the forked
Chairman queue: one fact, more than one definition, and the gap is silent.

WHAT IS AND IS NOT UNIFIED
--------------------------
The *age* computation is one function used everywhere — that is where the three
callers actually diverged. The *thresholds* stay distinct because they answer
genuinely different questions, and flattening them would delete the wiring-bug
detector `cit_pipeline_integrity_agent` exists for:

    STALE_H            the Manager has not decided in too long (absolute)
    WIRING_BUG_STALE_H the pipeline DID run and the Manager still did not
                       decide — only meaningful alongside a fresh heartbeat,
                       so it can be tighter
    HEARTBEAT_STALE_H  the pipeline itself has not started

Consumers: main.py, morning_briefing.py, cit_pipeline_integrity_agent.py.
Guarded by test_pipeline_smoke.py::TestDeadmansSwitchHasOneDefinition.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# ── Thresholds ────────────────────────────────────────────────────────────────
# 20h: the overnight gap between the last pipeline run (16:15 ET) and the first
# next-day run (09:45 ET) is 17.5h, so anything at or below ~18h fires every
# single weekday morning. 20h leaves 2.5h of headroom over the expected gap and
# still catches a genuinely missed day.
STALE_H = 20.0

# Tighter, because a fresh heartbeat has already proved the pipeline ran: if it
# ran and the Manager still did not write a decision, that is a wiring bug and
# not an outage. This is the 17-day manager_decisions bug this agent was built
# for — main.py called evaluate_recommendation() directly instead of
# manager_agent.run(), so the pipeline looked perfectly healthy while the
# Manager's output was never produced.
WIRING_BUG_STALE_H = 14.0

# The pipeline did not START. Covers a full missed day with ~2h of buffer.
HEARTBEAT_STALE_H = 26.0

# Below this the pipeline is definitely running right now.
HEARTBEAT_FRESH_H = 4.0


def _parse(ts) -> datetime | None:
    """Parse a producer timestamp, or None. Never raises.

    Naive on purpose: producers write `datetime.now().isoformat()`, which means
    the writing host's wall clock, and this process runs on that same host. See
    memory host_divergence_is_a_bug_class — attaching a fabricated zone here is
    what made three snapshot tests fail on the UTC VM for four hours a night.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)[:19])
    except Exception:
        return None


def latest_decision_ts(data) -> datetime | None:
    """The newest Manager decision timestamp, by VALUE — not by list position.

    Both previous callers read `data[-1]`. The file happens to be written in
    chronological order today, so that worked; it is an assumption about a
    producer's append order that nothing enforces, and if it ever broke, the
    alarm would under-report age — i.e. fail silent, in the direction that hides
    an outage. `max()` costs nothing at this file's size and cannot be wrong.

    Returns None when no entry carries a parseable timestamp. That is "we have
    never seen the Manager decide", which is a different statement from "the
    Manager decided a long time ago" — callers that can tell the difference
    should.
    """
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    stamps = [
        t for t in (
            _parse(e.get("generated_at") or e.get("timestamp"))
            for e in data if isinstance(e, dict)
        ) if t is not None
    ]
    return max(stamps) if stamps else None


def weekend_hours_between(start: datetime, end: datetime) -> float:
    """Wall-clock hours of Saturday/Sunday actually inside [start, end].

    Counts HOURS, not calendar dates. main.py added a flat 24h for every weekend
    date the interval so much as grazed, including both endpoints — so a Friday
    23:00 → Saturday 01:00 gap of two hours subtracted a full 24 and came out at
    zero. Over-subtraction is the dangerous direction: it makes a real outage
    look like an ordinary weekend and the alarm stays quiet.
    """
    if end <= start:
        return 0.0
    total = 0.0
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        nxt = day + timedelta(days=1)
        if day.weekday() >= 5:          # 5=Sat, 6=Sun
            lo, hi = max(day, start), min(nxt, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        day = nxt
    return total


def decision_age_hours(data, now: datetime | None = None) -> float:
    """Hours since the most recent Manager decision. inf if there is none.

    Reads the CONTENT timestamp and never the file mtime. mtime answers "was
    this file written", which is not the question being asked: a rewrite that
    appends nothing, or re-serialises an unchanged payload, moves mtime and
    proves nothing about whether the Manager decided. Taking
    `min(mtime, content_ts)` — as cit_pipeline_integrity_agent did — meant a
    mere touch silenced the alarm. Presence is not currency.
    """
    ts = latest_decision_ts(data)
    if ts is None:
        return float("inf")
    now = now or datetime.now()
    return (now - ts).total_seconds() / 3600.0


def effective_age_hours(data, now: datetime | None = None) -> float:
    """`decision_age_hours` minus the weekend hours inside the gap.

    The pipeline is weekday-only, so a gap that spans Saturday and Sunday is
    expected and must not fire. inf stays inf: no amount of weekend explains a
    Manager that has never decided.
    """
    ts = latest_decision_ts(data)
    if ts is None:
        return float("inf")
    now = now or datetime.now()
    raw = (now - ts).total_seconds() / 3600.0
    return max(0.0, raw - weekend_hours_between(ts, now))


def is_tripped(data, now: datetime | None = None,
               threshold_h: float = STALE_H) -> tuple:
    """(tripped, raw_age_h, effective_age_h) — the one answer, for every caller.

    Report BOTH ages wherever this is surfaced. The raw age is what a human
    recognises ("two days"); the effective age is what the threshold was
    compared against. Printing only the second is how a 65h Friday-to-Monday gap
    reads as "17h" to the Chairman and looks like nothing at all.
    """
    now = now or datetime.now()
    raw = decision_age_hours(data, now)
    eff = effective_age_hours(data, now)
    return (eff > threshold_h, raw, eff)

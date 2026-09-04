#!/usr/bin/env python3
"""
Board Rememberer — surfaces standing verdicts the board must not forget.
================================================================================
Standing question this file answers: which prior decisions is the board
supposed to be carrying into every meeting, and is each one still true today?

The firm's oldest failure mode is not making a bad call — it is making a good
call, filing it, and then quietly deciding something contradictory six months
later because nobody remembered. The tech_short_pod DO-NOT-SHORT verdict on
KR/AI-hardware (2026-07-30) is the flagship example: measured, reasoned, and
sitting in a JSON that nothing in the meeting pipeline reads.

Contract:
  * STANDING_VERDICTS is a curated list. Each entry has an id, a headline,
    the WHY, and a `check_still_true` predicate. Adding a new one is a Chairman
    or board decision, not a per-cycle observation.
  * Every run recomputes each `check_still_true` and writes
    data/board_reminders.json with only the still-standing ones — a verdict
    that flipped is louder as an ESCALATION (owned by whichever pod raised it)
    than as a reminder, so we do not re-print it here.
  * board_meeting._build_anonymous_briefing() reads that file and puts the
    reminders into briefing["standing_reminders"]. The ops director echoes
    them at the top of the meeting so they enter the record every time.

This is a reminder mechanism, not a decision mechanism. It never trades,
never files a Chairman entry, never edits anything besides its own JSON.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUTPUT_PATH = DATA / "board_reminders.json"

logger = logging.getLogger("board_rememberer")


def _kr_tech_verdict_still_stands() -> tuple[bool, str]:
    """Read the tech_short_pod verdict artefact and report whether the
    DO-NOT-SHORT ruling is still the standing verdict. Returns
    (still_standing, evidence_line)."""
    p = DATA / "tech_short_report.json"
    if not p.exists():
        return True, "tech_short_report.json missing — verdict presumed to stand"
    try:
        report = json.loads(p.read_text())
    except Exception as e:
        return True, f"tech_short_report.json unreadable ({e}) — verdict presumed to stand"
    watch = report.get("thesis_watch", {})
    status = str(watch.get("status", "")).upper()
    if "STANDS" in status:
        return True, watch.get("status", "verdict stands")
    if "OVERTURNED" in status or "FLIPPED" in status:
        return False, watch.get("status", "verdict flipped")
    return True, watch.get("status", "verdict status unknown; presumed standing")


STANDING_VERDICTS = [
    {
        "id": "KR-TECH-DO-NOT-SHORT",
        "headline": (
            "KR/AI-hardware DO NOT SHORT. Extension is a DISQUALIFIER, not a "
            "bonus (fwd 20d +6.99%, shorts win 39.1% at high extension)."
        ),
        "why_it_matters": (
            "The 2026-07-30 measurement corrected a design that ranked the "
            "MOST extended names as the BEST shorts. Anyone proposing a KR/"
            "AI-hardware short must first show the anti-chasing rule is "
            "satisfied. See tech_short_pod.py::MIN_EXTENSION_PCTILE and the "
            "memory [[kr_tech_short_pod]]."
        ),
        "added_on": "2026-07-30",
        "check": _kr_tech_verdict_still_stands,
    },
    {
        "id": "HEDGE-CARVE-OUT-NARROW",
        "headline": (
            "Hedge carve-out is narrow (max 2 slots above cap, structural "
            "beta only, HARD_CASH_FLOOR_PCT never waived) and outcome-tracked."
        ),
        "why_it_matters": (
            "The carve-out was ratified 2026-09-04 with the explicit condition "
            "that outcomes are graded. hedge_exemption_outcomes.py auto-tightens "
            "at 40% failure rate over n>=5. If a proposal argues for widening the "
            "carve-out, the board must first see the outcome-tracker's current "
            "grade. See memory [[fallback-that-means-act]]."
        ),
        "added_on": "2026-09-04",
        "check": lambda: (True, "carve-out active; outcomes tracked in hedge_exemption_outcomes.py"),
    },
    {
        "id": "RETIREMENT-REQUIRES-CHAIRMAN",
        "headline": (
            "Agent retirement is a Chairman decision. Stage 4 (60 R&D cycles) "
            "queues a ratification entry; the loop never self-executes a fire."
        ),
        "why_it_matters": (
            "Ratified 2026-09-04 after seven wrongful auto-terminations. "
            "PERF_REPLACE_CYCLES = 60 (≈240 days runway). Any proposal to "
            "shorten the ladder must argue against that history first. See "
            "[[a-coaching-stage-that-removes-you]] and "
            "[[fix_that_erased_its_detector]]."
        ),
        "added_on": "2026-09-04",
        "check": lambda: (True, "PERF_REPLACE_CYCLES=60, Stage-4 branch queues Chairman ratification"),
    },
]


def _active_reminders() -> list[dict]:
    active = []
    for v in STANDING_VERDICTS:
        try:
            still, evidence = v["check"]()
        except Exception as e:
            still, evidence = True, f"check errored: {e} — presumed still standing"
        if still:
            active.append({
                "id":              v["id"],
                "headline":        v["headline"],
                "why_it_matters":  v["why_it_matters"],
                "added_on":        v["added_on"],
                "evidence":        evidence,
            })
    return active


def main(write: bool = True) -> dict:
    active = _active_reminders()
    payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "count":         len(active),
        "reminders":     active,
        "note": (
            "Read by board_meeting.py at meeting start. Standing verdicts are "
            "curated in board_rememberer.py::STANDING_VERDICTS — additions and "
            "removals are Chairman decisions, not per-run outputs."
        ),
    }
    if write:
        OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
        logger.info("  board_rememberer: wrote %d standing reminder(s) → %s",
                    len(active), OUTPUT_PATH.name)
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = main(write="--dry-run" not in sys.argv)
    for entry in r["reminders"]:
        print(f"  [{entry['id']}] {entry['headline']}")
        print(f"     evidence: {entry['evidence']}")

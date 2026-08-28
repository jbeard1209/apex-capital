#!/usr/bin/env python3
"""
MIGRATION — void the zero-pick counters accrued while the ladder was broken.
===========================================================================
Companion to the fix in `agent_lifecycle.py`. Read that file's header first;
this script only carries out its consequence.

WHY EVERY COUNTER IS VOID, NOT JUST THE DEMOTED ONES
----------------------------------------------------
`consecutive_zero_cycles` is a proxy for "this agent had a fair opportunity and
wasted it". As of the fix, the ladder charges a cycle only against an agent the
run manifest says actually ran. No run manifest existed for any past cycle —
the evidence was never collected. So under the rule the firm now holds, every
count currently on the books is unproven. An instrument that was measuring the
wrong quantity does not produce readings worth adjusting; it produces readings
worth discarding. The counters restart from zero, on evidence.

The cost is about eight days of re-accrual before Stage 1 can fire again. The
alternative is keeping 43 demotions the firm's own code had already exonerated
seven times.

WHAT IT DOES
------------
  1. status in COACHING_STATUSES  ->  "active", stage state vacated.
  2. every row's consecutive_zero_cycles -> 0.
  3. the vacated notice/PIP text is MOVED to `lifecycle_history`, never deleted
     (memory `a-decision-recorded-nowhere`: "the board said no" and "we lost it"
     must not be the same state), and `reinstated_at`/`reinstatement_reason` are
     set in the firm's existing idiom — the same fields agent_roster.HR_FIELDS
     preserves when the two hosts' registries are merged.
  4. a `lifecycle_migrations` receipt is written at the top level of the
     registry. Re-running is a no-op; the receipt is the idempotency key.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
  * `retired` is terminal and only a Co-Chairman may set or clear it.
  * `inactive` is a pre-ladder deactivation, not a stage — status untouched
    (its counter is still voided; the counter is meaningless everywhere).
  * `reassigned` agents get their status back but NOT their original sector:
    Stage 3 overwrites sector/etf/tier and the pre-reassignment values are not
    recorded anywhere. Those rows are listed for manual review rather than
    silently "restored" to a sector they are not actually covering. There are
    none on either host today; this exists so the next one is not a surprise.

RUN IT ON EACH HOST SEPARATELY. There is no state sync between the Mac and the
VM (CLAUDE.md, memory `two_brains_one_account`); the registries are independent
files and each host's Chief of Staff grades its own runs.

    python3 migrate_lifecycle_counters.py            # dry run, prints the plan
    python3 migrate_lifecycle_counters.py --apply    # writes

On the VM: ~/venv/bin/python3, not bare python3.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import agent_lifecycle

BASE_DIR = Path(__file__).resolve().parent
REGISTRY = BASE_DIR / "data" / "agent_registry.json"

MIGRATION_ID = "2026-08-28-coaching-stage-is-not-a-termination"

REASON = (
    "Zero-pick counter voided by migration {mid}. The Chief of Staff's coaching "
    "stages set status to 'noticed'/'pip', which research_team.py would not "
    "load, so the agent could not run, could not earn the approved pick that "
    "recovery requires, and was charged another cycle for it. 43 agents crossed "
    "Stage 1 in a single pass on the VM on 2026-08-28 — 64% of the roster, same "
    "day, identical count, every one already carrying exoneration_count=7. The "
    "counter measured the firm's condition, not the agent's performance. It now "
    "requires positive evidence that the agent ran (agent_lifecycle.had_a_fair_cycle)."
)

# Live stage state written by the ladder. Vacated on restore and preserved in
# `lifecycle_history` — an active agent must not still carry an unrescinded
# performance notice (memory `a-record-that-outlived-its-force`).
STAGE_FIELDS = ("performance_notice", "performance_notice_sent",
                "pip_issued_at", "pip_plan", "reassigned_to")

# Carried forward, not overwritten: an earlier reinstatement is HR history.
HISTORY_CARRY = ("reinstated_at", "reinstatement_reason")


def _load(path: Path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _atomic_write(path: Path, payload: dict) -> None:
    """Same discipline as agent_lifecycle._atomic_write — never truncate in place.

    `open(path, "w")` empties the registry before a single byte is written. A
    crash there loses all 67 agents, and an empty registry reads as "the firm
    has no agents" to every consumer (memory `truncate-before-lock`).
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".registry-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def already_applied(reg: dict) -> Optional[dict]:
    for rec in reg.get("lifecycle_migrations") or []:
        if isinstance(rec, dict) and rec.get("id") == MIGRATION_ID:
            return rec
    return None


def plan(reg: dict):
    """(restore, needs_review, counters) — decided, not yet written."""
    restore, needs_review, counters = [], [], []
    for a in reg.get("agents", []):
        status = a.get("status") or agent_lifecycle.DEFAULT_STATUS
        if a.get("consecutive_zero_cycles"):
            counters.append(a)
        if status in agent_lifecycle.COACHING_STATUSES:
            if status == "reassigned":
                needs_review.append(a)
            restore.append(a)
    return restore, needs_review, counters


def apply(reg: dict, restore, counters, today: str) -> dict:
    for a in restore:
        old_status = a.get("status")
        history = a.setdefault("lifecycle_history", [])
        entry = {
            "vacated_at": today,
            "migration": MIGRATION_ID,
            "from_status": old_status,
            "from_consecutive_zero_cycles": a.get("consecutive_zero_cycles", 0),
            "exoneration_count_at_vacatur": a.get("exoneration_count", 0),
        }
        for f in STAGE_FIELDS + HISTORY_CARRY:
            if a.get(f) is not None:
                entry[f] = a[f]
        history.append(entry)

        for f in STAGE_FIELDS:
            a.pop(f, None)
        a["status"] = "active"
        a["reinstated_at"] = today
        a["reinstatement_reason"] = REASON.format(mid=MIGRATION_ID)

    for a in counters:
        a["consecutive_zero_cycles"] = 0

    reg.setdefault("lifecycle_migrations", []).append({
        "id": MIGRATION_ID,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "restored_to_active": sorted(a.get("class_name", "?") for a in restore),
        "counters_zeroed": len(counters),
        "note": "counters restart on run evidence; see agent_lifecycle.py",
    })
    reg["last_updated"] = datetime.now().isoformat(timespec="seconds")
    return reg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true",
                    help="write the registry (default is a dry run)")
    ap.add_argument("--registry", default=str(REGISTRY))
    args = ap.parse_args()

    path = Path(args.registry)
    if not path.exists():
        print(f"REFUSING: no registry at {path}")
        return 2

    reg = _load(path)
    agents = reg.get("agents") or []
    if not agents:
        print(f"REFUSING: {path} has no agents — an empty registry is not a "
              f"registry with nothing to migrate.")
        return 2

    done = already_applied(reg)
    if done:
        print(f"Already applied on {done.get('applied_at')} "
              f"(host {done.get('host')}): "
              f"{len(done.get('restored_to_active') or [])} restored, "
              f"{done.get('counters_zeroed')} counters zeroed. Nothing to do.")
        return 0

    restore, needs_review, counters = plan(reg)
    today = datetime.now().strftime("%Y-%m-%d")

    host = os.uname().nodename if hasattr(os, "uname") else "unknown"
    print(f"{path}  ({host})")
    print(f"  {len(agents)} agent(s); statuses "
          f"{dict(Counter(a.get('status') for a in agents))}")
    print(f"  counters {dict(Counter(a.get('consecutive_zero_cycles', 0) for a in agents))}")
    print()
    print(f"  restore to active : {len(restore)}")
    for a in sorted(restore, key=lambda x: x.get("class_name", "")):
        print(f"      {a.get('class_name','?'):38s} "
              f"{a.get('status'):>10s} czc={a.get('consecutive_zero_cycles', 0)} "
              f"exon={a.get('exoneration_count', 0)}  [{a.get('sector','?')}]")
    print(f"  counters zeroed   : {len(counters)}")
    if needs_review:
        print(f"  NEEDS MANUAL REVIEW: {len(needs_review)} agent(s) were reassigned "
              f"to a different sector by Stage 3. Status is restored; the original "
              f"sector is NOT, because it was never recorded:")
        for a in needs_review:
            print(f"      {a.get('class_name','?')} — now covering "
                  f"{a.get('sector','?')} / {a.get('etf','?')}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    apply(reg, restore, counters, today)
    _atomic_write(path, reg)
    print(f"\nApplied. {len(restore)} restored to active, "
          f"{len(counters)} counter(s) zeroed. Receipt: {MIGRATION_ID}")
    print("Run this on the OTHER host too — the registries do not sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

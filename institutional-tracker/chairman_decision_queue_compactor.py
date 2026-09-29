#!/usr/bin/env python3
"""Chairman DECISION queue compactor — for the WIDER app-facing inbox.

chairman_queue_compactor.py works on data/chairman_action_required.json, the
narrow producer feed (185 rows). This module works on the sibling file
data/chairman_decision_queue.json, the app-facing inbox that
chairman_decision_sla.py owns (254 rows on 2026-09-28, 207 of them terminal).

The two queues have DIFFERENT semantics for "closed":

  * chairman_action_required.json — producers upsert; PLS watches for silent
    drops; the compactor there refuses to archive anything PLS has not
    observed as RESOLVED/WONT_FIX. That is right for that queue: rows are
    dropped when producers forget them.

  * chairman_decision_queue.json — chairman_decision_sla.py copies items in;
    when the app or a downstream actor decides them it sets an explicit
    terminal status. There are three:
      - resolved            — the item was fixed and reported back
      - resolved_upstream   — a producer withdrew it (fixed by another path)
      - escalated_to_board  — handed off to the board; the decision-of-record
                              now lives in board minutes, not this queue
    These are declaratively terminal — the producer/SLA layer set them, so
    unlike the narrow queue's "closed" they are not inferred and do not need
    PLS to have observed them.

So the interlock here is different, not absent:

  1. A row leaves this queue ONLY if its status is in TERMINAL_STATUSES.
     The three above; nothing else, including the malformed AWAITING_CHAIRMAN
     row (case-mismatch bug — see audit report).
  2. Rows are MOVED to chairman_resolved_archive.json (the same archive the
     narrow compactor uses), never deleted, preserving id + status + note.
  3. Idempotent — a row already carrying archived_because is not re-added.

Authorised by Jack Beard 2026-09-08 under [[autoresolve_low_maintenance]].

  python3 chairman_decision_queue_compactor.py           # dry run
  python3 chairman_decision_queue_compactor.py --apply   # move to archive
"""
import json
import os
import sys
from datetime import datetime, timezone

import problem_lifecycle_sentinel as PLS

BASE = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(BASE, "data", "chairman_decision_queue.json")
ARCHIVE = os.path.join(BASE, "data", "chairman_resolved_archive.json")

TERMINAL_STATUSES = frozenset({
    "resolved",           # explicitly closed with a fix
    "resolved_upstream",  # withdrawn by producer (fixed by another path)
    "escalated_to_board", # handed off; board minutes are decision-of-record
})

# Why the row is archivable — human-readable, stamped on each archived row.
REASONS = {
    "resolved":           "status=resolved (closed with fix; app-inbox no longer owns it)",
    "resolved_upstream":  "status=resolved_upstream (producer withdrew; fixed elsewhere)",
    "escalated_to_board": "status=escalated_to_board (decision-of-record lives in board minutes)",
}


def _status(row) -> str:
    """Case-insensitive read of the status field; malformed rows return ''."""
    if not isinstance(row, dict):
        return ""
    return str(row.get("status") or "").lower()


def _terminal(row) -> bool:
    return _status(row) in TERMINAL_STATUSES


def plan(rows):
    """Return (archivable, kept). archivable is a list of (row, reason)."""
    archivable = []
    kept = []
    for row in rows:
        if _terminal(row):
            archivable.append((row, REASONS[_status(row)]))
        else:
            kept.append(row)
    return archivable, kept


def main():
    apply = "--apply" in sys.argv

    # Same writer-host gate the narrow compactor uses. This queue is
    # host-independent in principle (each host has its own file), but a run
    # that mutates state should only happen on the writer host so the two
    # hosts' archives do not diverge. If chairman_decision_sla runs on both,
    # let each host compact its own — writer_verdict() is what enforces "the
    # host currently designated to change shared state".
    if apply:
        may_write, why = PLS.writer_verdict()
        if not may_write:
            print("REFUSING to compact — %s" % why)
            return 0

    if not os.path.exists(QUEUE_PATH):
        print("No queue file at %s — nothing to do." % QUEUE_PATH)
        return 0

    with open(QUEUE_PATH) as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        print("Queue is not a list (got %s). Refusing." % type(rows).__name__)
        return 2

    archivable, kept = plan(rows)

    # Bucket counts for the operator.
    buckets = {}
    for row, _ in archivable:
        s = _status(row)
        buckets[s] = buckets.get(s, 0) + 1
    print("Decision queue: %d row(s), %d terminal, %d live."
          % (len(rows), len(archivable), len(kept)))
    if buckets:
        print("  by terminal status:")
        for s in sorted(buckets):
            print("    %s: %d" % (s, buckets[s]))
    if not apply:
        print("\nDry run. Re-run with --apply to move terminal rows to the archive.")
        return 0
    if not archivable:
        print("\nNothing to archive.")
        return 0

    stamp = datetime.now(timezone.utc).isoformat()
    archived = PLS._load(ARCHIVE, [])
    if not isinstance(archived, list):
        archived = []

    # Idempotence guard: if a row already carries archived_because AND is
    # still somehow here, we treat that as "already archived" and skip it
    # (never double-append). Should never happen in practice because we
    # remove them below, but the guard is cheap.
    for row, why in archivable:
        if row.get("archived_because"):
            continue
        sig = PLS.signature(row, "chairman")
        archived.append({
            **row,
            "archived_on": stamp,
            "signature": sig,
            "archived_because": why,
        })

    PLS._save(ARCHIVE, archived)
    PLS._save(QUEUE_PATH, kept)
    print("\nMoved %d row(s) to %s. Queue is now %d live."
          % (len(archivable), os.path.relpath(ARCHIVE, BASE), len(kept)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

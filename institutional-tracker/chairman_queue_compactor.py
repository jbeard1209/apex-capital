#!/usr/bin/env python3
"""
Chairman queue compactor — move closed items out, without inventing a drop.

The decision queue is append-mostly: producers upsert into it, and nothing ever
took closed rows back out. It reached 220 rows, of which one *single* bug — the
missing pyarrow dependency — accounted for four: the original finding, a
duplicate raised by a second agent, an SLA escalation about the duplicate, and
a problem_lifecycle_sentinel row noting that the escalation had vanished. Each
governance layer files a row about the previous layer's row. A queue that long
is not read, and a queue that is not read is not a control.

    python3 chairman_queue_compactor.py            # dry run — prints, writes nothing
    python3 chairman_queue_compactor.py --apply    # move them to the archive

WHY THIS IS NOT JUST `del rows[i]`
----------------------------------
problem_lifecycle_sentinel.py exists precisely to catch problems that leave
every queue with no recorded resolution — it calls that SILENT_DROP, and its
rule is one line: a signature that disappears while its history status is OPEN
is a silent drop. Deleting rows in bulk would manufacture, at scale, the exact
failure the sentinel was built to detect, and it would do it invisibly because
the evidence deleted is the evidence the detector reads.

So the interlock here is the sentinel's own history, not a judgement of mine: a
row may leave the queue only if its signature is either (a) still present in
some queue afterwards, or (b) already recorded in history as RESOLVED/WONT_FIX
— that is, the sentinel has *seen* the closure. A row whose signature the
sentinel has never seen is never archived, because "it left and nobody logged
it" is the thing we are avoiding.

That ordering is the whole discipline, and it means the correct sequence for
closing something is always: mark it resolved in place with the evidence → let
the sentinel observe it → THEN compact. Not the reverse.

Everything the sentinel defines is IMPORTED from it, never re-declared here.
On 2026-08-19 reconciliation_check.py was found holding its own copies of four
config guardrails that nothing ever compared to config.py; one had been stale
for 23 days and a legitimate board change was indistinguishable from tampering.
A second copy of a definition is a second thing that can be wrong.
"""
import json
import os
import sys
from datetime import datetime, timezone

import problem_lifecycle_sentinel as PLS

BASE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(BASE, "data", "chairman_resolved_archive.json")

CLOSED_STATUSES = PLS.RESOLVED_STATUSES | PLS.WONT_FIX_STATUSES


def _closed(row) -> bool:
    return isinstance(row, dict) and PLS._status_of(row) in CLOSED_STATUSES


def plan(rows, history, other_queue_sigs):
    """(archivable, refused) — refused carries the reason, which is the point.

    A refusal is information: it says the sentinel has not yet recorded this
    closure, so compacting now would erase the only trace of it.
    """
    candidates = [r for r in rows if _closed(r)]
    keepers = [r for r in rows if not _closed(r)]
    surviving = {PLS.signature(r, "chairman") for r in keepers} | other_queue_sigs

    archivable, refused = [], []
    for row in candidates:
        sig = PLS.signature(row, "chairman")
        if sig in surviving:
            # Some other row still carries this problem's identity, so removing
            # this one cannot make the signature vanish.
            archivable.append((row, sig, "signature still present elsewhere"))
            continue
        h = history.get(sig)
        if h is None:
            refused.append((row, sig, "the sentinel has no history for this "
                                      "signature — removing it would leave no "
                                      "record that it ever closed"))
            continue
        if h.get("status") == "OPEN":
            refused.append((row, sig, "sentinel history still says OPEN — run "
                                      "problem_lifecycle_sentinel.py first so "
                                      "the closure is observed, then compact"))
            continue
        archivable.append((row, sig, "closure recorded as %s" % h.get("status")))
    return archivable, refused


def main():
    apply = "--apply" in sys.argv
    # The interlock above is only as good as the history it reads, and since
    # 2026-08-19 only the designated host advances that history — the other
    # host's copy is frozen at whatever it last knew. A frozen history that
    # still says RESOLVED would let this archive a row the writer host
    # considers open, and chairman_queue_sync propagates the archive to the
    # other host as a tombstone. So the guard is imported from the sentinel,
    # not re-derived: whoever may not write the history may not act on it.
    if apply:
        may_write, why = PLS.writer_verdict()
        if not may_write:
            print("REFUSING to compact — %s\nThe history this decision depends "
                  "on is not maintained here; run it on the writer host." % why)
            return 0
    rows = PLS._load(PLS.CHAIRMAN_PATH, [])
    history = PLS._load(PLS.HISTORY_PATH, {})
    other = set()
    for path, kind in ((PLS.PENDING_PATH, "proposal"),
                       (PLS.IMPL_QUEUE_PATH, "impl")):
        for item in PLS._load(path, []):
            if isinstance(item, dict):
                other.add(PLS.signature(item, kind))

    archivable, refused = plan(rows, history, other)

    print("Chairman queue: %d row(s), %d closed, %d still awaiting a decision."
          % (len(rows), sum(1 for r in rows if _closed(r)),
             sum(1 for r in rows if not _closed(r))))
    print("\nArchivable (%d):" % len(archivable))
    for row, sig, why in archivable:
        print("  %-46s %s" % (str(row.get("id"))[:46], why))
    if refused:
        print("\nKEPT — closed but not safely removable (%d):" % len(refused))
        for row, sig, why in refused:
            print("  %-46s %s" % (str(row.get("id"))[:46], why))

    if not apply:
        print("\nDry run. Re-run with --apply to move the archivable rows.")
        return 0
    if not archivable:
        print("\nNothing to do.")
        return 0

    stamp = datetime.now(timezone.utc).isoformat()
    archived = PLS._load(ARCHIVE, [])
    if not isinstance(archived, list):
        archived = []
    drop = {id(r) for r, _, _ in archivable}
    for row, sig, why in archivable:
        archived.append({**row, "archived_on": stamp, "signature": sig,
                         "archived_because": why})
    remaining = [r for r in rows if id(r) not in drop]

    PLS._save(ARCHIVE, archived)
    PLS._save(PLS.CHAIRMAN_PATH, remaining)
    print("\nMoved %d row(s) to %s. Queue is now %d."
          % (len(archivable), os.path.relpath(ARCHIVE, BASE), len(remaining)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

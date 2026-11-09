"""Re-close inbox rows that chairman_queue_sync wrongly reopened.

THE DEFECT (fixed in chairman_queue_sync.apply_tombstones, 2026-11-09). When
an archived row was closed on one host, the sync dropped that closed copy
before the merge and kept the other host's stale open copy as a
"recurrence", then wrote it back open to both hosts. 158 rows carried
`reopened_after_archive` on 2026-11-09, including the Chairman's HR decisions,
which were reopened twice that day.

The fix stops new cases. It cannot repair the old ones, because by now both
hosts hold the stale open copy, so neither host has a close for the fix to
see. This script repairs them, with evidence, one row at a time.

EVIDENCE RULE. A row is a resurrected stale copy only if ALL of these hold:
  * it is open now;
  * the archive holds a CLOSED record with the SAME id;
  * that record has the SAME creation timestamp (`ts`, else `queued_at`), or,
    when the archived copy carries no creation time (197 of the VM's 365
    archive records on 2026-11-09), the open row was created BEFORE the
    archived copy was resolved: a row cannot be a new report of a problem
    that had not yet been closed, so it is the row that was closed.
    A genuine recurrence is filed as a new row, created after the close, and
    is left alone. So is any row without a creation time of its own.
Rows that match the archive only by content key, or by id with a different
creation time, are reported as genuine recurrences and not touched.

Safe to re-run. Dry run by default. --apply writes under the queue's own lock.
Run it on ONE host, then run chairman_queue_sync.py: the sync now treats the
other host's open copy of the same id as stale, and both copies leave the
queue together.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import atomic_json as aj
import chairman_queue as cq
import problem_lifecycle_sentinel as PLS

BASE = os.path.dirname(os.path.abspath(__file__))
ACTION = os.path.join(BASE, "data", "chairman_action_required.json")
ARCHIVE = os.path.join(BASE, "data", "chairman_resolved_archive.json")
CLOSED = PLS.RESOLVED_STATUSES | PLS.WONT_FIX_STATUSES


def _created(row):
    return str(row.get("ts") or row.get("queued_at") or "")


def _when(s):
    """Naive datetime from an ISO string, or None. Offsets are dropped: hosts
    write local time, so this is accurate to the hosts' offset (hours), which
    is far below the days-to-months between a close and a resurrection."""
    try:
        return datetime.fromisoformat(str(s)[:26].replace("Z", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _same_row(row, rec):
    created = _created(row)
    if not created:
        return False
    if _created(rec):
        return _created(rec) == created
    made, closed = _when(created), _when(rec.get("resolved_at"))
    return bool(made and closed and made < closed)


def classify(rows, archived):
    """(stale, genuine): stale is a list of (row, archived_record)."""
    by_id = {}
    for a in archived or []:
        if isinstance(a, dict) and a.get("id") and \
                str(a.get("status") or "").lower() in CLOSED:
            by_id.setdefault(a["id"], []).append(a)
    stale, genuine = [], []
    for r in rows:
        if not isinstance(r, dict) or r.get("status") not in cq.OPEN_STATUSES:
            continue
        if not r.get("reopened_after_archive") and r.get("id") not in by_id:
            continue
        match = next((a for a in by_id.get(r.get("id"), []) if _same_row(r, a)), None)
        (stale if match else genuine).append((r, match) if match else r)
    return stale, genuine


def reclose(path, archived, now=None):
    """Close every stale copy in `path`. Returns the closed rows."""
    now = now or datetime.now(timezone.utc).isoformat()
    with aj.locked(path):
        rows = cq._load(path)
        stale, _ = classify(rows, archived)
        for r, a in stale:
            r["status"] = "resolved"
            r["resolved_at"] = now
            r["resolved_by"] = (
                "re-closed by reclose_stale_reopened: same row (id %s, created %s) "
                "was already closed (%s, by %s) and archived %s; it was reopened "
                "only by the chairman_queue_sync stale-copy bug fixed 2026-11-09"
                % (r.get("id"), _created(r), a.get("resolved_at") or "?",
                   str(a.get("resolved_by") or "?")[:80], a.get("archived_on") or "?"))
            r.pop("reopened_after_archive", None)
        if stale:
            aj.write(path, rows)
    return [r for r, _ in stale]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the closures")
    args = ap.parse_args(argv)
    archived = PLS._load(ARCHIVE, [])
    if not isinstance(archived, list):
        print("archive unreadable or not a list: refusing")
        return 2
    stale, genuine = classify(cq._load(ACTION), archived)
    print("stale copies (same id and creation time as a closed archived row): %d"
          % len(stale))
    for r, a in stale:
        print("  %-60s %-28s archived %s" % (str(r.get("id"))[:60],
                                             str(r.get("source"))[:28],
                                             str(a.get("archived_on"))[:19]))
    print("genuine recurrences or no exact match (left open): %d" % len(genuine))
    for r in genuine:
        print("  %-60s %-28s created %s" % (str(r.get("id"))[:60],
                                            str(r.get("source"))[:28], _created(r)[:19]))
    if not args.apply:
        print("\nDry run. Re-run with --apply to close the stale copies.")
        return 0
    closed = reclose(ACTION, archived)
    print("\nclosed %d row(s)" % len(closed))
    return 0


if __name__ == "__main__":
    sys.exit(main())

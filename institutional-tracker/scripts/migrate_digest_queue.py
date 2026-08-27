#!/usr/bin/env python3
"""
ONE-TIME MIGRATION — retire the pre-delivery contents of data/digest_queue.json.

RUN ON EACH HOST SEPARATELY. There is no state sync between the Mac and the VM
(CLAUDE.md), so each has its own independent copy of this file with its own
independent contents. Running it here does nothing there.

    cd institutional-tracker && python3 scripts/migrate_digest_queue.py --apply

WHY A MIGRATION AT ALL
----------------------
`digest_queue.json` used to be where three ML monitors put every WARNING-level
finding, and nothing ever read it. As of this change it is a DEAD-LETTER file:
`ml_digest.deliver` sends findings through `email_buffer`, and a row lands here
only when that fails. The two meanings are incompatible — under the new one, a
row is a delivery fault with an age, and `coherence_laws.law_work_queues_drain`
measures that age against a deadline derived from the schedule.

The rows already in the file predate the delivery path entirely. They are not
delivery faults, and leaving them would keep the new detector permanently lit on
a cause that no longer exists — the lamp that is always on is the lamp nobody
reads (memory `presence-is-not-currency`).

WHY ARCHIVE RATHER THAN DELIVER
-------------------------------
They are 75 days old and every one is a DERIVED finding from a check that runs
on a schedule. Emailing "feature null rates are high" as if it were current
would be asserting a 75-day-old measurement about today. If the condition still
holds, the next scheduled run raises it again with today's numbers and it goes
out properly; if it does not, it should never have been sent. Nothing is lost
that the checks cannot reproduce — which is exactly what makes archiving safe
here and would not make it safe for a queue of un-reproducible work.

WHY NOT AUTOMATIC
-----------------
This is deliberately a script you run once, not a rule inside `ml_digest`. An
automatic "archive anything older than N days" would drain the dead-letter file
on a timer, and the file's whole value is that it only drains on successful
delivery. A remediation that quietly clears the evidence its own detector reads
is memory `fix_that_erased_its_detector`, and it is not being repeated here.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
QUEUE = DATA_DIR / "digest_queue.json"
ARCHIVE = DATA_DIR / "digest_queue_archive.json"

REASON = ("pre-delivery backlog: written when digest_queue.json had no consumer, "
          "retired when it became a dead-letter file. Derived findings — the "
          "originating checks re-run on schedule.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually move the rows (default is a dry run)")
    args = ap.parse_args()

    if not QUEUE.exists():
        print(f"{QUEUE.name} does not exist on this host — nothing to migrate.")
        return 0
    try:
        rows = json.loads(QUEUE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"REFUSING: {QUEUE.name} is unreadable ({e}). "
              f"Fix or remove it by hand — this script will not guess.")
        return 1
    if not isinstance(rows, list):
        print(f"REFUSING: {QUEUE.name} is not a list. Inspect it by hand.")
        return 1
    if not rows:
        print(f"{QUEUE.name} is already empty on this host — nothing to migrate.")
        return 0

    now = datetime.now(timezone.utc)
    print(f"{len(rows)} row(s) in {QUEUE.name}:")
    for r in rows:
        ts = r.get("timestamp", "?") if isinstance(r, dict) else "?"
        age = ""
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = f"  ({(now - dt).days}d old)"
        except ValueError:
            pass
        src = r.get("source", "?") if isinstance(r, dict) else "?"
        sub = r.get("subject", str(r)[:60]) if isinstance(r, dict) else str(r)[:60]
        print(f"  - [{src}] {sub}{age}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to archive these rows.")
        return 0

    archived = []
    if ARCHIVE.exists():
        try:
            prior = json.loads(ARCHIVE.read_text(encoding="utf-8"))
            archived = prior if isinstance(prior, list) else []
        except (ValueError, OSError):
            archived = []
    stamp = now.isoformat()
    archived.extend({"archived_at": stamp, "reason": REASON, "entry": r}
                    for r in rows)

    # Archive FIRST, then clear. The other order loses the rows outright if the
    # process dies between the two writes, and a migration that can destroy what
    # it was written to preserve is not a migration (memory `truncate-before-lock`).
    tmp = ARCHIVE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(archived, indent=2), encoding="utf-8")
    tmp.replace(ARCHIVE)

    tmp = QUEUE.with_suffix(".json.tmp")
    tmp.write_text("[]", encoding="utf-8")
    tmp.replace(QUEUE)

    print(f"\nArchived {len(rows)} row(s) -> {ARCHIVE.name} "
          f"({len(archived)} total). {QUEUE.name} is now the dead-letter file.")
    print("Remember: the OTHER host has its own copy. Run this there too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

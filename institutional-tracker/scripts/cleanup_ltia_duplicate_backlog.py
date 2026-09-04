#!/usr/bin/env python3
"""One-shot cleanup for the LTIA duplicate backlog in cit_approved_proposals.json.

The LOW/IMPROVEMENT "[LTIA] Expected agents silent" finding accumulated 193
approved-but-never-implemented rows and "CRO=HIGH / Canary=GREEN" 6 rows,
because _escalate_to_board only deduped against the PENDING queue. Once the
deputy board approved and moved the row to `cit_approved_proposals.json` the
next LTIA cycle re-filed. See [[a-queue-that-never-empties]].

The producer bug is already fixed (LTIA now also checks open rows in the
approved queue). This script collapses the historical mess: for each
still-open LTIA finding, keep the OLDEST row (as the canonical open item)
and mark every subsequent duplicate `closed_superseded_by_dedup_cleanup`
with a note pointing to the canonical id.

Idempotent — a second run finds no duplicates to close. Backs up the file
in place before writing.
"""
from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
APPROVED = DATA_DIR / "cit_approved_proposals.json"

# Same set the producer treats as "still open" — see logic_team_implementation_agent.py
_OPEN_STATUSES = {
    "pending", "approved", "in_progress", "in_review",
    "routed_to_implementer", "awaiting_chairman",
    "escalated_to_chairman",
}


def _dedup_key(p: dict) -> str:
    return p.get("dedup_key") or p.get("title", "")


def main() -> None:
    if not APPROVED.exists():
        print(f"missing: {APPROVED}")
        return
    proposals = json.loads(APPROVED.read_text())
    if not isinstance(proposals, list):
        print(f"unexpected shape: {type(proposals).__name__}")
        return

    open_by_key: dict[str, list[int]] = defaultdict(list)
    for idx, p in enumerate(proposals):
        # Only touch LTIA-produced rows — collapsing rows from other sources
        # would be scope creep and could hide unique operational context.
        if p.get("source") != "LT Implementation Agent":
            continue
        if str(p.get("status", "")).lower() not in _OPEN_STATUSES:
            continue
        open_by_key[_dedup_key(p)].append(idx)

    collapsed = 0
    canonical: dict[str, str] = {}
    for key, indices in open_by_key.items():
        if len(indices) <= 1:
            continue
        indices.sort(key=lambda i: proposals[i].get("created_at", ""))
        keep = indices[0]
        canonical[key] = proposals[keep].get("id") or f"row_{keep}"
        for i in indices[1:]:
            proposals[i]["status"] = "closed_superseded_by_dedup_cleanup"
            proposals[i]["closed_at"] = datetime.now().isoformat()
            proposals[i]["closed_reason"] = (
                f"Duplicate of open LTIA row {canonical[key]}; the LTIA producer "
                f"only deduped against pending, so approving the finding removed "
                f"the dedup shield and every subsequent cycle re-filed. Producer "
                f"fixed in logic_team_implementation_agent.py::_escalate_to_board. "
                f"See [[a-queue-that-never-empties]]."
            )
            collapsed += 1

    if not collapsed:
        print("no duplicates to close — nothing to do.")
        return

    shutil.copy2(APPROVED, APPROVED.with_suffix(".bak.json"))
    APPROVED.write_text(json.dumps(proposals, indent=2))
    print(f"closed {collapsed} duplicate row(s); canonical rows kept per dedup_key:")
    for key, canon_id in canonical.items():
        print(f"  {canon_id}  →  {key[:80]}")


if __name__ == "__main__":
    main()

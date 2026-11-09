#!/usr/bin/env python3
"""
One-off migration (2026-11-09): re-route board approvals out of the
Chairman's inbox.

Chairman directive 2026-11-09: board-approved changes go ahead without his
sign-off, "only super crucial ones or ones that we will talk about in the
future" still come to him. board_meeting._route_approved_cit_proposals now
applies that rule going forward (chairman_decision_categorizer.needs_signoff).
This applies the same rule to rows the router ALREADY filed as
awaiting_chairman, e.g. the 19 filed by the first vote after the board-vote
fix (inbox 11 -> 27).

Only router rows are touched (they carry `times_approved` and `queued_at`).
Rows from any other producer are left alone, whatever they say. Each moved
row becomes status=approved_by_board, owner=CIT, which is not an open status,
so chairman_decision_sla closes the mirrored inbox item on its next run
(resolved_upstream, "closed by re-observation").

Writes a timestamped backup next to the file before changing it, and the
change can be undone by copying the backup back.

Usage:  python3 reroute_board_approvals.py           # dry run: list what moves
        python3 reroute_board_approvals.py --apply
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import atomic_json as aj
from chairman_decision_categorizer import needs_signoff

ACTION_FILE = Path(__file__).resolve().parent / "data" / "chairman_action_required.json"


def _is_router_row(r: dict) -> bool:
    return isinstance(r, dict) and "times_approved" in r and "queued_at" in r


def plan(rows: list) -> "tuple[list, list]":
    """→ (to_move, kept_for_chairman) among open router rows."""
    move, keep = [], []
    for r in rows:
        if not _is_router_row(r) or r.get("status") != "awaiting_chairman":
            continue
        signoff, why = needs_signoff(r)
        (keep if signoff else move).append((r, why))
    return move, keep


def apply(path: Path = ACTION_FILE, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    backup = path.with_name(f"{path.stem}.before-reroute.{now:%Y%m%d%H%M%S}.json")
    shutil.copy2(path, backup)
    moved = 0
    with aj.update(str(path), default=[], expect=list) as rows:
        move, keep = plan(rows)
        ids = {id(r) for r, _ in move}
        for r in rows:
            if id(r) in ids:
                r["status"] = "approved_by_board"
                r["owner"] = "CIT"
                r["signoff_reason"] = "board approval is final (Chairman directive 2026-11-09)"
                r["rerouted_at"] = now.isoformat()
                r["rerouted_by"] = "reroute_board_approvals.py"
                moved += 1
    return {"moved": moved, "kept_for_chairman": len(keep), "backup": str(backup)}


def main():
    rows = json.loads(ACTION_FILE.read_text())
    move, keep = plan(rows)
    print(f"{len(move)} router row(s) leave the Chairman's inbox; "
          f"{len(keep)} stay because they need his sign-off.")
    for r, _ in move:
        print(f"  move  [{r.get('severity') or '-'}|{r.get('category')}] {str(r.get('title'))[:80]}")
    for r, why in keep:
        print(f"  KEEP  [{r.get('severity') or '-'}|{r.get('category')}] {str(r.get('title'))[:70]}  ({why})")
    if "--apply" in sys.argv:
        print(apply())


if __name__ == "__main__":
    main()

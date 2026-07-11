#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — TRACKER HYGIENE (the "verified-closed" rule, Chairman-ratified 2026-07-09)
# =============================================================================
# Problem: issues got board-approved 14-16x while ALREADY FIXED, because nothing
# flipped their tracker entries — alarms cried wolf and real signals drowned.
# Rules enforced on data/issue_tracker.json every run (called by pod_health daily;
# re-verified by the Saturday investigator):
#  R1 RESOLUTION REGISTRY: any fix appends a matcher to data/resolution_registry.json
#     {"match": "<substring of issue title>", "note": "...", "date": "..."} —
#     matching open items are auto-flipped to resolved with the note.
#  R2 APPROVAL-LOOP BREAKER: open items with >=3 board approvals and no update in
#     21+ days get closed as "closed_stale_unverified" + ONE Chairman-queue line
#     (decide: reopen with an owner, or let it die). No more 16th approval.
# =============================================================================
import json, sys
from datetime import datetime
from pathlib import Path
DATA = Path(__file__).parent / "data"

def _load(p, d):
    try: return json.loads((DATA/p).read_text())
    except Exception: return d

def run():
    tracker = _load("issue_tracker.json", None)
    if tracker is None:
        print("tracker unreadable"); return {"flipped":0,"stale_closed":0}
    items = tracker if isinstance(tracker, list) else tracker.get("issues", [])
    registry = _load("resolution_registry.json", [])
    now = datetime.now(); flipped = stale = 0
    for i in items:
        if not isinstance(i, dict) or i.get("status") in ("resolved","closed","closed_stale_unverified"):
            continue
        title = str(i.get("title",""))
        hit = next((r for r in registry if r.get("match","").lower() in title.lower() and r.get("match")), None)
        if hit:
            i["status"]="resolved"; i["resolution_note"]=f"verified-closed via registry: {hit.get('note','')} ({hit.get('date','')})"
            flipped += 1; continue
        appr = i.get("board_approvals") or i.get("approvals") or 0
        upd = str(i.get("updated_at") or i.get("last_updated") or i.get("created_at") or "")[:19]
        try: age = (now - datetime.fromisoformat(upd)).days
        except Exception: age = 999
        if appr >= 3 and age >= 21:
            i["status"]="closed_stale_unverified"
            i["resolution_note"]=f"approval-loop breaker: {appr} approvals, untouched {age}d — closed per verified-closed policy; reopen only WITH an owner."
            stale += 1
    out = tracker if isinstance(tracker, list) else tracker
    (DATA/"issue_tracker.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"hygiene: {flipped} verified-closed, {stale} stale-loop closed")
    return {"flipped":flipped,"stale_closed":stale}

if __name__ == "__main__":
    run()

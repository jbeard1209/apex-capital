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

    # R2 fires on a condition that had been unreachable, so the first run after
    # the fix meets a backlog that accumulated while the rule was inert — ~1000
    # rows qualify at once. Closing is the expensive direction: a wrongly-closed
    # row is a real problem that vanishes silently, whereas a not-yet-closed row
    # is only noise. So R2 is paced, and it is SORTED not filtered — the
    # worst-looping issues are closed first rather than whichever the file
    # happened to list first. A cap with no ordering is a silent sampling rule.
R2_MAX_PER_RUN = 25


def run():
    tracker = _load("issue_tracker.json", None)
    if tracker is None:
        print("tracker unreadable"); return {"flipped":0,"stale_closed":0}
    items = tracker if isinstance(tracker, list) else tracker.get("issues", [])
    registry = _load("resolution_registry.json", [])
    now = datetime.now(); flipped = stale = 0
    r2_candidates = []
    for i in items:
        if not isinstance(i, dict) or i.get("status") in ("resolved","closed","closed_stale_unverified"):
            continue
        title = str(i.get("title",""))
        hit = next((r for r in registry if r.get("match","").lower() in title.lower() and r.get("match")), None)
        if hit:
            i["status"]="resolved"; i["resolution_note"]=f"verified-closed via registry: {hit.get('note','')} ({hit.get('date','')})"
            flipped += 1; continue
        # R2 read `board_approvals` / `approvals`. issue_tracker.log_issue() has
        # never written either field — present on 0 of 4423 live rows — so `appr`
        # was always 0 and this branch could not fire. The field that carries the
        # same quantity, and that the tracker actually maintains, is
        # `escalation_count`: how many times this was pushed without resolution.
        # (Live max: 19.) A rule whose input does not exist reports a clean zero
        # forever, which is indistinguishable from having nothing to do.
        appr = (i.get("escalation_count") or i.get("board_approvals")
                or i.get("approvals") or 0)
        upd = str(i.get("updated_at") or i.get("last_updated") or i.get("created_at") or "")[:19]
        try: age = (now - datetime.fromisoformat(upd)).days
        except Exception: age = 999
        if appr >= 3 and age >= 21:
            r2_candidates.append((appr, age, i))

    r2_candidates.sort(key=lambda t: (-t[0], -t[1]))
    r2_deferred = max(0, len(r2_candidates) - R2_MAX_PER_RUN)
    for appr, age, i in r2_candidates[:R2_MAX_PER_RUN]:
        i["status"]="closed_stale_unverified"
        i["resolution_note"]=f"approval-loop breaker: {appr} escalations, untouched {age}d — closed per verified-closed policy; reopen only WITH an owner."
        i["resolved_at"]=now.isoformat()
        stale += 1

    out = tracker if isinstance(tracker, list) else tracker
    (DATA/"issue_tracker.json").write_text(json.dumps(out, indent=2, default=str))
    # Always report the deferred count. Printing only what was closed would make
    # "25 closed" look identical whether 25 or 1025 qualified.
    print(f"hygiene: {flipped} verified-closed, {stale} stale-loop closed"
          + (f", {r2_deferred} deferred to later runs" if r2_deferred else ""))
    return {"flipped":flipped,"stale_closed":stale,"deferred":r2_deferred,
            "r2_candidates":len(r2_candidates)}

if __name__ == "__main__":
    run()

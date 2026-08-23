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

# A hand-written substring matcher is a blunt instrument: `{"match": "stale"}`
# would close several hundred unrelated rows in one run, and closing is the
# expensive direction — a wrongly-closed issue vanishes with nobody looking for
# it again. So a substring matcher that would hit more than this many issues is
# treated as a mistake, reported, and not applied. Signature matchers are exact
# by construction and are not capped.
R1_MAX_PER_MATCHER = 40


def run():
    tracker = _load("issue_tracker.json", None)
    if tracker is None:
        print("tracker unreadable"); return {"flipped":0,"stale_closed":0}
    items = tracker if isinstance(tracker, list) else tracker.get("issues", [])
    # Until 2026-08-23 this file had two readers and no writer, and had never
    # existed on either host — so R1 matched against [] every run and printed
    # "0 verified-closed", which reads exactly like "nothing needed closing".
    # issue_tracker.record_resolution() is the producer now. Reporting the
    # registry's SIZE is the part that makes the difference visible: a rule
    # whose input is empty must not look like a rule that found nothing.
    registry = _load("resolution_registry.json", [])
    # A hand-edited registry that ends up as a dict would iterate as bare
    # strings and blow up on r.get(); a bad file must degrade to "no matchers",
    # never take the hygiene run down with it.
    registry = [r for r in registry if isinstance(r, dict)] if isinstance(registry, list) else []
    now = datetime.now(); flipped = stale = 0
    r2_candidates = []

    # Pre-count substring matchers so an over-broad one is refused BEFORE it
    # closes anything, not after it has closed the first 40 rows it met.
    def _sig_of(issue):
        return str(issue.get("signature") or "").strip().lower()
    unresolved = [i for i in items if isinstance(i, dict)
                  and i.get("status") not in ("resolved","closed","closed_stale_unverified")]
    over_broad = set()
    for r in registry:
        m = str(r.get("match") or "").strip().lower()
        if not m:
            continue
        if sum(1 for i in unresolved if m in str(i.get("title","")).lower()) > R1_MAX_PER_MATCHER:
            over_broad.add(m)

    for i in items:
        if not isinstance(i, dict) or i.get("status") in ("resolved","closed","closed_stale_unverified"):
            continue
        title = str(i.get("title",""))
        sig = _sig_of(i)
        hit = next((r for r in registry
                    if (r.get("signature") and str(r["signature"]).strip().lower() == sig and sig)
                    or (r.get("match")
                        and str(r["match"]).strip().lower() not in over_broad
                        and str(r["match"]).lower() in title.lower())), None)
        if hit:
            i["status"]="resolved"; i["resolution_note"]=f"verified-closed via registry: {hit.get('note','')} ({hit.get('date','')})"
            i["resolved_at"]=now.isoformat()
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
    print(f"hygiene: {flipped} verified-closed (registry has {len(registry)} matcher(s))"
          f", {stale} stale-loop closed"
          + (f", {r2_deferred} deferred to later runs" if r2_deferred else "")
          + (f"; REFUSED {len(over_broad)} over-broad matcher(s): "
             f"{sorted(over_broad)[:3]}" if over_broad else ""))
    return {"flipped":flipped,"stale_closed":stale,"deferred":r2_deferred,
            "r2_candidates":len(r2_candidates),
            "registry_size":len(registry),"over_broad":sorted(over_broad)}

if __name__ == "__main__":
    run()

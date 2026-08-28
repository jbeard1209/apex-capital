#!/usr/bin/env python3
"""
Chairman Decision SLA  (Department: Governance / Logic Team)
============================================================
WHY THIS EXISTS
---------------
The 272-day buy-freeze (2026-07-27 post-mortem) survived so long partly because
the one thing that could have stopped it — a human decision — never got asked
for cleanly. The bug disguised itself as a routine "board must vote" item and
sat in a 153-entry chairman_action_required.json queue that nobody triages, and
whatever notice existed went out by *email*, where it drowned. Two failure modes:
  (1) decisions the Chairman should make were never surfaced to him CLEARLY, and
  (2) nothing forced a resolution when he didn't respond.

This agent is the governance backstop the Chairman asked for (2026-07-27):
  • Surface decision-needed items to Jack FIRST in the Claude APP, never email.
    It maintains data/chairman_decision_queue.json — a clean, deduped inbox that
    a Claude app session reads and presents. Email is deliberately NOT used here.
  • Enforce the SLA_DAYS deadline — 20 days, per Chairman directive 2026-07-29,
    tightened from the original 50. Any item still undecided that long after it
    was first
    surfaced is auto-escalated to a BOARD VOTE (a STRATEGY proposal appended to
    data/cit_pending_proposals.json, consumed by deputy_board.py / board_meeting.py),
    and its status flips to "escalated_to_board". The firm never stalls on a
    silent non-decision again.

SOURCES it pulls decisions from:
  • data/chairman_action_required.json  — items with status "awaiting_chairman"
  • (extensible) any producer that writes an awaiting_chairman item

DECISION LIFECYCLE (status on each queue entry):
  awaiting_chairman  → surfaced, clock running (deadline = first_surfaced + SLA_DAYS)
  decided            → Chairman resolved it (set by the app session; we archive it)
  resolved_upstream  → the producer closed the row this mirrors; no decision needed
  escalated_to_board → SLA_DAYS elapsed with no decision; board proposal filed

This agent NEVER makes the decision itself and NEVER sends email. It only
surfaces, tracks the clock, and — as a last resort — hands the call to the board.
"""
from __future__ import annotations
import json, argparse, datetime
from pathlib import Path

# One identity function for the whole Chairman-queue contract. If the ingest
# de-duped on a different key than the reconciler merges on, every sync would be
# undone by the next ingest — the producer/consumer mismatch this firm keeps
# rediscovering. Imported, never re-implemented.
from chairman_queue_sync import group_key as _group_key
import problem_lifecycle_sentinel as _PLS

DATA = Path(__file__).resolve().parent / "data"
CHAIR_ACTION  = DATA / "chairman_action_required.json"   # upstream producers
QUEUE_PATH    = DATA / "chairman_decision_queue.json"     # the app-facing inbox (this agent owns it)
CIT_PENDING   = DATA / "cit_pending_proposals.json"       # board escalation target

SLA_DAYS = 20   # Chairman directive 2026-07-29: escalate to board after 20 days, not 50


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _save(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2))


def _now():
    return datetime.datetime.utcnow()


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "").split("+")[0])
    except Exception:
        return None


def _iso(dt):
    return dt.isoformat()


def _ingest(queue_by_id):
    """Pull awaiting_chairman items from chairman_action_required.json into the
    app-facing queue, stamping first_surfaced + an SLA_DAYS deadline exactly once."""
    actions = _load(CHAIR_ACTION, [])
    if not isinstance(actions, list):
        return 0
    now = _now()
    added = 0

    # Ingest de-dupes on the SAME identity chairman_queue_sync merges on, not on
    # the raw id. Producers re-append one grievance every cycle with a bumped
    # counter ("CRO at HIGH for 41 days" → "…42 days"), which is a distinct id
    # and an identical decision; keying on id alone let 124 rows of one CRO
    # complaint into the Chairman's inbox. Ids already folded into a queue entry
    # (merged_ids) must not walk back in as new items either.
    seen_keys = set()
    seen_ids = set(queue_by_id)
    for q in queue_by_id.values():
        seen_keys.add(_group_key(q))
        seen_ids.update(q.get("merged_ids") or [])

    for a in actions:
        if not isinstance(a, dict):
            continue
        if a.get("status") != "awaiting_chairman":
            continue
        aid = a.get("id")
        if not aid or aid in seen_ids:
            continue
        akey = _group_key(a)
        if akey in seen_keys:
            continue
        seen_keys.add(akey)
        seen_ids.add(aid)
        # The SLA clock starts when the item ENTERS this managed queue
        # (now), NOT from the upstream item's historical timestamp. Anchoring to
        # old first_seen values would retroactively blow the deadline on the
        # whole existing backlog and flood the board with escalations on the
        # first run. We keep the original timestamp for reference only.
        queue_by_id[aid] = {
            "id": aid,
            "severity": a.get("severity", "MEDIUM"),
            "description": a.get("description", ""),
            # dedup_key/title are carried through, not dropped: _group_key reads
            # them, so an item that arrives with a producer-supplied dedup_key
            # must keep it or it will hash differently on the next run and be
            # re-ingested as a "new" decision forever.
            "dedup_key": a.get("dedup_key"),
            "title": a.get("title"),
            "source": a.get("source", "unknown"),
            "status": "awaiting_chairman",
            "first_surfaced": _iso(now),
            "deadline": _iso(now + datetime.timedelta(days=SLA_DAYS)),
            "upstream_first_seen": a.get("first_seen_of_group") or a.get("ts"),
            "surfaced_channel": "claude_app",   # explicitly NOT email
        }
        added += 1
    return added


def _escalate_to_board(item):
    """Append a STRATEGY board-vote proposal for an item that blew its SLA."""
    pending = _load(CIT_PENDING, [])
    if not isinstance(pending, list):
        pending = []
    # Prefix imported, not spelled: problem_lifecycle_sentinel reads it to tell
    # that this row is a VIEW of item['id'] rather than a problem of its own,
    # and a second copy of the format is a second thing that can drift.
    pid = f"{_PLS.SLA_ESCALATION_PREFIX}{item['id']}"
    if any(isinstance(p, dict) and p.get("id") == pid for p in pending):
        return False
    pending.append({
        "id": pid,
        "source": "Chairman Decision SLA (Governance)",
        "category": "STRATEGY",   # always routes to the Full Board (deputy_board.py)
        "severity": item.get("severity", "MEDIUM"),
        "confidence": 100,
        # Prefix imported for the same reason as the id prefix: once this row is
        # consumed by the board its id is gone and the title is all the sentinel
        # has left to recognise it by.
        "title": f"{_PLS.SLA_ESCALATION_TITLE_PREFIX} (Chairman did not respond "
                 f"in {SLA_DAYS}d): {item['id']}",
        "description": (f"This decision was surfaced to the Chairman on "
                        f"{item.get('first_surfaced')} and remained undecided for "
                        f"{SLA_DAYS} days. Per standing governance policy, the board now "
                        f"decides on his behalf so the firm does not stall.\n\n"
                        f"ORIGINAL ITEM ({item.get('severity')}, from {item.get('source')}):\n"
                        f"{item.get('description')}"),
        "evidence": f"chairman_decision_queue: first_surfaced={item.get('first_surfaced')}, "
                    f"deadline={item.get('deadline')} elapsed with status=awaiting_chairman.",
        "proposed_fix": "Board votes to resolve the original decision (approve / reject / "
                        "defer-with-explicit-owner). Record the outcome and close the queue item.",
        "auto_implementable": False,
        "ts": _iso(_now()),
    })
    _save(CIT_PENDING, pending)
    return True


ARCHIVE_PATH = DATA / "chairman_resolved_archive.json"   # chairman_queue_compactor's


def _close_resolved_upstream(queue_by_id):
    """Close queue items whose producer has already closed the row they mirror.

    This inbox is a MIRROR of chairman_action_required.json, and it had no path
    to hear a closure. `chairman_queue.resolve()` closes a row when the condition
    it described stops being observed, and `chairman_queue_compactor` then moves
    the closed row to chairman_resolved_archive.json — neither of which this file
    ever read. So once ingested, an item stayed `awaiting_chairman` until the
    Chairman decided it or the 20-day clock fired, and a finding that had cleared
    itself still reached the board as an escalation: a vote on a condition that
    no longer exists.

    Measured 2026-08-27, both hosts, independently (there is no state sync, so
    each accumulated its own): 44 of the Mac's 134 pending items (33%) and 22 of
    the VM's 58 (38%) were already resolved-and-archived upstream and still
    counting down. Two of them were the top rows of the Chairman's session
    banner, both marked OVERDUE.

    ABSENCE IS NOT RESOLUTION — this is the half that must not be simplified.
    An item whose upstream row is in NEITHER file is left exactly as it was.
    `chairman_queue._submit_locked` evicts a producer's least-severe rows past a
    per-source cap of 20, so a row can vanish from the upstream file while
    remaining a live, undecided question; 5 items on the Mac and 10 on the VM are
    in that state. Closing on absence would delete real decisions on the strength
    of a cap. Only an upstream row that EXISTS and is explicitly out of
    OPEN_STATUSES counts — the same rule `chairman_queue.resolve()` states for
    itself ("absence of a finding means resolved ONLY if the check that would
    have produced it actually ran"), and the one sweep_governance._closable_kinds
    learned first.

    Returns the number closed.
    """
    from chairman_queue import OPEN_STATUSES

    upstream = {}
    for row in _load(CHAIR_ACTION, []) + _load(ARCHIVE_PATH, []):
        if isinstance(row, dict) and row.get("id"):
            # First writer wins: the live queue is read before the archive, so a
            # row that was re-raised after being archived reads as open.
            upstream.setdefault(row["id"], row)

    now = _now()
    closed = 0
    for item in queue_by_id.values():
        if item.get("status") != "awaiting_chairman":
            continue
        ids = [item.get("id")] + list(item.get("merged_ids") or [])
        rows = [upstream[i] for i in ids if i in upstream]
        if not rows:
            continue                      # unknown, not resolved — leave it
        if any(r.get("status") in OPEN_STATUSES for r in rows):
            continue                      # a merged item is open while ANY leg is
        item["status"] = "resolved_upstream"
        item["resolved_at"] = _iso(now)
        item["resolved_by"] = (
            "every upstream row was closed by its producer "
            "(chairman_queue.resolve / compactor archive) — closed by "
            "re-observation, not by declaration, and never by absence")
        closed += 1
    return closed


def run(write: bool = True) -> dict:
    queue = _load(QUEUE_PATH, [])
    if not isinstance(queue, list):
        queue = []
    queue_by_id = {q["id"]: q for q in queue if isinstance(q, dict) and q.get("id")}

    ingested = _ingest(queue_by_id) if write else 0
    # BEFORE the escalation loop, deliberately: an item the producer has already
    # closed must never be handed to the board. Ingest runs first so a row that
    # is re-raised and closed in the same window is still seen.
    resolved_upstream = _close_resolved_upstream(queue_by_id)

    now = _now()
    escalated = []
    for item in queue_by_id.values():
        if item.get("status") != "awaiting_chairman":
            continue
        deadline = _parse_ts(item.get("deadline"))
        if deadline and now >= deadline:
            if write and _escalate_to_board(item):
                item["status"] = "escalated_to_board"
                item["escalated_at"] = _iso(now)
                escalated.append(item["id"])

    pending_items = [q for q in queue_by_id.values() if q.get("status") == "awaiting_chairman"]
    # Sort the app inbox: soonest deadline first, then severity.
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    pending_items.sort(key=lambda q: (_parse_ts(q.get("deadline")) or now,
                                      sev_rank.get(q.get("severity", "MEDIUM"), 2)))

    if write:
        _save(QUEUE_PATH, list(queue_by_id.values()))

    return {
        "generated_at": _iso(now),
        "ingested": ingested,
        "resolved_upstream": resolved_upstream,
        "pending_for_chairman": pending_items,
        "escalated_to_board": escalated,
        "sla_days": SLA_DAYS,
    }


def _days_left(item, now):
    d = _parse_ts(item.get("deadline"))
    return (d - now).days if d else None


def main():
    # Built from SLA_DAYS rather than typed, because the prose in this file said
    # "50-day" for two days after the constant became 20 — the reader trusts the
    # sentence, not the constant, so the sentence has to be derived from it.
    ap = argparse.ArgumentParser(
        description="Surface Chairman decisions to the Claude app and enforce "
                    f"the {SLA_DAYS}-day board-escalation SLA.")
    ap.add_argument("--report", action="store_true", help="print the current queue without ingesting/escalating")
    args = ap.parse_args()

    rep = run(write=not args.report)
    now = _now()

    print("=" * 66)
    print("  CHAIRMAN DECISION QUEUE  (surfaced in the Claude app — never email)")
    print("=" * 66)
    pend = rep["pending_for_chairman"]
    if not pend:
        print("  ✅ No decisions currently awaiting the Chairman.")
    else:
        print(f"  {len(pend)} item(s) need your decision, Jack "
              f"(auto-escalate to board at {SLA_DAYS} days):\n")
        for q in pend:
            dl = _days_left(q, now)
            dl_str = f"{dl}d left" if dl is not None and dl >= 0 else (
                     "OVERDUE" if dl is not None else "no deadline")
            print(f"   • [{q.get('severity')}] {q.get('id')}  ({dl_str})")
            desc = (q.get("description") or "").strip().replace("\n", " ")
            print(f"       {desc[:160]}{'…' if len(desc) > 160 else ''}")
            print(f"       source: {q.get('source')}")
    if rep["escalated_to_board"]:
        print(f"\n  ⚖️  Auto-escalated to BOARD VOTE (SLA blown): "
              f"{', '.join(rep['escalated_to_board'])}")
    if not args.report:
        print(f"\n  (ingested {rep['ingested']} new item(s), closed "
              f"{rep['resolved_upstream']} already-resolved upstream)")
    elif rep["resolved_upstream"]:
        # A dry run reconciles in memory but writes nothing, so the count is
        # what a real run WOULD close. Saying so keeps `--report` honest: the
        # list above is already the post-reconciliation set.
        print(f"\n  ({rep['resolved_upstream']} of these were already resolved "
              f"upstream and are excluded above; --report writes nothing)")
    print()


if __name__ == "__main__":
    main()

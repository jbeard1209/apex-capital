#!/usr/bin/env python3
"""
Problem Lifecycle Sentinel
================================================================================
Kills the firm's most notorious failure mode: a problem gets *flagged* but then
either (a) is never acted on, or (b) is "fixed" with a band-aid and quietly
comes back — over and over — because nothing tracks a problem across its whole
life. The live evidence is a proposal in the queue right now that reads
"(first seen 2026-06-11, 210x). This needs a decision, not a rerun." Same
problem, raised 210 times, never eliminated.

Root cause of the loop: every producer re-emits findings each cycle with a NEW
id (e.g. `LTIA-20260729105803`), so any tracker keyed on ids is blind to
recurrence. The consumers (board, chairman) defer, the producer re-fires, and
the id churn hides it. So this sentinel keys on a *normalized signature* (the
same `dedup_key`-or-digit-stripped-title convention `board_meeting.py` uses),
maintains a persistent lifecycle history, and enforces three things the
scattered agents never did:

  1. COLLAPSE   — one problem == one lifecycle record (occurrence count), so a
                  210x re-flag becomes one tracked item, not 210 queue entries.
  2. PROGRESS   — an open problem past the SLA auto-escalates to the Chairman
                  (STALE) instead of sitting; a problem that vanished with no
                  recorded resolution is surfaced (SILENT_DROP) so we verify it
                  was actually fixed, not just dropped.
  3. NO LOOPS   — a problem that was marked resolved and then RECURS is a failed
                  fix (LOOPING). It is escalated to the FULL board as an
                  ARCHITECTURE item (a structural fix, not another band-aid) and
                  its signature is written to a loop-break set that the CIT
                  actuator consults to STOP re-applying band-aids to it.

Every escalation is IDEMPOTENT (deduped by deterministic id / dedup_key) so the
sentinel can run every pipeline cycle without flooding the very queues it
protects — the cardinal sin here would be to *become* the loop it detects.

Consumer wiring (verified — no orphan queues):
  * LOOPING     -> data/cit_pending_proposals.json, category ARCHITECTURE
                  (FULL_BOARD_ONLY in deputy_board.py -> board_meeting.py votes;
                  cannot be silently auto-deferred by the deputy path).
  * STALE/DROP  -> data/chairman_action_required.json (read by
                  chairman_decision_sla + the app inbox).
  * loop-break  -> data/problem_loops.json (read by cit_implementation_agent).

Runtime state (data/problem_lifecycle.json, data/problem_loops.json) is
gitignored + cloud-synced like every other data/*.json queue — never committed.
"""
from __future__ import annotations

import json
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# --- sources scanned (path, kind) ------------------------------------------
PENDING_PATH   = DATA / "cit_pending_proposals.json"
IMPL_QUEUE_PATH = DATA / "cit_implementation_queue.json"
CHAIRMAN_PATH  = DATA / "chairman_action_required.json"

# --- persistent lifecycle state --------------------------------------------
HISTORY_PATH = DATA / "problem_lifecycle.json"
LOOPS_PATH   = DATA / "problem_loops.json"

# --- policy ----------------------------------------------------------------
STALE_SLA_DAYS = 20          # aligns with the Chairman decision SLA
LOOP_RECUR_THRESHOLD = 1     # 1 recurrence after a "resolution" == a failed fix
CHRONIC_OCCURRENCE = 20      # raised this many distinct times == chronic loop

# A finding is CLOSED only if its status is terminal-resolved. Everything else
# (queued/needs_human/systemic/routed_to_human/pending/awaiting_*) is OPEN.
# won't-fix statuses are closed-but-not-resolved (owner assigned, decision made)
# and must NOT be treated as a silent drop.
RESOLVED_STATUSES = {"implemented", "resolved", "done", "closed", "completed", "fixed"}
WONT_FIX_STATUSES = {"skipped", "protected", "wont_fix", "rejected", "dismissed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_between(a: str, b: str) -> float:
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
        if da.tzinfo is None:
            da = da.replace(tzinfo=timezone.utc)
        if db.tzinfo is None:
            db = db.replace(tzinfo=timezone.utc)
        return abs((db - da).total_seconds()) / 86400.0
    except Exception:
        return 0.0


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _save(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# Signature: the anti-loop heart. Same convention board_meeting.py uses, so a
# problem carries ONE identity across producers and across id churn.
# ---------------------------------------------------------------------------
def normalize_title(title: str) -> str:
    """Digit/date/amount-insensitive so '6 ERROR lines' and '8 ERROR lines' are
    the SAME problem. Mirrors board_meeting._dedup convention (re.sub digits)."""
    t = str(title or "").lower()
    t = re.sub(r"\d{4}-\d{2}-\d{2}", "", t)   # ISO dates
    t = re.sub(r"[\$€£]\s*[\d,\.]+", "", t)    # money
    t = re.sub(r"\d+", "", t)                   # any remaining digits
    t = re.sub(r"[^a-z ]+", " ", t)             # punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t[:80]


def signature(item: dict, kind: str) -> str:
    """Prefer an upstream-provided stable dedup_key; else derive one from the
    normalized title. Namespaced so an impl-queue item and an unrelated chairman
    item with a coincidentally similar title don't collide."""
    dk = item.get("dedup_key")
    if dk:
        base = f"dk:{dk}"
    else:
        title = item.get("title") or item.get("description") or ""
        base = f"{kind}:{normalize_title(title)}"
    return "sig-" + hashlib.sha1(base.encode()).hexdigest()[:12]


def _status_of(item: dict) -> str:
    return str(item.get("status") or "").lower()


def _is_resolved(status: str) -> bool:
    return status in RESOLVED_STATUSES


def _is_wont_fix(status: str) -> bool:
    return status in WONT_FIX_STATUSES


def _is_open(status: str) -> bool:
    return not _is_resolved(status) and not _is_wont_fix(status)


# ---------------------------------------------------------------------------
# Scan the live queues into a signature -> aggregate snapshot.
# ---------------------------------------------------------------------------
def _scan() -> dict:
    snap: dict = {}
    for path, kind in ((PENDING_PATH, "proposal"),
                       (IMPL_QUEUE_PATH, "impl"),
                       (CHAIRMAN_PATH, "chairman")):
        for item in _load(path, []):
            if not isinstance(item, dict):
                continue
            sig = signature(item, kind)
            st = _status_of(item)
            title = item.get("title") or item.get("description") or "(untitled)"
            cur = snap.setdefault(sig, {
                "sig": sig, "kinds": set(), "titles": set(),
                "open": False, "resolved": False, "wont_fix": False,
                "title": title,
            })
            cur["kinds"].add(kind)
            cur["titles"].add(title[:120])
            if _is_open(st):
                cur["open"] = True
            elif _is_resolved(st):
                cur["resolved"] = True
            elif _is_wont_fix(st):
                cur["wont_fix"] = True
            # keep a human-readable representative title (prefer an open one)
            if _is_open(st) or cur["title"] == "(untitled)":
                cur["title"] = title
    return snap


# ---------------------------------------------------------------------------
# Merge the scan into persistent history. Idempotent under repeated runs with an
# unchanged queue state (present-last-run guard => no double counting).
# ---------------------------------------------------------------------------
def _update_history(snap: dict, history: dict, now: str) -> dict:
    for sig, cur in snap.items():
        h = history.get(sig)
        if h is None:
            h = {
                "sig": sig, "first_seen": now, "last_seen": now,
                "title": cur["title"], "kinds": sorted(cur["kinds"]),
                "occurrences": 1, "times_resolved": 0,
                "recurred_after_resolution": 0,
                "status": "OPEN" if cur["open"] else ("RESOLVED" if cur["resolved"] else "WONT_FIX"),
                "present": True, "last_open": now if cur["open"] else None,
            }
            history[sig] = h
            continue

        was_present = h.get("present", False)
        prev_status = h.get("status", "OPEN")

        # A recurrence is a problem that was RESOLVED and is open again — whether
        # it left the queue and came back, or its in-queue status flipped
        # implemented->queued. Continuous open presence == one ongoing occurrence.
        recurrence = (prev_status == "RESOLVED" and cur["open"])
        if (not was_present) or recurrence:
            h["occurrences"] = h.get("occurrences", 0) + 1
        if recurrence:
            h["recurred_after_resolution"] = h.get("recurred_after_resolution", 0) + 1

        # A resolution observed while previously open.
        if cur["resolved"] and not cur["open"] and prev_status == "OPEN":
            h["times_resolved"] = h.get("times_resolved", 0) + 1

        h["last_seen"] = now
        h["present"] = True
        h["title"] = cur["title"]
        h["kinds"] = sorted(set(h.get("kinds", [])) | cur["kinds"])
        if cur["open"]:
            h["status"] = "OPEN"
            if not h.get("last_open"):
                h["last_open"] = now
        elif cur["resolved"]:
            h["status"] = "RESOLVED"
            h["last_open"] = None
        elif cur["wont_fix"]:
            h["status"] = "WONT_FIX"
            h["last_open"] = None

    # Items in history but NOT in this scan: they left every queue.
    scanned = set(snap.keys())
    for sig, h in history.items():
        if sig in scanned:
            continue
        if h.get("present"):
            h["present"] = False
            # Vanished while still open and never recorded resolved == silent drop.
            if h.get("status") == "OPEN":
                h["status"] = "SILENT_DROP"
    return history


# ---------------------------------------------------------------------------
# Classify each tracked problem.
# ---------------------------------------------------------------------------
def classify(h: dict, now: str) -> str:
    recurred = h.get("recurred_after_resolution", 0)
    occ = h.get("occurrences", 0)
    status = h.get("status", "OPEN")

    if recurred >= LOOP_RECUR_THRESHOLD or (status == "OPEN" and occ >= CHRONIC_OCCURRENCE):
        return "LOOPING"
    if status == "SILENT_DROP":
        return "SILENT_DROP"
    if status == "OPEN":
        age = _days_between(h.get("last_open") or h.get("first_seen"), now)
        if age >= STALE_SLA_DAYS:
            return "STALE"
        return "OPEN"
    return status  # RESOLVED / WONT_FIX


# ---------------------------------------------------------------------------
# Idempotent escalation helpers.
# ---------------------------------------------------------------------------
def _append_deduped(path: Path, entry: dict, key_field: str) -> bool:
    """Append entry only if no existing item shares its key. Returns True if
    written. This is what keeps the sentinel from re-flooding the queues."""
    items = _load(path, [])
    kv = entry.get(key_field)
    for it in items:
        if isinstance(it, dict) and it.get(key_field) == kv:
            return False
    items.append(entry)
    _save(path, items)
    return True


def _escalate_looping(h: dict, now: str) -> bool:
    sig = h["sig"]
    entry = {
        "id": f"loop-{sig}",
        "source": "Problem Lifecycle Sentinel",
        "category": "ARCHITECTURE",   # FULL_BOARD_ONLY -> board must vote
        "severity": "HIGH",
        "confidence": "HIGH",
        "title": f"[LOOP] Recurring problem needs a STRUCTURAL fix, not another band-aid",
        "dedup_key": f"loop_{sig}",
        "description": (
            f"'{h.get('title')}' has recurred after being resolved "
            f"{h.get('recurred_after_resolution',0)}x (seen {h.get('occurrences',0)}x since "
            f"{h.get('first_seen','?')[:10]}). Band-aid fixes are not holding. The loop-break "
            f"set now blocks the CIT actuator from re-patching it — the board must ratify a "
            f"root-cause fix or a documented won't-fix decision."
        ),
        "proposed_fix": "Root-cause / structural remediation ratified by the full board.",
        "status": "pending",
        "ts": now,
    }
    return _append_deduped(PENDING_PATH, entry, "id")


def _escalate_chairman(h: dict, cls: str, now: str) -> bool:
    sig = h["sig"]
    if cls == "STALE":
        desc = (f"'{h.get('title')}' has been OPEN with no resolution for "
                f">= {STALE_SLA_DAYS}d (first seen {h.get('first_seen','?')[:10]}). "
                f"It needs a decision — resolve, reassign, or explicitly close.")
        eid = f"lifecycle-stale-{sig}"
    else:  # SILENT_DROP
        desc = (f"'{h.get('title')}' disappeared from every queue with NO recorded "
                f"resolution. Verify it was actually fixed, not silently dropped.")
        eid = f"lifecycle-drop-{sig}"
    entry = {
        "id": eid,
        "source": "Problem Lifecycle Sentinel",
        "severity": "MEDIUM",
        "description": desc,
        "status": "awaiting_chairman",
        "ts": now,
    }
    return _append_deduped(CHAIRMAN_PATH, entry, "id")


def _update_loops(history: dict) -> list:
    """Loop-break set: signatures currently LOOPING. Self-correcting — a sig
    drops out once it is no longer open/recurring (RESOLVED or gone)."""
    loops = _load(LOOPS_PATH, {})
    if not isinstance(loops, dict):
        loops = {}
    active = set()
    for sig, h in history.items():
        if h.get("_class") == "LOOPING":
            active.add(sig)
            loops[sig] = {
                "sig": sig, "title": h.get("title"),
                "recurred": h.get("recurred_after_resolution", 0),
                "occurrences": h.get("occurrences", 0),
                "since": h.get("first_seen"),
            }
    # prune sigs that are no longer looping
    for sig in list(loops.keys()):
        if sig not in active:
            loops.pop(sig, None)
    _save(LOOPS_PATH, loops)
    return sorted(loops.keys())


def is_loop_broken(item: dict, kind: str = "impl") -> bool:
    """Consulted by the CIT actuator: is this finding a known unbroken loop that
    must go to the board instead of being auto-patched again?"""
    loops = _load(LOOPS_PATH, {})
    if not isinstance(loops, dict):
        return False
    return signature(item, kind) in loops


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def run(escalate: bool = True) -> dict:
    now = _now()
    history = _load(HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}

    snap = _scan()
    history = _update_history(snap, history, now)

    report = {"ts": now, "tracked": len(history),
              "looping": [], "stale": [], "silent_drop": [],
              "escalated_board": 0, "escalated_chairman": 0}

    for sig, h in history.items():
        cls = classify(h, now)
        h["_class"] = cls
        if cls == "LOOPING":
            report["looping"].append(sig)
            if escalate and _escalate_looping(h, now):
                report["escalated_board"] += 1
        elif cls == "STALE":
            report["stale"].append(sig)
            if escalate and _escalate_chairman(h, cls, now):
                report["escalated_chairman"] += 1
        elif cls == "SILENT_DROP":
            report["silent_drop"].append(sig)
            if escalate and _escalate_chairman(h, cls, now):
                report["escalated_chairman"] += 1

    loops = _update_loops(history)
    report["loop_break_set"] = loops
    _save(HISTORY_PATH, history)
    return report


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Problem Lifecycle Sentinel")
    p.add_argument("--report", action="store_true", help="scan + classify, no escalation")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    rep = run(escalate=not args.report)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"[lifecycle] tracked={rep['tracked']} "
              f"looping={len(rep['looping'])} stale={len(rep['stale'])} "
              f"silent_drop={len(rep['silent_drop'])} "
              f"-> board+{rep['escalated_board']} chairman+{rep['escalated_chairman']} "
              f"loop_break={len(rep['loop_break_set'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

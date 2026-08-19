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
gitignored — and, as of 2026-08-17, actually is. This line previously claimed it
was "gitignored + cloud-synced". Neither half was true: no rule matched either
file (they were untracked only because nobody had run `git add -A` here), and
cloud-sync went away with Railway on 2026-07-10. Both files are now named in
.gitignore explicitly.

The correction matters beyond tidiness. Nothing syncs, so this state is PER-HOST:
the Mac and the VM keep separate lifecycles for the same problem and neither can
see the other's. Do not reason as if a signature recorded on one host is known to
the other (CLAUDE.md, "There is NO state sync between hosts"). `board_issue_ledger`
imports this module's `signature()` for exactly that reason — one definition of
one fact — and gates its board relay on the designated host so only one of the
two lifecycles ever reaches the board.
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

# ---------------------------------------------------------------------------
# DERIVED ROWS: a view of a problem is not a problem  (2026-08-19)
# ---------------------------------------------------------------------------
# Measured on the VM: 91 of the Chairman's 214 rows — 43% — were rows this
# sentinel had itself written about other rows, and 18 of those were about
# another governance row. The mechanism is a ratchet: a real problem goes 20
# days undecided, so this file writes `lifecycle-stale-sig-X` into the same
# queue it scans. That new row is then tracked as an independent problem, sits
# undecided for 20 days of its own, and earns a `lifecycle-stale-sig-Y` about
# it. Each generation is cheap to produce and indistinguishable, at a glance,
# from a real finding — and a queue nobody reads is not a control, which is
# the whole reason chairman_queue_compactor.py exists.
#
# The rule is that a derived row has no lifecycle of its own: it exists exactly
# as long as the thing it points at is unresolved. So it is TRACKED (its
# signature stays in history, so removing it later is never read as a drop) but
# never CLASSIFIED as OPEN, never escalated, and retired automatically once its
# subject closes. See retire_derived_rows().
#
# These prefixes are the ones this module mints, plus chairman_decision_sla's —
# which imports SLA_ESCALATION_PREFIX from here rather than holding its own
# copy, because a second copy of an identifier format is a second thing that
# can drift (reconciliation_check.py held four such copies; one was 23 days
# stale). The import direction is forced: chairman_decision_sla already depends
# on this module transitively, so the constant has to live at the root.
STALE_ID_PREFIX = "lifecycle-stale-"
DROP_ID_PREFIX = "lifecycle-drop-"
LOOP_ID_PREFIX = "loop-"
SLA_ESCALATION_PREFIX = "sla-escalation-"
DERIVED_ID_PREFIXES = (STALE_ID_PREFIX, DROP_ID_PREFIX, LOOP_ID_PREFIX,
                       SLA_ESCALATION_PREFIX)

# A terminal status that is emphatically NOT a silent drop. It means: we
# stopped tracking this row as a problem in its own right, on purpose, and
# here is the row it was a view of.
DERIVED_STATUS = "DERIVED"


def derived_subject(item: dict):
    """The id/signature this row is merely a VIEW of, or None if it is a
    problem in its own right.

    `lifecycle-stale-sig-abc` -> `sig-abc`;  `sla-escalation-foo-42` -> `foo-42`.
    """
    if not isinstance(item, dict):
        return None
    rid = str(item.get("id") or "")
    for pre in DERIVED_ID_PREFIXES:
        if rid.startswith(pre):
            return rid[len(pre):] or None
    return None


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
def _register(snap: dict, sig: str, kind: str, title: str, st: str,
              derived_of: str = None) -> None:
    cur = snap.setdefault(sig, {
        "sig": sig, "kinds": set(), "titles": set(),
        "open": False, "resolved": False, "wont_fix": False,
        "title": title, "derived_of": None,
    })
    # Derived-ness is a property of the row, not of the run: once any producer
    # of this signature is a view of something else, it stays one. Recording
    # the subject (not just a bool) is what lets retire_derived_rows close it
    # against the right history entry.
    if derived_of and not cur.get("derived_of"):
        cur["derived_of"] = derived_of
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


def _scan() -> dict:
    snap: dict = {}
    for path, kind in ((PENDING_PATH, "proposal"),
                       (IMPL_QUEUE_PATH, "impl"),
                       (CHAIRMAN_PATH, "chairman")):
        for item in _load(path, []):
            if not isinstance(item, dict):
                continue
            st = _status_of(item)
            title = item.get("title") or item.get("description") or "(untitled)"
            subj = derived_subject(item)
            _register(snap, signature(item, kind), kind, title, st, subj)

            # A row folded into this one by chairman_queue_sync is still in the
            # queue — it is just wearing the survivor's title. That file dedups
            # on digit-collapsed text and this one on a hash of the normalized
            # title, so a fold makes the loser's signature disappear from every
            # queue and lands here as a SILENT_DROP, complete with a
            # lifecycle-drop-* row to the Chairman about a problem nobody
            # dropped. Its lineage is carried in merged_titles for exactly this
            # reason; merged_ids cannot serve, because a signature is text.
            for folded in (item.get("merged_titles") or []):
                if isinstance(folded, str) and folded.strip():
                    _register(snap, signature({"title": folded}, kind), kind,
                              folded, st)
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
            if cur.get("derived_of"):
                h["derived_of"] = cur["derived_of"]
                h["status"] = DERIVED_STATUS
                h["last_open"] = None
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
        if cur.get("derived_of"):
            # Tracked so its later removal is never read as a drop, but never
            # OPEN, so it can never go STALE and spawn a row about itself.
            h["derived_of"] = cur["derived_of"]
            h["status"] = DERIVED_STATUS
            h["last_open"] = None
            continue
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

    # Checked before everything else, including LOOPING: a view of a problem
    # that keeps recurring is not itself a recurring problem, and escalating it
    # would put a second board proposal behind one root cause.
    if h.get("derived_of") or status == DERIVED_STATUS:
        return DERIVED_STATUS
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


SCANNED_QUEUES = ((PENDING_PATH, "proposal"),
                  (IMPL_QUEUE_PATH, "impl"),
                  (CHAIRMAN_PATH, "chairman"))


def flag_stale_in_place(sig: str, h: dict, now: str) -> int:
    """Stamp the SLA breach ON the stale row(s). Returns rows stamped.

    An escalation is an ATTRIBUTE of a problem, not a second problem. Appending
    `lifecycle-stale-<sig>` — the old behaviour — put a row in the queue whose
    entire content was "the row above me is 20 days old", doubling the queue's
    length without adding a fact, and then aged into a third row about the
    second. Stamping the row itself says the same thing in the place a reader
    is already looking, and cannot compound.

    Returns 0 when the signature is in no queue, which is the caller's signal
    to fall back to appending a row: a breach we cannot attach to anything must
    still surface somewhere rather than evaporate.
    """
    stamped = 0
    age = _days_between(h.get("last_open") or h.get("first_seen"), now)
    for path, kind in SCANNED_QUEUES:
        items = _load(path, [])
        if not isinstance(items, list):
            continue
        touched = False
        for it in items:
            if not isinstance(it, dict) or signature(it, kind) != sig:
                continue
            if not _is_open(_status_of(it)):
                continue
            # first_flagged is written once: it is when the SLA was breached,
            # not when the sentinel last looked at it.
            if not it.get("sla_breached"):
                it["sla_breached"] = True
                it["sla_breached_at"] = now
                touched = True
            if it.get("stale_days") != int(age):
                it["stale_days"] = int(age)
                it["sla_note"] = (
                    f"OPEN with no resolution for >= {STALE_SLA_DAYS}d "
                    f"(first seen {h.get('first_seen','?')[:10]}). It needs a "
                    f"decision — resolve, reassign, or explicitly close.")
                touched = True
            stamped += 1
        if touched:
            _save(path, items)
    return stamped


def _escalate_chairman(h: dict, cls: str, now: str) -> bool:
    sig = h["sig"]
    if cls == "STALE":
        desc = (f"'{h.get('title')}' has been OPEN with no resolution for "
                f">= {STALE_SLA_DAYS}d (first seen {h.get('first_seen','?')[:10]}). "
                f"It needs a decision — resolve, reassign, or explicitly close. "
                f"(No queue row carries this signature, so the breach could not "
                f"be stamped on the problem itself.)")
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


def _update_loops(history: dict, write: bool = True) -> list:
    """Loop-break set: signatures currently LOOPING. Self-correcting — a sig
    drops out once it is no longer open/recurring (RESOLVED or gone).

    `write=False` for --report: the loop-break set gates the CIT actuator, so a
    dry run that prunes it would quietly re-arm auto-patching on a loop.
    """
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
    if write:
        _save(LOOPS_PATH, loops)
    return sorted(loops.keys())


def is_loop_broken(item: dict, kind: str = "impl") -> bool:
    """Consulted by the CIT actuator: is this finding a known unbroken loop that
    must go to the board instead of being auto-patched again?"""
    loops = _load(LOOPS_PATH, {})
    if not isinstance(loops, dict):
        return False
    return signature(item, kind) in loops


def _subject_is_settled(subject: str, history: dict, by_id: dict):
    """(settled, why) for the thing a derived row points at.

    Two kinds of subject, because two producers mint derived rows: this file
    points at a signature, chairman_decision_sla points at a queue item id.
    Returns settled=False whenever the subject is still live OR cannot be
    identified — a view is only safe to retire once we can SAY what happened to
    what it was watching. "I can't find it" is not a closure; it is the reason
    _escalate_chairman exists.
    """
    h = history.get(subject)
    if h is not None:
        st = h.get("status", "OPEN")
        if st in ("RESOLVED", "WONT_FIX"):
            return True, "underlying %s is %s" % (subject, st)
        return False, "underlying %s is %s" % (subject, st)
    item = by_id.get(subject)
    if item is not None:
        st = _status_of(item)
        if _is_resolved(st) or _is_wont_fix(st):
            return True, "underlying item %s is %s" % (subject, st or "closed")
        return False, "underlying item %s is %s" % (subject, st or "open")
    return False, "underlying %s is not in history or any queue" % subject


def _sigs_flagged_in_place() -> set:
    """Signatures of rows that already carry the SLA breach on themselves."""
    out = set()
    for path, kind in SCANNED_QUEUES:
        for it in _load(path, []):
            if isinstance(it, dict) and it.get("sla_breached"):
                out.add(signature(it, kind))
    return out


def retire_derived_rows(rows: list, history: dict, now: str) -> list:
    """Close, in place, every derived row whose subject has settled.

    Not delete — the same rule the rest of the firm follows: close in place
    with the evidence, let the detector observe the closure, and let
    chairman_queue_compactor archive it afterwards. Deleting here would
    manufacture the silent drop this file was built to detect, at scale, using
    this file's own rows.

    Idempotent: a row that already carries resolved_at is left alone, so the
    stamp records when the view was retired and not when it was last read.
    """
    by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}
    flagged = _sigs_flagged_in_place()
    retired = []
    for row in rows:
        subject = derived_subject(row)
        if not subject or row.get("resolved_at"):
            continue
        if _status_of(row) in RESOLVED_STATUSES | WONT_FIX_STATUSES:
            continue
        settled, why = _subject_is_settled(subject, history, by_id)
        if not settled and str(row.get("id","")).startswith(STALE_ID_PREFIX) \
                and subject in flagged:
            # The breach this row announced now lives on the problem itself, so
            # the row is a duplicate of a fact the reader already has. Retiring
            # it is not dropping the escalation — flag_stale_in_place re-stamps
            # the subject every run for as long as it stays open.
            settled = True
            why = ("the SLA breach is now stamped on %s itself (sla_breached), "
                   "so this separate row says nothing the subject does not"
                   % subject)
        if not settled:
            continue
        row["status"] = "resolved"
        row["resolved_at"] = now
        row["resolution"] = (
            "Derived row retired by problem_lifecycle_sentinel: %s, so this "
            "view has nothing left to watch. The subject keeps its own history "
            "entry; this row is closed rather than deleted so the closure is "
            "observable, and chairman_queue_compactor archives it once it is."
            % why)
        retired.append(row.get("id"))
    return retired


def writer_verdict():
    """(may_write, why) — only the designated host owns lifecycle state.

    The lifecycle of a firm-wide problem is a firm-wide fact, and the two hosts
    share no `data/`. Each kept its own history, so each had its own opinion of
    when a problem went stale: on 2026-08-19 the VM retired 40 spent rows and
    the Mac retired 18, and 17 rows ended up archived on one host and still
    OPEN on the other — the private-diary failure, in the control that exists
    to notice failures.

    Unlike single_writer's trading guard this one is symmetric and blocks
    outright, because the asymmetry there was about not trapping a position: an
    exit blocked costs money with no ceiling. Nothing here can trap anything.
    The worst case is that governance escalation lags while the designated host
    is down, which chairman_decision_sla and the report-age checks already see.
    """
    try:
        import single_writer
        want, me = single_writer.designated_host(), single_writer.this_host()
    except Exception as exc:                      # pragma: no cover - defensive
        return True, "single_writer unavailable (%s); writing unguarded" % exc
    if not want:
        return True, "APEX_TRADING_HOST unset — shadow mode, writing allowed"
    if want == me:
        return True, "designated host"
    return False, ("read-only on '%s': lifecycle state is owned by '%s'. Run "
                   "the sentinel there; a second history is a second opinion "
                   "about the same problem." % (me, want))


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def run(escalate: bool = True) -> dict:
    now = _now()
    history = _load(HISTORY_PATH, {})
    if not isinstance(history, dict):
        history = {}

    may_write, why = writer_verdict()
    if escalate and not may_write:
        escalate = False

    # Retire spent views BEFORE the scan, against the history as it stood at
    # the end of the last run, so this run observes them already closed and
    # records the resolution. Doing it after would leave a one-run window in
    # which the row is closed but no detector has seen it close — which is
    # exactly the window chairman_queue_compactor refuses to compact in.
    chairman_rows = _load(CHAIRMAN_PATH, [])
    retired = retire_derived_rows(chairman_rows, history, now) if escalate else []
    if retired:
        _save(CHAIRMAN_PATH, chairman_rows)

    snap = _scan()
    history = _update_history(snap, history, now)

    report = {"ts": now, "tracked": len(history),
              "looping": [], "stale": [], "silent_drop": [], "derived": [],
              "retired_derived": retired,
              "escalated_board": 0, "escalated_chairman": 0,
              "flagged_in_place": 0,
              "writable": may_write, "writer_note": why}

    for sig, h in history.items():
        cls = classify(h, now)
        h["_class"] = cls
        if cls == "LOOPING":
            report["looping"].append(sig)
            if escalate and _escalate_looping(h, now):
                report["escalated_board"] += 1
        elif cls == "STALE":
            report["stale"].append(sig)
            if escalate:
                if flag_stale_in_place(sig, h, now):
                    report["flagged_in_place"] += 1
                elif _escalate_chairman(h, cls, now):
                    report["escalated_chairman"] += 1
        elif cls == "SILENT_DROP":
            report["silent_drop"].append(sig)
            if escalate and _escalate_chairman(h, cls, now):
                report["escalated_chairman"] += 1
        elif cls == DERIVED_STATUS:
            report["derived"].append(sig)

    loops = _update_loops(history, write=escalate)
    report["loop_break_set"] = loops
    if escalate:
        # --report used to persist history anyway. A dry run that advances the
        # `present` flags is not a dry run: the second --report classifies
        # against state the first one wrote, and on 2026-08-19 a --report on
        # the Mac silently replaced a 21-day-old history with 226 fresh
        # signatures. Read-only means read-only.
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
              f"derived={len(rep['derived'])} "
              f"-> board+{rep['escalated_board']} chairman+{rep['escalated_chairman']} "
              f"retired+{len(rep['retired_derived'])} "
              f"loop_break={len(rep['loop_break_set'])}")
        if not rep["writable"]:
            print(f"[lifecycle] NOTHING WRITTEN — {rep['writer_note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

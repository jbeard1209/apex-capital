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
from typing import Optional          # py3.9 on the Mac: never `str | None`

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
# CHRONIC_OCCURRENCE was priced against the OLD counting rule and has never
# fired. It dates from the incident where one proposal was re-raised 210x — but
# that count came from an id-keyed tracker, and under signature dedup the same
# incident counts as ~1. Measured on the VM's live 308-signature history
# 2026-08-21: max occurrences 9, distribution {1:242, 2:34, 3:2, 4:2, 5:1, 6:12,
# 7:13, 9:2}, and ZERO signatures at >= 20 across 72 days of queue data. So this
# half of the LOOPING test is a branch that cannot be reached, priced 2.2x above
# the observed maximum. Left at 20 deliberately: lowering it changes what gets
# escalated to the FULL board, which is the Chairman's call, not a tidy-up.
# Queued as a derivation for board review rather than silently retuned.
CHRONIC_OCCURRENCE = 20      # raised this many distinct times == chronic loop

# Fields that record when a row was RAISED. Write-once only — this list is the
# whole correctness of the staleness clock.
#
# `ts` is deliberately EXCLUDED even though it is the most common date-ish field
# in these queues (48 of 57 pending proposals, 91 of 145 Chairman rows). It is
# refreshed on write, not set on creation, and the proof is in the data: on
# 2026-08-21, 12 rows carried an sla_breached_at stamp — an event that by
# definition cannot happen before day 20 — while their `ts` claimed they were
# under 4 days old. Seeding the clock from `ts` would not merely be imprecise,
# it would reset every problem's age on every write and make an aging queue read
# as permanently new. A field that looks like a date is not a date.
RAISED_AT_FIELDS = ("queued_at", "created_at", "raised_at")

# first_seen_source values that mean "settled — do not reconsider this entry".
# Only "row" licenses age_phrase to state a raise date; everything else renders
# as a floor. Keeping the two ideas in one place is deliberate: the bug this
# guards against is a future value being added to the skip set and silently
# inheriting the right to make a claim it cannot source.
_SOURCE_FINAL = ("row", "observed_before_row")

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

# History is keyed by a hash of the TITLE, not by the id, so once a derived row
# leaves every queue its id is gone and derived_subject() has nothing to read.
# For rows registered after 2026-08-19 that is harmless — derived_of is already
# persisted on the history entry while the row is present, and survives its
# removal. It is not harmless for rows that vanished BEFORE that: 8 of the VM's
# 17 reported silent drops were spent SLA escalations, unrecognisable because
# the only surviving field was the title. So the title's fixed prefix is a
# constant here too, built into the title by chairman_decision_sla, and read
# back by the one-time backfill below.
SLA_ESCALATION_TITLE_PREFIX = "[SLA] Board must decide"

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


def backfill_derived_from_titles(history: dict) -> int:
    """One-time repair: tag pre-2026-08-19 SLA escalations as views.

    Idempotent and narrow on purpose — it only touches entries that carry no
    derived_of and whose title starts with the SLA escalator's own prefix, and
    it never un-tags anything. Runs on every load rather than as a script,
    because there is no state sync between the hosts and a migration that must
    be remembered separately on each one is a migration that will be run on one.
    """
    fixed = 0
    for sig, h in history.items():
        if not isinstance(h, dict) or h.get("derived_of"):
            continue
        title = str(h.get("title") or "")
        if not title.startswith(SLA_ESCALATION_TITLE_PREFIX):
            continue
        # "…in 20d): watch-20260727-data-integrity" -> the subject's id.
        subject = title.rsplit("):", 1)[-1].strip() or sig
        h["derived_of"] = subject
        h["status"] = DERIVED_STATUS
        h["last_open"] = None
        h["backfilled_derived"] = True
        fixed += 1
    return fixed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(s: str):
    """Parse an ISO timestamp to an aware datetime, or None.

    The queues carry aware UTC (`_now()`); the board's ledgers carry
    `datetime.now().isoformat()`, which is naive. Comparing those two as
    STRINGS puts a 09:12 local stamp before a 12:55Z one on the same day, so a
    real decision reads as "before this occurrence" and the item is filed as a
    silent drop. Naive values are read as UTC, which is what both hosts run.
    """
    try:
        d = datetime.fromisoformat(str(s))
    except Exception:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


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
def raised_at(item: dict) -> Optional[str]:
    """When the ROW says it was raised, normalised to a tz-aware ISO string.

    None when the row does not say — which is a real answer and callers must
    branch on it, rather than substituting the observation time and reporting a
    guess as a fact. Coverage measured on the VM 2026-08-21: 148/148 impl-queue
    rows, 53/145 Chairman rows, 6/57 pending proposals.
    """
    if not isinstance(item, dict):
        return None
    for field in RAISED_AT_FIELDS:
        raw = item.get(field)
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            # These queues write naive local stamps (queued_at is naive on all
            # 148 impl rows). Treating naive as UTC is the same convention _now()
            # uses, so the two are comparable; guessing a local zone here would
            # reintroduce the ET-vs-UTC split that already bit this firm.
            t = t.replace(tzinfo=timezone.utc)
        return t.isoformat()
    return None


def _register(snap: dict, sig: str, kind: str, title: str, st: str,
              derived_of: str = None, raised: Optional[str] = None) -> None:
    cur = snap.setdefault(sig, {
        "sig": sig, "kinds": set(), "titles": set(),
        "open": False, "resolved": False, "wont_fix": False,
        "title": title, "derived_of": None, "raised": None,
    })
    # Earliest wins: one signature can be carried by rows in several queues, and
    # the problem is as old as the OLDEST of them, not as old as whichever the
    # scan happened to read last.
    if raised and (cur.get("raised") is None or raised < cur["raised"]):
        cur["raised"] = raised
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


# The queues a problem can be OPEN in. One definition, read by the scan, by the
# in-place SLA stamp and by the retirement pass — it used to be spelled out
# again inside _scan(), so a fourth queue added here would have been invisible
# to the scan and every row in it reported as vanished.
#
# A FUNCTION, not a module-level tuple. As a tuple it captured the three Path
# objects at IMPORT time, so rebinding PENDING_PATH/IMPL_QUEUE_PATH/CHAIRMAN_PATH
# — which is how every test in this suite sandboxes itself — left this copy
# pointing at data/. The tests therefore read the firm's real queues and
# flag_stale_in_place STAMPED sla_breached onto real rows, on a host that is not
# even the designated writer. A frozen copy of configuration is the same defect
# as a frozen copy of state: whoever rebinds the source is not the only reader,
# and the copy never finds out.
def scanned_queues():
    return ((PENDING_PATH, "proposal"),
            (IMPL_QUEUE_PATH, "impl"),
            (CHAIRMAN_PATH, "chairman"))

# Where a decision is RECORDED once the item leaves the queues above. These are
# append-only ledgers, not queues: they must never be scanned as if their
# contents were open, or every proposal ever filed would look permanently
# present and the drop detector would go blind. They are consulted at exactly
# one moment — when a signature disappears — to answer "was this decided, or
# lost?". Measured on the VM 2026-08-19: of 17 signatures reported as
# SILENT_DROP, 8 had been approved by the board and were sitting in the first
# of these files the whole time.
OUTCOME_LEDGERS = (
    (DATA / "cit_approved_proposals.json", "RESOLVED", "approved_at",
     "the board approved it"),
    (DATA / "cit_rejected_proposals.json", "WONT_FIX", "rejected_at",
     "the board rejected it"),
)


def _scan() -> dict:
    snap: dict = {}
    for path, kind in scanned_queues():
        for item in _load(path, []):
            if not isinstance(item, dict):
                continue
            st = _status_of(item)
            title = item.get("title") or item.get("description") or "(untitled)"
            subj = derived_subject(item)
            _register(snap, signature(item, kind), kind, title, st, subj,
                      raised_at(item))

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
def backfill_raised_from_rows(snap: dict, history: dict) -> int:
    """Correct first_seen on entries recorded before the clock was fixed.

    Returns how many entries moved. Without this the fix only helps problems
    raised from today on, and the 308 signatures already in the VM's history —
    the ones actually in front of the Chairman — keep reporting an age that is a
    median 49.8 days short.

    Idempotent three ways: it only ever moves first_seen EARLIER, it stamps a
    terminal first_seen_source so a corrected entry is never reconsidered, and
    the row timestamps it reads are write-once (see RAISED_AT_FIELDS).

    last_open is pulled back only when the entry has never been observed
    resolving or recurring. If it has, some later moment is the real start of
    the current open spell and the raise date is not it — moving it would age
    a freshly-reopened problem by however long its first incarnation ran.
    """
    moved = 0
    for sig, cur in snap.items():
        raised = cur.get("raised")
        h = history.get(sig)
        if not raised or h is None or h.get("first_seen_source") in _SOURCE_FINAL:
            continue
        prev = h.get("first_seen")
        if prev and raised >= prev:
            # We saw this signature BEFORE the row in front of us was written —
            # it recurred, and this row is the later incarnation. first_seen is
            # already the earlier, correct value, so there is nothing to move.
            #
            # Do NOT stamp "row" here. "row" is what makes age_phrase print
            # `raised <date>` as a fact, and the date it would print came from
            # our own observation, not from any row — the row says something
            # later. Stamping it would turn "at least this old" into a specific
            # raise claim we cannot source, which is the exact substitution this
            # whole fix exists to remove. Measured on the VM 2026-08-21: 18 of
            # the 69 entries this loop touches land here, so it is not a corner
            # case. A distinct terminal value stops the rescan without lying.
            h["first_seen_source"] = "observed_before_row"
            continue
        h["first_seen"] = raised
        h["first_seen_source"] = "row"
        if (h.get("last_open") and h["last_open"] > raised
                and not h.get("recurred_after_resolution")
                and not h.get("times_resolved")):
            h["last_open"] = raised
        moved += 1
    return moved


def _update_history(snap: dict, history: dict, now: str) -> dict:
    for sig, cur in snap.items():
        h = history.get(sig)
        if h is None:
            # The staleness clock starts when the problem was RAISED, not when
            # this sentinel first happened to look. Those differed by a median
            # of 49.8 days on the VM (2026-08-21): the impl queue's 148 rows
            # were all queued 2026-06-10 and the history only began 2026-07-29,
            # so every Chairman escalation reported a 72-day-old problem as 22
            # days old — a 3.2x understatement, in the understating direction.
            # Same shape as the 48h staleness guard that was priced in hours
            # against a hazard measured in deploys: the threshold was fine, the
            # quantity underneath it was not.
            raised = cur.get("raised")
            h = {
                "sig": sig, "first_seen": raised or now, "last_seen": now,
                "first_seen_source": "row" if raised else "observed",
                "title": cur["title"], "kinds": sorted(cur["kinds"]),
                "occurrences": 1, "times_resolved": 0,
                "recurred_after_resolution": 0,
                "status": "OPEN" if cur["open"] else ("RESOLVED" if cur["resolved"] else "WONT_FIX"),
                "present": True,
                "last_open": (raised or now) if cur["open"] else None,
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
    outcomes = _outcome_index()
    missing_ledgers = absent_decision_ledgers()
    for sig, h in history.items():
        if sig in scanned:
            continue
        h["present"] = False
        # Vanished while still open. Before calling that a silent drop, ask the
        # outcome ledgers whether a decision was recorded — a proposal the board
        # voted on leaves the queue by design, and treating that as a loss
        # buries the real losses in noise.
        #
        # SILENT_DROP is re-examined, not just OPEN. A drop filed before this
        # check existed (or before the board kept a rejection ledger) would
        # otherwise stay wrong forever: the verdict was reached once, on the one
        # run where the row happened to disappear, and nothing ever revisited
        # it. A conclusion drawn from evidence that has since arrived is not
        # history, it is a stale answer.
        if h.get("status") not in ("OPEN", "SILENT_DROP"):
            continue
        verdict = _decision_for(sig, h, outcomes)
        if verdict is None:
            h["status"] = "SILENT_DROP"
            # Not a decision, but not "no record anywhere" either. Kept on the
            # row so the Chairman escalation can say which of the two it is.
            h["undated_ledger_hit"] = _undated_hit(sig, outcomes)
            # Which ledgers this verdict could not consult at all. Stamped on
            # the row, not just reported in aggregate, because the row is what
            # the Chairman reads weeks later — an aggregate in a report nobody
            # rereads is not a caveat attached to the claim.
            if missing_ledgers:
                h["ledgers_absent"] = missing_ledgers
            else:
                h.pop("ledgers_absent", None)
            continue
        h.pop("undated_ledger_hit", None)
        h.pop("ledgers_absent", None)
        status, when, why = verdict
        was_drop = h.get("status") == "SILENT_DROP"
        h["status"] = status
        h["last_open"] = None
        h["decided_at"] = when
        h["decision"] = why + (" (found on re-check; it was filed as a silent "
                               "drop before the ledger was read)" if was_drop else "")
        if status == "RESOLVED":
            h["times_resolved"] = h.get("times_resolved", 0) + 1
    return history


def _host_label() -> str:
    """Which host this run is speaking for. Persisted identity via single_writer,
    never derived from a hostname here — see memory two_brains_one_account."""
    try:
        import single_writer
        return single_writer.this_host() or "this host"
    except Exception:                              # pragma: no cover - defensive
        return "this host"


def absent_decision_ledgers() -> list:
    """Which OUTCOME_LEDGERS files do not exist on THIS host.

    SILENT_DROP means "it left every queue and no decision was recorded". That
    verdict is only as good as the ledgers it consulted, and `_load` returns []
    for a missing file exactly as it does for an empty one — so a ledger that
    was never created reads as a ledger that recorded nothing, and every item
    the board decided through that route escalates to the Chairman as a loss.

    Measured 2026-08-21: `cit_rejected_proposals.json` exists on the Mac (2
    signatures) and does NOT exist on the VM — and the VM is the designated
    lifecycle writer, so the WONT_FIX half of the verdict is unreachable on the
    only host that reaches a verdict. It costs nothing *today*: all 26 of the
    VM's current SILENT_DROPs were checked against the Mac's ledgers and none
    had been decided anywhere. It is recorded rather than "fixed" because the
    obvious fix is wrong — the ledgers are per-host with no sync, and the VM's
    approved ledger is AHEAD of the Mac's (92 signatures vs 87), so shipping the
    Mac's copy over would destroy VM records. What the verdict needs is not the
    other host's file, it is to stop claiming certainty it does not have.
    """
    return [path.name for path, _s, _t, _w in OUTCOME_LEDGERS
            if not path.exists()]


def _outcome_index() -> dict:
    """signature -> list of (status, decided_at, why), newest last.

    Read once per run; the approved ledger alone was 1688 rows on 2026-08-19.
    """
    idx: dict = {}
    for path, status, ts_field, why in OUTCOME_LEDGERS:
        for row in _load(path, []):
            if not isinstance(row, dict):
                continue
            idx.setdefault(signature(row, "proposal"), []).append(
                (status, str(row.get(ts_field) or row.get("ts") or ""), why))
    return idx


def _decision_for(sig: str, h: dict, outcomes: dict):
    """The decision that closed THIS occurrence, or None.

    A decision only closes the occurrence it postdates. The ledgers are
    permanent, so a proposal approved in June is still in there when the same
    problem is re-filed in August — accepting that entry would mark the new
    occurrence resolved by an old vote and hide a genuine drop. This is the
    same discipline as the Kalshi sample rule: a verdict from one regime does
    not settle a question asked in another.

    An UNDATED ledger row is therefore not evidence at all. 727 of the 1688
    rows in the approved ledger on 2026-08-19 carry neither `approved_at` nor
    `ts`, and the first cut of this function accepted them: with no date to
    compare against, the "must postdate the occurrence" guard had nothing to
    reject, so a missing value read as YES. That is presence-is-not-currency —
    asking whether a decision exists *ever* instead of whether one closed *this*
    occurrence — and it is the exact hole the anchor was added to plug. Undated
    rows are skipped; `_undated_hit` lets the caller say so rather than report a
    clean loss.
    """
    anchor = _dt(h.get("last_open") or h.get("first_seen") or "")
    best = None
    for status, when, why in outcomes.get(sig, []):
        wdt = _dt(when)
        if wdt is None:
            continue        # undated: cannot be shown to postdate anything
        if anchor and wdt < anchor:
            continue        # decided before this occurrence started
        # Newest wins. File order is append order across two ledgers, so the
        # first qualifying row is not necessarily the decision that actually
        # ended this occurrence.
        if best is None or wdt > best[0]:
            best = (wdt, status, when, why)
    return None if best is None else (best[1], best[2], best[3])


def _undated_hit(sig: str, outcomes: dict) -> bool:
    """True if the only ledger evidence for `sig` is a row with no timestamp.

    Reported, never acted on: the row still needs a human, but calling it a
    silent drop with "NO record anywhere" would be a lie the Chairman can read
    in his queue.
    """
    return bool(outcomes.get(sig))


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


def age_phrase(h: dict, now: str) -> str:
    """"open 72d (raised 2026-06-10)" — or, when the row never said when it was
    raised, "open >= 22d (first observed 2026-07-29; the row carries no raise
    date, so this is a floor)".

    Two different facts used to render as one sentence. Before 2026-08-21 every
    escalation said "first seen <date>" and meant "when this sentinel started
    keeping notes", which on the VM was a median 49.8 days after the problem was
    actually raised. The Chairman was reading 22d for a 72d-old problem. Where
    the raise date is genuinely unknown the number is now marked as the floor it
    is, rather than quietly presented as the age.
    """
    started = h.get("last_open") or h.get("first_seen")
    days = int(_days_between(started, now) or 0)
    src = h.get("first_seen_source")
    seen = str(h.get("first_seen", "?"))[:10]
    if src == "row":
        return f"open {days}d (raised {seen})"
    if src == "observed_before_row":
        # We hold a raise date, but it is LATER than our own first sighting —
        # this signature recurred. The floor is still the honest number; the
        # reason it is a floor is different, and saying the wrong reason would
        # send someone looking for a missing field that is not missing.
        return (f"open >= {days}d (first observed {seen}; the row in the queue "
                f"was raised later, so this problem recurred and this is a "
                f"floor, not the age)")
    return (f"open >= {days}d (first observed {seen}; the row carries no raise "
            f"date, so this is a floor, not the age)")


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
    for path, kind in scanned_queues():
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
                    f"OPEN with no resolution past the {STALE_SLA_DAYS}d SLA — "
                    f"{age_phrase(h, now)}. It needs a decision — resolve, "
                    f"reassign, or explicitly close.")
                touched = True
            stamped += 1
        if touched:
            _save(path, items)
    return stamped


def _escalate_chairman(h: dict, cls: str, now: str) -> bool:
    sig = h["sig"]
    if cls == "STALE":
        desc = (f"'{h.get('title')}' has been OPEN with no resolution past the "
                f"{STALE_SLA_DAYS}d SLA — {age_phrase(h, now)}. "
                f"It needs a decision — resolve, reassign, or explicitly close. "
                f"(No queue row carries this signature, so the breach could not "
                f"be stamped on the problem itself.)")
        eid = f"lifecycle-stale-{sig}"
    else:  # SILENT_DROP
        # Two different facts wear the same status, and the Chairman reads this
        # sentence in his queue. "No record anywhere" is the alarm; "a decision
        # exists but is undated" is a ledger defect wearing the alarm's clothes,
        # and telling him the first when the second is true is how a detector
        # earns the right to be ignored.
        if h.get("undated_ledger_hit"):
            desc = (f"'{h.get('title')}' disappeared from every queue. A board "
                    f"decision for it IS in the outcome ledger, but that row "
                    f"carries no timestamp, so it cannot be matched to this "
                    f"occurrence — it may be an older vote on the same problem. "
                    f"Confirm the outcome, and note the ledger row needs a date.")
        else:
            desc = (f"'{h.get('title')}' disappeared from every queue with NO recorded "
                    f"resolution. Verify it was actually fixed, not silently dropped.")
        # "No decision was recorded" and "one of the places a decision gets
        # recorded does not exist on this host" are different claims, and only
        # the first one is an alarm. Scoped conservatism: the verdict still
        # escalates — an unverifiable drop is not a safe drop — but it says what
        # it could not check, so nobody spends an hour hunting a decision that
        # was never lost.
        absent = h.get("ledgers_absent")
        if absent:
            desc += (f" NOTE: this host has no {', '.join(absent)}, so that "
                     f"decision route could not be checked at all — 'no record' "
                     f"here means 'no record I can see from {_host_label()}'.")
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
    for path, kind in scanned_queues():
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
        if not settled and str(row.get("id", "")).startswith(DROP_ID_PREFIX):
            # A drop row asserts one thing: this problem vanished with no record
            # of a decision. That assertion can stop being true two ways, and
            # NEITHER of them is a resolution, so the RESOLVED/WONT_FIX test
            # above can never retire the row.
            #
            # Measured on the VM 2026-08-19, after the ledger fix cut the drop
            # count from 17 to 1: 47 drop rows were still open in the Chairman's
            # queue. Twenty-one pointed at a signature that is DERIVED — a view
            # of another row, which is never OPEN, never ages and never
            # escalates, so the row asked for a decision about nothing.
            # Twenty-four pointed at a signature that had simply COME BACK and
            # was sitting in a queue, open, at that moment. One was the real
            # loss. The history had corrected itself on every run since the fix;
            # the queue had not, because nothing retracts an escalation whose
            # premise expired. A verdict reached once and never revisited is the
            # same defect on the queue side that SILENT_DROP had on the history
            # side.
            h = history.get(subject)
            if h is not None and h.get("status") == DERIVED_STATUS:
                settled = True
                why = ("%s is a derived view of another row, not a problem in "
                       "its own right — it cannot be silently dropped because "
                       "it was never independently open" % subject)
            elif h is not None and h.get("present"):
                settled = True
                why = ("%s is back in a queue and open again, so it was not "
                       "lost. If it disappears again the next scan files a "
                       "fresh row with the new dates" % subject)
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
    backfilled = backfill_derived_from_titles(history)

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
    rebased = backfill_raised_from_rows(snap, history)
    history = _update_history(snap, history, now)

    # LOOPING / STALE / SILENT_DROP are all statements about a problem's history
    # ACROSS runs. A run that will not persist history is not a run that found
    # none of them — it is a run that could not look: the next run reloads an
    # empty file, every signature is first-seen again, and all three lists are
    # unreachable BY CONSTRUCTION. A control that can never be red must say so,
    # or its permanent zeros get read as a clean bill of health. Measured
    # 2026-08-21: the red team scored the loop detector CRITICAL-BYPASSED off
    # exactly these zeros, on a host where they could not have been anything
    # else, while the designated writer held 4 LOOPING and 86 STALE.
    report = {"ts": now, "tracked": len(history),
              "looping": [], "stale": [], "silent_drop": [], "derived": [],
              "retired_derived": retired,
              "escalated_board": 0, "escalated_chairman": 0,
              "flagged_in_place": 0, "backfilled_derived": backfilled,
              "rebased_to_raise_date": rebased,
              "decision_ledgers_absent": absent_decision_ledgers(),
              "decided_not_dropped": [],
              "evaluated": may_write,
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
        elif h.get("decided_at"):
            # Left the queues, but a vote is on record. Reported so the number
            # is visible rather than inferred from a shrinking drop count.
            report["decided_not_dropped"].append(sig)

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
    elif not rep["evaluated"]:
        # Deliberately printed INSTEAD of the counts, not after them. The old
        # order led with `looping=0 stale=0 silent_drop=0` and put the caveat on
        # the following line, so the first thing any reader — human or grep —
        # took from a blind run was three zeros.
        print(f"[lifecycle] NOT EVALUATED — {rep['writer_note']}")
        print(f"[lifecycle] scanned {rep['tracked']} signature(s), but lifecycle "
              f"class (LOOPING/STALE/SILENT_DROP) needs a history this host does "
              f"not maintain. No counts are reported because none were measured.")
    else:
        print(f"[lifecycle] tracked={rep['tracked']} "
              f"looping={len(rep['looping'])} stale={len(rep['stale'])} "
              f"silent_drop={len(rep['silent_drop'])} "
              f"derived={len(rep['derived'])} "
              f"-> board+{rep['escalated_board']} chairman+{rep['escalated_chairman']} "
              f"retired+{len(rep['retired_derived'])} "
              f"loop_break={len(rep['loop_break_set'])}")
        if rep["decided_not_dropped"] or rep["backfilled_derived"]:
            print(f"[lifecycle] decided-not-dropped="
                  f"{len(rep['decided_not_dropped'])} "
                  f"backfilled_derived={rep['backfilled_derived']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

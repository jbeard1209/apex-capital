#!/usr/bin/env python3
"""
Chairman Queue Sync  (Department: Governance / Logic Team)
==========================================================
WHY THIS EXISTS
---------------
The Chairman's queue was running as TWO firms' queues.

  data/chairman_action_required.json   Mac 188 items · VM  54 · 2 shared ids
  data/chairman_decision_queue.json    Mac 139 items · VM  55 · 0 shared ids

Neither file is in deploy.sh's `data/` allowlist, so once the VM started writing
its own copies (2026-07-10) the two hosts diverged permanently and silently.
Three concrete failures came out of that:

  1. chairman_decision_sla.py runs ONLY on the VM. Everything the Mac queued
     escalated to nobody — 139 items with no SLA clock at all.
  2. The Mac's app inbox is what the SessionStart hook prints. It showed Jack
     10 items while the VM was independently tracking 55 he had never seen.
  3. Worst: the Mac held 129 *resolved* decisions the VM knew nothing about.
     The VM's SLA agent was on course to escalate already-decided items to a
     board vote — the governance backstop generating governance noise.

WHAT THIS DOES
--------------
Union both hosts' copies of both files, then collapse near-duplicates:

  • Identity is `dedup_key` when a producer supplied one, else the title /
    description with digits collapsed to `#`. That last part matters: the two
    queues held 242 raw items but only 107 distinct complaints, because three
    variants of one CRO grievance ("CRO at HIGH for 41 days…") were re-appended
    every cycle with a bumped counter, 124 rows in total.
  • MOST-ADVANCED STATUS WINS. A decision recorded on either host is a decision;
    it must never be downgraded back to "awaiting" by a merge.
  • EARLIEST TIMESTAMP WINS. An item's SLA clock is how long the Chairman has
    actually had it. Merging must not hand anything a fresh 20 days.
  • NOTHING IS LOST. Every id folded into a group is kept in `merged_ids`, and
    every pre-merge file is archived before a byte is written.

DIRECTION
---------
Runs on the Mac only: the Mac can ssh to the VM, the VM cannot ssh back. It is
a reconciler, not a push — it merges both directions and writes the same merged
result to both hosts, so it is safe to run from either host's data being newer.
If the Mac is off for a week the VM keeps queueing locally and the next run
folds it in; nothing is lost, it is just late.

THE ARCHIVE IS A TOMBSTONE, NOT A COPY  (2026-08-19)
----------------------------------------------------
chairman_queue_compactor.py archived 12 closed rows out of the VM's queue. This
file put all 12 straight back, three minutes later, because a union of two hosts
resurrects anything removed from only one of them. The rows then existed twice —
once in the queue, once in the archive — which is strictly worse than never
having compacted. So the union now reads `chairman_resolved_archive.json` from
both hosts and suppresses any row whose identity is archived AND whose own
status on that host is closed.

The "AND its own status is closed" half is the safety. Tombstoning on identity
alone would silence a genuine recurrence forever: a problem archived in July
that a producer re-files as open in September would be deleted on arrival, and
because problem_lifecycle_sentinel only calls a vanishing row a SILENT_DROP when
its history says OPEN, nothing downstream would notice. Filtering each host's
own record before the merge — never the merged result, whose status lattice puts
"resolved" above "open" — means one host holding it open is enough to keep it,
and it comes back flagged `reopened_after_archive`.

FOLDING IS NOT DROPPING  (2026-08-19) — latent, measured at zero
----------------------------------------------------------------
This file and problem_lifecycle_sentinel.py de-duplicate the same rows on two
DIFFERENT notions of identity: `group_key` here (digit-collapsed title PLUS
description, 110 chars), `signature()` there (hashed normalized title OR
description, 80 chars). Where those disagree, a fold deletes the loser's text
from every queue and the sentinel — which only ever sees text — reads it as a
problem that vanished with no recorded resolution: a SILENT_DROP, plus a
`lifecycle-drop-*` row to the Chairman about a problem nobody dropped.

They disagree whenever two producers split the same sentence differently
between the two fields: title "Alpha" + description "Beta gamma" and title
"Alpha Beta" + description "gamma" share a group_key and do not share a
signature.

Be clear about the evidence: across the last six sync runs, the number of
signatures that existed pre-merge and not post-merge is ZERO. Nothing has
escaped through this — the 2026-08-19 SILENT_DROP that prompted the audit was a
coherence-law producer withdrawing its own row, not a fold. This is a reachable
gap being closed while it is cheap, not a postmortem. A fold records
`merged_titles`, the sentinel counts those as present, and folds recorded before
today are repaired on read from the pre-merge snapshots this file already wrote.

USAGE
    python3 chairman_queue_sync.py --report     # dry run, prints the plan
    python3 chairman_queue_sync.py              # merge + write both hosts
    python3 chairman_queue_sync.py --local-only # merge into the Mac only
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import problem_lifecycle_sentinel as PLS

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

VM_HOST = "apex-capital"
VM_BASE = "~/institutional-tracker"

ACTION_FILE = "chairman_action_required.json"
QUEUE_FILE = "chairman_decision_queue.json"
ARCHIVE_FILE = "chairman_resolved_archive.json"

# What counts as "closed" is the sentinel's definition, imported, never a second
# copy — chairman_queue_compactor.py holds the same import for the same reason.
CLOSED_STATUSES = PLS.RESOLVED_STATUSES | PLS.WONT_FIX_STATUSES

# Status lattice. Merging may only move an item UP these ladders, never down.
# "known_infrastructure" outranks "awaiting_chairman" because it is a triage
# outcome — someone looked and decided it wasn't a decision to make.
#
# "needs_owner" outranks "known_infrastructure" because it is the CORRECTION of
# a triage that was wrong: stale-file findings were parked as self-resolving on
# the premise that the Oracle 24/7 host would fix them, and that host was
# terminated 2026-07-27. Ranking the correction below the mistake would let the
# mistake win every merge and the rows would stay parked forever. Note that
# _collapse() does rank.get(status, 0) — an unregistered status silently sorts
# LAST-advanced and loses every merge, so a new status that is not listed here
# is a status that does nothing.
ACTION_RANK = {"open": 0, "awaiting_chairman": 1, "known_infrastructure": 2,
               "needs_owner": 3, "resolved": 4}
QUEUE_RANK = {"awaiting_chairman": 0, "escalated_to_board": 1, "decided": 2,
              "resolved": 3}

# Fields where the EARLIEST value is the true one (they anchor SLA clocks).
ACTION_TS_FIELDS = ("ts", "queued_at", "first_seen_of_group")
QUEUE_TS_FIELDS = ("first_surfaced", "upstream_first_seen")

SLA_DAYS = 20  # must track chairman_decision_sla.SLA_DAYS

_EMPTY = (None, "", [], {})


# ──────────────────────────────────────────────────────────────────────────
#  identity
# ──────────────────────────────────────────────────────────────────────────
def group_key(item: dict) -> str:
    """The identity a Chairman item is deduped on.

    A producer-supplied `dedup_key` is authoritative. Absent one we fall back to
    the text with digits collapsed, because the duplicate rows in this queue
    differ only in a counter: "CRO at HIGH for 41 days" and "…for 42 days" are
    one grievance, not two decisions.
    """
    dk = item.get("dedup_key")
    if dk:
        return f"dk:{str(dk).strip().lower()}"
    text = (item.get("title") or "") + " " + (item.get("description") or "")
    text = re.sub(r"\d+", "#", text.lower())
    text = re.sub(r"[^a-z#]+", " ", text).strip()
    return "tx:" + text[:110]


def _text_of(item: dict) -> str:
    """The text the sentinel would key on. Same fallback order as
    problem_lifecycle_sentinel.signature(): title, else description."""
    return str(item.get("title") or item.get("description") or "")[:200]


def _richness(item: dict) -> int:
    return sum(1 for v in item.values() if v not in _EMPTY)


def _min_ts(values):
    vals = [v for v in values if v]
    return min(vals) if vals else None


#  A queue item is surfaced at most a few times a day and the oldest open item
#  dates to June 2026, so a genuine `times_seen` cannot plausibly reach four
#  figures. Anything above this is 2^n wreckage from the sum() merge, not a
#  count of anything.
TIMES_SEEN_CEILING = 1000


def sanitize_times_seen(item: dict) -> dict:
    """Floor an impossible recurrence counter, in place, and say so.

    Switching the merge from sum() to max() stopped the doubling but could not
    undo it: `max` PRESERVES the poisoned value forever, so 107 records in
    chairman_action_required.json and 108 in chairman_decision_queue.json still
    carried 69-and-70-digit counters weeks after the merge was fixed. One of
    them already had a note reading "floored to 1" beside a value that had
    never been floored — the remediation had written its own postmortem and not
    run. That is the shape to avoid here, hence a sanitiser that executes on
    every load rather than a migration script someone has to remember.

    Self-healing on read is also the only design that works across this firm's
    two hosts: they share no state and no migration can run "everywhere" from
    one machine. Each host repairs its own copy the next time it syncs.

    The original is unrecoverable — 2^n counts merge runs, not sightings — so it
    is floored to 1 and the digit count is kept as evidence rather than the
    number itself, matching the `times_seen_lost` convention already in the data.
    Idempotent: a floored record has a plain small int and is left alone.
    """
    v = item.get("times_seen")
    if isinstance(v, bool) or not isinstance(v, int):
        return item
    if 0 < v <= TIMES_SEEN_CEILING:
        return item
    if v <= 0:
        item["times_seen"] = 1
        return item
    item["times_seen"] = 1
    item["times_seen_lost"] = (
        f"original value had {len(str(v))} digits (non-idempotent sum() merge, "
        f"fixed 2026-08-06); true count unrecoverable, floored to 1")
    return item


def _collapse(items, rank: dict, ts_fields, label: str) -> list:
    """Fold a list of queue items into one record per distinct complaint."""
    groups: "OrderedDict[str, list]" = OrderedDict()
    for it in items:
        if isinstance(it, dict):
            # Before grouping, so a poisoned record cannot win the max() below
            # and re-infect the merged output.
            groups.setdefault(group_key(it), []).append(sanitize_times_seen(it))

    merged_out = []
    for grp in groups.values():
        # Least-advanced first, so the most-advanced record's fields land last
        # and win every conflict.
        ordered = sorted(grp, key=lambda x: (rank.get(x.get("status"), 0),
                                             _richness(x)))
        merged: dict = {}
        for x in ordered:
            for k, v in x.items():
                if v not in _EMPTY:
                    merged[k] = v

        winner = ordered[-1]
        merged["status"] = winner.get("status")
        merged["id"] = winner.get("id") or next(
            (x.get("id") for x in ordered if x.get("id")), None)

        for f in ts_fields:
            v = _min_ts(x.get(f) for x in grp)
            if v:
                merged[f] = v

        ids = [x.get("id") for x in grp if x.get("id")]
        folded = sorted({i for i in ids if i != merged["id"]})
        if folded:
            merged["merged_ids"] = folded

        # The TEXT of every folded row, not just its id. problem_lifecycle_
        # sentinel derives a problem's identity from its title, so an id it has
        # never seen tells it nothing: without this the loser of every fold reads
        # to the sentinel as a problem that left the queue unresolved. Accumulate
        # rather than overwrite — a group can already contain a previous run's
        # merged record, and this must stay idempotent for the same reason
        # times_seen uses max() below.
        kept_text = _text_of(merged)
        folded_titles = []
        for x in grp:
            for t in [_text_of(x)] + list(x.get("merged_titles") or []):
                t = str(t)[:200] if t else ""
                if t and t != kept_text and t not in folded_titles:
                    folded_titles.append(t)
        if folded_titles:
            merged["merged_titles"] = folded_titles

        if len(grp) > 1:
            # MAX, not SUM. This merge is not a one-shot: the merged record is
            # written back to BOTH sides, so the next run folds a group that
            # already contains its own previous output. Summing two copies of
            # one lineage doubles the counter every single run — 2^n — and the
            # deadman entry reached 6.47e68 (≈2^228) before anyone read it.
            #
            # The property that matters is idempotence:
            #     merge(merge(a,b), merge(a,b)) == merge(a,b)
            # `max` has it, `sum` cannot. It does under-count when two hosts
            # genuinely observed the same complaint independently (3 + 5 reads
            # as 5, not 8), and that trade is deliberate: `times_seen` is a
            # recurrence hint, not an accounting figure, and a stable
            # under-count is worth incomparably more than an unbounded one.
            # Guarded by TestChairmanSyncMergeIsIdempotent.
            merged["times_seen"] = max(
                (max(1, int(x.get("times_seen") or 1)) for x in grp), default=1)
            merged["merge_note"] = (
                f"{len(grp)} {label} record(s) folded by chairman_queue_sync "
                f"(key={group_key(grp[0])[:60]})")
        merged_out.append(merged)
    return merged_out


# ──────────────────────────────────────────────────────────────────────────
#  merges (pure — these are what the tests drive)
# ──────────────────────────────────────────────────────────────────────────
def merge_actions(local: list, remote: list) -> list:
    return _collapse(list(local) + list(remote), ACTION_RANK,
                     ACTION_TS_FIELDS, "action")


def unbacked_suppressions(suppressed: list, arch_ids: set, arch_keys: set) -> list:
    """Suppressed rows whose subject the archive is NOT holding. Empty is good.

    A suppressed row is only allowed to leave the queue because the archive is
    holding it; if it is not, we are deleting evidence, which is the failure
    `chairman_queue_compactor` exists to avoid.

    THE DEADLOCK THIS FIXES
    -----------------------
    This test used to be `id not in {ids in the archive}` — by ID ONLY — while
    `apply_tombstones` admits a row for suppression on `id in arch_ids OR
    group_key in arch_keys`. Two notions of identity for one decision. A row
    suppressed through the group_key path can never satisfy an id-based check,
    so the RuntimeError was PERMANENT rather than transient: measured on the
    Mac's queue 2026-08-27, of 50 lifecycle-drop-sig rows 43 matched by id and
    5 matched by group_key alone, and those 5 deadlocked the reconcile for 8.6
    days. `chairman_inbox` catches this into "could not reach the VM", so the
    only symptom Jack saw named a cause that was not the cause — a network blip
    self-heals and needs nothing from him; this never self-heals.

    Matching on the group_key is genuinely backed, not a loosening: group_key is
    a CONTENT key, so an archived sibling means the archive holds a decision
    about the same subject and the row leaving is a duplicate detection of an
    already-filed problem. Nothing about the subject is lost.

    THE RELATION IS RIGHT ONLY IF THE SOURCE IS DIFFERENT
    -----------------------------------------------------
    Widening the relation is half a fix and the dangerous half. Read against
    `apply_tombstones`, this predicate is the exact negation of that function's
    admission rule — so if it is handed the SAME sets that admitted the rows, it
    cannot fire, by construction. That is what the first cut of this fix did on
    2026-08-27: it swapped an over-strict guard for a vacuous one, deleting a
    check that had been comparing the suppression against the archive RECORDS
    while only the identity relation was wrong. The pre-push gate caught it on
    an existing poison pill; a guard that cannot fail reads as coverage and is
    worth less than no guard.

    So the caller must source these sets from `archive_backing(m_arch)` — a
    second, independent read of the archive — never from `tombstones()`. Then
    the check is a genuine two-source comparison: it fires when the compactor's
    idea of what is filed and the archive's actual contents disagree, which is
    real evidence loss, while a row legitimately backed by an archived sibling's
    group_key passes and the 8.6-day deadlock stays fixed.
    """
    out = set()
    for x in suppressed:
        if not isinstance(x, dict) or not x.get("id"):
            continue
        if x["id"] in arch_ids or group_key(x) in arch_keys:
            continue
        out.add(x["id"])
    return sorted(out)


def tombstones(archived: list):
    """(ids, group_keys) that chairman_queue_compactor has already filed away."""
    ids, keys = set(), set()
    for r in archived or []:
        if not isinstance(r, dict):
            continue
        if r.get("id"):
            ids.add(r["id"])
        keys.add(group_key(r))
    return ids, keys


def archive_backing(archived: list):
    """(ids, group_keys) the archive is HOLDING — the verification's own source.

    Deliberately a second, independent read of the same records `tombstones()`
    reads. It must stay that way: `unbacked_suppressions` is only a real check
    because the sets it validates against come from somewhere other than the
    sets that made the admission decision. Hand it `tombstones()`' output and it
    becomes the exact negation of `apply_tombstones`' admission rule — a guard
    that cannot fire, which reads as coverage and is worth less than no guard.
    That collapse was written and caught by the gate on 2026-08-28; the AST test
    `test_the_check_is_not_handed_the_sets_that_made_the_decision` pins it.

    What the pair actually catches is drift BETWEEN the two: `tombstones()`
    naming a subject the archive does not contain means the compactor and the
    reconciler disagree about what has been filed, and suppressing on that
    deletes evidence. Same shape as every coherence law — two independently
    sourced measurements of one quantity, alarm when the arithmetic disagrees.
    """
    ids, keys = set(), set()
    for r in archived or []:
        if not isinstance(r, dict):
            continue
        if r.get("id"):
            ids.add(r["id"])
        keys.add(group_key(r))
    return ids, keys


def apply_tombstones(items: list, arch_ids: set, arch_keys: set):
    """(kept, suppressed, reopened_ids) for ONE host's own rows.

    Deliberately applied per-host and BEFORE the merge. The merge's status
    lattice ranks "resolved" above "open", so a row that one host has re-opened
    would read as closed in the merged record and be tombstoned away — the
    recurrence would be deleted by the machinery meant to tidy up after it.
    Judged on each host's own copy, one host holding it open is enough to keep
    it, and it returns flagged rather than silently.

    The "still open here" veto applies to PROBLEMS, not to views of problems.
    A derived row (`lifecycle-stale-*`, `sla-escalation-*`, …) has no lifecycle
    of its own — it cannot recur, because it is not a thing that happens, it is
    a thing that gets written about a thing that happens. It stayed open on the
    Mac for one reason only: the two hosts keep independent lifecycle histories,
    the Mac's was discontinuous, so the two disagreed about which subjects had
    gone stale. Vetoing on that produced 17 rows that were archived on one host
    and permanently un-archivable on the other. The subject keeps its own row
    and its own veto; the view follows the archive.
    """
    kept, suppressed, reopened = [], [], []
    for it in items:
        if not isinstance(it, dict):
            kept.append(it)
            continue
        if it.get("id") not in arch_ids and group_key(it) not in arch_keys:
            kept.append(it)
            continue
        if str(it.get("status") or "").lower() in CLOSED_STATUSES:
            suppressed.append(it)
            continue
        if PLS.derived_subject(it):
            suppressed.append(it)
            continue
        it["reopened_after_archive"] = True
        reopened.append(it.get("id"))
        kept.append(it)
    return kept, suppressed, reopened


def merge_archives(local: list, remote: list) -> list:
    """Union both hosts' archives, one row per id, earliest archival kept.

    The archive has to be the same on both hosts or the tombstone only holds on
    whichever host ran the compactor, which is how the 12 rows came back.
    """
    out: "OrderedDict[str, dict]" = OrderedDict()
    for r in list(local or []) + list(remote or []):
        if not isinstance(r, dict):
            continue
        key = r.get("id") or group_key(r)
        prev = out.get(key)
        if prev is None:
            out[key] = r
        elif str(r.get("archived_on") or "") < str(prev.get("archived_on") or ""):
            out[key] = r
    return list(out.values())


def backfill_merged_titles(items: list, recovered: dict) -> int:
    """Repair folds recorded before merged_titles existed. Returns rows healed.

    Self-healing on read, not a migration script: the two hosts share no state,
    so nothing can be run "everywhere" from one machine — the same reasoning
    sanitize_times_seen() above is built on.
    """
    healed = 0
    for it in items:
        if not isinstance(it, dict) or it.get("merged_titles"):
            continue
        found = [recovered[i] for i in (it.get("merged_ids") or [])
                 if recovered.get(i)]
        if found:
            kept_text = _text_of(it)
            it["merged_titles"] = [t for t in dict.fromkeys(found)
                                   if t != kept_text]
            healed += 1
    return healed


def _recoverable_titles() -> dict:
    """id -> text, read from the newest pre-merge snapshot this file wrote.

    Only the Mac has these snapshots (it is the only host that runs the sync),
    which is enough: the repaired rows are written back to both hosts.
    """
    root = DATA / "chairman_queue_archive"
    if not root.is_dir():
        return {}
    out: dict = {}
    for stamp in sorted((d for d in root.iterdir() if d.is_dir()),
                        key=lambda d: d.name)[-3:]:
        for host in sorted(stamp.iterdir()):
            f = host / ACTION_FILE
            if not f.is_file():
                continue
            try:
                rows = json.loads(f.read_text())
            except Exception:
                continue
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, dict) and r.get("id"):
                    t = _text_of(r)
                    if t:
                        out[r["id"]] = t
    return out


def merge_decisions(local: list, remote: list, now=None) -> list:
    """Merge the app-facing decision inbox.

    After taking the earliest `first_surfaced`, the deadline is RECOMPUTED from
    it. Carrying the old deadline forward would leave the two fields disagreeing
    about the same clock, and the one the escalator reads is the deadline.
    """
    out = _collapse(list(local) + list(remote), QUEUE_RANK,
                    QUEUE_TS_FIELDS, "decision")
    for item in out:
        fs = _parse_ts(item.get("first_surfaced"))
        if fs:
            item["deadline"] = (fs + datetime.timedelta(days=SLA_DAYS)).isoformat()
    return out


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "").split("+")[0])
    except Exception:
        return None


def overdue_after_merge(decisions: list, now=None) -> list:
    """Items whose honest (earliest) clock has already blown the SLA.

    Reported, never silently acted on — escalation stays chairman_decision_sla's
    job. A merge that quietly filed 60 board proposals would be the same class
    of governance noise this file exists to stop.
    """
    now = now or datetime.datetime.utcnow()
    out = []
    for d in decisions:
        if d.get("status") != "awaiting_chairman":
            continue
        dl = _parse_ts(d.get("deadline"))
        if dl and now >= dl:
            out.append(d.get("id"))
    return out


# ──────────────────────────────────────────────────────────────────────────
#  host I/O
# ──────────────────────────────────────────────────────────────────────────
def _load_local(name: str) -> list:
    p = DATA / name
    try:
        v = json.loads(p.read_text())
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _load_remote(name: str, missing_ok: bool = False) -> list:
    r = subprocess.run(["ssh", VM_HOST, f"cat {VM_BASE}/data/{name}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        # missing_ok is for a file a host may legitimately not have yet (the
        # archive before its first compaction). It does NOT cover an unreachable
        # host: an ssh that fails to connect prints to stderr too, and treating
        # that as "the VM has no rows" would let the merge quietly write the
        # Mac's queue over the VM's.
        if missing_ok and "No such file" in (r.stderr or ""):
            return []
        raise RuntimeError(f"cannot read {name} on {VM_HOST}: {r.stderr.strip()[:200]}")
    try:
        v = json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(f"{name} on {VM_HOST} is not valid JSON: {e}")
    return v if isinstance(v, list) else []


def _write_local(name: str, obj) -> None:
    # The tmp name is per-process. A shared "<name>.tmp" let two sessions whose
    # SessionStart hooks fired together race: one's replace() moved the other's
    # tmp away, the loser raised FileNotFoundError on a Mac path, and the inbox
    # reported it as "could not reach the VM" (seen 2026-10-03).
    p = DATA / name
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)


def _write_remote(name: str, obj) -> None:
    payload = json.dumps(obj, indent=2)
    r = subprocess.run(
        ["ssh", VM_HOST, f"cat > {VM_BASE}/data/{name}.tmp && "
                         f"mv {VM_BASE}/data/{name}.tmp {VM_BASE}/data/{name}"],
        input=payload, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"cannot write {name} on {VM_HOST}: {r.stderr.strip()[:200]}")


def _archive(stamp: str, host: str, name: str, obj) -> Path:
    d = DATA / "chairman_queue_archive" / stamp / host
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(obj, indent=2))
    return p


# ──────────────────────────────────────────────────────────────────────────
def sync(write: bool = True, local_only: bool = False) -> dict:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    loc_act, loc_dec = _load_local(ACTION_FILE), _load_local(QUEUE_FILE)
    loc_arch = _load_local(ARCHIVE_FILE)
    if local_only:
        rem_act, rem_dec, rem_arch = [], [], []
    else:
        rem_act, rem_dec = _load_remote(ACTION_FILE), _load_remote(QUEUE_FILE)
        rem_arch = _load_remote(ARCHIVE_FILE, missing_ok=True)

    m_arch = merge_archives(loc_arch, rem_arch)
    arch_ids, arch_keys = tombstones(m_arch)

    # Per-host, pre-merge. See apply_tombstones' docstring for why not after.
    n_in = (len(loc_act), len(rem_act))   # what each host actually held
    loc_act, sup_loc, reo_loc = apply_tombstones(loc_act, arch_ids, arch_keys)
    rem_act, sup_rem, reo_rem = apply_tombstones(rem_act, arch_ids, arch_keys)
    suppressed = sup_loc + sup_rem

    recovered = _recoverable_titles()
    healed = (backfill_merged_titles(loc_act, recovered)
              + backfill_merged_titles(rem_act, recovered))

    m_act = merge_actions(loc_act, rem_act)
    m_dec = merge_decisions(loc_dec, rem_dec)

    rep = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        # mac/vm are what each host HELD, before tombstoning — reporting the
        # post-filter count next to "N kept out" double-subtracts on the page.
        "actions": {"mac": n_in[0], "vm": n_in[1],
                    "merged": len(m_act),
                    "awaiting": sum(1 for a in m_act
                                    if a.get("status") in ("awaiting_chairman", "open"))},
        "decisions": {"mac": len(loc_dec), "vm": len(rem_dec),
                      "merged": len(m_dec),
                      "awaiting": sum(1 for d in m_dec
                                      if d.get("status") == "awaiting_chairman"),
                      "resolved": sum(1 for d in m_dec
                                      if d.get("status") in ("resolved", "decided"))},
        "overdue_after_merge": overdue_after_merge(m_dec),
        "archive": {"mac": len(loc_arch), "vm": len(rem_arch),
                    "merged": len(m_arch)},
        "suppressed": sorted({x.get("id") for x in suppressed if x.get("id")}),
        "reopened_after_archive": sorted({i for i in reo_loc + reo_rem if i}),
        "titles_healed": healed,
        "wrote": False,
    }
    # Nothing may vanish: every input id must survive as an id or inside merged_ids.
    rep["ids_in"] = len({x.get("id") for x in loc_act + rem_act if x.get("id")} |
                        {x.get("id") for x in loc_dec + rem_dec if x.get("id")})
    kept = set()
    for x in m_act + m_dec:
        if x.get("id"):
            kept.add(x["id"])
        kept.update(x.get("merged_ids") or [])
    rep["ids_out"] = len(kept)
    rep["ids_dropped"] = sorted(
        ({x.get("id") for x in loc_act + rem_act + loc_dec + rem_dec if x.get("id")}) - kept)

    if rep["ids_dropped"]:
        raise RuntimeError(f"merge would drop {len(rep['ids_dropped'])} id(s) — refusing to write")

    # NOT arch_ids/arch_keys: those came from tombstones() and are what admitted
    # these rows in the first place. See archive_backing's docstring.
    unbacked = unbacked_suppressions(suppressed, *archive_backing(m_arch))
    if unbacked:
        raise RuntimeError(
            f"{len(unbacked)} row(s) would be suppressed with no archive record "
            f"— refusing to write: {unbacked[:5]}")

    if write:
        _archive(stamp, "mac", ACTION_FILE, loc_act)
        _archive(stamp, "mac", QUEUE_FILE, loc_dec)
        if not local_only:
            _archive(stamp, "vm", ACTION_FILE, rem_act)
            _archive(stamp, "vm", QUEUE_FILE, rem_dec)
        _write_local(ACTION_FILE, m_act)
        _write_local(QUEUE_FILE, m_dec)
        _write_local(ARCHIVE_FILE, m_arch)
        if not local_only:
            _write_remote(ACTION_FILE, m_act)
            _write_remote(QUEUE_FILE, m_dec)
            _write_remote(ARCHIVE_FILE, m_arch)
        rep["wrote"] = True
        rep["snapshot"] = str(DATA / "chairman_queue_archive" / stamp)
    return rep


def main():
    ap = argparse.ArgumentParser(
        description="Reconcile the Chairman's action/decision queues across both hosts.")
    ap.add_argument("--report", action="store_true", help="dry run — merge and print, write nothing")
    ap.add_argument("--local-only", action="store_true", help="skip the VM (offline / no ssh)")
    args = ap.parse_args()

    try:
        rep = sync(write=not args.report, local_only=args.local_only)
    except Exception as e:
        print(f"  [chairman_queue_sync] FAILED: {e}")
        return 1

    a, d = rep["actions"], rep["decisions"]
    print("=" * 66)
    print("  CHAIRMAN QUEUE SYNC" + ("  (dry run)" if args.report else ""))
    print("=" * 66)
    print(f"  action_required : mac {a['mac']:>4} + vm {a['vm']:>4} "
          f"-> {a['merged']:>4} merged  ({a['awaiting']} awaiting)")
    print(f"  decision_queue  : mac {d['mac']:>4} + vm {d['vm']:>4} "
          f"-> {d['merged']:>4} merged  ({d['awaiting']} awaiting, "
          f"{d['resolved']} already decided)")
    print(f"  ids in {rep['ids_in']} -> ids preserved {rep['ids_out']}  (dropped: {len(rep['ids_dropped'])})")
    ar = rep["archive"]
    print(f"  archive         : mac {ar['mac']:>4} + vm {ar['vm']:>4} "
          f"-> {ar['merged']:>4} tombstones  "
          f"({len(rep['suppressed'])} row(s) kept out of the queue)")
    if rep["titles_healed"]:
        print(f"  repaired {rep['titles_healed']} pre-2026-08-19 fold(s) with the "
              f"folded text the sentinel keys on")
    if rep["reopened_after_archive"]:
        print(f"\n  ⚠  {len(rep['reopened_after_archive'])} archived problem(s) are "
              f"OPEN again on a host and were kept in the queue:")
        for i in rep["reopened_after_archive"][:10]:
            print(f"       • {i}")
    if rep["overdue_after_merge"]:
        print(f"\n  ⚠  {len(rep['overdue_after_merge'])} item(s) are past the {SLA_DAYS}-day SLA "
              f"on their true (earliest) clock.\n     chairman_decision_sla.py will escalate "
              f"these to the board on its next run:")
        for i in rep["overdue_after_merge"][:10]:
            print(f"       • {i}")
    if rep["wrote"]:
        print(f"\n  wrote both hosts. pre-merge snapshot: {rep['snapshot']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

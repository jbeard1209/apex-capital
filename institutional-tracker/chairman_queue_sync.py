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

USAGE
    python3 chairman_queue_sync.py --report     # dry run, prints the plan
    python3 chairman_queue_sync.py              # merge + write both hosts
    python3 chairman_queue_sync.py --local-only # merge into the Mac only
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

VM_HOST = "apex-capital"
VM_BASE = "~/institutional-tracker"

ACTION_FILE = "chairman_action_required.json"
QUEUE_FILE = "chairman_decision_queue.json"

# Status lattice. Merging may only move an item UP these ladders, never down.
# "known_infrastructure" outranks "awaiting_chairman" because it is a triage
# outcome — someone looked and decided it wasn't a decision to make.
ACTION_RANK = {"open": 0, "awaiting_chairman": 1, "known_infrastructure": 2,
               "resolved": 3}
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


def _load_remote(name: str) -> list:
    r = subprocess.run(["ssh", VM_HOST, f"cat {VM_BASE}/data/{name}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read {name} on {VM_HOST}: {r.stderr.strip()[:200]}")
    try:
        v = json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(f"{name} on {VM_HOST} is not valid JSON: {e}")
    return v if isinstance(v, list) else []


def _write_local(name: str, obj) -> None:
    p = DATA / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(p)


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
    if local_only:
        rem_act, rem_dec = [], []
    else:
        rem_act, rem_dec = _load_remote(ACTION_FILE), _load_remote(QUEUE_FILE)

    m_act = merge_actions(loc_act, rem_act)
    m_dec = merge_decisions(loc_dec, rem_dec)

    rep = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "actions": {"mac": len(loc_act), "vm": len(rem_act),
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

    if write:
        _archive(stamp, "mac", ACTION_FILE, loc_act)
        _archive(stamp, "mac", QUEUE_FILE, loc_dec)
        if not local_only:
            _archive(stamp, "vm", ACTION_FILE, rem_act)
            _archive(stamp, "vm", QUEUE_FILE, rem_dec)
        _write_local(ACTION_FILE, m_act)
        _write_local(QUEUE_FILE, m_dec)
        if not local_only:
            _write_remote(ACTION_FILE, m_act)
            _write_remote(QUEUE_FILE, m_dec)
        rep["wrote"] = True
        rep["archive"] = str(DATA / "chairman_queue_archive" / stamp)
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
    if rep["overdue_after_merge"]:
        print(f"\n  ⚠  {len(rep['overdue_after_merge'])} item(s) are past the {SLA_DAYS}-day SLA "
              f"on their true (earliest) clock.\n     chairman_decision_sla.py will escalate "
              f"these to the board on its next run:")
        for i in rep["overdue_after_merge"][:10]:
            print(f"       • {i}")
    if rep["wrote"]:
        print(f"\n  wrote both hosts. pre-merge archive: {rep['archive']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

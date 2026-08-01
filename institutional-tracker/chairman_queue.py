"""Single source of truth for writing data/chairman_action_required.json.

Six agents raise items into this ONE shared file. Five of them implemented the
size cap as `combined = combined[-20:]` -- which trims the *shared* list, so
whichever agent happened to run last silently deleted up to 20 of every *other*
agent's open items. On 2026-07-31 a single apex_system_cit_agent run took the
queue from 153 entries to 20, evicting 141 items marked awaiting_chairman
(including "CRO has blocked ALL buys for 272 days" and the pyarrow bug).

Nothing was ultimately lost -- chairman_decision_sla.py had already copied those
items into data/chairman_decision_queue.json, the app-facing inbox it owns -- but
a feed is not allowed to be correct only by accident of who read it first.

cit_pipeline_integrity_agent.py already had the right policy (and a smoke test
for it); the other five were never updated. This module is that policy, once:

  * an agent may only ever evict its OWN entries -- another producer's item is
    untouchable,
  * an item that is still open is not re-raised (dedup_key), so a check that
    fires every 2h does not consume the queue, and
  * `dedup_key` is a stable *semantic* key (which check fired), distinct from
    the human-readable `id` (which carries a run timestamp and so changes every
    run). Deduping on `id` would never match; capping on `id` alone is why ten
    distinct stale-cron findings collapsed into one queue entry.

Consumer: chairman_decision_sla.py::_ingest() copies awaiting_chairman items
here into the Chairman's inbox, keyed on `id` -- so ids must be unique per item
or the inbox silently under-reports. enforced by _uniquify_ids() below.
"""

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/chairman_action_required.json"
DEFAULT_MAX_ENTRIES = 20

#: statuses that mean "the Chairman has not dealt with this yet"
OPEN_STATUSES = frozenset({"awaiting_chairman", "open"})

#: Eviction order when a source exceeds its cap — least severe goes first.
SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _load(path):
    """Fail-OPEN to an empty list only when the file genuinely isn't there.

    A corrupt/unreadable queue must NOT silently become [] -- that would let one
    bad write erase every producer's items on the next run. We re-raise instead
    so the caller's error handling surfaces it.
    """
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    return raw if isinstance(raw, list) else []


def _derive_dedup_key(entry):
    """A stable identity for 'the same problem', independent of run time."""
    for field in ("dedup_key", "check", "rule"):
        val = entry.get(field)
        if val:
            return str(val)
    # Fall back to the full description -- NOT a prefix. Truncating to 20-30
    # chars is what made ten different "Cron job stale on weekday: <job>"
    # findings share one key.
    desc = str(entry.get("description", ""))
    return "sha1:" + hashlib.sha1(desc.encode("utf-8")).hexdigest()[:16]


def _uniquify_ids(entries):
    """Guarantee every entry carries an id unique within the file.

    The SLA ingester keys on `id`; colliding ids mean it ingests one item and
    drops the rest without a word.
    """
    seen = set()
    for e in entries:
        eid = e.get("id") or "item"
        if eid in seen:
            suffix = hashlib.sha1(
                _derive_dedup_key(e).encode("utf-8")).hexdigest()[:6]
            eid = "%s-%s" % (eid, suffix)
            # Pathological case: same id AND same dedup_key. Walk a counter
            # rather than loop forever.
            n = 2
            while eid in seen:
                eid, n = "%s-%s-%d" % (e.get("id"), suffix, n), n + 1
            e["id"] = eid
        seen.add(eid)
    return entries


def submit(new_entries, source, path=DEFAULT_PATH,
           max_entries=DEFAULT_MAX_ENTRIES):
    """Merge `new_entries` into the shared Chairman queue on behalf of `source`.

    Returns (n_added, n_suppressed_as_duplicate). Entries should already be
    formed dicts (severity/description/status/...); `source` is stamped on and
    is what scopes both the dedup and the cap.
    """
    existing = _load(path)

    open_keys = {
        _derive_dedup_key(e) for e in existing
        if e.get("source") == source and e.get("status") in OPEN_STATUSES
    }

    added, suppressed = [], 0
    for entry in new_entries:
        entry.setdefault("source", source)
        key = _derive_dedup_key(entry)
        entry.setdefault("dedup_key", key)
        if key in open_keys:
            suppressed += 1
            continue
        open_keys.add(key)
        added.append(entry)

    combined = existing + added

    # Cap ONLY our own entries. Another producer's item is never a candidate for
    # eviction, whatever the queue length.
    #
    # Evict the LEAST severe first, oldest-first within a severity — not blind
    # oldest. Straight FIFO threw away a HIGH "seven agents were retired and
    # never stopped trading" to make room for a LOW "we cannot date the risk
    # memo", because the HIGH happened to be submitted one position earlier in
    # the same call. What the Chairman loses to a cap has to be the least
    # important thing in the queue, not the earliest.
    own = [i for i, e in enumerate(combined) if e.get("source") == source]
    evicted = []
    if len(own) > max_entries:
        ranked = sorted(own, key=lambda i: (SEVERITY_RANK.get(
            str(combined[i].get("severity", "")).upper(), 0), i))
        drop = set(ranked[: len(own) - max_entries])
        evicted = [combined[i] for i in sorted(drop)]
        combined = [e for i, e in enumerate(combined) if i not in drop]

    # Report what was WRITTEN, not what was offered. `len(added)` counted entries
    # the cap had already thrown out one line later, so a dropped HIGH finding
    # was logged as "raised" and the caller believed it had reached someone.
    dropped_ids = {id(e) for e in evicted}
    added = [e for e in added if id(e) not in dropped_ids]
    if evicted:
        logger.warning(
            "  Chairman queue: %d %s entr(ies) EVICTED by the per-source cap of "
            "%d and did NOT reach the Chairman: %s",
            len(evicted), source, max_entries,
            ", ".join("%s[%s]" % (e.get("id"), e.get("severity")) for e in evicted))

    _uniquify_ids(combined)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    os.replace(tmp, path)   # atomic: a crash mid-write can't truncate the queue

    logger.info("  Chairman queue: +%d new, %d suppressed as already-open, "
                "%d entries total.", len(added), suppressed, len(combined))
    return len(added), suppressed

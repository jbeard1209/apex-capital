#!/usr/bin/env python3
"""
Chairman Inbox reader  (companion to chairman_decision_sla.py)
==============================================================
Prints the decisions currently awaiting Jack, in a compact form meant to be
shown at the TOP of a Claude session (never email). Also emits a directive
block instructing the session to surface pending decisions BEFORE starting new
work — the "make the mailbox ring" half of Chairman directive 2026-09-29
("before i do anything else, i should have to agree or disagree with whatever
is pending my decision").

This is the surfacer for the SLA design: chairman_decision_sla.py maintains the
queue (data/chairman_decision_queue.json); this reader shows it. Wired to run
at session start via a SessionStart hook (.claude/settings.json) so pending
decisions can't hide in a file nobody opens — the failure mode that let the
272-day buy-freeze persist.

The reader is CATEGORY-AWARE (via chairman_decision_categorizer.classify):

  • CARVE-OUT categories (arm_gate / capital / hr / protected_file) never
    auto-escalate; they stay Chairman-only forever, so they must be surfaced
    prominently — a chairman-only overdue is not "waiting on the board", it is
    a decision only Jack can make and it has been sitting.
  • Auto-board categories still surface, but the 15-day SLA takes them off the
    critical-path list once they escalate.

Flags:
  --limit N           top N items in the compact banner (default 5).
  --full              print ALL items, grouped by category (no top-N cap).
  --quiet-if-empty    print nothing if the queue is empty (SessionStart hook).
  --no-sync           skip the cross-host reconcile.
  --no-directive      omit the Claude-directive block at the end.

Read-only. Safe to run anytime.
"""
from __future__ import annotations
import json, argparse, datetime
from pathlib import Path

QUEUE_PATH = Path(__file__).resolve().parent / "data" / "chairman_decision_queue.json"
SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# Category ordering for display — carve-outs first (Chairman-only means the
# most urgent surface), then the auto-board categories, then anything the
# categorizer could not place. Kept here rather than imported so a new
# category in the categorizer surfaces as "other" until its display slot is
# added explicitly.
DISPLAY_ORDER = [
    "arm_gate", "capital", "hr", "protected_file",     # carve-outs
    "cit_code", "wiring", "freshness", "governance",   # auto-board
    "other",
]


def _parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "").split("+")[0])
    except Exception:
        return None


def load_pending():
    try:
        q = json.loads(QUEUE_PATH.read_text())
    except Exception:
        return []
    if not isinstance(q, list):
        return []
    return [i for i in q if isinstance(i, dict) and i.get("status") == "awaiting_chairman"]


def reconcile():
    """Fold the VM's copy of the queue into this host's before reading it.

    Until 2026-08-01 this reader showed the MAC's queue while chairman_decision_sla
    ran only on the VM: Jack saw 10 items while 55 more sat on the other host, and
    129 decisions he had already made were invisible to the escalator that was
    about to hand them to the board. The inbox is only honest if it reconciles
    first. Returns a warning string when it could not, because a reader that
    silently falls back to a stale local file is the same blind spot again.

    Two failures reach here and they are NOT the same fact. `sync()` raises
    RuntimeError when it reached the VM, computed the merge, and REFUSED to
    write because the result would lose evidence; anything else means it could
    not get the VM's copy at all. Both were reported as "could not reach the
    VM" until 2026-08-27, and the difference is the whole decision: a network
    blip self-heals and needs nothing from Jack, while a refusal never
    self-heals — it had been firing every session for 8.6 days, holding the
    queue he reads at 206h stale against the VM's 14h, and the only signal said
    "may be stale". Naming the wrong cause is worse than naming none, because
    the wrong cause came with an implied "it'll sort itself out".
    """
    try:
        from chairman_queue_sync import sync
        sync(write=True)
        return None
    except RuntimeError as e:
        return (f"the VM was REACHED and the merge REFUSED to write ({str(e)[:80]}). "
                f"This does not self-heal and the queue below is this host's stale "
                f"copy — chairman_queue_sync needs a look")
    except Exception as e:
        # A local OSError is not a network failure; say which, and keep the TAIL
        # of the message — a 60-char head cut a path to "/Users/jackbeard/Claud"
        # and hid the one fact (which file) that names the cause.
        msg = str(e)
        msg = msg if len(msg) <= 120 else "…" + msg[-119:]
        if isinstance(e, OSError) and getattr(e, "filename", None):
            return (f"local file error during queue sync ({type(e).__name__}: {msg}) "
                    f"— the VM may be fine; this queue may be stale")
        return f"could not reach the VM ({msg}) — this queue may be stale"


def _category_of(item: dict) -> str:
    """Prefer the queue row's stamped category (written by chairman_decision_sla
    on ingest), falling back to a live classification. Two reasons to prefer
    the stamp: it is what the SLA gated the escalation decision on, and it is
    what the archive will record; a live re-classification that disagrees with
    it would show the Chairman a taxonomy his SLA is not enforcing."""
    stamped = (item.get("category") or "").strip()
    if stamped:
        return stamped
    try:
        from chairman_decision_categorizer import classify
        return classify(item).name
    except Exception:
        return "other"


def _sort_key(item, now):
    """Sort by deadline (soonest first), then severity. Overdue items go
    first because a negative days-remaining sorts before a positive one."""
    dl = _parse_ts(item.get("deadline")) or now
    return (dl, SEV_RANK.get(item.get("severity", "MEDIUM"), 2))


def _fmt_deadline(item, now):
    dl = _parse_ts(item.get("deadline"))
    if not dl:
        return "no-deadline"
    delta = (dl - now).days
    if delta < 0:
        return f"OVERDUE {abs(delta)}d"
    return f"{delta}d left"


CARVE_OUTS = {"arm_gate", "capital", "hr", "protected_file"}


def _print_compact(pending, now, limit, warning):
    n = len(pending)
    print("┌─ CHAIRMAN DECISION QUEUE " + "─" * 38)
    if warning:
        print(f"│  ⚠  {warning}")
    if n == 0:
        print("│  ✅ Nothing awaiting your decision, Jack.")
        print("└" + "─" * 63)
        return

    # By-category counts — show carve-outs separately so their weight is
    # visible. "3 chairman-only, 44 auto-board" reads correctly even when
    # both totals shift.
    from collections import Counter
    cat_counts = Counter(_category_of(i) for i in pending)
    carve = sum(v for k, v in cat_counts.items() if k in CARVE_OUTS)
    autob = n - carve

    soonest = _parse_ts(pending[0].get("deadline"))
    days = (soonest - now).days if soonest else None
    tail = f" · soonest board-escalation in {days}d" if days is not None else ""
    print(f"│  {n} item(s) need your decision{tail}")
    if carve:
        parts = [f"{v} {k}" for k, v in cat_counts.most_common() if k in CARVE_OUTS]
        print(f"│  🔒 {carve} chairman-only ({', '.join(parts)}) · "
              f"{autob} auto-board fallback")
    print("│")
    for q in pending[:limit]:
        cat = _category_of(q)
        mark = "🔒 " if cat in CARVE_OUTS else "   "
        desc = " ".join((q.get("description") or "").split())
        left = _fmt_deadline(q, now)
        title = q.get("title") or q.get("id")
        print(f"│  {mark}[{q.get('severity','?'):8}] {title[:56]}  ({left})")
        print(f"│     {desc[:120]}{'…' if len(desc) > 120 else ''}")
    if n > limit:
        print(f"│  … and {n - limit} more. Ask me: \"what needs my decision?\"")
    print("└" + "─" * 63)


def _print_full(pending, now):
    """Categorized dump — the shape the Chairman wants when he asks 'what
    needs my decision?'. Groups within each category by deadline, then
    severity. Prints Chairman-only categories first."""
    if not pending:
        print("✅ Nothing awaiting your decision, Jack.")
        return

    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for it in pending:
        by_cat[_category_of(it)].append(it)

    # Category display order: DISPLAY_ORDER first, then any unknown category
    # (a category the categorizer emits but this reader has not been taught
    # about) — that "unknown but seen" case is worth surfacing rather than
    # silently dropping into "other".
    cats_seen = list(by_cat.keys())
    ordered = [c for c in DISPLAY_ORDER if c in by_cat] + \
              [c for c in cats_seen if c not in DISPLAY_ORDER]

    n = len(pending)
    print("=" * 72)
    print(f"  CHAIRMAN DECISION QUEUE — {n} pending, grouped by category")
    print("=" * 72)

    for cat in ordered:
        items = sorted(by_cat[cat], key=lambda x: _sort_key(x, now))
        header_mark = "🔒 CHAIRMAN-ONLY" if cat in CARVE_OUTS else "⚖  auto-board fallback"
        print(f"\n[{cat}]  {len(items)} item(s)   ({header_mark})")
        print("-" * 72)
        for i, q in enumerate(items, 1):
            left = _fmt_deadline(q, now)
            title = q.get("title") or q.get("id")
            desc = " ".join((q.get("description") or "").split())
            print(f" {i:3}. [{q.get('severity','?'):8}] {title[:80]}")
            print(f"      id: {q.get('id')}   ({left})")
            if desc:
                print(f"      {desc[:200]}{'…' if len(desc) > 200 else ''}")
    print()


def _print_directive(pending):
    """Session directive — printed after the inbox so a Claude session
    reading this output at SessionStart sees an unmissable instruction to
    surface the pending list before doing new work. This is the Chairman's
    2026-09-29 directive materialized: "before i do anything else, i should
    have to agree or disagree with whatever is pending my decision."

    The block is designed to READ the same to a human and to a Claude
    session. It names the count so a truncated context still knows to ask,
    and points at the concrete command that expands the list."""
    if not pending:
        return
    n = len(pending)
    carve = sum(1 for i in pending if _category_of(i) in CARVE_OUTS)
    autob = n - carve
    print()
    print("─" * 72)
    print("SESSION DIRECTIVE  (Chairman standing order 2026-09-29)")
    print("─" * 72)
    print(f"There are {n} decision(s) awaiting Jack "
          f"({carve} chairman-only, {autob} board-fallback).")
    print()
    print("BEFORE starting new work in this session, the assistant must:")
    print("  1. Show Jack the list of pending decisions grouped by category")
    print("     (run: python3 chairman_inbox.py --full).")
    print("  2. Offer to walk through them and record his decision on each,")
    print("     starting with the CHAIRMAN-ONLY (carve-out) items — those")
    print("     never auto-escalate to the board and can only be closed by Jack.")
    print("  3. Only after Jack has either dispositioned them or told the")
    print("     assistant to defer them, proceed with any new user request.")
    print()
    print("Jack asked for this so decisions cannot silently accumulate. If he")
    print("says 'skip it', or 'later', or 'just do the new thing', that is an")
    print("explicit defer — proceed. Do not proceed without asking.")
    print("─" * 72)


def main():
    ap = argparse.ArgumentParser(description="Show decisions awaiting the Chairman (app inbox).")
    ap.add_argument("--limit", type=int, default=5,
                    help="items shown in the compact banner (default 5)")
    ap.add_argument("--full", action="store_true",
                    help="print all items grouped by category (no top-N cap)")
    ap.add_argument("--quiet-if-empty", action="store_true",
                    help="print nothing at all when the queue is empty (for hooks)")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip the cross-host reconcile (offline / tests)")
    ap.add_argument("--no-directive", action="store_true",
                    help="omit the Claude-directive block at the end")
    args = ap.parse_args()

    warning = None if args.no_sync else reconcile()

    pending = load_pending()
    if not pending and args.quiet_if_empty and not warning:
        return

    now = datetime.datetime.utcnow()
    pending.sort(key=lambda q: _sort_key(q, now))

    if args.full:
        _print_full(pending, now)
    else:
        _print_compact(pending, now, args.limit, warning)

    if not args.no_directive:
        _print_directive(pending)


if __name__ == "__main__":
    main()

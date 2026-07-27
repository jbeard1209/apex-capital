#!/usr/bin/env python3
"""
Chairman Inbox reader  (companion to chairman_decision_sla.py)
==============================================================
Prints the decisions currently awaiting Jack, newest-deadline-first, in a
compact form meant to be shown at the TOP of a Claude session (never email).

This is the "make the mailbox ring" half of the decision-SLA design: the SLA
agent maintains data/chairman_decision_queue.json; this reader surfaces it. It
is wired to run at session start via a SessionStart hook (.claude/settings.json)
so pending decisions can't hide in a file nobody opens — the exact failure mode
that let the 272-day buy-freeze persist.

Read-only. Safe to run anytime. `--limit N` caps how many items print (default 5).
"""
from __future__ import annotations
import json, argparse, datetime
from pathlib import Path

QUEUE_PATH = Path(__file__).resolve().parent / "data" / "chairman_decision_queue.json"
SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


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


def main():
    ap = argparse.ArgumentParser(description="Show decisions awaiting the Chairman (app inbox).")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--quiet-if-empty", action="store_true",
                    help="print nothing at all when the queue is empty (for hooks)")
    args = ap.parse_args()

    pending = load_pending()
    if not pending and args.quiet_if_empty:
        return

    now = datetime.datetime.utcnow()
    pending.sort(key=lambda q: (_parse_ts(q.get("deadline")) or now,
                                SEV_RANK.get(q.get("severity", "MEDIUM"), 2)))

    n = len(pending)
    print("┌─ CHAIRMAN DECISION QUEUE " + "─" * 38)
    if n == 0:
        print("│  ✅ Nothing awaiting your decision, Jack.")
        print("└" + "─" * 63)
        return

    soonest = _parse_ts(pending[0].get("deadline"))
    days = (soonest - now).days if soonest else None
    tail = f" · soonest board-escalation in {days}d" if days is not None else ""
    print(f"│  {n} item(s) need your decision{tail}")
    print("│")
    for q in pending[: args.limit]:
        dl = _parse_ts(q.get("deadline"))
        left = (dl - now).days if dl else None
        left_str = f"{left}d" if left is not None and left >= 0 else "OVERDUE"
        desc = " ".join((q.get("description") or "").split())
        print(f"│  [{q.get('severity','?'):8}] {q.get('id')}  ({left_str} left)")
        print(f"│     {desc[:120]}{'…' if len(desc) > 120 else ''}")
    if n > args.limit:
        print(f"│  … and {n - args.limit} more. Ask me: \"what needs my decision?\"")
    print("└" + "─" * 63)


if __name__ == "__main__":
    main()

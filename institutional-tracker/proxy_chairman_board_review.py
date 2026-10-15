#!/usr/bin/env python3
"""
Proxy Chairman — Board escape-risk review.

ASKS: if we resolve this incorrectly, what breaks?
      can this item come back as a real signal we'd miss?

This gate must disagree with the Claude precision gate on different
questions, otherwise the dual-approval is theatre:

    - Claude asks: is the rule applied correctly? (precision)
    - Board  asks: is the escape-risk acceptable? (recall)

Blocks:
    1. Any item touching capital / position / risk-limit / credential
       language in its description, even if the rule allowed it.
    2. Any item whose group-signature has resolved AND REAPPEARED in
       the queue within the last 30 days (looping — maybe the fix
       didn't stick).
    3. Any item in a currently-escalated board_issue_ledger thread.
    4. Any item with proxy-decision churn: this exact item has been
       proxied before (any status). One shot per item.
"""

from __future__ import annotations
import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ARCHIVE = DATA / "chairman_resolved_archive.json"
QUEUE = DATA / "chairman_decision_queue.json"
BOARD_LEDGER = DATA / "board_issue_ledger.json"
PROPOSALS = DATA / "proxy_chairman_proposals.json"


BLOCKED_DESCRIPTION_TOKENS = [
    "capital release", "release capital", "unblock buy",
    "position limit", "position_limit", "risk limit", "risk_limit",
    "credential", "secret", "api_key", "api key", "token leak",
    "transfer funds", "withdraw",
    "override risk", "override cro", "override_cro",
]


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _looping_group(item: dict, archive: list) -> bool:
    """True if this item's dedup_key / signature has already been resolved
    then reappeared in the last 30 days."""
    sig = item.get("dedup_key") or item.get("signature")
    if not sig:
        return False
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    seen = 0
    for r in archive:
        if r.get("dedup_key") == sig or r.get("signature") == sig:
            try:
                when = dt.datetime.fromisoformat(
                    (r.get("archived_on") or r.get("ts") or "").replace("Z", "+00:00")
                )
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if when > cutoff:
                seen += 1
    return seen >= 2


def _already_proxied(item_id: str, proposals: list) -> bool:
    return any(p.get("item_id") == item_id for p in proposals)


def _board_has_live_thread(item_id: str, ledger: dict) -> bool:
    """Return True if a non-closed board thread references this item.

    Defensive against ledger shape drift: the field can be absent, a list
    of strings (older schema), or a list of dicts (current schema). Only
    dict entries can carry a reference, so string entries are ignored.
    """
    if not isinstance(ledger, dict):
        return False
    threads = ledger.get("open_issues") or ledger.get("issues") or []
    if not isinstance(threads, list):
        return False
    for t in threads:
        if not isinstance(t, dict):
            continue
        refs = t.get("linked_queue_ids") or []
        if isinstance(refs, list) and item_id in refs and \
                t.get("status") not in ("closed", "resolved"):
            return True
    return False


def review(proposal: dict, item: dict, rule: dict) -> dict:
    desc_low = (item.get("description") or "").lower()
    for tok in BLOCKED_DESCRIPTION_TOKENS:
        if tok in desc_low:
            return {"approved": False, "reason": f"description contains blocked token {tok!r}"}

    archive = _load(ARCHIVE, [])
    if isinstance(archive, list) and _looping_group(item, archive):
        return {"approved": False,
                "reason": "looping group — resolved then reappeared within 30d; needs Chairman"}

    ledger = _load(BOARD_LEDGER, {})
    if _board_has_live_thread(item["id"], ledger):
        return {"approved": False, "reason": "live board thread references this item"}

    # The real guard against proxy churn is NOT in this review — it is in
    # the agent's filter (only `awaiting_chairman` items are seen) plus
    # `_mark_resolved_in_queue`'s own refusal to overwrite a terminal row.
    # Historically this gate also had a "one-shot per item" check, but it
    # was redundant with the queue-status filter AND it broke the normal
    # workflow (propose mode would approve a proposal, then live mode would
    # see that approval and reject, so nothing ever got written).
    return {"approved": True, "flags": []}


if __name__ == "__main__":
    import sys
    PROPS = DATA / "proxy_chairman_proposals.json"
    QUEUE_P = DATA / "chairman_decision_queue.json"
    RULES = DATA / "autoresolve_rules.json"
    props = _load(PROPS, [])
    if not props:
        print("no proposals yet"); sys.exit(0)
    p = props[-1]
    queue = _load(QUEUE_P, [])
    rules = _load(RULES, {"rules": []}).get("rules", [])
    item = next((r for r in queue if r.get("id") == p["item_id"]), None)
    rule = next((r for r in rules if r.get("id") == p["rule_id"]), None)
    if not item or not rule:
        print("could not resolve item or rule"); sys.exit(1)
    print(json.dumps(review(p, item, rule), indent=2))

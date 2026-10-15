#!/usr/bin/env python3
"""
Proxy Chairman — Claude precision review.

ASKS: is this rule actually applied correctly to this item?
      is the cited premise still true?

This is one of two independent gates. It must disagree with the Board
gate on different questions, otherwise the dual-approval is theatre.

Checks (deterministic, no LLM call — the whole proxy architecture is
rules-based by Chairman decision 2026-10-15):

    1. Re-apply the rule's match predicate against the item; the match
       must still hold (guards against proposal/queue drift between
       propose-time and review-time).
    2. For "superseded by X" / "fixed upstream" verdicts, verify the
       cited anchor can be found in the resolved archive or in the
       memory corpus (data/chairman_decision_corpus.json if present).
    3. For "resolved" (hard-close) verdicts, flag HIGH severity for
       extra scrutiny.
    4. Reject any item whose severity is CRITICAL — those always go
       to Chairman, regardless of rule.
"""

from __future__ import annotations
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ARCHIVE = DATA / "chairman_resolved_archive.json"
CORPUS = DATA / "chairman_decision_corpus.json"


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _rule_still_matches(item: dict, rule: dict) -> bool:
    # Re-import the matcher at review time so we use the current logic.
    from proxy_chairman_agent import _match_predicate
    return _match_predicate(item, rule["match"])


def _anchor_findable(citation: str) -> bool:
    if not citation:
        return False
    citation = citation.lower()
    hay = []
    arc = _load(ARCHIVE, [])
    if isinstance(arc, list):
        for r in arc[-500:]:  # recent only; keeps the scan cheap
            hay.append((r.get("resolution_note") or "").lower())
            hay.append((r.get("description") or "").lower())
            hay.append((r.get("id") or "").lower())
    corpus = _load(CORPUS, {})
    if isinstance(corpus, dict):
        for key in ("memory_citations", "chairman_directives", "fix_commits"):
            for c in corpus.get(key, []):
                hay.append(str(c).lower())
    # Pick a small set of landmark tokens from the citation.
    tokens = [t for t in citation.replace("(", " ").replace(")", " ").split() if len(t) > 3]
    if not tokens:
        return True  # citation is empty-ish — fall through
    for t in tokens:
        if any(t in h for h in hay):
            return True
    return False


def review(proposal: dict, item: dict, rule: dict) -> dict:
    reasons = []

    # 1. CRITICAL items never auto-close — regardless of rule.
    if (proposal.get("item_severity") or "").upper() == "CRITICAL":
        return {"approved": False, "reason": "CRITICAL severity — Chairman only."}

    # 2. Re-apply match (guard against drift).
    try:
        if not _rule_still_matches(item, rule):
            return {"approved": False,
                    "reason": "rule predicate no longer holds against item at review time"}
    except Exception as e:
        return {"approved": False, "reason": f"re-match raised: {e!r}"}

    # 3. Anchor verification for superseded/upstream verdicts.
    verdict = proposal.get("verdict", "")
    rationale = (proposal.get("rationale") or "").lower()
    if ("superseded" in rationale or "fixed" in rationale or verdict == "resolved_upstream"):
        if not _anchor_findable(proposal.get("rule_citation") or ""):
            reasons.append("anchor citation not findable in archive/corpus")

    # 4. HIGH severity + hard-close verdict is noteworthy; approve but mark.
    sev = (proposal.get("item_severity") or "").upper()
    if sev == "HIGH" and verdict == "resolved":
        reasons.append("note: HIGH severity + hard-close verdict (approved with flag)")

    if any("not findable" in r for r in reasons):
        return {"approved": False, "reason": "; ".join(reasons)}

    return {"approved": True, "flags": reasons}


if __name__ == "__main__":
    import sys
    # Smoke-run: show the result of reviewing the most recent proposal.
    PROPOSALS = DATA / "proxy_chairman_proposals.json"
    QUEUE = DATA / "chairman_decision_queue.json"
    RULES = DATA / "autoresolve_rules.json"
    props = _load(PROPOSALS, [])
    if not props:
        print("no proposals yet"); sys.exit(0)
    p = props[-1]
    queue = _load(QUEUE, [])
    rules = _load(RULES, {"rules": []}).get("rules", [])
    item = next((r for r in queue if r.get("id") == p["item_id"]), None)
    rule = next((r for r in rules if r.get("id") == p["rule_id"]), None)
    if not item or not rule:
        print("could not resolve item or rule"); sys.exit(1)
    print(json.dumps(review(p, item, rule), indent=2))

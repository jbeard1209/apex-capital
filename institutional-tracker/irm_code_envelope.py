#!/usr/bin/env python3
"""
IRM risk-envelope review for autonomous code fixes.
================================================================================
The sign-off ledger requires TWO distinct lenses on every autonomous fix: CIT
(is it correct?) and IRM (is it safe to let a machine ship unattended?). The CIT
lens is the implementation agent itself. This module is the IRM lens.

It is deliberately a BOUNDED, RULE-BASED risk judgement — not an LLM opinion —
so it is auditable and can't hallucinate an approval. IRM signs only when the
change sits inside a small, reversible "risk envelope"; anything outside it is
withheld (a veto) and routes to a human. Reputation still attaches to the
"IRM-Risk-Envelope" signer, so a fix that later reverts costs it (3x a hold) —
the sign-off is not free.

This is v1. It can be upgraded to add an LLM risk read LATER without changing the
contract: `review(...)` returns {ok, rationale, reason}. Widening the envelope is
a governance change (more autonomy) and should go through the board, not a quiet
edit here.
"""
from __future__ import annotations

import signoff_ledger as _sl

# Mirror deputy_board.AUTO_IMPLEMENT_CATEGORIES — the only categories trivial
# enough to ship unattended. Kept in sync deliberately (a copy, not an import,
# to avoid pulling the whole board module into the actuator's import path).
_ENVELOPE_CATEGORIES = {"BUG_FIX", "LOGIC_ERROR", "INTEGRATION"}
_ENVELOPE_MAX_SEVERITY = "LOW"


def review(files: list, added: int, removed: int,
           severity: str, category: str) -> dict:
    """Return {ok, rationale, reason}. ok=True → IRM signs; ok=False → IRM withholds
    (veto → human). Fail-closed: any doubt withholds."""
    reasons = []

    # 1) Never inside the envelope if it touches a hard line (defense in depth —
    #    the actuator already filters these, but IRM re-checks independently).
    hard = _sl.touches_hard_line(files or [])
    if hard:
        reasons.append(f"touches hard-line file(s): {', '.join(hard)}")

    # 2) Severity ceiling — only the most trivial fixes ship unattended.
    if str(severity or "").upper() != _ENVELOPE_MAX_SEVERITY:
        reasons.append(f"severity '{severity}' exceeds the LOW-only envelope "
                       f"— needs a human risk review")

    # 3) Category must be one of the known-trivial classes.
    if str(category or "").upper() not in _ENVELOPE_CATEGORIES:
        reasons.append(f"category '{category}' is outside the low-risk envelope "
                       f"({', '.join(sorted(_ENVELOPE_CATEGORIES))})")

    # 4) Must be small and reversible (mirror the ledger's blast limits).
    if not _sl.blast_radius_ok(files or [], added, removed):
        reasons.append(f"blast radius outside the envelope "
                       f"({len(files or [])} file(s), {int(added or 0)+int(removed or 0)} lines)")

    if reasons:
        return {"ok": False, "rationale": "", "reason": "; ".join(reasons)}
    return {
        "ok": True,
        "rationale": (f"within LOW-risk envelope: {category}, {len(files or [])} file(s)/"
                      f"{int(added or 0)+int(removed or 0)} lines, no hard-line, revertable"),
        "reason": "",
    }


if __name__ == "__main__":
    # tiny self-check
    print("clear :", review(["foo.py"], 2, 0, "LOW", "BUG_FIX"))
    print("sev   :", review(["foo.py"], 2, 0, "HIGH", "BUG_FIX"))
    print("hard  :", review(["config.py"], 1, 0, "LOW", "BUG_FIX"))
    print("blast :", review(["a.py", "b.py", "c.py"], 90, 10, "LOW", "BUG_FIX"))

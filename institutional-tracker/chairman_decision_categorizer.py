#!/usr/bin/env python3
"""
Chairman decision categorizer  (companion to chairman_decision_sla.py)
======================================================================
A single decision belongs to a single category. Two callers need to agree
on that category:

  * chairman_decision_sla.py — needs to know whether a 15-day-idle item
    may be auto-ratified by the board on the Chairman's behalf, or must
    stay Chairman-only forever.
  * chairman_inbox.py — needs to group and label the pending list at
    session start so the Chairman can decide by class ("here are the 22
    wiring gaps: yes/yes/no/defer") instead of item by item.

If those two callers keep separate taxonomies, one of them will drift and
the wrong item eventually reaches the wrong destination — the aggregate-is-
not-a-finding failure mode. So the taxonomy lives here, in one function
(``classify``), and both callers import it.

CATEGORIES
----------
The queue holds strings, not types, so classification is text-based. Each
category has (1) a set of substrings that identify it, checked against the
concatenated id + title + description + dedup_key + source, and (2) a
boolean ``board_may_ratify`` — the whole point of a category is to answer
that question. Order matters: the first match wins, so more-specific
categories are checked before less-specific ones.

CARVE-OUTS  (board_may_ratify = False)
--------------------------------------
The board CANNOT auto-ratify these, no matter how long the Chairman sits
on them. They stay in the Chairman queue past 15 days, past 30, forever.
Reason: each involves an authority the board does not hold.

  * arm_gate         — flipping a live-trading arm gate. Kalshi mirror
                       arm, Kalshi sector arm, FI arm, kill-switch flips.
                       The whole point of an arm gate is that authority
                       is not delegable.
  * capital          — moving money between pods, changing per-pod
                       budgets or firm capital allocation. Only the
                       Chairman decides how the account is sliced.
  * hr               — hiring, firing, PIP verdicts on an agent's own
                       fate. Board can propose, only Chairman decides.
                       (Convention: the [[hr_keep_advocacy_veto]] rule
                       already applies at the board level.)
  * protected_file   — a proposal that would edit a protected file
                       (alpaca_trader.py, main.py, risk_manager_agent.py,
                       irm_review_agent.py, board_meeting.py,
                       manager_agent.py). The list of protected files is
                       encoded in CLAUDE.md, and code that touches those
                       does not get auto-approved by the deputy board.

AUTO-BOARD  (board_may_ratify = True)
-------------------------------------
Everything else. The board's default vote is "approve the proposed
remedy the sweep already drafted". If the sweep did not draft one,
the item is not in a shape the board can vote on and stays.

  * wiring           — producer/consumer wiring gaps from the weekly
                       sweep. The proposed remedy is always "write the
                       missing producer, or degrade the consumer honestly."
  * cit_code         — code proposals from CIT with a specific fix.
  * freshness        — stale data files where the fix is "rerun the
                       producer" or "close as dead."
  * governance       — routine governance items (SLA reminders, dedup
                       cleanup, redundant escalations).
  * other            — anything that fell through. Board may vote, but a
                       category=other item is a hint the categorizer is
                       missing a rule.

USAGE
-----
    from chairman_decision_categorizer import classify, CATEGORIES
    cat = classify(item)                 # returns (name, board_may_ratify)
    if not cat.board_may_ratify: ...     # is a carve-out

The result carries the category NAME so callers can group by it; the
boolean is a convenience so ``sla.py`` can gate escalation without
re-reading the taxonomy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


PROTECTED_FILES = (
    "alpaca_trader.py",
    "main.py",
    "risk_manager_agent.py",
    "irm_review_agent.py",
    "board_meeting.py",
    "manager_agent.py",
)


@dataclass(frozen=True)
class Category:
    name: str
    board_may_ratify: bool
    label: str      # short human-facing label for the inbox banner


# Fields used to build the text a rule is matched against. Most rules use
# WIDE (everything) — but protected_file uses NARROW (no description). A
# protected filename mentioned casually inside a sentence ("~63 cron jobs
# including main.py on the same paper account") should NOT flag the item
# as a protected-file edit proposal. Only shape-carrying fields (title,
# proposed_fix, dedup_key, category) count for that carve-out.
FIELDS_WIDE:   tuple[str, ...] = (
    "id", "title", "description", "dedup_key", "source",
    "category", "proposed_fix",
)
FIELDS_NARROW: tuple[str, ...] = (
    "id", "title", "dedup_key", "category", "proposed_fix",
)


# Order matters: first match wins. The carve-outs are checked FIRST so a
# capital-allocation proposal that happens to mention "wiring" downstream
# does not slip into the auto-ratify bucket by accident.
# Each rule is (Category, needles, fields_to_search).
_RULES: list[tuple[Category, tuple[str, ...], tuple[str, ...]]] = [
    (
        Category("arm_gate", False, "🔒 ARM GATE"),
        (
            "kalshi_armed", "kalshi_mirror_armed", "fi_armed", ".fi_armed",
            "arm gate", "arm-gate", "arming decision", "arm the ",
            "arm kalshi", "arm the kalshi", "kalshi-phase-", "kalshi phase ",
            "-arm ", "_arm ", "arm mirror",
            "kill-switch", "kill switch", "flip enforce", "shadow_to_enforce",
            "phase 2 arm", "phase-2 arm", "mirror phase 2", "arm phase 2",
        ),
        FIELDS_WIDE,
    ),
    (
        Category("capital", False, "💰 CAPITAL"),
        (
            "capital allocation", "capital-allocation", "pod budget",
            "pod-budget", "reallocate capital", "increase budget",
            "position cap", "position-cap", "sizing cap", "sizing-cap",
            "raise cap ", "lower cap ",
        ),
        FIELDS_WIDE,
    ),
    (
        Category("hr", False, "👥 HR"),
        (
            "terminate agent", "fire agent", "pip verdict", "pip decision",
            "hr verdict", "hr-verdict", "hr_retirement",
            "keep-advocacy veto", "advocacy veto",
            "reinstate agent", "demote agent",
        ),
        FIELDS_WIDE,
    ),
    (
        Category("protected_file", False, "🛡  PROTECTED FILE"),
        # A protected filename appearing in the item's SHAPE fields — not
        # in casual description prose — flags it as a protected-file edit.
        tuple(f.lower() for f in PROTECTED_FILES),
        FIELDS_NARROW,
    ),
    (
        Category("wiring", True, "🔌 wiring"),
        (
            "sweep-agent_input_unproduced", "sweep-agent_output_unconsumed",
            "agent_input_unproduced", "agent_output_unconsumed",
            "unproduced", "unconsumed", "missing producer",
            "no consumer", "no reader", "wiring gap", "wiring contract",
        ),
        FIELDS_WIDE,
    ),
    (
        # cit_code intentionally BEFORE freshness: a CIT-authored staleness
        # item is a CIT item, not a freshness_sentinel item. Its fix path
        # is CIT triage → board proposal, not freshness_sentinel's queue.
        Category("cit_code", True, "🐛 cit-code"),
        (
            "apex_system_cit_agent", "apex-sys-cit", "cit_pipeline_integrity",
            "cit_director", "cit_code_inspection", "code inspection",
            "cit_self_improvement", "cit_data_integrity",
            "pip-integrity",              # cit_pipeline_integrity dedup prefix
        ),
        FIELDS_WIDE,
    ),
    (
        Category("freshness", True, "⏱  freshness"),
        (
            "hours old", "h old", "stale ", " stale",
            "freshness", "freshness_sentinel", "pipeline_heartbeat",
            "silent producer", "silent_producer",
        ),
        FIELDS_WIDE,
    ),
    (
        Category("governance", True, "⚖  governance"),
        (
            "sla-escalation", "sla_escalation", "repeated email",
            "email subjects", "dedup", "de-dup", "duplicate escalation",
            "chairman_decision_sla", "logic_officer",
        ),
        FIELDS_WIDE,
    ),
]

CATEGORIES = tuple(cat for cat, _, _ in _RULES) + (
    Category("other", True, "❓ other"),
)


def _haystack(item: dict, fields: tuple[str, ...] = FIELDS_WIDE) -> str:
    """The text a rule is matched against. Case-folded, so rules can be
    written in lower case. ``fields`` picks which of the item's keys
    contribute — most rules use FIELDS_WIDE, protected_file uses NARROW."""
    parts: list[str] = []
    for k in fields:
        v = item.get(k) or ""
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        parts.append(str(v))
    return " ".join(parts).lower()


def classify(item: dict) -> Category:
    """Return the single category this decision item belongs to. First-
    match-wins over ``_RULES`` in order; the fall-through is ``other``."""
    if not isinstance(item, dict):
        return CATEGORIES[-1]                       # other
    for cat, needles, fields in _RULES:
        hay = _haystack(item, fields)
        for n in needles:
            if n in hay:
                return cat
    return CATEGORIES[-1]                           # other


def is_carve_out(item: dict) -> bool:
    """True if the board must NOT auto-ratify this item, no matter how
    long it sits. The 15-day fallback in chairman_decision_sla.py reads
    this to decide whether to escalate."""
    return not classify(item).board_may_ratify


def group(items: Iterable[dict]) -> dict:
    """Group items by category name. Returns {name: [item, ...]}. Order
    of the dict follows CATEGORIES so the inbox prints carve-outs first."""
    out: dict = {c.name: [] for c in CATEGORIES}
    for it in items:
        out[classify(it).name].append(it)
    return out


if __name__ == "__main__":
    # A tiny self-check so `python3 chairman_decision_categorizer.py`
    # exits non-zero when a rule is broken, which the wiring audit runs.
    import sys
    checks = [
        ({"id": "kalshi-phase-2-arm", "title": "arm Kalshi mirror"},
         "arm_gate", False),
        ({"id": "hr-terminate-fi_analyst_2",
          "title": "terminate agent fi_analyst_2"}, "hr", False),
        ({"id": "cap-1", "title": "reallocate capital to ML pod"},
         "capital", False),
        ({"id": "prot-1", "title": "improve error handling in alpaca_trader.py"},
         "protected_file", False),
        ({"id": "sweep-agent_input_unproduced",
          "title": "Sweep: 13 finding(s) — inputs unproduced"},
         "wiring", True),
        ({"id": "apex-sys-cit-20260910-1300-edgar_cache_partially_stale",
          "title": "EDGAR cache partially stale"}, "cit_code", True),
        ({"id": "misc-1", "title": "nothing recognizable"},
         "other", True),
    ]
    fail = 0
    for item, want_name, want_ratify in checks:
        got = classify(item)
        if got.name != want_name or got.board_may_ratify != want_ratify:
            print(f"FAIL: {item['id']} -> {got.name} "
                  f"(ratify={got.board_may_ratify}), "
                  f"expected {want_name} (ratify={want_ratify})")
            fail += 1
    if fail:
        sys.exit(1)
    print(f"OK: {len(checks)} classification checks pass.")

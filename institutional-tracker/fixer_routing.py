"""fixer_routing — which specialist fixer owns a queued board fix.

Promoted from docs/fixers/fixer_routing_draft.py on build day (docs/fixer_build_plan.md).
Pure functions, no I/O except brief_path().

route(row, problem_text="") -> (area, rule) where area is one of AREAS and rule names the
matcher that fired. The fallback is "wiring", never None: an unrouted row is the firm's
main bug class (a producer emits something no consumer handles), so the default owner is
the fixer whose job is exactly that.

"chairman" is not a fixer. Capital, risk limits, the Kalshi arm gate and terminations go
to the Chairman queue and a fixer must refuse them.
"""
import re

AREAS = ("risk", "fi", "fe", "kalshi", "ml", "ops", "wiring", "research", "chairman")
# "research" is an eighth fixer the 2026-11-12 plan did not list: zero-pick / pod-output rows
# (13 of 103 live rows) belong to neither Risk nor Wiring.

# Order matters: first match wins. Chairman carve-outs come first so a capital row that
# also mentions "ML" can never be handed to the ML fixer.
_RULES = [
    ("chairman:capital", "chairman", r"idle pod capital|risk limit|capital allocation|terminat"),
    ("chairman:arm-gate", "chairman", r"\barm gate\b|\barm_gate\b"),
    ("prefix:fund-sanity", "fe", r"^fund-sanity-"),
    ("ops", "ops", r"cron job stale|disk usage|email buffer|email subjects|heartbeat|"
                   r"runs_match_schedule|unreachable|deployed_set_matches_repo|"
                   r"shipped_config_matches_repo|ERROR lines|mail_guard|catch_up|INDEX_GAP|"
                   r"None score|None canary|canary_state"),
    ("kalshi", "kalshi", r"kalshi|nested_probabilities"),
    ("risk:broker", "risk", r"positions_conserve|recorded_exits_are_executed|designated writer|"
                            r"unexplained_loss|below starting capital|alpaca|single_writer|"
                            r"signoff_ledger|\bCRO\b|loss needs corrective"),
    ("fi", "fi", r"\bfi_|fixed income"),
    ("fe", "fe", r"\bfe_|fe watchlist|company_research|first eagle|impossible fundamentals"),
    ("ml", "ml", r"\bML\b|predictive_model|feature_engineer|\bml_"),
    ("research", "research", r"zero_picks|zero picks|research pipeline|ETF\]|macro/etf|"
                             r"zero-pick|R&D|BUY_NEW pick|untradeable"),
    ("wiring", "wiring", r"sweep:|wiring|orphan|cit_director|unrouted|logic_team|board_dispatch|"
                         r"deputy board|test coverage|smoke tests|silent exception|"
                         r"agents never reviewed by HR"),
]
_COMPILED = [(n, a, re.compile(p, re.I)) for n, a, p in _RULES]


def route(row, problem_text=""):
    """Return (area, rule_name). Never returns an area outside AREAS.

    Two passes. The headline (id + title + the loop-sig problem text) is matched first;
    evidence only if the headline matches nothing. Evidence is a context dump: zero-pick
    rows quote the CRO level, so a single-pass match sent them to Risk (measured
    2026-11-13 on the VM queue: 10 misroutes from evidence alone)."""
    # Only the first line of a problem's text is its headline; loop-sig titles carry an
    # OBSERVED: dump after a blank line (a zero-picks one quotes the CRO level).
    first = str(problem_text or "").strip().splitlines()[:1]
    head = " ".join(str(x or "") for x in (
        row.get("proposal_id"), row.get("title"), *first))
    body = " ".join(str(x or "") for x in (row.get("source"), row.get("evidence")))
    for tag, hay in (("", head), ("evidence:", body)):
        for name, area, rx in _COMPILED:
            if rx.search(hay):
                return area, tag + name
    return "wiring", "fallback:unrouted"


# Areas that have a fixer. "chairman" is a destination, not a fixer: no brief, never claimed.
FIXER_AREAS = tuple(a for a in AREAS if a != "chairman")


def problem_text(row, history=None):
    """The text a fixer (and route()) should read for this row.

    loop-sig-* rows are filed by problem_lifecycle_sentinel with a generic headline
    ("[LOOP] Recurring problem needs a STRUCTURAL fix") — the actual problem lives in
    data/problem_lifecycle.json under the signature. Before 2026-11-19 the row carried
    no copy of it, so 60 queued rows on the VM were unactionable by anyone."""
    pid = str(row.get("proposal_id") or row.get("id") or "")
    if pid.startswith("loop-sig-") and isinstance(history, dict):
        h = history.get(pid[len("loop-"):]) or {}
        return str(h.get("title") or "")
    return ""


def brief_path(area, repo_root=None):
    """docs/fixers/<area>.md — the brief the fixer routine loads. None for 'chairman'."""
    from pathlib import Path
    if area not in FIXER_AREAS:
        return None
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    return root / "docs" / "fixers" / f"{area}.md"


def group_key(row, problem=""):
    """(area, normalised headline): rows with the same key are ONE problem and close
    with one PR. 106 open rows were ~50 problems on 2026-11-13; without grouping the
    routine spends its PR budget opening duplicates of one bug."""
    area = row.get("owner_area") or route(row, problem)[0]
    head = (problem or row.get("title") or "").strip().splitlines()[:1]
    head = head[0] if head else ""
    norm = re.sub(r"\d+(\.\d+)?", "#", head.lower())
    norm = re.sub(r"\s+", " ", norm).strip()[:90]
    return area, norm

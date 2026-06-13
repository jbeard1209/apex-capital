#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — WIRING AUDIT
# =============================================================================
# Sweeps for the firm's main bug class: a producer emits something (a finding
# rule, a routed proposal, a queue entry) that NO consumer ever handles, and
# the mismatch is silent because .get()/try-except swallow it.
#
# READ-ONLY. It reports; it never deletes or edits. (Dropping an "empty" file
# is unsafe — most code does _load(path, default) and an empty queue is a
# normal resting state, not proof of a dead integration. The signal is an
# empty file whose basename has NO reader in the codebase, or a routed target
# with no consumer. Those are what this flags.)
#
# Exit code: 0 if no hard contract violations, 1 if any are found (CI-friendly).
#
# Usage:
#   python3 wiring_audit.py            # full report
#   python3 wiring_audit.py --quiet    # only print problems
# =============================================================================

import argparse
import glob
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

problems = []   # hard contract violations -> nonzero exit
notes    = []   # soft observations (worth a look, not a failure)


def _py_sources() -> dict:
    out = {}
    for p in BASE_DIR.glob("*.py"):
        try:
            out[p.name] = p.read_text()
        except Exception:
            pass
    return out


def check_logic_rule_handlers():
    """Every rule logic_team.py can emit must resolve to a handler."""
    try:
        import logic_team_implementation_agent as ltia
    except Exception as e:
        problems.append(f"[logic-handlers] cannot import implementation agent: {e}")
        return
    src = (BASE_DIR / "logic_team.py").read_text()
    static = set(re.findall(r'rule="([a-z_]+)"', src))
    dynamic = re.findall(r'rule=f"([^"]+)"', src)
    samples = []
    for tmpl in dynamic:
        if "{pod_id}" in tmpl:
            samples += [tmpl.replace("{pod_id}", "fi"), tmpl.replace("{pod_id}", "fe")]
        else:
            samples.append(re.sub(r"\{[^}]+\}", "x", tmpl))
    unmatched = [r for r in sorted(static) + samples
                 if ltia._normalize_rule(r) not in ltia.RESOLUTION_HANDLERS]
    if unmatched:
        problems.append(f"[logic-handlers] rules with no handler: {sorted(set(unmatched))}")
    else:
        notes.append(f"[logic-handlers] OK — all {len(static)+len(samples)} emitted rules handled")


def check_pod_routing():
    """Every pod the router can target must have a live consumer queue."""
    try:
        import cit_director as cd
    except Exception as e:
        problems.append(f"[pod-routing] cannot import cit_director: {e}")
        return
    bad = []
    for pod in {"rd", "ml", "fi", "fe", "etf", "main"}:
        dest = cd._effective_pod({"title": f"audit-{pod}", "pod": pod,
                                  "description": "x", "source": "audit"})
        if not (dest == "main" or dest in cd._LIVE_POD_QUEUES):
            bad.append(f"{pod}->{dest}")
    if bad:
        problems.append(f"[pod-routing] proposals routed to dead queue: {bad}")
    else:
        notes.append("[pod-routing] OK — all routes land in a consumed queue")


def check_proposal_categories():
    """Board-proposal categories from LT/CIT must be in a deputy_board scope set."""
    try:
        import deputy_board as db
    except Exception as e:
        problems.append(f"[categories] cannot import deputy_board: {e}")
        return
    known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES | db.AUTO_IMPLEMENT_CATEGORIES)
    src = (BASE_DIR / "logic_team_implementation_agent.py").read_text()
    m = re.search(r'def _escalate_to_board\(.*?\n(?=def )', src, re.DOTALL)
    cats = set(re.findall(r'"category":\s*"([A-Z_]+)"', m.group(0) if m else ""))
    unknown = cats - known
    if unknown:
        problems.append(f"[categories] LTIA escalates with unrecognized category: {unknown}")
    else:
        notes.append("[categories] OK — escalation categories recognized by board")


def check_orphan_queues(sources: dict):
    """Empty queue/log/pending files whose basename has no reader = candidate dead integration."""
    patterns = ["*_pending.json", "*_queue.json", "*_log.json", "*pending*.json", "*queue*.json"]
    seen = set()
    for pat in patterns:
        for fp in DATA_DIR.glob(pat):
            if fp.name in seen:
                continue
            seen.add(fp.name)
            # size = total entries
            try:
                d = json.loads(fp.read_text())
                size = (sum(len(v) for v in d.values() if isinstance(v, (list, dict)))
                        if isinstance(d, dict) else len(d))
            except Exception:
                continue
            if size > 0:
                continue  # non-empty = clearly used
            stem = fp.stem  # filename without .json
            refs = [name for name, txt in sources.items() if stem in txt]
            if not refs:
                problems.append(f"[orphan] {fp.name} is EMPTY and referenced by NO .py file — dead file")
            elif len(refs) == 1:
                notes.append(f"[orphan?] {fp.name} EMPTY, referenced by only {refs[0]} "
                             f"(producer but maybe no consumer — verify)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    args = ap.parse_args()

    sources = _py_sources()
    check_logic_rule_handlers()
    check_pod_routing()
    check_proposal_categories()
    check_orphan_queues(sources)

    print("=" * 60)
    print("  APEX CAPITAL — WIRING AUDIT")
    print("=" * 60)
    if problems:
        print(f"\n  ❌ {len(problems)} CONTRACT VIOLATION(S):")
        for p in problems:
            print(f"     {p}")
    else:
        print("\n  ✅ No hard contract violations.")
    if notes and not args.quiet:
        print(f"\n  Notes ({len(notes)}):")
        for n in notes:
            print(f"     {n}")
    print()
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()

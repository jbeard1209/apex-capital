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


def check_glob_consumers_have_producers(sources: dict):
    """A consumer that globs/loads a data file pattern nothing produces is blind.

    This is the exact class of the blind AUC monitor: logic_team.py globbed
    `ml_model_report*.json`, a file no code ever writes and that never existed,
    so the metric silently defaulted to 0 and the alarm could never fire.
    """
    # Find glob.glob(... "<stem>*.json") read sites
    glob_pat = re.compile(r'glob\.glob\([^)]*?"([A-Za-z0-9_]+)\*\.json"')
    for name, txt in sources.items():
        for stem in glob_pat.findall(txt):
            # Is there a producer? i.e. any .py that writes a file starting with stem
            produced = any(
                re.search(rf'"{stem}[A-Za-z0-9_]*\.json"', wtxt) and
                re.search(r'open\([^)]*"w"|json\.dump|\.write_text|_save', wtxt)
                for wtxt in sources.values())
            # Or any matching file already on disk
            on_disk = bool(list(DATA_DIR.glob(f"{stem}*.json")))
            if not produced and not on_disk:
                problems.append(
                    f"[blind-read] {name} globs '{stem}*.json' but NO code produces it "
                    f"and none exist on disk — this consumer is blind (silent default)")


def check_orphan_agents(sources: dict):
    """An AGENT that produces a report nobody ever runs — the orphan-agent variant
    of the producer/consumer bug class.

    check_orphan_queues() can only see files that ALREADY EXIST and match a
    queue/log/pending glob. It is structurally blind to a producer that has never
    run even once: no file, no glob match, nothing to flag. That blindness let
    international_analyst.py — which tracks ^KS11 (KOSPI) and had FIVE wired
    consumers — sit unscheduled and crash-on-import for the firm's entire history,
    producing zero reports. The firm was therefore blind to the July 2026 Korean
    market crash despite owning exactly the right agent for it.

    Detection: build the reachability closure over the import graph starting from
    the real scheduler roots (main.py + the cron manifest). Any module that writes
    a data/*.json output which other modules READ, but that is NOT reachable from
    any scheduler root, is an agent that never comes to work.
    """
    found_orphans = set()
    roots = {"main.py"}
    cron = BASE_DIR / "new_crontab.txt"
    cron_txt = cron.read_text() if cron.exists() else ""
    for name in sources:
        if name in cron_txt:
            roots.add(name)

    # Fixpoint over "module A mentions module B's stem" (import or subprocess call).
    reachable, frontier = set(roots), set(roots)
    while frontier:
        nxt = set()
        for cur in frontier:
            txt = sources.get(cur, "")
            for name in sources:
                if name in reachable:
                    continue
                stem = name[:-3]
                if re.search(rf"\b{re.escape(stem)}\b", txt):
                    nxt.add(name)
        reachable |= nxt
        frontier = nxt

    # Which modules declare a data/*.json output, and who reads each output?
    # Tolerate a directory prefix: both os.path.join(DATA_DIR, "x.json") and the
    # equally common "data/x.json" must match, or the check silently misses half
    # the producers in the codebase.
    out_re = re.compile(r'["\'](?:[\w.\-/]*/)?([a-z0-9_]+\.json)["\']')
    for name, txt in sources.items():
        if name in reachable or name.startswith("test_"):
            continue
        # only consider modules that look like agents writing a report
        if not re.search(r'open\([^)]*,\s*["\']w["\']|\.write_text\(|json\.dump\(', txt):
            continue
        outs = {m for m in out_re.findall(txt) if "data" in txt.split(m)[0][-120:]}
        for out in outs:
            readers = [n for n, t in sources.items()
                       if n != name and not n.startswith("test_") and out in t]
            if not readers:
                continue
            exists = (DATA_DIR / out).exists()
            msg = (
                f"[orphan-agent] {name} writes {out} (read by {len(readers)} module(s): "
                f"{', '.join(sorted(readers)[:3])}) but is NOT reachable from main.py or "
                f"the cron manifest — it is scheduled NOWHERE"
                + ("" if exists else f", and {out} has NEVER been produced")
            )
            # RATCHET: the 11 orphans this check found on day one are pre-existing
            # and are with the full board for SCHEDULE/RETIRE/DELETE triage
            # (ARCH-2026-0730-orphan-agents). Hard-failing on them would block every
            # push until that triage lands — including the push that ships this very
            # detector. So known orphans are loud notes, and only NEW ones are hard
            # failures. The list may only ever shrink: see check_orphan_baseline_shrinks().
            (notes if name in _orphan_baseline() else problems).append(
                msg + (" [known — pending board triage]" if name in _orphan_baseline() else "")
            )
            found_orphans.add(name)
            break   # one finding per orphan module is enough

    # A module that got fixed must be REMOVED from the baseline, or the ratchet
    # silently loosens and a future regression would be waved through.
    stale = _orphan_baseline() - found_orphans
    for name in sorted(stale):
        problems.append(
            f"[orphan-agent] {name} is in {_BASELINE_PATH.name} but is no longer an orphan — "
            f"remove it from the baseline so the ratchet cannot loosen")


_BASELINE_PATH = BASE_DIR / "data" / "orphan_agents_baseline.json"


def _orphan_baseline() -> set:
    """Modules already known to be unscheduled, accepted only until the board triages
    them. New entries must never be added to silence a finding — that is the exact
    'flagged but never solved' loop the firm has been burned by."""
    try:
        d = json.loads(_BASELINE_PATH.read_text())
        return set(d.get("known_orphans", []))
    except Exception:
        return set()


def run_checks() -> dict:
    """Run all wiring checks and return {problems, notes} without exiting.

    Reusable by both the CLI (main) and the Logic Team's WiringContractLogicAgent
    so the firm self-detects this bug class.
    """
    global problems, notes
    problems, notes = [], []
    sources = _py_sources()
    check_logic_rule_handlers()
    check_pod_routing()
    check_proposal_categories()
    check_glob_consumers_have_producers(sources)
    check_orphan_queues(sources)
    check_orphan_agents(sources)
    return {"problems": list(problems), "notes": list(notes)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    args = ap.parse_args()

    run_checks()

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

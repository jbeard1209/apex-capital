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


def _code_only(txt: str) -> str:
    """Module text with docstrings and # comments stripped.

    Ordinary string literals are KEPT on purpose: a scheduler root invokes work
    as subprocess.run(["python3", "some_agent.py"]), so dropping every string
    would blind the reachability walk to exactly the calls it exists to find.
    """
    txt = re.sub(r'""".*?"""', '', txt, flags=re.S)
    txt = re.sub(r"'''.*?'''", '', txt, flags=re.S)
    return re.sub(r'#[^\n]*', '', txt)


def _declared_outputs(txt: str, out_re, writer_stems=()) -> list:
    """The data/*.json files a module actually WRITES, in stable order.

    2026-07-30: this used to be `{every .json literal near the word "data"}`, which
    conflated inputs with outputs and then iterated a SET — so the same module was
    reported as writing a different file on each run (Python randomises str hashing
    per process). dynamic_position_sizer.py was reported as writing positions.json,
    which it only READS; it writes position_sizes.json. The set of flagged agents was
    right, but every explanation naming a file was a coin flip, and a triager chasing
    the named artifact would find a file the agent never touches.

    So: resolve `SOME_PATH = .../"x.json"` constants, then keep a filename only if it
    (or its variable) appears at a write site. If nothing resolves, fall back to the
    old broad set — sorted — so a parsing miss loses determinism, not findings.
    """
    var_of = {}                      # VARNAME -> filename.json
    for m in re.finditer(r'^\s*([A-Za-z_]\w*)\s*=\s*([^\n]+)$', txt, re.M):
        hit = out_re.search(m.group(2))
        if hit:
            var_of[m.group(1)] = hit.group(1)

    patterns = [r'open\(([^)]*),\s*["\']w["\']', r'([\w.\[\]"\']+)\.write_text\(',
                r'json\.dump\((?:[^()]|\([^()]*\))*\)', r'os\.replace\(([^)]*)\)']
    # Handing the write to a shared helper is still a write, and the path the
    # caller passes is still its declared output. Must be a CALL — `helper.fn(` —
    # not a bare mention: chairman_inbox.py's docstring says "companion to
    # chairman_decision_sla.py" and is a pure reader.
    if writer_stems:
        alt = "|".join(re.escape(s) for s in sorted(writer_stems))
        patterns.append(rf'(?:{alt})\.\w+\((?:[^()]|\([^()]*\))*\)')

    write_ctx = [m.group(0) for m in re.finditer("|".join(patterns), txt)]
    blob = "\n".join(write_ctx)

    written = set()
    for var, fname in var_of.items():
        if re.search(rf'\b{re.escape(var)}\b', blob):
            written.add(fname)
    for fname in out_re.findall(blob):
        written.add(fname)

    if written:
        return sorted(written)
    return sorted({m for m in out_re.findall(txt) if "data" in txt.split(m)[0][-120:]})


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
    # Scheduler ground truth = the VM's LIVE crontab, mirrored into
    # data/vm_crontab_snapshot.txt by scripts/refresh_crontab_snapshot.sh.
    #
    # 2026-07-30: this used to read new_crontab.txt alone — a file whose own header
    # says "THE VM'S LIVE CRONTAB IS CANONICAL FOR PROD — this file is the Mac-path
    # template". It had drifted 10 agents behind prod, so agents that run every four
    # hours in production (data_integrity_sentinel, tech_short_pod, problem_lifecycle_
    # sentinel, ...) were being reported as scheduled NOWHERE. An audit that judges
    # reality against a stale document produces confident, wrong findings.
    #
    # 2026-08 (crontab consolidation): new_crontab.txt and gcp_setup/vm_crontab.txt
    # are now demoted, hand-maintained templates that are NEVER read here — merging
    # them in used to make an agent that was removed from prod but left in a stale
    # template look "still scheduled", which is a false negative in the same
    # direction as the false positive above. The snapshot alone is ground truth
    # when it exists; new_crontab.txt is a last-resort fallback ONLY so a fresh
    # checkout with no snapshot yet doesn't flag every agent as orphaned.
    snap = BASE_DIR / "data/vm_crontab_snapshot.txt"
    cron_txt = snap.read_text() if snap.exists() else (
        (BASE_DIR / "new_crontab.txt").read_text()
        if (BASE_DIR / "new_crontab.txt").exists() else "")
    for name in sources:
        # Test files are NOT scheduler roots even when cron runs them: a test
        # importing an agent does not mean the agent does its job in production.
        # (It also stops a test that merely *names* a module from making that
        # module look reachable — which is how this check first fooled itself.)
        if name in cron_txt and not name.startswith("test_"):
            roots.add(name)

    # Fixpoint over "module A mentions module B's stem" (import or subprocess call).
    #
    # Test files are excluded from the traversal ENTIRELY, not just from the root
    # seeding above. 2026-07-30: barring them as roots was only half the guard —
    # a test could still be dragged into `reachable` by any production module that
    # merely mentioned its name, and once inside, the frontier expanded from its
    # text like any other module. Adding a 12-name WIRED_AGENTS list to
    # test_pipeline_smoke.py therefore silently reclassified NINE baselined orphans
    # as "scheduled", and the audit dutifully asked to delete them from the
    # baseline — the ratchet would have loosened itself on the strength of a list
    # that exists only inside a test. Being named by a test is not employment.
    #
    # And the traversal reads CODE, not prose. A docstring naming a module is
    # not a call to it. 2026-07-31: chairman_queue.py's docstring explains which
    # five agents used to carry duplicated copies of its merge; naming
    # apex_system_cit_agent there was enough to pull that agent into `reachable`
    # via a scheduled caller, and the audit promptly reported it "no longer an
    # orphan" and asked for its removal from the baseline. It is still scheduled
    # nowhere. Being named in a docstring is not employment either.
    prod = {n for n in sources if not n.startswith("test_")}
    code = {n: _code_only(t) for n, t in sources.items()}
    reachable, frontier = set(roots), set(roots)
    while frontier:
        nxt = set()
        for cur in frontier:
            txt = code.get(cur, "")
            for name in prod:
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
    write_re = re.compile(r'open\([^)]*,\s*["\']w["\']|\.write_text\(|json\.dump\(')

    # A module that DELEGATES its write to a shared helper is still a producer.
    # This gate used to test for a literal json.dump()/open(...,"w") in the
    # module's own text, so on 2026-07-31 — when five agents' duplicated queue
    # writes were consolidated into chairman_queue.py — apex_system_cit_agent.py
    # stopped matching and the audit announced it was "no longer an orphan" and
    # asked for its removal from the baseline. It had not been scheduled; the
    # detector had merely stopped recognising it. That is the exact false
    # all-clear the ratchet exists to prevent, and it would have loosened the
    # net on the strength of a refactor.
    #
    # So: a module counts as a producer if it writes directly, or if it names a
    # module that writes directly AND the two declare a common output. Requiring
    # the shared output keeps this precise — merely importing something that
    # happens to write a file is not producing that file.
    writer_stems = {n[:-3] for n, t in sources.items()
                    if not n.startswith("test_") and write_re.search(t)}

    for name, txt in sources.items():
        if name in reachable or name.startswith("test_"):
            continue
        others = writer_stems - {name[:-3]}
        call_re = re.compile(r'(?:%s)\.\w+\(' % "|".join(re.escape(s) for s in sorted(others))) \
            if others else None
        if not write_re.search(txt) and not (call_re and call_re.search(txt)):
            continue
        outs = _declared_outputs(txt, out_re, writer_stems=others)
        for out in outs:
            # Readers are counted in code too. This file's own docstrings name
            # position_sizes.json while explaining a past bug, which was enough
            # to make wiring_audit.py the sole "reader" of dynamic_position_sizer
            # and promote it to a hard failure. A tool must not be a consumer of
            # the thing it audits by virtue of describing it.
            readers = [n for n, t in code.items()
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
            # failures. The list may only ever shrink — enforced by the `stale`
            # check below, which fails if a baselined entry stops being an orphan.
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

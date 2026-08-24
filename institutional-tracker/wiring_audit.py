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
import ast
import glob
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent


def _data_dir() -> Path:
    """data/ resolved from BASE_DIR *now*, never at import.

    This was `DATA_DIR = BASE_DIR / "data"`, and that is a frozen container: the
    smoke tests aim this module at a sandbox by rebinding `wiring_audit.BASE_DIR`,
    and a path derived at import time does not move with it. The sweep then read
    the sandbox's sources and the REAL firm's data/ in the same pass — half the
    answer about one repo, half about another, with no error anywhere. Same shape
    as `_cron_snapshot` and `_baseline_path` below, and as memory
    `frozen-container-of-derived-paths`; a module that supports being re-rooted
    has to derive every path from the root on each call.
    """
    return BASE_DIR / "data"

# The canonical way this codebase recognises a data/*.json reference and a write
# site, hoisted to module level 2026-08-01 so agent_sweep.py can IMPORT them
# rather than keep its own copy. It kept one for about an hour: a near-identical
# regex that required a literal `data/` prefix, which made every path built as
# DATA / "x.json" invisible and reported six well-wired agents as speaking to
# nobody. Two definitions of "an output" give the firm two answers about who
# talks to whom, which is the same shape as the three definitions of "retired".
DATA_JSON_RE = re.compile(r'["\'](?:[\w.\-/]*/)?([a-z0-9_]+\.json)["\']')
WRITE_RE = re.compile(r'open\([^)]*,\s*["\']w["\']|\.write_text\(|json\.dump\(')

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
        for fp in _data_dir().glob(pat):
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
            on_disk = bool(list(_data_dir().glob(f"{stem}*.json")))
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


# The callables that actually START another module. A module name inside any
# OTHER call is data, not employment.
_INVOKERS = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "system",
    "spawnl", "spawnv", "spawnlp", "spawnvp",
    "run_path", "run_module", "import_module", "__import__",
    "spec_from_file_location",
})


def _invocation_edges(txt: str, stems: set) -> tuple:
    """Which module stems does this one IMPORT or RUN — as opposed to merely name?

    Returns (edges, unresolved). `unresolved` is True when the module dispatches
    to a target this cannot read statically (a variable, an f-string, a table
    lookup); the caller must then fall back to the loose text rule for that
    module, because "I cannot tell who you run" must never be reported as "you
    run nobody" (memory `verdict-with-no-consequence`).

    WHY THIS EXISTS. The reachability walk used to count ANY mention of a module
    stem as employment. `_code_only` strips docstrings and comments for exactly
    that reason, and its own note explains why ordinary strings are kept: a
    scheduler root invokes work as subprocess.run(["python3", "x.py"]), so
    dropping every string would blind the walk. True — but keeping every string
    let a module be "employed" by appearing in a plain data table:

      • wake_agent.py's AGENT_REGISTRY lists ~50 scripts by filename. A wake is
        on-demand and nothing schedules it; being wakeable is not employment.
      • hr_agent.py's _GOVERNANCE_CHAIN is a frozenset of the officers whose work
        HR has never reviewed. The list exists to say "nobody audits these" — and
        naming them there was what told this audit they were employed. A list of
        the unwatched, read as proof of watching.
      • strategic_roadmap.py describes agents in prose fields; apex_system_cit_
        agent.py tables their log paths for a health check.

    On 2026-08-21 that made 25 agents look scheduled while running on no crontab,
    no launchd job, no systemd timer, no catch_up entry and no pipeline step —
    on either host. Among them: position_risk_monitor (the circuit breaker),
    deputy_monitor (stop-loss enforcement, and the owner internal_audit_agent
    records for the severity_below_the_escalation_floor failure class), and
    cit_watchdog. The baseline ratchet stood at 3.

    This is the third variant of one bug, and the two above it are already
    written into this function's caller: being named in a test is not employment
    (2026-07-30), being named in a docstring is not employment (2026-07-31),
    being named in a string literal is not employment (today).
    """
    edges, unresolved = set(), False
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        # Unparseable: claim nothing, and say so — the caller stays conservative.
        return set(), True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                edges.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                edges.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            f = node.func
            fname = (f.attr if isinstance(f, ast.Attribute)
                     else f.id if isinstance(f, ast.Name) else "")
            if fname not in _INVOKERS:
                continue
            hit, dynamic = False, False
            for arg in list(node.args) + [k.value for k in node.keywords if k.value]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        for s in stems:
                            if s in sub.value:
                                edges.add(s)
                                hit = True
                    elif isinstance(sub, (ast.Name, ast.Attribute,
                                          ast.Subscript, ast.JoinedStr)):
                        dynamic = True
            # sys.executable is a Name and appears in almost every subprocess
            # call here, so "contains a non-constant" alone would mark nearly
            # every dispatcher unresolved and preserve the blindness. A call is
            # only unreadable when it resolves to NO literal module at all.
            if dynamic and not hit:
                unresolved = True
    return edges, unresolved


# The firm's documented escalation entry points: call one and your item lands in
# a queue a board, pod or supervisor actually drains (see CLAUDE.md's
# producer -> queue -> consumer section). {owner_module: (function, ...)}.
#
# Keyed by owner so a module that DEFINES one of these is never accused of
# calling it — cit_director.py defines submit_to_pod_queue; it does not escalate
# to itself. Everything else is matched on the bare function name, because
# `from cit_director import submit_to_pod_queue` is how callers actually write
# it and a `<module>.<fn>(` pattern misses that entirely.
ESCALATION_ENTRYPOINTS = {
    "cit_director":   ("submit_to_pod_queue", "trigger_rd_research",
                       "route_proposal_to_pod"),
    "chairman_queue": ("submit",),
    "firm_channel":   ("post_task_request", "request_cos_meeting"),
}


def _escalation_path(name: str, code_txt: str):
    """The escalation entry point this module calls, or None.

    `code_txt` must be comment-stripped: a docstring explaining that some agent
    "escalates via submit_to_pod_queue" is not a call, and this check has been
    fooled by prose twice already (see check_orphan_agents' traversal notes).
    """
    stem = name[:-3]
    for owner, fns in ESCALATION_ENTRYPOINTS.items():
        if stem == owner:
            continue          # defining it is not calling it
        for fn in fns:
            if re.search(rf"(?<![\w.]){re.escape(fn)}\s*\(", code_txt) or \
               re.search(rf"\b{re.escape(owner)}\.{re.escape(fn)}\s*\(", code_txt):
                return f"{owner}.{fn}()"
    return None


#: The shapes that are, on their face, a write to a file. Shared with
#: _local_write_helpers so "what counts as a write" is defined once — a helper
#: recognised by one rule and missed by the other is how a module ends up
#: half-classified.
_WRITE_SITE_PATTERNS = (
    r'open\(([^)]*),\s*["\']w["\']',
    # `.write_text(` on ANY expression, not just a bare name. kalshi_scanner's
    # helper is `Path(path).write_text(...)`; the old `[\w.\[\]"\']+` stopped at
    # the closing paren of `Path(path)` and matched nothing, so the module's only
    # write was invisible and every file it merely read became a fabricated output.
    r'[\w.\[\]"\'()]+\.write_text\(',
    r'json\.dump\((?:[^()]|\([^()]*\))*\)',
    r'os\.replace\(([^)]*)\)',
)


def _local_write_helpers(tree) -> set:
    """Names of functions defined IN this module whose body writes a file.

    `writer_stems` already covers handing the write to another module. This
    covers the far commoner shape: a five-line `_save(path, payload)` at the top
    of the file that every write in the module goes through. Without it such a
    module has zero visible write sites, which sends it to the fallback below —
    and the fallback's guess about a module that writes through a helper is that
    everything it READS is an output.

    Resolved to a fixpoint so a helper that delegates to another helper counts.
    """
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                bodies[node.name] = ast.unparse(node)
            except Exception:
                pass
    base = re.compile("|".join(_WRITE_SITE_PATTERNS))
    helpers = {n for n, b in bodies.items() if base.search(b)}
    while helpers:
        alt = "|".join(re.escape(s) for s in sorted(helpers))
        grown = {n for n, b in bodies.items()
                 if n not in helpers and re.search(rf'(?<![\w.])(?:{alt})\s*\(', b)}
        if not grown:
            break
        helpers |= grown
    return helpers


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

    patterns = list(_WRITE_SITE_PATTERNS)
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

    # Calls to this module's own write helpers, resolved through the AST rather
    # than a regex. The regex form cannot be made to work here: the first
    # argument is the path but the LAST is arbitrary Python, and
    # `_save(QUEUE_PATH, list(queue_by_id.values()))` nests two levels deep —
    # every balanced-paren pattern short of a real parser drops it silently, and
    # a silently dropped write is exactly a fabricated producer edge.
    helpers = set()
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        tree = None
    if tree is not None:
        helpers = _local_write_helpers(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in helpers and node.args):
                continue
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in var_of:
                written.add(var_of[target.id])
                continue
            for sub in ast.walk(target):     # `_save_json(os.path.join(D, "x.json"), …)`
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    hit = out_re.search('"%s"' % sub.value)
                    if hit:
                        written.add(hit.group(1))

    if written:
        return sorted(written)

    # The fallback is a hedge against a PARSING MISS — "we found no write sites,
    # so we probably failed to recognise one". It must not fire when write sites
    # were found and simply produced no JSON: that is not a miss, it is the
    # answer. A module that writes an .html page and reads a dozen .json files is
    # a pure consumer, and running the fallback on it reports the file it reads
    # MOST as the file it writes.
    #
    # Found 2026-08-01 by reentry_slate.py, which reads hr_history.json and was
    # reported as writing it — a fabricated producer→consumer edge, raised as a
    # CONTRACT VIOLATION, headed for the board. Same failure the docstring above
    # already describes for dynamic_position_sizer.py, one layer further down:
    # an auditor that guesses is worse than one that abstains, because the guess
    # arrives with the authority of a finding. See memory `auditor_ground_truth`.
    #
    # But the hedge earns its keep: measured across the tree, 41 modules rely on
    # it because their JSON writes go through helpers this regex cannot follow.
    # Suppressing it whenever ANY write site exists would blind the audit to a
    # third of the firm. So suppress it only when EVERY write target resolves to
    # a known non-JSON file — then it is not a parsing miss, it is a module that
    # demonstrably writes something else. One unresolvable target and the hedge
    # stays on.
    if write_ctx and _writes_only_non_json(write_ctx, txt, out_re):
        return []

    # The other half of the same rule, and the one the paragraph above missed:
    # a module with NO write site of any kind and NO write helper of its own is
    # not a parsing miss either. It is a pure reader, and a pure reader is the
    # clearest case there is of "not a producer" — yet it was the one case that
    # fell straight through to the guess.
    #
    # Measured 2026-08-18 before the fix: 19 modules in that state were credited
    # with 75 producer edges they do not have, and 13 data files with no real
    # writer anywhere were shielded from agent_sweep's AGENT_INPUT_UNPRODUCED by
    # a fabricated one. irm_board/pipeline_ops_analyst.py — 19 reads, zero
    # writes — was recorded as the producer of fe_pipeline_report.json, a file
    # nothing in the firm has ever written, which is why nobody noticed that the
    # IRM's FE health check reads a filename that does not exist.
    # ...but "no write site and no LOCAL helper" is not yet the same thing as a
    # pure reader, and the first version of this rule conflated them. A module
    # whose only write is `_atomic(REPORT, payload)` — helper imported, defined
    # elsewhere — has no write site the regex can see and no helper this module
    # defines, and would have been declared a pure reader and credited with
    # nothing. That is the 41-producer blinding the docstring above warns about,
    # arriving through a different door.
    #
    # The discriminator is the shape of the call, not the name of the callee
    # (naming it would be a guess, and guesses are what this function exists to
    # stop): a call whose RESULT IS DISCARDED and whose FIRST argument is a
    # data/*.json path is `write(path, payload)` and cannot be anything else — a
    # read whose return value is thrown away does nothing. `json.load(open(P))`
    # and `x = _load(P, {})` keep their results and are unaffected.
    if not write_ctx and not helpers and not _opaque_write_call(tree, var_of, out_re):
        return []
    return sorted({m for m in out_re.findall(txt) if "data" in txt.split(m)[0][-120:]})


def _opaque_write_call(tree, var_of, out_re) -> bool:
    """True if some statement calls an unrecognised helper on a data/*.json path.

    Deliberately returns a BOOLEAN and not the filename. Resolving it precisely
    would look like an upgrade and would in fact be a quiet narrowing: the
    module's other writes still go through helpers nothing here can follow, so
    naming this one path would replace "we could not tell, keep the hedge" with
    "these are the outputs" — the same overconfidence, one layer up. The hedge
    stays on; this only decides whether it is switched off.
    """
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        args = node.value.args
        if not args:
            continue
        first = args[0]
        if isinstance(first, ast.Name) and first.id in var_of:
            return True
        if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and out_re.search('"%s"' % first.value)):
            return True
    return False


#: Extensions a module can write that are, by definition, not a JSON handoff to
#: another module. Kept explicit: an unknown extension must read as "unresolved"
#: so the fallback stays on, never as "safe to ignore".
_NON_JSON_EXT = (".html", ".htm", ".csv", ".txt", ".md", ".log", ".png", ".svg")


def _writes_only_non_json(write_ctx, txt, out_re) -> bool:
    """True when every detected write site provably targets a non-JSON file.

    Conservative on purpose. A target we cannot resolve returns False, which
    leaves the broad fallback running — the audit keeps guessing rather than
    going quiet, and a guess is recoverable where a blind spot is not.
    """
    var_ext = {}
    for m in re.finditer(r'^\s*([A-Za-z_]\w*)\s*=\s*([^\n]+)$', txt, re.M):
        hit = re.search(r'["\']([\w./-]+\.\w+)["\']', m.group(2))
        if hit:
            var_ext[m.group(1)] = hit.group(1)

    for site in write_ctx:
        if out_re.search(site):
            return False                       # writes a data/*.json outright
        names = re.findall(r'[A-Za-z_]\w*', site)
        target = next((n for n in names if n in var_ext), None)
        literal = re.search(r'["\']([\w./-]+\.\w+)["\']', site)
        resolved = var_ext.get(target) if target else (
            literal.group(1) if literal else None)
        if not resolved or not resolved.lower().endswith(_NON_JSON_EXT):
            return False                       # unresolved — do not go quiet
    return True


def _cron_snapshot() -> Path:
    """The live-crontab snapshot, resolved from BASE_DIR at call time. See _data_dir."""
    return BASE_DIR / "data" / "vm_crontab_snapshot.txt"


def scheduler_reachable(sources: dict, code: Optional[dict] = None,
                        by_root: bool = False, strict: bool = False,
                        cron_text: Optional[str] = None):
    """Modules reachable from a real scheduler root, or None if that is unknowable.

    `strict=True` drops the loose-name fallback below and follows only RESOLVED
    invocation edges. Not a stylistic preference — the same over-approximation
    is safe for one question and reckless for the other, and both callers live
    in this file:

      * "does this module run AT ALL?" (check_orphan_agents). Over-reaching
        means fewer orphan accusations. The error lands on not-accusing, which
        is where an error belongs when the penalty is calling a working agent
        dead.
      * "how OFTEN does it run?" (effective_gaps). Over-reaching means every
        module inherits the cadence of the fastest thing that merely mentions
        it, and the answer comes back reassuring. `canary_agent.py` runs every
        30 minutes and names practically every module in the firm because it
        health-checks them — under the loose rule it "reached" `main.py`
        itself, so the pipeline's real worst gap of 65.5h was reported as
        0.5h. A gap check that inherits a monitor's heartbeat says the firm is
        fast on the strength of the thing that was only WATCHING it, and the
        weekend blind window it was written to find disappears.

    So the loose rule stays where a false pass is the cheap error and is
    refused where a false pass is the expensive one.

    `by_root=True` returns `{root: reachable_set}` instead of the flat union.
    Same walk, second view — deliberately NOT a second function. The cadence
    check below needs to know WHICH root reaches a module (a detector inherits
    the schedule of whatever runs it), and computing that with its own traversal
    would put two copies of these rules in one file, which is the failure this
    docstring already warns about one paragraph down.

    Extracted from check_orphan_agents on 2026-08-22 so there is exactly ONE
    implementation of "does this actually run". internal_audit_agent's census
    needs the same answer about the detectors it names, and the alternative was a
    second copy of this walk — which is the `both-sides-of-a-comparison` bug in
    its purest form: two hand-written traversals that agree today, diverge on the
    first fix applied to one of them, and disagree with no error. The rules below
    were each paid for by a real false verdict (see check_orphan_agents' notes);
    a second copy would not inherit any of them.

    None means CANNOT TELL, not "nothing is scheduled". With no crontab snapshot
    the walk knows only main.py, so every cron-only module would look orphaned —
    callers must fail closed and judge nobody rather than accuse the whole firm.
    """
    # `cron_text` is injectable so a test can pose a schedule instead of
    # inheriting whichever crontab the running host happens to hold. A test
    # that reads host state answers a question about the host, not about the
    # code — that is host divergence #8, and it was found in production, not
    # by the two-zone push gate.
    if cron_text is None:
        snapshot = _cron_snapshot()
        if not snapshot.exists():
            return None
        cron_text = snapshot.read_text()
    cron_txt = cron_text
    if code is None:
        code = {n: _code_only(t) for n, t in sources.items()}
    # Test files are NOT scheduler roots even when cron runs them: a test
    # importing an agent does not mean the agent does its job in production.
    # (It also stops a test that merely *names* a module from making that
    # module look reachable — which is how this check first fooled itself.)
    roots = {"main.py"} | {n for n in sources
                           if n in cron_txt and not n.startswith("test_")}
    prod = {n for n in sources if not n.startswith("test_")}
    stems = {n[:-3] for n in prod}
    edges, loose = {}, set()
    for n in prod:
        e, unresolved = _invocation_edges(code.get(n, ""), stems)
        edges[n] = e
        if unresolved:
            loose.add(n)
    # Successors, resolved ONCE. by_root=True walks from every root, and the
    # loose-name regex used to be re-run per root per frontier node — the same
    # ~200x200 sweep sixty times over, which took wiring_audit from seconds to
    # minutes. The answer does not depend on the root, so it is computed here.
    succ = {}
    for cur in prod | roots:
        txt = code.get(cur, "")
        e = edges.get(cur, ())
        s = {n for n in prod if n[:-3] in e}
        if cur in loose and not strict:
            s |= {n for n in prod
                  if re.search(rf"\b{re.escape(n[:-3])}\b", txt)}
        succ[cur] = s

    def _walk(seed):
        reachable, frontier = set(seed), set(seed)
        while frontier:
            nxt = set()
            for cur in frontier:
                nxt |= succ.get(cur, set()) - reachable
            reachable |= nxt
            frontier = nxt
        return reachable

    if by_root:
        return {r: _walk({r}) for r in roots}
    return _walk(roots)


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
    code = {n: _code_only(t) for n, t in sources.items()}
    reachable = scheduler_reachable(sources, code)
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
    # 2026-08-01 (crontab consolidation, Chairman directive 2026-07-30: "three
    # crontab documents, one truth... find a solution that is both most efficient
    # and most accurate"). There is now exactly ONE schedule document and it is
    # GENERATED, never hand-maintained: new_crontab.txt and gcp_setup/vm_crontab.txt
    # are deleted. A hand-maintained second copy drifts in both directions — an
    # agent added to prod looks orphaned, and an agent removed from prod looks
    # "still scheduled" — and a document that only a human remembers to update is
    # the thing this audit kept measuring reality against.
    #
    # And there is no fallback. A missing snapshot used to fall back to the stale
    # template, which is silently answering a question you could not see the answer
    # to — the failure mode this firm has been burned by most. Fail loud instead:
    # with no snapshot the reachability walk knows only main.py, so EVERY cron-only
    # agent would be branded an orphan and the ratchet would fire on the whole
    # firm. Report that as a hard problem and judge nobody.
    if reachable is None:
        problems.append(
            "[orphan-agent] data/vm_crontab_snapshot.txt is missing — the schedule "
            "ground truth is unreadable, so no agent can be judged scheduled or "
            "orphaned. Run scripts/refresh_crontab_snapshot.sh")
        return

    # The walk itself now lives in scheduler_reachable() so the census in
    # internal_audit_agent asks the identical question of the detectors it names.
    # The rules it enforces, and what each one cost, are below.
    #
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
    #
    # And the traversal reads INVOCATIONS, not mentions. See _invocation_edges:
    # a module stem sitting in a registry table, a governance list or a roadmap
    # string is data, and counting it as employment hid 25 unscheduled agents
    # behind a baseline that read 3. Where a module dispatches to a target this
    # cannot resolve statically, `unresolved` is True and that module alone
    # reverts to the loose text rule — an unreadable dispatcher must not be able
    # to orphan its own children.
    # Which modules declare a data/*.json output, and who reads each output?
    # Tolerate a directory prefix: both os.path.join(DATA_DIR, "x.json") and the
    # equally common "data/x.json" must match, or the check silently misses half
    # the producers in the codebase.
    out_re, write_re = DATA_JSON_RE, WRITE_RE

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

        # An agent whose product is a ROUTED ITEM, not a file, is an orphan the
        # same way — and until 2026-08-01 this check could not see one.
        #
        # rd_market_event_monitor.py was built 2026-07-30 to Chairman spec ("a
        # live event feed... make sure that this agent can quickly report to the
        # board so that they can act fast"), then ran nowhere for two days and
        # nothing here said a word. Two independent reasons, both worth keeping
        # in mind before narrowing this check again:
        #   1. It escalates with `from cit_director import submit_to_pod_queue`
        #      and then a BARE `submit_to_pod_queue(proposal)`. The producer gate
        #      below looks for `<module>.<fn>(`, which that never matches.
        #   2. Its only named file output, rd_market_event_report.json, has zero
        #      readers — so even once past the gate, `if not readers: continue`
        #      skipped it. The narrowing is right for a report nobody consumes;
        #      it is wrong when the agent's real output is a board proposal.
        # Its queue's readers are not in question: these entry points are the
        # firm's documented producer->queue->consumer contracts (see CLAUDE.md),
        # every one of them lands in a queue a board or pod actually drains.
        # So an unreachable module that calls one is an orphan by definition,
        # and no reader search is needed to say so.
        esc = _escalation_path(name, code[name])
        if esc:
            msg = (f"[orphan-agent] {name} escalates via {esc} but is NOT reachable "
                   f"from main.py or the cron manifest — it is scheduled NOWHERE, so "
                   f"nothing it would escalate ever reaches the board")
            (notes if name in _orphan_baseline() else problems).append(
                msg + (" [known — pending board triage]" if name in _orphan_baseline() else ""))
            found_orphans.add(name)
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
            exists = (_data_dir() / out).exists()
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
            f"[orphan-agent] {name} is in {_baseline_path().name} but is no longer an orphan — "
            f"remove it from the baseline so the ratchet cannot loosen")


def _baseline_path() -> Path:
    """The orphan baseline, resolved from BASE_DIR at call time. See _data_dir."""
    return BASE_DIR / "data" / "orphan_agents_baseline.json"


def _orphan_baseline() -> set:
    """Modules already known to be unscheduled, accepted only until the board triages
    them. New entries must never be added to silence a finding — that is the exact
    'flagged but never solved' loop the firm has been burned by."""
    try:
        d = json.loads(_baseline_path().read_text())
        return set(d.get("known_orphans", []))
    except Exception:
        return set()


def check_coherence_layer():
    """The watchdog needs a watchdog.

    coherence_laws.py is the only detector in the firm that can find a bug
    nobody anticipated, which makes it the worst possible thing to let fail
    quietly. Two ways it could:

      1. A law gets registered without declaring two sources — it degrades into
         an assertion and stops catching this bug class at all. The decorator
         raises at import, so this check catching an ImportError IS that alarm.
      2. The report goes stale. `coherence_report.json` is written on every run
         of the daily pipeline; if its timestamp stops moving, the layer has
         stopped firing and every law is silently HOLDS-by-absence. That is the
         orphan tell this audit was built to sweep for, applied to itself.
    """
    try:
        import coherence_laws as cl
    except Exception as e:
        problems.append(f"[coherence] coherence_laws will not import ({type(e).__name__}: "
                        f"{e}) — the conservation layer is OFF")
        return

    if not cl.LAWS:
        problems.append("[coherence] no laws registered — the module exists and "
                        "checks nothing")
        return
    for fn, statement in cl.LAWS:
        if not getattr(fn, "sources", None) or len(fn.sources) != 2 \
           or fn.sources[0] == fn.sources[1]:
            problems.append(f"[coherence] {fn.__name__} does not name two independent "
                            f"sources — it is an assertion, not a law")
        if not (statement or "").strip():
            problems.append(f"[coherence] {fn.__name__} has no conservation statement")

    rep = cl.REPORT
    if not rep.exists():
        notes.append(f"[coherence] {rep.name} has NEVER been produced — the layer is "
                     f"wired but has not run on this host")
        return
    try:
        checked = datetime.fromisoformat(json.loads(rep.read_text())["checked_at"])
    except Exception as e:
        problems.append(f"[coherence] {rep.name} unreadable ({e}) — cannot tell whether "
                        f"the layer is still firing")
        return
    age_h = (datetime.now() - checked).total_seconds() / 3600.0
    # 30h: the pipeline runs 4x on weekdays, so one quiet weekend day is normal
    # and a genuinely stopped layer still shows up within a day and a half.
    if age_h > 30:
        problems.append(f"[coherence] report is {age_h:.0f}h old — the conservation "
                        f"layer has stopped running on this host; every law is "
                        f"passing by absence")
    else:
        holds = sum(1 for r in json.loads(rep.read_text())["results"]
                    if r["verdict"] == "HOLDS")
        notes.append(f"[coherence] OK — {len(cl.LAWS)} laws, all name two sources; "
                     f"report {age_h:.1f}h old ({holds} holding)")


def check_internal_audit():
    """The third line has no fourth line — so this sweep is its outer ring.

    `internal_audit_agent` is the only control whose subject is the other
    controls. Nothing else in the firm would notice if it stopped: its report
    would simply stay at the last good answer, and a frozen "0 blind, 0
    uncovered" reads exactly like a healthy one. That is the orphan tell this
    audit exists for, applied to the one module where it matters most.

    Three ways it could fail quietly, in descending order of nastiness:
      1. It will not import — the layer is off and every law's falsifiability
         is unmeasured.
      2. A law has no poison pill. Then that law's HOLDS is not evidence, and
         the audit's own summary would still say "0 blind".
      3. The report goes stale, so every verdict below is a fossil.
    """
    try:
        import internal_audit_agent as ia
        import coherence_laws as cl
    except Exception as e:
        problems.append(f"[internal audit] will not import ({type(e).__name__}: {e}) "
                        f"— nothing is checking whether the controls can fail")
        return

    unpilled = sorted({fn.__name__.replace("law_", "") for fn, _ in cl.LAWS}
                      - set(ia.POISON_PILLS))
    if unpilled:
        problems.append(f"[internal audit] {len(unpilled)} law(s) have no poison pill "
                        f"({', '.join(unpilled)}) — their HOLDS is an assertion, not "
                        f"a measurement")

    uncensused = [c.id for c in ia.CONTROL_CENSUS if not c.detectors]
    if uncensused:
        problems.append(f"[internal audit] failure class(es) with no detector: "
                        f"{', '.join(uncensused)}")

    rep = ia.REPORT
    if not rep.exists():
        notes.append(f"[internal audit] {rep.name} has NEVER been produced — wired "
                     f"but not yet run on this host")
        return
    try:
        r = json.loads(rep.read_text())
        checked = datetime.fromisoformat(r["checked_at"])
    except Exception as e:
        problems.append(f"[internal audit] {rep.name} unreadable ({e})")
        return
    age_h = (datetime.now() - checked).total_seconds() / 3600.0
    if age_h > 30:
        problems.append(f"[internal audit] report is {age_h:.0f}h old — the third line "
                        f"has stopped running on this host, and a frozen 'all clear' "
                        f"is indistinguishable from a real one")
    else:
        notes.append(f"[internal audit] OK — {len(r.get('falsifiability', []))} control(s) "
                     f"proven falsifiable, {len(ia.CONTROL_CENSUS)} failure class(es) "
                     f"censused; report {age_h:.1f}h old")


def effective_gaps(sources: dict, code: Optional[dict] = None,
                   gap_of=None, cron_text: Optional[str] = None) -> Optional[dict]:
    """{module: (best_h, worst_h)} hours between runs, or None if unknowable.

    A module that is on no crontab line of its own is not therefore slow — it
    runs at the cadence of whatever reaches it. `coherence_laws.py` has zero
    cron entries and runs because `main.py` calls it.

    Why a RANGE and not the best cadence. Reachability is resolved per module,
    but a root does not necessarily invoke all of a module. `kalshi_scanner.py`
    imports `coherence_laws` every 12h to call exactly one law
    (`law_nested_probabilities_are_monotone`); the law that reads
    `kalshi_scan_log.json` runs only from `run_all()`, which only `main.py`
    reaches, at 65.5h. Collapsing that to the 12h minimum reports the module as
    keeping up with a file that one of its own laws provably cannot see — it
    would have suppressed the single finding this pair of functions exists to
    make. Callers get both ends and must decide which one their question can
    afford to be wrong about; see check_reader_keeps_up_with_writer, which
    spends the reassuring direction and not the alarming one.

    Roots are walked with `strict=True`: a cadence inherited from a module that
    merely NAMES this one is not a cadence (see scheduler_reachable).

    `gap_of` and `cron_text` are injectable so a test can pose a schedule
    instead of measuring the host it happens to run on.
    """
    if gap_of is None:
        try:
            import schedule_expectations as se
        except Exception:
            return None
        if cron_text is None:
            snap = _cron_snapshot()
            if not snap.exists():
                return None
            cron_text = snap.read_text()
        cron_txt = cron_text

        def gap_of(script):
            return se.max_legit_gap_h(script, cron_text=cron_txt)

    by_root = scheduler_reachable(sources, code, by_root=True, strict=True,
                                  cron_text=cron_text)
    if by_root is None:
        return None
    out = {}
    for root, members in by_root.items():
        g = gap_of(root)
        if g is None:
            continue                  # named in cron but no parsable entry
        for m in members:
            lo, hi = out.get(m, (g, g))
            out[m] = (min(lo, g), max(hi, g))
    return out


#: What a READ looks like. The `open(` arm carries a negative lookahead because
#: `open(OUT, "w")` is the single most common way to write a file in this
#: codebase, and a bare `open\s*\(` counted it as a read — so a writer that
#: replaces its output wholesale was classified as load-append-store, and the
#: report downgraded destroyed generations to "lateness, not loss". The error
#: sat on the reassuring side, which is where it does damage: it can only ever
#: hide a real loss, never invent one.
_READ_CALL = (r"(?:json\.load|\.read_text|\.read_bytes|_load|read_json"
              r"|open\s*\((?![^)\n]*[\"'][wax]))")


def _replaces_its_output(code_txt: str, out: str) -> Optional[bool]:
    """True if the writer replaces `out` wholesale, False if it accumulates, None
    if this cannot be told from the source.

    Three states on purpose. A writer that loads its own file before rewriting
    it is doing load-append-store, so a slow reader is merely late and still
    sees every row. A writer that only writes is replacing, and whatever it
    replaced is gone. Those two want opposite words in the report.

    The third state is the one worth defending. Almost every path here is bound
    to a module constant (`SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"`) and
    then read through that name, so a search for the literal next to a read
    finds nothing and would classify the whole firm as destructive — a
    confident answer produced by not looking properly. So: resolve the constant
    first, and when no constant binds the literal AND the literal never appears
    beside a read, say None. `presence-is-not-currency`: the absence of evidence
    that it reads is not evidence that it doesn't.
    """
    if re.search(_READ_CALL + r"[^\n]*" + re.escape(out), code_txt) or \
       re.search(re.escape(out) + r"[^\n]*" + _READ_CALL, code_txt):
        return False
    consts = set(re.findall(r"^(\w+)\s*=[^\n]*" + re.escape(out),
                            code_txt, re.M))
    if not consts:
        return None                      # nothing names it; cannot follow it
    for c in consts:
        if re.search(_READ_CALL + r"[^\n]*\b" + re.escape(c) + r"\b", code_txt) or \
           re.search(r"\b" + re.escape(c) + r"\b[^\n]*" + _READ_CALL, code_txt):
            return False
    return True


def _file_is_replaced(code: dict, writers, out: str) -> Optional[bool]:
    """Is `out` replaced wholesale, or accumulated? A property of the FILE.

    Asking one writer was wrong and produced eight confident false lines on the
    first run. `board_issue_ledger.py` binds `CHAIRMAN_PATH` for reference and
    writes through `chairman_queue.py`, the documented single source of truth
    for that file — which loads it before every write. Judged on
    board_issue_ledger's own source alone, the firm's main accumulating queue
    read as destructive and buried the one real finding.

    So: if ANY module that writes this file reads it first, the file
    accumulates. Unanimous None (nobody's source settles it) stays None.
    """
    verdicts = [_replaces_its_output(code.get(w, ""), out) for w in writers]
    if any(v is False for v in verdicts):
        return False
    if all(v is None for v in verdicts):
        return None
    return True


def _cadence_phrase(best: float, worst: float) -> str:
    """How often, said so the range is attributed to the side that has one."""
    if best == worst:
        return f"at worst every {worst:.1f}h"
    return (f"every {best:.1f}h if invoked on its fastest scheduler path and "
            f"{worst:.1f}h on its slowest")


def check_reader_keeps_up_with_writer(sources: dict, code: Optional[dict] = None,
                                      gap_of=None,
                                      cron_text: Optional[str] = None):
    """A reader slower than its writer is SAMPLING its evidence, not observing it.

    Not a latency complaint. `kalshi_scanner.py` rewrites
    `data/kalshi_scan_log.json` whole, four times a day — worst gap 12h. The
    only thing that checks that book for structural coherence is
    `coherence_laws.py`, which is on no crontab line and runs inside `main.py`,
    scheduled `* * 1-5` — worst gap **65.5h**, because the weekend is in it.

    Friday evening to Monday morning the scanner publishes about ten reports
    and each one overwrites the last. Nine of them are never read by any law,
    and cannot be: by the time a checker runs, the evidence has been replaced.
    That is not a delayed check, it is a permanently unobservable window, and
    every HOLDS reported on Monday is a verdict about one scan in ten while
    reading exactly like a verdict about the weekend.

    The overwrite test is what separates the two cases, and it is deliberately
    conservative: a writer that also READS its own output is accumulating
    (load, append, rewrite), so a slow reader there is merely late and sees the
    backlog. A writer that only writes is replacing, and the generations in
    between are destroyed. Only the destructive case is called a loss.

    Reported as notes rather than violations. Every instance here is a
    scheduling decision on a host whose crontab is the Chairman's to change —
    failing the push gate over it would block work on a machine that cannot fix
    it. What this buys is that the window has a NUMBER and a name attached,
    instead of being a thing nobody had measured.
    """
    code = code or {n: _code_only(t) for n, t in sources.items()}
    gaps = effective_gaps(sources, code, gap_of, cron_text)
    if gaps is None:
        notes.append("[reader-cadence] CANNOT TELL — no crontab snapshot, so no "
                     "module's cadence is knowable here; judged nobody")
        return

    out_re = re.compile(r'["\']([\w./-]+\.json)["\']')
    write_re = re.compile(r"\b(?:write_text|json\.dump|atomic_write|_save)\b")
    writer_stems = {n[:-3] for n, t in sources.items()
                    if not n.startswith("test_") and write_re.search(t)}

    # Writers per output, and the fastest cadence any of them refreshes it at.
    writers_of = {}
    for name, txt in sources.items():
        if name.startswith("test_") or not write_re.search(txt):
            continue
        for out in _declared_outputs(txt, out_re,
                                     writer_stems=writer_stems - {name[:-3]}):
            writers_of.setdefault(out, set()).add(name)

    findings, examined = [], 0
    for out, writers in writers_of.items():
        w_range = [gaps[w] for w in writers if w in gaps]
        if not w_range:
            continue                      # unscheduled: check_orphan_agents' job
        w_gap = min(lo for lo, _ in w_range)   # changes at least this often
        w_worst = max(hi for _, hi in w_range)
        destructive = _file_is_replaced(code, writers, out)
        slowest = None
        for reader, rtxt in code.items():
            if reader in writers or reader.startswith("test_") or out not in rtxt:
                continue
            if reader not in gaps:
                continue                  # unscheduled reader: a different bug
            r_best, r_worst = gaps[reader]
            examined += 1
            # Spend the range on the ALARMING side: r_worst raises the finding,
            # r_best only decides whether it is certain. A module reached by a
            # fast root and a slow one may do its reading on either path, and
            # taking the fast end would let one incidental import vouch for a
            # function that root never calls.
            if r_worst > w_gap and (slowest is None or r_worst > slowest[1]):
                # Certain only if the reader's FASTEST path is still slower
                # than the writer's SLOWEST. Comparing against the writer's
                # fastest instead called it certain whenever a gap was merely
                # possible — and the pair that exposed it,
                # internal_audit_agent reading coherence_report.json, are both
                # called by main.py inside ONE process, where the reader sees
                # exactly what the writer wrote a moment earlier and no gap
                # exists at all.
                slowest = (reader, r_worst, r_best, r_best > w_worst)
        if slowest is None:
            continue
        # ONE finding per file, named for its slowest reader. Reporting every
        # slow reader separately buried kalshi_scan_log under eight near-
        # identical lines about one shared queue — the finding was there and
        # unreadable, which is the same as absent.
        reader, r_gap, r_best, certain = slowest
        rank = {True: 2, None: 1, False: 0}[destructive]
        findings.append(dict(
            rank=rank, certain=certain, ratio=r_gap / w_gap, out=out,
            writer=sorted(writers)[0], reader=reader, destructive=destructive,
            w_best=w_gap, w_worst=w_worst, r_best=r_best, r_worst=r_gap,
            # ceil, not floor. A 72h reader against a 65.5h writer floors to
            # 1 generation seen and 0 missed, and the line then read "up to 0
            # generation(s) are overwritten ... and cannot be recovered" — a
            # loss finding asserting nothing was lost. Two writes DO fit in
            # that window (t=0 and t=65.5), so one is missed. This is an upper
            # bound and the sentence says "up to".
            missed=math.ceil(r_gap / w_gap) - 1))

    if not examined:
        # A pass on zero pairs is not a pass. `gaps` can be non-None and still
        # empty — a crontab that parses to no usable entry leaves every module
        # cadence-less, every reader skipped, and the check printing OK on a
        # denominator of nothing. Same rule the coherence laws are held to.
        notes.append("[reader-cadence] CANNOT TELL — no reader/writer pair had a "
                     "knowable cadence on both sides; judged nobody")
        return
    if not findings:
        notes.append(f"[reader-cadence] OK — every scheduled reader keeps up with "
                     f"its writer ({examined} reader->file pair(s) examined)")
        return
    lost = sum(1 for f in findings if f["destructive"] is True and f["certain"])
    notes.append(f"[reader-cadence] {len(findings)} file(s) have a reader slower "
                 f"than the file changes, {lost} of them destructively and on "
                 f"every path ({examined} reader->file pair(s) examined)")
    for f in sorted(findings, key=lambda f: (f["rank"], f["certain"],
                                             f["ratio"]), reverse=True)[:8]:
        out, r, w = f["out"], f["reader"], f["writer"]
        missed = f["missed"]
        # Say which SIDE the range is on. A reader reached by a fast root and a
        # slow one may do its reading on either path; so may a writer. Phrasing
        # every uncertain finding as the reader's doubt printed
        # "every 65.5h on its fastest path and 65.5h on its slowest" — a reader
        # with no range at all, carrying the writer's uncertainty under the
        # reader's name.
        rd = _cadence_phrase(f["r_best"], f["r_worst"])
        wr = _cadence_phrase(f["w_best"], f["w_worst"])
        maybe = "" if f["certain"] else (
            f"; {r} and {w} may share a scheduler path, on which the read "
            f"happens in the same run as the write and nothing is missed")
        if f["destructive"] is True:
            notes.append(
                f"[reader-cadence] {r} reads {out} {rd}, but {w} REPLACES it "
                f"{wr} — up to {missed} generation(s) are overwritten before "
                f"{r} ever sees them, and cannot be recovered{maybe}")
        elif f["destructive"] is None:
            notes.append(
                f"[reader-cadence] {r} reads {out} {rd} vs {w}'s {wr} — whether "
                f"{out} is replaced or accumulated could not be told from its "
                f"writers' source, so up to {missed} generation(s) MAY be "
                f"unrecoverable{maybe}")
        else:
            notes.append(
                f"[reader-cadence] {r} reads {out} {rd} vs {w}'s {wr} — {out} "
                f"accumulates, so this is lateness, not loss")


#: A temp file older than this is not a write in flight, it is a corpse.
_TEMP_ORPHAN_MIN_AGE_H = 1.0


def check_interrupted_writes(data_dir=None):
    """Count temp files left in data/ — each one is a write that never landed.

    An atomic writer unlinks its temp on failure, so a leftover means the
    process DIED between mkstemp and os.replace. That matters far beyond the
    debris: it is direct evidence that writes here really do get interrupted,
    which is the precondition for every truncate-in-place writer to leave a
    reader an empty file. Six were found on the Mac on 2026-08-23, all from
    agent_meeting._save (agent_status registry), all dated 2026-08-01 between
    15:34 and 20:04.

    The VM held **42**, dated 2026-05-28 through 2026-08-01, most of them zero
    bytes — a mkstemp that never got as far as its first write. The detector
    had been reporting all 42 there since it shipped; it was the count that was
    wrong everywhere else, because `data/` does not sync between hosts and the
    archive below had only ever been run on the Mac. So the sentence "the count
    is a signal now, not a permanently-lit lamp" was true of the dev laptop and
    false of the machine that trades around the clock. Cleaning host A does not
    clean host B, and a per-host number quoted firm-wide is the
    `private-diary-of-a-shared-fact` bug with the diary on the quieter host.

    Reported as a note, not a violation: the corpses are harmless in
    themselves, and blocking a push on litter would teach people to bypass the
    gate. What it buys is a running count — if it climbs, something is killing
    the pipeline mid-write and that IS worth a look.

    `data_dir` is injectable so the suite can hand this a directory holding a
    *known* corpse and prove the check still fires. Both hosts' corpses were
    MOVED to `data/.interrupted-writes/` (Mac 2026-08-23, VM 2026-08-24), never
    deleted — they are the only production evidence that writes here get
    interrupted, and a detector whose evidence has been swept up is exactly the
    kind that quietly stops working. Live count is now 0 on both, so the next
    one to appear means something.
    """
    import time as _time

    data = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
    if not data.is_dir():
        return
    now = _time.time()
    stale = []
    for f in data.glob("*.tmp"):
        try:
            age_h = (now - f.stat().st_mtime) / 3600.0
        except OSError:
            continue
        if age_h >= _TEMP_ORPHAN_MIN_AGE_H:
            stale.append((f.name, age_h))
    if not stale:
        # A denominator, so "OK" cannot mean "the directory moved". See
        # coherence_laws: a pass on zero examined items is not a pass.
        notes.append(f"[interrupted-writes] OK — 0 orphaned temp files in "
                     f"{data.name}/ ({len(list(data.glob('*.json')))} json files examined)")
        return
    stale.sort(key=lambda t: -t[1])
    notes.append(
        f"[interrupted-writes] {len(stale)} orphaned temp file(s) in {data.name}/ "
        f"— each is a process killed between mkstemp and os.replace, i.e. a "
        f"write that was interrupted. Oldest {stale[0][1]:.0f}h: "
        + ", ".join(n for n, _ in stale[:4]))


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
    check_reader_keeps_up_with_writer(sources)
    check_coherence_layer()
    check_internal_audit()
    check_interrupted_writes()
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

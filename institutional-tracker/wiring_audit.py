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
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

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
    snap = BASE_DIR / "data/vm_crontab_snapshot.txt"
    if not snap.exists():
        problems.append(
            "[orphan-agent] data/vm_crontab_snapshot.txt is missing — the schedule "
            "ground truth is unreadable, so no agent can be judged scheduled or "
            "orphaned. Run scripts/refresh_crontab_snapshot.sh")
        return
    cron_txt = snap.read_text()
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
    #
    # And the traversal reads INVOCATIONS, not mentions. See _invocation_edges:
    # a module stem sitting in a registry table, a governance list or a roadmap
    # string is data, and counting it as employment hid 25 unscheduled agents
    # behind a baseline that read 3. Where a module dispatches to a target this
    # cannot resolve statically, `unresolved` is True and that module alone
    # reverts to the loose text rule — an unreadable dispatcher must not be able
    # to orphan its own children.
    prod = {n for n in sources if not n.startswith("test_")}
    code = {n: _code_only(t) for n, t in sources.items()}
    stems = {n[:-3] for n in prod}
    edges, loose = {}, set()
    for n in prod:
        e, unresolved = _invocation_edges(code.get(n, ""), stems)
        edges[n] = e
        if unresolved:
            loose.add(n)
    reachable, frontier = set(roots), set(roots)
    while frontier:
        nxt = set()
        for cur in frontier:
            txt = code.get(cur, "")
            for name in prod:
                if name in reachable:
                    continue
                stem = name[:-3]
                if stem in edges.get(cur, ()):
                    nxt.add(name)
                elif cur in loose and re.search(rf"\b{re.escape(stem)}\b", txt):
                    nxt.add(name)
        reachable |= nxt
        frontier = nxt

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
    check_coherence_layer()
    check_internal_audit()
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

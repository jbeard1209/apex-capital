#!/usr/bin/env python3
"""
Agent Roster  (Department: HR / Governance)
===========================================
WHY THIS EXISTS
---------------
"How many agents does this firm employ?" had three different answers on
2026-08-01, and every one of them was wrong.

  agent_registry.json (Mac)   37     agent_registry.json (VM)   66
  union by class_name         67     agents actually running    ~100

Three separate defects produced that spread:

  1. THE REGISTRY IS FORKED. deploy.sh rsyncs `data/` through a two-file
     allowlist, so the moment the VM's Chief-of-Staff factory spawned an agent,
     that agent existed on exactly one host forever. 30 agents — the whole FE
     and FI pods — lived only on the VM.

  2. "inactive" DOES NOT MEAN RETIRED. Sixteen VM rows read `status: inactive`
     with `deactivation_reason: "Non-research agent — should not run in research
     cycle"`. That flag exists for research_team.py, which loads only
     active/reassigned. But the rows carrying it are the CRO, the Canary, the
     Manager, the five CIT agents — tier-0 infrastructure, `permanent: true`,
     all of them running on cron right now. Anything that reads `status` as
     employment concludes the Risk Manager quit.

  3. MOST AGENTS ARE NOT IN THE REGISTRY AT ALL. The registry only holds what
     the Chief-of-Staff factory spawned. Thirty-five agent modules run on cron
     or inside main.py — logic_team, the boards, the sentinels, HR's own
     retirement audit — and no registry has ever contained them. HR audits the
     registry, so HR has never been able to see its own team.

WHAT A ROSTER ROW IS
--------------------
Employment and research-cycle participation are split, because conflating them
is defect #2:

  employment       employed | superseded | retired   ← is this agent still ours
  research_cycle   bool                              ← does research_team load it
  runs_via         cron / main / called_by:<mod> / UNSCHEDULED

`superseded` is for a row whose file was renamed out from under it —
`RiskManager -> risk_manager.py` still sits in the registry pointing at a path
deleted months ago, while the agent itself runs from risk_manager_agent.py. The
agent was never lost; the pointer was. A "cleanup" that deleted rows with
missing files would have fired the CRO, the IRM, and the Chief of Staff.

THE RATCHET
-----------
data/agent_roster_manifest.json is the DECLARED roster and is tracked in git.
`--check` compares discovery against it: growth is absorbed silently, but an
agent that disappears is a RED finding unless the manifest records a
`retired_at` and a `retirement_reason`. Losing an agent must take a decision,
never a rename or a bad merge.

USAGE
    python3 agent_roster.py                # build data/agent_roster.json (this host)
    python3 agent_roster.py --report       # print, write nothing
    python3 agent_roster.py --check        # ratchet against the manifest (exit 1 on RED)
    python3 agent_roster.py --merge-remote # fold the VM's roster + registry in
    python3 agent_roster.py --adopt        # accept the current roster as the manifest
"""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import agent_lifecycle

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

REGISTRY_PATH = DATA / "agent_registry.json"
ROSTER_PATH = DATA / "agent_roster.json"
MANIFEST_PATH = DATA / "agent_roster_manifest.json"

VM_HOST = "apex-capital"
VM_BASE = "~/institutional-tracker"

# A module is agent-shaped if its name says so, or if it is one of the named
# core modules that predate the naming convention.
#
# _tracker/_runner/_analyst/_sizer were added 2026-08-01 after HR's attendance
# audit — which scans for those suffixes — found seven agents the roster did not
# hold, including international_analyst (the module that tracked the KOSPI,
# crashed on import for a month, and started this entire line of work) and the
# IRM's four-analyst board. A roster that omits agents because of a suffix it
# forgot to list is the same failure as the registry it replaces, one level up.
MODULE_HINT = re.compile(
    r"(_agent|_sentinel|_monitor|_officer|_board|_team|_pod|_director|"
    r"_watchdog|_auditor|_checker|_guard|_council|_sla|_committee|"
    r"_tracker|_runner|_analyst|_sizer)\.py$"
    r"|^(canary|hr|logic|board_meeting|deputy_board|research_team|manager|"
    r"strategy_council|chairman_|cos_)")
# ``cos_`` is the Chief-of-Staff factory's prefix — every file whose stem
# begins with it was created by that factory and is an agent by definition.
# The factory sometimes concatenates names into one word (``cos_biotech
# researchagent.py``, ``cos_hrdevelopmentofficer.py``), which the suffix
# alternatives above cannot catch because they require an underscore before
# the role word. Without the prefix match, source-2 in ``build()`` missed
# every factory-made agent whose name happened to drop that underscore, and
# a host without the per-host ``data/agent_registry.json`` (source-1)
# reported those agents as VANISHED. The prefix match turns the discovery
# into one that works from the file tree alone.

# Modules that match the name pattern but are libraries, not agents: they hold
# no schedule and make no decisions. Listed explicitly so the exclusion is a
# recorded judgement rather than a silent regex accident.
NOT_AGENTS = {
    "mail_guard.py",            # smtp wrapper used by everyone
    "manager_knowledge_base.py",  # lookup tables for manager_agent
    "chairman_inbox.py",        # read-only printer for the SLA queue
    "chairman_queue_sync.py",   # reconciler, not a decision-maker
    "agent_roster.py",          # this file
    "wake_agent.py",            # shell shim, no logic
}

# Superseded pointers: a registry row's `file` was renamed. Recorded here, not
# repaired by guesswork, so the succession is auditable.
SUPERSEDED = {
    "risk_manager.py": "risk_manager_agent.py",
    "irm_agent.py": "irm_review_agent.py",
    "chief_of_staff.py": "chief_of_staff_agent.py",
}


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _registry_rows(reg) -> list:
    if isinstance(reg, dict):
        return reg.get("agents", [])
    return reg if isinstance(reg, list) else []


# ── one listing per tree, one parse per file ──────────────────────────────
# `build()` cost 156s, and almost none of it was work: 1,215 ast.parse calls
# over ~300 distinct files, 20 full rglob walks of the same tree, and
# `dynamic_agent_registries()` run twice inside a single build. The pre-push
# gate runs the suite twice and the suite rebuilds the roster eight times, so
# the firm was paying roughly forty minutes per push to recompute an answer
# that had not changed. Measured 2026-08-28 with cProfile: compile 50.2s,
# ast.walk 40.5s over 1.8M nodes, rglob 33.6s over 99,440 scandir calls.
#
# Each cache is keyed so a change invalidates it. The AST is keyed on the
# file's own (mtime_ns, size), so an edit is picked up. The listing is keyed on
# `base`, which is a per-process snapshot of WHICH files exist — the tests that
# synthesise a module do it in a tempdir and pass that as `base`, a different
# key, and nothing in this firm writes a .py file into a tree it then re-lists
# in the same process. `cache_clear()` exists for anything that ever does.
_AST_CACHE = {}
_SRC_CACHE = {}
_FACTS_CACHE = {}
_PYFILES_CACHE = {}


def cache_clear() -> None:
    """Drop every cache — for a process that edits the tree it is reading."""
    _AST_CACHE.clear()
    _SRC_CACHE.clear()
    _FACTS_CACHE.clear()
    _PYFILES_CACHE.clear()


def _read_keyed(base: Path, rel: str):
    """`((path, digest), text)` for one file, or `(None, None)` if unreadable.

    THE KEY IS THE CONTENT, NOT ITS TIMESTAMP. This was `(path, st_mtime_ns,
    st_size)` and it is a genuine content key on APFS and a broken one on Linux:
    the kernel stamps inode times from a coarse clock (a jiffy, commonly 1-4ms),
    so two writes of the SAME LENGTH inside one tick produce byte-identical
    stat() output. The Mac never saw it — `TestRosterCachesInvalidateOnEdit` and
    `TestTheControlLayerIsCheapEnoughToRunTwice` both passed here and both FAILED
    on the VM (2026-08-28), which is the host that actually runs the audit on
    cron. A stale AST inside a control is worse than a slow one: the suite writes
    modules under the audit process and then audits them, so the window is not
    hypothetical (memory `harness-ran-stale-bytecode`, and `host_divergence_is_a_
    bug_class` for why green here proved nothing).

    Hashing costs a read plus a digest — roughly 2ms on the 1.2MB suite against
    790ms to parse it, i.e. ~0.3% of what the cache is saving. Paying 0.3% for
    an invariant that actually holds is not a trade worth thinking about twice.
    The bytes are decoded here rather than re-read by `_source_of`, so one
    lookup is one read and the text can never disagree with the digest that
    keys it.
    """
    p = base / rel
    try:
        data = p.read_bytes()
    except OSError:
        return None, None
    key = (str(p), hashlib.sha1(data).hexdigest())
    try:
        return key, data.decode()
    except (UnicodeDecodeError, ValueError):
        return key, None


def _content_key(base: Path, rel: str):
    """Content identity for one file — None if it cannot be read."""
    return _read_keyed(base, rel)[0]


def _source_of(base: Path, rel: str):
    """File text, cached on content identity. None if unreadable."""
    key, text = _read_keyed(base, rel)
    if key is None:
        return None
    if key not in _SRC_CACHE:
        _SRC_CACHE[key] = text
    return _SRC_CACHE[key]


def _ast_of(base: Path, rel: str):
    """Parsed module, cached on content identity. None if unreadable.

    Returns None rather than raising because every call site here already
    treats an unparseable file as "tells us nothing", and a cache that stored
    only successes would re-read a broken file on every pass.
    """
    key, src = _read_keyed(base, rel)
    if key is None:
        return None
    if key not in _SRC_CACHE:
        _SRC_CACHE[key] = src
    if key not in _AST_CACHE:
        try:
            _AST_CACHE[key] = ast.parse(src) if src is not None else None
        except Exception:
            _AST_CACHE[key] = None
    return _AST_CACHE[key]


def parsed_module(path: Path):
    """Cached AST for an absolute path. None if it cannot be read or parsed.

    The same content-keyed cache as `_ast_of`, reached by full path rather than
    (base, rel), because `internal_audit_agent._symbol_exists` holds paths and
    was re-parsing them: 84 census refs, ~30 of which name the 28k-line suite,
    cost 208s per audit and the pre-push gate pays it twice. Exported rather
    than copied so there is ONE cache and `cache_clear()` drains all of it — a
    second private cache in the caller is how a stale parse outlives the edit
    that should have invalidated it (memory `harness-ran-stale-bytecode`).
    """
    return _ast_of(path.parent, path.name)


_EMPTY_FACTS = {"classes": frozenset(), "bases": frozenset(),
                "declares_contract": False, "imports": frozenset()}


def _facts_of(base: Path, rel: str) -> dict:
    """Everything this file's AST is ever asked, read off in one walk, cached.

    `research_team_skips.declares()` answered "does a sibling define this base
    class?" by walking every sibling module in agents/ — for each of the 17
    agents it checks, against ~40 siblings, a full `ast.walk` each: 166,000
    generator calls to answer 17 questions. The facts those walks extract
    (classes defined, base classes named, whether the context contract is
    declared, first-level imports) are properties of the FILE, not of the
    question being asked of it, so they belong to the file's content key.

    An unreadable or unparseable file yields the empty facts, which is what
    every caller already did with a None tree.
    """
    key = _content_key(base, rel)
    if key is None:
        return _EMPTY_FACTS
    if key not in _FACTS_CACHE:
        tree = _ast_of(base, rel)
        classes, bases, imports, declares = set(), set(), set(), False
        for n in (ast.walk(tree) if tree is not None else ()):
            if isinstance(n, ast.ClassDef):
                classes.add(n.name)
                bases.update(b.id for b in n.bases if isinstance(b, ast.Name))
                for stmt in n.body:
                    tgts = (stmt.targets if isinstance(stmt, ast.Assign)
                            else [stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
                    if any(isinstance(t, ast.Name) and t.id == CONTEXT_CONTRACT
                           for t in tgts):
                        declares = True
            elif isinstance(n, ast.Import):
                imports.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imports.add(n.module.split(".")[0])
        _FACTS_CACHE[key] = {"classes": frozenset(classes),
                             "bases": frozenset(bases),
                             "declares_contract": declares,
                             "imports": frozenset(imports)}
    return _FACTS_CACHE[key]


def _py_files(base: Path) -> list:
    key = str(base)
    if key not in _PYFILES_CACHE:
        out = []
        for p in base.rglob("*.py"):
            s = str(p.relative_to(base))
            if s.startswith((".git/", "venv/", "__pycache__/")) or "/__pycache__/" in s:
                continue
            out.append(s)
        _PYFILES_CACHE[key] = sorted(out)
    # A copy, on BOTH paths: a cached list handed out by reference is one
    # careless `.remove()` away from a roster that silently shrinks for every
    # later caller — and the hit path is the one that would do it 19 times.
    return list(_PYFILES_CACHE[key])


def _crontab() -> str:
    """This host's crontab UNIONED with the VM's deployed snapshot.

    The local crontab alone is not the firm's schedule. The Mac has a handful of
    jobs; the VM has 75, and it is the VM that runs the pipeline. Reading only
    `crontab -l` made a Mac-built roster report 74 agents as running nowhere
    against the merged view's 45 — the same agent "employed but unscheduled" or
    not depending on which host last wrote the file. That is the host-divergence
    class this firm keeps paying for, and here it would have fed HR's audit and
    the shrink ratchet two different firms.

    data/vm_crontab_snapshot.txt is refreshed daily on the VM, travels in
    deploy.sh's allowlist, and is already the ground truth wiring_audit.py and
    hr_agent.py measure against. One schedule, both hosts.
    """
    parts = [local_crontab()]
    try:
        parts.append((DATA / "vm_crontab_snapshot.txt").read_text())
    except Exception:
        pass
    return "\n".join(parts)


def local_crontab() -> str:
    """This host's OWN crontab, with no VM snapshot mixed in.

    _crontab() above deliberately unions both hosts so the roster and the shrink
    ratchet see one firm — "is this agent employed anywhere?" is a firm-wide
    question. Attendance is the opposite kind of question. An agent's output
    freshness is evidence about the AGENT only on the host that actually runs
    it; on any other box a perfectly healthy agent's files sit untouched
    forever. The Mac has no crontab at all, so measuring VM-scheduled agents
    against Mac mtimes nominated six working agents for a PIP.
    """
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""


def schedule_pattern(stem: str):
    """The one matcher for "does this schedule text invoke this module".

    Word-boundary, because the substring test calls `risk_manager` scheduled on
    the strength of `risk_manager_agent.py`. Exported so callers import it
    rather than re-typing the regex — a second copy of this exact pattern in
    the sweep's isolation graph is what produced six false reports.
    """
    return re.compile(rf"\b{re.escape(stem)}\.py\b")


_WORD_RE = re.compile(r"\w+")
_PYREF_RE = re.compile(r"\w+\.py\b")


def schedule_index(text: str) -> dict:
    """Every module reference in a schedule text, extracted in one pass.

    `build()` asked `\\b<stem>\\.py\\b` of 481 stems against the crontab and
    against main.py: 1,752 regex scans of the same two haystacks, 16.5s of the
    30s a warm build cost. The question is a set membership wearing a regex —
    `\\b...\\b` matches exactly when the stem is a MAXIMAL run of word
    characters, so one pass that collects those runs answers all 481 at once.

    This is knowingly a second implementation of `schedule_pattern`, which the
    docstring above exists to discourage, so it is held to the standard that
    makes a second implementation safe: `TestScheduleIndexMatchesThePattern`
    asserts the two agree **per stem**, on the real crontab and the real
    main.py, for every module in the tree — not on a sample and not on their
    union (memory a-union-cannot-prove-all-n). A stem that is not a plain word
    run cannot be represented this way at all, and `indexed_search` falls back
    to the reference regex for those rather than answering a nearby question.
    """
    return {
        "text": text,
        "py": frozenset(m.group(0)[:-3] for m in _PYREF_RE.finditer(text)),
        "words": frozenset(_WORD_RE.findall(text)),
    }


def indexed_search(index: dict, stem: str, as_word: bool = False) -> bool:
    """Does this schedule text invoke `stem`? Same answer as the regex, O(1).

    `as_word` asks the looser question main.py needs — the bare module name
    rather than `<stem>.py` — which is the second of the two searches build()
    ran against main.py.
    """
    if not _WORD_RE.fullmatch(stem):
        pat = (re.compile(rf"\b{re.escape(stem)}\b") if as_word
               else schedule_pattern(stem))
        return bool(pat.search(index["text"]))
    return stem in (index["words"] if as_word else index["py"])


def runs_here(agents: list, crontab_text: str = None) -> set:
    """Stems whose invocation chain is rooted in THIS host's crontab.

    Being on the firm's schedule is not the same as running on this box. An
    agent qualifies if its own module is in the local crontab, or if whatever
    invokes it is — main.py's agents, research_team's registry agents and
    called_by: chains all inherit the host of the entry that starts them.
    Returns stems, matching the sweep's identity for an agent.
    """
    txt = local_crontab() if crontab_text is None else crontab_text
    direct = set()
    for a in agents:
        stem = os.path.basename(a.get("file") or "")[:-3]
        if stem and schedule_pattern(stem).search(txt):
            direct.add(stem)
    if not direct:
        # Nothing on this host's crontab: nothing here is attendance-assessable.
        # Returning early keeps the empty case explicit rather than letting the
        # closure below quietly conclude the same thing.
        return set()

    here = set(direct)
    main_here = bool(schedule_pattern("main").search(txt))
    for a in agents:
        stem = os.path.basename(a.get("file") or "")[:-3]
        if not stem or stem in here:
            continue
        for via in a.get("runs_via") or []:
            if via in ("main", "research_team") and main_here:
                here.add(stem)
            elif via.startswith("called_by:") and via.split(":", 1)[1] in direct:
                here.add(stem)
    return here


# One name, used by both the cheap text pre-filter and the structural test it
# guards. Two copies of this string would be two things that can drift apart,
# and the drift is silent: the filter would skip a file the detector wanted.
LOADER_CALL = "spec_from_file_location"


def _dynamic_agent_rows(base: Path = None) -> list:
    """Rows [(class_name, pipeline_stem, agent_file_rel)] parsed off the AST.

    The structural detector that `dynamic_agent_registries` is written on top
    of has the file path in hand — `RESEARCH_AGENTS = [(path, cls), ...]` is
    the exact shape it is scanning for. The old function kept only `cls →
    stem` and threw the path away, which forced `research_team_skips` to
    recover the file through `data/agent_registry.json` (a per-host runtime
    cache written by the Chief-of-Staff factory). A host that had never run
    the factory had no registry file, and the function silently returned {} —
    the smoke suite's "is FI skipped?" check then failed on that host for a
    data-availability reason, not a code reason. Keep both derivations in one
    pass, and the static-code question stays answerable from static code.
    """
    base = base or BASE
    rows = []
    for rel in _py_files(base):
        if os.path.dirname(rel):        # top-level pipelines only
            continue
        # A necessary condition, checked as text before paying for a parse: an
        # AST call node named `spec_from_file_location` cannot exist unless that
        # identifier is in the source. 7 of 219 top-level files contain it, so
        # this skips 212 compiles — including the 1.4MB test module — and the
        # detection below is unchanged, because a file that merely MENTIONS the
        # name in a comment still has to satisfy the structural test to count.
        src = _source_of(base, rel)
        if src is None or LOADER_CALL not in src:
            continue
        tree = _ast_of(base, rel)
        if tree is None:
            continue
        loads_by_path = any(
            isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr == LOADER_CALL)
                 or (isinstance(n.func, ast.Name) and n.func.id == LOADER_CALL))
            for n in ast.walk(tree))
        if not loads_by_path:
            continue
        stem = rel[:-3]
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple))):
                continue
            for elt in node.value.elts:
                if not (isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == 2):
                    continue
                try:
                    path, cls = ast.literal_eval(elt.elts[0]), ast.literal_eval(elt.elts[1])
                except Exception:
                    continue
                if isinstance(path, str) and isinstance(cls, str) \
                        and path.endswith(".py") and cls[:1].isupper():
                    rows.append((cls, stem, path))
    return rows


def dynamic_agent_registries(base: Path = None) -> dict:
    """Agents a module runs by PATH rather than by import: {ClassName: stem}.

    `_imports_of` walks import statements, so it sees every caller that says
    `import x`. It is blind to the other way this firm starts an agent:

        RESEARCH_AGENTS = [("agents/cos_fi_creditspread_agent.py",
                            "FICreditSpreadAgent"), ...]
        spec = importlib.util.spec_from_file_location(class_name, path)

    fi_pipeline runs 7 agents that way and fe_pipeline runs 10. None of the 17
    appears in `called_by`, so the roster could only explain them through the
    `research_cycle` fallback further down — "no module schedule, so
    research_team must be running it." That fallback happened to be true and
    happened to be the wrong reason, which is the worst kind of correct: the
    moment research_team stops loading them the roster calls 17 employed agents
    UNSCHEDULED, and the shrink-only orphan ratchet fires on a fiction.

    Detected structurally, never by name: a module that calls
    spec_from_file_location AND declares a module-level sequence of
    (`*.py`, `ClassName`) pairs is loading agents from disk. Adding an
    eighteenth agent to either list needs no edit here; writing a third such
    pipeline needs none either.
    """
    return {cls: stem for cls, stem, _file in _dynamic_agent_rows(base)}


CONTEXT_CONTRACT = "REQUIRED_SPREAD_KEYS"


def research_team_skips(base: Path = None, dyn: dict = None) -> dict:
    """{ClassName: pipeline_stem} research_team declines to construct (#79).

    Two conditions, both required, mirroring research_team.py's own guard:
      1. the class declares CONTEXT_CONTRACT — inputs it cannot get from a bare
         AgentClass() in a generic registry loop, and
      2. a pipeline registry loads it by path, so skipping it here does not
         make it stop running anywhere.

    Condition 1 is read off the AST, following one level of base class within
    agents/, because that is where the contract actually lives (the seven FI
    agents inherit it from cos_fi_base). Condition 2 is dynamic_agent_registries.

    The FE agents satisfy 2 and not 1 — they need no runtime context and
    research_team still runs them — which is the distinction this function
    exists to make. Keyed off a declared class attribute rather than an "FI"
    name prefix so a renamed pod does not silently rejoin the research cycle.
    """
    base = base or BASE
    # `dyn` is condition 2, and `build()` has already computed it by the time it
    # gets here. Passing it in is the difference between one scan of the tree
    # and two; recomputing it would be a second derivation of a fact the caller
    # is holding, which is the shape this file's own docstrings keep warning
    # about. Default None keeps every other caller working unchanged.
    dyn = dyn if dyn is not None else dynamic_agent_registries(base)
    if not dyn:
        return {}

    def declares(rel: str, depth: int = 0) -> bool:
        f = _facts_of(base, rel)
        if f["declares_contract"]:
            return True
        if depth or not f["bases"]:
            return False
        # One hop: find the sibling file in agents/ that defines a base class.
        d = os.path.dirname(rel)
        for sib in sorted(_py_files(base)):
            if os.path.dirname(sib) != d or sib == rel:
                continue
            if (_facts_of(base, sib)["classes"] & f["bases"]) and declares(sib, depth + 1):
                return True
        return False

    # The file path for each class is already in the pipeline's source
    # (RESEARCH_AGENTS = [(path, cls), ...]), captured off the same AST pass
    # that produced `dyn`. Reading `data/agent_registry.json` to recover it
    # made this function fail-closed on any host where the Chief-of-Staff
    # factory had not populated that per-host runtime cache — the Mac smoke
    # suite returned 0 skips not because fi_pipeline had stopped declaring
    # them but because the registry was absent. The static answer deserves a
    # static source.
    reg = {cls: path for cls, _stem, path in _dynamic_agent_rows(base)}
    out = {}
    for cls, stem in dyn.items():
        rel = reg.get(cls) or ""
        if rel and (base / rel).exists() and declares(rel):
            out[cls] = stem
    return out


def _imports_of(base: Path, rel: str) -> set:
    """Module names this file imports, first level only."""
    # A copy: callers own the result, the cache owns the frozenset.
    return set(_facts_of(base, rel)["imports"])


# ──────────────────────────────────────────────────────────────────────────
def build(base: Path = BASE, host: str = "local") -> dict:
    """Discover every agent this host employs, from every source available."""
    files = _py_files(base)
    cron = _crontab()
    main_src = ""
    try:
        main_src = (base / "main.py").read_text()
    except Exception:
        pass

    stems = {os.path.basename(f)[:-3]: f for f in files}

    # Two haystacks, scanned once each instead of 481 times each.
    cron_idx = schedule_index(cron)
    main_idx = schedule_index(main_src)

    def scheduled_in(stem):
        via = []
        if indexed_search(cron_idx, stem):
            via.append("cron")
        if indexed_search(main_idx, stem) or indexed_search(main_idx, stem, as_word=True):
            via.append("main")
        return via

    # Everything a scheduled module imports is itself running, one level deep.
    dyn_registries = dynamic_agent_registries(base)
    skipped_by_research_team = research_team_skips(base, dyn=dyn_registries)
    directly_scheduled = {s for s in stems if scheduled_in(s)}
    called_by = {}
    for s in sorted(directly_scheduled):
        for imported in _imports_of(base, stems[s]):
            if imported in stems and imported not in directly_scheduled:
                called_by.setdefault(imported, set()).add(s)

    rows = {}

    # ── source 1: the Chief-of-Staff registry ────────────────────────────
    for a in _registry_rows(_load(base / "data" / "agent_registry.json", [])):
        name = a.get("class_name")
        if not name:
            continue
        f = a.get("file") or ""
        exists = bool(f) and (base / f).exists()
        employment = "employed"
        superseded_by = None
        if a.get("retired_at") or a.get("status") == "retired":
            employment = "retired"
        elif not exists:
            superseded_by = SUPERSEDED.get(os.path.basename(f))
            employment = "superseded" if superseded_by else "employed"
        rows[name] = {
            "name": name,
            "kind": "registry",
            "file": f,
            "file_exists": exists,
            "tier": a.get("tier"),
            "team": a.get("team") or a.get("division"),
            "lane": a.get("lane"),
            "specialization": a.get("specialization"),
            "employment": employment,
            "superseded_by": superseded_by,
            # research_cycle is what `status` actually controls (research_team.py
            # loads agent_lifecycle.RUNNABLE_STATUSES). It is NOT employment.
            # Read from the shared frozenset, never re-spelled: this line used to
            # carry its own ("active", "reassigned") literal, which stayed
            # accidentally correct only while research_team carried the same
            # wrong one. Two copies of a wrong rule agree with each other.
            #
            # Except for agents research_team now declines to construct: since
            # decision #79 it skips a registry agent that declares required
            # context AND has a pipeline that supplies it, so `active` no longer
            # implies "runs in the research cycle" for those. Recorded here
            # rather than left to drift, because agent_sweep reads this field to
            # decide whether a retired agent is still executing.
            "research_cycle": (agent_lifecycle.is_runnable(a.get("status"))
                               and name not in skipped_by_research_team),
            "registry_status": a.get("status"),
            "permanent": bool(a.get("permanent")),
            "retirement_reason": a.get("retirement_reason") or a.get("deactivation_reason"),
            "runs_via": [],
            "hosts": [host],
        }

    known_files = {r["file"] for r in rows.values() if r["file"]}
    known_files.update(SUPERSEDED.values())

    # ── source 2a: path-loaded agents keyed by their declared class name ──
    # fi_pipeline's RESEARCH_AGENTS and fe_pipeline's FE_AGENT_REGISTRY
    # declare (file, class) pairs that no `import` statement ever carries.
    # Without this source, a host with no agent_registry.json knows the
    # agent's file (through source 2b below) but keys its row by the file
    # stem, not by its class — so a manifest that declares the class name
    # cannot find it, every FI agent `research_team_skips` lists is "the
    # row vanished entirely", and the ratchet reports loss on a host that
    # has simply never run the factory. This source adds the row at its
    # declared class name so the key the manifest uses actually exists.
    for cls, stem, path in _dynamic_agent_rows(base):
        if cls in rows:
            continue
        if path in known_files:
            continue
        if not (base / path).exists():
            continue
        rows[cls] = {
            "name": cls,
            "kind": "module",
            "file": path,
            "file_exists": True,
            "tier": 0,
            "team": None, "lane": None, "specialization": None,
            "employment": "employed",
            "superseded_by": None,
            # A path-loaded agent runs if-and-only-if its pipeline runs. We
            # know the pipeline, so attribute it there, and let the ratchet's
            # `left_research_cycle` half see the declared truth.
            "research_cycle": cls not in skipped_by_research_team,
            "registry_status": None,
            "permanent": False,
            "retirement_reason": None,
            "runs_via": [f"called_by:{stem}"],
            "hosts": [host],
        }
        known_files.add(path)

    # ── source 2b: agent modules no registry has ever held ────────────────
    for f in files:
        b = os.path.basename(f)
        if b.startswith("test_") or b in NOT_AGENTS or not MODULE_HINT.search(b):
            continue
        if f in known_files:
            continue
        stem = b[:-3]
        via = scheduled_in(stem)
        if not via and stem in called_by:
            via = [f"called_by:{s}" for s in sorted(called_by[stem])]
        rows[stem] = {
            "name": stem,
            "kind": "module",
            "file": f,
            "file_exists": True,
            "tier": 0,
            "team": None, "lane": None, "specialization": None,
            "employment": "employed",
            "superseded_by": None,
            "research_cycle": False,
            "registry_status": None,
            "permanent": False,
            "retirement_reason": None,
            "runs_via": via or ["UNSCHEDULED"],
            "hosts": [host],
        }

    # ── fill runs_via for registry rows (their file may be renamed) ───────
    for r in rows.values():
        if r["kind"] != "registry":
            continue
        f = r["superseded_by"] or r["file"]
        stem = os.path.basename(f)[:-3] if f.endswith(".py") else ""
        via = scheduled_in(stem) if stem else []
        if not via and stem in called_by:
            via = [f"called_by:{s}" for s in sorted(called_by[stem])]
        # Loaded from disk by a pipeline's own registry (fi_pipeline's
        # RESEARCH_AGENTS, fe_pipeline's FE_AGENT_REGISTRY). This is a real
        # schedule edge that `called_by` cannot see, because the pipeline never
        # imports the agent — it opens the file. Checked BEFORE the
        # research_team fallback so the roster names the path that actually
        # gives the agent its inputs.
        if not via and r["name"] in dyn_registries:
            via = [f"called_by:{dyn_registries[r['name']]}"]
        # A research agent with no module schedule still runs: research_team.py
        # instantiates it from the registry every cycle.
        if not via and r["research_cycle"]:
            via = ["research_team"]
        r["runs_via"] = via or ["UNSCHEDULED"]

    return {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "host": host,
        "counts": _counts(rows.values()),
        "agents": [rows[k] for k in sorted(rows)],
    }


def _counts(agents) -> dict:
    agents = list(agents)
    return {
        "total": len(agents),
        "registry": sum(1 for a in agents if a["kind"] == "registry"),
        "module": sum(1 for a in agents if a["kind"] == "module"),
        "employed": sum(1 for a in agents if a["employment"] == "employed"),
        "superseded": sum(1 for a in agents if a["employment"] == "superseded"),
        "retired": sum(1 for a in agents if a["employment"] == "retired"),
        "research_cycle": sum(1 for a in agents if a["research_cycle"]),
        "unscheduled": sum(1 for a in agents if a["runs_via"] == ["UNSCHEDULED"]),
        "zombie": sum(1 for a in agents if a.get("zombie")),
    }


# ──────────────────────────────────────────────────────────────────────────
def _canonical_names(*rosters) -> dict:
    """file path -> the registry class_name that owns it, across every input.

    One agent must not appear twice because the hosts disagree about what to
    call it. The Mac's registry has 37 rows and the VM's has 66, so
    `agents/cos_fe_hardmoney_agent.py` is the registry row `FEHardMoneyAgent`
    on the VM and an anonymous module row `cos_fe_hardmoney_agent` on the Mac.
    Same file, same agent — the class_name wins, and 24 phantom FE/FI agents
    stop being double-counted.
    """
    m = {}
    for r in rosters:
        for a in r.get("agents", []):
            if a["kind"] != "registry":
                continue
            for f in (a.get("file"), a.get("superseded_by")):
                if f:
                    m.setdefault(f, a["name"])
    return m


def merge_rosters(a: dict, b: dict) -> dict:
    """Union two hosts' rosters. An agent employed on either host is employed.

    Merging may only ADD. There is no rule here under which an agent present in
    one input is absent from the output — losing an agent to a merge is the
    accident this whole module exists to make impossible.
    """
    # NOT a ladder. "employed" is the default state — the absence of news — so
    # it must never outrank a recorded decision. Only one host holds HR's
    # retirement records; if the merge let the other host's silence win, every
    # sync would quietly re-hire everyone HR has ever retired.
    EMP_RANK = {"employed": 0, "superseded": 1, "retired": 2}
    canon = _canonical_names(a, b)
    out = {}
    for src in (a, b):
        for r in src.get("agents", []):
            # Only a nameless MODULE row gets folded onto a registry row by
            # file. Two registry rows may legitimately share a file — the five
            # CIT agents all live in cit_director.py — and collapsing those by
            # path would delete four agents in the name of de-duplication.
            name = (canon.get(r.get("file"), r["name"])
                    if r["kind"] == "module" else r["name"])
            r = {**r, "name": name}
            cur = out.get(name)
            if cur is None:
                out[name] = dict(r)
                continue
            if EMP_RANK.get(r["employment"], 0) > EMP_RANK.get(cur["employment"], 0):
                cur["employment"] = r["employment"]
            cur["research_cycle"] = cur["research_cycle"] or r["research_cycle"]
            cur["permanent"] = cur["permanent"] or r["permanent"]
            cur["file_exists"] = cur["file_exists"] or r["file_exists"]
            for k in ("file", "tier", "team", "lane", "specialization",
                      "superseded_by", "registry_status", "retirement_reason"):
                if not cur.get(k) and r.get(k):
                    cur[k] = r[k]
            if cur["kind"] == "module" and r["kind"] == "registry":
                cur["kind"] = "registry"
            live = [v for v in cur["runs_via"] + r["runs_via"] if v != "UNSCHEDULED"]
            cur["runs_via"] = sorted(set(live)) or ["UNSCHEDULED"]
            cur["hosts"] = sorted(set(cur["hosts"]) | set(r["hosts"]))

    # A retirement one host never heard about leaves the agent still on cron
    # somewhere. That is the reverse of a lost agent and just as invisible.
    for r in out.values():
        r["zombie"] = (r["employment"] == "retired"
                       and r["runs_via"] != ["UNSCHEDULED"])
    return {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "host": "merged",
        "counts": _counts(out.values()),
        "agents": [out[k] for k in sorted(out)],
    }


def merge_registries(mac: dict, vm: dict) -> dict:
    """Reconcile the two hosts' agent_registry.json into one.

    The VM's row wins on OPERATIONAL fields, because the VM is production: its
    `status` is what research_team.py actually loads there, and its
    "inactive / Non-research agent" flags on the CRO, Canary, Manager and the
    five CIT agents are deliberate and correct — those are tier-0 infrastructure
    that must not be instantiated as research agents. Taking the Mac's `active`
    for them would push eight infrastructure classes into the research cycle.

    The Mac's row wins on HR HISTORY, because only the Mac has any: retirement
    records, performance notices, the voided 2026-05-06 reassignments. The VM
    schema has none of those fields, so "VM wins" alone would erase the entire
    disciplinary record of the firm.

    Mac-only agents are added, never dropped. MissedWinnersAgent exists on
    exactly one host and a naive `deploy.sh` allowlist entry would have deleted
    the other 30.
    """
    HR_FIELDS = ("division", "retired_at", "retirement_reason",
                 "performance_notice", "performance_notice_sent",
                 "voided_reassignment", "reinstated_at", "reinstatement_reason",
                 "whole_picture_exonerated")

    mac_rows = {a.get("class_name"): a for a in _registry_rows(mac) if a.get("class_name")}
    vm_rows = {a.get("class_name"): a for a in _registry_rows(vm) if a.get("class_name")}

    out = []
    for name in sorted(set(mac_rows) | set(vm_rows)):
        m, v = mac_rows.get(name), vm_rows.get(name)
        row = dict(v or m)
        if m and v:
            for f in HR_FIELDS:
                if m.get(f) is not None and row.get(f) is None:
                    row[f] = m[f]
        f = row.get("file") or ""
        successor = SUPERSEDED.get(os.path.basename(f))
        if successor:
            # Record the rename instead of leaving a dangling path. A tidy-up
            # that deleted rows whose file was missing would have fired the CRO,
            # the IRM and the Chief of Staff in one commit.
            row["superseded_by"] = successor
            row["file_note"] = (f"{f} was renamed to {successor}; the agent runs "
                                f"from the new module. Row kept for continuity.")
        out.append(row)

    return {
        "agents": out,
        "last_updated": datetime.datetime.utcnow().isoformat(),
        "total_spawned": max(int((mac or {}).get("total_spawned") or 0),
                             int((vm or {}).get("total_spawned") or 0),
                             len(out)),
        "merged_by": "agent_roster.py --sync-registry",
        "merged_at": datetime.datetime.utcnow().isoformat(),
    }


def sync_registry(write: bool = True) -> dict:
    """One canonical registry on both hosts."""
    mac = _load(REGISTRY_PATH, {"agents": []})
    r = subprocess.run(["ssh", VM_HOST, f"cat {VM_BASE}/data/agent_registry.json"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read the VM registry: {r.stderr.strip()[:200]}")
    vm = json.loads(r.stdout)

    merged = merge_registries(mac, vm)
    names_in = {a.get("class_name") for a in _registry_rows(mac)} | \
               {a.get("class_name") for a in _registry_rows(vm)}
    names_out = {a.get("class_name") for a in merged["agents"]}
    lost = sorted(n for n in names_in - names_out if n)
    if lost:
        raise RuntimeError(f"registry merge would lose {len(lost)} agent(s): {lost}")

    rep = {"mac": len(_registry_rows(mac)), "vm": len(_registry_rows(vm)),
           "merged": len(merged["agents"]), "lost": lost, "wrote": False}
    if write:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        arch = DATA / "agent_roster_archive" / stamp
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "agent_registry.mac.json").write_text(json.dumps(mac, indent=2))
        (arch / "agent_registry.vm.json").write_text(json.dumps(vm, indent=2))
        payload = json.dumps(merged, indent=2)
        tmp = REGISTRY_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        tmp.replace(REGISTRY_PATH)
        w = subprocess.run(
            ["ssh", VM_HOST,
             f"cat > {VM_BASE}/data/agent_registry.json.tmp && "
             f"mv {VM_BASE}/data/agent_registry.json.tmp {VM_BASE}/data/agent_registry.json"],
            input=payload, capture_output=True, text=True, timeout=120)
        if w.returncode != 0:
            raise RuntimeError(f"cannot write the VM registry: {w.stderr.strip()[:200]}")
        rep["wrote"] = True
        rep["archive"] = str(arch)
    return rep


def fetch_remote() -> dict:
    """Build the roster on the VM and bring it back."""
    cmd = (f"cd {VM_BASE} && ~/venv/bin/python3 agent_roster.py --report --json --host vm")
    r = subprocess.run(["ssh", VM_HOST, cmd], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"remote roster build failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


# ──────────────────────────────────────────────────────────────────────────
def check(roster: dict, manifest: dict) -> dict:
    """Ratchet: the roster may grow freely; it may only shrink with a reason.

    Two ways to lose an agent, and the ratchet has to cover both.

    VANISHED — the row is gone. That is the loss this ratchet was built for.

    LEFT THE RESEARCH CYCLE — the row is still here, still "employed", and no
    longer runs. On 2026-08-28 the VM went from 50 loaded agents at 05:00 to 8
    at 08:00: the Chief of Staff moved 43 rows to status "noticed", which
    research_team.py would not load. Nothing vanished, so this function returned
    GREEN while 84% of the firm's research coverage — Technology, Financials,
    Industrials, all ten First-Eagle agents, the whole FI pod — stopped
    producing. The count was right there in the roster's own printout
    (`c['research_cycle']`) and nothing ratcheted it.

    Named per agent, not totalled: "43 fewer" is a number nobody can act on,
    and a drop that exactly offsets a rise reads as no change at all (memory
    `an-aggregate-is-not-a-finding`).

    `research_cycle` is absent from manifests adopted before this check existed,
    so the ratchet arms itself on the next --adopt rather than firing on every
    row at once. `research_cycle_declared` reports how many rows carry the
    baseline, so an unarmed ratchet cannot be mistaken for a passing one
    (memory `presence-is-not-currency`).
    """
    have = {a["name"]: a for a in roster.get("agents", [])}
    declared = {a["name"]: a for a in manifest.get("agents", [])}
    # Secondary indices for the ratchet. The declaration lives in the manifest
    # by CLASS NAME, but a live roster row is keyed by class name only when
    # source-1 (data/agent_registry.json) is populated — a per-host runtime
    # file written by the Chief-of-Staff factory. On a host that has never
    # completed a factory run — a developer Mac — source-1 is empty and the
    # same agent appears under its filename stem (source-2). The agent did
    # not vanish; the row's label is different. Fall back to a FILE match,
    # then to a raw on-disk check for rows whose module does not match the
    # MODULE_HINT (fe_pipeline, fe_moat_analyzer, …) and never lands in
    # source-2 either. SUPERSEDED closes the three documented renames whose
    # old filenames do not exist in the current tree.
    have_files = {a.get("file"): a for a in roster.get("agents", []) if a.get("file")}
    base = Path(__file__).resolve().parent

    def _file_present_on_disk(path_rel: str) -> bool:
        if not path_rel:
            return False
        return (base / path_rel).exists()

    def _matches(d: dict) -> bool:
        name = d.get("name")
        if name in have:
            return True
        f = d.get("file") or ""
        if f and f in have_files:
            return True
        if _file_present_on_disk(f):
            return True
        succ = SUPERSEDED.get(os.path.basename(f))
        if succ:
            for cand in have_files:
                if os.path.basename(cand) == succ:
                    return True
            if (base / succ).exists():
                return True
        return False

    vanished = []
    for name, d in declared.items():
        if _matches(d):
            continue
        if d.get("retired_at") and d.get("retirement_reason"):
            continue          # a recorded decision, not a loss
        vanished.append({"name": name, "file": d.get("file"),
                         "last_seen": d.get("last_seen"),
                         "hosts": d.get("hosts")})

    left_research = []
    for name, d in declared.items():
        if d.get("research_cycle") is not True:
            continue                       # never declared as loading, or unarmed
        # research_cycle is a RUNTIME claim, carried only by source-1 rows
        # (registry status), so we can only compare like-for-like when the
        # live roster has a row at this class name. On hosts without the
        # registry we cannot read research_cycle at all — the file might be
        # present under its stem (source-2) with research_cycle=False by
        # default, which is a representation artefact, not a demotion. The
        # VM, which has source-1, runs the actual check; this host reports
        # whichever rows it can resolve by name.
        a = have.get(name)
        if a is None or a.get("research_cycle"):
            continue                       # gone entirely (vanished), or still loading
        if d.get("retired_at") and d.get("retirement_reason"):
            continue                       # a recorded decision, not a loss
        left_research.append({"name": name, "file": d.get("file"),
                              "last_seen": d.get("last_seen"),
                              "status": a.get("status"),
                              "employment": a.get("employment")})

    added = sorted(set(have) - set(declared))
    unscheduled = sorted(n for n, a in have.items()
                         if a["runs_via"] == ["UNSCHEDULED"] and a["employment"] == "employed")
    lost = bool(vanished or left_research)
    return {
        "declared": len(declared),
        "discovered": len(have),
        "vanished": vanished,
        "left_research_cycle": sorted(left_research, key=lambda r: r["name"]),
        "research_cycle_declared": sum(1 for d in declared.values()
                                       if d.get("research_cycle") is True),
        "added": added,
        "unscheduled": unscheduled,
        "status": "RED" if lost else ("GREEN" if not added else "GREEN_GREW"),
    }


CHAIR_ACTION_PATH = DATA / "chairman_action_required.json"


def escalate(rep: dict) -> bool:
    """A RED ratchet has to reach a person, not an exit code.

    This runs from cron. `sys.exit(1)` into a log file is precisely the shape of
    failure this module was written to end — the orphaned KOSPI agent, the
    272-day buy freeze, the forked queue: all of them were *detected* somewhere
    nobody was reading. So a vanished agent is filed as an awaiting_chairman
    action, which chairman_decision_sla ingests into the app inbox and escalates
    to the board on its own if it goes 20 days undecided.

    The dedup_key is stable across runs (it names the agents, not the date), so
    a daily cron re-stating the same loss folds into one decision rather than
    filing thirty.

    The two loss classes are filed SEPARATELY. A vanished agent and an agent
    that stopped being loaded have different remedies — recover the row, versus
    find out why the loader declined it — and folding them into one "N agents
    lost" row is the aggregate that names no cause.
    """
    vanished = sorted(v["name"] for v in rep.get("vanished", []))
    left = sorted(v["name"] for v in rep.get("left_research_cycle", []))
    if not vanished and not left:
        return False

    pending = []
    if vanished:
        pending.append((
            "agent_roster:vanished:" + ",".join(vanished),
            f"roster-vanished-{datetime.date.today().isoformat()}",
            f"{len(vanished)} agent(s) vanished from the roster",
            (f"agent_roster --check found {len(vanished)} agent(s) declared in "
             f"agent_roster_manifest.json but discovered on NO host, with no "
             f"retired_at/retirement_reason recorded: {', '.join(vanished)}.\n\n"
             "An agent leaving the firm must be a decision, never a rename, a "
             "bad merge, or a deploy that overwrote a host's registry. Either "
             "recover it, or record the retirement and its reason so the "
             "manifest reflects a choice that was actually made.")))
    if left:
        by_status = {}
        for v in rep.get("left_research_cycle", []):
            by_status[v.get("status")] = by_status.get(v.get("status"), 0) + 1
        pending.append((
            "agent_roster:left_research_cycle:" + ",".join(left),
            f"roster-left-research-{datetime.date.today().isoformat()}",
            f"{len(left)} agent(s) stopped being loaded into the research cycle",
            (f"{len(left)} agent(s) still on the books, still employed, are no "
             f"longer loaded by research_team.py: {', '.join(left)}.\n\n"
             f"Current statuses: {by_status}.\n\n"
             "This is the shape of the 2026-08-28 incident: 43 agents moved to "
             "a coaching status the loader would not run, research coverage "
             "fell 84% in one cycle, and nothing vanished — so the ratchet was "
             "green. Either the status change was intended (record why), or the "
             "agents are being held out of service by a rule nobody chose.")))

    try:
        actions = _load(CHAIR_ACTION_PATH, [])
        if not isinstance(actions, list):
            return False
        seen = {a.get("dedup_key") for a in actions if isinstance(a, dict)}
        filed = False
        for key, aid, title, desc in pending:
            if key in seen:
                continue
            actions.append({
                "id": aid,
                "severity": "CRITICAL",
                "category": "GOVERNANCE",
                "title": title,
                "description": desc,
                "source": "agent_roster",
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "status": "awaiting_chairman",
                "dedup_key": key,
            })
            filed = True
        if filed:
            CHAIR_ACTION_PATH.write_text(json.dumps(actions, indent=2))
        return filed
    except Exception:
        return False


def adopt(roster: dict) -> dict:
    """Freeze the current roster as the declared manifest."""
    prev = {a["name"]: a for a in _load(MANIFEST_PATH, {}).get("agents", [])}
    today = datetime.date.today().isoformat()
    agents = []
    for a in roster["agents"]:
        p = prev.get(a["name"], {})
        agents.append({
            "name": a["name"], "kind": a["kind"], "file": a["file"],
            "tier": a["tier"], "team": a["team"],
            "employment": a["employment"],
            # The baseline the research-coverage half of the ratchet compares
            # against. Without it, an agent that stops being loaded is a silent
            # loss — see check()'s docstring for the 50→8 drop that proved it.
            #
            # Declared-True is sticky, for the same reason the loop below keeps
            # a vanished name: taking the discovered value would let ONE --adopt
            # re-baseline a demotion away and leave the ratchet permanently
            # green on it. Growth is free, shrinkage needs a reason — and the
            # reason is the same escape hatch check() honours, a recorded
            # retirement. Without this, the two halves of one ratchet had
            # opposite laundering rules: `vanished` was protected here since it
            # was written, `left_research_cycle` was not.
            "research_cycle": bool(a.get("research_cycle")) or (
                p.get("research_cycle") is True
                and not (p.get("retired_at") and p.get("retirement_reason"))
            ),
            "hosts": a["hosts"],
            "first_declared": p.get("first_declared", today),
            "last_seen": today,
            "retired_at": p.get("retired_at"),
            "retirement_reason": p.get("retirement_reason"),
        })
    # Anything already declared but not discovered stays in the manifest so the
    # ratchet keeps complaining about it. Dropping it here would let a vanished
    # agent be laundered out of existence by one --adopt.
    for name, p in prev.items():
        if name not in {a["name"] for a in agents}:
            agents.append(p)
    return {"generated_at": datetime.datetime.utcnow().isoformat(),
            "count": len(agents),
            "agents": sorted(agents, key=lambda a: a["name"])}


# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Build and ratchet the firm's canonical agent roster.")
    ap.add_argument("--report", action="store_true", help="print only, write nothing")
    ap.add_argument("--json", action="store_true", help="emit raw JSON (for --merge-remote over ssh)")
    ap.add_argument("--host", default="local", help="label this host in the roster")
    ap.add_argument("--merge-remote", action="store_true", help="fold the VM's roster in")
    ap.add_argument("--check", action="store_true", help="ratchet against the manifest; exit 1 on RED")
    ap.add_argument("--adopt", action="store_true", help="accept the current roster as declared")
    ap.add_argument("--sync-registry", action="store_true",
                    help="reconcile agent_registry.json across both hosts first")
    args = ap.parse_args()

    if args.sync_registry:
        rep = sync_registry(write=not args.report)
        print(f"  registry: mac {rep['mac']} + vm {rep['vm']} -> {rep['merged']} "
              f"(lost {len(rep['lost'])}){'  [written to both hosts]' if rep['wrote'] else '  [dry run]'}")

    roster = build(host=args.host)
    if args.merge_remote:
        roster = merge_rosters(roster, fetch_remote())

    if args.json:
        print(json.dumps(roster))
        return 0

    if not args.report:
        ROSTER_PATH.write_text(json.dumps(roster, indent=2))

    c = roster["counts"]
    print("=" * 68)
    print(f"  APEX CAPITAL — AGENT ROSTER  ({roster['host']})")
    print("=" * 68)
    print(f"  {c['total']} agents on the books "
          f"({c['registry']} in the CoS registry, {c['module']} module agents)")
    print(f"    employed {c['employed']} · superseded {c['superseded']} · retired {c['retired']}")
    print(f"    {c['research_cycle']} loaded into the research cycle · "
          f"{c['unscheduled']} employed but running nowhere")

    if args.adopt:
        MANIFEST_PATH.write_text(json.dumps(adopt(roster), indent=2))
        print(f"\n  adopted: {MANIFEST_PATH.name} now declares {c['total']} agents.")

    if args.check:
        rep = check(roster, _load(MANIFEST_PATH, {"agents": []}))
        print(f"\n  RATCHET: {rep['status']}  "
              f"(declared {rep['declared']} · discovered {rep['discovered']})")
        if rep["added"]:
            print(f"    +{len(rep['added'])} new since the manifest: "
                  f"{', '.join(rep['added'][:8])}{'…' if len(rep['added']) > 8 else ''}")
        # The research-coverage half only means anything once a manifest has
        # been adopted that records it. Say so, rather than letting an unarmed
        # ratchet read as a clean one.
        if not rep["research_cycle_declared"]:
            print("    research-coverage ratchet NOT ARMED — no manifest row "
                  "declares research_cycle; run --adopt to arm it")
        if rep["vanished"] or rep["left_research_cycle"]:
            if rep["vanished"]:
                print(f"\n  ⛔ {len(rep['vanished'])} DECLARED AGENT(S) HAVE VANISHED "
                      f"with no recorded retirement:")
                for v in rep["vanished"]:
                    print(f"       • {v['name']}  ({v['file']}, last seen {v['last_seen']})")
            if rep["left_research_cycle"]:
                print(f"\n  ⛔ {len(rep['left_research_cycle'])} DECLARED AGENT(S) NO "
                      f"LONGER LOAD INTO THE RESEARCH CYCLE "
                      f"(of {rep['research_cycle_declared']} declared):")
                for v in rep["left_research_cycle"]:
                    print(f"       • {v['name']}  (status {v['status']}, "
                          f"{v['employment']}, last seen {v['last_seen']})")
            if not args.report:
                filed = escalate(rep)
                print(f"    {'filed for the Chairman' if filed else 'already filed'} "
                      f"— chairman_action_required.json")
            print()
            return 1
        if rep["unscheduled"]:
            print(f"    {len(rep['unscheduled'])} employed agent(s) run nowhere: "
                  f"{', '.join(rep['unscheduled'][:6])}"
                  f"{'…' if len(rep['unscheduled']) > 6 else ''}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

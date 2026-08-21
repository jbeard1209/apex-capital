#!/usr/bin/env python3
"""
Parameter Audit — which numbers in this firm were DERIVED, and which were guessed?
================================================================================
Jack's lesson, 2026-08-21, generalised into a control:

    "I initially priced the staleness guard at 48h, then measured it — a 19.7h
     snapshot on the VM, well inside the threshold, held 160 agents where the
     tree held 161. Age was the wrong margin; the hazard is deploys, which
     outnumber roster runs several to one."

The threshold was not too loose. The QUANTITY underneath it was wrong, and no
value of a wrong quantity is right. The same shape has now been found four times
in this codebase (see data/parameter_derivations.json), and each time it was
found by hand, by someone who happened to look. This module is the part that
does not depend on anyone happening to look.

WHAT IT MEASURES
    Every module-level numeric constant, narrowed in three steps, each with its
    own denominator so a shrinking numerator cannot be mistaken for progress:
        constants  -> those compared against something (thresholds)
                   -> those whose comparison gates an if-branch
                   -> those whose gated branch DOES something to the firm
    Only the last group matters. A threshold that gates a log line is a
    preference; a threshold that gates an order, an email, an escalation or a
    status change is a policy, and a policy nobody derived is a guess with
    consequences.

WHAT IT WILL NOT DO
    It never edits a parameter. Deriving a number and changing a live limit are
    different acts, and the second one is the Chairman's (the same rule that
    pins pick_gate_trial_review.py out of config.py). This reports; Jack
    decides. That separation is pinned by a test.

WHY AST AND NOT GREP
    Every parameter worth auditing is discussed in some postmortem comment in
    this repo, usually by name, usually next to the words "threshold" and
    "measured". A grep-based auditor reads those comments and reports the
    parameter as documented — it would grade itself against my own prose. So
    constants, comparisons and branch bodies are all read from the AST, and
    comments are invisible to it by construction.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional          # py3.9 on the Mac: never `str | None`

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
REGISTRY_PATH = DATA / "parameter_derivations.json"
BASELINE_PATH = DATA / "parameter_audit_baseline.json"
REPORT_PATH = DATA / "parameter_audit_report.json"

# Directories that are not the firm's own decision-making code.
SKIP_DIRS = {"__pycache__", ".git", "venv", "node_modules", "gcp_setup",
             "scripts", "archive", "logs", "data"}

# A constant is a PARAMETER candidate only if it is a bare number. Strings,
# paths, lists of names and booleans are configuration, not thresholds.
#
# 0 and 1 are excluded deliberately and this is a judgement call worth stating:
# they are overwhelmingly sentinels (empty, first, on/off) rather than prices,
# and including them buried the real findings 6:1 when measured on 2026-08-21.
# The cost is real — LOOP_RECUR_THRESHOLD = 1 IS a priced parameter and is
# invisible here — so anything set to 0 or 1 that genuinely gates a consequence
# must be registered by hand. LOOP_RECUR_THRESHOLD is, for exactly this reason.
TRIVIAL_VALUES = (0, 1, 0.0, 1.0, -1)

# Calls whose presence in a branch body means the branch DOES something. Matched
# on the attribute/function name only, so `x.submit_order(...)` and
# `submit_order(...)` both count. Substring matching on purpose: the firm names
# these consistently and a new `submit_bracket_order` should count on day one.
CONSEQUENTIAL_CALLS = (
    "submit_order", "submit_buy", "submit_sell", "close_position",
    "cancel_order", "liquidate",
    "send_email", "send_mail", "notify", "_send",
    "escalate", "queue_proposal", "submit_to_pod_queue", "append_deduped",
    "set_mode", "mark_deployed", "block", "halt", "trip",
)
# Assigning one of these names inside a branch is also a consequence: it is how
# most of this codebase records a verdict.
CONSEQUENTIAL_ASSIGNS = ("status", "verdict", "severity", "action", "decision",
                         "approved", "blocked", "alarm")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def python_files(root: Optional[Path] = None) -> list:
    """Every .py file that is part of the firm's decision-making code."""
    root = root or HERE
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        if p.name.startswith("test_") or p.name == Path(__file__).name:
            continue
        out.append(p)
    return out


def _numeric(node) -> Optional[float]:
    """The numeric value of a literal, including a negated one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand)
        return None if inner is None else -inner
    return None


def module_constants(tree: ast.Module) -> dict:
    """{NAME: value} for module-level numeric constants (UPPER_SNAKE only).

    Restricted to UPPER_SNAKE because that is this codebase's convention for a
    tunable, and lowercase module-level numbers are overwhelmingly derived
    working values rather than dials.
    """
    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        val = _numeric(node.value) if node.value is not None else None
        if val is None or val in TRIVIAL_VALUES:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                out[t.id] = val
    return out


def _names_in(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _branch_is_consequential(body: list) -> bool:
    """Does this branch body do something to the firm, or only observe?"""
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                f = n.func
                name = getattr(f, "attr", None) or getattr(f, "id", "")
                low = str(name).lower()
                if any(c in low for c in CONSEQUENTIAL_CALLS):
                    return True
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    tn = getattr(t, "attr", None) or getattr(t, "id", "")
                    if any(c in str(tn).lower() for c in CONSEQUENTIAL_ASSIGNS):
                        return True
            if isinstance(n, (ast.Return, ast.Raise)):
                return True
    return False


def audit_module(path: Path) -> dict:
    """Classify every module-level numeric constant in one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as e:                      # pragma: no cover - defensive
        return {"error": "%s: %s" % (type(e).__name__, e)}
    consts = module_constants(tree)
    if not consts:
        return {"constants": {}, "compared": set(), "gating": set(),
                "consequential": set()}

    compared, gating, consequential = set(), set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        used = _names_in(node) & set(consts)
        if not used:
            continue
        compared |= used

    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp, ast.While)):
            continue
        test = node.test
        used = _names_in(test) & set(consts)
        if not used or not any(isinstance(n, ast.Compare) for n in ast.walk(test)):
            continue
        gating |= used
        body = node.body if isinstance(node.body, list) else [node.body]
        orelse = getattr(node, "orelse", [])
        orelse = orelse if isinstance(orelse, list) else [orelse]
        if _branch_is_consequential(body) or _branch_is_consequential(orelse):
            consequential |= used
    return {"constants": consts, "compared": compared, "gating": gating,
            "consequential": consequential}


# ---------------------------------------------------------------------------
# Reachability. A threshold in code nothing runs is not a policy.
# ---------------------------------------------------------------------------
# Files that name what actually gets executed. The crontab snapshot is the
# canonical prod schedule (a mirror of the VM's real crontab, refreshed by the
# audit that captures it); restore_crontab.sh is the script that installs it.
SCHEDULE_SOURCES = ("data/vm_crontab_snapshot.txt", "gcp_setup/restore_crontab.sh")
# The two documented entry points. main.py is the daily pipeline; catch_up.py is
# the recovery scheduler. Both are roots by definition — nothing imports them.
ENTRY_POINTS = ("main.py", "catch_up.py", "dashboard_server.py")


def schedule_roots(root: Optional[Path] = None) -> set:
    """Module stems named by something that actually runs them."""
    root = root or HERE
    stems = set(Path(e).stem for e in ENTRY_POINTS)
    import re
    for rel in SCHEDULE_SOURCES:
        p = root / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        stems |= {Path(m).stem for m in re.findall(r"[\w./-]+\.py", text)}
    return stems


def import_edges(files: list) -> dict:
    """{module stem: set of module stems it imports}, first-party only."""
    known = {p.stem for p in files} | set(Path(e).stem for e in ENTRY_POINTS)
    edges = {}
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:                       # pragma: no cover - defensive
            continue
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    out.add(a.name.split(".")[-1])
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.split(".")[-1])
                for a in n.names:
                    out.add(a.name)
            # `subprocess.run([..., "foo.py"])` is an import in every sense that
            # matters here: it is how this codebase's schedulers reach a module.
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and n.value.endswith(".py"):
                out.add(Path(n.value).stem)
        edges[p.stem] = out & known
    return edges


def reachable_modules(files: list, root: Optional[Path] = None) -> set:
    """Transitive closure of imports from everything a scheduler actually runs.

    Fails OPEN on purpose. If the schedule sources are missing or empty, the
    honest answer is "I cannot tell what runs", and the safe response is to
    treat every module as reachable — under-counting reachability would silently
    excuse live parameters from the audit, which is the exact failure this
    module exists to prevent. A detector that goes quiet when its input is
    missing is worse than no detector.
    """
    roots = schedule_roots(root)
    stems = {p.stem for p in files}
    if not (roots & stems):
        return set(stems)                          # cannot tell -> audit all
    edges = import_edges(files)
    seen, queue = set(), [r for r in roots if r in stems]
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        queue.extend(edges.get(cur, ()))
    return seen


def load_registry() -> dict:
    try:
        blob = json.loads(REGISTRY_PATH.read_text())
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _key(module: str, name: str) -> str:
    return "%s::%s" % (module, name)


def _is_measured(rec: dict) -> bool:
    """Does this registry entry cite a MEASUREMENT, or just an opinion?

    Requires all three of: the quantity that was measured, how many things were
    measured, and when. Any one of them missing and the entry is an assertion.
    `n` is allowed to be 0 only for an analytic derivation (a standard-error
    argument measures no sample), which must say so by setting n to null and
    giving `analytic: true`.
    """
    if not isinstance(rec, dict):
        return False
    m = rec.get("measured")
    if not isinstance(m, dict):
        return False
    if not str(m.get("quantity") or "").strip():
        return False
    if not str(m.get("on") or "").strip():
        return False
    if m.get("analytic") is True:
        return True
    return isinstance(m.get("n"), (int, float)) and m["n"] > 0


def run(root: Optional[Path] = None) -> dict:
    """Census every parameter and report the undocumented consequential ones."""
    reg = load_registry()
    files = python_files(root)
    live = reachable_modules(files, root)
    n_const = n_cmp = n_gate = 0
    consequential, undocumented, documented, drifted = [], [], [], []
    dormant, asserted = [], []

    for path in files:
        res = audit_module(path)
        if "error" in res:
            continue
        mod = path.name
        n_const += len(res["constants"])
        n_cmp += len(res["compared"])
        n_gate += len(res["gating"])
        for name in sorted(res["consequential"]):
            k = _key(mod, name)
            entry = {"key": k, "module": mod, "name": name,
                     "value": res["constants"][name],
                     "reachable": path.stem in live}
            consequential.append(entry)
            rec = reg.get(k) or reg.get(name)
            if rec is not None and not _is_measured(rec):
                # A REASON is not a DERIVATION. "We need a cumulative cap" says
                # why the dial exists; it does not say why the dial is 60.
                # Accepting prose here would turn the registry into a rubber
                # stamp and the audit into a check that an explanation EXISTS —
                # which is satisfied forever by one stale paragraph. So an entry
                # with no measurement is reported by name and still counts as
                # undocumented.
                entry["asserted_reason"] = str(rec.get("rationale")
                                               or rec.get("derived_from"))[:160]
                asserted.append(entry)
                (undocumented if entry["reachable"] else dormant).append(entry)
                continue
            if not rec:
                # A dial in code no scheduler can reach is not a policy. It is
                # still reported — dormant code gets revived — but it does not
                # sit in the number the ratchet guards, because a backlog of
                # dead parameters would drown the live ones and make the
                # ratchet unmovable.
                (undocumented if entry["reachable"] else dormant).append(entry)
                continue
            documented.append(entry)
            # A derivation that no longer describes the live value is worse than
            # no derivation: it reads as due diligence. Report it separately.
            if "value" in rec and rec["value"] != entry["value"]:
                drifted.append({**entry, "registered_value": rec["value"],
                                "derived_from": rec.get("derived_from")})

    report = {
        "ts": _now(),
        "modules_examined": len(files),
        "modules_reachable": len(live & {p.stem for p in files}),
        "n_constants": n_const,
        "n_compared": n_cmp,
        "n_gating_a_branch": n_gate,
        "n_consequential": len(consequential),
        "n_consequential_reachable": sum(1 for e in consequential if e["reachable"]),
        "n_documented": len(documented),
        "n_undocumented": len(undocumented),
        "n_dormant": len(dormant),
        "n_asserted_not_measured": len(asserted),
        "n_drifted": len(drifted),
        "undocumented": undocumented,
        "dormant": dormant,
        "asserted_not_measured": asserted,
        "drifted": drifted,
        "consequential_definition": {
            "calls": list(CONSEQUENTIAL_CALLS),
            "assigns": list(CONSEQUENTIAL_ASSIGNS),
            "note": "a branch counts if it calls one of these, assigns one of "
                    "these names, or returns/raises",
        },
        "excluded_trivial_values": list(TRIVIAL_VALUES),
    }
    return report


def ratchet(report: dict) -> dict:
    """Shrink-only baseline on the undocumented count.

    The number may fall (someone derived a parameter) or stay flat. It may not
    rise silently: a new consequential threshold with no derivation is the exact
    thing this module exists to catch, and without a ratchet it would simply
    join the pile and be averaged away. Same shape as the orphan-agent baseline.
    """
    n = report["n_undocumented"]
    try:
        base = json.loads(BASELINE_PATH.read_text())
        prev = int(base.get("undocumented", n))
    except Exception:
        prev = None
    if prev is None:
        BASELINE_PATH.write_text(json.dumps(
            {"undocumented": n, "set_on": report["ts"],
             "note": "shrink-only: this number may fall, never rise"}, indent=2))
        return {"baseline": n, "status": "SET", "delta": 0}
    if n > prev:
        return {"baseline": prev, "status": "REGRESSED", "delta": n - prev}
    if n < prev:
        BASELINE_PATH.write_text(json.dumps(
            {"undocumented": n, "set_on": report["ts"],
             "note": "shrink-only: this number may fall, never rise"}, indent=2))
        return {"baseline": n, "status": "IMPROVED", "delta": n - prev}
    return {"baseline": prev, "status": "FLAT", "delta": 0}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    rep = run()
    rat = ratchet(rep)
    rep["ratchet"] = rat
    try:
        REPORT_PATH.write_text(json.dumps(rep, indent=2, default=str))
    except Exception:                             # pragma: no cover - defensive
        pass

    print("=" * 74)
    print("  PARAMETER AUDIT — which numbers were derived, and which guessed?")
    print("=" * 74)
    print("  modules examined            : %d  (reachable from a scheduler: %d)"
          % (rep["modules_examined"], rep["modules_reachable"]))
    print("  module-level numeric consts : %d" % rep["n_constants"])
    print("    ...compared against sth   : %d" % rep["n_compared"])
    print("    ...gating an if-branch    : %d" % rep["n_gating_a_branch"])
    print("    ...gating a CONSEQUENCE   : %d   (of which reachable: %d)"
          % (rep["n_consequential"], rep["n_consequential_reachable"]))
    print()
    print("  LIVE dials with a derivation: %d" % rep["n_documented"])
    print("  LIVE dials WITHOUT one      : %d   <- the number that matters"
          % rep["n_undocumented"])
    print("  in dormant code (reported,")
    print("    not ratcheted)            : %d" % rep["n_dormant"])
    if rep["n_drifted"]:
        print("  registered value now STALE  : %d   <- worse than undocumented"
              % rep["n_drifted"])
        for d in rep["drifted"]:
            print("      %-52s live=%s registered=%s"
                  % (d["key"][:52], d["value"], d["registered_value"]))
    print("  ratchet: %s (baseline %s, delta %+d)"
          % (rat["status"], rat["baseline"], rat["delta"]))
    if "-v" in argv or "--verbose" in argv:
        print("\n  undocumented consequential parameters:")
        for e in rep["undocumented"]:
            print("      %-52s = %s" % (e["key"][:52], e["value"]))
    else:
        print("\n  (run with -v to list them; full report -> %s)"
              % REPORT_PATH.name)
    if rat["status"] == "REGRESSED":
        print("\n  REGRESSED: %d new consequential parameter(s) with no "
              "derivation." % rat["delta"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

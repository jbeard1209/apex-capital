#!/usr/bin/env python3
"""Weekly per-agent health sweep — every agent the firm employs, probed in a sandbox.

WHAT THIS ANSWERS, PER AGENT
    Does it load?            (import probe)
    Does it run?             (`--report` probe)
    Does it speak to anyone? (file + import graph)
    Does it come to work?    (schedule + output freshness)
    Does the firm know it?   (roster / registry / HR review)

WHY IT IS SANDBOXED
    Some of these agents send email and some talk to the broker. A naive
    "import every module and see what happens" sweep would, on its first run,
    email the Chairman a hundred times and touch a live paper account. So the
    probe runs in a subprocess with:

        * all outbound sockets refused          (kills email, Alpaca, yfinance, HTTP)
        * smtplib refused at construction       (belt and braces on the email path)
        * subprocess spawning refused           (no escaping the sandbox)
        * every file write and delete refused   (the firm's state cannot change)

    Blocked writes are NOT reported as crashes. A write attempt is classified as
    its own outcome, because reaching the point where an agent writes its report
    is positive evidence that the agent ran — and because writing at *import*
    time is a real defect worth reporting on its own (AGENT_WRITES_ON_IMPORT).

    The sweep therefore cannot change firm state, which is what lets it run
    unattended on a weekly cron against 155 agents.

Findings are returned as `sweep_taxonomy` findings and consumed by
`sweep_governance.py`, which routes them to the board, HR and CIT. Running this
module alone reports; it never files anything.
"""

from __future__ import annotations

import argparse
import builtins
import concurrent.futures
import datetime
import json
import os
import re
import runpy
import shutil
import socket
import subprocess
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

IMPORT_TIMEOUT_S = 30
REPORT_TIMEOUT_S = 75
PARALLELISM = 6

# An output older than this, from an agent that IS scheduled, is an attendance
# failure. Deliberately generous: the slowest legitimate cadence in the firm is
# weekly, so 14 days is two missed runs, not one unlucky one.
STALE_OUTPUT_DAYS = 14

# Modules that are infrastructure rather than agents. The roster already excludes
# most; these are the ones that would otherwise be probed as if they were staff.
NOT_PROBED = {
    "agent_sweep", "sweep_governance", "sweep_taxonomy",
    "detector_mutation_harness", "test_pipeline_smoke", "conftest", "setup",
}

# An unread output is only a DEFECT when the file was meant to be handed to
# another module. Three shapes are legitimately terminal and must not be flagged:
#
#   *_report / *_briefing / *_audit  — written for a human or an email, not code
#   *cache* / *_state / *_history    — the module's own memory, re-read by itself
#                                      next run (refs_of excludes self by design)
#   *_results / *_meta               — experiment artifacts read by a person
#
# This is a deliberate precision-over-recall trade. Flagging all 52 unread outputs
# would be defensible on the letter of the rule and useless in practice: a board
# packet of 52 items on week one gets skimmed and then ignored, and an ignored
# alarm is a dead alarm. The count of suppressed ones is reported in stats
# ("unconsumed_terminal") so the choice stays visible instead of silently
# shrinking the sweep.
_HANDOFF_RE = re.compile(
    r'(queue|pending|approved|nomination|candidate|signal|alert|trigger'
    r'|memo|inbox|request|task|escalat|position_sizes)', re.I)
_TERMINAL_RE = re.compile(r'(report|briefing|audit|cache|_state|history|results|_meta)', re.I)

# Timestamp fields agents actually stamp their reports with, best first. Preferred
# over mtime: a deploy or a git checkout rewrites mtime and would fake freshness.
_TS_KEYS = ("generated_at", "timestamp", "last_run", "updated_at", "as_of", "date")


class SandboxViolation(Exception):
    """Raised inside the probe when an agent tries to escape the sandbox.

    Distinct type so the parent can tell 'the agent tried to write its report'
    (healthy) from 'the agent crashed' (a finding).
    """


# ===========================================================================
# THE SANDBOX — installed only inside a --probe subprocess, never in-process.
# ===========================================================================
def _install_sandbox() -> None:
    def _refuse(what):
        def blocked(*a, **k):
            raise SandboxViolation(what)
        return blocked

    # ── network: one chokepoint covers email, broker and every HTTP client ──
    socket.socket.connect = _refuse("network connect")
    socket.socket.connect_ex = _refuse("network connect_ex")
    socket.create_connection = _refuse("network create_connection")

    # ── email: refuse at construction so a stubbed transport cannot slip past ─
    try:
        import smtplib
        smtplib.SMTP.__init__ = _refuse("smtplib.SMTP")
        smtplib.SMTP_SSL.__init__ = _refuse("smtplib.SMTP_SSL")
    except Exception:
        pass

    # ── no escaping via a child process ─────────────────────────────────────
    subprocess.Popen.__init__ = _refuse("subprocess.Popen")
    os.system = _refuse("os.system")

    # ── firm state is read-only for the duration of the probe ───────────────
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in ("w", "a", "x", "+")):
            raise SandboxViolation("open(%s, %r)" % (os.path.basename(str(file)), mode))
        return real_open(file, mode, *a, **k)

    builtins.open = guarded_open
    Path.write_text = _refuse("Path.write_text")
    Path.write_bytes = _refuse("Path.write_bytes")
    Path.unlink = _refuse("Path.unlink")
    os.replace = _refuse("os.replace")
    os.rename = _refuse("os.rename")
    os.remove = _refuse("os.remove")
    os.unlink = _refuse("os.unlink")
    shutil.rmtree = _refuse("shutil.rmtree")
    # mkdir/makedirs stay allowed on purpose: they are idempotent, create no
    # content, and blocking them turns a harmless `logs/` bootstrap into a
    # fake import failure.

    os.environ["APEX_SWEEP_SANDBOX"] = "1"


def _probe(stem: str, mode: str, syspath: str = None) -> dict:
    """Run inside the subprocess. Prints one JSON verdict on stdout.

    `syspath` exists so the mutation harness can point the probe at a throwaway
    directory holding a deliberately broken module. Without it there is no way to
    prove this probe can fail, and "90 agents, zero import failures" would be
    indistinguishable from a probe that never ran.
    """
    _install_sandbox()
    sys.path.insert(0, syspath or str(BASE))
    out = {"module": stem, "mode": mode, "outcome": "OK", "detail": ""}
    try:
        if mode == "import":
            __import__(stem)
        else:
            sys.argv = ["%s.py" % stem, "--report"]
            runpy.run_module(stem, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        code = e.code
        if code not in (0, None):
            out["outcome"] = "EXIT"
            out["detail"] = "exited with status %r" % (code,)
    except SandboxViolation as e:
        out["outcome"] = "SANDBOX"
        out["detail"] = str(e)
    except BaseException as e:                       # noqa: BLE001 — verdict, not control flow
        tb = traceback.format_exc()
        # A sandbox violation re-raised as something else still means the agent
        # reached a write, not that it is broken. Classify on the cause.
        out["outcome"] = "SANDBOX" if "SandboxViolation" in tb else "FAIL"
        out["detail"] = ("%s: %s" % (type(e).__name__, e))[:400]
        out["traceback"] = tb[-1500:]
    return out


def _run_probe(stem: str, mode: str, syspath: str = None) -> dict:
    """Parent side: spawn the probe, enforce a timeout, decode the verdict."""
    timeout = IMPORT_TIMEOUT_S if mode == "import" else REPORT_TIMEOUT_S
    cmd = [sys.executable, str(BASE / "agent_sweep.py"), "--probe", stem, "--mode", mode]
    if syspath:
        cmd += ["--syspath", syspath]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(BASE))
    except subprocess.TimeoutExpired:
        return {"module": stem, "mode": mode, "outcome": "TIMEOUT",
                "detail": "no verdict within %ds" % timeout}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    # No verdict line at all: the probe itself died (segfault, os._exit, a module
    # that called sys.exit before we could catch it). That is still a failure of
    # the agent, and must never be silently treated as OK.
    return {"module": stem, "mode": mode, "outcome": "NO_VERDICT",
            "detail": ("rc=%s stderr=%s" % (p.returncode, (p.stderr or "")[-300:])).strip()}


# ===========================================================================
# STATIC GRAPH — who writes what, who reads what, who imports whom.
# ===========================================================================
def _sources() -> dict:
    out = {}
    for p in sorted(BASE.glob("*.py")):
        try:
            out[p.stem] = p.read_text()
        except Exception:
            pass
    return out


def _graph(sources: dict) -> dict:
    """writes/reads per module, and the reverse indices.

    Uses wiring_audit's regexes, not its own — see the note on DATA_JSON_RE there.
    Test files are excluded from the graph entirely: being named by a test is not
    employment, and a test that merely mentions a file must not count as its
    consumer. wiring_audit learned that the hard way; this inherits the lesson.
    """
    import wiring_audit
    out_re = wiring_audit.DATA_JSON_RE

    prod = {s: t for s, t in sources.items() if not s.startswith("test_")}
    writer_stems = {s for s, t in prod.items() if wiring_audit.WRITE_RE.search(t)}

    writes, reads, imports = {}, {}, {}
    for stem, txt in prod.items():
        code = wiring_audit._code_only(txt)
        w = set(wiring_audit._declared_outputs(
            code, out_re, writer_stems=writer_stems - {stem}))
        allrefs = set(out_re.findall(code))
        writes[stem] = w
        reads[stem] = allrefs - w
        imports[stem] = {m for m in re.findall(r'^\s*(?:from|import)\s+([a-zA-Z_]\w*)',
                                               code, re.M) if m in prod and m != stem}
    sources = prod

    writers_of, readers_of, refs_of = {}, {}, {}
    for stem in prod:
        for f in writes[stem]:
            writers_of.setdefault(f, set()).add(stem)
            refs_of.setdefault(f, set()).add(stem)
        for f in reads[stem]:
            readers_of.setdefault(f, set()).add(stem)
            refs_of.setdefault(f, set()).add(stem)

    # A queue is normally read-modify-written by its consumer, so `reads` alone
    # (which is allrefs minus writes) counts that consumer as a writer and nobody
    # as a reader — which made every genuinely-wired queue in the firm look
    # orphaned. cit_clarification_requests.json is written by cit_director and
    # consumed by deputy_board and board_meeting; all three write it, so the naive
    # index found zero readers for a three-party contract. Consumption is
    # therefore "referenced by some OTHER module", the same test
    # wiring_audit.check_orphan_queues uses.
    #
    # Non-Python assets count too: a report whose only consumer is the dashboard
    # or a shell script is consumed, and flagging it would be a false positive.
    for asset in list(BASE.glob("*.html")) + list(BASE.glob("*.js")) + \
            list(BASE.glob("scripts/*.sh")) + list(BASE.glob("gcp_setup/*.sh")):
        try:
            txt = asset.read_text()
        except Exception:
            continue
        for f in set(out_re.findall(txt)):
            refs_of.setdefault(f, set()).add(asset.name)

    imported_by = {}
    for stem, deps in imports.items():
        for d in deps:
            imported_by.setdefault(d, set()).add(stem)

    return {"writes": writes, "reads": reads, "imports": imports,
            "writers_of": writers_of, "readers_of": readers_of,
            "refs_of": refs_of, "imported_by": imported_by}


def _age_days(fname: str, now: datetime.datetime):
    """Age of a data file in days, or None if it has never existed.

    Prefers a timestamp the agent stamped into its own report over the file's
    mtime, because a deploy or a checkout rewrites mtime and would report a dead
    agent as fresh.
    """
    p = DATA / fname
    if not p.exists():
        return None
    stamped = None
    try:
        blob = json.loads(p.read_text())
        if isinstance(blob, dict):
            for k in _TS_KEYS:
                v = blob.get(k)
                if isinstance(v, str) and len(v) >= 10:
                    stamped = v[:19]
                    break
    except Exception:
        pass
    if stamped:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return (now - datetime.datetime.strptime(stamped[:len(fmt) + 2].strip(), fmt)).days
            except ValueError:
                continue
    return (now - datetime.datetime.utcfromtimestamp(p.stat().st_mtime)).days


# ===========================================================================
# THE SWEEP
# ===========================================================================
def collect(probe: bool = True, host: str = None, now: datetime.datetime = None,
            only: list = None) -> dict:
    """Run the sweep. Returns {'findings': [...], 'stats': {...}}.

    `probe=False` runs the static half only — used by the contract tests, which
    must not spawn 155 subprocesses.
    """
    import agent_roster
    from sweep_taxonomy import make_finding

    now = now or datetime.datetime.utcnow()
    host = host or os.uname().nodename
    sources = _sources()
    g = _graph(sources)
    roster = agent_roster.build(BASE, host=host)
    agents = roster.get("agents", [])
    findings, stats = [], {"agents_seen": 0, "probed": 0}

    hr_rated = set()
    try:
        hr = json.loads((DATA / "hr_history.json").read_text())
        hr_rated = set((hr.get("agent_records") or {}).keys())
    except Exception:
        hr_rated = set()          # unknown, not empty — see the guard below

    def add(kind, subject, detail, evidence=None, severity=None):
        findings.append(make_finding(kind, subject, detail, evidence,
                                     severity=severity, host=host))

    probe_targets, seen_stems = {}, set()
    for a in agents:
        name = a.get("name") or ""
        f = a.get("file") or ""
        stem = os.path.basename(f)[:-3] if f.endswith(".py") else ""
        employed = a.get("employment") == "employed"
        scheduled = a.get("runs_via") and a.get("runs_via") != ["UNSCHEDULED"]
        stats["agents_seen"] += 1

        if only and name not in only and stem not in only:
            continue

        # ── the roster names a file that is not there ───────────────────────
        if f and not a.get("file_exists") and a.get("employment") != "superseded":
            add("AGENT_FILE_MISSING", name,
                "The roster lists %s as this agent's module, but that file does not "
                "exist on %s." % (f, host), [f])
            continue

        # ── retired on paper, still running ────────────────────────────────
        if a.get("employment") == "retired" and (a.get("research_cycle") or scheduled):
            add("AGENT_ZOMBIE", name,
                "Registry marks this agent retired (%s), but it is still loaded: "
                "research_cycle=%s runs_via=%s."
                % (a.get("registry_status"), a.get("research_cycle"), a.get("runs_via")),
                [a.get("retirement_reason") or "no retirement reason recorded"])

        # ── employed but on nobody's schedule ──────────────────────────────
        if employed and not scheduled:
            add("AGENT_UNSCHEDULED", name,
                "Employed on the roster but reachable from no crontab entry and no "
                "scheduler root on %s. It never runs." % host,
                [f or "(no module file)"])

        # ── HR has never scored it ─────────────────────────────────────────
        if employed and a.get("kind") == "registry" and hr_rated and name not in hr_rated:
            add("AGENT_UNREVIEWED", name,
                "No HR evaluation record exists for this agent, so its performance "
                "has never been graded.", ["absent from hr_history.json agent_records"])

        if not stem or stem not in sources or stem in NOT_PROBED:
            continue

        # Five registry agents share cit_director.py. A wiring defect in that
        # module is ONE defect, not five, and filing it once per agent name would
        # bury the board under duplicates of a single fix. Employment findings
        # above are per-agent; everything from here down is per-module.
        if stem in seen_stems:
            continue
        seen_stems.add(stem)

        # ── connectivity ───────────────────────────────────────────────────
        my_w, my_r = g["writes"].get(stem, set()), g["reads"].get(stem, set())
        consumers = {m for fl in my_w for m in g["refs_of"].get(fl, set())} - {stem}
        producers = {m for fl in my_r for m in g["refs_of"].get(fl, set())} - {stem}
        peers_in = g["imported_by"].get(stem, set()) - {stem}
        peers_out = g["imports"].get(stem, set()) - {stem}

        if not (consumers or producers or peers_in or peers_out):
            add("AGENT_ISOLATED", stem,
                "%s speaks to no other module in the firm: nothing reads its output, "
                "nothing produces its input, it imports nothing of ours and nothing "
                "imports it. It runs alone and its conclusions reach no one." % stem,
                ["registry name: %s" % name,
                 "writes: %s" % (sorted(my_w) or "nothing"),
                 "reads: %s" % (sorted(my_r) or "nothing")])
        else:
            for fl in sorted(my_w):
                if g["refs_of"].get(fl, set()) - {stem}:
                    continue
                if _TERMINAL_RE.search(fl) or not _HANDOFF_RE.search(fl):
                    stats["unconsumed_terminal"] = stats.get("unconsumed_terminal", 0) + 1
                    continue
                add("AGENT_OUTPUT_UNCONSUMED", "%s -> %s" % (stem, fl),
                        "%s writes %s every run and no module in the firm reads "
                        "it. That work is produced and discarded." % (stem, fl), [fl])
            for fl in sorted(my_r):
                # A missing writer is only evidence of a missing PRODUCER when the
                # file also isn't there. _declared_outputs is deliberately
                # conservative — it reports only writes it can prove — so "no
                # module writes this" is routinely wrong for a file that plainly
                # exists on disk. Gating on existence turned 72 confident false
                # positives into the handful of genuinely never-produced inputs.
                if not g["writers_of"].get(fl, set()) and not (DATA / fl).exists():
                    add("AGENT_INPUT_UNPRODUCED", "%s <- %s" % (stem, fl),
                        "%s reads %s, but no module writes it and it has never "
                        "existed on %s — the agent runs on a default forever."
                        % (stem, fl, host), [fl])

        # ── attendance: is its output actually moving? ─────────────────────
        if scheduled and my_w:
            ages = {fl: _age_days(fl, now) for fl in sorted(my_w)}
            if ages and all(v is None for v in ages.values()):
                add("AGENT_OUTPUT_NEVER_PRODUCED", stem,
                    "%s is scheduled (%s) but none of its declared outputs has ever "
                    "been written: %s." % (stem, ",".join(a.get("runs_via") or []),
                                           ", ".join(sorted(ages))),
                    sorted(ages))
            else:
                live = {k: v for k, v in ages.items() if v is not None}
                if live and min(live.values()) > STALE_OUTPUT_DAYS:
                    add("AGENT_OUTPUT_STALE", stem,
                        "%s is scheduled (%s) but its freshest output is %d days old "
                        "(threshold %d). It is on the roster and on the crontab and "
                        "has stopped producing."
                        % (stem, ",".join(a.get("runs_via") or []),
                           min(live.values()), STALE_OUTPUT_DAYS),
                        ["%s: %sd" % (k, v) for k, v in sorted(live.items())])

        if employed:
            probe_targets[stem] = (name, stem, "--report" in sources[stem])

    # ── the dynamic half ───────────────────────────────────────────────────
    if probe and probe_targets:
        findings.extend(_probe_all(sorted(probe_targets.values()), host))
        stats["probed"] = len(probe_targets)

    stats["findings"] = len(findings)
    stats["by_kind"] = {}
    for f in findings:
        stats["by_kind"][f["kind"]] = stats["by_kind"].get(f["kind"], 0) + 1
    return {"generated_at": now.isoformat(), "host": host,
            "findings": findings, "stats": stats,
            "roster_counts": roster.get("counts", {})}


def _probe_all(targets: list, host: str) -> list:
    from sweep_taxonomy import make_finding
    out = []

    def one(t):
        name, stem, has_report = t
        imp = _run_probe(stem, "import")
        rep = _run_probe(stem, "report") if (has_report and imp["outcome"] in ("OK", "SANDBOX")) else None
        return name, stem, imp, rep

    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        for name, stem, imp, rep in pool.map(one, targets):
            if imp["outcome"] == "TIMEOUT":
                out.append(make_finding("AGENT_IMPORT_TIMEOUT", name,
                    "Importing %s did not finish in %ds — something blocks at module "
                    "level." % (stem, IMPORT_TIMEOUT_S), [imp["detail"]], host=host))
            elif imp["outcome"] in ("FAIL", "NO_VERDICT", "EXIT"):
                out.append(make_finding("AGENT_IMPORT_FAILS", name,
                    "%s cannot be imported on %s, so every consumer of it is running "
                    "without it: %s" % (stem, host, imp["detail"]),
                    [imp.get("traceback") or imp["detail"]], host=host))
            elif imp["outcome"] == "SANDBOX":
                out.append(make_finding("AGENT_WRITES_ON_IMPORT", name,
                    "Merely importing %s tries to change firm state (%s). Import must "
                    "be side-effect free." % (stem, imp["detail"]),
                    [imp["detail"]], host=host))

            if rep and rep["outcome"] in ("FAIL", "TIMEOUT", "NO_VERDICT", "EXIT"):
                out.append(make_finding("AGENT_REPORT_FAILS", name,
                    "`python3 %s.py --report` fails on %s (%s): %s"
                    % (stem, host, rep["outcome"], rep["detail"]),
                    [rep.get("traceback") or rep["detail"]], host=host))
            # rep SANDBOX == the agent ran far enough to write its report. Healthy.
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", metavar="MODULE", help=argparse.SUPPRESS)
    ap.add_argument("--mode", default="import", choices=("import", "report"))
    ap.add_argument("--syspath", help=argparse.SUPPRESS)
    ap.add_argument("--report", action="store_true", help="print the sweep summary")
    ap.add_argument("--static-only", action="store_true",
                    help="skip the subprocess probes (fast)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", nargs="*", help="restrict to these agent/module names")
    args = ap.parse_args(argv)

    if args.probe:
        print(json.dumps(_probe(args.probe, args.mode, args.syspath)))
        return 0

    res = collect(probe=not args.static_only, only=args.only)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0

    s = res["stats"]
    print("=" * 64)
    print("  APEX CAPITAL — AGENT SWEEP  (%s)" % res["host"])
    print("=" * 64)
    print("  agents on roster : %d" % s["agents_seen"])
    print("  agents probed    : %d" % s["probed"])
    print("  findings         : %d" % s["findings"])
    if not res["findings"]:
        print("\n  ✅ Nothing found.")
    for kind, n in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]):
        print("\n  [%s] × %d" % (kind, n))
        for f in [x for x in res["findings"] if x["kind"] == kind][:8]:
            print("     • %s — %s" % (f["subject"], f["detail"][:110]))
        if n > 8:
            print("     … and %d more" % (n - 8))
    return 0


if __name__ == "__main__":
    sys.exit(main())

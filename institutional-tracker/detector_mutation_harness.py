#!/usr/bin/env python3
"""Proves the firm's alarms can actually fire — by breaking things on purpose.

THE PROBLEM THIS EXISTS FOR
    Every guard in this codebase reports "OK" the vast majority of the time, and
    an OK is indistinguishable from a detector that has quietly stopped working.
    The firm has already shipped that failure repeatedly:

      * a coverage test that WROTE the registry it wished for, using a status
        spelling no live agent carries — it passed against a fiction;
      * an alarm that asked "does an explanation exist?" and stayed green forever
        on one stale record;
      * a VM freshness check documented in CLAUDE.md for a month that could never
        have run, erroring out silently every time.

    In every case the alarm was PRESENT. Presence is not currency.

WHAT A MUTATION CASE IS
    Two halves, and both are required:

      bad()   a state with a planted defect. The detector MUST flag it.
              If it does not: DETECTOR_BLIND (CRITICAL — a false all-clear).
      good()  a healthy control. The detector must NOT flag it.
              If it does: DETECTOR_FALSE_POSITIVE (an alarm that cries wolf
              gets muted, which disables it just as thoroughly as blindness).

    Omitting the control is the tempting shortcut and it is wrong: a detector
    hardcoded to `return True` passes every bad-input test ever written.

COVERAGE IS PART OF THE OUTPUT
    DETECTORS below lists the guards the firm actually relies on. Any name there
    without a case is reported as DETECTOR_UNTESTED rather than quietly omitted,
    so the harness cannot flatter itself by only testing what it already covers.

Run standalone for a report; `sweep_governance.py` runs it weekly and routes the
findings. This module mutates only in-memory objects and temp files — it never
touches live firm state.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# The guards the firm's assurance actually rests on. A name here with no case is
# a reported gap, not an omission. Keep this list honest — adding a detector to
# the firm without adding it here is how coverage silently rots.
DETECTORS = [
    "hr_agent.audit_retirement_coverage",
    "hr_retirement_audit.assess",
    "agent_roster.check",
    "problem_lifecycle_sentinel.classify",
    "chairman_queue.submit",
    "wiring_audit.check_orphan_queues",
    "agent_sweep.import_probe",
    "agent_sweep.isolation_graph",
    "sweep_taxonomy.make_finding",
    "hr_agent.review_sweep_queue",
    "sweep_governance.blind_closure_guard",
    "cit_director.sweep_deadman",
    "agent_roster.attendance_host_gate",
    "hr_agent.pip_evidence_guard",
]

_CASES = {}


def case(detector):
    def deco(fn):
        _CASES[detector] = fn
        return fn
    return deco


# ===========================================================================
# CASES. Each returns (fired_on_bad, fired_on_good, note).
# ===========================================================================

@case("hr_agent.audit_retirement_coverage")
def _c_retirement_coverage():
    """Plant a tombstoned agent with no monitoring lane; the audit must notice.

    The control is the VACATED shape — the one the seven reinstated sector agents
    actually carry — because a coverage audit that flags healthy reinstatements
    would have re-raised seven resolved Chairman rows every week.
    """
    import hr_agent
    # A retired agent whose (sector, etf) lane no live agent covers.
    tomb = [{"class_name": "GhostAgent", "status": "active", "sector": "Uranium",
             "etf": "URA", "retired_at": "2026-05-14",
             "retirement_reason": "consolidation"}]
    # Control: same retirement, but a live agent still holds the lane.
    live = tomb + [{"class_name": "SuccessorAgent", "status": "active",
                    "sector": "Uranium", "etf": "URA"}]
    bad = hr_agent.audit_retirement_coverage(registry=tomb)
    good = hr_agent.audit_retirement_coverage(registry=live)
    return (bool(bad.get("coverage_holes")),
            bool(good.get("coverage_holes")),
            "uncovered lane holes=%d covered lane holes=%d"
            % (len(bad.get("coverage_holes") or []), len(good.get("coverage_holes") or [])))


@case("hr_retirement_audit.assess")
def _c_assess_window():
    """A probation graded on an unmeasurable window must fail toward NOT_ASSESSABLE.

    The bad input is the trap that nearly convicted seven innocent agents: a blind
    window (no channel data) that an unscoped detector reads as 'measured, and the
    agent produced nothing' — i.e. as grounds to terminate.
    """
    import hr_retirement_audit as hra
    agent = {"class_name": "ProbationAgent", "status": "active", "permanent": False}
    obs = [{"agent": "ProbationAgent", "date": "2026-08-%02d" % d,
            "credited_picks_in_window": 0} for d in range(1, 13)]
    # BAD: the window cannot be measured at all. An unscoped detector reads this
    # as "measured, and the agent produced nothing" — grounds to terminate.
    blind = {"cycles_scanned": 0, "alive": False, "approved": 0, "attributed": 0,
             "credited": {}, "window_start": None}
    # CONTROL: a live window in which other agents WERE credited and this one
    # was not. That is a real, measurable case and must stay convictable, or the
    # fix would have made the detector useless in the other direction.
    alive = {"cycles_scanned": 30, "alive": True, "approved": 12, "attributed": 12,
             "credited": {"someone_else": 5}, "window_start": "2026-07-01"}
    v_blind = hra.assess("ProbationAgent", agent, obs, blind)
    v_alive = hra.assess("ProbationAgent", agent, obs, alive)
    return (str(v_blind.get("verdict")) == "NOT_ASSESSABLE",
            str(v_alive.get("verdict")) == "NOT_ASSESSABLE",
            "blind=%s measurable=%s" % (v_blind.get("verdict"), v_alive.get("verdict")))


@case("agent_roster.check")
def _c_roster_ratchet():
    """Shrink the roster below its manifest; the shrink-only ratchet must go red."""
    import agent_roster
    row = lambda n: {"name": n, "file": "%s.py" % n, "runs_via": ["cron"],
                     "employment": "employed"}
    manifest = {"agents": [{"name": n, "file": "%s.py" % n} for n in ("a", "b", "c")]}
    shrunk = {"agents": [row("a"), row("b")]}          # "c" vanished with no reason
    full = {"agents": [row(n) for n in ("a", "b", "c")]}
    bad = agent_roster.check(shrunk, manifest)
    good = agent_roster.check(full, manifest)
    fired = lambda r: r.get("status") == "RED" or bool(r.get("vanished"))
    return fired(bad), fired(good), "shrunk=%s full=%s" % (bad.get("status"), good.get("status"))


@case("problem_lifecycle_sentinel.classify")
def _c_lifecycle_stale():
    """An item open past the SLA must classify as STALE, a fresh one must not."""
    import problem_lifecycle_sentinel as pls
    now = datetime.datetime(2026, 8, 1)
    old = (now - datetime.timedelta(days=pls.STALE_SLA_DAYS + 15)).isoformat()
    fresh = (now - datetime.timedelta(days=1)).isoformat()
    mk = lambda seen: {"first_seen": seen, "last_seen": now.isoformat(),
                       "open": True, "resolved": False, "wont_fix": False,
                       "title": "planted", "observations": 9, "resolutions": 0,
                       "reopens": 0, "kinds": ["proposal"]}
    b = pls.classify(mk(old), now.isoformat())
    g = pls.classify(mk(fresh), now.isoformat())
    # OPEN is the healthy resting state, not an alarm. Only the escalation
    # classes count as "fired" — treating OPEN as a firing would have reported
    # this detector as a false positive on every healthy item in the queue.
    escalations = {"STALE", "SILENT_DROP", "LOOPING"}
    fired = lambda r: str(r).upper() in escalations
    return fired(b), fired(g), "old=%s fresh=%s" % (b, g)


@case("chairman_queue.submit")
def _c_chairman_eviction():
    """Flood the per-source cap and confirm the CRITICAL is not what gets evicted.

    This guard is a protection rather than an alarm, so "fired" reads as "the
    guard held". BAD = a hostile flood of 40 LOWs around one CRITICAL; the
    CRITICAL must survive. CONTROL = a small batch well under the cap; nothing
    may be evicted at all.

    Severity-blind eviction is the defect being probed: it would silently drop
    the most serious item the firm has precisely when a noisy producer is at its
    noisiest, and it would look identical to a quiet week.
    """
    import chairman_queue
    with tempfile.TemporaryDirectory() as td:
        flood = [{"id": "low-%02d" % i, "severity": "LOW", "check": "flood%02d" % i,
                  "description": "noise %d" % i} for i in range(40)]
        flood.append({"id": "the-critical", "severity": "CRITICAL", "check": "crit",
                      "description": "the one that matters"})
        p1 = os.path.join(td, "flood.json")
        chairman_queue.submit(flood, source="mutation-harness", path=p1)
        kept = {str(e.get("id")) for e in json.load(open(p1))}
        held = any("the-critical" in k for k in kept)

        p2 = os.path.join(td, "calm.json")
        small = [{"id": "calm-%d" % i, "severity": "LOW", "check": "calm%d" % i,
                  "description": "routine %d" % i} for i in range(3)]
        chairman_queue.submit(small, source="mutation-harness", path=p2)
        lost_calm = len(json.load(open(p2))) < 3

        return held, lost_calm, ("critical survived 40-LOW flood=%s (kept %d); "
                                 "calm batch intact=%s" % (held, len(kept), not lost_calm))


@case("wiring_audit.check_orphan_queues")
def _c_orphan_queue():
    """An empty queue file with exactly one referencing module must be reported.

    check_orphan_queues() appends to wiring_audit's module-level `problems` and
    `notes` rather than returning, so the case has to snapshot and restore those
    lists — reading its return value gives None and would score the detector
    BLIND on every run, which is how this case failed the first time it ran.
    """
    import wiring_audit
    qf = BASE / "data" / "mutation_orphan_queue.json"
    lonely = {"lonely_agent.py": 'Q = "data/mutation_orphan_queue.json"\n'
                                 'json.dump([], open(Q, "w"))\n'}
    wired = dict(lonely)
    wired["consumer_agent.py"] = 'x = json.load(open("data/mutation_orphan_queue.json"))\n'

    def run(sources):
        keep_p, keep_n = list(wiring_audit.problems), list(wiring_audit.notes)
        wiring_audit.problems.clear()
        wiring_audit.notes.clear()
        try:
            wiring_audit.check_orphan_queues(sources)
            out = list(wiring_audit.problems) + list(wiring_audit.notes)
        finally:
            wiring_audit.problems[:] = keep_p
            wiring_audit.notes[:] = keep_n
        return [x for x in out if "mutation_orphan_queue" in str(x)]

    made = False
    try:
        if not qf.exists():
            qf.write_text("[]")          # empty == the condition the check looks for
            made = True
        b, g = run(lonely), run(wired)
        return bool(b), bool(g), "one-referencer=%d two-referencers=%d" % (len(b), len(g))
    finally:
        if made and qf.exists():
            qf.unlink()


@case("agent_sweep.import_probe")
def _c_import_probe():
    """The headline check: plant a module that cannot import and confirm the probe
    says so. Without this case, "90 agents, zero import failures" proves nothing —
    it is equally consistent with a probe that never ran."""
    import agent_sweep
    with tempfile.TemporaryDirectory() as td:
        Path(td, "planted_broken.py").write_text("import a_module_that_does_not_exist\n")
        Path(td, "planted_fine.py").write_text("VALUE = 1\n")
        b = agent_sweep._run_probe("planted_broken", "import", syspath=td)
        g = agent_sweep._run_probe("planted_fine", "import", syspath=td)
        return (b["outcome"] in ("FAIL", "NO_VERDICT", "EXIT"),
                g["outcome"] not in ("OK", "SANDBOX"),
                "broken=%s fine=%s" % (b["outcome"], g["outcome"]))


@case("agent_sweep.isolation_graph")
def _c_isolation():
    """An agent talking to nobody must read as isolated; a wired pair must not.

    This is the Chairman's worked example — 'if a sweep finds that one agent
    doesn't speak to any other agents' — so it is the one case that must not be
    taken on trust.
    """
    import agent_sweep
    lonely = {"hermit_agent": 'P = "data/hermit_only.json"\njson.dump({}, open(P, "w"))\n'}
    pair = {"talker_agent": 'P = "data/shared_queue.json"\njson.dump({}, open(P, "w"))\n',
            "listener_agent": 'x = json.load(open("data/shared_queue.json"))\n'}
    gl, gp = agent_sweep._graph(lonely), agent_sweep._graph(pair)
    alone = not (gl["refs_of"].get("hermit_only.json", set()) - {"hermit_agent"})
    wired = bool(gp["refs_of"].get("shared_queue.json", set()) - {"talker_agent"})
    return alone, not wired, "hermit alone=%s pair wired=%s" % (alone, wired)


@case("sweep_taxonomy.make_finding")
def _c_taxonomy_closed():
    """The catch-all itself: an unrouted kind must RAISE, never return quietly.

    If this ever passes silently, the whole taxonomy stops being closed and the
    sweep becomes another producer of identifiers nobody consumes.
    """
    import sweep_taxonomy as tax
    try:
        tax.make_finding("A_KIND_NOBODY_DECLARED", "subj", "detail")
        raised = False
    except tax.UnroutedFinding:
        raised = True
    try:
        tax.make_finding("AGENT_ISOLATED", "subj", "detail")
        good_raised = False
    except tax.UnroutedFinding:
        good_raised = True
    return raised, good_raised, "unrouted raised=%s routed raised=%s" % (raised, good_raised)


@case("hr_agent.review_sweep_queue")
def _c_hr_consumes_its_queue():
    """HR must notice a finding kind its policy table has never heard of.

    BAD = an open item whose kind HR has no disposition for. That is producer/
    consumer drift: the taxonomy grew and HR was not told, so the item would sit
    in HR's inbox with nobody knowing what to do with it. HR must report
    TAXONOMY_DRIFT rather than shrugging via a `.get()` default.
    CONTROL = the same queue with a kind HR does handle; status must be OK.
    """
    import hr_agent
    row = lambda kind: [{"key": "%s::x" % kind, "kind": kind, "subject": "x",
                         "owner": "HR", "severity": "HIGH", "detail": "d",
                         "remedy": "r", "why": "w", "sla_days": 20,
                         "status": "open", "first_seen": "2026-08-01T00:00:00",
                         "last_seen": "2026-08-01T00:00:00"}]
    with tempfile.TemporaryDirectory() as td:
        pb = os.path.join(td, "bad.json")
        json.dump(row("A_KIND_HR_NEVER_HEARD_OF"), open(pb, "w"))
        b = hr_agent.review_sweep_queue(path=pb, now="2026-08-02T00:00:00")

        pg = os.path.join(td, "good.json")
        json.dump(row("AGENT_ISOLATED"), open(pg, "w"))
        g = hr_agent.review_sweep_queue(path=pg, now="2026-08-02T00:00:00")
    return (b["status"] == "TAXONOMY_DRIFT" and bool(b["unmapped_kinds"]),
            g["status"] != "OK",
            "unmapped=%s mapped=%s" % (b["status"], g["status"]))


@case("sweep_governance.blind_closure_guard")
def _c_no_closure_while_blind():
    """Silence from a collector that never ran must not read as "fixed".

    BAD = agent_sweep crashed and reports nothing. The guard must FIRE, meaning
    it refuses to treat that silence as evidence and holds the items open. If it
    does not, every open finding is marked resolved at the exact moment the firm
    went blind to it — a false all-clear generated by the closure logic itself.
    CONTROL = a healthy collector, where absence really is evidence of repair
    and closing is the correct behaviour.
    """
    import sweep_governance as sg
    import sweep_taxonomy as tax

    f = tax.make_finding("AGENT_ISOLATED", "lonely", "talks to nobody")
    with tempfile.TemporaryDirectory() as td:
        original = sg.HR_QUEUE_PATH
        sg.HR_QUEUE_PATH = Path(td) / "q.json"
        try:
            sg._reconcile([f], "2026-08-01T00:00:00", {"AGENT_ISOLATED"})
            blind = sg._reconcile([], "2026-08-08T00:00:00",
                                  sg._closable_kinds({"agent_sweep": {"error": "boom"}}, True))
            held = blind["open"] == 1 and bool(blind["held_open_unverifiable"])

            sg.HR_QUEUE_PATH = Path(td) / "q2.json"
            sg._reconcile([f], "2026-08-01T00:00:00", {"AGENT_ISOLATED"})
            seeing = sg._reconcile([], "2026-08-08T00:00:00",
                                   sg._closable_kinds({"agent_sweep": {}}, True))
            wrongly_held = seeing["open"] != 0
        finally:
            sg.HR_QUEUE_PATH = original
    return held, wrongly_held, "blind_held=%s healthy_closed=%s" % (held, not wrongly_held)


@case("cit_director.sweep_deadman")
def _c_sweep_deadman():
    """The weekly sweep going silent must itself raise an alarm.

    BAD = the sweep's report is months old, i.e. the sweep stopped running and
    every week since has looked exactly like a clean one. That is the shape of
    the Oracle host running unwatched for 69 days.
    CONTROL = a report written today; no finding.

    Note this exercises the REAL CITPipelineMonitor against a fake BASE_DIR, not
    a reimplementation of its arithmetic — a test of my own copy of the threshold
    logic would pass even if the monitor's own copy were deleted.
    """
    import cit_director

    def run_with(age_days):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data"
            d.mkdir(parents=True)
            rp = d / "agent_sweep_report.json"
            rp.write_text("{}")
            ts = datetime.datetime.now().timestamp() - age_days * 86400
            os.utime(rp, (ts, ts))
            orig = cit_director.BASE_DIR
            cit_director.BASE_DIR = Path(td)
            try:
                found = cit_director.CITPipelineMonitor().analyze()
            finally:
                cit_director.BASE_DIR = orig
        return any("agent_sweep_report.json" in str(f.get("title", "")) and "stale" in
                   str(f.get("title", "")).lower() for f in found)

    return run_with(60), run_with(1), "stale60=%s fresh1=%s" % (run_with(60), run_with(1))


@case("agent_roster.attendance_host_gate")
def _c_attendance_host_gate():
    """Off the host that runs an agent, attendance must judge NOTHING.

    The mutation here is the host, not the code. Output freshness is the one
    check in the sweep whose answer depends on which box you ask from: the Mac
    runs no agents, so every VM-scheduled agent's files sit untouched there
    forever. Measured from the wrong machine, six healthy agents came back as
    PIP candidates for having "stopped producing" — a firing offence
    manufactured entirely by reading the wrong disk.

    Both halves matter. Suppressing off-host is worthless if the gate also
    suppresses on-host, which would silently retire the staleness check
    everywhere and look exactly like a clean firm.
    """
    import agent_roster
    agents = [
        {"file": "vm_only_agent.py",  "runs_via": ["cron"]},
        {"file": "child_agent.py",    "runs_via": ["called_by:vm_only_agent"]},
        {"file": "main_child.py",     "runs_via": ["main"]},
    ]
    off_host = agent_roster.runs_here(agents, crontab_text="# no jobs here\n")
    on_host = agent_roster.runs_here(
        agents, crontab_text="0 5 * * * python3 vm_only_agent.py\n"
                             "0 6 * * * python3 main.py\n")
    # The gate "fires" by withholding judgement. Off-host it must withhold on
    # everything; on-host it must withhold on nothing, including the agents that
    # inherit their host from main.py and from a called_by: parent.
    suppressed_off_host = not off_host
    suppressed_on_host = not ({"vm_only_agent", "child_agent", "main_child"} <= on_host)
    return (suppressed_off_host, suppressed_on_host,
            "off_host=%d on_host=%d" % (len(off_host), len(on_host)))


@case("hr_agent.pip_evidence_guard")
def _c_pip_needs_verified_evidence():
    """A PIP must rest on an observation somebody actually made.

    sweep_governance holds an item open and stamps it last_unverifiable when the
    run could not observe it. That is the honest state, but in the queue it then
    looks exactly like a confirmed, still-failing agent — and the stamp is worth
    nothing if the consumer never reads it. This is the presence-is-not-currency
    trap in its most expensive form: the output is a disciplinary action.

    BAD = an open PIP-disposition item whose last observation was unverifiable;
    HR must withhold it. CONTROL = the same item genuinely observed; HR must
    still nominate it, or the guard has quietly disabled attendance discipline
    altogether.
    """
    import hr_agent
    def row(**extra):
        e = {"key": "AGENT_OUTPUT_STALE::a", "kind": "AGENT_OUTPUT_STALE",
             "subject": "a", "owner": "HR", "severity": "HIGH", "detail": "d",
             "remedy": "r", "why": "w", "sla_days": 10, "status": "open",
             "first_seen": "2026-07-01T00:00:00", "last_seen": "2026-08-01T00:00:00"}
        e.update(extra)
        return [e]
    with tempfile.TemporaryDirectory() as td:
        pb = os.path.join(td, "blind.json")
        json.dump(row(last_unverifiable="2026-08-01T00:00:00"), open(pb, "w"))
        b = hr_agent.review_sweep_queue(path=pb, now="2026-08-02T00:00:00")

        pg = os.path.join(td, "seen.json")
        json.dump(row(), open(pg, "w"))
        g = hr_agent.review_sweep_queue(path=pg, now="2026-08-02T00:00:00")
    return (b["pip_candidates"] == [] and b["pip_withheld_unverifiable"] == ["a"],
            g["pip_candidates"] != ["a"],
            "unverified_pips=%s verified_pips=%s"
            % (b["pip_candidates"], g["pip_candidates"]))


# ===========================================================================
# RUNNER
# ===========================================================================
def collect(host: str = None) -> dict:
    from sweep_taxonomy import make_finding

    host = host or os.uname().nodename
    findings, results = [], []

    for name in DETECTORS:
        fn = _CASES.get(name)
        if fn is None:
            findings.append(make_finding("DETECTOR_UNTESTED", name,
                "%s is listed as a detector the firm relies on, but no mutation "
                "case exists for it — nothing proves it can fire." % name,
                host=host))
            results.append({"detector": name, "result": "UNTESTED"})
            continue
        try:
            fired_bad, fired_good, note = fn()
        except Exception as e:
            findings.append(make_finding("DETECTOR_ERRORS", name,
                "The mutation case for %s crashed: %s: %s. A detector that raises "
                "is not reporting — callers swallow the exception and read it as "
                "nothing to report." % (name, type(e).__name__, e),
                [traceback.format_exc()[-1200:]], host=host))
            results.append({"detector": name, "result": "ERROR", "note": str(e)[:200]})
            continue

        if not fired_bad:
            findings.append(make_finding("DETECTOR_BLIND", name,
                "%s was fed a state with a planted defect and did not flag it. "
                "Every clean report this detector has produced is unverified. (%s)"
                % (name, note), [note], host=host))
            results.append({"detector": name, "result": "BLIND", "note": note})
        elif fired_good:
            findings.append(make_finding("DETECTOR_FALSE_POSITIVE", name,
                "%s flagged a healthy control state. An alarm that fires on good "
                "input gets muted by its readers. (%s)" % (name, note),
                [note], host=host))
            results.append({"detector": name, "result": "FALSE_POSITIVE", "note": note})
        else:
            results.append({"detector": name, "result": "PASS", "note": note})

    return {"generated_at": datetime.datetime.utcnow().isoformat(), "host": host,
            "detectors": len(DETECTORS), "cases": len(_CASES),
            "results": results, "findings": findings,
            "passed": sum(1 for r in results if r["result"] == "PASS")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    res = collect()
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print("=" * 64)
    print("  DETECTOR MUTATION HARNESS — can the alarms actually fire?")
    print("=" * 64)
    for r in res["results"]:
        mark = {"PASS": "✅", "BLIND": "🚨", "FALSE_POSITIVE": "⚠️ ",
                "ERROR": "💥", "UNTESTED": "❓"}.get(r["result"], "?")
        print("  %s %-42s %s" % (mark, r["detector"], r["result"]))
        if r.get("note"):
            print("       %s" % r["note"][:100])
    print("\n  %d/%d detectors proven able to fire." % (res["passed"], res["detectors"]))
    return 0 if res["passed"] == res["detectors"] else 1


if __name__ == "__main__":
    sys.exit(main())

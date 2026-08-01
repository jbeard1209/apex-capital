#!/usr/bin/env python3
"""The Quality Assurance Office — turns the weekly sweep into decisions.

A sweep that only sweeps is theatre. This module is the part that makes it
governance: it collects what the sweep and the mutation harness found, batches
it into proposals the deputy board actually votes on, hands each owner its work,
and — the part that usually gets skipped — closes items when the underlying
condition genuinely clears.

    agent_sweep ─┐
                 ├─► taxonomy ─► batch ─► cit_pending_proposals ─► deputy board VOTES
    mutation ────┘      │                                              │
                        ├─► hr_action_queue ──────► hr_agent           ├─► approved: CIT implements
                        └─► chairman_action_required (4 kinds only)    └─► problem_lifecycle_sentinel
                                                                            tracks to closure

WHY EACH JOINT IS AN EXISTING ONE
    Nothing here is a parallel governance system. The board queue, the Chairman
    queue, the lifecycle sentinel and HR all already exist and already run on
    cron; the sweep files into them. A second, private queue would be a second
    place for findings to rot, which is the failure this whole build exists to
    prevent.

CLOSURE IS BY RE-OBSERVATION, NEVER BY DECLARATION
    An item is resolved when the next sweep no longer finds the condition. Not
    when someone marks it done. That is the only closure rule that cannot be
    gamed, and it is why `_reconcile` runs before anything is filed.

WHAT HAPPENS WHEN SOMETHING GOES WRONG
    Every outcome has a declared handler, including the handlers' own failures:

      a finding kind with no route  -> UnroutedFinding -> UNROUTED_FINDING, board + Chairman
      a collector raises            -> SWEEP_CRASHED, board + Chairman
      the whole run raises          -> caught in main(), SWEEP_CRASHED filed anyway
      the sweep stops running       -> report goes stale -> the freshness sentinel
                                       raises SWEEP_DID_NOT_RUN (the deadman)
      a filing target is missing    -> logged and counted in `undelivered`, and the
                                       run still writes its report rather than dying
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
sys.path.insert(0, str(BASE))

REPORT_PATH = DATA / "agent_sweep_report.json"
PACKET_PATH = DATA / "sweep_board_packet.json"
HR_QUEUE_PATH = DATA / "hr_action_queue.json"

# Findings per batch listed by name in the proposal body. Beyond this the board
# gets a count and a pointer to the report — a 45-item wall of text in a
# proposal description does not get read, and an unread proposal is an unmade
# decision.
MAX_NAMED = 12


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _save(path, obj):
    """Atomic: a half-written governance queue is worse than a stale one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, p)


# ===========================================================================
# COLLECT — with the collectors' own failures treated as findings
# ===========================================================================
def _collect_all(host: str, probe: bool = True) -> tuple:
    """Run both collectors. A collector that dies becomes a finding, not silence."""
    import sweep_taxonomy as tax

    findings, meta = [], {}

    def guarded(label, fn):
        try:
            res = fn()
            meta[label] = {k: v for k, v in res.items() if k != "findings"}
            return res.get("findings", [])
        except tax.UnroutedFinding as e:
            meta[label] = {"error": "unrouted"}
            return [tax.make_finding("UNROUTED_FINDING", label,
                "%s tried to emit a finding kind that has no owner: %s. Until a "
                "route exists, that finding reaches nobody." % (label, e),
                [traceback.format_exc()[-1200:]], host=host)]
        except Exception as e:
            meta[label] = {"error": str(e)[:200]}
            return [tax.make_finding("SWEEP_CRASHED", label,
                "%s crashed during the weekly sweep (%s: %s), so the firm has no "
                "assurance from it this week." % (label, type(e).__name__, e),
                [traceback.format_exc()[-1500:]], host=host)]

    import agent_sweep
    import detector_mutation_harness
    findings += guarded("agent_sweep", lambda: agent_sweep.collect(probe=probe, host=host))
    findings += guarded("detector_mutation_harness",
                        lambda: detector_mutation_harness.collect(host=host))
    return findings, meta


# ===========================================================================
# RECONCILE — close what genuinely cleared, keep what did not
# ===========================================================================
def _key(f) -> str:
    return "%s::%s" % (f["kind"], f["subject"])


def _closable_kinds(meta: dict, probe: bool) -> set:
    """Which kinds this run was actually in a position to DISPROVE.

    Absence of a finding only means "resolved" if the check that would have
    produced it really ran. A crashed collector reports nothing, and closing on
    that silence would mark every one of its open items fixed at the exact
    moment the firm went blind to them — the failure mode this whole build
    exists to prevent, reproduced by its own closure logic.
    """
    import sweep_taxonomy as tax
    probe_kinds = {"AGENT_IMPORT_FAILS", "AGENT_IMPORT_TIMEOUT",
                   "AGENT_WRITES_ON_IMPORT", "AGENT_REPORT_FAILS"}
    owners = {"agent_sweep": {k for k in tax.KINDS if k.startswith("AGENT_")},
              "detector_mutation_harness": {k for k in tax.KINDS if k.startswith("DETECTOR_")}}
    closable = set()
    for collector, kinds in owners.items():
        if not meta.get(collector, {}).get("error"):
            closable |= kinds
    if not probe:
        closable -= probe_kinds
    # Same rule one level down. Attendance is only assessable on the host that
    # runs the agent, so a sweep from the wrong box looked at nothing and must
    # not conclude anything — least of all that a silent agent recovered.
    if not (meta.get("agent_sweep", {}).get("stats") or {}).get("attendance_assessable"):
        closable -= {"AGENT_OUTPUT_STALE", "AGENT_OUTPUT_NEVER_PRODUCED"}
    return closable


def _reconcile(findings: list, now: str, closable: set, dry_run: bool = False) -> dict:
    """Merge this run into the HR queue, closing conditions that no longer hold.

    Idempotent per run: re-running the sweep on the same day neither duplicates an
    open item nor re-opens a closed one. (The VM runs the pipeline six times a
    day; anything that appends per-run reports one afternoon as a hundred
    observations of a month.)
    """
    live = {_key(f) for f in findings if f["to_hr"]}
    queue = _load(HR_QUEUE_PATH, [])
    if not isinstance(queue, list):
        queue = []
    by_key = {e.get("key"): e for e in queue if isinstance(e, dict)}

    closed, opened, unverifiable = [], [], []
    for k, e in by_key.items():
        if e.get("status") == "open" and e.get("kind") not in closable:
            e["last_unverifiable"] = now
            unverifiable.append(k)
            continue
        if e.get("status") == "open" and k not in live:
            e["status"] = "resolved"
            e["resolved_at"] = now
            e["resolved_by"] = ("condition no longer observed by the sweep of %s "
                                "— closed by re-observation, not by declaration" % now[:10])
            closed.append(k)

    for f in findings:
        if not f["to_hr"]:
            continue
        k = _key(f)
        e = by_key.get(k)
        if e and e.get("status") == "open":
            # Read the previous last_seen BEFORE overwriting it, or the same-day
            # test compares now against now and the counter never moves.
            prev = str(e.get("last_seen") or "")[:10]
            if prev != now[:10]:
                e["times_seen"] = int(e.get("times_seen") or 1) + 1
            e["last_seen"] = now
            e["detail"] = f["detail"]
            continue
        if e and e.get("status") == "resolved":
            e["status"] = "open"
            e["reopened_at"] = now
            e["reopen_count"] = int(e.get("reopen_count") or 0) + 1
            e["last_seen"] = now
            opened.append(k)
            continue
        by_key[k] = {
            "key": k, "kind": f["kind"], "subject": f["subject"],
            "owner": f["owner"], "severity": f["severity"], "detail": f["detail"],
            "remedy": f["remedy"], "why": f["why"], "sla_days": f["sla_days"],
            "status": "open", "first_seen": now, "last_seen": now,
            "times_seen": 1, "host": f.get("host"),
        }
        opened.append(k)

    out = sorted(by_key.values(), key=lambda e: (e.get("status") != "open", e.get("key") or ""))
    if not dry_run:
        _save(HR_QUEUE_PATH, out)
    return {"open": sum(1 for e in out if e.get("status") == "open"),
            "closed_this_run": closed, "opened_this_run": opened,
            "held_open_unverifiable": unverifiable}


# ===========================================================================
# BATCH — one decision per kind, not one per affected agent
# ===========================================================================
def _batch(findings: list) -> list:
    """Collapse findings into board proposals, one per kind.

    45 unscheduled agents are ONE decision ("what is our policy on agents that
    run nowhere"), not 45. Filing them individually would bury the board and
    guarantee none of them got read — the same reasoning that collapses the
    Chairman's reinstatement rows onto a single batch key.
    """
    import sweep_taxonomy as tax

    groups = {}
    for f in findings:
        if f["to_board"]:
            groups.setdefault(f["kind"], []).append(f)

    proposals = []
    for kind, fs in sorted(groups.items()):
        route = tax.route_for(kind)
        n = len(fs)
        worst = max(fs, key=lambda x: tax.SEVERITIES.index(x["severity"]))
        names = [f["subject"] for f in fs[:MAX_NAMED]]
        more = ("\n  … and %d more — full list in data/agent_sweep_report.json"
                % (n - MAX_NAMED)) if n > MAX_NAMED else ""

        # Title must normalize to a STABLE dedup key. cit_director dedups by
        # title with every digit collapsed to "#", so the count is free — but
        # anything that varies with the count in a non-digit way (a plural "s",
        # say) silently creates a second proposal the week a batch drops to one
        # item. The headline is therefore fixed per kind.
        title = "Sweep: %d finding(s) — %s" % (n, _headline(kind))
        desc = (
            "The weekly firm sweep found %d instance(s) of %s.\n\n"
            "WHY THIS MATTERS\n%s\n\n"
            "AFFECTED\n  %s%s\n\n"
            "WHAT THE BOARD IS DECIDING\n%s\n\n"
            "OWNER: %s. SLA: %d days — after that problem_lifecycle_sentinel "
            "escalates it to the Chairman as STALE."
            % (n, kind, route["why"], "\n  ".join(names), more,
               route["remedy"], route["owner"], route["sla_days"])
        )
        proposals.append({
            "agent": "QualityAssuranceOffice",
            # Explicit, because route_proposal_to_pod() routes on keywords and a
            # sweep proposal naming `ml_longterm_drift_monitor` would land in the
            # ML pod's queue. These are firm-level employment and assurance
            # decisions; they belong to the board that can actually make them.
            "pod": "main",
            "category": route["category"],
            "severity": worst["severity"],
            "confidence": 90,
            "title": title,
            "description": desc,
            # Stable across weeks on purpose: the deputy board tracks
            # clarification rounds by proposal_id, and a fresh id each week would
            # reset the counter forever, so a finding it could never resolve
            # would loop instead of escalating to the full board on round 3.
            "id": "SWEEP-%s" % kind,
            "evidence": "%d finding(s) of kind %s in data/agent_sweep_report.json "
                        "(sweep of %s, host %s). Reproduce with: python3 "
                        "agent_sweep.py --report. Affected modules: %s."
                        % (n, kind, datetime.date.today().isoformat(),
                           fs[0].get("host") or "?", ", ".join(names[:6])),
            # Name the actual files. The deputy board rejects a fix it cannot act
            # on, and it is right to — "review these agents" is a sentiment, not
            # an instruction. Naming the modules turns the vote into a decision
            # someone can carry out the same afternoon.
            "proposed_fix": "In %s: %s (full list: data/agent_sweep_report.json)"
                            % (_fix_targets(fs), route["remedy"]),
            # Nothing from the sweep auto-implements. Every one of these is a
            # judgement about an agent's employment or a detector's correctness,
            # and the Chairman asked for a vote, not for the machine to act on
            # its own reading of its own output.
            "auto_implementable": False,
            "source": "sweep_governance",
            "finding_kind": kind,
            "dedup_key": "sweep-%s" % kind.lower(),
        })
    return proposals


def _fix_targets(fs: list, limit: int = 6) -> str:
    """Turn finding subjects into real file paths the implementer can open.

    Subjects are not uniformly module names — a wiring finding reads
    `cit_director <- etf_pod_pending.json`, and blindly appending ".py" produced
    `cit_director <- etf_pod_pending.json.py`, a path that exists nowhere. A fix
    that names a file which does not exist is worse than a vague one: it looks
    actionable and wastes the implementer's first ten minutes.
    """
    import re
    out = []
    for f in fs[:limit]:
        subj = str(f.get("subject") or "")
        m = re.match(r"[A-Za-z_][\w]*", subj)
        stem = m.group(0) if m else ""
        if stem and (BASE / ("%s.py" % stem)).exists():
            target = "%s.py" % stem
            rest = subj[len(stem):].strip()
            out.append("%s (%s)" % (target, rest) if rest else target)
        else:
            out.append(subj)
    return ", ".join(sorted(set(out))) or "(see report)"


def _headline(kind: str) -> str:
    return {
        "AGENT_IMPORT_FAILS": "agents that cannot be imported",
        "AGENT_IMPORT_TIMEOUT": "agents that hang on import",
        "AGENT_WRITES_ON_IMPORT": "agents that mutate firm state at import time",
        "AGENT_REPORT_FAILS": "agents that crash on --report",
        "AGENT_ISOLATED": "agents that speak to no other agent",
        "AGENT_OUTPUT_UNCONSUMED": "agent outputs that nobody reads",
        "AGENT_INPUT_UNPRODUCED": "agent inputs that nobody produces",
        "AGENT_UNSCHEDULED": "employed agents that run nowhere",
        "AGENT_OUTPUT_STALE": "scheduled agents that have stopped producing",
        "AGENT_OUTPUT_NEVER_PRODUCED": "agents that have never produced output",
        "AGENT_FILE_MISSING": "roster entries naming a file that is gone",
        "AGENT_ZOMBIE": "agents retired on paper and still running",
        "AGENT_UNREVIEWED": "agents never reviewed by HR",
        "DETECTOR_BLIND": "detectors that stayed green on a planted defect",
        "DETECTOR_FALSE_POSITIVE": "detectors that fire on healthy input",
        "DETECTOR_ERRORS": "detectors that crash when exercised",
        "DETECTOR_UNTESTED": "detectors with no mutation case",
        "HOST_DIVERGENCE": "divergences between the two hosts",
        "SWEEP_CRASHED": "sweep components that crashed",
        "SWEEP_DID_NOT_RUN": "missed weekly sweeps",
        "UNROUTED_FINDING": "finding kinds with no owner",
    }[kind]          # KeyError here is a bug, and a loud one — never .get()


# ===========================================================================
# FILE — into the queues that already exist
# ===========================================================================
def _norm_title(t: str) -> str:
    import re
    return re.sub(r"\d+", "#", t or "")


CLOSED_STATUSES = ("resolved", "closed", "done", "implemented")


def _already_decided(action_queue: list) -> set:
    """Sweep dedup_keys the board has decided and whose decision is still open.

    A decision that has been EXECUTED (resolved/closed) is not a reason to stay
    quiet — if the condition is still there next week the board should hear
    about it again. Only an outstanding decision suppresses a re-file.
    """
    out = set()
    for row in action_queue or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("dedup_key") or "")
        if key.startswith("sweep-") and str(row.get("status", "")).lower() not in CLOSED_STATUSES:
            out.add(key)
    return out


def _file_board(proposals: list, dry_run: bool) -> dict:
    """File to the board queue, then VERIFY by reading the queue back.

    `submit_to_pod_queue` catches its own exceptions and returns the pod name
    regardless, so its return value is not evidence that anything was written —
    counting calls would report a silently-swallowed write as a delivery. The
    only honest check is whether the proposal is in the file afterwards.
    Already-present (the weekly re-file of an unresolved finding) counts as
    delivered; absent does not.
    """
    if dry_run:
        return {"filed": 0, "dry_run": True, "would_file": len(proposals)}
    try:
        import cit_director
    except Exception as e:
        return {"filed": 0, "undelivered": len(proposals), "error": str(e)[:200]}

    # Do not re-litigate a decision the board has already made. Once a kind is
    # approved it moves out of pending into chairman_action_required, so a naive
    # weekly re-file would put it back on the agenda every Monday until someone
    # executed it — 8 fresh votes a week on questions already answered, and 8
    # more rows in cos_meeting_requests.json (already 9,994 entries deep).
    # Filed once, tracked by problem_lifecycle_sentinel, re-asked only if the
    # decision goes stale or the condition returns.
    decided = _already_decided(_load(DATA / "chairman_action_required.json", []))
    pending_now = [p for p in proposals if p["dedup_key"] not in decided]
    deferred = [p["title"] for p in proposals if p["dedup_key"] in decided]

    before = {_norm_title(p.get("title")) for p in _load(cit_director.CIT_PENDING, [])}
    errors = []
    for p in pending_now:
        try:
            cit_director.submit_to_pod_queue(p)
        except Exception as e:
            errors.append({"title": p["title"], "error": str(e)[:200]})

    after = {_norm_title(p.get("title")) for p in _load(cit_director.CIT_PENDING, [])}
    delivered = [p for p in pending_now if _norm_title(p["title"]) in after]
    missing = [p["title"] for p in pending_now if _norm_title(p["title"]) not in after]
    return {
        "filed": len(delivered),
        "newly_added": len(after - before),
        "already_open": len(delivered) - len(after - before),
        "already_decided_not_refiled": len(deferred),
        "already_decided_titles": deferred,
        "undelivered": len(missing),
        "undelivered_titles": missing,
        "errors": errors,
    }


def _file_chairman(findings: list, dry_run: bool) -> dict:
    """Only the four kinds that mean the assurance system itself is broken."""
    rows = [f for f in findings if f["to_chairman"]]
    if not rows:
        return {"filed": 0}
    if dry_run:
        return {"filed": 0, "dry_run": True, "would_file": len(rows)}
    try:
        import chairman_queue
    except Exception as e:
        return {"filed": 0, "undelivered": len(rows), "error": str(e)[:200]}
    entries = [{
        "id": "sweep-%s-%s" % (f["kind"].lower(), (f["subject"] or "")[:40]),
        "severity": f["severity"],
        "check": "sweep_%s" % f["kind"].lower(),
        "dedup_key": "sweep-%s-%s" % (f["kind"].lower(), f["subject"]),
        "description": "%s\n\nWHY: %s\n\nWHAT HAPPENS NEXT: %s"
                       % (f["detail"], f["why"], f["remedy"]),
    } for f in rows]
    try:
        # submit() returns what was WRITTEN after its per-source cap, not what
        # was offered — report that, so an evicted escalation is never logged
        # as having reached him.
        added, suppressed = chairman_queue.submit(entries, source="sweep_governance")
        return {"filed": added, "suppressed_as_open": suppressed,
                "undelivered": len(entries) - added - suppressed}
    except Exception as e:
        return {"filed": 0, "undelivered": len(entries), "error": str(e)[:200]}


# ===========================================================================
# RUN
# ===========================================================================
def run(dry_run: bool = False, probe: bool = True, host: str = None) -> dict:
    import sweep_taxonomy as tax

    host = host or os.uname().nodename
    now = datetime.datetime.utcnow().isoformat()

    # Did the firm go blind since the last sweep? CIT's freshness monitor catches
    # a sweep that has stopped; this catches the other half — a sweep that
    # stopped and then RESUMED, which otherwise looks identical to a clean week
    # and quietly erases the gap it should be reporting.
    gap = None
    prev = _load(REPORT_PATH, {})
    if isinstance(prev, dict) and prev.get("generated_at"):
        try:
            since = (datetime.datetime.fromisoformat(now)
                     - datetime.datetime.fromisoformat(prev["generated_at"])).days
            if since >= 11:      # weekly cadence + the same grace CIT allows
                gap = since
        except Exception:
            pass

    findings, meta = _collect_all(host, probe=probe)

    if gap is not None:
        import sweep_taxonomy as _tax
        findings.append(_tax.make_finding(
            "SWEEP_DID_NOT_RUN", "sweep_governance",
            "The weekly firm sweep last ran %d days ago (expected every 7). For "
            "those %d days no agent was checked for import failure, isolation, "
            "silence or unreviewed status, and nothing distinguished that blind "
            "window from a clean one." % (gap, gap),
            ["previous report generated_at=%s" % prev.get("generated_at"),
             "this run=%s" % now], host=host))

    # Defence in depth: a finding that reached here without a route would mean
    # make_finding was bypassed. Convert it rather than letting it through.
    checked = []
    for f in findings:
        try:
            tax.route_for(f.get("kind"))
            checked.append(f)
        except tax.UnroutedFinding as e:
            checked.append(tax.make_finding("UNROUTED_FINDING", str(f.get("kind")),
                "A finding reached the router with no declared owner: %s" % e,
                [json.dumps(f, default=str)[:600]], host=host))
    findings = checked

    hr = _reconcile(findings, now, _closable_kinds(meta, probe), dry_run=dry_run)
    proposals = _batch(findings)
    board = _file_board(proposals, dry_run)
    chair = _file_chairman(findings, dry_run)

    by_kind = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1

    report = {
        "generated_at": now, "host": host, "dry_run": dry_run,
        "findings": len(findings), "by_kind": by_kind,
        "collectors": meta, "hr_queue": hr,
        "board": board, "chairman": chair,
        "proposals": [{"title": p["title"], "category": p["category"],
                       "severity": p["severity"], "kind": p["finding_kind"]}
                      for p in proposals],
        "detail": findings,
    }
    packet = {
        "generated_at": now, "host": host,
        "headline": "%d finding(s) across %d kind(s); %d proposal(s) to the board; "
                    "%d open HR item(s)." % (len(findings), len(by_kind),
                                             len(proposals), hr["open"]),
        "proposals": report["proposals"],
        "closed_since_last_sweep": hr["closed_this_run"],
        "newly_opened": hr["opened_this_run"],
    }
    if not dry_run:
        _save(REPORT_PATH, report)
        _save(PACKET_PATH, packet)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="print without filing")
    ap.add_argument("--dry-run", action="store_true", help="file nothing")
    ap.add_argument("--static-only", action="store_true", help="skip subprocess probes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    dry = args.dry_run or args.report

    try:
        rep = run(dry_run=dry, probe=not args.static_only)
    except Exception as e:
        # The runner itself died. File it rather than exiting quietly — a
        # monitoring system that dies takes its own alarm with it.
        tb = traceback.format_exc()
        try:
            import chairman_queue
            import sweep_taxonomy as tax
            f = tax.make_finding("SWEEP_CRASHED", "sweep_governance.run",
                                 "The weekly sweep runner itself crashed: %s: %s"
                                 % (type(e).__name__, e), [tb[-1500:]])
            chairman_queue.submit([{
                "id": "sweep-runner-crash", "severity": "CRITICAL",
                "check": "sweep_crashed", "dedup_key": "sweep-runner-crash",
                "description": f["detail"] + "\n\n" + f["remedy"]}],
                source="sweep_governance")
        except Exception:
            pass
        print("SWEEP CRASHED: %s\n%s" % (e, tb), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0

    print("=" * 68)
    print("  QUALITY ASSURANCE OFFICE — weekly sweep  (%s)" % rep["host"])
    print("=" * 68)
    print("  findings          : %d across %d kind(s)" % (rep["findings"], len(rep["by_kind"])))
    print("  proposals to board: %d %s" % (len(rep["proposals"]),
                                           "(dry run — nothing filed)" if dry else
                                           "(%d filed)" % rep["board"].get("filed", 0)))
    if rep["board"].get("already_decided_not_refiled"):
        print("  not re-litigated   : %d — the board already decided these and the "
              "decision is still open" % rep["board"]["already_decided_not_refiled"])
    print("  HR queue open     : %d  (+%d new, -%d closed this run)"
          % (rep["hr_queue"]["open"], len(rep["hr_queue"]["opened_this_run"]),
             len(rep["hr_queue"]["closed_this_run"])))
    if rep["hr_queue"]["held_open_unverifiable"]:
        print("  held open (blind) : %d — this run could not observe them (a collector"
              " failed, or\n                      they are only assessable on the host"
              " that runs the agent), so\n                      their absence from the"
              " findings proves nothing"
              % len(rep["hr_queue"]["held_open_unverifiable"]))
    print("  to the Chairman   : %d" % (rep["chairman"].get("filed", 0)
                                        or rep["chairman"].get("would_file", 0)))
    und = (rep["board"].get("undelivered", 0) or 0) + (rep["chairman"].get("undelivered", 0) or 0)
    if und:
        print("  ⚠️  UNDELIVERED   : %d — findings that reached no queue" % und)
    print("\n  Board packet:")
    for p in rep["proposals"]:
        print("    [%-8s %-12s] %s" % (p["severity"], p["category"], p["title"]))
    if rep["hr_queue"]["closed_this_run"]:
        print("\n  Closed by re-observation (condition no longer present):")
        for k in rep["hr_queue"]["closed_this_run"][:10]:
            print("    ✔ %s" % k)
    return 0


if __name__ == "__main__":
    sys.exit(main())

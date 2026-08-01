"""hr_retirement_audit.py — did the firm fire anyone it shouldn't have?

Retiring an agent is the only irreversible thing HR does, and it was running
without a check. Two agents were retired on 2026-05-14 for producing zero
APPROVED picks across 14 cycles. They had submitted work the whole time; the
firm-wide buy freeze (CRO risk-calc bug, 272 days and counting) meant nothing
could be approved, by anyone. The CRO exoneration that freezes the zero-pick
counter during exactly that situation shipped 2026-06-09 — 26 days after they
were gone.

Four failure modes, each of which produced a real wrongful retirement:

  A. FIRED_DURING_BLOCK   — retired while buys were blocked firm-wide. The
     agent was graded on an outcome it does not control.
  B. CLONE_REPLACEMENT    — "a fresh specialist will be spawned for this slot"
     resolved to the same class_name and the same file. Nothing changed except
     consecutive_zero_cycles going back to 0. AGENT_CATALOG holds 31 entries
     across 31 distinct sectors, so this was structurally guaranteed for every
     agent in the firm, not bad luck.
  C. HOLLOW_SECOND_CHANCE — the retirement reason cites a role reassignment as
     evidence the agent had its chance, but the reassignment moved nothing:
     `reassigned_to` has no reader, `sector` was never updated, and the
     promised performance reset never happened (the counter ran 9 -> 14).
  D. DUPLICATE_RECORDS    — retire-then-respawn leaves two registry rows for
     one class_name, so the roster count and every per-agent evaluation
     double-counts.

C and D are detectable from the registry alone. A needs the risk state at
retirement time; for a retirement happening now that is cit_hr_memo.json, and
for past ones we fall back to the same memo only when it is fresh, because
grading a May retirement against July's risk posture would be exactly the kind
of stale-evidence reasoning this audit exists to catch.

Findings go to the Chairman queue via chairman_queue.submit(), which scopes the
cap and dedup to this source.
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
from collections import defaultdict, OrderedDict

import chairman_queue

logger = logging.getLogger("hr_retirement_audit")

REGISTRY_PATH = "data/agent_registry.json"
HR_MEMO_PATH = "data/cit_hr_memo.json"
HR_HISTORY_PATH = "data/hr_history.json"
CATALOG_SOURCE = "chief_of_staff_agent.py"
REPORT_PATH = "data/hr_retirement_audit.json"

#: How stale the risk memo may be before we refuse to judge a retirement by it.
MEMO_MAX_AGE_DAYS = 2

#: How early to start surfacing a reinstatement review before its due date.
REVIEW_LEAD_DAYS = 7

#: Which of two severities survives when a batch of findings collapses to one row.
SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

SOURCE = "hr_retirement_audit"


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        logger.warning("could not read %s: %s", path, e)
        return default


def _catalog_sectors():
    """class_name -> sector, parsed out of AGENT_CATALOG.

    Imported textually rather than by importing chief_of_staff_agent, which
    pulls in the whole firm on a module that runs on a cron.
    """
    try:
        src = open(CATALOG_SOURCE).read()
    except OSError:
        return {}
    m = re.search(r'AGENT_CATALOG\s*(?::[^=]*)?=\s*\[(.*?)\n\]', src, re.S)
    if not m:
        return {}
    out = {}
    for entry in re.findall(r'\{[^{}]*\}', m.group(1), re.S):
        cn = re.search(r'"class_name"\s*:\s*"([^"]+)"', entry)
        sec = re.search(r'"sector"\s*:\s*"([^"]+)"', entry)
        if cn and sec:
            out[cn.group(1)] = sec.group(1)
    return out


def _memo_is_fresh(memo):
    ts = memo.get("generated_at")
    if not ts:
        return False
    try:
        age = dt.datetime.now() - dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    return age.days <= MEMO_MAX_AGE_DAYS


def _memo_covers(memo, retired_at):
    """Was this memo current when the retirement happened?

    Returns (covers, gap_days). A missing date on either side is not coverage —
    an unknown retirement date cannot be vouched for by any memo.
    """
    ts, when = memo.get("generated_at"), retired_at
    if not ts or not when:
        return False, None
    try:
        memo_day = dt.date.fromisoformat(str(ts)[:10])
        ret_day = dt.date.fromisoformat(str(when)[:10])
    except ValueError:
        return False, None
    gap = abs((memo_day - ret_day).days)
    return gap <= MEMO_MAX_AGE_DAYS, gap


def _is_retired(a):
    """The firm's one definition of "this agent was retired".

    Every check below used to key on `status == "retired"` alone. Not one agent
    in the registry carries that status — the seven that were retired on
    2026-05-14 carry `retired_at` + `retirement_reason` and `status: "active"`.
    So `retired` was the empty list, checks A and B ran over nothing, and this
    audit reported `clean: true` with seven tombstoned agents on the books.

    Two spellings of one fact is the same defect the roster merge and the
    Chairman queue both had: a producer writes the record one way and the
    consumer looks for it another way, and the gap is silent. One function, used
    everywhere, is the fix — including in the contradiction check itself, which
    is the only place the two spellings are allowed to be told apart.
    """
    return bool(a.get("status") == "retired"
                or (a.get("retired_at") and a.get("retirement_reason")))


def _lifecycle_is_observable(agents):
    """Could this audit find anything at all, on this registry?

    Every check below keys off a lifecycle field — status 'retired',
    `reassigned_to`, a duplicate row. On the VM's registry NONE of those exist:
    66 agents, all 'active' or 'inactive', no retirements, no reassignments,
    because the PIP lifecycle was deadlocked and never ran (see the deadlock
    note in chief_of_staff_agent.run()). So this audit reported "no wrongful
    retirement signals, 66 agents checked" every week — true, and completely
    uninformative, because its subject matter structurally could not occur.

    An auditor that cannot distinguish "nothing is wrong" from "I cannot see
    anything" is not an auditor. Returns (observable, reasons).
    """
    reasons = []
    if not agents:
        return False, ["registry is empty — nothing to audit"]

    lifecycle_fields = ("reassigned_to", "retired_at", "performance_notice_sent",
                        "pip_issued_at")
    lifecycle_statuses = {"retired", "reassigned", "pip", "noticed"}

    if not any(a.get("status") in lifecycle_statuses for a in agents) and \
       not any(any(f in a for f in lifecycle_fields) for a in agents):
        reasons.append(
            "no agent in the registry carries any lifecycle field (no retirement, "
            "reassignment, PIP or notice) — the PIP lifecycle has never run here, "
            "so a clean result means 'nothing to see', not 'nothing wrong'")

    if not any("consecutive_zero_cycles" in a for a in agents):
        reasons.append(
            "no agent carries consecutive_zero_cycles — the performance counter "
            "has never been persisted on this host")

    return (not reasons), reasons


def _reinstatement_reviews(history, today=None):
    """Reinstated agents whose promised monitoring review has come due.

    A vacated retirement records `monitoring_review_by`. Nothing read that field
    — it was a promise written into a JSON file, which is how "flagged but never
    solved" starts. This makes it load-bearing: when the date is within
    REVIEW_LEAD_DAYS, the Chairman is asked to confirm the agent has earned its
    place, which is the condition the reinstatement was granted on.
    """
    today = today or dt.date.today()
    findings = []
    for cn, rec in sorted((history.get("agent_records") or {}).items()):
        for ev in rec.get("events") or []:
            if ev.get("event") != "RETIREMENT_VACATED":
                continue
            due_raw = ev.get("monitoring_review_by")
            if not due_raw:
                continue
            try:
                due = dt.date.fromisoformat(str(due_raw)[:10])
            except ValueError:
                continue
            days_left = (due - today).days
            if days_left > REVIEW_LEAD_DAYS:
                continue
            findings.append({
                "check": "REINSTATEMENT_REVIEW_DUE",
                "severity": "HIGH" if days_left < 0 else "MEDIUM",
                "class_name": cn,
                "description": (
                    "%s was reinstated on %s after its %s retirement was vacated, on "
                    "the condition that it be monitored and confirmed necessary. That "
                    "review is due %s (%s). Confirm the agent has earned its place or "
                    "retire it deliberately this time.%s"
                    % (cn, ev.get("date", "?"),
                       ev.get("summary", "").split(".")[0][:60] or "earlier",
                       due.isoformat(),
                       "OVERDUE by %d day(s)" % -days_left if days_left < 0
                       else "in %d day(s)" % days_left,
                       _productivity_caveat())),
            })
    return findings


def _productivity_caveat():
    """State plainly what evidence the review can and cannot rest on."""
    return (" NOTE: the natural evidence — approved picks credited to this agent — "
            "is currently unavailable. No approved decision in the manager log "
            "carries a source_agent, so per-agent productivity cannot be measured "
            "and the PIP lifecycle is paused by the attribution interlock. Judge on "
            "submitted work and coverage until attribution is repaired.")


def audit(registry=None, memo=None, catalog=None, history=None):
    """Return a list of findings. Pure — no I/O when the args are supplied,
    so the tests can drive it with fixtures."""
    registry = registry if registry is not None else _load(REGISTRY_PATH, {})
    memo = memo if memo is not None else _load(HR_MEMO_PATH, {})
    catalog = catalog if catalog is not None else _catalog_sectors()
    history = history if history is not None else _load(HR_HISTORY_PATH, {})
    agents = registry.get("agents", [])

    findings = []
    by_class = defaultdict(list)
    for a in agents:
        by_class[a.get("class_name")].append(a)

    # D. duplicate records ---------------------------------------------------
    for cn, recs in sorted(by_class.items()):
        if len(recs) > 1:
            statuses = sorted(r.get("status", "?") for r in recs)
            findings.append({
                "check": "DUPLICATE_RECORDS",
                "severity": "MEDIUM",
                "class_name": cn,
                "description": (
                    "%s has %d registry records (statuses: %s). Retire-then-respawn "
                    "leaves the old row in place, so the roster count and every "
                    "per-agent evaluation double-count this agent."
                    % (cn, len(recs), ", ".join(statuses))),
            })

    retired = [a for a in agents if _is_retired(a)]

    # E. the retirement was recorded and never enforced -----------------------
    # A tombstone on a working agent is a live contradiction, not a tidy-up job.
    # Whichever way it gets resolved, one of two real things is true today: seven
    # sector agents were fired and never stopped trading, or they were spared and
    # the firing was never withdrawn. Both are the Chairman's call, and until it
    # is made, any code that starts honouring `retired_at` silently deletes seven
    # sectors of coverage on its first run.
    for a in agents:
        if a.get("status") == "retired" or not a.get("retired_at"):
            continue
        findings.append({
            "check": "RETIREMENT_NEVER_ENFORCED",
            "severity": "HIGH",
            "class_name": a.get("class_name"),
            "batch": "retired-%s" % a.get("retired_at"),
            "description": (
                "%s carries retired_at=%s and a retirement reason, but status is "
                "'%s'%s — so it is still loaded and still working. The retirement "
                "was written down and never carried out. Decide it deliberately: "
                "vacate the retirement (clear retired_at + retirement_reason, log a "
                "RETIREMENT_VACATED event) or enforce it (status='retired', and "
                "accept losing this agent's coverage). Leaving it contradictory means "
                "the next reader of retired_at decides by accident."
                % (a.get("class_name"), a.get("retired_at"), a.get("status"),
                   " and permanent=true" if a.get("permanent") else "")),
        })

    # B. clone replacement ---------------------------------------------------
    for a in retired:
        cn = a.get("class_name")
        clones = [o for o in by_class[cn]
                  if o is not a and not _is_retired(o)
                  and o.get("file") == a.get("file")]
        if clones:
            findings.append({
                "check": "CLONE_REPLACEMENT",
                "severity": "HIGH",
                "class_name": cn,
                "description": (
                    "%s was retired on %s and 'replaced' by a record with the same "
                    "class_name and the same file (%s). The replacement cannot behave "
                    "differently — the only thing the retirement changed is "
                    "consecutive_zero_cycles going back to 0."
                    % (cn, a.get("retired_at", "?"), a.get("file"))),
            })

    # A. fired during a firm-wide block --------------------------------------
    # The memo has to be contemporaneous with the RETIREMENT, not merely recent.
    # `fresh` alone asks "is this memo current?", which is the wrong question for
    # a retirement dated 2026-05-14: today's memo is maximally fresh and says
    # nothing whatsoever about May. Until `retired` stopped being the empty list
    # this never fired, so the gap never showed. There is no historical risk
    # record on disk (risk_bulletin.json is current-state only), so a retirement
    # older than the memo's window is genuinely unverifiable — say that instead of
    # asserting a wrongful firing on evidence from 79 days after the fact. The
    # audit exists to catch stale-evidence reasoning; it does not get an exemption.
    blocked = bool(memo.get("buys_blocked")) or memo.get("cro_level") in ("HIGH", "CRITICAL")
    fresh = _memo_is_fresh(memo)
    for a in retired:
        if not blocked:
            continue
        covers, gap = _memo_covers(memo, a.get("retired_at"))
        if not fresh or not covers:
            # Refuse to judge on stale evidence rather than guess.
            findings.append({
                "check": "UNVERIFIABLE_RETIREMENT",
                "severity": "LOW",
                "class_name": a.get("class_name"),
                "batch": "retired-%s" % a.get("retired_at"),
                "description": (
                    "%s is retired (retired_at=%s) but cit_hr_memo.json cannot speak to "
                    "that date (generated_at=%s%s), so whether buys were blocked at "
                    "retirement time cannot be established. Not asserting a wrongful "
                    "retirement on evidence that post-dates the decision."
                    % (a.get("class_name"), a.get("retired_at", "?"),
                       memo.get("generated_at"),
                       "" if gap is None else ", %d day(s) apart" % gap)),
            })
            continue
        findings.append({
            "check": "FIRED_DURING_BLOCK",
            "severity": "HIGH",
            "class_name": a.get("class_name"),
            "description": (
                "%s is retired for lack of APPROVED picks while buys are blocked "
                "firm-wide (cro_level=%s, buys_blocked=%s). Approval is not something "
                "a research agent controls; under a freeze no agent can earn one. "
                "Review whether this retirement should be vacated."
                % (a.get("class_name"), memo.get("cro_level"), memo.get("buys_blocked"))),
        })

    # C2. the retirement still cites a second chance that has been voided ------
    # The remediation for check C moves `reassigned_to` into `voided_reassignment`,
    # which is correct — it stops the hollow move being cited as a fresh start
    # ever again. But C keys on `reassigned_to`, so voiding it also switched C off:
    # on 2026-07-31 all seven of these were voided and this audit went from finding
    # them to reporting clean: true, with the retirements those hollow moves
    # justified still standing. A fix that deletes the detector's input closes the
    # finding without closing the problem. Read the void record too.
    for a in agents:
        void = a.get("voided_reassignment")
        if not void or not a.get("retirement_reason"):
            continue
        if "reassignment" not in str(a.get("retirement_reason", "")).lower():
            continue
        findings.append({
            "check": "RETIREMENT_CITES_VOIDED_CHANCE",
            "severity": "HIGH",
            "class_name": a.get("class_name"),
            "batch": "retired-%s" % a.get("retired_at"),
            "description": (
                "%s's retirement reason rests on a role reassignment ('%s', %s) that "
                "this firm itself voided on %s: %s. The stated grounds for the "
                "retirement no longer hold, and the retirement is still on the record. "
                "Vacate it or restate it on grounds that survive."
                % (a.get("class_name"), void.get("was", "?"), void.get("at", "?"),
                   void.get("voided_on", "?"), str(void.get("why", ""))[:160])),
        })

    # C. hollow second chance ------------------------------------------------
    for a in agents:
        dest = a.get("reassigned_to")
        if not dest:
            continue
        home = catalog.get(a.get("class_name"))
        if a.get("sector") == dest:
            continue        # the move actually landed
        findings.append({
            "check": "HOLLOW_SECOND_CHANCE",
            "severity": "HIGH" if _is_retired(a) else "MEDIUM",
            "class_name": a.get("class_name"),
            "description": (
                "%s was reassigned to '%s' on %s but its sector is still '%s'%s. "
                "`reassigned_to` has no reader anywhere in the codebase, so this "
                "agent never changed what it does. A reassignment that moves nothing "
                "cannot count as the second chance a retirement reason relies on."
                % (a.get("class_name"), dest, a.get("reassigned_at", "?"),
                   a.get("sector"),
                   "" if home is None else " (catalog home: '%s')" % home)),
        })

    # E. reinstatement monitoring comes due ----------------------------------
    findings.extend(_reinstatement_reviews(history))

    # F. can this audit see anything at all? ---------------------------------
    observable, reasons = _lifecycle_is_observable(agents)
    if not observable:
        findings.append({
            "check": "LIFECYCLE_UNAUDITABLE",
            # Not LOW. A LOW is triaged away, and the thing being reported is
            # that this audit's weekly all-clear has been meaningless — the
            # blindness matters more than any single finding it could have made.
            "severity": "MEDIUM",
            "class_name": "(registry)",
            "description": (
                "This audit cannot produce a meaningful result on this registry "
                "(%d agent(s)): %s. Reporting 'clean' here would mean 'I could not "
                "see anything', which is exactly the false all-clear this check "
                "exists to prevent."
                % (len(agents), "; ".join(reasons))),
        })

    return findings


def _as_decisions(findings):
    """One row per DECISION, not one per agent.

    Seven agents were retired on the same day, by the same batch, for the same
    reason, and every question about them has one answer. Filed per-agent that is
    21 rows in the Chairman queue for three decisions — and each row carries its
    own 50-day board escalation, so one call would escalate 21 times. That is the
    "124 rows of one complaint" shape this firm has now hit three times.

    A finding carrying `batch` is part of one event and collapses with its
    siblings; every affected agent is still named in the description, so nothing
    is lost and nothing is counted twice. A finding with no `batch` (a duplicate
    record, a reinstatement coming due) is genuinely about one agent and stays
    its own row. Returns findings with a `subject` field — the id/dedup key.
    """
    out, groups = [], OrderedDict()
    for f in findings:
        if not f.get("batch"):
            out.append(dict(f, subject=f["class_name"]))
            continue
        groups.setdefault((f["check"], f["batch"]), []).append(f)

    for (check, batch), group in groups.items():
        if len(group) == 1:
            out.append(dict(group[0], subject=group[0]["class_name"]))
            continue
        names = sorted(g["class_name"] for g in group)
        rep = next(g for g in group if g["class_name"] == names[0])
        out.append({
            "check": check,
            "severity": max((g["severity"] for g in group),
                            key=lambda s: SEVERITY_RANK.get(s, 0)),
            "class_name": names[0],
            "subject": batch,
            # The representative's own wording, unedited — splicing a name out of
            # a sentence written for one agent produces "7 agents ... is retired".
            "description": ("%d agents share this finding, all retired in one batch "
                            "(%s): %s. ONE decision covers all %d. Stated for %s:\n\n%s"
                            % (len(names), batch, ", ".join(names), len(names),
                               names[0], rep["description"])),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="print findings, do not touch the Chairman queue")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")

    findings = audit()
    agents = _load(REGISTRY_PATH, {}).get("agents", [])
    observable, inert_reasons = _lifecycle_is_observable(agents)

    # `clean` must mean "I looked and found nothing", never "I could not look".
    report = {
        "generated_at": dt.datetime.now().isoformat(),
        "agents_checked": len(agents),
        "auditable": observable,
        "inert_reasons": inert_reasons,
        "findings": findings,
        "clean": observable and not findings,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if not findings:
        if observable:
            logger.info("No wrongful-retirement signals. %d agent(s) checked.",
                        len(agents))
        else:
            logger.warning(
                "NOT CLEAN — UNAUDITABLE. %d agent(s), but nothing this audit "
                "checks can occur on this registry: %s",
                len(agents), "; ".join(inert_reasons))
        return 0

    for f in findings:
        logger.warning("[%s] %s: %s", f["severity"], f["check"], f["description"])

    if args.report:
        logger.info("--report: %d finding(s), Chairman queue untouched.", len(findings))
        return 0

    entries = [{
        # class_name is part of the id, not just the dedup_key: two agents can
        # trip the same check on the same day (both reinstatements come due
        # together), and without it they'd be raised as two rows sharing one id.
        "id": "hr-retire-%s-%s-%s" % (dt.date.today().strftime("%Y%m%d"),
                                      f["check"].lower(),
                                      re.sub(r"[^a-z0-9]+", "",
                                             f["subject"].lower())[:24]),
        "dedup_key": "%s:%s" % (f["check"], f["subject"]),
        "severity": f["severity"],
        "category": "GOVERNANCE",
        "description": f["description"],
        "status": "awaiting_chairman",
        "raised_at": dt.datetime.now().isoformat(),
    } for f in _as_decisions(findings)]
    added, suppressed = chairman_queue.submit(entries, SOURCE)
    logger.info("%d finding(s): %d raised, %d already open.",
                len(findings), added, suppressed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

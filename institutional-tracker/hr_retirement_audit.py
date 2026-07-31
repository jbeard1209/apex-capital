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
from collections import defaultdict

import chairman_queue

logger = logging.getLogger("hr_retirement_audit")

REGISTRY_PATH = "data/agent_registry.json"
HR_MEMO_PATH = "data/cit_hr_memo.json"
CATALOG_SOURCE = "chief_of_staff_agent.py"
REPORT_PATH = "data/hr_retirement_audit.json"

#: How stale the risk memo may be before we refuse to judge a retirement by it.
MEMO_MAX_AGE_DAYS = 2

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


def audit(registry=None, memo=None, catalog=None):
    """Return a list of findings. Pure — no I/O when the args are supplied,
    so the tests can drive it with fixtures."""
    registry = registry if registry is not None else _load(REGISTRY_PATH, {})
    memo = memo if memo is not None else _load(HR_MEMO_PATH, {})
    catalog = catalog if catalog is not None else _catalog_sectors()
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

    retired = [a for a in agents if a.get("status") == "retired"]

    # B. clone replacement ---------------------------------------------------
    for a in retired:
        cn = a.get("class_name")
        clones = [o for o in by_class[cn]
                  if o is not a and o.get("status") != "retired"
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
    blocked = bool(memo.get("buys_blocked")) or memo.get("cro_level") in ("HIGH", "CRITICAL")
    fresh = _memo_is_fresh(memo)
    for a in retired:
        if not blocked:
            continue
        if not fresh:
            # Refuse to judge on stale evidence rather than guess.
            findings.append({
                "check": "UNVERIFIABLE_RETIREMENT",
                "severity": "LOW",
                "class_name": a.get("class_name"),
                "description": (
                    "%s is retired but cit_hr_memo.json is stale (generated_at=%s), so "
                    "whether buys were blocked at retirement time cannot be established. "
                    "Not asserting a wrongful retirement on evidence this old."
                    % (a.get("class_name"), memo.get("generated_at"))),
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
            "severity": "HIGH" if a.get("status") == "retired" else "MEDIUM",
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

    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="print findings, do not touch the Chairman queue")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")

    findings = audit()
    report = {
        "generated_at": dt.datetime.now().isoformat(),
        "findings": findings,
        "clean": not findings,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if not findings:
        logger.info("No wrongful-retirement signals. %d agent(s) checked.",
                    len(_load(REGISTRY_PATH, {}).get("agents", [])))
        return 0

    for f in findings:
        logger.warning("[%s] %s: %s", f["severity"], f["check"], f["description"])

    if args.report:
        logger.info("--report: %d finding(s), Chairman queue untouched.", len(findings))
        return 0

    entries = [{
        "id": "hr-retire-%s-%s" % (dt.date.today().strftime("%Y%m%d"),
                                   f["check"].lower()),
        "dedup_key": "%s:%s" % (f["check"], f["class_name"]),
        "severity": f["severity"],
        "category": "GOVERNANCE",
        "description": f["description"],
        "status": "awaiting_chairman",
        "raised_at": dt.datetime.now().isoformat(),
    } for f in findings]
    added, suppressed = chairman_queue.submit(entries, SOURCE)
    logger.info("%d finding(s): %d raised, %d already open.",
                len(findings), added, suppressed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

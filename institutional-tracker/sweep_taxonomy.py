"""The closed taxonomy of everything the weekly firm sweep can discover.

WHY THIS FILE EXISTS
--------------------
CLAUDE.md names the firm's main bug class: a producer emits an identifier that no
consumer handles, `.get()` swallows it, and the finding is produced but never acted
on. A weekly sweep is a *machine for emitting identifiers*, so building one without
closing that hole would industrialise the exact failure it is meant to catch.

So the sweep may not emit a free-form finding. Every finding carries a `kind`, every
kind must appear in ROUTES below, and `make_finding()` REFUSES to build a finding
whose kind has no route. There is no `.get(kind, default)` anywhere in this module —
an unknown kind raises `UnroutedFinding`, and the governance runner converts that
raise into a CRITICAL board + Chairman escalation. The failure mode of adding a new
finding without a handler is therefore a loud escalation, never silence.

The bijection (KINDS <-> ROUTES), the reality of every owner queue, and the board's
willingness to accept every category are all asserted by
`test_pipeline_smoke.py::TestSweepTaxonomyIsClosed`.

READING A ROUTE
---------------
    owner      who must act. Must be a key of OWNERS, each of which is a real queue
               file with a real consumer module (asserted by test).
    severity   LOW | MEDIUM | HIGH | CRITICAL. Drives Chairman escalation and the
               deputy board's auto-implement gate.
    category   must be a category `deputy_board.py` recognises, or the proposal is
               filed and then silently skipped by the voter — the bug class again.
    to_board   file a proposal the deputy board votes on.
    to_hr      additionally drop it in HR's action queue.
    to_chairman  additionally raise a Chairman row. Reserved for decisions Jack
               actually has to make; routine findings must NOT reach him.
    sla_days   how long the owner has before problem_lifecycle_sentinel calls it
               STALE and escalates.
    remedy     the concrete action. A finding without a stated remedy is a
               complaint, not a work item.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Owners. Each is a real queue with a real consumer — not an aspiration.
# `consumer` names the module that READS the queue; the contract test greps it.
# ---------------------------------------------------------------------------
OWNERS = {
    "CIT": {
        "queue": "data/cit_pending_proposals.json",
        "consumer": ["deputy_board.py", "board_meeting.py"],
        "desc": "Code Inspection Team — fixes defects in code and wiring",
    },
    "HR": {
        "queue": "data/hr_action_queue.json",
        "consumer": ["hr_agent.py"],
        "desc": "HR — agent employment, attendance, review, retirement",
    },
    "BOARD": {
        "queue": "data/cit_pending_proposals.json",
        "consumer": ["deputy_board.py", "board_meeting.py"],
        "desc": "Deputy board — votes on what to do when the answer is a judgement call",
    },
    "CHAIRMAN": {
        "queue": "data/chairman_action_required.json",
        "consumer": ["chairman_queue.py", "chairman_inbox.py"],
        "desc": "Jack — only for decisions the firm may not make for itself",
    },
}

SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# Categories the deputy board actually routes. Kept as a literal here and
# cross-checked against deputy_board.py by the contract test, so that a rename
# on either side fails a test instead of silently orphaning every finding.
BOARD_CATEGORIES = frozenset({
    "BUG_FIX", "LOGIC_ERROR", "INTEGRATION", "IMPROVEMENT", "VISION",
    "STRATEGY", "ARCHITECTURE",
})


class UnroutedFinding(Exception):
    """Raised when something tries to emit a finding kind with no declared route.

    Never caught quietly. `sweep_governance.run()` turns this into a CRITICAL
    board + Chairman escalation, because a finding nobody owns is precisely the
    condition that produced the Oracle host and the seven wrongful retirements.
    """


def _route(owner, severity, category, remedy, *, to_board=True, to_hr=False,
           to_chairman=False, sla_days=20, why=""):
    return {
        "owner": owner, "severity": severity, "category": category,
        "remedy": remedy, "to_board": to_board, "to_hr": to_hr,
        "to_chairman": to_chairman, "sla_days": sla_days, "why": why,
    }


# ---------------------------------------------------------------------------
# THE TAXONOMY.
#
# Adding a kind here without a route is impossible: this dict IS the route table
# and KINDS is derived from it, so the two cannot drift apart. The danger is the
# opposite one — code emitting a string that was never added here at all — and
# that is what make_finding() rejects.
# ---------------------------------------------------------------------------
ROUTES = {

    # ── Agent health: can it even run? ──────────────────────────────────────
    "AGENT_IMPORT_FAILS": _route(
        "CIT", "CRITICAL", "BUG_FIX",
        "Fix the import error, then re-run the sweep to confirm the agent loads. "
        "If the agent is genuinely obsolete, take it to the board for retirement "
        "rather than leaving a module that cannot load.",
        to_chairman=False, sla_days=7,
        why="international_analyst.py crashed on import for the firm's entire "
            "history with five wired consumers, which is why the firm was blind to "
            "the July 2026 Korean crash. An agent that cannot import does not fail "
            "loudly — it fails as an absence of output nobody notices."),

    "AGENT_IMPORT_TIMEOUT": _route(
        "CIT", "HIGH", "BUG_FIX",
        "Find the module-level blocking call (usually a network fetch at import) "
        "and move it inside a function.",
        sla_days=14,
        why="A module that hangs at import stalls every scheduler root that "
            "imports it, so one slow agent can silently halt the whole pipeline."),

    "AGENT_WRITES_ON_IMPORT": _route(
        "CIT", "MEDIUM", "BUG_FIX",
        "Move the write into a function called by main()/run(); importing a module "
        "must not mutate firm state.",
        sla_days=20,
        why="Import-time writes mean merely inspecting the firm changes it, which "
            "makes every audit unreliable and every dry run a live run."),

    "AGENT_REPORT_FAILS": _route(
        "CIT", "HIGH", "BUG_FIX",
        "Fix the crash in the agent's --report path.",
        sla_days=14,
        why="--report is the documented way to inspect an agent. If it crashes, "
            "the agent is unauditable and its silence is indistinguishable from health."),

    # ── Agent connectivity: does it speak to anyone? ─────────────────────────
    "AGENT_ISOLATED": _route(
        "CIT", "HIGH", "INTEGRATION",
        "Decide and implement one of: (a) wire the agent's output to a real "
        "consumer, (b) wire a real producer to its input, or (c) take it to the "
        "board for retirement. Notify HR either way so the roster reflects reality.",
        to_hr=True, sla_days=20,
        why="The Chairman's standing instruction: the firm must never lose track of "
            "an agent, because a key agent that quietly stops doing its job is "
            "disastrous. An agent that reads nothing anyone writes and writes "
            "nothing anyone reads is already lost — it is running and irrelevant."),

    "AGENT_OUTPUT_UNCONSUMED": _route(
        "CIT", "MEDIUM", "INTEGRATION",
        "Either wire a consumer to the output file or stop producing it. Record "
        "which, so the next sweep does not re-raise a decision already made.",
        sla_days=20,
        why="fi_cit_agent wrote to a file no consumer read — work performed every "
            "cycle and discarded every cycle."),

    "AGENT_INPUT_UNPRODUCED": _route(
        "CIT", "MEDIUM", "INTEGRATION",
        "Write the missing producer, or change the agent to degrade honestly when "
        "its input is absent (never to fail open).",
        sla_days=20,
        why="A reader whose producer does not exist does not crash — it reads an "
            "empty default and reports all-clear forever."),

    # ── Agent attendance: does it come to work? ─────────────────────────────
    "AGENT_UNSCHEDULED": _route(
        "BOARD", "MEDIUM", "INTEGRATION",
        "Board decides SCHEDULE / RETIRE / DELETE for each agent. HR updates the "
        "roster to match the decision; CIT implements it.",
        to_hr=True, sla_days=30,
        why="An employed agent that runs nowhere costs nothing and does nothing, "
            "but it inflates the roster and hides real coverage gaps behind a "
            "plausible-looking name."),

    "AGENT_OUTPUT_STALE": _route(
        "HR", "HIGH", "BUG_FIX",
        "HR treats this as an attendance failure: confirm whether the agent ran, "
        "then route the root cause to CIT if it is a defect.",
        to_hr=True, sla_days=10,
        why="A scheduled agent whose output stopped changing is the exact shape of "
            "the failure the Chairman asked to be made impossible: it is on the "
            "roster, it is on the crontab, and it has quietly stopped working."),

    "AGENT_OUTPUT_NEVER_PRODUCED": _route(
        "CIT", "HIGH", "BUG_FIX",
        "Determine whether the agent has ever run successfully. Fix or retire it.",
        sla_days=14,
        why="An output file that has never existed proves the agent has never "
            "completed a run, no matter how long it has been on the roster."),

    "AGENT_FILE_MISSING": _route(
        "HR", "HIGH", "BUG_FIX",
        "The roster names a module file that is not on disk. Correct the roster "
        "entry or restore the file.",
        to_hr=True, sla_days=10,
        why="A roster that names files that do not exist cannot be trusted as the "
            "firm's headcount, and the roster is what the ratchet measures."),

    "AGENT_ZOMBIE": _route(
        "HR", "HIGH", "LOGIC_ERROR",
        "Resolve the contradiction: either truly retire the agent (and stop loading "
        "it) or reinstate it in the registry. Never leave both true.",
        to_hr=True, sla_days=10,
        why="Retired on paper and still loaded every cycle: the firm believes it "
            "fired an agent that is still trading its opinion into the pipeline."),

    "AGENT_UNREVIEWED": _route(
        "HR", "LOW", "IMPROVEMENT",
        "HR adds the agent to its review roster and scores it in the next cycle.",
        to_hr=True, sla_days=45,
        why="The graders were ungraded: agents HR never scores can never be found "
            "wanting, so poor performance in that set is structurally invisible."),

    # ── Detector health: can the alarms actually fire? ──────────────────────
    "DETECTOR_BLIND": _route(
        "CIT", "CRITICAL", "LOGIC_ERROR",
        "Fix the detector so the planted defect is caught, then re-run the "
        "mutation harness to prove it now fires.",
        to_chairman=True, sla_days=7,
        why="A detector that stays green when fed a known defect is worse than no "
            "detector: it converts an unknown risk into a false assurance. This is "
            "the 'presence is not currency' failure — the alarm exists, so nobody "
            "checks whether it works."),

    "DETECTOR_FALSE_POSITIVE": _route(
        "CIT", "HIGH", "LOGIC_ERROR",
        "Tighten the detector so a healthy control state does not trip it.",
        sla_days=14,
        why="An alarm that fires on healthy input gets muted by its readers, which "
            "disables it for the real defect just as effectively as blindness."),

    "DETECTOR_ERRORS": _route(
        "CIT", "HIGH", "BUG_FIX",
        "Fix the crash. A detector that raises is not reporting — its exception is "
        "usually swallowed by the caller's try/except.",
        sla_days=14,
        why="Detectors run inside broad try/except blocks, so a crashing detector "
            "presents to the pipeline as a detector with nothing to report."),

    "DETECTOR_UNTESTED": _route(
        "CIT", "MEDIUM", "IMPROVEMENT",
        "Add a mutation case: a known-bad input the detector must flag, and a "
        "known-good control it must ignore.",
        sla_days=30,
        why="An untested detector is an unverified assurance; the firm has already "
            "shipped three of those in guard tests that wrote the state they wished for."),

    # ── Firm-level: the sweep's own integrity ───────────────────────────────
    "HOST_DIVERGENCE": _route(
        "CIT", "HIGH", "INTEGRATION",
        "Deploy so both hosts match, then re-run the sweep on both and confirm "
        "identical findings.",
        sla_days=7,
        why="Two hosts trade the same Alpaca account. Green on the Mac is not "
            "green — the VM ran June-3 code for a month and traded differently."),

    "SWEEP_CRASHED": _route(
        "CIT", "CRITICAL", "BUG_FIX",
        "Fix the sweep. Until it runs, the firm has no weekly assurance at all.",
        to_chairman=True, sla_days=3,
        why="A monitoring system that dies takes its own alarm with it, so its "
            "failure is silent by construction."),

    "SWEEP_DID_NOT_RUN": _route(
        "CIT", "CRITICAL", "BUG_FIX",
        "Restore the weekly schedule on both hosts and confirm a fresh report stamp.",
        to_chairman=True, sla_days=3,
        why="The deadman. A weekly job that quietly stops looks exactly like a "
            "weekly job with nothing to report."),

    "UNROUTED_FINDING": _route(
        "CIT", "CRITICAL", "LOGIC_ERROR",
        "Add the missing kind to sweep_taxonomy.ROUTES with an owner and a remedy, "
        "then re-run the sweep so the finding reaches its owner.",
        to_chairman=True, sla_days=3,
        why="This is the catch-all that makes the taxonomy closed. It fires only "
            "when code tried to emit a finding kind nobody owns — the firm's named "
            "main bug class, caught at the moment of emission instead of being "
            "discovered months later as an empty queue file."),
}

KINDS = frozenset(ROUTES)

# Kinds that describe the sweep's own health rather than the firm's. Reported even
# on an otherwise clean run, because their absence is the interesting signal.
META_KINDS = frozenset({"SWEEP_CRASHED", "SWEEP_DID_NOT_RUN", "UNROUTED_FINDING"})


def route_for(kind: str) -> dict:
    """The single lookup. Raises rather than defaulting — that is the whole point."""
    if kind not in ROUTES:
        raise UnroutedFinding(
            "finding kind %r has no route in sweep_taxonomy.ROUTES; "
            "every kind needs a declared owner and remedy" % (kind,))
    return ROUTES[kind]


def make_finding(kind: str, subject: str, detail: str, evidence=None,
                 severity: str = None, host: str = None) -> dict:
    """Build one validated finding. The only sanctioned way to create one.

    `subject` is the thing the finding is about (an agent name, a detector name).
    It is the dedup identity, so it must be stable run-to-run — never a timestamp.
    """
    route = route_for(kind)          # raises UnroutedFinding for unknown kinds
    sev = severity or route["severity"]
    if sev not in SEVERITIES:
        raise UnroutedFinding("finding %r carries severity %r, which is not one of %s"
                              % (kind, sev, ", ".join(SEVERITIES)))
    if not subject:
        raise UnroutedFinding("finding %r has no subject; it would dedup against "
                              "every other finding of its kind" % (kind,))
    return {
        "kind": kind,
        "subject": subject,
        "detail": detail,
        "evidence": evidence if evidence is not None else [],
        "severity": sev,
        "owner": route["owner"],
        "category": route["category"],
        "remedy": route["remedy"],
        "why": route["why"],
        "sla_days": route["sla_days"],
        "to_board": route["to_board"],
        "to_hr": route["to_hr"],
        # Deliberately NOT `or sev == "CRITICAL"`. The Chairman's standing
        # instruction is to be told only when a serious DECISION is made, and a
        # CRITICAL import failure is not a decision for him — it is work for CIT
        # with a 7-day SLA. If CIT lets it go stale, problem_lifecycle_sentinel
        # escalates it to him then. Auto-escalating every CRITICAL would refill
        # the queue he has spent this week draining, and a queue nobody can drain
        # is a queue nobody reads. Only the four kinds that mean "the assurance
        # system itself is broken" carry to_chairman=True.
        "to_chairman": route["to_chairman"],
        "host": host,
    }

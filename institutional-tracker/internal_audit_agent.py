#!/usr/bin/env python3
"""APEX CAPITAL — INTERNAL AUDIT (the third line of defence)

WHY THIS EXISTS
===============
A real firm defends itself in three lines. The FIRST does the work — research
team, pods, manager. The SECOND checks the work — CRO, IRM, canary, CIT, the
pre-trade devil's advocate. Apex has both, and has had both for months.

It has never had a THIRD line: the one whose only job is to check that *the
checking works*. Every expensive escape this firm has had lived in that gap, and
they all look identical in hindsight — **a green light was on the whole time**:

    the Oracle host traded for 69 days      → every health check GREEN
    FI's "BUY" traded nothing for 66 days   → ~118 orders/cycle stamped APPROVED
    catch_up ran main.py 20x/day            → pipeline reported healthy
    13 positions had no entry date          → state.json perfectly self-consistent
    the Kalshi ladder filter dropped 0      → "0 incoherent" read as "all clean"
    deploy.sh could not ship gcp_setup/     → the deploy reported success

WHY CIT AND THE RED TEAM CANNOT COVER THIS
==========================================
It is not a thoroughness gap. It is a shape.

Both are CRITICS. A critic is `critique(artifact) -> findings`, so a critic's
coverage is exactly its input set: `cit_director` is handed .py files and is
complete over .py files; `pre_trade_challenge.challenge(ticker, conviction, ...)`
is handed a proposed trade and is complete over proposed trades. Neither can
find:

  1. a RELATION between two artifacts — FI's "BUY" was correct code in the
     producer and correct code in the consumer; the missing thing was the edge,
     and an edge is in no file;
  2. an ABSENCE — no document anywhere said "there is a third host";
  3. its OWN blindness — a critic cannot notice that it has never once objected.

Adding rules to a critic buys more coverage of the same shape. So this module is
not a fourth critic. It does three things a critic structurally cannot:

  FALSIFIABILITY  Every coherence law is run against a poison pill built to make
                  it fail. A law that returns HOLDS on data that violates it is
                  BLIND, and every HOLDS it has ever reported was worthless.
                  This is mutation testing pointed at the alarms.

  DENOMINATOR     Every law must say HOW MANY things its verdict is about.
                  A pass with n=0 is not a pass — it is a check that found
                  nothing to check and said "clean". That one substitution is
                  behind more escapes here than any other bug.

  CENSUS          The failure classes this firm has ACTUALLY suffered, each with
                  the detector that now watches for it. A class with no detector
                  becomes a ROW — and a row is an artifact, which means CIT and
                  the red team can finally read it. That is how the blind spot
                  gets covered by the existing critics: not by making them
                  sharper, but by manufacturing the artifact they were missing.

The census also checks its own references, because a doc that names a deleted
detector is worse than no doc (memory `auditor_ground_truth`, and
`fix_that_erased_its_detector` where a remediation removed the field its own
check read). A named detector that no longer exists is reported BROKEN.

WHAT THIS MODULE DOES NOT CLAIM
===============================
It cannot enumerate failure classes nobody has thought of. The census is a
record of what has bitten us, not a proof of completeness, and the honest way to
use it is as a ratchet: when a new escape happens, the ritual is to add the row.
A row with no detector is the backlog, in writing, with an owner — which is
strictly better than the same gap being unwritten.

Usage:
    python3 internal_audit_agent.py                # full audit, print, write report
    python3 internal_audit_agent.py --report       # same (agent convention)
    python3 internal_audit_agent.py --controls-md  # render CONTROLS.md to stdout

Exit code: 0 if every control is PROVEN and every class COVERED, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORT = DATA_DIR / "internal_audit_report.json"
CONTROLS_MD = BASE_DIR / "CONTROLS.md"

logger = logging.getLogger("internal_audit")

# --- falsifiability verdicts -------------------------------------------------
PROVEN = "PROVEN"                # the pill made it fail: the alarm works
BLIND = "BLIND"                  # the pill did NOT make it fail: the alarm is broken
INCONCLUSIVE = "INCONCLUSIVE"    # the pill could not reach the comparison
UNPROVEN = "UNPROVEN"            # no pill exists for this control

# --- census verdicts ---------------------------------------------------------
COVERED = "COVERED"
UNCOVERED = "UNCOVERED"
BROKEN = "BROKEN"


# =============================================================================
# 1. FALSIFIABILITY — prove each law can fail
# =============================================================================

class Pill:
    """A synthetic input that a working law MUST reject.

    `run` takes ONE argument — the law callable to feed — and returns its
    LawResult. It takes the law rather than reaching for `coherence_laws.law_x`
    itself, and that is not a style choice: the first version of this module
    hardcoded the module attribute, so swapping the entry in `LAWS` for a broken
    implementation left the pill still exercising the old one, and the audit
    reported PROVEN on a law that could no longer fail. The harness had the
    exact bug it was built to catch — a check pointed at something other than
    what production runs. `prove_can_fail` enumerates `LAWS` and hands each pill
    the registered callable, so there is only one path.

    It is a callable rather than a dict of kwargs because some pills need a
    temporary directory or a fake process runner and have to clean up.

    `injected` records HOW the pill got its data in. True means it went through
    the law's own parameters — the law was exercised the way production calls
    it, and the proof covers the wiring as well as the arithmetic. False means
    the pill had to reach past the signature and substitute a module global; the
    arithmetic is still proven, but nothing here shows the law is connected to
    the real source. That distinction is reported, not hidden, and a False is a
    standing request for the law to grow an injection point.
    """

    def __init__(self, run: Callable, why: str, injected: bool = True):
        self.run = run
        self.why = why
        self.injected = injected


def _pill_positions_conserve(law):
    return law(broker_positions={"ZZPILL": 7.0}, state={"entry_dates": {}})


def _pill_emitted_verbs_are_handled(law):
    """An agent emitting a verb the manager has never heard of."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "zz_pill_agent.py").write_text(
            'def run():\n    return {"action": "ZZPILLVERB", "ticker": "AAPL"}\n')
        return law(agent_dir=d)


def _pill_broker_host_census(law):
    """An order stamped by a host in no registry row — the Oracle, replayed."""
    from order_ledger import STRICT_FROM
    after = datetime.fromisoformat(STRICT_FROM).replace(microsecond=0)
    order = {"client_order_id": "apex-zzpillhost-audit-000000000000",
             "submitted_at": after.isoformat(), "symbol": "AAPL"}
    return law(orders=[order], documented={})


def _pill_deployed_set_matches_repo(law):
    """The trading host reporting a different md5 for a file we track."""

    class _R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def runner(cmd, timeout):
        if cmd[0] == "git":
            return _R("coherence_laws.py")
        return _R("0" * 32 + "  coherence_laws.py\n---FIND---\ncoherence_laws.py\n")

    return law(runner, has_checkout=True)


def _pill_nested_probabilities_are_monotone(law):
    """"Over 1.5 goals" BID above what "over 0.5 goals" ASKS — impossible.

    Carries both sides of the book on purpose. The pill used to quote asks
    only, and when the law was corrected on 2026-08-17 to compare ask against
    bid — the only comparison a spread cannot explain — an ask-only pill would
    have gone quiet and reported this law BLIND. A pill has to be rebuilt
    against the law's real failure condition whenever that condition changes,
    or the third line certifies the second line on a test it no longer takes.
    """
    rows = [
        {"event_ticker": "ZZPILL-EVENT", "ticker": "ZZPILL-A",
         "title": "Will over 0.5 goals be scored?",
         "market_price": 0.40, "yes_bid": 0.38},
        {"event_ticker": "ZZPILL-EVENT", "ticker": "ZZPILL-B",
         "title": "Will over 1.5 goals be scored?",
         "market_price": 0.92, "yes_bid": 0.90},
    ]
    return law(rows=rows)


def _pill_quoted_prices_have_two_sides(law):
    """An ask with nobody on the other side of it."""
    return law(rows=[{"ticker": "ZZPILL", "market_price": 0.97, "yes_bid": 0}])


def _pill_runs_match_schedule(law):
    """A day with more pipeline starts than the schedule asks for — catch_up.

    NOT `injected`: this law takes only `now`, and both of its sources are read
    from module state (`DATA_DIR / pipeline_heartbeat.json` and a crontab probe).
    The pill therefore substitutes those globals and restores them, which proves
    the comparison is right but proves nothing about whether the law is still
    pointed at the files production writes. Wiring an injection point into the
    law would upgrade this to a real proof; until then the gap is reported.
    """
    import coherence_laws as cl
    real_dir, real_sched = cl.DATA_DIR, cl._scheduled_pipeline_runs_today
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
            (d / "pipeline_heartbeat.json").write_text(json.dumps(
                {"starts": [today.isoformat(), today.isoformat(),
                            today.isoformat(), today.isoformat()]}))
            cl.DATA_DIR = d
            cl._scheduled_pipeline_runs_today = lambda now: 1     # 4 observed vs 1
            return law(now=today)
    finally:
        cl.DATA_DIR, cl._scheduled_pipeline_runs_today = real_dir, real_sched


POISON_PILLS: Dict[str, Pill] = {
    "positions_conserve": Pill(
        _pill_positions_conserve,
        "broker holds a symbol the ledger has never heard of"),
    "emitted_verbs_are_handled": Pill(
        _pill_emitted_verbs_are_handled,
        "an agent emits a verb outside the manager's vocabulary"),
    "broker_host_census": Pill(
        _pill_broker_host_census,
        "an order stamped by a host in no registry row"),
    "deployed_set_matches_repo": Pill(
        _pill_deployed_set_matches_repo,
        "the trading host reports a different md5 than the repo"),
    "nested_probabilities_are_monotone": Pill(
        _pill_nested_probabilities_are_monotone,
        "a higher rung priced above a lower one"),
    "quoted_prices_have_two_sides": Pill(
        _pill_quoted_prices_have_two_sides,
        "an ask recorded with no bid against it"),
    "runs_match_schedule": Pill(
        _pill_runs_match_schedule,
        "more pipeline starts observed than the schedule asks for",
        injected=False),
}


def prove_can_fail() -> List[dict]:
    """Run every law against its poison pill. Enumerated from `LAWS`, never from
    the pill table — a law added without a pill must show up as UNPROVEN rather
    than be silently skipped by iterating the wrong side of the pair.
    """
    try:
        import coherence_laws as cl
    except Exception as e:
        return [{"control": "coherence_laws", "verdict": BLIND,
                 "detail": f"the module will not import ({type(e).__name__}: {e}) — "
                           f"the conservation layer is OFF and nothing below ran"}]

    out = []
    for fn, _statement in cl.LAWS:
        name = fn.__name__.replace("law_", "")
        pill = POISON_PILLS.get(name)
        if pill is None:
            out.append({
                "control": name, "verdict": UNPROVEN, "injected": None,
                "detail": "no poison pill — nothing here shows this law is capable "
                          "of returning VIOLATED, so its HOLDS carries no evidence"})
            continue
        try:
            # `fn`, not `cl.law_<name>` — the pill must exercise the callable the
            # registry actually dispatches, or a law swapped out from under us
            # keeps reporting PROVEN on an implementation nothing runs.
            r = pill.run(fn)
        except Exception as e:
            out.append({"control": name, "verdict": INCONCLUSIVE,
                        "injected": pill.injected, "pill": pill.why,
                        "detail": f"the pill itself raised {type(e).__name__}: {e}"})
            continue

        if r.verdict == cl.VIOLATED:
            v, detail = PROVEN, f"rejected the pill: {r.detail[:160]}"
        elif r.verdict == cl.CANNOT_TELL:
            v, detail = INCONCLUSIVE, (
                f"the pill never reached the comparison — the law returned "
                f"CANNOT_TELL ({r.detail[:140]}). The alarm is not shown broken, "
                f"but it is not shown working either")
        else:
            v, detail = BLIND, (
                f"THE LAW PASSED DATA BUILT TO VIOLATE IT ({pill.why}). Every "
                f"HOLDS this law has ever reported is worthless until this is fixed")
        out.append({"control": name, "verdict": v, "injected": pill.injected,
                    "pill": pill.why, "detail": detail})
    return out


# =============================================================================
# 2. DENOMINATOR — a pass with nothing behind it is not a pass
# =============================================================================

def audit_denominators(results: Optional[list] = None) -> List[dict]:
    """Flag every HOLDS that cannot say how many things it examined.

    `results` may be LawResult objects or the dicts from coherence_report.json,
    so this works both inline in the pipeline and against a report on disk.
    """
    if results is None:
        if not REPORT_COHERENCE.exists():
            return [{"control": "coherence_report", "verdict": "CANNOT_TELL",
                     "detail": "no coherence report on this host — the denominator "
                               "rule has nothing to read"}]
        try:
            results = json.loads(REPORT_COHERENCE.read_text()).get("results", [])
        except (ValueError, OSError) as e:
            return [{"control": "coherence_report", "verdict": "CANNOT_TELL",
                     "detail": f"coherence report unreadable: {e}"}]

    findings = []
    for r in results:
        get = r.get if isinstance(r, dict) else (lambda k, _r=r: getattr(_r, k, None))
        name = get("law") or get("name")
        if get("verdict") != "HOLDS":
            continue
        n = get("n_examined")
        if n is None:
            findings.append({
                "control": name, "verdict": "NO_DENOMINATOR",
                "detail": "reports HOLDS without saying how many things it looked "
                          "at — the verdict cannot be told from a pass on nothing"})
        elif n == 0:
            findings.append({
                "control": name, "verdict": "EMPTY_SUCCESS",
                "detail": "HOLDS on ZERO examined items. This is the shape of every "
                          "false all-clear this firm has had: nothing was checkable, "
                          "and 'nothing failed' was recorded instead"})
    return findings


REPORT_COHERENCE = DATA_DIR / "coherence_report.json"


# =============================================================================
# 3. CENSUS — the failure classes we have actually suffered, and who watches
# =============================================================================

class FailureClass:
    """One way this firm has actually been hurt, and the control that now watches.

    `detectors` are `file.py::symbol` references, resolved by AST — no import, so
    listing a heavyweight module or the test suite here costs nothing and has no
    side effects. AST resolution answers "does this control still exist in the
    codebase", which is the question the census is for; whether it also still
    RUNS is the falsifiability harness's job and `wiring_audit`'s.
    """

    def __init__(self, cid: str, what: str, proved_by: str,
                 detectors: List[str], owner: str, memory: str = ""):
        self.id = cid
        self.what = what
        self.proved_by = proved_by
        self.detectors = detectors
        self.owner = owner
        self.memory = memory


CONTROL_CENSUS: List[FailureClass] = [
    FailureClass(
        "unhandled_verb",
        "A producer emits a token no consumer handles; .get()/try-except swallow "
        "it, so output is produced and never acted on.",
        "FI pod: six agents emitted \"BUY\", the manager branched on BUY_NEW/ADD/"
        "SELL/TRIM with no else. 66 days, zero trades, ~118 orders/cycle stamped "
        "APPROVED by an initialiser.",
        ["coherence_laws.py::law_emitted_verbs_are_handled",
         "wiring_audit.py::check_logic_rule_handlers",
         "test_pipeline_smoke.py::TestWiringContracts"],
        "CIT + Logic Team", "unknown-verb-approved-nothing"),

    FailureClass(
        "reader_without_producer",
        "A consumer reads a file or queue nothing writes any more; the empty read "
        "is indistinguishable from 'nothing to do'.",
        "catch_up.py judged pipeline freshness by a log it never wrote, and ran "
        "main.py 12-22x/day against a design of 4.",
        ["wiring_audit.py::check_glob_consumers_have_producers",
         "wiring_audit.py::check_orphan_queues",
         "coherence_laws.py::law_runs_match_schedule"],
        "CIT", "reader-outlived-its-producer"),

    FailureClass(
        "undeployed_fix",
        "Correct code that cannot physically reach the machine that runs it.",
        "deploy.sh rsynced with --exclude=gcp_setup/, so the VM's own rebuild "
        "script stayed three months stale and its fix could never land.",
        ["coherence_laws.py::law_deployed_set_matches_repo"],
        "whoever owns deploy.sh", "fix-that-cannot-reach-production"),

    FailureClass(
        "undocumented_actor",
        "A host, agent or job that exists in reality and in no document. The "
        "inventory is asked how many there are and answers from itself.",
        "A forgotten Oracle VM ran a full parallel firm on this account for 69 "
        "days. Separately: the roster said 67 agents; there were 155.",
        ["coherence_laws.py::law_broker_host_census",
         "wiring_audit.py::check_orphan_agents",
         "test_pipeline_smoke.py::TestHRSeesTheWholeFirm"],
        "Chairman + HR", "oracle_third_host"),

    FailureClass(
        "unfalsifiable_control",
        "A check whose success path is reachable without evidence — it cannot "
        "fail, so its GREEN means nothing and no one can tell.",
        "The Kalshi ladder filter reported 0 incoherent candidates because all 11 "
        "rungs failed to parse; 'nothing was checkable' was recorded as 'all clean'.",
        ["internal_audit_agent.py::prove_can_fail",
         "internal_audit_agent.py::audit_denominators",
         "test_pipeline_smoke.py::TestInternalAudit"],
        "Internal Audit (this module)", "presence-is-not-currency"),

    FailureClass(
        "fallback_that_acts",
        "A degraded read substitutes a constant, and downstream that constant is "
        "an instruction. The direction is what matters: most of ours meant ACT.",
        "fi_credit_monitor returned 350bp for a dead feed (dead-centre NORMAL, so "
        "a broken feed asserted a calm market); review_agent substituted 'bought "
        "yesterday' for a missing entry date, so 13 positions could never hit "
        "max-hold.",
        ["test_pipeline_smoke.py::TestEntryDateLedger",
         "entry_date_ledger.py::reconcile_state_file"],
        "CIT — NO RUNTIME DETECTOR, tests only",
        "fallback-that-means-act"),

    FailureClass(
        "private_ledger_of_shared_fact",
        "Host-local state describing something an external system owns. Wrong by "
        "design, not by bug: it diverges every time anything happens off-host.",
        "state.json entry_dates: 13 broker positions unledgered, 11 phantom rows, "
        "because there is no state sync between the two hosts. Recurred inside "
        "governance itself: two lifecycle histories of one firm-wide problem, so "
        "the VM called 37 subjects stale and the Mac 25, and 17 rows finished "
        "archived on one host and un-archivable on the other.",
        ["coherence_laws.py::law_positions_conserve",
         "entry_date_ledger.py::reconcile",
         "problem_lifecycle_sentinel.py::writer_verdict",
         "chairman_queue_compactor.py::main",
         "test_pipeline_smoke.py::TestSingleWriterGuard",
         "test_pipeline_smoke.py::TestCompactorWillNotActOnAHistoryItCannotMaintain"],
        "designated writer", "private-diary-of-a-shared-fact"),

    FailureClass(
        "host_divergence",
        "Green on the Mac is not green. Python version, host paths, undeployed "
        "config and ET-vs-UTC clocks all differ between the two hosts.",
        "The suite was green on the Mac and red on the VM five separate ways; a "
        "py3.10 union annotation crashed a module on import on the Mac.",
        ["coherence_laws.py::law_deployed_set_matches_repo",
         "test_pipeline_smoke.py::TestHostSpecificChecksStayOnTheirHost",
         "test_pipeline_smoke.py::TestThePushGateIsTheGateTheRepoShips"],
        "both hosts", "host_divergence_is_a_bug_class"),

    FailureClass(
        "silent_bad_data",
        "A value that is impossible, NaN, or stale passes every guard. NaN in "
        "particular satisfies `is not None` and fails every threshold, so bad "
        "data becomes silence rather than an alarm.",
        "ABBV P/E 256, INCY debt ratio 2.74, WING long-term debt exceeding total "
        "assets, RSI NaN passing its own None-check.",
        ["data_integrity_sentinel.py::check_fundamentals",
         "metric_sanity_sentinel.py::check_risk_metrics",
         "data_integrity_sentinel.py::check_json_health"],
        "Data Integrity Sentinel", "nan-is-not-none"),

    FailureClass(
        "schedule_drift",
        "A scheduled job stops, over-runs, or is disabled while its own status "
        "still reports healthy.",
        "Two Claude scheduled tasks were dead for 9 days at enabled=true; the VM "
        "was 'up' for 8 days while trading nothing.",
        ["coherence_laws.py::law_runs_match_schedule",
         "freshness_sentinel.py::run",
         "test_pipeline_smoke.py::TestDesignatedWriterSilence"],
        "whoever owns the host's scheduler",
        "scheduler-stopped-while-reporting-healthy"),

    FailureClass(
        "never_closed_problem",
        "A problem is found, filed, and never solved — or is band-aided and "
        "recurs under a slightly different title forever.",
        "Findings sat in the Chairman queue past 50 days; the same issue re-filed "
        "under mutating titles defeated dedup.",
        ["problem_lifecycle_sentinel.py::signature",
         "problem_lifecycle_sentinel.py::normalize_title",
         "test_pipeline_smoke.py::TestProblemLifecycle"],
        "Problem Lifecycle Sentinel", "problem_lifecycle_sentinel"),

    FailureClass(
        "incoherent_external_data",
        "The counterparty's own numbers are not arithmetic. Our code is correct "
        "and the input is not a probability/price at all.",
        "45 of 46 Kalshi ladders priced nested outcomes incoherently; 12,000 open "
        "markets carried an ask and zero carried a bid.",
        ["coherence_laws.py::law_nested_probabilities_are_monotone",
         "coherence_laws.py::law_quoted_prices_have_two_sides",
         "test_pipeline_smoke.py::TestKalshiSector"],
        "Kalshi sector", "kalshi_sector"),

    FailureClass(
        "stale_sample",
        "A real measurement, honestly computed, about a producer that no longer "
        "exists — a verdict on a rule we already replaced.",
        "216 settled Kalshi rows said the market was wildly mispriced; every one "
        "came from the loose filter that had already been removed.",
        ["kalshi_calibration.py::is_edge_proven"],
        "Kalshi sector — NO GENERAL DETECTOR, one sector only",
        "sample-outlived-its-rule"),

    FailureClass(
        "fix_erases_its_detector",
        "A remediation deletes the field, file or record its own check reads, so "
        "the check goes quiet and the quiet reads as fixed.",
        "Seven sector agents were vacated by a fix that removed the field the "
        "wrongful-retirement check inspected.",
        ["test_pipeline_smoke.py::TestWrongfulRetirement"],
        "CIT — NO RUNTIME DETECTOR, tests only",
        "fix_that_erased_its_detector"),

    FailureClass(
        "control_output_becomes_its_own_input",
        "A control writes its finding into the queue it scans, so the finding "
        "is tracked as a new problem, ages, and earns a finding about itself. "
        "Every step is individually correct and the queue fills with governance "
        "about governance until nobody reads it — and a queue nobody reads is "
        "not a control.",
        "96 of the Chairman's 214 rows (45%) were rows the sentinel or the SLA "
        "escalator had written about other rows; 18 were about another "
        "governance row. One pyarrow bug accounted for four separate rows.",
        ["problem_lifecycle_sentinel.py::derived_subject",
         "problem_lifecycle_sentinel.py::flag_stale_in_place",
         "problem_lifecycle_sentinel.py::retire_derived_rows",
         "test_pipeline_smoke.py::TestGovernanceDoesNotEscalateItself"],
        "Problem Lifecycle Sentinel", "problem_lifecycle_sentinel"),

    FailureClass(
        "decision_recorded_nowhere",
        "A decision is made and then deleted rather than written down, so the "
        "detector for LOST work cannot tell a considered NO from a disappearance "
        "and reports both as loss. The detector is then correct and useless: "
        "every alarm it raises is indistinguishable from a false one.",
        "clear_voted_proposals wrote only the approved half of each vote, so a "
        "rejected proposal ceased to exist firm-wide. Of 17 signatures the "
        "lifecycle sentinel reported as SILENT_DROP on 2026-08-19, 8 were "
        "approved (in a 1688-row ledger no consumer read) and 9 had been "
        "rejected and deleted. Exactly 1 was a genuine loss.",
        ["cit_director.py::clear_voted_proposals",
         "problem_lifecycle_sentinel.py::_decision_for",
         "test_pipeline_smoke.py::TestABoardDecisionIsNotASilentDrop"],
        "CIT / Problem Lifecycle Sentinel", "problem_lifecycle_sentinel"),

    FailureClass(
        "frozen_copy_of_configuration",
        "A module-level constant captures mutable configuration at IMPORT time. "
        "Whoever later rebinds the source is not the only reader, and the copy "
        "never finds out — so a redirected component keeps reading, and writing, "
        "the original target.",
        "The lifecycle sentinel's queue list was a tuple of Paths built at "
        "import. Every test that sandboxed the sentinel by rebinding its path "
        "globals still scanned data/ through that copy, and flag_stale_in_place "
        "stamped sla_breached onto 146 live rows across all three real queues — "
        "on the Mac, which is not the designated writer host.",
        ["problem_lifecycle_sentinel.py::scanned_queues",
         "test_pipeline_smoke.py::TestABoardDecisionIsNotASilentDrop"],
        "Problem Lifecycle Sentinel — NO GENERAL DETECTOR, tests only",
        "private-diary-of-a-shared-fact"),

    FailureClass(
        "the_test_refreshes_the_liveness_signal",
        "A test exercises a control by running its real entry point, and that "
        "run emits the freshness artifact some OTHER detector reads to decide "
        "the control is still firing. The act of testing the detector keeps the "
        "detector green, so a dead scheduler is indistinguishable from a live "
        "one for as long as anybody runs the suite.",
        "test_pipeline_smoke shelled out to coherence_laws.py, which rewrote "
        "data/coherence_report.json. wiring_audit.check_coherence_layer() reads "
        "that file's timestamp as the tell that the conservation layer has "
        "stopped running — the one detector in the firm that can find an "
        "unanticipated bug. A subprocess cannot be sandboxed by rebinding "
        "module globals the way the in-process tests are, so the write went to "
        "the live data/ every time the suite ran.",
        ["coherence_laws.py::main",
         "test_pipeline_smoke.py::TestCoherenceLaws",
         "test_pipeline_smoke.py::TestTheSuiteDoesNotWriteLiveState"],
        "Coherence Laws / Wiring Audit",
        "scheduler-stopped-while-reporting-healthy"),

    FailureClass(
        "severity_below_the_escalation_floor",
        "A detector grades findings by severity, and the escalation path is "
        "wired only to the top grade. Everything below it is written to a "
        "report and rendered somewhere a human might read, which is not an "
        "owner. The finding is detected, published, and actioned by nobody.",
        "reconciliation_check grades LOW/MEDIUM/HIGH/CRITICAL. "
        "deputy_monitor._escalation_sweep read n_critical and nothing else, and "
        "the report's only other consumer paints top_flags into the morning "
        "briefing. On 2026-08-19 three HIGHs were standing with no queue row "
        "anywhere: pod ledger $6,376 adrift from the broker, cash at 6.0% "
        "against a 20% floor with the flag text itself saying a buy had "
        "bypassed the guardrail, and 21 positions against a cap of 20.",
        ["deputy_monitor.py::_upsert_chairman_row",
         "test_pipeline_smoke.py::TestAReconciliationHIGHReachesSomebody"],
        "Deputy Monitor",
        "verdict-with-no-consequence"),

    FailureClass(
        "limit_enforced_on_one_path_of_many",
        "A risk limit is implemented inside ONE function that reaches the "
        "outside world, while many others reach it directly. The limit is real, "
        "tested, mirrored and reported — and optional, because a caller that "
        "never learned about it cannot forget it. Nothing in the codebase reads "
        "as a bypass: the other paths simply never mentioned the check.",
        "MIN_CASH_FLOOR_PCT and MAX_OPEN_POSITIONS lived in "
        "AlpacaTrader.submit_buy. Sixteen call sites across ten modules reach "
        "TradingClient.submit_order, and six of them open exposure without "
        "consulting either limit: head_agent.py:161, alpaca_trader.py:645 and "
        ":727 (hedge buys), alpaca_trader.py:927 (short open), "
        "fi_executor.py:431 (its own different floor) and main.py:1465. On "
        "2026-08-19 the account stood at 6.0% cash against a 20% floor and 21 "
        "positions against a cap of 20. The fix moves enforcement to the same "
        "monkey-patch choke point order_ledger already uses for exactly this "
        "reason, and gates only exposure-INCREASING orders — blocking an entry "
        "costs an opportunity, blocking an exit costs money with no ceiling.",
        ["risk_gate.py::check",
         "order_ledger.py::install",
         "test_pipeline_smoke.py::TestPreTradeRiskGate",
         "test_pipeline_smoke.py::TestTheRiskGateSitsAtTheChokePoint",
         "test_pipeline_smoke.py::TestTheCashFloorFailsClosed"],
        "CRO / order_ledger",
        "severity-below-the-escalation-floor"),

    FailureClass(
        "an_aggregate_is_not_a_finding",
        "A control reports a single reconciled TOTAL. The total is arithmetic, "
        "so it is always correct and never closable: it is the sum of several "
        "unrelated defects, and no owner can fix a sum. It re-fires every run "
        "with a slightly different number, which reads as noise rather than as "
        "several standing bugs.",
        "reconciliation_check reported LEDGER_DRIFT -6.3% for weeks. Behind the "
        "one number: (a) pod_ledger.update_prices(), documented as a daily "
        "mark-to-market, had NO CALLER anywhere in the firm, so ETF-pod "
        "positions still carried current_price == avg_entry months after "
        "opening; (b) 6 of the broker's 21 positions belonged to no pod; (c) a "
        "$10,000 book reserve had no counterpart at the broker. The fix "
        "decomposes the residual into individually closable named causes and "
        "deliberately does NOT scale the total to force agreement — a "
        "reconciliation that forces agreement destroys the evidence of the "
        "thing it was built to find.",
        ["pod_ledger.py::reconcile_to_broker",
         "pod_ledger.py::_drift_causes",
         "test_pipeline_smoke.py::TestPodLedgerAttributionHasNamedCauses"],
        "Pod Ledger / reconciliation_check",
        "verdict-with-no-consequence"),

    FailureClass(
        "an_entitlement_counted_as_an_asset",
        "A book quantity that represents PERMISSION TO SPEND is summed with "
        "quantities that represent things actually owned. The sum is "
        "arithmetically valid, has a plausible magnitude, and describes nothing "
        "that exists — so no downstream check can tell it is wrong.",
        "pod_ledger gave each of three pods its own `cash_balance` and defined "
        "`total_value = cash_balance + positions_value`. There is ONE cash "
        "balance and it is at the broker: pods are an ATTRIBUTION of firm "
        "equity, not custody accounts. Pod cash summed to $40,998 against "
        "$13,262 real, and pod totals plus a $10,000 reserve came to $104,646 "
        "against $98,965 of equity. Six modules sized, forecast and graded "
        "against that number. Fixed 2026-08-19: `total_value` returns None "
        "rather than a corrected number, so a reader that still wants it fails "
        "instead of quietly receiving something plausible; `deployed_value` "
        "(real, from the broker) and `budget_remaining` (an entitlement) are "
        "two numbers that must not be added, and an AST test asserts nothing "
        "adds them.",
        ["pod_ledger.py::get_firm_summary",
         "pod_ledger.py::sync_from_broker",
         "test_pipeline_smoke.py::TestAPodBudgetIsNotMoney"],
        "Pod Ledger / CFO",
        "an-aggregate-is-not-a-finding"),

    FailureClass(
        "recorded_by_whoever_remembered_to",
        "A book that only learns about an event when a caller volunteers it. "
        "Every caller that does record looks correct, every caller that does "
        "not looks like ordinary code, and the book's coverage is invisible "
        "because it has no denominator.",
        "pod_ledger.record_trade was called from 2 modules / 4 call sites while "
        "15 modules reached TradingClient.submit_order. On 2026-08-19 the pod "
        "books and the broker account intersected on exactly ONE symbol out of "
        "33: 13 pod claims the broker did not hold, 19 broker positions no pod "
        "owned. Same shape as the risk limits living in submit_buy. Fixed by "
        "claiming the symbol at the order_ledger choke point every path already "
        "passes through, with undeclared callers recorded as `unattributed` "
        "WITH their submitting module — an absence you can see is a finding, an "
        "absence you cannot see is what this ledger had been for three months.",
        ["pod_ledger.py::claim",
         "pod_ledger.py::attributing",
         "order_ledger.py::install",
         "test_pipeline_smoke.py::TestAttributionSitsAtTheChokePoint"],
        "Pod Ledger / order_ledger",
        "guardrail-on-one-path"),

    FailureClass(
        "a_proxy_that_binds_hardest_on_the_cure",
        "A limit written on a PROXY for the quantity it means to bound. The "
        "proxy tracks the real quantity in ordinary conditions and comes apart "
        "at the boundary — so the rule is loosest when nothing is wrong and "
        "tightest against the one action that would relieve it. The tell is a "
        "rule people rewrite whenever it binds: that is not a rule, it is a "
        "suggestion with extra steps.",
        "risk_gate enforced a 20% cash floor and a 20-position cap. Both are "
        "proxies for risk capacity. On 2026-08-19 the firm was 91% gross "
        "invested at 20/20 positions with cash at 13.4% — maximum risk by both "
        "proxies — while measured NET BETA was 0.6439 at 100% coverage, well "
        "inside appetite. The gate refused the SH buy, the single order that "
        "reduced the exposure the limits exist to bound. Fixed 2026-08-19 by "
        "measuring the quantity itself (portfolio_beta) and carving out only "
        "structural-mandate hedges that measurably reduce net beta, never past "
        "flat, never below a separately derived hard liquidity floor, capped at "
        "MAX_HEDGE_SLOTS, and escalated to the Chairman on every use. The "
        "carve-out keys on the instrument's MANDATE, not a measured sign: VZ "
        "measured beta -0.3027 the same day and is a telecom long.",
        ["risk_gate.py::_hedge_verdict",
         "risk_gate.py::_escalate_exemption",
         "portfolio_beta.py::beta_effect_of",
         "portfolio_beta.py::is_structural_hedge",
         "test_pipeline_smoke.py::TestTheHedgeCarveOutIsNarrow",
         "test_pipeline_smoke.py::TestNetBetaIsMeasuredNotAssumed"],
        "CRO / Risk Gate",
        "guardrail-on-one-path"),
]


def _symbol_exists(ref: str) -> Optional[bool]:
    """Does `file.py::symbol` still exist? None if the file is unparseable.

    AST, deliberately. A grep for the symbol name would match the postmortem
    comments that name every one of these controls — the same trap that made
    `_emitted_verbs` use AST instead of a text search. Comments are never AST
    nodes.
    """
    try:
        fname, symbol = ref.split("::", 1)
    except ValueError:
        return False
    path = BASE_DIR / fname
    if not path.exists():
        return False
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, OSError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
           and node.name == symbol:
            return True
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == symbol:
                    return True
    return False


def audit_census() -> List[dict]:
    """Resolve every named detector. COVERED / UNCOVERED / BROKEN per class."""
    out = []
    for fc in CONTROL_CENSUS:
        missing = [d for d in fc.detectors if _symbol_exists(d) is not True]
        if not fc.detectors:
            verdict, detail = UNCOVERED, ("no detector named — this failure class has "
                                          "happened and nothing watches for it")
        elif len(missing) == len(fc.detectors):
            verdict, detail = BROKEN, (
                f"every named detector is gone: {', '.join(missing)}. The census "
                f"outlived the code, which is the one way a coverage map does more "
                f"harm than none")
        elif missing:
            verdict, detail = COVERED, (
                f"{len(fc.detectors) - len(missing)} of {len(fc.detectors)} detectors "
                f"present; MISSING: {', '.join(missing)}")
        else:
            verdict, detail = COVERED, f"{len(fc.detectors)} detector(s) present"
        out.append({"class": fc.id, "verdict": verdict, "detail": detail,
                    "owner": fc.owner, "what": fc.what, "memory": fc.memory,
                    "detectors": list(fc.detectors), "missing": missing})
    return out


# =============================================================================
# report / escalation / render
# =============================================================================

def run_audit() -> dict:
    falsify = prove_can_fail()
    census = audit_census()
    denom = audit_denominators()
    report = {
        "checked_at": datetime.now().isoformat(),
        "falsifiability": falsify,
        "census": census,
        "denominators": denom,
        "blind": [f["control"] for f in falsify if f["verdict"] == BLIND],
        "unproven": [f["control"] for f in falsify
                     if f["verdict"] in (UNPROVEN, INCONCLUSIVE)],
        "uncovered": [c["class"] for c in census
                      if c["verdict"] in (UNCOVERED, BROKEN)],
        "empty_success": [d["control"] for d in denom
                          if d["verdict"] == "EMPTY_SUCCESS"],
    }
    try:
        DATA_DIR.mkdir(exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=1))
    except OSError as e:
        logger.warning("could not write %s: %s", REPORT.name, e)
    return report


CHAIRMAN_QUEUE = DATA_DIR / "chairman_action_required.json"


def escalate(report: dict) -> int:
    """BLIND controls go to the Chairman; everything else goes to CIT.

    The split is deliberate. A BLIND control is an emergency with no workaround —
    a whole class of bug is currently unwatched and the firm believes otherwise —
    so it goes in front of the Chairman, ONE row per control, keyed and retired
    the way `coherence_laws.escalate_violations` does it. That queue already
    holds 110 items; a detector that appends is a detector that makes the queue
    unreadable, and an unreadable queue is how a CRITICAL sits for 50 days.

    Everything else is ordinary engineering backlog and goes to CIT as a
    proposal — which is the point of the whole module. The gap CIT could not see
    is now a row CIT can read.
    """
    written = 0
    blind = {f"internal-audit-blind-{f['control']}": f
             for f in report["falsifiability"] if f["verdict"] == BLIND}
    mine_prefix = "internal-audit-blind-"
    try:
        rows = json.loads(CHAIRMAN_QUEUE.read_text()) if CHAIRMAN_QUEUE.exists() else []
        if not isinstance(rows, list):
            rows = None
    except (ValueError, OSError):
        rows = None

    if rows is not None:
        kept = []
        for row in rows:
            rid = row.get("id") if isinstance(row, dict) else None
            if rid in blind:
                f = blind.pop(rid)
                row["description"] = (f"CONTROL IS BLIND — {f['control']}: {f['detail']}")
                row["last_seen"] = datetime.now().isoformat()
                kept.append(row)
                written += 1
            elif isinstance(rid, str) and rid.startswith(mine_prefix):
                continue          # proven since: the row is retired, not left to rot
            else:
                kept.append(row)
        for rid, f in sorted(blind.items()):
            kept.append({
                "id": rid, "severity": "CRITICAL",
                "description": (f"CONTROL IS BLIND — {f['control']} passed an input "
                                f"built to violate it ({f.get('pill')}). This bug "
                                f"class is currently unwatched."),
                "source": "internal_audit", "ts": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat()})
            written += 1
        try:
            CHAIRMAN_QUEUE.write_text(json.dumps(kept, indent=1))
        except OSError:
            pass

    proposals = []
    for f in report["falsifiability"]:
        if f["verdict"] == UNPROVEN:
            proposals.append({
                "source": "internal_audit", "severity": "MEDIUM", "category": "INTEGRATION",
                "title": f"Coherence law '{f['control']}' has no falsifiability proof",
                "detail": f["detail"],
                "proposed_fix": f"Add a poison pill for {f['control']} to "
                                f"internal_audit_agent.POISON_PILLS — a synthetic input "
                                f"the law MUST return VIOLATED on."})
        elif f["verdict"] == INCONCLUSIVE:
            proposals.append({
                "source": "internal_audit", "severity": "LOW", "category": "INTEGRATION",
                "title": f"Coherence law '{f['control']}' could not be proven falsifiable",
                "detail": f["detail"],
                "proposed_fix": "Give the law an injection point for both sources so a "
                                "pill can reach its comparison without patching globals."})
        elif f["verdict"] == PROVEN and f.get("injected") is False:
            proposals.append({
                "source": "internal_audit", "severity": "LOW", "category": "INTEGRATION",
                "title": f"Coherence law '{f['control']}' is proven only by monkeypatch",
                "detail": "The pill had to substitute module globals; nothing shows the "
                          "law is still pointed at the sources production writes.",
                "proposed_fix": "Add parameters for both sources, defaulting to the real "
                                "reads, and flip the pill to injected=True."})

    for c in report["census"]:
        if c["verdict"] == UNCOVERED:
            proposals.append({
                "source": "internal_audit", "severity": "HIGH", "category": "INTEGRATION",
                "title": f"Failure class '{c['class']}' has no detector",
                "detail": f"{c['what']} Owner: {c['owner']}.",
                "proposed_fix": "Build a detector, or record in CONTROL_CENSUS why this "
                                "class is accepted unwatched."})
        elif c["verdict"] == BROKEN:
            proposals.append({
                "source": "internal_audit", "severity": "HIGH", "category": "BUG_FIX",
                "title": f"Failure class '{c['class']}' names detectors that no longer exist",
                "detail": c["detail"],
                "proposed_fix": "Restore the detector or update CONTROL_CENSUS. A census "
                                "that names deleted code reports coverage it does not have."})

    for d in report["denominators"]:
        if d["verdict"] in ("EMPTY_SUCCESS", "NO_DENOMINATOR"):
            proposals.append({
                "source": "internal_audit", "severity": "MEDIUM", "category": "LOGIC_ERROR",
                "title": f"Control '{d['control']}' reports success on nothing "
                         f"({d['verdict']})",
                "detail": d["detail"],
                "proposed_fix": "Set n_examined on the HOLDS path, and return CANNOT_TELL "
                                "rather than HOLDS when there was nothing to examine."})

    if proposals:
        try:
            import cit_director
            for p in proposals:
                cit_director.submit_to_pod_queue(p)
                written += 1
        except Exception as e:
            logger.warning("could not queue internal-audit proposals: %s", e)
    return written


def render_controls_md(report: Optional[dict] = None) -> str:
    """The census as a document Jack can read, generated from the code.

    Generated, never hand-maintained: a coverage map that can drift from the
    detectors it names is the exact failure this module exists to catch, so the
    census lives in CONTROL_CENSUS and this only renders it. A smoke test
    asserts the committed file is byte-identical to this output.
    """
    lines = [
        "# Apex Capital — Control Census",
        "",
        "**Generated by `internal_audit_agent.py --controls-md`. Do not hand-edit —",
        "the source of truth is `CONTROL_CENSUS` in that module, and a smoke test",
        "fails if this file drifts from it.**",
        "",
        "Every row is a way this firm has *actually* been hurt, and the control that",
        "now watches for it. A row with no detector is not a gap we are unaware of —",
        "it is a gap in writing, with an owner.",
        "",
        "This list is a ratchet, not a proof of completeness: it records what has",
        "bitten us. When something new escapes, the ritual is to add the row.",
        "",
    ]
    for fc in CONTROL_CENSUS:
        lines.append(f"## `{fc.id}`")
        lines.append("")
        lines.append(fc.what)
        lines.append("")
        lines.append(f"- **Proved real by:** {fc.proved_by}")
        lines.append(f"- **Owner:** {fc.owner}")
        if fc.memory:
            lines.append(f"- **Writeup:** memory `{fc.memory}`")
        lines.append("- **Detectors:**")
        for d in fc.detectors:
            lines.append(f"  - `{d}`")
        lines.append("")
    return "\n".join(lines)


def _print(report: dict) -> None:
    print("=" * 74)
    print("INTERNAL AUDIT — the third line: does the checking work?")
    print("=" * 74)
    print("\nFALSIFIABILITY — can each control actually fail?")
    for f in report["falsifiability"]:
        mark = {PROVEN: "OK ", BLIND: "!! ", INCONCLUSIVE: "?  ", UNPROVEN: "?  "}[
            f["verdict"]]
        note = "" if f.get("injected") is not False else "  [monkeypatch only]"
        print(f"  {mark}{f['verdict']:13} {f['control']}{note}")
        if f["verdict"] != PROVEN:
            print(f"       {f['detail'][:150]}")

    print("\nDENOMINATOR — is any control passing on zero examined items?")
    if not report["denominators"]:
        print("  OK  every HOLDS carries a non-zero denominator")
    for d in report["denominators"]:
        print(f"  !!  {d['verdict']:15} {d['control']}: {d['detail'][:110]}")

    print("\nCENSUS — failure classes we have suffered, and who watches them")
    for c in report["census"]:
        mark = "OK " if c["verdict"] == COVERED and not c["missing"] else "!! "
        print(f"  {mark}{c['verdict']:10} {c['class']:32} {c['owner']}")
        if c["missing"] or c["verdict"] != COVERED:
            print(f"       {c['detail'][:150]}")

    print("-" * 74)
    print(f"  {len(report['blind'])} blind, {len(report['unproven'])} unproven, "
          f"{len(report['uncovered'])} uncovered class(es), "
          f"{len(report['empty_success'])} empty success(es)")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Audit the firm's controls, not the firm.")
    ap.add_argument("--report", action="store_true", help="run the audit (default)")
    ap.add_argument("--controls-md", action="store_true",
                    help="render the control census as markdown to stdout")
    ap.add_argument("--write-md", action="store_true",
                    help="write CONTROLS.md from the census")
    ap.add_argument("--no-escalate", action="store_true",
                    help="print findings without queueing them")
    args = ap.parse_args()

    if args.controls_md:
        print(render_controls_md())
        return 0
    if args.write_md:
        CONTROLS_MD.write_text(render_controls_md())
        print(f"wrote {CONTROLS_MD}")
        return 0

    report = run_audit()
    _print(report)
    if not args.no_escalate:
        n = escalate(report)
        if n:
            print(f"  escalated {n} item(s) — BLIND to the Chairman, the rest to CIT")
    clean = not (report["blind"] or report["unproven"] or report["uncovered"]
                 or report["empty_success"])
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())

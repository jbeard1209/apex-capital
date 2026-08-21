#!/usr/bin/env python3
"""
APEX CAPITAL — DEPUTY BOARD
============================
Lightweight technical review body that handles BUG_FIX and LOGIC_ERROR
proposals. Meets whenever CIT routes a bug fix that doesn't need full
board attention.

Separation of concerns
-----------------------
  Main Board      → strategy, architecture, investment policy, major improvements
  Deputy Board    → bug fixes, logic errors, code corrections, data pipeline gaps

How it works
------------
  1. Reads pending BUG_FIX proposals from data/cit_pending_proposals.json
  2. Each deputy agent votes independently (APPROVE / REJECT / DEFER)
  3. Majority APPROVE → fix is written to data/cos_meeting_requests.json
     for the cit-code-bug-fixer scheduled task to implement
  4. Results written to data/deputy_board_report.json
  5. Outcome posted to firm channel

Deputy voters (7 agents, majority = 4+):
  • Deputy Manager          — execution safety lens
  • Deputy CRO              — risk/compliance lens
  • Deputy CIT Director     — code quality lens
  • Deputy Chief of Staff   — process/priority lens
  • Deputy IRM              — governance/oversight lens
  • Deputy R&D Leader       — research pipeline lens
  • Deputy Operations Dir   — floor operations lens (voice of floor agents)

Deputies vote only on BUG_FIX / LOGIC_ERROR / INTEGRATION categories.
IMPROVEMENT, STRATEGY, ARCHITECTURE always go to the full board.

Schedule
--------
  Called from cit_director.py after each session when BUG_FIX proposals exist.
  Also callable directly: python3 deputy_board.py
"""

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("deputy_board")

PENDING_PATH       = "data/cit_pending_proposals.json"
APPROVED_PATH      = "data/cit_approved_proposals.json"
REPORT_PATH        = "data/deputy_board_report.json"
CMR_PATH           = "data/cos_meeting_requests.json"
BOARD_ACTIVE_PATH  = "data/board_meeting_active.json"

# ── Proposal routing tiers (Chairman directive 2026-06-09) ─────────────────
#
# Tier 0 — CIT Implementation Agent (auto, no meeting needed):
#   BUG_FIX / LOGIC_ERROR / INTEGRATION  severity=LOW   confidence>=70
#   These are safe, well-scoped, low-risk code fixes. No vote needed.
#
# Tier 1 — Deputy Board (this file):
#   BUG_FIX / LOGIC_ERROR / INTEGRATION  severity=MEDIUM/HIGH
#   IMPROVEMENT / VISION                 severity=LOW/MEDIUM
#   Deputies vote (4/7 majority). Approved proposals → CIT Implementation Agent.
#
# Tier 2 — Full Board:
#   IMPROVEMENT / VISION                 severity=HIGH/CRITICAL
#   STRATEGY / ARCHITECTURE              any severity
#   Anything the deputy board DEFERs or cannot reach consensus on.

# Categories the deputy board handles (extended from original BUG_FIX-only)
DEPUTY_CATEGORIES = {
    "BUG_FIX", "LOGIC_ERROR", "INTEGRATION",   # original scope
    "IMPROVEMENT", "VISION",                    # NEW: mid-level ideas filtered here
}

# Categories that ALWAYS escalate to the full board regardless of severity
FULL_BOARD_ONLY_CATEGORIES = {"STRATEGY", "ARCHITECTURE"}

# Auto-implement without a vote: simple bugs, low severity, high confidence
AUTO_IMPLEMENT_CATEGORIES  = {"BUG_FIX", "LOGIC_ERROR", "INTEGRATION"}
AUTO_IMPLEMENT_MAX_SEVERITY = "LOW"
AUTO_IMPLEMENT_MIN_CONFIDENCE = 70

# Minimum votes to approve at deputy level (out of 7 deputies)
APPROVAL_THRESHOLD = 4

# Path for CIT clarification requests
CLARIFY_PATH = "data/cit_clarification_requests.json"

# After this many clarification rounds with no approval, escalate to Chairman
MAX_CLARIFICATION_ROUNDS = 3


def _load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _load_json_dict(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _is_board_meeting_active() -> bool:
    data = _load_json_dict(BOARD_ACTIVE_PATH)
    if not data.get("active"):
        return False
    try:
        started = datetime.fromisoformat(data.get("started_at", ""))
        return (datetime.now() - started).total_seconds() / 3600 < 3
    except Exception:
        return False


# =============================================================================
# EVIDENCE + FIX QUALITY HELPERS
# =============================================================================

import re as _re

_PRECISE_RE = _re.compile(
    r'(\w+\.py:\d+'           # file:line  e.g. main.py:42
    r'|data/[\w/]+\.json'     # data file  e.g. data/state.json
    r'|\d+\s*h\s+old'        # file age   e.g. 48h old
    r'|\d+\s+ERROR'          # log count  e.g. 142 ERROR
    r'|\d+\s+WARNING'
    r'|\$[\d,]+'             # dollar amount
    r'|\d+\.\d+%'            # percentage with decimal
    r'|\bline\s+\d+\b'       # "line 42"
    r'|[A-Z_]{4,}\(\)'       # CONSTANT() — uppercase function call
    r'|\bdef\s+\w+'          # def function_name
    r'|\bclass\s+\w+)',      # class ClassName
    _re.IGNORECASE,
)

_CODE_REF_RE = _re.compile(
    r'\w+\.py:\d+|\bdef\s+\w+|\bclass\s+\w+|data/\w+\.json',
    _re.IGNORECASE,
)


def _evidence_is_precise(proposal: dict) -> bool:
    """Return True if evidence contains a specific, verifiable reference."""
    ev = proposal.get("evidence", "")
    return bool(_PRECISE_RE.search(ev))


def _fix_is_specific(proposal: dict) -> bool:
    """
    Return True if proposed_fix is specific enough to implement.
    Requires a code/file reference OR a sufficiently detailed description (>80 chars
    that contains a filename or function name).
    """
    fix = proposal.get("proposed_fix", "").strip()
    if not fix or len(fix) < 20:
        return False
    # Explicit code/file reference: best case
    if bool(_CODE_REF_RE.search(fix)):
        return True
    # Shell command or directive with substance
    directive_start = ("chmod", "pip ", "run ", "python", "Add ", "Replace ",
                       "Use ", "Move ", "Wrap ", "Insert ", "Update ", "Change ",
                       "In ", "At ", "Check ", "Create ")
    if any(fix.startswith(d) for d in directive_start) and len(fix) > 40:
        return True
    return False


def _request_clarification_batch(proposal: dict, questions: list):
    """
    Write all clarification questions for a proposal in a single file write.
    'questions' is a list of (deputy_name, question_text) tuples.
    Also increments clarification_rounds on the proposal in cit_pending.json.
    """
    existing = _load_json(CLARIFY_PATH) or []
    if not isinstance(existing, list):
        existing = []

    now_iso = datetime.now().isoformat()
    batch_round = proposal.get("clarification_rounds", 0) + 1

    for deputy_name, question in questions:
        existing.append({
            "id":               "CLR-" + uuid.uuid4().hex[:6].upper(),
            "ts":               now_iso,
            "proposal_id":      proposal.get("id", "?"),
            "proposal_title":   proposal.get("title", "")[:80],
            "requesting_deputy": deputy_name,
            "source":           "deputy_board",
            "round":            batch_round,
            "question":         question,
            "status":           "PENDING",
        })

    os.makedirs("data", exist_ok=True)
    with open(CLARIFY_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    # Increment round counter on the proposal so escalation counts by batch, not question
    try:
        pending = _load_json(PENDING_PATH) or []
        if isinstance(pending, list):
            for p in pending:
                if p.get("id") == proposal.get("id"):
                    p["clarification_rounds"] = batch_round
                    break
            with open(PENDING_PATH, "w") as f:
                json.dump(pending, f, indent=2, default=str)
    except Exception:
        pass


# =============================================================================
# DEPUTY VOTER AGENTS
# Each returns {"vote": "APPROVE"|"REJECT"|"DEFER"|"CLARIFY", "rationale": str}
# CLARIFY = needs more info from CIT Director before we can approve
# =============================================================================

class _DeputyManagerVoter:
    name = "Deputy Manager"

    def vote(self, proposal: dict) -> dict:
        sev   = proposal.get("severity", "MEDIUM")
        cat   = proposal.get("category", "")
        fix   = proposal.get("proposed_fix", "")
        ev    = proposal.get("evidence", "")
        conf  = proposal.get("confidence", 50)

        # Hard block: fix touches live trading order submission without safety guard
        risky_keywords = ["submit_buy", "submit_sell", "MarketOrderRequest", "alpaca_trader"]
        is_risky = any(k in fix for k in risky_keywords)
        if is_risky and "guard" not in fix.lower() and "check" not in fix.lower():
            return {"vote": "DEFER",
                    "rationale": "Fix touches live trading code without an explicit safety guard — this must go to the full board with a risk review."}

        # Require actual evidence before approving anything HIGH+
        if sev in ("HIGH", "CRITICAL") and not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": f"Severity is {sev} but evidence is vague — no file:line, metric, or data path cited. CIT Director: provide a specific evidence reference before this can be approved."}

        # Require a real fix description for anything we're about to implement
        if not _fix_is_specific(proposal):
            return {"vote": "CLARIFY",
                    "rationale": f"Proposed fix is not specific enough to implement safely: '{fix[:80]}'. CIT Director: rewrite the fix with exact function names, file paths, or code changes."}

        if cat in DEPUTY_CATEGORIES and sev in ("CRITICAL", "HIGH", "MEDIUM") and conf >= 65:
            return {"vote": "APPROVE",
                    "rationale": f"[{sev}/{conf}%] Evidence is specific, fix is actionable, no trading risk — approve."}

        if conf < 60:
            return {"vote": "DEFER",
                    "rationale": f"Confidence {conf}% is below 60% threshold for deputy approval — route to full board or raise confidence before re-submitting."}

        return {"vote": "APPROVE", "rationale": f"Standard {cat} bug, evidence verified, safe to implement."}


class _DeputyCROVoter:
    name = "Deputy CRO"

    def vote(self, proposal: dict) -> dict:
        title = proposal.get("title", "").lower()
        fix   = proposal.get("proposed_fix", "").lower()
        sev   = proposal.get("severity", "MEDIUM")
        ev    = proposal.get("evidence", "")

        risk_keywords = ["stop.loss", "stop_loss", "cash.floor", "position.size", "position_size",
                         "guardrail", "cro", "risk_bulletin", "cvar", "drawdown", "max_position"]
        touches_risk = any(k in title or k in fix for k in risk_keywords)

        if touches_risk and sev == "CRITICAL":
            if not _fix_is_specific(proposal):
                return {"vote": "CLARIFY",
                        "rationale": "CRITICAL risk control fix but proposed change is vague. Need exact code reference before approving."}
            return {"vote": "APPROVE",
                    "rationale": "CRITICAL risk control bug with specific fix — must be addressed immediately."}

        if touches_risk:
            return {"vote": "DEFER",
                    "rationale": "Fix touches risk controls — this requires the full board and CRO principal sign-off, not just deputies."}

        # Non-risk proposals: check evidence quality
        if not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "No verifiable evidence cited (no file:line, metric, or data path). CIT Director must provide specific evidence before CRO can approve."}

        return {"vote": "APPROVE", "rationale": "No risk control exposure — evidence checks out."}


class _DeputyCITVoter:
    name = "Deputy CIT Director"

    def vote(self, proposal: dict) -> dict:
        cat   = proposal.get("category", "")
        conf  = proposal.get("confidence", 50)
        fix   = proposal.get("proposed_fix", "")
        title = proposal.get("title", "")
        ev    = proposal.get("evidence", "")

        if conf < 60:
            return {"vote": "REJECT",
                    "rationale": f"Confidence {conf}% is below the 60% floor for deputy approval. Raise evidence quality or demote to backlog."}

        if not fix or len(fix.strip()) < 20:
            return {"vote": "REJECT",
                    "rationale": "Proposed fix is absent or trivially short — cannot implement without guessing. CIT must provide a complete fix description."}

        if not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": f"Evidence for '{title[:60]}' contains no verifiable reference (no file:line, metric, data path). CIT Director: add a specific citation."}

        if not _fix_is_specific(proposal):
            return {"vote": "CLARIFY",
                    "rationale": f"Fix description reads like a general suggestion rather than actionable code change. Provide a specific function name, file, or code snippet."}

        if cat not in DEPUTY_CATEGORIES:
            return {"vote": "DEFER",
                    "rationale": f"Category '{cat}' is outside deputy scope — route to the full board."}

        return {"vote": "APPROVE",
                "rationale": f"[{conf}%] Evidence precise, fix specific, category in scope — CIT Director approves for implementation."}


class _DeputyCoSVoter:
    name = "Deputy Chief of Staff"

    def vote(self, proposal: dict) -> dict:
        sev   = proposal.get("severity", "MEDIUM")
        cat   = proposal.get("category", "")
        conf  = proposal.get("confidence", 50)
        fix   = proposal.get("proposed_fix", "")
        ev    = proposal.get("evidence", "")

        # CoS cares about priority and pipeline impact — but still demands evidence
        if sev == "CRITICAL":
            if not _fix_is_specific(proposal):
                return {"vote": "CLARIFY",
                        "rationale": "CRITICAL fix is vague — implementer won't know what to change. Need exact code reference."}
            return {"vote": "APPROVE",
                    "rationale": "CRITICAL severity with specific fix — CoS approves immediate action."}

        if sev == "LOW" and cat != "SECURITY":
            return {"vote": "DEFER",
                    "rationale": "LOW severity — acceptable to batch with next scheduled CIT cycle rather than acting now."}

        if not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "CoS cannot prioritise a fix with no specific evidence. Provide file:line or a data metric."}

        if sev == "HIGH" and cat in DEPUTY_CATEGORIES and conf >= 65:
            return {"vote": "APPROVE",
                    "rationale": "HIGH priority with verified evidence — within deputy mandate, CoS supports."}

        if conf < 60:
            return {"vote": "DEFER",
                    "rationale": f"Confidence {conf}% is marginal — CoS defers to full board rather than rushing a low-confidence fix."}

        return {"vote": "APPROVE",
                "rationale": "Evidence and priority both check out — CoS approves."}


class _DeputyIRMVoter:
    name = "Deputy IRM"

    def vote(self, proposal: dict) -> dict:
        title = proposal.get("title", "").lower()
        fix   = proposal.get("proposed_fix", "").lower()
        ev    = proposal.get("evidence", "")
        sev   = proposal.get("severity", "MEDIUM")
        conf  = proposal.get("confidence", 50)

        governance_keywords = ["irm", "board", "vote", "consensus", "sealed", "governance",
                                "oversight", "audit", "accountability", "report", "firm_channel"]
        touches_governance = any(k in title or k in fix for k in governance_keywords)

        if touches_governance and sev in ("CRITICAL", "HIGH"):
            if not _fix_is_specific(proposal):
                return {"vote": "CLARIFY",
                        "rationale": "Governance-related HIGH/CRITICAL fix lacks specificity. IRM needs exact change before signing off."}
            return {"vote": "APPROVE",
                    "rationale": "Governance integrity bug with specific fix — IRM supports immediate resolution."}

        if touches_governance:
            return {"vote": "DEFER",
                    "rationale": "Change touches governance/oversight mechanisms — must have IRM principal review, not just deputy approval."}

        # Non-governance: IRM checks that evidence is verifiable and fix doesn't
        # introduce a new accountability gap
        if not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "IRM requires a specific, verifiable evidence reference before approving any code change. Vague evidence makes post-implementation audit impossible."}

        if "pass" in fix and "logger" not in fix and "log" not in fix:
            return {"vote": "CLARIFY",
                    "rationale": "Fix appears to add a silent pass block — IRM requires logging added alongside any exception swallowing so failures are traceable."}

        return {"vote": "APPROVE",
                "rationale": "No governance exposure, evidence is verifiable, fix doesn't introduce accountability gap."}


class _DeputyRDVoter:
    name = "Deputy R&D Leader"

    def vote(self, proposal: dict) -> dict:
        title = proposal.get("title", "").lower()
        fix   = proposal.get("proposed_fix", "").lower()
        ev    = proposal.get("evidence", "")
        conf  = proposal.get("confidence", 50)
        sev   = proposal.get("severity", "MEDIUM")

        research_keywords = ["research", "signal", "sector", "rd_team", "conviction", "backtest",
                              "feature", "ml_model", "predictor", "label_builder"]
        touches_research = any(k in title or k in fix for k in research_keywords)

        # Research/signal changes need a backtest or data reference
        if touches_research and sev in ("HIGH", "CRITICAL") and not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "Change touches research/signal pipeline — R&D requires a data-backed evidence reference (backtest result, metric, or file:line) before approving."}

        if not _fix_is_specific(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "Fix description is too vague for R&D to assess signal quality impact. Need specific function/file reference."}

        if touches_research:
            return {"vote": "APPROVE",
                    "rationale": f"Research pipeline bug with specific evidence [{conf}%] — fixing will improve signal quality."}

        return {"vote": "APPROVE",
                "rationale": "No research pipeline exposure — fix is specific and evidence is present."}


class _DeputyOpsDirVoter:
    name = "Deputy Operations Director"

    def vote(self, proposal: dict) -> dict:
        title = proposal.get("title", "").lower()
        fix   = proposal.get("proposed_fix", "").lower()
        ev    = proposal.get("evidence", "")
        conf  = proposal.get("confidence", 50)

        infra_keywords = ["cron", "schedule", "log", "file", "path", "missing output", "orphan",
                          "stale", "backup", "deploy", "gcp", "nohup"]
        is_infra = any(k in title or k in fix for k in infra_keywords)

        if not _evidence_is_precise(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "Operations cannot verify or assign this fix without a specific file path, log reference, or metric. CIT Director: add a concrete evidence reference."}

        if not _fix_is_specific(proposal):
            return {"vote": "CLARIFY",
                    "rationale": "Fix is not actionable — no specific command, file, or function identified. Operations needs exact steps to implement."}

        if is_infra:
            return {"vote": "APPROVE",
                    "rationale": f"Infrastructure/pipeline fix with verified evidence [{conf}%] — Operations Director approves."}

        return {"vote": "APPROVE",
                "rationale": "Evidence present, fix actionable, no operational concerns."}


DEPUTY_VOTERS = [
    _DeputyManagerVoter(),
    _DeputyCROVoter(),
    _DeputyCITVoter(),
    _DeputyCoSVoter(),
    _DeputyIRMVoter(),
    _DeputyRDVoter(),
    _DeputyOpsDirVoter(),
]


# =============================================================================
# VOTE + ROUTE
# =============================================================================

def _inject_clarification_context(proposal: dict) -> dict:
    """
    If this proposal has been through a clarification round, prepend the CIT
    Director's answers into the evidence and proposed_fix fields so deputies
    see the full Q&A when re-voting. Returns a shallow copy — orignal unchanged.
    """
    log = proposal.get("clarification_log", [])
    if not log:
        return proposal

    p = dict(proposal)  # shallow copy

    qa_block = "\n--- CIT Director Clarification Responses ---\n"
    for entry in log:
        qa_block += (
            f"[{entry.get('deputy','?')}] Q: {entry.get('question','?')[:150]}\n"
            f"  A: {entry.get('answer','?')[:300]}\n"
        )

    p["evidence"]      = qa_block + "\n" + p.get("evidence", "")
    p["proposed_fix"]  = p.get("proposed_fix", "") + "\n" + qa_block
    return p


def _vote_on_proposal(proposal: dict) -> dict:
    votes     = []
    approves  = 0
    rejects   = 0
    defers    = 0
    clarifies = 0

    # If this is a re-vote, inject the CIT Director's clarification answers
    # so deputies can see the full Q&A when making their decision
    proposal = _inject_clarification_context(proposal)

    for voter in DEPUTY_VOTERS:
        result = voter.vote(proposal)
        v = result["vote"]
        votes.append({"deputy": voter.name, "vote": v, "rationale": result["rationale"]})
        if v == "APPROVE":   approves  += 1
        elif v == "REJECT":  rejects   += 1
        elif v == "CLARIFY": clarifies += 1
        else:                defers    += 1

    # Any CLARIFY or REJECT prevents approval — proposal is held for CIT response
    passed = approves >= APPROVAL_THRESHOLD and rejects == 0 and clarifies == 0

    # ── Fable 5 deputy tie-breaker — gated to split votes only ────────────
    # When deputies disagree (some approve, some reject/defer), an LLM read
    # of the proposal often resolves what keyword rules can't.
    if not passed and approves >= 1 and clarifies == 0:
        try:
            from apex_llm import reason as _r, is_available
            if is_available("deputy_board"):
                import json as _j
                raw = _r(
                    "You are a senior deputy on Apex Capital's operations board. "
                    'Reply JSON only: {"vote": "APPROVE"|"REJECT", "reason": "one sentence"}',
                    f"Split vote ({approves} approve, {rejects} reject, {defers} defer) on:\n"
                    f"[{proposal.get('severity','?')}|{proposal.get('category','?')}] "
                    f"{proposal.get('title','')}\n"
                    f"{proposal.get('description','')[:250]}\n"
                    f"Fix: {proposal.get('proposed_fix','')[:200]}",
                    agent_name="deputy_board", max_tokens=100,
                )
                if raw:
                    p = _j.loads(raw.strip())
                    llm_v = p.get("vote", "").upper()
                    votes.append({"deputy": "LLM Tie-Breaker", "vote": llm_v,
                                  "rationale": p.get("reason", "")})
                    if llm_v == "APPROVE" and rejects == 0:
                        approves += 1
                        passed = approves >= APPROVAL_THRESHOLD
        except Exception:
            pass

    # Write ALL clarification questions at once so CIT Director gets them simultaneously
    if clarifies > 0:
        all_questions = [
            (v["deputy"], v["rationale"])
            for v in votes if v["vote"] == "CLARIFY"
        ]
        _request_clarification_batch(proposal, all_questions)

    classification = (
        "UNANIMOUS"    if approves == len(DEPUTY_VOTERS)
        else "MAJORITY"    if passed
        else "NEEDS_INFO"  if clarifies > 0 and rejects == 0
        else "REJECTED"    if rejects > 0
        else "DEFERRED"
    )

    return {
        "proposal_id":    proposal.get("id", "?"),
        "title":          proposal.get("title", ""),
        "severity":       proposal.get("severity", ""),
        "category":       proposal.get("category", ""),
        "passed":         passed,
        "classification": classification,
        "approves":       approves,
        "rejects":        rejects,
        "defers":         defers,
        "clarifies":      clarifies,
        "votes":          votes,
    }


CHAIRMAN_ACTION_FILE = "data/chairman_action_required.json"


def _queue_fix(proposal: dict, vote_result: dict):
    """
    Write an approved proposal to two places:
      1. cos_meeting_requests.json  — for any future automated tooling
      2. chairman_action_required.json — so it surfaces at every check-in
         (no auto-fixer exists; Chairman implements approved fixes directly)
    """
    now_iso = datetime.now().isoformat()

    # ── 1. cos_meeting_requests (legacy / future automation) ─────────────
    existing = _load_json(CMR_PATH) or []
    if not isinstance(existing, list):
        existing = []
    urgency_map = {"CRITICAL": "EMERGENCY", "HIGH": "HIGH", "MEDIUM": "NORMAL", "LOW": "NORMAL"}
    existing.append({
        "id":          "CMR-" + uuid.uuid4().hex[:6].upper(),
        "ts":          now_iso,
        "from_agent":  "Deputy Board",
        "summary": (
            f"DEPUTY BOARD APPROVED [{vote_result['classification']}] "
            f"[{proposal.get('severity','?')}] {proposal.get('title','?')} | "
            f"Fix: {proposal.get('proposed_fix','?')[:200]}"
        ),
        "urgency":     urgency_map.get(proposal.get("severity", "MEDIUM"), "NORMAL"),
        "status":      "PENDING",
        "proposal_id": proposal.get("id", ""),
        "evidence":    proposal.get("evidence", ""),
    })
    os.makedirs("data", exist_ok=True)
    with open(CMR_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    # ── 2. chairman_action_required — picked up at every check-in ────────
    # Dedup on a normalized key, not the raw title: chronic findings (CRO
    # paralysis, stale-chairman-actions, etc.) render a growing cycle/day
    # count into the title every run, so exact-title dedup never matches its
    # own prior entry and the queue bloats with near-duplicates of the same
    # unresolved issue. Prefer the upstream dedup_key (stable finding rule)
    # when the proposal carries one; otherwise fall back to a digit-collapsed
    # title so two renders of the same finding still compare equal.
    action_queue = _load_json(CHAIRMAN_ACTION_FILE) or []
    if not isinstance(action_queue, list):
        action_queue = []
    title = str(proposal.get("title", ""))[:80]
    dedup_key = proposal.get("dedup_key") or _re.sub(r"\d+", "#", title)
    existing_keys = {a.get("dedup_key") or _re.sub(r"\d+", "#", str(a.get("title", ""))[:80])
                      for a in action_queue}
    if dedup_key not in existing_keys:
        action_queue.append({
            "id":             proposal.get("id", ""),
            "title":          title,
            "dedup_key":      dedup_key,
            "category":       proposal.get("category", ""),
            "description":    proposal.get("description", ""),
            "proposed_fix":   proposal.get("proposed_fix", ""),
            "evidence":       proposal.get("evidence", ""),
            "confidence":     proposal.get("confidence", 0),
            "severity":       proposal.get("severity", ""),
            "approved_by":    f"Deputy Board ({vote_result['classification']})",
            "status":         "awaiting_chairman",
            "times_approved": 1,
            "queued_at":      now_iso,
        })
        with open(CHAIRMAN_ACTION_FILE, "w") as f:
            json.dump(action_queue, f, indent=2, default=str)


def _mark_approved(proposal: dict):
    """Move proposal from pending to approved list."""
    pending  = _load_json(PENDING_PATH)
    approved = _load_json(APPROVED_PATH)
    if not isinstance(pending, list): pending = []
    if not isinstance(approved, list): approved = []

    pid = proposal.get("id", "")
    pending  = [p for p in pending if p.get("id") != pid]
    proposal["approved_by"] = "Deputy Board"
    proposal["approved_at"] = datetime.now().isoformat()
    approved.append(proposal)

    with open(PENDING_PATH, "w") as f:
        json.dump(pending, f, indent=2, default=str)
    with open(APPROVED_PATH, "w") as f:
        json.dump(approved, f, indent=2, default=str)


# =============================================================================
# CIT IMPLEMENTATION AGENT ROUTER
# =============================================================================

CIT_IMPL_QUEUE_PATH = "data/cit_implementation_queue.json"

def _route_to_cit_implementer(proposals: list) -> None:
    """
    Route auto-approved trivial fixes to the CIT Implementation Agent queue.
    The CIT Implementation Agent reads this file and applies the fixes.
    """
    try:
        queue = []
        if os.path.exists(CIT_IMPL_QUEUE_PATH):
            with open(CIT_IMPL_QUEUE_PATH) as f:
                queue = json.load(f)
        if not isinstance(queue, list):
            queue = []

        existing_ids = {item.get("proposal_id") for item in queue}
        added = 0
        for p in proposals:
            pid = p.get("id", p.get("proposal_id", ""))
            if pid in existing_ids:
                continue
            queue.append({
                "proposal_id":   pid,
                "title":         p.get("title", "?"),
                "category":      p.get("category", "?"),
                "severity":      p.get("severity", "LOW"),
                "confidence":    p.get("confidence", 0),
                "proposed_fix":  p.get("proposed_fix", ""),
                "evidence":      p.get("evidence", ""),
                "queued_at":     datetime.now().isoformat(),
                "status":        "queued",
                "routed_by":     "deputy_board:auto_tier0",
            })
            added += 1

        with open(CIT_IMPL_QUEUE_PATH, "w") as f:
            json.dump(queue, f, indent=2, default=str)

        if added > 0:
            logger.info(f"  → {added} fix(es) queued in cit_implementation_queue.json for CIT Implementation Agent")
    except Exception as e:
        logger.warning(f"  Could not route to CIT implementer: {e}")


# =============================================================================
# MAIN
# =============================================================================

def run_deputy_board(dry_run: bool = False) -> dict:
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — DEPUTY BOARD")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Don't convene during a full board meeting — deputies are occupied
    if _is_board_meeting_active():
        logger.info("  Full board meeting active — deputy board standing down.")
        return {"status": "SKIPPED", "reason": "board_meeting_active"}

    # Load pending proposals in deputy scope
    all_pending = _load_json(PENDING_PATH)
    if not isinstance(all_pending, list):
        all_pending = []

    # Load answered clarifications so we can skip proposals still awaiting CIT response
    unanswered_pids: set = set()
    try:
        if os.path.exists(CLARIFY_PATH):
            clr_records = _load_json(CLARIFY_PATH) or []
            if isinstance(clr_records, list):
                unanswered_pids = {
                    c["proposal_id"] for c in clr_records
                    if c.get("status") == "PENDING"
                }
    except Exception:
        pass

    if unanswered_pids:
        logger.info(
            f"  {len(unanswered_pids)} proposal(s) skipped — still awaiting CIT Director response. "
            f"Check {CLARIFY_PATH} for open questions."
        )

    # ── Tier 0: auto-implement trivial fixes without a vote ──────────────────
    auto_implement = [
        p for p in all_pending
        if p.get("category", "") in AUTO_IMPLEMENT_CATEGORIES
        and p.get("severity", "MEDIUM") == AUTO_IMPLEMENT_MAX_SEVERITY
        and p.get("confidence", 0) >= AUTO_IMPLEMENT_MIN_CONFIDENCE
        and p.get("id", "") not in unanswered_pids
    ]
    if auto_implement:
        logger.info(f"  {len(auto_implement)} trivial fix(es) routed to CIT Implementation Agent (no vote needed)")
        _route_to_cit_implementer(auto_implement)

    # ── Tier 2: full-board-only categories — never touch ─────────────────────
    # (STRATEGY / ARCHITECTURE are left in pending for the main board to pick up)

    # ── Tier 1: proposals for deputy vote ────────────────────────────────────
    def _should_deputy_vote(p: dict) -> bool:
        cat = p.get("category", "")
        sev = p.get("severity", "MEDIUM")
        pid = p.get("id", "")
        if pid in unanswered_pids:
            return False
        if cat in FULL_BOARD_ONLY_CATEGORIES:
            return False  # escalate to full board
        if cat in AUTO_IMPLEMENT_CATEGORIES and sev == AUTO_IMPLEMENT_MAX_SEVERITY:
            return False  # already handled above
        # IMPROVEMENT/VISION HIGH/CRITICAL → full board
        if cat in ("IMPROVEMENT", "VISION") and sev in ("HIGH", "CRITICAL"):
            return False
        return cat in DEPUTY_CATEGORIES

    bug_proposals = [p for p in all_pending if _should_deputy_vote(p)]

    if not bug_proposals:
        logger.info("  No proposals in deputy scope — deputy board not needed.")
        return {
            "status": "NO_ACTION",
            "proposals_reviewed": 0,
            "auto_implemented": len(auto_implement),
        }

    logger.info(f"  {len(bug_proposals)} bug fix proposal(s) to review")

    results      = []
    approved     = []
    rejected     = []
    deferred     = []
    needs_info   = []

    for prop in bug_proposals:
        revote_tag = " [RE-VOTE — CIT Director responded]" if prop.get("needs_revote") else ""
        logger.info(f"\n  [{prop.get('severity','?')}] {prop.get('title','')[:70]}{revote_tag}")

        # Hard stop: if a proposal has exceeded the max clarification rounds,
        # escalate to Chairman and remove from deputy queue rather than looping forever.
        # Count batch rounds (not individual questions) so a 3-question first round = 1 round.
        n_rounds = prop.get("clarification_rounds", 0)
        if n_rounds >= MAX_CLARIFICATION_ROUNDS:
            logger.warning(
                f"  ESCALATED — proposal has {n_rounds} clarification rounds "
                f"with no approval. Routing to Chairman."
            )
            try:
                from firm_channel import request_cos_meeting as _rcm
                _rcm(
                    from_agent="Deputy Board",
                    summary=(
                        f"ESCALATION: Proposal '{prop.get('title','?')[:80]}' "
                        f"has been through {n_rounds} deputy clarification rounds "
                        f"and still cannot be approved. Deputy board cannot resolve this — "
                        f"Chairman review required. Proposal ID: {prop.get('id','?')}. "
                        f"Last clarification: {prop.get('clarification_log',[''])[- 1].get('question','?')[:150]}"
                    ),
                    urgency="HIGH",
                )
            except Exception:
                pass
            prop["status"] = "ESCALATED_TO_CHAIRMAN"
            prop.pop("needs_revote", None)
            # Write status back to pending so CIT Director sees it
            _pending_all = _load_json(PENDING_PATH) or []
            if isinstance(_pending_all, list):
                for _p in _pending_all:
                    if _p.get("id") == prop.get("id"):
                        _p["status"] = "ESCALATED_TO_CHAIRMAN"
                        break
                with open(PENDING_PATH, "w") as _f:
                    json.dump(_pending_all, _f, indent=2, default=str)
            results.append({
                "proposal_id": prop.get("id"), "title": prop.get("title"),
                "passed": False, "classification": "ESCALATED",
                "approves": 0, "rejects": 0, "defers": 0, "clarifies": 0,
                "votes": [{"deputy": "Deputy Board", "vote": "ESCALATE",
                           "rationale": f"Max clarification rounds ({MAX_CLARIFICATION_ROUNDS}) exceeded."}],
            })
            continue
        vote_result = _vote_on_proposal(prop)

        cls = vote_result["classification"]
        status_icon = (
            "✅" if vote_result["passed"]
            else "❌" if vote_result["rejects"] > 0
            else "❓" if cls == "NEEDS_INFO"
            else "⏳"
        )
        logger.info(
            f"  {status_icon} {cls} "
            f"({vote_result['approves']}✓ {vote_result['rejects']}✗ "
            f"{vote_result['defers']}⏳ {vote_result.get('clarifies',0)}❓)"
        )

        if cls == "NEEDS_INFO":
            logger.info(f"  → Clarification requests written to {CLARIFY_PATH}")
            for v in vote_result["votes"]:
                if v["vote"] == "CLARIFY":
                    logger.info(f"    [{v['deputy']}]: {v['rationale'][:100]}")

        results.append(vote_result)

        if vote_result["passed"]:
            approved.append(prop)
            if not dry_run:
                prop.pop("needs_revote", None)   # clear flag on approval
                prop.pop("revote_reason", None)
                _queue_fix(prop, vote_result)
                _mark_approved(prop)
                logger.info(f"  → Queued for implementation in cos_meeting_requests.json")
        elif vote_result["rejects"] > 0:
            rejected.append(prop)
        elif cls == "NEEDS_INFO":
            needs_info.append(prop)
        else:
            deferred.append(prop)

    # Build report
    report = {
        "generated_at":      now.isoformat(),
        "dry_run":           dry_run,
        "proposals_reviewed": len(bug_proposals),
        "approved_count":    len(approved),
        "rejected_count":    len(rejected),
        "deferred_count":    len(deferred),
        "needs_info_count":  len(needs_info),
        "clarify_path":      CLARIFY_PATH if needs_info else None,
        "results":           results,
        "status":            "CLEAN" if not bug_proposals else (
            "ALL_APPROVED" if len(approved) == len(bug_proposals)
            else "PARTIAL"     if approved
            else "NEEDS_INFO"  if needs_info and not approved
            else "NONE_APPROVED"
        ),
    }

    os.makedirs("data", exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Post to firm channel
    try:
        from firm_channel import post
        ni_note = f", {len(needs_info)} need clarification from CIT Director" if needs_info else ""
        summary = (
            f"Deputy Board: {len(approved)} approved, {len(rejected)} rejected, "
            f"{len(deferred)} deferred{ni_note} out of {len(bug_proposals)} proposal(s)."
        )
        if needs_info:
            summary += (
                f" Clarification requests written to {CLARIFY_PATH} — "
                f"CIT Director must respond before these proposals can be re-voted."
            )
        post("Deputy Board", "PIPELINE_ALERT", summary,
             priority="HIGH" if (approved or needs_info) else "NORMAL")
    except Exception as e:
        logger.warning(f"  Firm channel post failed: {e}")

    logger.info(f"\n  Deputy Board complete — {len(approved)} approved, "
                f"{len(needs_info)} awaiting CIT clarification")
    return report


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('DeputyBoard'):
        raise SystemExit(0)
    import sys
    dry = "--dry-run" in sys.argv
    report = run_deputy_board(dry_run=dry)
    print(json.dumps(report, indent=2, default=str))

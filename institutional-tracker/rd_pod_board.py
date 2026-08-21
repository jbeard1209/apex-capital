#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — R&D / INSTITUTIONAL POD BOARD (Pod 1)
# =============================================================================
#
# The original board that used to handle everything now serves the Main Board
# for firm oversight. This board owns Pod 1 (R&D / Institutional, $30k).
#
# Responsibilities:
#   1. Investment picks — vote on research_team picks before Manager execution
#   2. Operational issues — vote on CIT/Logic proposals tagged to this pod
#   3. Agent governance — PIP/retire/amnesty decisions for pod1 agents
#   4. Escalation — flag decisions above pod authority to main board
#
# Board members:
#   - R&D Investment Director  (research quality, sector coverage, conviction)
#   - Portfolio Risk Officer    (position sizing, concentration, stop-loss)
#   - Research Quality Officer  (agent health, zero-pick streaks, coverage gaps)
#
# Reads from: rd_pod_pending.json  (operational proposals routed here)
#             research_team_report.json  (picks to vote on)
# Writes to:  rd_pod_approved.json
#             rd_pod_board_log.json
#             cit_pending_proposals.json  (escalations to main board)
#
# Usage:
#   python3 rd_pod_board.py            # full session (picks + operations)
#   python3 rd_pod_board.py --ops      # operational proposals only
#   python3 rd_pod_board.py --picks    # pick review only
#   python3 rd_pod_board.py --report   # show recent decisions
# =============================================================================

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rd_pod_board")

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
PENDING_PATH  = DATA_DIR / "rd_pod_pending.json"      # incoming proposals for this pod
APPROVED_PATH = DATA_DIR / "rd_pod_approved.json"     # approved operational decisions
BOARD_LOG     = DATA_DIR / "rd_pod_board_log.json"
MAIN_PENDING  = DATA_DIR / "cit_pending_proposals.json"  # escalation target

os.chdir(BASE_DIR)


def _load(path: Path, default=None):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# =============================================================================
# BOARD MEMBER VOTE LOGIC
# =============================================================================

_BOARD_SYSTEM = """You are a member of the Apex Capital R&D / Institutional Pod Board.
Apex Capital is an AI-driven paper trading firm with $100k AUM split across 5 pods.
Pod 1 (R&D/Institutional, $30k) uses 13F institutional signals to find high-conviction buys.

You will receive a proposal from the CIT (Continuous Improvement Team) and must vote:
  APPROVE  — implement this proposal
  HOLD     — defer, needs more evidence or lower priority
  ESCALATE — beyond pod authority, send to main board

Reply in exactly this JSON format (no other text):
{
  "vote": "APPROVE" | "HOLD" | "ESCALATE",
  "reason": "one concise sentence explaining your vote"
}"""


def _llm_vote(member_role: str, member_context: str, proposal: dict) -> tuple[str, str]:
    """Try an LLM vote; fall back to rule-based if unavailable."""
    try:
        from apex_llm import reason as _llm_reason, is_available
        if not is_available("rd_pod_board"):
            return None, None
        prop_text = (
            f"Title: {proposal.get('title','')}\n"
            f"Severity: {proposal.get('severity','LOW')}\n"
            f"Category: {proposal.get('category','')}\n"
            f"Description: {proposal.get('description','')[:400]}\n"
            f"Proposed fix: {proposal.get('proposed_fix','')[:300]}\n"
            f"Confidence: {proposal.get('confidence',70)}%"
        )
        user_msg = f"You are the {member_role}.\n{member_context}\n\nProposal to vote on:\n{prop_text}"
        import json as _json
        raw = _llm_reason(_BOARD_SYSTEM, user_msg, agent_name="rd_pod_board", max_tokens=200)
        if not raw:
            return None, None
        parsed = _json.loads(raw.strip())
        vote   = parsed.get("vote", "").upper()
        reason = parsed.get("reason", "")
        if vote in ("APPROVE", "HOLD", "ESCALATE") and reason:
            return vote, reason
    except Exception as _e:
        logger.debug("[RD Board] LLM vote failed: %s", _e)
    return None, None


def _investment_director_vote(proposal: dict) -> tuple[str, str]:
    """R&D Investment Director: focused on research quality and deployment."""
    v, r = _llm_vote(
        "R&D Investment Director",
        "You focus on research output quality, pick conviction, sector coverage, and agent health. "
        "You care most about whether this proposal will lead to better or more picks.",
        proposal,
    )
    if v:
        return v, r
    # Rule-based fallback
    sev   = proposal.get("severity", "LOW")
    title = proposal.get("title", "").lower()
    if sev in ("CRITICAL", "HIGH"):
        return "APPROVE", f"HIGH/CRITICAL severity warrants action"
    if any(x in title for x in ["pick", "research", "agent", "watchlist", "conviction", "sector"]):
        return "APPROVE", "Directly improves research output"
    if "pipeline" in title or "dark" in title:
        return "APPROVE", "Pipeline health = pod health"
    if sev == "MEDIUM":
        return "APPROVE", "MEDIUM severity — acceptable improvement"
    return "HOLD", "LOW severity — queue for next cycle"


def _risk_officer_vote(proposal: dict) -> tuple[str, str]:
    """Portfolio Risk Officer: checks if proposed change could hurt portfolio."""
    v, r = _llm_vote(
        "Portfolio Risk Officer",
        "You focus on whether this proposal could disrupt live positions, affect capital allocation, "
        "or introduce execution risk. Escalate anything touching live positions or budget.",
        proposal,
    )
    if v:
        return v, r
    # Rule-based fallback
    fix   = proposal.get("proposed_fix", "").lower()
    title = proposal.get("title", "").lower()
    sev   = proposal.get("severity", "LOW")
    if any(x in fix+title for x in ["close all", "emergency exit", "liquidate", "halt all"]):
        return "ESCALATE", "Proposal affects live positions — must go to main board"
    if any(x in fix+title for x in ["budget", "capital", "allocation", "$"]):
        return "ESCALATE", "Budget/capital changes require main board approval"
    if sev == "CRITICAL":
        return "APPROVE", "CRITICAL: risk of inaction exceeds risk of action"
    return "APPROVE", "Operational improvement, no portfolio risk"


def _quality_officer_vote(proposal: dict) -> tuple[str, str]:
    """Research Quality Officer: agent health, coverage gaps, data quality."""
    v, r = _llm_vote(
        "Research Quality Officer",
        "You focus on agent output quality, data integrity, coverage gaps, and whether proposals "
        "have enough evidence to act on. Hold low-confidence proposals.",
        proposal,
    )
    if v:
        return v, r
    # Rule-based fallback
    title = proposal.get("title", "").lower()
    sev   = proposal.get("severity", "LOW")
    conf  = proposal.get("confidence", 70)
    if conf < 50:
        return "HOLD", f"Low confidence ({conf}%) — needs more evidence"
    if any(x in title for x in ["agent", "hr", "pip", "retire", "amnesty", "streak"]):
        return "APPROVE", "Agent governance within pod authority"
    if any(x in title for x in ["data", "source", "missing", "stale", "gap"]):
        return "APPROVE", "Data quality improvement is always worthwhile"
    if sev in ("HIGH", "CRITICAL"):
        return "APPROVE", f"Quality concern at {sev}"
    return "APPROVE", "Supports research quality"


def _board_vote(proposal: dict) -> dict:
    """Run all three board members, determine outcome."""
    v1, r1 = _investment_director_vote(proposal)
    v2, r2 = _risk_officer_vote(proposal)
    v3, r3 = _quality_officer_vote(proposal)

    votes = [v1, v2, v3]

    if "ESCALATE" in votes:
        decision = "ESCALATE"
    elif votes.count("APPROVE") >= 2:
        decision = "APPROVED"
    elif votes.count("HOLD") >= 2:
        decision = "DEFERRED"
    else:
        decision = "APPROVED"

    return {
        "decision":    decision,
        "voted_at":    datetime.now().isoformat(),
        "votes": [
            {"member": "Investment Director", "vote": v1, "reason": r1},
            {"member": "Risk Officer",        "vote": v2, "reason": r2},
            {"member": "Quality Officer",     "vote": v3, "reason": r3},
        ],
    }


# =============================================================================
# OPERATIONAL PROPOSALS SESSION
# =============================================================================

def run_ops_session() -> dict:
    """Vote on pending operational proposals for this pod."""
    pending_raw = _load(PENDING_PATH, [])
    if not isinstance(pending_raw, list):
        pending_raw = []

    pending = [p for p in pending_raw if p.get("status") in ("pending", None, "")]
    if not pending:
        logger.info("[RD Board] No pending operational proposals.")
        return {"voted": 0, "approved": 0, "deferred": 0, "escalated": 0}

    logger.info("[RD Board] Ops session: %d proposal(s) to review", len(pending))

    approved_ops = []
    deferred     = []
    escalated    = []
    results      = []

    for prop in pending:
        vote_result = _board_vote(prop)
        decision    = vote_result["decision"]
        prop.update(vote_result)
        prop["status"] = decision.lower()

        if decision == "APPROVED":
            approved_ops.append(prop)
            logger.info("[RD Board] APPROVED: %s", prop.get("title","?")[:60])
        elif decision == "ESCALATE":
            escalated.append(prop)
            logger.warning("[RD Board] ESCALATED to main board: %s", prop.get("title","?")[:60])
        else:
            deferred.append(prop)

        results.append(prop)

    # Write back updated pending list
    _save(PENDING_PATH, results)

    # Write approved ops decisions
    existing_approved = _load(APPROVED_PATH, [])
    if not isinstance(existing_approved, list):
        existing_approved = []
    existing_approved.extend(approved_ops)
    existing_approved = existing_approved[-100:]  # keep last 100
    _save(APPROVED_PATH, existing_approved)

    # Escalate to main board
    if escalated:
        main_pending = _load(MAIN_PENDING, [])
        if not isinstance(main_pending, list):
            main_pending = []
        existing_titles = {p.get("title","") for p in main_pending}
        added = 0
        for prop in escalated:
            if prop.get("title","") not in existing_titles:
                prop["escalated_from"] = "rd_pod_board"
                prop["escalated_at"]   = datetime.now().isoformat()
                main_pending.append(prop)
                added += 1
        _save(MAIN_PENDING, main_pending)
        if added:
            logger.warning("[RD Board] %d proposal(s) escalated to main board", added)

    # Log session
    log = _load(BOARD_LOG, {"sessions": []})
    if not isinstance(log, dict):
        log = {"sessions": []}
    log["sessions"].append({
        "ts":        datetime.now().isoformat(),
        "type":      "ops",
        "reviewed":  len(pending),
        "approved":  len(approved_ops),
        "deferred":  len(deferred),
        "escalated": len(escalated),
    })
    log["sessions"] = log["sessions"][-50:]
    _save(BOARD_LOG, log)

    return {
        "voted":     len(pending),
        "approved":  len(approved_ops),
        "deferred":  len(deferred),
        "escalated": len(escalated),
    }


def print_report():
    log = _load(BOARD_LOG, {})
    sessions = log.get("sessions", [])
    pending  = _load(PENDING_PATH, [])
    approved = _load(APPROVED_PATH, [])

    print(f"\n{'='*60}")
    print(f"  R&D POD BOARD REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Sessions on record: {len(sessions)}")

    open_props = [p for p in pending if isinstance(pending, list) and p.get("status") in ("pending","")]
    print(f"  Open proposals: {len(open_props) if isinstance(pending, list) else 0}")
    print(f"  Approved lifetime: {len(approved) if isinstance(approved, list) else 0}")

    if sessions:
        last = sessions[-1]
        print(f"\n  Last session: {last.get('ts','?')[:16]}")
        print(f"    Approved: {last.get('approved',0)} | Deferred: {last.get('deferred',0)} | Escalated: {last.get('escalated',0)}")

    if isinstance(open_props, list) and open_props:
        print(f"\n  OPEN PROPOSALS:")
        for p in open_props[:5]:
            print(f"    [{p.get('severity','?')}] {p.get('title','?')[:60]}")
    print()


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('RDPodBoard'):
        raise SystemExit(0)
    parser = argparse.ArgumentParser()
    parser.add_argument("--ops",    action="store_true", help="Operational proposals only")
    parser.add_argument("--report", action="store_true", help="Print board report")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        run_ops_session()

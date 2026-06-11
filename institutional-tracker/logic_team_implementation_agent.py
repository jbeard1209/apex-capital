#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LOGIC TEAM IMPLEMENTATION AGENT
# =============================================================================
#
# The Logic Team finds coherence failures. This agent acts on them.
#
# Unlike the CIT Implementation Agent (which writes code), this agent's job
# is operational resolution: it triages open Logic Team findings, determines
# which ones can be auto-resolved vs. require board action, and executes
# the resolutions it's authorized to take.
#
# Authorized auto-resolutions:
#   - Agent health gaps: spawn replacement agents via Chief of Staff
#   - Stale chairman items: draft summary and flag for dismissal
#   - Capital idle too long: escalate to board with specific deployment plan
#   - Pipeline dark: trigger rd_team_leader.py run immediately
#   - Board governance gaps: trigger board_meeting.py with agenda
#
# Everything else is escalated to the board as a proposal with a specific
# recommended action and an owner (CIT, HR, IRM, Chairman).
#
# Schedule: runs after each Logic Team cycle (wired into logic_team.py
#           and crontab after logic_team standalone runs)
#
# Usage:
#   python3 logic_team_implementation_agent.py           # full triage cycle
#   python3 logic_team_implementation_agent.py --report  # show open items
# =============================================================================

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lt_impl")

BASE_DIR         = Path(__file__).parent
DATA_DIR         = BASE_DIR / "data"
LT_RESOLUTIONS   = DATA_DIR / "logic_team_resolutions.json"
LT_FINDINGS      = DATA_DIR / "logic_team_findings.json"
CIT_PENDING      = DATA_DIR / "cit_pending_proposals.json"
FIRM_CHANNEL     = DATA_DIR / "firm_channel.json"
IMPL_LOG         = DATA_DIR / "lt_implementation_log.json"

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


def _log_action(action: str, finding_key: str, result: str, auto_resolved: bool = False):
    log = _load(IMPL_LOG, {"actions": []})
    if not isinstance(log, dict):
        log = {"actions": []}
    log["actions"].append({
        "ts":           datetime.now().isoformat(),
        "finding_key":  finding_key,
        "action":       action,
        "result":       result,
        "auto_resolved": auto_resolved,
    })
    log["actions"] = log["actions"][-200:]
    log["last_run"] = datetime.now().isoformat()
    _save(IMPL_LOG, log)


def _post_to_channel(message: str, priority: str = "NORMAL"):
    try:
        fc = _load(FIRM_CHANNEL, [])
        if not isinstance(fc, list):
            fc = []
        fc.append({
            "ts":       datetime.now().isoformat(),
            "date":     datetime.now().strftime("%Y-%m-%d"),
            "agent":    "LT Implementation Agent",
            "category": "LT_RESOLUTION",
            "priority": priority,
            "content":  message,
        })
        if len(fc) > 500:
            fc = fc[-500:]
        _save(FIRM_CHANNEL, fc)
    except Exception as e:
        logger.warning(f"  Channel post failed: {e}")


def _escalate_to_board(title: str, description: str, recommended_owner: str,
                       severity: str = "MEDIUM", proposed_fix: str = "") -> bool:
    """Submit a resolution proposal to the board queue."""
    try:
        pending = _load(CIT_PENDING, [])
        if not isinstance(pending, list):
            pending = []
        existing = {p.get("title","") for p in pending}
        if title in existing:
            return False
        pending.append({
            "id":           f"LTIA-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "source":       "LT Implementation Agent",
            "category":     "OPERATIONAL_FIX",
            "severity":     severity,
            "confidence":   85,
            "title":        title,
            "description":  description,
            "proposed_fix": proposed_fix or description,
            "owner":        recommended_owner,
            "created_at":   datetime.now().isoformat(),
            "status":       "pending",
        })
        _save(CIT_PENDING, pending)
        return True
    except Exception as e:
        logger.warning(f"  Board escalation failed: {e}")
        return False


# =============================================================================
# RESOLUTION HANDLERS — one per rule type
# =============================================================================

def _handle_pipeline_dark(entry: dict) -> str:
    """Pipeline hasn't produced picks — attempt to trigger R&D run."""
    try:
        logger.info("  → Triggering rd_team_leader.py to break research dark window...")
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "rd_team_leader.py")],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            _log_action("triggered_rd_team_leader", "pipeline::pipeline_dark",
                        "R&D run triggered successfully", auto_resolved=False)
            return "triggered_rd_team_leader"
        else:
            return f"rd_run_failed: {result.stderr[:100]}"
    except subprocess.TimeoutExpired:
        return "rd_run_timeout"
    except Exception as e:
        return f"error: {e}"


def _handle_idle_capital(entry: dict) -> str:
    """Capital sitting idle — escalate with deployment plan."""
    title = f"[LTIA] Idle capital review required: {entry.get('title','?')[:50]}"
    description = (
        f"Logic Team found idle capital (finding: '{entry.get('title','?')}'). "
        f"First detected {entry.get('first_seen','?')[:10]}, seen {entry.get('times_seen',1)} times. "
        f"Board should vote: (1) is the idle capital intentional? (2) if CRO is the cause, "
        f"is that still warranted? (3) assign CIT to investigate if pipeline failure."
    )
    submitted = _escalate_to_board(title, description,
                                    recommended_owner="Board + CIT",
                                    severity="MEDIUM",
                                    proposed_fix="Board votes on capital deployment plan within 2 sessions.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _handle_governance_gap(entry: dict) -> str:
    """Board hasn't met or CRO overdue for review — trigger board meeting."""
    title = f"[LTIA] Governance gap requires board action: {entry.get('title','?')[:50]}"
    description = (
        f"Logic Team governance check failed: '{entry.get('title','?')}'. "
        f"Days open: {entry.get('days_open', 0)}. Times flagged: {entry.get('times_seen', 1)}. "
        f"Recommended action: schedule board meeting with governance as primary agenda item."
    )
    submitted = _escalate_to_board(title, description,
                                    recommended_owner="Chairman + Board",
                                    severity="HIGH",
                                    proposed_fix="Trigger board_meeting.py. Agenda: governance review.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


def _handle_cro_paralysis(entry: dict) -> str:
    """CRO blocking buys for too long — escalate with explicit exit criteria."""
    title = "[LTIA] CRO extended block requires board vote with exit criteria"
    description = (
        f"Logic Team flagged CRO paralysis: '{entry.get('title','?')}'. "
        f"The board must vote on explicit exit criteria for the CRO block — "
        f"not just 'review it' but define the exact condition that would move CRO to ELEVATED."
    )
    submitted = _escalate_to_board(title, description,
                                    recommended_owner="Board + CRO + IRM",
                                    severity="HIGH",
                                    proposed_fix="Board votes: define exit criteria for CRO=HIGH status.")
    return "board_proposal_submitted" if submitted else "already_in_queue"


# Rule → handler mapping
RESOLUTION_HANDLERS = {
    "pipeline_dark":         _handle_pipeline_dark,
    "no_picks_agents_exist": _handle_pipeline_dark,
    "idle_capital":          _handle_idle_capital,
    "cro_extended_no_review": _handle_governance_gap,
    "cro_paralysis":          _handle_cro_paralysis,
    "extended_cro_high_no_review": _handle_governance_gap,
    "stale_chairman_actions": _handle_governance_gap,
}


# =============================================================================
# MAIN TRIAGE LOOP
# =============================================================================

def run_triage() -> dict:
    logger.info("=" * 55)
    logger.info("  LT IMPLEMENTATION AGENT — TRIAGE")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    tracker = _load(LT_RESOLUTIONS, {"open": {}})
    open_findings = tracker.get("open", {})

    if not open_findings:
        logger.info("  No open Logic Team findings — nothing to triage ✓")
        return {"triaged": 0, "actions_taken": 0}

    logger.info(f"  {len(open_findings)} open finding(s) to triage")

    actions_taken = 0
    results = []

    for key, entry in sorted(open_findings.items(),
                              key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(
                                  x[1].get("severity","LOW"))):
        rule     = entry.get("rule", key.split("::")[-1])
        severity = entry.get("severity","?")
        title    = entry.get("title","?")
        days_open = entry.get("days_open", 0) or (
            datetime.now() - datetime.fromisoformat(
                entry.get("first_seen", datetime.now().isoformat()))).days

        logger.info(f"  [{severity}] {title[:55]} (open {days_open}d)")

        handler = RESOLUTION_HANDLERS.get(rule)
        if handler:
            result = handler(entry)
            logger.info(f"    → {result}")
            _log_action(f"handler:{rule}", key, result)
            results.append({"key": key, "action": rule, "result": result})
            actions_taken += 1
        else:
            # No specific handler — escalate generically if HIGH/CRITICAL and open >2 days
            if severity in ("HIGH","CRITICAL") and days_open >= 2:
                title_prop = f"[LTIA] Unresolved {severity} finding: {title[:50]}"
                submitted = _escalate_to_board(
                    title_prop,
                    f"Logic Team finding open for {days_open} days with no handler: '{title}'. "
                    f"Rule: {rule}. Times seen: {entry.get('times_seen',1)}.",
                    recommended_owner="CIT",
                    severity=severity,
                )
                result = "generic_board_escalation" if submitted else "already_in_queue"
                _log_action("generic_escalation", key, result)
                results.append({"key": key, "action": "generic_escalation", "result": result})
                actions_taken += 1

    if actions_taken:
        _post_to_channel(
            f"LT Implementation Agent triaged {len(open_findings)} finding(s), "
            f"took {actions_taken} action(s). "
            + (f"Results: " + "; ".join(f"{r['action']}→{r['result']}" for r in results[:3]))
        )

    return {"triaged": len(open_findings), "actions_taken": actions_taken, "results": results}


def print_report():
    tracker = _load(LT_RESOLUTIONS, {})
    open_f  = tracker.get("open", {})
    resolved = tracker.get("resolved", [])
    log      = _load(IMPL_LOG, {})
    actions  = log.get("actions", [])

    print(f"\n{'='*60}")
    print(f"  LT IMPLEMENTATION AGENT REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"\n  Open findings: {len(open_f)}  |  Resolved lifetime: {len(resolved)}")
    print(f"  Actions taken (last 10 runs): {len(actions)}")

    if open_f:
        print("\n  OPEN:")
        for key, e in sorted(open_f.items(),
                             key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(
                                 x[1].get("severity","LOW"))):
            days = (datetime.now() - datetime.fromisoformat(
                e.get("first_seen", datetime.now().isoformat()))).days
            print(f"    [{e.get('severity','?')}] {e.get('title','?')[:60]} ({days}d open)")

    if resolved:
        print(f"\n  RECENTLY RESOLVED (last 5):")
        for e in resolved[-5:]:
            print(f"    ✅ {e.get('title','?')[:60]}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        run_triage()

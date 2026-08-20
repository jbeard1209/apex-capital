#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LOGIC OFFICER
# =============================================================================
#
# The one agent whose job is to ask: "Does the whole picture make sense?"
#
# Every other agent watches its own lane:
#   - Canary: market levels
#   - CRO: portfolio risk
#   - HR: individual agent output
#   - CIT: code quality
#   - IRM: investment risk
#
# The Logic Officer connects the dots across all of them. It reads the firm's
# full state and asks simple, high-level questions:
#
#   "We have 29 active research agents and the canary is GREEN.
#    Why are there zero picks this cycle?"
#
#   "The CRO has been at HIGH for 17 consecutive cycles.
#    The board has never voted on whether that's still appropriate."
#
#   "All 29 agents were in 'reassigned' status for two weeks.
#    Not one watchdog flagged that the research pipeline was dark."
#
# When it finds something that doesn't add up, it:
#   1. Formats a clear anomaly report (what it observed, why it's strange)
#   2. Posts to the firm channel so all agents can see it
#   3. If severity is HIGH/CRITICAL, creates a board proposal with a
#      concrete suggested resolution for the board to vote on
#   4. Adds to the Chairman Briefing digest so Jack sees it at next login
#
# It does NOT try to fix things itself. That's not its job. Its job is
# to be the one voice in the room that asks the obvious questions everyone
# else is too specialized to notice.
#
# Usage:
#   python3 logic_officer.py              # run full coherence check
#   python3 logic_officer.py --report     # print latest findings to console
# =============================================================================

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("logic_officer")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
os.chdir(BASE_DIR)

FINDINGS_PATH       = DATA_DIR / "logic_officer_findings.json"
BRIEFING_PATH       = DATA_DIR / "chairman_briefing.json"
FIRM_CHANNEL_PATH   = DATA_DIR / "firm_channel.json"
CIT_PENDING_PATH    = DATA_DIR / "cit_pending_proposals.json"


# =============================================================================
# DATA LOADERS
# =============================================================================

def _load(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


# =============================================================================
# COHERENCE RULES
# Each rule is a function that takes the full state snapshot and returns
# a finding dict if something is wrong, or None if everything is fine.
# =============================================================================

def _rule_zero_picks(state: dict) -> Optional[dict]:
    """Active agents + green canary + zero picks = broken pipeline."""
    registry  = state.get("registry", {})
    agents    = registry.get("agents", [])
    active    = [a for a in agents if a.get("status") == "active"]
    canary    = state.get("canary_level", "GREEN")
    picks     = state.get("picks_today", 0)
    cro_level = state.get("cro_level", "LOW")

    if len(active) < 5:
        return None   # too few agents to have a meaningful expectation
    if canary in ("RED", "ORANGE") or cro_level == "CRITICAL":
        return None   # expected to have reduced output
    if picks > 0:
        return None   # working fine

    # Check how long this has been going on
    last_pick_date = state.get("last_pick_date")
    days_dark = 0
    if last_pick_date:
        try:
            days_dark = (datetime.now() - datetime.fromisoformat(last_pick_date)).days
        except Exception:
            pass

    if days_dark < 2:
        return None   # might just be a slow day

    return {
        "rule":        "zero_picks_anomaly",
        "severity":    "CRITICAL" if days_dark > 5 else "HIGH",
        "title":       f"Research pipeline producing ZERO picks despite {len(active)} active agents",
        "observation": (
            f"The firm has {len(active)} agents with status=active. "
            f"Canary is {canary}. CRO is {cro_level}. "
            f"Despite all of this, the research pipeline has produced zero picks "
            f"for approximately {days_dark} day(s). "
            f"This is almost certainly a pipeline or routing bug, not a market condition."
        ),
        "why_its_wrong": (
            "With active agents and a non-emergency canary, the pipeline should "
            "always produce at least some pick candidates for the Manager to review. "
            "Zero picks means either: agents are not loading, agents are running but "
            "their output is not reaching the Manager, or all picks are being blocked "
            "at the Manager level by a misconfigured rule."
        ),
        "suggested_resolution": (
            "Board should vote to: (1) check research_team.py's agent status filter "
            "(must load both 'active' AND 'reassigned'), "
            "(2) verify manager_agent.py is not blocking all picks due to a CRO/PDO "
            "misconfiguration, "
            "(3) run a manual research cycle and check the output."
        ),
    }


def _rule_mass_reassignment(state: dict) -> Optional[dict]:
    """If most/all research agents are in non-active status, flag it."""
    registry = state.get("registry", {})
    agents   = registry.get("agents", [])
    if not agents:
        return None

    total    = len(agents)
    active   = [a for a in agents if a.get("status") == "active"]
    non_active = [a for a in agents if a.get("status") not in ("active", "retired")]
    pct_non_active = len(non_active) / total if total > 0 else 0

    if pct_non_active < 0.5:
        return None   # majority still active — ok

    # Check if there's a legitimate reason (recent amnesty = expected)
    amnesty_agents = [a for a in agents if a.get("amnesty_restored_at")]
    if len(amnesty_agents) > len(non_active) * 0.8:
        return None   # amnesty explains it

    return {
        "rule":        "mass_reassignment",
        "severity":    "HIGH",
        "title":       f"{len(non_active)}/{total} agents are NOT in active status",
        "observation": (
            f"{len(non_active)} out of {total} agents have status != 'active'. "
            f"Only {len(active)} agents are actively generating research. "
            f"Statuses: {dict(__import__('collections').Counter(a.get('status','?') for a in agents))}"
        ),
        "why_its_wrong": (
            "Most agents being non-active almost certainly means the performance "
            "system over-fired during a period when CRO was blocking all buys. "
            "Agents were penalized for zero approved picks even though no picks "
            "could be approved system-wide. This is a structural false-positive."
        ),
        "suggested_resolution": (
            "Board should vote to: (1) audit CIT exoneration memo for the affected "
            "period, (2) apply Chairman amnesty to restore agents to active, "
            "(3) check whether CRO exoneration logic is properly preventing "
            "czc increments during HIGH/CRITICAL regimes."
        ),
    }


def _rule_extended_cro_high(state: dict) -> Optional[dict]:
    """CRO has been HIGH for a long time with no board review."""
    cro_level  = state.get("cro_level", "LOW")
    cro_streak = state.get("cro_streak", 0)
    board_reviewed = state.get("cro_board_reviewed_at")

    if cro_level not in ("HIGH", "CRITICAL"):
        return None
    if cro_streak < 5:
        return None   # short streaks are fine

    # Check if board has reviewed this recently
    if board_reviewed:
        try:
            days_since = (datetime.now() - datetime.fromisoformat(board_reviewed)).days
            if days_since < 3:
                return None   # reviewed recently
        except Exception:
            pass

    return {
        "rule":        "extended_cro_high_no_review",
        "severity":    "HIGH",
        "title":       f"CRO has been at {cro_level} for {cro_streak} cycles — no board review",
        "observation": (
            f"The CRO has been at {cro_level} for {cro_streak} consecutive cycles "
            f"(roughly {cro_streak * 4} days). During this entire period, all new "
            f"buy orders have been blocked or severely restricted. The board has not "
            f"formally voted on whether this posture is still appropriate."
        ),
        "why_its_wrong": (
            "An extended buy block is a major business decision that should require "
            "ongoing board ratification, not just persist automatically. It keeps "
            "$60k+ in idle cash, penalizes research agents for producing zero approved "
            "picks, and may be based on conditions that no longer exist."
        ),
        "suggested_resolution": (
            f"Board should formally vote: (1) is {cro_level} still warranted given "
            f"current market conditions? (2) If yes, set an explicit review date. "
            f"(3) If no, step down to ELEVATED with specific criteria for next escalation."
        ),
    }


def _rule_idle_capital(state: dict) -> Optional[dict]:
    """Large pods with no positions and no picks for extended period."""
    pod_alloc = state.get("pod_allocations", {})
    pods      = pod_alloc.get("pods", {}) if isinstance(pod_alloc, dict) else {}

    idle_pods = []
    for pod_id, pod in pods.items():
        positions    = pod.get("positions", {})
        # See the same change in logic_team.CapitalLogicAgent: the quantity this
        # rule is about is unspent entitlement, not the retired `total_value`.
        total_value  = pod.get("budget_remaining")
        if total_value is None:
            total_value = pod.get("cash_balance", 0) or 0
        n_positions  = len(positions)
        budget       = pod.get("budget_inception", 0) or 0

        if budget >= 20000 and n_positions == 0 and total_value >= 15000:
            idle_pods.append({
                "pod": pod.get("name", pod_id),
                "capital": total_value,
                "positions": n_positions,
            })

    if not idle_pods:
        return None

    total_idle = sum(p["capital"] for p in idle_pods)

    return {
        "rule":        "idle_capital",
        "severity":    "MEDIUM",
        "title":       f"{len(idle_pods)} pod(s) holding ${total_idle:,.0f} with zero positions",
        "observation": (
            f"The following pods have significant capital but zero open positions:\n"
            + "\n".join(f"  • {p['pod']}: ${p['capital']:,.0f}" for p in idle_pods)
        ),
        "why_its_wrong": (
            f"Idle cash earns nothing and creates a drag on portfolio returns. "
            f"${total_idle:,.0f} sitting in cash means the firm is not executing "
            f"on its mandate. This is either a CRO block (acceptable short-term), "
            f"a research pipeline failure (not acceptable), or an ML model not yet "
            f"cleared for live trading (expected)."
        ),
        "suggested_resolution": (
            "Board should vote: (1) for each idle pod, identify root cause of zero "
            "deployment, (2) if CRO block is reason, confirm this is still justified, "
            "(3) if pipeline failure is reason, assign CIT to fix it with priority."
        ),
    }


def _rule_cit_proposal_pileup(state: dict) -> Optional[dict]:
    """CIT pending queue growing faster than board can process it."""
    pending = state.get("cit_pending", [])
    if not isinstance(pending, list):
        pending = []

    unprocessed = [p for p in pending
                   if p.get("status","") not in ("approved","rejected","deferred","implemented")]

    if len(unprocessed) < 15:
        return None

    return {
        "rule":        "proposal_pileup",
        "severity":    "MEDIUM",
        "title":       f"CIT proposal queue has {len(unprocessed)} unprocessed items",
        "observation": (
            f"{len(unprocessed)} CIT proposals are pending without a board decision. "
            f"The deputy board and main board need to run more frequently to keep pace."
        ),
        "why_its_wrong": (
            "A large proposal backlog means real improvements are stalled. "
            "It also means the board is spending cycles reviewing proposals that "
            "may already be stale. The overnight sprint system should prevent this "
            "but may need to run more aggressively."
        ),
        "suggested_resolution": (
            "Board should vote to: (1) run overnight_board_sprint.py tonight, "
            "(2) verify deputy board is running after each CIT session, "
            "(3) consider raising the auto-implement confidence threshold to clear "
            "more items without a vote."
        ),
    }


def _rule_portfolio_unexplained_loss(state: dict) -> Optional[dict]:
    """Portfolio down significantly with no coherent attribution."""
    portfolio_value = state.get("portfolio_value", 0)
    starting_capital = 100000
    pnl_pct = ((portfolio_value - starting_capital) / starting_capital * 100) if portfolio_value > 0 else 0

    if pnl_pct > -3:
        return None   # within acceptable range

    # Currency, not presence. `snapshots[-1].get("loss_attribution")` was true
    # of a single hand-written 2026-06-09 record and stayed true forever, so
    # this rule reported "attribution provided" for every later drawdown --
    # including to the board on 2026-07-07, about a loss a month newer than the
    # document it cited. See loss_attribution.py.
    import loss_attribution
    attrib = loss_attribution.status(state.get("performance_history", {}))

    return {
        "rule":        "unexplained_loss",
        "severity":    "HIGH" if pnl_pct < -5 else "MEDIUM",
        "title":       f"Portfolio is {pnl_pct:.1f}% below starting capital — attribution {attrib['label']}",
        "observation": (
            f"Portfolio value is ${portfolio_value:,.0f}, which is {pnl_pct:.1f}% "
            f"below the $100,000 starting capital. " + attrib["detail"]
        ),
        "why_its_wrong": (
            "Every loss should have an explanation. If we don't know why we're losing, "
            "we can't fix it, and we're likely to repeat the same mistakes."
        ),
        "suggested_resolution": (
            "Board should vote to: (1) produce a written loss attribution, "
            "(2) identify which positions/decisions caused the drawdown, "
            "(3) review whether the same conditions still exist."
        ),
    }


# All rules in priority order
ALL_RULES = [
    _rule_zero_picks,
    _rule_mass_reassignment,
    _rule_extended_cro_high,
    _rule_idle_capital,
    _rule_cit_proposal_pileup,
    _rule_portfolio_unexplained_loss,
]


# =============================================================================
# STATE BUILDER
# =============================================================================

def _build_state() -> dict:
    """Load all relevant data files into a single state dict for rules to read."""
    state = {}

    # Registry
    state["registry"] = _load(DATA_DIR / "agent_registry.json", {"agents": []})

    # Canary
    canary = _load(DATA_DIR / "canary_state.json", {})
    state["canary_level"] = canary.get("last_level", "GREEN")

    # Risk bulletin
    rb = _load(DATA_DIR / "risk_bulletin.json", {})
    state["cro_level"]  = rb.get("risk_level", "LOW")
    state["cro_streak"] = rb.get("consecutive_high_cycles", 0)

    # Manager decisions — find last pick date
    mgr = _load(DATA_DIR / "manager_decisions.json", [])
    if isinstance(mgr, list) and mgr:
        last = mgr[-1] if isinstance(mgr[-1], dict) else {}
        decisions = last.get("decisions", [])
        picks_today = sum(1 for d in decisions
                          if d.get("manager_decision") == "APPROVED"
                          and d.get("manager_action") not in ("HOLD","WATCH","SELL","TRIM"))
        state["picks_today"] = picks_today

        # Find last date when there were any picks
        for entry in reversed(mgr):
            if isinstance(entry, dict):
                decs = entry.get("decisions", [])
                had_pick = any(d.get("manager_decision") == "APPROVED"
                               and d.get("manager_action") not in ("HOLD","WATCH","SELL","TRIM")
                               for d in decs)
                if had_pick:
                    state["last_pick_date"] = entry.get("generated_at","")
                    break
    else:
        state["picks_today"]    = 0
        state["last_pick_date"] = None

    # Pod allocations
    state["pod_allocations"] = _load(DATA_DIR / "pod_allocations.json", {})

    # CIT pending
    state["cit_pending"] = _load(DATA_DIR / "cit_pending_proposals.json", [])

    # Portfolio value
    review = _load(DATA_DIR / "review_recommendations.json", {})
    state["portfolio_value"] = review.get("account", {}).get("portfolio_value", 0)

    # Performance history
    state["performance_history"] = _load(DATA_DIR / "performance_history.json", {})

    return state


# =============================================================================
# BOARD PROPOSAL WRITER
# =============================================================================

def _write_board_proposals(findings: list) -> int:
    """Write HIGH/CRITICAL findings as board proposals for a vote."""
    serious = [f for f in findings if f.get("severity") in ("HIGH", "CRITICAL")]
    if not serious:
        return 0

    pending = _load(CIT_PENDING_PATH, [])
    if not isinstance(pending, list):
        pending = []

    # Dedup on rule, not title: every title here embeds a value that changes
    # run to run (cycle count, day count, $ amount, agent count), so matching
    # the literal title let the same unresolved issue re-queue every cycle
    # instead of being recognized as already pending.
    existing_keys = {p.get("dedup_key") or p.get("title","") for p in pending}
    added = 0
    now   = datetime.now().isoformat()

    for finding in serious:
        title = finding.get("title","")
        key = finding.get("rule") or title
        if key in existing_keys:
            continue

        proposal = {
            "id":            f"LO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{finding['rule']}",
            "source":        "Logic Officer",
            "category":      "IMPROVEMENT",
            "severity":      finding["severity"],
            "confidence":    90,
            "title":         title,
            "dedup_key":     key,
            "description":   finding["observation"],
            "evidence":      (
                f"Logic Officer coherence check. Rule: {finding['rule']}. "
                f"Observation: {finding['observation'][:200]}"
            ),
            "proposed_fix":  finding["suggested_resolution"],
            "why":           finding["why_its_wrong"],
            "created_at":    now,
            "status":        "pending",
        }
        pending.append(proposal)
        existing_keys.add(key)
        added += 1

    if added:
        _save(CIT_PENDING_PATH, pending)
    return added


# =============================================================================
# FIRM CHANNEL POST
# =============================================================================

def _post_to_firm_channel(findings: list):
    try:
        fc = _load(FIRM_CHANNEL_PATH, [])
        if not isinstance(fc, list):
            fc = fc.get("messages", []) if isinstance(fc, dict) else []

        for finding in findings:
            sev   = finding.get("severity","MEDIUM")
            emoji = "🚨" if sev == "CRITICAL" else "⚠️" if sev == "HIGH" else "ℹ️"
            fc.append({
                "ts":       datetime.now().isoformat(),
                "date":     datetime.now().strftime("%Y-%m-%d"),
                "agent":    "Logic Officer",
                "category": "COHERENCE_ALERT",
                "priority": sev,
                "content":  (
                    f"{emoji} LOGIC OFFICER — {sev}: {finding['title']}\n\n"
                    f"OBSERVED: {finding['observation']}\n\n"
                    f"WHY THIS IS WRONG: {finding['why_its_wrong']}\n\n"
                    f"SUGGESTED RESOLUTION: {finding['suggested_resolution']}"
                ),
            })

        if len(fc) > 500:
            fc = fc[-500:]
        _save(FIRM_CHANNEL_PATH, fc)
    except Exception as e:
        logger.warning(f"  Could not post to firm channel: {e}")


# =============================================================================
# CHAIRMAN BRIEFING DIGEST
# =============================================================================

def _update_chairman_briefing(findings: list, state: dict):
    """
    Add today's Logic Officer summary to the chairman briefing digest.
    The dashboard reads this file and shows it to Jack on login.
    """
    try:
        briefing = _load(BRIEFING_PATH, {"entries": []})
        if not isinstance(briefing, dict):
            briefing = {"entries": []}
        entries = briefing.get("entries", [])

        portfolio_value = state.get("portfolio_value", 0)
        pnl = portfolio_value - 100000

        entry = {
            "ts":           datetime.now().isoformat(),
            "date":         datetime.now().strftime("%Y-%m-%d"),
            "source":       "Logic Officer",
            "portfolio":    portfolio_value,
            "pnl_vs_start": pnl,
            "canary":       state.get("canary_level", "?"),
            "cro_level":    state.get("cro_level", "?"),
            "picks_today":  state.get("picks_today", 0),
            "anomalies":    len(findings),
            "items":        [
                {
                    "severity": f.get("severity","?"),
                    "title":    f.get("title","?"),
                    "short":    f.get("observation","")[:150],
                }
                for f in findings
            ],
        }
        entries.append(entry)

        # Keep 90 days of entries
        if len(entries) > 90:
            entries = entries[-90:]

        briefing["entries"] = entries
        _save(BRIEFING_PATH, briefing)
    except Exception as e:
        logger.warning(f"  Could not update chairman briefing: {e}")


# =============================================================================
# MAIN RUN
# =============================================================================

def run_logic_officer() -> dict:
    logger.info("Logic Officer — coherence check...")
    state    = _build_state()
    findings = []

    for rule_fn in ALL_RULES:
        try:
            result = rule_fn(state)
            if result:
                findings.append(result)
                sev = result.get("severity","?")
                logger.info(f"  [{sev}] {result.get('title','?')[:70]}")
        except Exception as e:
            logger.warning(f"  Rule {rule_fn.__name__} failed: {e}")

    if not findings:
        logger.info("  No coherence anomalies detected — system logic checks out.")
    else:
        logger.warning(f"  {len(findings)} anomaly(ies) found — {sum(1 for f in findings if f['severity'] in ('HIGH','CRITICAL'))} serious")

    # Persist findings
    prev = _load(FINDINGS_PATH, {"history": []})
    if not isinstance(prev, dict):
        prev = {"history": []}
    prev["history"].append({
        "ts":       datetime.now().isoformat(),
        "findings": findings,
    })
    prev["history"] = prev["history"][-30:]
    prev["latest"]  = findings
    _save(FINDINGS_PATH, prev)

    # Write board proposals for serious anomalies
    n_proposals = _write_board_proposals(findings)
    if n_proposals:
        logger.info(f"  {n_proposals} board proposal(s) submitted for vote")

    # Post to firm channel
    if findings:
        _post_to_firm_channel(findings)

    # Update chairman briefing
    _update_chairman_briefing(findings, state)

    return {
        "findings":        findings,
        "anomalies_found": len(findings),
        "proposals_added": n_proposals,
    }


def print_report():
    data = _load(FINDINGS_PATH, {})
    latest = data.get("latest", [])
    print(f"\n=== LOGIC OFFICER REPORT ===")
    print(f"Last run findings: {len(latest)}")
    if not latest:
        print("  No anomalies detected.")
    for f in latest:
        print(f"\n  [{f.get('severity','?')}] {f.get('title','?')}")
        print(f"  → {f.get('observation','')[:200]}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        result = run_logic_officer()
        print_report()

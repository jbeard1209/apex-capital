#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LOGIC TEAM
# =============================================================================
#
# A dedicated team of coherence-checking agents, structured like the CIT.
# Each sub-agent has a specific domain of "does this make sense?" questions.
# The Logic Team Director synthesises all findings and routes them appropriately.
#
# The Logic Team is distinct from the CIT:
#   CIT        → finds bugs, code quality issues, improvement ideas
#   Logic Team → finds logical inconsistencies, systemic gaps, governance lapses
#
# Sub-agents:
#   PipelineLogicAgent    — Is research actually flowing? Are picks reaching the Manager?
#   CapitalLogicAgent     — Is capital being deployed or sitting idle for bad reasons?
#   GovernanceLogicAgent  — Has the board reviewed things it should have reviewed?
#   PerformanceLogicAgent — Do the numbers make sense? Is the firm learning from results?
#   AgentHealthLogicAgent — Are agent statuses coherent with actual system conditions?
#
# Each sub-agent returns a list of Finding dicts. The Director:
#   1. Collects all findings
#   2. Deduplicates and ranks by severity
#   3. Posts HIGH/CRITICAL findings to the board as proposals
#   4. Updates the chairman briefing digest
#   5. Posts a summary to the firm channel
#
# Usage:
#   python3 logic_team.py              # full team run
#   python3 logic_team.py --report     # print latest findings
#   python3 logic_team.py --agent PipelineLogicAgent  # run one sub-agent
# =============================================================================

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("logic_team")

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
os.chdir(BASE_DIR)

FINDINGS_PATH    = DATA_DIR / "logic_team_findings.json"
BRIEFING_PATH    = DATA_DIR / "chairman_briefing.json"
FIRM_CHANNEL     = DATA_DIR / "firm_channel.json"
CIT_PENDING      = DATA_DIR / "cit_pending_proposals.json"
LT_RESOLUTIONS   = DATA_DIR / "logic_team_resolutions.json"


# =============================================================================
# SHARED DATA LOADER
# =============================================================================

def _load(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default if default is not None else {}


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def _days_since(iso_ts: str) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso_ts)[:19])).days
    except Exception:
        return None


# =============================================================================
# FINDING SCHEMA
# All sub-agents return lists of these dicts.
# =============================================================================

def _finding(rule: str, severity: str, title: str,
             observation: str, why: str, suggestion: str,
             agent: str = "LogicTeam") -> dict:
    return {
        "rule":       rule,
        "agent":      agent,
        "severity":   severity,
        "title":      title,
        "observation": observation,
        "why_wrong":  why,
        "suggestion": suggestion,
        "ts":         datetime.now().isoformat(),
    }


# =============================================================================
# SUB-AGENT 1 — PIPELINE LOGIC
# Is the research pipeline actually producing output?
# =============================================================================

class PipelineLogicAgent:
    name = "PipelineLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        registry = state.get("registry", {})
        agents   = registry.get("agents", []) if isinstance(registry, dict) else []
        active   = [a for a in agents if a.get("status") == "active"]
        canary   = state.get("canary_level", "GREEN")
        cro      = state.get("cro_level", "LOW")
        picks_today = state.get("picks_today", 0)
        days_dark   = state.get("days_since_last_pick", 0) or 0

        # R1: Zero picks despite active agents + non-emergency conditions
        if len(active) >= 5 and canary not in ("RED","ORANGE") and cro != "CRITICAL":
            if days_dark >= 2 and picks_today == 0:
                findings.append(_finding(
                    rule="zero_picks",
                    severity="CRITICAL" if days_dark > 5 else "HIGH",
                    title=f"Research pipeline DARK: {days_dark} days without picks despite {len(active)} active agents",
                    observation=(
                        f"{len(active)} agents active, canary={canary}, CRO={cro}, "
                        f"yet zero picks have reached the Manager in ~{days_dark} days."
                    ),
                    why=(
                        "Active agents in a non-emergency environment should always produce "
                        "candidates. Zero output means the pipeline has a structural break — "
                        "agents not loading, output not routing, or Manager rejecting everything."
                    ),
                    suggestion=(
                        "Check research_team.py status filter (must include 'active' + 'reassigned'). "
                        "Check manager_agent.py BUY gates aren't misconfigured. "
                        "Run a manual research cycle: `python3 -c 'from research_team import ResearchTeamCoordinator; ResearchTeamCoordinator().run()'`"
                    ),
                    agent=self.name,
                ))

        # R2: Research report stale
        rpt_age = state.get("research_report_age_h", 0) or 0
        if rpt_age > 120:  # >5 days
            findings.append(_finding(
                rule="stale_research_report",
                severity="MEDIUM",
                title=f"Research report is {rpt_age:.0f}h old — R&D team may not be running",
                observation=f"research_team_report.json was last updated {rpt_age:.0f} hours ago.",
                why="R&D should run every 3-4 days. A >5-day gap means the research cycle is skipping.",
                suggestion="Check main.py Step 3c R&D cycle gate. Ensure the 72h TTL hasn't been accidentally extended.",
                agent=self.name,
            ))

        # R3: FI pipeline agents exist but FI pipeline not running
        fi_agents_exist = any("fi_" in a.get("file","").lower() or "fi" in a.get("sector","").lower()
                               for a in agents)
        fi_pipeline_age = state.get("fi_pipeline_age_h")
        if fi_agents_exist and (fi_pipeline_age is None or fi_pipeline_age > 48):
            findings.append(_finding(
                rule="fi_pipeline_not_running",
                severity="MEDIUM",
                title="Fixed Income agents exist but FI pipeline hasn't run recently",
                observation=(
                    f"FI research agents are registered but fi_pipeline_report.json is "
                    f"{'missing' if fi_pipeline_age is None else f'{fi_pipeline_age:.0f}h old'}."
                ),
                why="FI agents without a running FI pipeline produce no picks. The FI pod's $30k capital sits idle.",
                suggestion="Verify fi_pipeline.py is scheduled in main.py or on GCP cron. Run manually: `python3 fi_pipeline.py`",
                agent=self.name,
            ))

        return findings


# =============================================================================
# SUB-AGENT 2 — CAPITAL LOGIC
# Is capital being deployed appropriately?
# =============================================================================

class CapitalLogicAgent:
    name = "CapitalLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        pods = state.get("pod_allocations", {}).get("pods", {}) or {}
        cro  = state.get("cro_level", "LOW")
        cro_streak = state.get("cro_streak", 0)

        total_portfolio = state.get("portfolio_value", 0) or 0
        total_idle = 0

        for pod_id, pod in pods.items():
            budget   = pod.get("budget_inception", 0) or 0
            n_pos    = len(pod.get("positions", {}))
            total_v  = pod.get("total_value", 0) or 0

            if budget >= 20000 and n_pos == 0:
                total_idle += total_v

                # R4: Large pod completely idle
                findings.append(_finding(
                    rule=f"idle_pod_{pod_id}",
                    severity="MEDIUM",
                    title=f"{pod.get('name',pod_id)}: ${total_v:,.0f} capital, zero positions",
                    observation=(
                        f"Pod '{pod.get('name',pod_id)}' has a ${budget:,.0f} budget "
                        f"and currently holds ${total_v:,.0f} with zero open positions."
                    ),
                    why=(
                        f"{'CRO at ' + cro + ' may justify caution, but ' if cro in ('HIGH','CRITICAL') else ''}"
                        f"idle capital earns zero return and represents a failure to execute on the pod's mandate."
                    ),
                    suggestion=(
                        f"Board should confirm: is this pod intentionally idle (CRO/model readiness) "
                        f"or is it a pipeline failure? If intentional, document why."
                    ),
                    agent=self.name,
                ))

        # R5: CRO HIGH but portfolio also fully deployed (contradiction)
        n_positions = state.get("n_positions", 0) or 0
        cash_pct    = state.get("cash_pct", 100)
        if cro in ("HIGH","CRITICAL") and cash_pct < 15 and n_positions > 8:
            findings.append(_finding(
                rule="cro_high_fully_deployed",
                severity="HIGH",
                title=f"CRO={cro} but portfolio is {100-cash_pct:.0f}% deployed — risk policy contradiction",
                observation=(
                    f"CRO is at {cro} (risk bulletin says reduce exposure) but "
                    f"the portfolio has {n_positions} positions with only {cash_pct:.0f}% cash."
                ),
                why="CRO HIGH should mean reduced deployment. Being fully invested during HIGH regime is inconsistent with the firm's stated risk policy.",
                suggestion="Board should either step down CRO to MEDIUM/ELEVATED (if conditions warrant) or trim positions to reach the 25% cash floor required at HIGH.",
                agent=self.name,
            ))

        return findings


# =============================================================================
# SUB-AGENT 3 — GOVERNANCE LOGIC
# Has the board reviewed things it should have reviewed?
# =============================================================================

class GovernanceLogicAgent:
    name = "GovernanceLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        cro_level  = state.get("cro_level", "LOW")
        cro_streak = state.get("cro_streak", 0)
        board_rpt  = state.get("board_meeting", {}) or {}
        last_board = board_rpt.get("generated_at", "") or board_rpt.get("timestamp", "")
        days_since_board = _days_since(last_board) or 999

        # R6: Extended CRO HIGH with no board vote
        if cro_level in ("HIGH","CRITICAL") and cro_streak >= 5:
            findings.append(_finding(
                rule="cro_extended_no_review",
                severity="HIGH",
                title=f"CRO={cro_level} for {cro_streak} cycles ({cro_streak*4} days) — board must vote",
                observation=(
                    f"The CRO has been at {cro_level} for {cro_streak} consecutive pipeline cycles "
                    f"(~{cro_streak*4} days). During this time, all new buy orders have been blocked. "
                    f"The board last met {days_since_board} days ago."
                ),
                why=(
                    "An extended buy block is a major business decision. It should require ongoing "
                    "board ratification — not just persist automatically. It penalizes research agents "
                    "for low approved-picks even when they're working correctly."
                ),
                suggestion=(
                    f"Board should formally vote: (1) Is {cro_level} still warranted? "
                    f"(2) If yes, what specific condition triggers a downgrade? "
                    f"(3) If no, step down to ELEVATED with 25% cash floor instead of 40%."
                ),
                agent=self.name,
            ))

        # R7: No board meeting in >7 days despite active flags
        irm_flags = state.get("irm_flags", 0)
        if days_since_board > 7 and irm_flags >= 2:
            findings.append(_finding(
                rule="board_overdue",
                severity="MEDIUM",
                title=f"Board has not met in {days_since_board} days despite {irm_flags} IRM flags",
                observation=(
                    f"Last board meeting was {days_since_board} days ago. "
                    f"The IRM has {irm_flags} active red flag(s). "
                    f"These should have triggered a board session."
                ),
                why="IRM flags that sit unaddressed for >7 days represent a governance failure. The board exists to act on them.",
                suggestion="Trigger a board meeting: `python3 board_meeting.py`. IRM and CRO should be primary agenda items.",
                agent=self.name,
            ))

        # R8: Chairman action required items sitting unresolved >3 days
        chairman_items = state.get("chairman_action_required", []) or []
        if isinstance(chairman_items, dict):
            chairman_items = chairman_items.get("items", [])
        stale_chairman = []
        for item in chairman_items:
            age = _days_since(item.get("created_at","") or item.get("ts",""))
            if age is not None and age > 3 and item.get("status","open") == "open":
                stale_chairman.append(item)
        if stale_chairman:
            findings.append(_finding(
                rule="stale_chairman_actions",
                severity="MEDIUM",
                title=f"{len(stale_chairman)} Chairman action item(s) unresolved for >3 days",
                observation=(
                    f"Items: " + ", ".join(f"'{i.get('title',i.get('action','?'))[:50]}'" for i in stale_chairman[:3])
                ),
                why="Items flagged for Chairman action should be resolved promptly. Stale items indicate a process gap.",
                suggestion="Review data/chairman_action_required.json and action or dismiss each item.",
                agent=self.name,
            ))

        return findings


# =============================================================================
# SUB-AGENT 4 — PERFORMANCE LOGIC
# Do the P&L numbers make sense? Is the firm learning from results?
# =============================================================================

class PerformanceLogicAgent:
    name = "PerformanceLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        portfolio_value = state.get("portfolio_value", 0) or 0
        starting_capital = 100_000
        pnl_pct = ((portfolio_value - starting_capital) / starting_capital * 100) if portfolio_value > 0 else 0

        # R9: Down >3% with no written attribution
        perf_history = state.get("performance_history", {}) or {}
        snapshots    = perf_history.get("snapshots", []) if isinstance(perf_history, dict) else []
        has_attribution = any(s.get("loss_attribution") for s in snapshots)

        if pnl_pct < -3 and not has_attribution:
            findings.append(_finding(
                rule="loss_no_attribution",
                severity="HIGH",
                title=f"Portfolio {pnl_pct:.1f}% below start with no written loss attribution",
                observation=f"Portfolio is at ${portfolio_value:,.0f} (-${abs(portfolio_value - starting_capital):,.0f} from $100k start). No loss attribution is documented.",
                why="Jack explicitly said he wants to know why the firm is down. Undocumented losses mean we can't learn from them or prevent recurrence.",
                suggestion="Logic Officer will write a loss attribution based on available trade history. Board should review and ratify it.",
                agent=self.name,
            ))
        elif pnl_pct < -3 and has_attribution:
            # Attribution exists — check if fixes are in progress
            latest_snap = snapshots[-1] if snapshots else {}
            fixes = latest_snap.get("loss_attribution", {}).get("fixes_applied_today", [])
            if not fixes:
                findings.append(_finding(
                    rule="loss_attribution_no_fixes",
                    severity="MEDIUM",
                    title=f"Loss attributed but no fixes are in progress — just documenting isn't enough",
                    observation=f"Portfolio is {pnl_pct:.1f}% below start. Attribution exists but no corrective actions are logged.",
                    why="Attribution without action is just documentation. The firm needs to actually fix what's causing the loss.",
                    suggestion="Board should approve specific recovery actions: cover QQQ short per Review Agent, deploy idle ML pod capital when CRO permits.",
                    agent=self.name,
                ))

        # R10: ML model exists but live picks aren't being tracked
        ml_picks = state.get("ml_live_picks", {}) or {}
        picks_list = ml_picks.get("picks", []) if isinstance(ml_picks, dict) else []
        if not picks_list:
            findings.append(_finding(
                rule="ml_model_not_tracked",
                severity="MEDIUM",
                title="ML model running but no live picks are being tracked for real-world validation",
                observation="ml_live_picks_log.json has zero entries. The ST model (AUC=0.6347) is scoring picks but results aren't being captured.",
                why="Without tracking real-world picks, we can't validate whether the model's AUC score translates to actual returns. This was explicitly requested by the Chairman.",
                suggestion="Run `python3 ml_live_test_runner.py --enter` on GCP to begin tracking. Evaluation after 20 days.",
                agent=self.name,
            ))

        return findings


# =============================================================================
# SUB-AGENT 5 — AGENT HEALTH LOGIC
# Are agent statuses coherent with what's actually happening?
# =============================================================================

class AgentHealthLogicAgent:
    name = "AgentHealthLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        registry = state.get("registry", {}) or {}
        agents   = registry.get("agents", []) if isinstance(registry, dict) else []
        if not agents:
            return findings

        total    = len(agents)
        active   = [a for a in agents if a.get("status") == "active"]
        retired  = [a for a in agents if a.get("status") == "retired"]
        cro      = state.get("cro_level", "LOW")

        # R11: Mass retirement during CRO HIGH period (unfair penalization)
        if len(retired) > 5:
            # Check if most retirements happened during a CRO HIGH period
            recent_retirements = [a for a in retired if _days_since(a.get("retired_at","")) and _days_since(a.get("retired_at","")) < 60]
            if len(recent_retirements) > 3:
                findings.append(_finding(
                    rule="suspicious_mass_retirement",
                    severity="MEDIUM",
                    title=f"{len(recent_retirements)} agents retired in the last 60 days — likely during CRO HIGH buy block",
                    observation=(
                        f"{len(recent_retirements)} agents were retired recently. "
                        f"CRO has been at {cro} for {state.get('cro_streak',0)} cycles. "
                        f"Agents cannot get approved picks when CRO blocks all buying."
                    ),
                    why=(
                        "Retiring agents during a system-wide buy block is like firing salespeople for not "
                        "closing deals when the store is closed. The CRO exoneration logic prevents new "
                        "retirements, but agents already retired can't be automatically restored."
                    ),
                    suggestion=(
                        "Board should review the {len(recent_retirements)} recently retired agents. "
                        "If their zero-pick cycles coincide with CRO HIGH periods, they should be "
                        "restored (Chairman amnesty) and their specializations re-evaluated."
                    ),
                    agent=self.name,
                ))

        # R12: Agents in PIP but CRO is still HIGH (PIP is unfair)
        pip_agents = [a for a in agents if a.get("status") == "pip"]
        if pip_agents and cro in ("HIGH","CRITICAL"):
            findings.append(_finding(
                rule="pip_during_cro_high",
                severity="MEDIUM",
                title=f"{len(pip_agents)} agent(s) on PIP while CRO={cro} — performance plan is unfair",
                observation=(
                    f"Agents {[a.get('class_name') for a in pip_agents]} are on formal PIPs "
                    f"while CRO is at {cro}, meaning their picks cannot be approved regardless."
                ),
                why="A PIP requires agents to generate approved picks to clear it. If the CRO blocks all buys, no PIP can be cleared — the system traps agents.",
                suggestion="Pause PIP timers for all agents during CRO HIGH/CRITICAL periods. Resume timer when CRO drops to ELEVATED or below.",
                agent=self.name,
            ))

        # R13: Sector coverage gaps (key sectors with no active agent)
        key_sectors = {
            "Technology", "Financials", "Healthcare", "Industrials",
            "Consumer Discretionary", "Materials", "Energy",
        }
        covered = {a.get("sector","").strip() for a in active}
        gaps    = [s for s in key_sectors if not any(s.lower() in c.lower() for c in covered)]
        if gaps:
            findings.append(_finding(
                rule="sector_coverage_gap",
                severity="LOW",
                title=f"Key sectors with no active research agent: {', '.join(gaps)}",
                observation=f"The following important sectors have no active research agent: {', '.join(gaps)}.",
                why="Sectors without active agents produce zero picks from those areas. Missing Technology or Financials in particular is a major research blind spot.",
                suggestion=f"Chief of Staff should spawn new agents for: {', '.join(gaps)}. Use the Agent Factory in chief_of_staff_agent.py.",
                agent=self.name,
            ))

        return findings



# =============================================================================
# SUB-AGENT 6 — STRATEGY LOGIC
# Are we actually pursuing a coherent investment strategy?
# =============================================================================

class StrategyLogicAgent:
    name = "StrategyLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        picks_today   = state.get("picks_today", 0)
        active_agents = state.get("active_agents", 0)
        cro_level     = state.get("cro_level", "LOW")
        cro_streak    = state.get("cro_streak", 0)
        portfolio_val = state.get("portfolio_value", 0) or 0
        positions     = state.get("positions", [])

        # R14: CRO blocking buys for >30 days — strategy incoherence
        if cro_level in ("HIGH", "CRITICAL") and cro_streak >= 8:
            findings.append(_finding(
                rule="cro_paralysis",
                severity="HIGH",
                title=f"CRO at {cro_level} for {cro_streak * 4}+ days — firm has no buy strategy",
                observation=(
                    f"The CRO has blocked all buys for {cro_streak} cycles (~{cro_streak*4} days). "
                    f"The firm currently holds {len(positions)} position(s) and is generating "
                    f"picks it cannot deploy. This is not a risk posture — it is a strategy vacuum."
                ),
                why="A firm that can never buy is not an investment firm. Extended buy blocks "
                    "that persist without board review indicate the risk framework has captured "
                    "the governance framework.",
                suggestion="Board must vote: either (1) accept the current posture with an "
                           "explicit end condition, or (2) define specific criteria to step down "
                           "CRO level. A perpetual HIGH with no exit criteria is not governance.",
                agent=self.name,
            ))

        # R15: No sells either — firm completely inactive
        sell_log = state.get("sell_log", [])
        recent_sells = [s for s in sell_log if isinstance(s, dict) and
                        (datetime.now() - datetime.fromisoformat(s.get("date", "2000-01-01"))).days <= 14
                        ] if sell_log else []
        if len(positions) == 0 and picks_today == 0 and len(recent_sells) == 0:
            findings.append(_finding(
                rule="complete_inactivity",
                severity="MEDIUM",
                title="Firm completely inactive — no positions, no picks, no sells in 14 days",
                observation="The firm has zero open positions, zero picks today, and no sell "
                            "activity in the past 14 days. It is running but not operating.",
                why="A paper trading firm that never trades cannot demonstrate whether its "
                    "strategy works. Complete inactivity produces no learning signal.",
                suggestion="Board should review: is this intentional (market conditions), "
                           "a pipeline failure (CIT should investigate), or an ML/research gap?",
                agent=self.name,
            ))

        return findings


# =============================================================================
# SUB-AGENT 7 — ML MODEL LOGIC
# Are the ML models actually contributing anything?
# =============================================================================

class MLLogicAgent:
    name = "MLLogicAgent"

    def run(self, state: dict) -> list:
        findings = []
        ml_picks  = state.get("ml_live_picks", {})
        picks_arr = ml_picks.get("picks", []) if isinstance(ml_picks, dict) else []
        ml_pod    = next((p for p in state.get("pods", []) if "ml" in p.get("name","").lower() or
                          "quantitative" in p.get("name","").lower()), None)
        auc_st    = state.get("ml_auc_st", 0) or 0

        # R16: ML pod has capital but zero live picks tracked
        if ml_pod and len(picks_arr) == 0:
            pod_budget = ml_pod.get("budget", 0) or 0
            if pod_budget > 0:
                findings.append(_finding(
                    rule="ml_no_live_validation",
                    severity="MEDIUM",
                    title=f"ML pod has ${pod_budget:,.0f} capital but zero live picks logged",
                    observation=f"The ML / Quantitative pod has ${pod_budget:,.0f} allocated "
                                f"but ml_live_picks_log.json is empty. The model is not being "
                                f"validated against real price outcomes.",
                    why="Without live validation, we cannot know if the model's AUC on "
                        "backtest data translates to live performance. The capital allocation "
                        "is speculative without evidence.",
                    suggestion="Ensure ml_live_test_runner.enter_new_picks() is being called "
                                "each pipeline run. Check if the ST model is clearing picks "
                                "through the conviction gate.",
                    agent=self.name,
                ))

        # R17: AUC below random — model is worse than a coin flip
        if 0 < auc_st < 0.50:
            findings.append(_finding(
                rule="ml_below_random",
                severity="HIGH",
                title=f"ST model AUC {auc_st:.3f} — worse than random. Capital at risk.",
                observation=f"The Short-Term model has AUC={auc_st:.3f}, below the 0.50 "
                            f"random baseline. Any capital deployed on these signals is "
                            f"expected to lose money vs. a coin flip.",
                why="An AUC below 0.50 means the model is systematically wrong — worse than "
                    "guessing. This is a governance failure if the model is live.",
                suggestion="Immediately suspend ML-driven buy recommendations until the model "
                           "is retrained. Board should vote on suspension.",
                agent=self.name,
            ))

        return findings

# =============================================================================
# LOGIC TEAM DIRECTOR
# =============================================================================

SUB_AGENTS = [
    PipelineLogicAgent(),
    CapitalLogicAgent(),
    GovernanceLogicAgent(),
    PerformanceLogicAgent(),
    AgentHealthLogicAgent(),
    StrategyLogicAgent(),
    MLLogicAgent(),
]


def _build_state() -> dict:
    """Aggregate all relevant state into one dict for sub-agents."""
    state = {}

    # Registry
    state["registry"] = _load(DATA_DIR / "agent_registry.json", {"agents": []})

    # Canary
    canary = _load(DATA_DIR / "canary_state.json", {})
    state["canary_level"] = canary.get("last_level", "GREEN")

    # CRO
    rb = _load(DATA_DIR / "risk_bulletin.json", {})
    state["cro_level"]  = rb.get("risk_level", "LOW")
    state["cro_streak"] = rb.get("consecutive_high_cycles", 0)

    # Manager decisions
    mgr = _load(DATA_DIR / "manager_decisions.json", [])
    picks_today = 0
    last_pick_date = None
    if isinstance(mgr, list) and mgr:
        last_cycle = mgr[-1] if isinstance(mgr[-1], dict) else {}
        decisions  = last_cycle.get("decisions", [])
        picks_today = sum(1 for d in decisions
                          if d.get("manager_decision") == "APPROVED"
                          and d.get("manager_action") not in ("HOLD","WATCH","SELL","TRIM"))
        for entry in reversed(mgr):
            if isinstance(entry, dict):
                decs = entry.get("decisions", [])
                if any(d.get("manager_decision")=="APPROVED" and d.get("manager_action") not in ("HOLD","WATCH","SELL","TRIM") for d in decs):
                    last_pick_date = entry.get("generated_at","")
                    break
    state["picks_today"]     = picks_today
    state["last_pick_date"]  = last_pick_date
    state["days_since_last_pick"] = _days_since(last_pick_date) if last_pick_date else 999

    # Research report age
    import os as _os, time as _time
    rpt_path = DATA_DIR / "research_team_report.json"
    if rpt_path.exists():
        age_h = (_time.time() - rpt_path.stat().st_mtime) / 3600
        state["research_report_age_h"] = age_h

    # FI pipeline age
    fi_path = DATA_DIR / "fi_pipeline_report.json"
    if fi_path.exists():
        state["fi_pipeline_age_h"] = (_time.time() - fi_path.stat().st_mtime) / 3600
    else:
        state["fi_pipeline_age_h"] = None

    # Pods
    state["pod_allocations"] = _load(DATA_DIR / "pod_allocations.json", {})

    # Portfolio
    review = _load(DATA_DIR / "review_recommendations.json", {})
    acct   = review.get("account", {}) if isinstance(review, dict) else {}
    state["portfolio_value"] = acct.get("portfolio_value", 0)
    state["n_positions"]     = acct.get("n_positions", 0)
    pv = state["portfolio_value"]
    cash = acct.get("cash", 0)
    state["cash_pct"] = (cash / pv * 100) if pv > 0 else 100

    # IRM
    irm = _load(DATA_DIR / "irm_daily_check.json", {})
    bp  = irm.get("board_panel", {}) if isinstance(irm, dict) else {}
    state["irm_flags"] = len(bp.get("combined_red_flags", []))

    # Board meeting
    state["board_meeting"] = _load(DATA_DIR / "board_meeting_report.json", {})

    # Chairman action required
    state["chairman_action_required"] = _load(DATA_DIR / "chairman_action_required.json", [])

    # Performance history
    state["performance_history"] = _load(DATA_DIR / "performance_history.json", {})

    # ML live picks
    state["ml_live_picks"] = _load(DATA_DIR / "ml_live_picks_log.json", {})

    # Pods as list (for MLLogicAgent / StrategyLogicAgent)
    pod_alloc = state.get("pod_allocations", {})
    raw_pods  = pod_alloc.get("pods", {}) if isinstance(pod_alloc, dict) else {}
    state["pods"] = [{"name": k, **v} for k, v in raw_pods.items()] if isinstance(raw_pods, dict) else []

    # ML AUC scores
    try:
        import glob, os as _os
        auc_files = sorted(glob.glob(str(DATA_DIR / "ml_model_report*.json")))
        if auc_files:
            ml_rpt = _load(Path(auc_files[-1]), {})
            state["ml_auc_st"] = ml_rpt.get("shortterm", {}).get("auc", 0) or                                   ml_rpt.get("auc_roc", 0) or 0
        else:
            state["ml_auc_st"] = 0
    except Exception:
        state["ml_auc_st"] = 0

    # Sell log (for StrategyLogicAgent)
    review_data = _load(DATA_DIR / "review_recommendations.json", {})
    state["sell_log"]  = review_data.get("sell_log", []) if isinstance(review_data, dict) else []
    state["positions"] = review_data.get("positions", []) if isinstance(review_data, dict) else []

    return state


def _post_findings_to_channel(findings: list):
    try:
        fc = _load(FIRM_CHANNEL, [])
        if not isinstance(fc, list):
            fc = fc.get("messages", []) if isinstance(fc, dict) else []

        by_severity = {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []}
        for f in findings:
            sev = f.get("severity","LOW")
            by_severity.setdefault(sev, []).append(f)

        critical_high = by_severity["CRITICAL"] + by_severity["HIGH"]
        if critical_high:
            summary_lines = [f"  [{f['severity']}] {f['title']}" for f in critical_high]
            fc.append({
                "ts":       datetime.now().isoformat(),
                "date":     datetime.now().strftime("%Y-%m-%d"),
                "agent":    "Logic Team Director",
                "category": "COHERENCE_ALERT",
                "priority": "HIGH",
                "content":  (
                    f"🚨 LOGIC TEAM — {len(critical_high)} HIGH/CRITICAL anomaly(ies) detected:\n"
                    + "\n".join(summary_lines)
                    + "\n\nBoard proposals submitted. See data/logic_team_findings.json for full detail."
                ),
            })

        medium_low = by_severity["MEDIUM"] + by_severity["LOW"]
        if medium_low:
            summary_lines = [f"  [{f['severity']}] {f['title']}" for f in medium_low]
            fc.append({
                "ts":       datetime.now().isoformat(),
                "date":     datetime.now().strftime("%Y-%m-%d"),
                "agent":    "Logic Team Director",
                "category": "COHERENCE_NOTE",
                "priority": "NORMAL",
                "content":  (
                    f"ℹ️ LOGIC TEAM — {len(medium_low)} MEDIUM/LOW note(s):\n"
                    + "\n".join(summary_lines)
                ),
            })

        if len(fc) > 500:
            fc = fc[-500:]
        _save(FIRM_CHANNEL, fc)
    except Exception as e:
        logger.warning(f"  Firm channel post failed: {e}")


def _submit_board_proposals(findings: list) -> int:
    serious = [f for f in findings if f.get("severity") in ("HIGH","CRITICAL")]
    if not serious:
        return 0

    pending = _load(CIT_PENDING, [])
    if not isinstance(pending, list):
        pending = []
    existing_titles = {p.get("title","") for p in pending}
    added = 0

    for f in serious:
        if f.get("title","") in existing_titles:
            continue
        pending.append({
            "id":           f"LT-{datetime.now().strftime('%H%M%S')}-{f['rule']}",
            "source":       f"Logic Team / {f.get('agent','?')}",
            "category":     "IMPROVEMENT",
            "severity":     f["severity"],
            "confidence":   88,
            "title":        f["title"],
            "description":  f["observation"],
            "evidence":     f"Logic Team coherence check. Rule={f['rule']}. Agent={f.get('agent')}",
            "proposed_fix": f["suggestion"],
            "why":          f["why_wrong"],
            "created_at":   datetime.now().isoformat(),
            "status":       "pending",
        })
        existing_titles.add(f["title"])
        added += 1

    if added:
        _save(CIT_PENDING, pending)
    return added


def _update_chairman_briefing(findings: list, state: dict):
    try:
        briefing = _load(BRIEFING_PATH, {"entries": []})
        if not isinstance(briefing, dict):
            briefing = {"entries": []}
        entries = briefing.get("entries", [])

        entry = {
            "ts":           datetime.now().isoformat(),
            "date":         datetime.now().strftime("%Y-%m-%d"),
            "source":       "Logic Team",
            "portfolio":    state.get("portfolio_value", 0),
            "pnl_vs_start": (state.get("portfolio_value",0) or 0) - 100000,
            "canary":       state.get("canary_level","?"),
            "cro_level":    state.get("cro_level","?"),
            "picks_today":  state.get("picks_today",0),
            "anomalies":    len(findings),
            "items": [
                {"severity": f.get("severity","?"), "title": f.get("title","?"),
                 "agent": f.get("agent","?"), "short": f.get("observation","")[:120]}
                for f in sorted(findings, key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(x.get("severity","LOW")))
            ],
        }
        entries.append(entry)
        briefing["entries"] = entries[-90:]
        _save(BRIEFING_PATH, briefing)
    except Exception as e:
        logger.warning(f"  Briefing update failed: {e}")



def _escalate_critical_to_irm(findings: list) -> int:
    """Email Chairman immediately for any CRITICAL finding — bypasses daily digest."""
    critical = [f for f in findings if f.get("severity") == "CRITICAL"]
    if not critical:
        return 0
    escalated = 0
    for f in critical:
        try:
            from email_sender import send_alert_email
            subject = f"🚨 LOGIC TEAM CRITICAL: {f.get('title','?')[:60]}"
            body = (
                f"<h2>Logic Team — CRITICAL Coherence Alert</h2>"
                f"<p><b>Rule:</b> {f.get('rule','?')}<br>"
                f"<b>Agent:</b> {f.get('agent','?')}<br>"
                f"<b>Detected:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>"
                f"<h3>{f.get('title','?')}</h3>"
                f"<p>{f.get('observation','?')}</p>"
                f"<h4>Why this matters:</h4><p>{f.get('why_wrong','?')}</p>"
                f"<h4>Suggested resolution:</h4><p>{f.get('suggestion','?')}</p>"
                f"<hr><p><i>This alert was sent immediately — it did not wait for the daily digest.</i></p>"
            )
            send_alert_email(subject=subject, body=body)
            escalated += 1
            logger.warning(f"  🚨 CRITICAL escalated to Chairman: {f.get('title','?')[:60]}")
        except Exception as e:
            logger.warning(f"  CRITICAL escalation failed: {e}")
    return escalated


def _update_resolution_tracker(findings: list):
    """
    Maintain a lifecycle record for each finding: open → board_voted → resolved.
    Findings are matched by rule+agent. When the same rule stops firing, it's
    auto-closed after 3 consecutive clean runs.
    """
    try:
        tracker = _load(LT_RESOLUTIONS, {"open": {}, "resolved": [], "run_count": 0})
        if not isinstance(tracker, dict):
            tracker = {"open": {}, "resolved": [], "run_count": 0}

        tracker["run_count"] = tracker.get("run_count", 0) + 1
        open_findings = tracker.get("open", {})
        resolved      = tracker.get("resolved", [])

        # Current finding keys
        current_keys = set()
        for f in findings:
            key = f"{f.get('agent','?')}::{f.get('rule','?')}"
            current_keys.add(key)
            if key not in open_findings:
                open_findings[key] = {
                    "first_seen":   datetime.now().isoformat(),
                    "last_seen":    datetime.now().isoformat(),
                    "severity":     f.get("severity","?"),
                    "title":        f.get("title","?"),
                    "agent":        f.get("agent","?"),
                    "rule":         f.get("rule","?"),
                    "times_seen":   1,
                    "clean_runs":   0,
                    "status":       "open",
                }
            else:
                open_findings[key]["last_seen"]  = datetime.now().isoformat()
                open_findings[key]["times_seen"] += 1
                open_findings[key]["clean_runs"] = 0  # reset — still firing

        # Increment clean_runs for findings that didn't fire this cycle
        for key in list(open_findings.keys()):
            if key not in current_keys:
                open_findings[key]["clean_runs"] = open_findings[key].get("clean_runs", 0) + 1
                # Auto-close after 3 consecutive clean runs
                if open_findings[key]["clean_runs"] >= 3:
                    entry = open_findings.pop(key)
                    entry["resolved_at"]   = datetime.now().isoformat()
                    entry["resolution"]    = "auto_closed: 3 consecutive clean runs"
                    resolved.append(entry)
                    logger.info(f"  ✅ Finding auto-resolved: {entry.get('title','?')[:60]}")

        tracker["open"]     = open_findings
        tracker["resolved"] = resolved[-200:]  # keep last 200 resolutions
        tracker["last_run"] = datetime.now().isoformat()
        _save(LT_RESOLUTIONS, tracker)

        open_count     = len(open_findings)
        resolved_count = len(resolved)
        logger.info(f"  Resolution tracker: {open_count} open, {resolved_count} resolved lifetime")
    except Exception as e:
        logger.warning(f"  Resolution tracker update failed: {e}")


def get_open_findings_summary() -> list:
    """Return currently open (unresolved) findings for dashboard display."""
    tracker = _load(LT_RESOLUTIONS, {})
    open_findings = tracker.get("open", {})
    result = []
    for key, entry in sorted(open_findings.items(),
                             key=lambda x: ["CRITICAL","HIGH","MEDIUM","LOW"].index(
                                 x[1].get("severity","LOW"))):
        result.append({
            "key":        key,
            "severity":   entry.get("severity","?"),
            "title":      entry.get("title","?"),
            "first_seen": entry.get("first_seen","?")[:10],
            "times_seen": entry.get("times_seen", 1),
            "days_open":  (datetime.now() - datetime.fromisoformat(
                entry.get("first_seen", datetime.now().isoformat()))).days,
        })
    return result


def run_logic_team(agent_name: str = None) -> dict:
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — LOGIC TEAM")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    state    = _build_state()
    findings = []

    for agent in SUB_AGENTS:
        if agent_name and agent.name != agent_name:
            continue
        try:
            agent_findings = agent.run(state)
            for f in agent_findings:
                logger.info(f"  [{agent.name[:20]:20s}] [{f.get('severity','?'):8s}] {f.get('title','?')[:55]}")
            findings.extend(agent_findings)
        except Exception as e:
            logger.warning(f"  {agent.name} error: {e}")

    if not findings:
        logger.info("  No coherence anomalies — all systems logically consistent ✓")
    else:
        logger.warning(f"  {len(findings)} anomaly(ies) found across {len(SUB_AGENTS)} agents")

    # Save findings
    history = _load(FINDINGS_PATH, {"history": []})
    if not isinstance(history, dict):
        history = {"history": []}
    history["history"].append({"ts": datetime.now().isoformat(), "findings": findings})
    history["history"] = history["history"][-30:]
    history["latest"]  = findings
    _save(FINDINGS_PATH, history)

    # Submit serious ones as board proposals
    n_submitted = _submit_board_proposals(findings)
    if n_submitted:
        logger.info(f"  {n_submitted} board proposal(s) submitted")

    # CRITICAL findings → immediate IRM escalation (bypass daily digest)
    n_escalated = _escalate_critical_to_irm(findings)

    # Post summary to firm channel
    if findings:
        _post_findings_to_channel(findings)

    # Update chairman briefing
    _update_chairman_briefing(findings, state)

    # Update resolution lifecycle tracker
    _update_resolution_tracker(findings)

    # Trigger implementation agent to act on open findings
    try:
        import logic_team_implementation_agent as _ltia
        _ltia_result = _ltia.run_triage()
        if _ltia_result.get("actions_taken", 0) > 0:
            logger.info(f"  Implementation agent: {_ltia_result['actions_taken']} action(s) taken")
    except Exception as _ltia_e:
        logger.debug(f"  Implementation agent skipped: {_ltia_e}")

    return {
        "agents_run":    len(SUB_AGENTS),
        "findings":      len(findings),
        "critical_high": sum(1 for f in findings if f.get("severity") in ("HIGH","CRITICAL")),
        "proposals":     n_submitted,
        "escalated":     n_escalated,
        "detail":        findings,
    }


# =============================================================================
# CLI
# =============================================================================

def print_report():
    data   = _load(FINDINGS_PATH, {})
    latest = data.get("latest", [])
    print(f"\n{'='*60}")
    print(f"  LOGIC TEAM REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    if not latest:
        print("  No anomalies on record.")
        return
    for sev in ("CRITICAL","HIGH","MEDIUM","LOW"):
        items = [f for f in latest if f.get("severity") == sev]
        if items:
            print(f"\n  {sev}:")
            for f in items:
                print(f"    [{f.get('agent','?')[:20]}] {f.get('title','?')}")
                print(f"    → {f.get('observation','')[:120]}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--agent", type=str, default=None,
                        help="Run only one sub-agent by class name")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        run_logic_team(agent_name=args.agent)
        print_report()

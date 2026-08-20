# =============================================================================
# APEX CAPITAL — HUMAN RESOURCES TEAM
# =============================================================================
#
# Two HR officers with distinct mandates and mutual accountability.
# Both report to the Chief of Staff, who escalates material findings to the board.
#
#   HR Performance Officer (HRPO)
#   ─────────────────────────────
#   Quantitative mandate. Tracks every agent against measurable output goals:
#   picks generated, approval rates, conviction scores, output completeness,
#   execution accuracy. Issues performance notices when goals are missed.
#   Evaluates the HR Development Officer's report for leniency bias.
#
#   HR Development Officer (HRDO)
#   ─────────────────────────────
#   Qualitative mandate. Tracks improvement trajectory, cultural alignment
#   (culture of learning, pursuit of knowledge), thesis sharpness over time,
#   and whether underperforming agents are actually developing or stagnating.
#   Evaluates the HR Performance Officer's report for excessive harshness.
#
# MUTUAL ACCOUNTABILITY
#   Each officer reads the other's evaluation report and files a cross-audit.
#   If HRPO scores agents harshly across the board, HRDO flags it.
#   If HRDO's qualitative scores are uniformly positive despite poor output,
#   HRPO flags it. Both cross-audit scores are visible to the CoS and board.
#
# PERFORMANCE GOALS
#   Goals are tiered by agent type and reviewed on a rolling 30-day basis.
#   They are defined in AGENT_PERFORMANCE_GOALS below and can be updated
#   by the Co-Chairmen at any time. Goals auto-generate for newly spawned
#   agents based on their tier and specialization.
#
# OUTPUT
#   data/hr_report.json   — structured findings for CoS and board
#
# Usage:
#   python3 hr_agent.py
#   python3 hr_agent.py --no-email
# =============================================================================

import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hr_agent")

HR_REPORT_PATH      = "data/hr_report.json"
HR_IRM_SEALED_PATH  = "data/hr_irm_sealed.json"   # sealed — IRM never reads this
HR_HISTORY_PATH     = "data/hr_history.json"
CIT_HR_MEMO_PATH    = "data/cit_hr_memo.json"     # CIT context channel — read before evaluating streaks

# The IRM is evaluated by HR but the evaluation is sealed from him.
# It is written to HR_IRM_SEALED_PATH as a base64-encoded blob and passed
# through the IRM's email pipeline opaquely — IRM never sees the contents.
IRM_CLASS_NAME = "IndependentRiskManager"
REGISTRY_PATH    = "data/agent_registry.json"
MANAGER_LOG      = "data/manager_decisions.json"
RESEARCH_RPT     = "data/research_team_report.json"
SYSTEM_PERF_RPT  = "data/rd_system_performance.json"
REVIEW_RPT       = "data/review_recommendations.json"
RISK_RPT         = "data/risk_bulletin.json"

# How many daily cycles constitute a review window
REVIEW_WINDOW_CYCLES = 7   # rolling 7-cycle (~4 weeks) window for trend analysis


# =============================================================================
# PERFORMANCE GOALS
# =============================================================================
# Goals are defined per agent type. The HR team uses these to evaluate whether
# agents are meeting expectations. Co-Chairmen can update any goal at any time.
#
# Goal keys:
#   picks_per_cycle      — minimum approved picks expected per research cycle
#   approval_rate_pct    — minimum % of submitted picks approved by Manager
#   conviction_avg_min   — minimum average conviction score (1–5 scale)
#   output_completeness  — for context/infrastructure agents: must produce output (bool)
#   output_timeliness    — output must be produced within 1 pipeline run (bool)
#   zero_miss_tolerance  — max consecutive zero-output cycles before NOTICE
#   review_period        — "weekly" | "monthly" | "per_cycle"
#   weight               — how heavily this agent type is weighted in firm health
# =============================================================================

AGENT_PERFORMANCE_GOALS: dict = {

    # ── Research agents (pick-generating) ────────────────────────────────
    "_research_default": {
        "picks_per_cycle":     1,      # at least 1 approved pick per R&D cycle
        "approval_rate_pct":   30.0,   # 30% of submitted picks approved
        "conviction_avg_min":  3.0,    # average conviction ≥ 3 stars
        "zero_miss_tolerance": 4,      # notice after 4 zero-pick cycles
        "review_period":       "per_cycle",
        "weight":              1.0,
        "notes": (
            "Research agents are the firm's alpha generation engine. "
            "Consistent output is expected. Occasional dry spells are normal "
            "in defensive market regimes — the HR team accounts for CRO risk level "
            "when assessing performance."
        ),
    },

    # ── Context agents (permanent R&D layer) ─────────────────────────────
    "HistoricalContextAnalyst": {
        "output_completeness":  True,
        "output_timeliness":    True,
        "min_findings":         2,     # at least 2 historical analogues identified
        "zero_miss_tolerance":  1,     # context must be fresh every cycle
        "review_period":        "per_cycle",
        "weight":               1.5,
        "notes": "Context agents must produce output every cycle — no exceptions.",
    },
    "GeopoliticalRiskAnalyst": {
        "output_completeness":  True,
        "output_timeliness":    True,
        "min_findings":         1,
        "zero_miss_tolerance":  1,
        "review_period":        "per_cycle",
        "weight":               1.5,
        "notes": "Geopolitical context is time-sensitive — staleness is a failure.",
    },
    "MacroPolicyHistorian": {
        "output_completeness":  True,
        "output_timeliness":    True,
        "min_findings":         1,
        "zero_miss_tolerance":  1,
        "review_period":        "per_cycle",
        "weight":               1.5,
        "notes": "Policy regime context underpins every sector allocation decision.",
    },
    "SystemPerformanceAnalyst": {
        "output_completeness":  True,
        "output_timeliness":    True,
        "min_findings":         0,     # clean bill of health is also a valid output
        "zero_miss_tolerance":  1,
        "review_period":        "per_cycle",
        "weight":               2.0,   # highest weight — system health is foundational
        "notes": (
            "The SystemPerformanceAnalyst must run every cycle without exception. "
            "A missed run is itself a system performance failure."
        ),
    },

    # ── Infrastructure agents ─────────────────────────────────────────────
    "ManagerAgent": {
        "execution_fill_rate_pct": 95.0,   # ≥95% of submitted orders filled
        "stale_sell_tolerance":    0,       # zero tolerance for unexecuted SELL signals
        "stop_enforcement":        True,    # stop-losses must be enforced same cycle
        "zero_miss_tolerance":     0,
        "review_period":           "per_cycle",
        "weight":                  3.0,
        "notes": (
            "The Manager Agent is the execution layer. Missed exits or unexecuted "
            "stop-losses directly cost the firm money. Zero tolerance for stale signals."
        ),
    },
    "ChiefOfStaff": {
        "briefing_completeness":   True,
        "factory_agent_per_3_cycles": 1,   # at least 1 new quality agent per 3 cycles
        "review_period":           "per_cycle",
        "weight":                  2.0,
        "notes": "CoS must produce a complete briefing every cycle and maintain R&D expansion.",
    },
    "RiskManager": {
        "bulletin_completeness":   True,
        "cvar_non_zero_when_positions": True,
        "review_period":           "per_cycle",
        "weight":                  2.5,
        "notes": "CRO bulletin must be complete and contain non-zero risk metrics when positions are held.",
    },
    "IndependentRiskManager": {
        "independence_flag":       True,   # must run regardless of email flag
        "review_period":           "per_cycle",
        "weight":                  2.5,
        "notes": "IRM must run independently every cycle — that is its entire purpose.",
    },

    # ── Continuous Improvement Team ──────────────────────────────────────
    "CITDirector": {
        "report_completeness":      True,    # must produce morning + evening reports
        "min_proposals_per_session": 3,       # at least 3 proposals per session
        "sensitivity_filter_active": True,    # sensitive findings must route correctly
        "zero_miss_tolerance":       1,       # one missed session triggers NOTICE
        "review_period":             "per_session",
        "weight":                    2.0,
        "notes": (
            "The CIT Director orchestrates the firm's self-improvement loop. "
            "Output completeness and proposal volume are the primary KPIs. "
            "Missed sessions or empty reports are a NOTICE-level failure."
        ),
    },
    "CITCodeInspector": {
        "output_completeness":      True,
        "min_findings":              1,       # must surface ≥1 finding per session
        "zero_miss_tolerance":       1,
        "review_period":             "per_session",
        "weight":                    1.5,
        "notes": (
            "The Code Inspector is the firm's automated code-quality layer. "
            "Silent runs (zero findings) when the codebase has active issues "
            "indicate the agent is not working correctly."
        ),
    },
    "CITPipelineMonitor": {
        "output_completeness":      True,
        "min_findings":              0,       # clean bill is valid — pipeline health is the goal
        "zero_miss_tolerance":       1,
        "review_period":             "per_session",
        "weight":                    1.5,
        "notes": (
            "The Pipeline Monitor watches file freshness, log errors, and backup health. "
            "A zero-finding report is acceptable when the pipeline is genuinely healthy."
        ),
    },
    "CITStrategyAnalyst": {
        "output_completeness":      True,
        "min_findings":              1,
        "zero_miss_tolerance":       2,       # strategy patterns take time to emerge
        "review_period":             "per_session",
        "weight":                    1.5,
        "notes": (
            "The Strategy Analyst surfaces win/loss patterns, rejection rates, and "
            "research-to-trade conversion. Occasional dry spells in volatile regimes "
            "are acceptable — two consecutive zero-finding sessions is a NOTICE."
        ),
    },
    "CITArchitectureAdvisor": {
        "output_completeness":      True,
        "min_findings":              1,
        "zero_miss_tolerance":       2,
        "review_period":             "per_session",
        "weight":                    1.5,
        "notes": (
            "The Architecture Advisor identifies structural gaps: missing circuit breakers, "
            "attribution gaps, test coverage. A zero-finding session may mean the "
            "firm has addressed its backlog — but two in a row warrants a NOTICE."
        ),
    },

    # ── HR Team (self-assessment) ─────────────────────────────────────────
    "HRPerformanceOfficer": {
        "report_completeness":     True,
        "cross_audit_filed":       True,   # must file cross-audit of HRDO
        "review_period":           "per_cycle",
        "weight":                  1.0,
        "notes": "HRPO must produce a full report and file a cross-audit of HRDO every cycle.",
    },
    "HRDevelopmentOfficer": {
        "report_completeness":     True,
        "cross_audit_filed":       True,   # must file cross-audit of HRPO
        "review_period":           "per_cycle",
        "weight":                  1.0,
        "notes": "HRDO must produce a full report and file a cross-audit of HRPO every cycle.",
    },
}


# =============================================================================
# UTILITIES
# =============================================================================

def _safe_load(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_hr_history() -> dict:
    h = _safe_load(HR_HISTORY_PATH)
    return h if h else {"agent_records": {}, "cycles_evaluated": 0}


def _save_hr_history(history: dict):
    os.makedirs("data", exist_ok=True)
    with open(HR_HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2, default=str)


def _get_goals(agent_class: str) -> dict:
    """Return goals for a specific agent, falling back to research defaults."""
    if agent_class in AGENT_PERFORMANCE_GOALS:
        return AGENT_PERFORMANCE_GOALS[agent_class]
    return AGENT_PERFORMANCE_GOALS["_research_default"]


# =============================================================================
# HR PERFORMANCE OFFICER — quantitative evaluation
# =============================================================================

class HRPerformanceOfficer:
    """
    Quantitative performance tracking. Measures every agent against
    their defined output goals: picks, approval rates, execution metrics,
    output completeness. Issues PASS / WATCH / NOTICE / CONCERN ratings.
    """

    def evaluate_all_agents(
        self,
        registry: dict,
        manager_data,
        research_data: dict,
        system_perf: dict,
    ) -> dict:
        """Evaluate all registered agents. Returns structured ratings."""
        agents   = registry.get("agents", [])
        ratings  = {}
        today    = datetime.now().strftime("%Y-%m-%d")

        # ── Pull approved picks per source agent ──────────────────────────
        cycles = manager_data if isinstance(manager_data, list) else [manager_data]
        approved_by_agent: dict[str, int] = {}
        submitted_by_agent: dict[str, int] = {}
        for cycle in cycles[-REVIEW_WINDOW_CYCLES:]:
            for d in cycle.get("decisions", []):
                sa = d.get("source_agent", "unknown")
                submitted_by_agent[sa] = submitted_by_agent.get(sa, 0) + 1
                if (d.get("manager_decision") == "APPROVED"
                        and d.get("manager_action") not in ("HOLD", "WATCH")):
                    approved_by_agent[sa] = approved_by_agent.get(sa, 0) + 1

        # ── Check context agent outputs ───────────────────────────────────
        context_outputs = {
            "HistoricalContextAnalyst": os.path.exists("data/rd_historical_context.json"),
            "GeopoliticalRiskAnalyst":  os.path.exists("data/rd_geopolitical_context.json"),
            "MacroPolicyHistorian":     os.path.exists("data/rd_geopolitical_context.json"),
            "SystemPerformanceAnalyst": os.path.exists("data/rd_system_performance.json"),
        }

        # ── Load CIT reports for sub-agent evaluation ─────────────────────
        cit_morning = _safe_load("data/cit_morning_report.json")
        cit_evening = _safe_load("data/cit_evening_report.json")
        # Pick the most recent non-empty report for metric extraction
        _cit_latest = cit_evening if cit_evening.get("generated_at") else cit_morning
        def _cit_count(val):
            return val if isinstance(val, int) else len(val) if val else 0
        cit_proposals_total = (
            _cit_count(_cit_latest.get("board_proposals", 0))
            + _cit_count(_cit_latest.get("backlog", 0))
        )
        # Freshness: Director should have run within 14 hours (twice-daily cadence)
        cit_is_fresh = False
        _cit_gen = _cit_latest.get("generated_at", "")
        if _cit_gen:
            try:
                _cit_age_h = (datetime.now() - datetime.fromisoformat(str(_cit_gen)[:19])).total_seconds() / 3600
                cit_is_fresh = _cit_age_h < 14
            except Exception:
                pass

        # ── Manager Agent: check for stale exits ─────────────────────────
        review_recs   = _safe_load(REVIEW_RPT)
        recs_list     = review_recs if isinstance(review_recs, list) else review_recs.get("recommendations", [])
        stale_sells   = [
            r for r in recs_list
            if r.get("action") == "SELL"
            and (
                (r.get("unrealized_pnl_pct", 0) or 0) < -7.0
                or (r.get("unrealized_pnl_pct", 0) or 0) > 25.0
            )
        ]

        # ── CRO: bulletin completeness ────────────────────────────────────
        risk_bulletin   = _safe_load(RISK_RPT)
        cro_complete    = bool(risk_bulletin.get("risk_level"))
        cvar_non_zero   = (risk_bulletin.get("cvar_95") or 0) != 0

        # ── CIT HR memo: exoneration context for zero-pick agents ─────────
        # CIT cross-references canary + CRO + sector assignments and writes
        # this memo so HR doesn't penalise agents for market-condition blockages.
        cit_memo = _safe_load(CIT_HR_MEMO_PATH)
        exonerated_classes = {
            e.get("class_name") for e in cit_memo.get("exonerated", [])
        }
        cit_buys_blocked = cit_memo.get("buys_blocked", False)
        cit_canary       = cit_memo.get("canary_level", "UNKNOWN")
        cit_cro          = cit_memo.get("cro_level", "UNKNOWN")

        if cit_memo:
            logger.info(
                f"  [HRPO] CIT memo loaded — canary={cit_canary}, CRO={cit_cro}, "
                f"buys_blocked={cit_buys_blocked}, exonerated agents: {len(exonerated_classes)}"
            )

        for agent in agents:
            cn     = agent.get("class_name", "?")
            status = agent.get("status", "active")
            goals  = _get_goals(cn)

            rating  = "PASS"
            issues  = []
            metrics = {}

            # ── CIT Sub-agents evaluated separately below; skip here ──────
            # CIT sub-agents have output_completeness=True but their findings
            # are embedded in the CIT Director report, not individual output files.
            # Evaluating them via context_outputs gives a false CONCERN every cycle.
            _CIT_SUB_AGENTS = frozenset({
                "CITCodeInspector", "CITPipelineMonitor",
                "CITStrategyAnalyst", "CITArchitectureAdvisor",
            })

            # ── Context agents ────────────────────────────────────────────
            if goals.get("output_completeness") and cn not in _CIT_SUB_AGENTS:
                produced = context_outputs.get(cn, False)
                metrics["output_produced"] = produced
                if not produced:
                    rating = "CONCERN"
                    issues.append(f"No output file found — agent did not run this cycle.")

            # ── Research agents (pick-generating) ────────────────────────
            elif "picks_per_cycle" in goals:
                agent_key  = cn.lower().replace("agent", "").strip("_")
                approved   = sum(v for k, v in approved_by_agent.items()
                                 if agent_key in k.lower())
                submitted  = sum(v for k, v in submitted_by_agent.items()
                                 if agent_key in k.lower())

                approval_rate = (approved / submitted * 100) if submitted > 0 else 0
                czc           = agent.get("consecutive_zero_cycles", 0)
                target_picks  = goals["picks_per_cycle"]
                target_rate   = goals["approval_rate_pct"]
                tolerance     = goals["zero_miss_tolerance"]

                # ── WHOLE-PICTURE CONTEXT CHECK (Chairman directive 2026-06-09) ──
                # Before issuing ANY performance flag for a research agent, HR must
                # consider the full operating environment:
                #   (a) CRO level — HIGH/CRITICAL blocks all buys system-wide
                #   (b) Canary level — ORANGE/RED further constrains deployment
                #   (c) Low submitted count — if the agent submitted picks but
                #       the Manager rejected them all, that is NOT a research failure;
                #       it is a risk-regime outcome. Rejection ≠ underperformance.
                #   (d) Sector conditions — some sectors legitimately have no
                #       buy opportunities for extended periods (value investing,
                #       fixed income in tight-spread environments, etc.)
                #
                # Research agents should NEVER be penalized purely for low approved
                # picks when the CRO is HIGH or canary is ORANGE/RED. They should
                # also not be penalized when they ARE submitting picks but the
                # Manager is rejecting them for macro/risk reasons beyond the
                # agent's control.
                #
                # The performance question HR should ask is:
                #   "Is this agent TRYING and the environment is blocking them?"
                #   vs. "Is this agent genuinely not producing research?"
                _whole_picture_exonerated = False
                _whole_picture_reason = ""

                if cit_buys_blocked or cit_cro in ("HIGH", "CRITICAL"):
                    _whole_picture_exonerated = True
                    _whole_picture_reason = (
                        f"CRO={cit_cro}, buys_blocked={cit_buys_blocked} — "
                        f"system-wide constraints prevent approved picks regardless of research quality."
                    )
                elif submitted > 0 and approved == 0:
                    # Agent submitted picks but ALL were rejected — this is a
                    # Manager/CRO decision, not a research failure.
                    _whole_picture_exonerated = True
                    _whole_picture_reason = (
                        f"Agent submitted {submitted} pick(s) but 0 were approved — "
                        f"rejections were at Manager/CRO level, not research level. "
                        f"Agent is actively producing research."
                    )
                elif cit_canary in ("ORANGE", "RED"):
                    _whole_picture_exonerated = True
                    _whole_picture_reason = (
                        f"Canary={cit_canary} — risk posture restricts capital deployment. "
                        f"Research quality cannot be fairly assessed in this environment."
                    )

                metrics.update({
                    "approved_picks":       approved,
                    "submitted":            submitted,
                    "approval_rate_pct":    round(approval_rate, 1),
                    "consecutive_zero":     czc,
                    "target_picks":         target_picks,
                    "target_approval_rate": target_rate,
                })

                if _whole_picture_exonerated:
                    # Whole-picture context: environment prevents approved picks.
                    # Never flag a research agent when the system is blocking them
                    # or when the Manager is rejecting picks for macro/risk reasons.
                    metrics["whole_picture_exonerated"] = True
                    metrics["whole_picture_reason"]     = _whole_picture_reason
                    issues.append(
                        f"[WHOLE-PICTURE PASS] {czc} zero-pick cycles — "
                        f"context: {_whole_picture_reason} No performance action warranted."
                    )
                elif czc >= tolerance and cn in exonerated_classes:
                    # CIT has cross-referenced canary + CRO + sector assignments
                    # and confirmed this agent is blocked by market conditions,
                    # not underperforming. Do not penalise — log the context instead.
                    exon_reasons = next(
                        (e.get("exoneration_reasons", []) for e in cit_memo.get("exonerated", [])
                         if e.get("class_name") == cn), []
                    )
                    metrics["cit_exonerated"] = True
                    metrics["exoneration_reasons"] = exon_reasons
                    issues.append(
                        f"[CIT EXONERATED] {czc} zero-pick cycles — confirmed market-condition blockage "
                        f"({'; '.join(exon_reasons) or 'see cit_hr_memo.json'}). No performance action warranted."
                    )
                elif czc >= tolerance * 2:
                    rating = "CONCERN"
                    issues.append(
                        f"{czc} consecutive zero-pick cycles "
                        f"(tolerance: {tolerance}, concern: {tolerance*2}). "
                        f"HR note: only flagged after full context review confirmed genuine underperformance."
                    )
                elif czc >= tolerance:
                    rating = max(rating, "NOTICE",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        f"{czc} consecutive zero-pick cycles "
                        f"(tolerance before notice: {tolerance}). "
                        f"HR note: pick rejections by Manager are NOT counted against agent."
                    )
                elif approved < target_picks and submitted > 0:
                    # Low approvals but agent IS submitting — this is a macro/risk outcome,
                    # not a research failure. Only log as informational WATCH.
                    rating = max(rating, "WATCH",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        f"Approved picks this window: {approved} "
                        f"(target: ≥{target_picks}/cycle). Note: agent submitted {submitted} "
                        f"pick(s) — rejections at Manager/CRO level, not research level."
                    )

                # Never penalize for low approval rate alone — that is a Manager/CRO
                # decision, not an indicator of poor research quality.
                if submitted > 0 and approval_rate < target_rate and not _whole_picture_exonerated:
                    issues.append(
                        f"[INFO ONLY] Approval rate {approval_rate:.1f}% below target {target_rate:.0f}%. "
                        f"This reflects current risk posture, not research quality — not counted against agent."
                    )

            # ── Manager Agent ─────────────────────────────────────────────
            elif cn in ("ManagerAgent",):
                metrics["stale_sell_count"] = len(stale_sells)
                if stale_sells:
                    rating = "CONCERN"
                    tickers = [r.get("ticker","?") for r in stale_sells[:5]]
                    issues.append(
                        f"{len(stale_sells)} stale SELL signal(s) unexecuted: "
                        f"{', '.join(tickers)}. Zero tolerance."
                    )

            # ── CRO ───────────────────────────────────────────────────────
            elif cn in ("RiskManager",):
                metrics.update({"bulletin_complete": cro_complete, "cvar_non_zero": cvar_non_zero})
                if not cro_complete:
                    rating = "CONCERN"
                    issues.append("Risk bulletin missing or incomplete.")
                if not cvar_non_zero and len(recs_list) > 0:
                    rating = max(rating, "NOTICE",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append("CVaR is zero despite open positions — calculation may have failed.")

            # ── IRM — sealed evaluation ───────────────────────────────────
            # The IRM's mandate is simple: run every cycle, independently,
            # regardless of the --no-email flag. We evaluate whether that
            # happened and whether the board panel was substantive.
            elif cn == IRM_CLASS_NAME:
                irm_data  = _safe_load("data/irm_daily_check.json")
                irm_panel = irm_data.get("board_panel", {})
                irm_ran   = bool(irm_panel.get("board_verdict"))

                # Freshness check: IRM should have run within 30 hours
                gen_at = irm_data.get("generated_at", "")
                irm_is_fresh = False
                if gen_at:
                    try:
                        age_h = (datetime.now() - datetime.fromisoformat(gen_at)).total_seconds() / 3600
                        irm_is_fresh = age_h < 30
                    except Exception:
                        pass

                metrics.update({
                    "irm_ran":         irm_ran,
                    "irm_is_fresh":    irm_is_fresh,
                    "red_flags_count": len(irm_panel.get("combined_red_flags", [])),
                    "risk_score":      irm_panel.get("highest_risk_score", 0),
                    "specialists":     irm_panel.get("specialists_consulted", []),
                })

                if not irm_ran:
                    rating = "CONCERN"
                    issues.append(
                        "IRM board panel missing or empty this cycle — "
                        "the independence mandate requires the IRM to run every cycle "
                        "regardless of the email flag. This is its entire purpose."
                    )
                elif not irm_is_fresh:
                    rating = max(rating, "NOTICE",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        f"IRM last ran more than 30 hours ago — likely missed a daily cycle. "
                        f"Independence mandate requires every cycle without exception."
                    )

            # ── System Performance Analyst ────────────────────────────────
            if cn == "SystemPerformanceAnalyst" and system_perf:
                health = system_perf.get("health_score", 10)
                metrics["system_health_score"] = health
                if health < 5:
                    issues.append(f"System health score {health}/10 — firm is in degraded state.")

            # ── CIT Director ──────────────────────────────────────────────
            elif cn == "CITDirector":
                min_props = goals.get("min_proposals_per_session", 3)
                metrics.update({
                    "latest_report_fresh":    cit_is_fresh,
                    "proposals_in_latest":    cit_proposals_total,
                    "morning_ran":            bool(cit_morning.get("generated_at")),
                    "evening_ran":            bool(cit_evening.get("generated_at")),
                })
                if not cit_is_fresh:
                    rating = max(rating, "NOTICE",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        "CIT Director latest report is more than 14 hours old — "
                        "likely missed a scheduled session."
                    )
                if cit_proposals_total < min_props and cit_is_fresh:
                    rating = max(rating, "WATCH",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        f"Only {cit_proposals_total} proposals in latest session "
                        f"(target: ≥{min_props}). Output may be lower-quality or "
                        f"analysis is not comprehensive enough."
                    )

            # ── CIT Sub-agents (Inspector, Monitor, Strategist, Advisor) ─
            elif cn in ("CITCodeInspector", "CITPipelineMonitor",
                        "CITStrategyAnalyst", "CITArchitectureAdvisor"):
                # Sub-agent findings are embedded in the CIT Director report
                _subkey = {
                    "CITCodeInspector":      "code_inspector",
                    "CITPipelineMonitor":    "pipeline_monitor",
                    "CITStrategyAnalyst":    "strategy_analyst",
                    "CITArchitectureAdvisor":"architecture_advisor",
                }.get(cn, cn.lower())
                sub_findings = _cit_latest.get("agent_findings", {}).get(_subkey, [])
                min_f = goals.get("min_findings", 1)
                tolerance = goals.get("zero_miss_tolerance", 1)
                metrics.update({
                    "findings_count":  len(sub_findings),
                    "report_fresh":    cit_is_fresh,
                    "min_findings":    min_f,
                })
                if not cit_is_fresh:
                    rating = max(rating, "NOTICE",
                                 key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                    issues.append(
                        f"Parent CIT Director report is stale — {cn} output "
                        f"cannot be verified for this cycle."
                    )
                elif len(sub_findings) < min_f and min_f > 0:
                    czc = agent.get("consecutive_zero_cycles", 0)
                    if czc >= tolerance * 2:
                        rating = "CONCERN"
                        issues.append(
                            f"{czc} consecutive zero-finding sessions "
                            f"(tolerance: {tolerance}). Agent may not be "
                            f"analysing the codebase / pipeline effectively."
                        )
                    elif czc >= tolerance:
                        rating = max(rating, "NOTICE",
                                     key=lambda r: ["PASS","WATCH","NOTICE","CONCERN"].index(r))
                        issues.append(
                            f"Zero findings in latest session "
                            f"({czc} consecutive, tolerance: {tolerance})."
                        )

            ratings[cn] = {
                "agent":    cn,
                "status":   status,
                "rating":   rating,
                "issues":   issues,
                "metrics":  metrics,
                "goals":    {k: v for k, v in goals.items() if k != "notes"},
                "goal_notes": goals.get("notes", ""),
                "evaluated_at": today,
            }

        return ratings


# =============================================================================
# HR DEVELOPMENT OFFICER — qualitative evaluation
# =============================================================================

class HRDevelopmentOfficer:
    """
    Qualitative development tracking. Measures improvement trajectory,
    cultural alignment, thesis sharpness over time, and whether agents
    in performance stages are genuinely developing or stagnating.
    """

    def evaluate_all_agents(
        self,
        registry: dict,
        hr_history: dict,
        hrpo_ratings: dict,
    ) -> dict:
        """Qualitative evaluation of all agents. Returns development assessments."""
        agents      = registry.get("agents", [])
        records     = hr_history.get("agent_records", {})
        today       = datetime.now().strftime("%Y-%m-%d")
        assessments = {}

        for agent in agents:
            cn      = agent.get("class_name", "?")
            status  = agent.get("status", "active")
            czc     = agent.get("consecutive_zero_cycles", 0)
            perm    = agent.get("permanent", False)
            history = records.get(cn, {})

            rating     = "DEVELOPING"
            trajectory = "STABLE"
            observations = []
            development_plan = []

            past_ratings = history.get("rating_history", [])

            # ── Trajectory analysis ───────────────────────────────────────
            if len(past_ratings) >= 3:
                recent = past_ratings[-3:]
                order  = ["PASS", "WATCH", "NOTICE", "CONCERN"]
                scores = [order.index(r) if r in order else 0 for r in recent]
                if scores[-1] > scores[0]:
                    trajectory = "DECLINING"
                    observations.append(
                        f"Performance trend is declining over the last "
                        f"{len(recent)} evaluations: {' → '.join(recent)}."
                    )
                elif scores[-1] < scores[0]:
                    trajectory = "IMPROVING"
                    observations.append(
                        f"Performance trend is improving: {' → '.join(recent)}. "
                        f"Development plan is working."
                    )
                else:
                    trajectory = "STABLE"

            # ── Sign-off accountability (read-only) ───────────────────────
            # An agent that signs off on autonomous fixes carries its
            # reputation on the outcome. HR surfaces a burning record — this is
            # the loop that makes a sign-off cost something (see signoff_ledger).
            try:
                import signoff_ledger as _sl
                rep = _sl.reputation(cn)
                if _sl.hr_flag(cn):
                    observations.append(
                        f"Sign-off reputation is negative (ewma {rep['ewma']:+.2f}; "
                        f"{rep.get('n_burned',0)} of {rep['n_signed']} signed fixes "
                        f"reverted/regressed). Review the rigour of this agent's "
                        f"correctness/risk sign-offs before it clears more changes."
                    )
                    if rating == "DEVELOPING":
                        rating = "NEEDS_ATTENTION"
            except Exception:
                pass  # ledger optional; never let it break HR evaluation

            # ── Culture of learning assessment ────────────────────────────
            if perm:
                observations.append(
                    f"Permanent agent — full institutional protection. "
                    f"Expected to model the culture of learning for the team."
                )

            # ── Stage awareness ───────────────────────────────────────────
            if status == "noticed":
                observations.append(
                    f"Agent is at Stage 1 (Performance Notice). "
                    f"Development Officer recommends: review watchlist breadth, "
                    f"lower conviction threshold, cross-reference CRO bulletin each cycle."
                )
                development_plan.append(
                    "Broaden watchlist coverage by 20%. "
                    "Submit at least 3 candidates per cycle even if conviction is moderate."
                )
                development_plan.append(
                    "Study the most recent approved picks from peer agents — "
                    "identify what patterns the Manager Agent is approving."
                )

            elif status == "reassigned":
                observations.append(
                    f"Agent is at Stage 2 (Role Reassignment) with {czc} zero-pick cycles. "
                    f"New sector assignment should be treated as a genuine fresh start. "
                    f"Development Officer is monitoring for genuine adaptation vs. stagnation."
                )
                development_plan.append(
                    "Deep-dive into new sector: read all relevant KNOWLEDGE_BASE frameworks "
                    "and case studies before first pick submission in new role."
                )
                development_plan.append(
                    "First pick in new sector should cite at least 2 specific data points "
                    "from the sector's knowledge base — demonstrate learning, not recycling."
                )

            # ── Cross-reference with HRPO ─────────────────────────────────
            hrpo = hrpo_ratings.get(cn, {})
            hrpo_rating = hrpo.get("rating", "PASS")
            if hrpo_rating == "CONCERN" and trajectory == "IMPROVING":
                observations.append(
                    f"NOTE: HRPO rates this agent CONCERN (quantitative) but "
                    f"qualitative trajectory is IMPROVING. Recommend monitoring "
                    f"for another cycle before escalating — improvement is genuine."
                )
                rating = "WATCH"
            elif hrpo_rating == "PASS" and trajectory == "DECLINING":
                observations.append(
                    f"NOTE: HRPO rates this agent PASS (quantitative) but "
                    f"qualitative trajectory is DECLINING. Early warning — "
                    f"quantitative metrics have not yet caught up with the trend."
                )
                rating = "WATCH"
            elif hrpo_rating in ("NOTICE", "CONCERN"):
                rating = "NEEDS_SUPPORT"
            elif hrpo_rating == "WATCH":
                rating = "WATCH"
            else:
                rating = "DEVELOPING"

            assessments[cn] = {
                "agent":            cn,
                "status":           status,
                "development_rating": rating,
                "trajectory":       trajectory,
                "observations":     observations,
                "development_plan": development_plan,
                "evaluated_at":     today,
            }

        return assessments


# =============================================================================
# MUTUAL CROSS-AUDIT
# =============================================================================

def _cross_audit_hrpo(hrpo_ratings: dict) -> dict:
    """
    HRDO cross-audits HRPO for excessive harshness.
    If HRPO rates ≥50% of agents NOTICE or CONCERN, flag potential harshness bias.
    """
    total    = len(hrpo_ratings)
    concerns = sum(1 for r in hrpo_ratings.values()
                   if r.get("rating") in ("NOTICE", "CONCERN"))
    passes   = sum(1 for r in hrpo_ratings.values() if r.get("rating") == "PASS")

    concern_rate = (concerns / total * 100) if total > 0 else 0
    pass_rate    = (passes   / total * 100) if total > 0 else 0

    verdict = "CALIBRATED"
    notes   = []

    if concern_rate >= 60:
        verdict = "POTENTIAL_HARSHNESS_BIAS"
        notes.append(
            f"HRPO flagged {concerns}/{total} agents ({concern_rate:.0f}%) as NOTICE or CONCERN. "
            f"This is unusually high. Development Officer recommends reviewing whether "
            f"market conditions (defensive regime, canary active) were factored in. "
            f"Agents cannot generate picks if the CRO has blocked all BUYs."
        )
    elif pass_rate >= 90:
        verdict = "POTENTIAL_LENIENCY_FLAG"
        notes.append(
            f"HRPO gave PASS to {passes}/{total} agents ({pass_rate:.0f}%). "
            f"Verify that zero-pick agents in defensive regime were correctly assessed — "
            f"blanket passes during a bear cycle may mask genuine underperformance."
        )
    else:
        notes.append(
            f"HRPO ratings appear well-calibrated: {passes} PASS, "
            f"{concerns} NOTICE/CONCERN out of {total} agents."
        )

    return {"verdict": verdict, "concern_rate_pct": round(concern_rate, 1),
            "pass_rate_pct": round(pass_rate, 1), "notes": notes}


def _cross_audit_hrdo(hrdo_assessments: dict) -> dict:
    """
    HRPO cross-audits HRDO for excessive leniency.
    If HRDO rates ≥70% of agents DEVELOPING (positive) despite poor HRPO scores, flag it.
    """
    total      = len(hrdo_assessments)
    positive   = sum(1 for a in hrdo_assessments.values()
                     if a.get("development_rating") in ("DEVELOPING",))
    supported  = sum(1 for a in hrdo_assessments.values()
                     if a.get("development_rating") == "NEEDS_SUPPORT")

    positive_rate = (positive / total * 100) if total > 0 else 0
    verdict       = "CALIBRATED"
    notes         = []

    if positive_rate >= 70 and supported == 0:
        verdict = "POTENTIAL_LENIENCY_BIAS"
        notes.append(
            f"HRDO rated {positive}/{total} agents ({positive_rate:.0f}%) as DEVELOPING "
            f"with zero NEEDS_SUPPORT ratings. If HRPO is simultaneously flagging "
            f"quantitative concerns, HRDO may be over-weighting trajectory and cultural "
            f"alignment at the expense of output accountability."
        )
    else:
        notes.append(
            f"HRDO development assessments appear balanced: "
            f"{positive} DEVELOPING, {supported} NEEDS_SUPPORT out of {total}."
        )

    return {"verdict": verdict, "positive_rate_pct": round(positive_rate, 1),
            "notes": notes}


# =============================================================================
# ORCHESTRATOR
# =============================================================================

def _probe_import(name: str, rel: str, here: str):
    """Import an agent the way its scheduler would, and raise if it can't.

    Root-level modules import by name. Agents inside a package directory must
    NOT: `teaching_division/` is shadowed by `teaching_division.py`, so
    `import_module("teaching_division.chief_learning_officer")` raises
    ModuleNotFoundError and the attendance audit would invent a broken agent
    that in fact runs fine as `python3 teaching_division/chief_learning_officer.py`.
    A false CANNOT_START is worse than none — it is a management finding that
    sends someone to fix nothing, and it makes the real ones easier to dismiss.
    So package-dir agents are loaded from their file with their own directory
    on sys.path, which is exactly what running them as a script does.
    """
    import importlib.util
    if not rel:
        importlib.import_module(name)
        return
    path = os.path.join(here, rel)
    spec = importlib.util.spec_from_file_location(f"_hr_probe_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no import spec for {rel}")
    mod = importlib.util.module_from_spec(spec)
    pkg_dir = os.path.dirname(path)
    sys.path.insert(0, pkg_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        try:
            sys.path.remove(pkg_dir)
        except ValueError:
            pass


def audit_attendance(base_dir: str = None) -> dict:
    """Which agents never come to work, and which cannot work at all?

    THE GAP THIS CLOSES (Chairman question, 2026-07-29): "how did you or HR not
    catch this?" — international_analyst.py had five wired consumers, tracked the
    KOSPI, and was scheduled nowhere while also crashing on import under Python
    3.9. HR gave the firm a clean bill of health the whole time.

    The reason is structural, not careless. HR evaluated the 39 names on its
    ROSTER, and every one of them looked fine, because HR's inputs are the
    *outputs* agents produce — picks, decisions, reports. An agent that never runs
    produces no bad output. It produces no output at all, which HR's scoring read
    as "not applicable" rather than "absent without leave". The roster was never
    checked against the set of agents the firm actually executes.

    So this is an ATTENDANCE check, deliberately separate from performance review:
      - CANNOT_START — the module does not import. It is not underperforming, it
        is broken, and no amount of coaching or PIP fixes that.
      - NEVER_SCHEDULED — importable, has downstream consumers, but no scheduler
        root reaches it. Consumers see "no data today", forever.

    Both are firm/management failures, not agent failures, so they are reported
    as such and never feed an agent's performance rating or PIP status.
    """
    import importlib
    # `base_dir` exists so the missing-snapshot path below can be exercised by a
    # test without renaming a live repo file out from under the running firm
    # (same escape hatch _corroborate_ratchet_entry_resolved already takes). An
    # untested blind path is how a fail-safe quietly becomes a fail-silent.
    here = base_dir or os.path.dirname(os.path.abspath(__file__))

    main_src = ""
    cron_src = ""
    try:
        with open(os.path.join(here, "main.py")) as fh:
            main_src = fh.read()
    except Exception:
        pass
    # Ground truth = the VM's live crontab snapshot, and nothing else (see
    # wiring_audit.py's check_orphan_agents for the full history: judging
    # "scheduled" against new_crontab.txt, a hand-maintained Mac-path template,
    # let it drift 10 agents behind prod). 2026-08-01: the template fallback is
    # gone along with the template — falling back to a stale document is how this
    # check answered a question it could not see, and here the answer becomes an
    # HR finding against an agent that runs fine.
    cron_missing = False
    try:
        with open(os.path.join(here, "data/vm_crontab_snapshot.txt")) as fh:
            cron_src = fh.read()
    except Exception:
        cron_missing = True

    findings = {"cannot_start": [], "never_scheduled": [], "missing_file": [],
                "checked": 0}
    try:
        import wiring_audit
        watched = list(getattr(wiring_audit, "AGENT_MODULES", []) or [])
    except Exception:
        watched = []
    if not watched:
        # Fall back to the modules HR can see on disk that look like agents.
        watched = sorted(
            f[:-3] for f in os.listdir(here)
            if f.endswith(".py") and not f.startswith("test_")
            and any(f.endswith(sfx) for sfx in ("_agent.py", "_analyst.py",
                                                "_model.py", "_tracker.py",
                                                "_runner.py", "_sizer.py"))
        )

    # ── Union in the canonical roster ────────────────────────────────────────
    # The suffix scan above finds 48 modules. The roster (agent_roster.py, merged
    # across both hosts) lists 72, and the 46 it adds are not marginal: the full
    # board, the deputy board, the Logic Team, HR's own retirement audit, the
    # Chairman SLA, and every ML drift monitor. Attendance was checking the
    # agents whose FILENAMES happened to end in _agent.py — which is a naming
    # convention, not a roster. `board_meeting.py` grades proposals and could
    # have failed to import for a month without HR noticing, for no better
    # reason than that it isn't called board_meeting_agent.py.
    #
    # Keyed by import target, not bare name: roster modules live in packages
    # (fe_research_agents/, agents/), and `import_module("activist_investor_
    # monitor")` would raise ModuleNotFoundError and be reported as CANNOT_START
    # — a checker inventing its own broken agents.
    targets = {n: None for n in watched}
    runs_via, by_file = {}, {}
    try:
        roster = _safe_load(os.path.join(here, ROSTER_PATH))
        if roster and _roster_is_stale(roster, base=here):
            # Attendance decides which modules get imported and which are
            # reported missing. Off a frozen snapshot it would take the register
            # of a firm that has moved on: agents added since the freeze are not
            # marked absent, they are never called, and an agent nobody calls
            # roll for cannot be noticed missing. Same rebuild as the coverage
            # audit below, for the same reason.
            #
            # Two things this must not do. Staleness is a property of a file
            # that EXISTS — no file under an explicit base_dir is a caller
            # scoping the audit to a sandbox, not a writer that died, and
            # rebuilding there would drag the whole real firm into a run that
            # was deliberately narrowed. And the rebuild is pinned to `here`,
            # so it cannot answer a question about one tree by discovering
            # another.
            import agent_roster
            roster = agent_roster.build(base=Path(here))
        for r in (roster.get("agents", []) if isinstance(roster, dict) else []):
            if r.get("name") and r.get("runs_via"):
                runs_via[r["name"]] = r["runs_via"]
            if r.get("file"):
                by_file[r["file"]] = r["name"]
            f = r.get("file") or ""
            if not f.endswith(".py"):
                continue
            if not r.get("file_exists", True):
                # A superseded row pointing at a deleted predecessor is recorded
                # history, not a missing agent — risk_manager.py was replaced by
                # risk_manager_agent.py on purpose. Reporting those three every
                # day would train the reader to ignore the field that is supposed
                # to catch a real disappearance.
                if r.get("employment") != "superseded":
                    findings["missing_file"].append({"agent": r.get("name"), "file": f})
                continue
            if r.get("kind") != "module":
                continue   # registry agents are exercised via research_team
            targets[r.get("name")] = f if "/" in f else None
    except Exception as _r_e:
        logger.debug(f"  [HR] Roster unavailable to attendance audit: {_r_e}")

    # Reconcile the suffix scan's names against the roster's. `fe_cit_agent.py`
    # is the registry agent FECITAgent; left alone, HR would report one agent as
    # absent under its module name while the roster reported it employed under
    # its class name — two rows, one agent, and a headcount nobody can reconcile.
    # The roster also records which agent-shaped files are NOT agents (wake_agent
    # is a shell shim); that judgement lives in one place, not two.
    try:
        from agent_roster import NOT_AGENTS as _not_agents
    except Exception:
        _not_agents = set()
    for stem in list(targets):
        if f"{stem}.py" in _not_agents:
            targets.pop(stem)
        elif by_file.get(f"{stem}.py", stem) != stem:
            targets.pop(stem)   # already on the roster under its class name

    for name, rel in sorted(targets.items()):
        findings["checked"] += 1
        try:
            _probe_import(name, rel, here)
        except Exception as e:
            findings["cannot_start"].append({"agent": name,
                                             "error": f"{type(e).__name__}: {e}"[:180]})
            continue
        # ONE definition of "scheduled" for the whole firm. The roster's is
        # strictly better than the substring test below it — word-boundary
        # matched (so `risk_manager` isn't called scheduled because
        # `risk_manager_agent.py` is in cron) and it follows one level of
        # imports (so a module the pipeline calls but cron never names is
        # correctly employed). Two definitions would give HR and the roster
        # different headcounts for the same firm, which is how an audit ends up
        # arguing with itself instead of with reality.
        via = runs_via.get(name)
        if via is not None:
            if via == ["UNSCHEDULED"]:
                findings["never_scheduled"].append({"agent": name})
        elif cron_missing:
            # No schedule document = no basis for the claim. Saying nothing about
            # this agent is the honest answer; the missing snapshot is reported
            # once, below, as the firm-level problem it actually is.
            pass
        elif name not in main_src and name not in cron_src:
            findings["never_scheduled"].append({"agent": name})

    if cron_missing:
        findings["schedule_unreadable"] = (
            "data/vm_crontab_snapshot.txt is missing — attendance could not be "
            "checked for agents the roster has no runs_via for. Run "
            "scripts/refresh_crontab_snapshot.sh")

    findings["status"] = ("ATTENTION" if (findings["cannot_start"] or
                                          findings["never_scheduled"] or
                                          findings["missing_file"] or
                                          cron_missing) else "OK")
    findings["note"] = ("Attendance is a MANAGEMENT finding, not a performance "
                        "one — these never affect an agent's rating or PIP.")
    return findings


RATCHET_INTEGRITY_HISTORY_PATH = "data/ratchet_integrity_history.json"

# Every shrink-only baseline the firm relies on, and how to independently
# corroborate that an entry which vanished from it was actually fixed —
# NOT just re-derived from the same detector that removed it.
_RATCHET_SPECS = [
    {"name": "orphan_agents", "baseline_path": "data/orphan_agents_baseline.json",
     "entry_key": "known_orphans"},
]

# >1 simultaneous UNCORROBORATED disappearance from one baseline in one audit
# cycle is the exact shape of the 2026-07-30 incident: nine baselined orphans
# vanished at once because a test-root leak made the reachability graph lie,
# not because nine agents got scheduled. One real fix landing at a time is
# normal; a cluster landing at once is the tell that the DETECTOR changed, not
# the code. Counted on the UNCORROBORATED subset on purpose — the board
# triaging all eleven at once is the intended outcome, not an incident.
_MASS_ALL_CLEAR_THRESHOLD = 1

# Decisions the board may record in a baseline's `ratified_removals` to justify
# an entry leaving the ratchet. SCHEDULE is deliberately absent: "we scheduled
# it" is checkable against the live crontab and must be proven, not asserted.
_RATIFIED_DECISIONS = ("RETIRE", "DELETE", "SUPERSEDED")

# A crontab snapshot older than this proves nothing about today's schedule.
_SNAPSHOT_MAX_AGE_DAYS = 14


def _corroborate_ratchet_entry_resolved(entry: str, base_dir: str = None,
                                        baseline_doc: dict = None):
    """Independent second opinion that `entry` (an agent filename) really left
    the ratchet for a legitimate reason. Returns a short reason string, or None
    if the disappearance is unjustified.

    Uses a DIFFERENT method than the detector that cleared it.
    wiring_audit.check_orphan_agents() decides "scheduled" via a fuzzy,
    regex-driven reachability walk over the whole import graph — exactly the
    algorithm that once quietly reclassified nine orphans as scheduled because
    of a graph-traversal bug. Nothing here calls that function.

    Three ways an entry may legitimately leave, matching the three outcomes of
    the board's SCHEDULE / RETIRE / DELETE triage:

      "scheduled"  — the name appears literally in the VM's live crontab
                     snapshot. Refuses to corroborate off a snapshot older than
                     _SNAPSHOT_MAX_AGE_DAYS: stale evidence must not rubber-stamp
                     a removal, so staleness fails CLOSED (stays an alarm).
      "deleted"    — the module is simply gone from disk. A file that does not
                     exist cannot be an unscheduled agent.
      "ratified:X" — the board explicitly recorded the removal in the baseline's
                     own `ratified_removals` list, with a decision and rationale.

    That third channel exists because RETIRE and DELETE were previously
    *unfalsifiable*: corroboration only accepted "now in the crontab", so
    carrying out the board's own approved triage raised a false alarm forever.
    Note its limit honestly — the ratification lives in the file being audited,
    so it is not tamper-proof against something that can edit that file. What it
    buys is that a removal can no longer be made QUIET: silence now requires a
    written, dated, diffable claim that a human reviews in the commit.
    """
    import re
    import time
    here = base_dir or os.path.dirname(os.path.abspath(__file__))
    stem = entry[:-3] if entry.endswith(".py") else entry

    # (a) genuinely scheduled now — proven against the canonical prod schedule
    snap_path = os.path.join(here, "data", "vm_crontab_snapshot.txt")
    try:
        age_days = (time.time() - os.stat(snap_path).st_mtime) / 86400.0
        if age_days <= _SNAPSHOT_MAX_AGE_DAYS:
            if re.search(rf"\b{re.escape(stem)}\b", open(snap_path).read()):
                return "scheduled"
    except Exception:
        pass

    # (b) the file is gone
    if not os.path.exists(os.path.join(here, f"{stem}.py")):
        return "deleted"

    # (c) the board wrote down why
    try:
        for rec in (baseline_doc or {}).get("ratified_removals", []) or []:
            if not isinstance(rec, dict):
                continue
            if os.path.basename(str(rec.get("entry", ""))) != os.path.basename(entry):
                continue
            decision = str(rec.get("decision", "")).upper()
            if decision in _RATIFIED_DECISIONS and str(rec.get("rationale", "")).strip():
                return f"ratified:{decision}"
    except Exception:
        pass

    return None


def audit_ratchet_integrity() -> dict:
    """A shrinking ratchet is not itself proof anything got fixed.

    THE GAP THIS CLOSES (Chairman directive, 2026-07-30): on the same day the
    orphan-agent baseline was created, a bug in check_orphan_agents()'s own
    reachability walk made NINE of the eleven baselined orphans look
    "scheduled" in a single run — not because anyone fixed them, but because a
    test file leaked into the import-graph traversal. The check that owns the
    ratchet asked, in good faith, to delete all nine from the baseline. If
    that recommendation had been accepted mechanically, the ratchet would have
    loosened itself on the strength of the exact bug that made the finding.

    So: nine disappearances at once is not nine fixes, it is the detector
    changing its mind. This audit watches every registered shrink-only
    baseline/ratchet across runs, and for any entry that vanished since the
    last check, demands independent corroboration (a DIFFERENT check than the
    one that cleared it — see _corroborate_ratchet_entry_resolved) before
    treating the disappearance as legitimate. Corroborated fixes pass silently;
    anything uncorroborated, or an uncorroborated burst above
    _MASS_ALL_CLEAR_THRESHOLD in one cycle, is a MANAGEMENT finding for the
    board — never an agent rating or PIP input, same as attendance.

    THE FINDING IS STICKY, and that is the whole design. The first version of
    this audit advanced its own high-water mark unconditionally: it reported the
    nine-at-once incident on one cycle, wrote the shrunken list to history, and
    said OK forever after. A watchdog that forgets what it saw turns "somebody
    missed one email" into "permanently accepted", which is precisely the silent
    loosening it exists to prevent. So an uncorroborated entry is KEPT in the
    watermark and re-reported every single cycle until it is either genuinely
    resolved or explicitly ratified. The alarm can only be cleared by fixing it
    or by writing down why — never by waiting.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    history_path = os.path.join(here, RATCHET_INTEGRITY_HISTORY_PATH)
    history = _safe_load(history_path)
    if not isinstance(history, dict):
        history = {}

    findings = {"checked": 0, "vanished_uncorroborated": [], "mass_all_clear": [],
                "ratified_removals": [], "status": "OK"}

    for spec in _RATCHET_SPECS:
        name = spec["name"]
        findings["checked"] += 1
        baseline_doc = _safe_load(os.path.join(here, spec["baseline_path"]))
        current = set(baseline_doc.get(spec["entry_key"], []) if baseline_doc else [])
        prior_rec = history.get(name, {})
        prior = set(prior_rec.get("entries", []))

        # Nothing to compare against on the very first audit of a ratchet.
        unresolved = set()
        if prior_rec:
            vanished = prior - current
            justified = {}
            for e in sorted(vanished):
                why = _corroborate_ratchet_entry_resolved(e, base_dir=here,
                                                          baseline_doc=baseline_doc)
                if why:
                    justified[e] = why
                else:
                    unresolved.add(e)
            if unresolved:
                findings["vanished_uncorroborated"].append(
                    {"ratchet": name, "entries": sorted(unresolved)})
            if justified:
                findings["ratified_removals"].append(
                    {"ratchet": name, "entries": justified})
            if len(unresolved) > _MASS_ALL_CLEAR_THRESHOLD:
                findings["mass_all_clear"].append(
                    {"ratchet": name, "count": len(unresolved),
                     "entries": sorted(unresolved)})

        # Carry unresolved disappearances forward so the finding recurs until
        # someone actually deals with it (see the docstring — a watchdog that
        # advances past an unexplained removal has silently accepted it).
        history[name] = {"entries": sorted(current | unresolved),
                         "unresolved": sorted(unresolved),
                         "checked_at": datetime.now().isoformat()}

    findings["status"] = ("ATTENTION" if (findings["vanished_uncorroborated"] or
                                          findings["mass_all_clear"]) else "OK")
    findings["note"] = ("A shrinking baseline is not proof of a fix — this never "
                        "affects an agent's rating or PIP, same as attendance.")

    try:
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        with open(history_path, "w") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception as e:
        logger.debug(f"  [HR] Could not persist ratchet integrity history: {e}")

    return findings


# One definition of "retired", imported not copied. This module used to key on
# a status tuple alone, so it was blind to the seven agents retired on
# 2026-05-14 — they carry `retired_at` + `retirement_reason` with
# `status: "active"`, and a status-only test reports zero retirements and a
# clean coverage audit. hr_retirement_audit.is_retired() is the union of both
# spellings; the fallback below keeps this module standing if that import ever
# breaks, but it is deliberately the LOOSER of the two so a failure here can
# only over-report, never under-report.
try:
    from hr_retirement_audit import is_retired as _is_retired
except Exception:                                    # pragma: no cover
    def _is_retired(a):
        return bool(str(a.get("status", "")).lower()
                    in ("retired", "decommissioned", "terminated")
                    or a.get("retired_at") or a.get("retirement_reason"))


# =============================================================================
# SWEEP QUEUE — HR's side of the weekly firm sweep
# =============================================================================

SWEEP_QUEUE_PATH = "data/hr_action_queue.json"

# What HR *does* about each kind of sweep finding. This table is the whole point
# of routing anything to HR: a queue that is only read is not a consumer, it is a
# spectator. Every kind the taxonomy can send here has a disposition, so an item
# can never land in HR's inbox with nobody knowing what to do with it.
#
# Dispositions are deliberately conservative on the first pass — the sweep sees
# wiring, not work quality, and an agent that runs nowhere has done nothing
# wrong. Only the kinds that mean "this agent is not doing its job" reach PIP.
SWEEP_DISPOSITIONS = {
    "AGENT_UNSCHEDULED":           ("NOT_AN_AGENT_FAULT", "Management defect: the agent is employed but scheduled nowhere. HR does not rate an agent for work it was never asked to do — CIT owns the cron fix; HR holds the headcount question until it runs."),
    "AGENT_UNREVIEWED":            ("ENROL_IN_REVIEW",    "Agent has never been through an HR cycle. Add to the review roster; a rating cannot be earned without one."),
    "AGENT_ISOLATED":              ("INVESTIGATE",        "Agent talks to nobody. Either its output was meant to be consumed and the wiring is broken (CIT), or it was never needed (headcount decision for HR + board)."),
    "AGENT_OUTPUT_UNCONSUMED":     ("INVESTIGATE",        "The agent works and nobody reads it. Confirm whether the consumer was lost or never existed before judging the agent."),
    "AGENT_INPUT_UNPRODUCED":      ("NOT_AN_AGENT_FAULT", "The agent is starved of an input nobody writes. Upstream defect — CIT owns it."),
    "AGENT_OUTPUT_STALE":          ("PIP_CANDIDATE",      "Scheduled and silent: the agent is expected to produce and has stopped. This is the shape of an agent that has quietly died at its desk."),
    "AGENT_OUTPUT_NEVER_PRODUCED": ("PIP_CANDIDATE",      "Employed since day one and has never produced anything. Verify it was ever wired, then decide employment."),
    "AGENT_ZOMBIE":                ("TERMINATE_PROPERLY", "Retired on paper and still executing. This is the Oracle failure in miniature — stop it, then reconcile the three retirement records."),
    "AGENT_FILE_MISSING":          ("CLEAN_ROSTER",       "The roster names a file that no longer exists. Either restore it or strike the row; a roster that lists ghosts under-reports every coverage gap."),
    "AGENT_IMPORT_FAILS":          ("SUSPEND_PENDING_FIX","The agent cannot even load, so it has not worked since the day it broke. Suspend its rating until CIT restores it."),
    "AGENT_IMPORT_TIMEOUT":        ("SUSPEND_PENDING_FIX","Hangs on import — same effect as broken, plus it can wedge whatever imports it."),
    "AGENT_REPORT_FAILS":          ("INVESTIGATE",        "Runs but cannot report. Its work may be fine and invisible, which is indistinguishable from no work at all."),
    "AGENT_WRITES_ON_IMPORT":      ("NOT_AN_AGENT_FAULT", "Mutates firm state merely by being imported — a code defect for CIT, not an employment matter."),
    "REVIEW_BLOCKED":              ("NOT_AN_AGENT_FAULT", "HR owes a KEEP/TERMINATE verdict on a reinstated agent and cannot reach one: the evidence channel it grades through is broken. Grading the agent anyway is not a strict standard, it is a false one — CIT owns the channel, HR re-reviews the moment it is alive, and the deferral is not a mark against the agent."),
}


def review_sweep_queue(path: str = None, now: str = None) -> dict:
    """Read the weekly sweep's HR queue, assign a disposition, escalate breaches.

    THE CONTRACT: sweep_governance.py writes data/hr_action_queue.json; this is
    its only consumer. If this function is deleted, that queue becomes exactly
    the orphan the sweep exists to find — so test_pipeline_smoke asserts the pair.

    HR is not asked to fix wiring; it is asked to decide what each finding means
    for the AGENT. That decision is stamped back onto the queue so the next
    sweep, the board packet, and problem_lifecycle_sentinel all read the same
    record rather than three private opinions.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = path or os.path.join(here, SWEEP_QUEUE_PATH)
    now = now or datetime.now().isoformat()

    queue = _safe_load(path)
    if not isinstance(queue, list):
        return {"status": "NO_QUEUE", "open": 0, "triaged": 0, "overdue": [],
                "note": "sweep queue absent or malformed — sweep_governance may not have run"}

    triaged, overdue, unknown_kinds, by_disposition = 0, [], [], {}
    changed = False
    for e in queue:
        if not isinstance(e, dict) or e.get("status") != "open":
            continue
        kind = e.get("kind")
        disp, rationale = SWEEP_DISPOSITIONS.get(
            kind,
            # No silent default. An unmapped kind means the taxonomy grew and HR
            # was not told, which is precisely the producer/consumer drift this
            # firm keeps rediscovering — so it is surfaced as its own finding.
            ("UNMAPPED_ESCALATE", "HR has no disposition for finding kind %r. "
                                  "The taxonomy has outgrown HR's policy table." % kind))
        if disp == "UNMAPPED_ESCALATE":
            unknown_kinds.append(kind)
        if e.get("hr_disposition") != disp:
            e["hr_disposition"] = disp
            e["hr_rationale"] = rationale
            e["hr_reviewed_at"] = now
            changed = True
        triaged += 1
        by_disposition[disp] = by_disposition.get(disp, 0) + 1

        # SLA breach: HR flags it, but does not escalate to the Chairman itself —
        # problem_lifecycle_sentinel owns that path, and two producers racing to
        # escalate the same item is how the Chairman's queue got to 88.
        try:
            age = (datetime.fromisoformat(now)
                   - datetime.fromisoformat(str(e.get("first_seen")))).days
        except Exception:
            age = 0
        if age > int(e.get("sla_days") or 20):
            e["sla_breached"] = True
            overdue.append({"key": e.get("key"), "age_days": age,
                            "disposition": disp, "severity": e.get("severity")})

    if changed:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(queue, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception as _we:
            logger.debug(f"  [HR] Could not write sweep queue triage: {_we}")

    resolved = sum(1 for e in queue if isinstance(e, dict) and e.get("status") == "resolved")

    # An item the last sweep could not actually observe is not evidence against
    # the agent. sweep_governance holds such items open and stamps them
    # last_unverifiable rather than closing them — the honest state — but "open"
    # then looks identical to a confirmed failure, and a PIP on an unverified
    # observation is a disciplinary action taken on no evidence. Attendance is
    # the case that bites: judged from the wrong host it condemns every agent
    # the host doesn't run.
    pip, withheld = [], []
    for e in queue:
        if not isinstance(e, dict) or e.get("status") != "open":
            continue
        if e.get("hr_disposition") != "PIP_CANDIDATE":
            continue
        if e.get("last_unverifiable"):
            withheld.append(e.get("subject"))
        else:
            pip.append(e.get("subject"))

    return {
        "status": "OK" if not unknown_kinds else "TAXONOMY_DRIFT",
        "open": triaged, "resolved_to_date": resolved,
        "triaged": triaged, "by_disposition": by_disposition,
        "overdue": sorted(overdue, key=lambda x: -x["age_days"])[:20],
        "overdue_count": len(overdue),
        "unmapped_kinds": sorted(set(unknown_kinds)),
        "pip_candidates": pip,
        "pip_withheld_unverifiable": sorted(withheld),
    }


def audit_retirement_coverage(registry=None) -> dict:
    """A retirement is not just an agent leaving — it's a sector losing a voice.

    THE GAP THIS CLOSES (Chairman directive, 2026-07-30, "it would be a shame
    if we killed a perfectly useful agent"): the registry has always recorded
    *that* an agent retired, but nothing has ever checked *what that leaves
    uncovered*. The precedent that makes this non-hypothetical: three agents
    once looked, by name, like they had international markets covered while
    KOSPI went unwatched for months — a roster read as coverage when it
    wasn't. A retirement with no replacement in the same sector/ETF lane is
    the same failure mode, just faster and mechanical to catch.

    For every agent that has ever carried a retired/decommissioned/terminated
    status, check whether any currently-active agent shares its (sector, etf)
    lane. If none does, that lane has zero live coverage — a MANAGEMENT
    finding for the board, never an agent rating or PIP input. This does not
    second-guess whether a retirement was justified (that's HR's ordinary
    performance process) — it only asks whether the seat got refilled.
    """
    # `registry` is injectable so detector_mutation_harness can feed this a state
    # with a planted uncovered lane and prove the audit still fires. A detector
    # that can only ever read live disk cannot be mutation-tested, and an
    # untestable detector is an unverified assurance — which is how this codebase
    # shipped a coverage test that passed against a registry it wrote itself.
    if registry is None:
        here = os.path.dirname(os.path.abspath(__file__))
        registry = _safe_load(os.path.join(here, REGISTRY_PATH))
    if isinstance(registry, list):
        agents = registry
    else:
        agents = registry.get("agents", []) if isinstance(registry, dict) else []

    active_lanes = set()
    for a in agents:
        if not _is_retired(a):
            sector = a.get("sector")
            etf = a.get("etf")
            if sector:
                active_lanes.add((sector, etf))

    seen_retired = {}
    for a in agents:
        if _is_retired(a):
            sector = a.get("sector")
            etf = a.get("etf")
            if not sector:
                continue
            key = (a.get("class_name"), sector, etf)
            # Same agent can appear more than once (retired + later re-hired
            # under the same class_name); only the most recent retired record
            # for a given class_name/lane matters for the coverage question.
            seen_retired[key] = a

    holes = []
    for (class_name, sector, etf), a in seen_retired.items():
        if (sector, etf) not in active_lanes:
            holes.append({
                "class_name": class_name,
                "sector": sector,
                "etf": etf,
                "retired_at": a.get("retired_at") or a.get("last_checked"),
                # The registry field is `retirement_reason`. The first version of
                # this audit reported a.get("status") under the key "reason",
                # which could only ever echo the tautology "retired" — a findings
                # payload that looks answered and says nothing.
                "reason": a.get("retirement_reason"),
            })

    # ── Was the remediation the agent was promised actually real? ────────────
    # A retirement is only fair if the ladder that preceded it ran. The Chief of
    # Staff's stage-3 reassignment writes `reassigned_to` and tells the agent it
    # has been "moved to a fresh start in a new domain" — but it never updates
    # the agent's own `sector`, and NOTHING in the firm reads `reassigned_to`
    # (2026-07-31: two references in the whole codebase, both writes). So the
    # move never happened; only the performance clock reset. Both agents retired
    # to date were retired at cycle 14 after a cycle-9 "reassignment" that was a
    # no-op, i.e. they were dismissed for failing the same job twice while being
    # told they had been given a different one.
    #
    # This is a MANAGEMENT finding about the lifecycle machinery, never an agent
    # rating or PIP input — the agents did nothing wrong here; the ladder did.
    unfair = []
    for (class_name, sector, etf), a in seen_retired.items():
        promised = a.get("reassigned_to")
        if promised and promised != sector:
            unfair.append({
                "class_name": class_name,
                "promised_lane": promised,
                "actual_lane_at_retirement": sector,
                "reassigned_at": a.get("reassigned_at"),
                "retired_at": a.get("retired_at"),
                "defect": ("reassignment recorded but never applied — `sector` "
                           "unchanged and no consumer reads `reassigned_to`"),
            })

    findings = {
        "checked": len(seen_retired),
        "coverage_holes": sorted(holes, key=lambda h: h["class_name"] or ""),
        "unapplied_remediation": sorted(unfair, key=lambda h: h["class_name"] or ""),
        "status": "ATTENTION" if (holes or unfair) else "OK",
        "note": ("A retired agent's lane with no live cover is a hole; a retirement "
                 "that followed a remediation step which never executed is unsound. "
                 "Neither ever affects an agent's rating or PIP, same as attendance."),
    }
    return findings


ROSTER_PATH = "data/agent_roster.json"

# Backstop only. This bounds the case where the roster job dies and the CODE
# stops changing too — a quiet weekend on a host nobody deployed to. The job is
# daily, so one missed run is a hiccup and two means the writer has stopped.
#
# It is deliberately NOT the main trigger. Age is a proxy for the thing that
# actually matters, and a bad one: it prices staleness by the clock when the
# hazard is priced by deploys. A roster three days old on a host whose code has
# not moved describes the firm perfectly; one built four hours ago, before a
# deploy that added an agent, does not. See _newest_module_mtime.
ROSTER_MAX_AGE_H = 48


def _newest_module_mtime(base) -> Optional[float]:
    """Epoch seconds of the most recently written agent module under `base`.

    The real question behind "is the roster stale" is not how old it is, it is
    whether the firm has changed since it was built. This measures that
    directly instead of guessing a number of hours: rsync preserves mtimes, so
    a module edited on the Mac at 06:00 still reads 06:00 on the VM, and a
    deploy that lands a new agent is visible here the moment it arrives.

    A scandir over ~320 files costs well under a millisecond; a rebuild costs
    seconds; an agent HR cannot see is the outcome the whole audit exists to
    prevent. With that asymmetry the check should fire on the real signal and
    fire early, not wait out a threshold.

    Returns None if the directory cannot be read — the caller must not turn an
    unreadable tree into "nothing has changed".
    """
    newest = None
    try:
        with os.scandir(str(base)) as it:
            for e in it:
                if not e.name.endswith(".py") or e.name.startswith("test_"):
                    continue
                try:
                    m = e.stat().st_mtime
                except OSError:
                    continue
                if newest is None or m > newest:
                    newest = m
    except OSError:
        return None
    return newest


def _roster_is_stale(roster, base=None, max_age_h: int = ROSTER_MAX_AGE_H) -> bool:
    """Is this snapshot still a description of the firm, or only of a past one?

    Asking "does the roster exist?" is a question a file from any date answers
    yes to forever — presence is not currency. agent_roster stamps every build
    with `generated_at`; nothing read it until now.

    Three ways to fail, in order of what actually bites:
      1. a module under `base` is NEWER than the snapshot — the firm changed
         after the roster was written, which is every deploy that ships an
         agent, and is the common case rather than the exotic one;
      2. the snapshot is older than the backstop, covering a dead writer on a
         host where nothing is being deployed either;
      3. no stamp, or one that will not parse. An artifact that cannot say when
         it was made cannot be trusted to say who works here, and since the
         remedy is cheap the ambiguous case fails toward doing the work.

    `base` is optional so the pure-timestamp behaviour stays callable and
    testable on its own, but every real caller passes it.
    """
    if not isinstance(roster, dict):
        return True
    ts = roster.get("generated_at")
    if not ts:
        return True
    try:
        # fromisoformat cannot read a trailing "Z" on 3.9, and agent_roster
        # writes a naive utcnow() stamp — normalise both shapes to naive UTC
        # without importing timezone, which this module does not carry.
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if when.tzinfo is not None:
            when = when.replace(tzinfo=None) - when.utcoffset()
    except Exception:
        return True

    if (datetime.utcnow() - when).total_seconds() / 3600.0 > max_age_h:
        return True

    if base is not None:
        newest = _newest_module_mtime(base)
        if newest is None:
            return True     # could not look; do not report "unchanged"
        # generated_at is naive UTC; st_mtime is epoch. Compare in epoch via
        # the same utcnow() reference, so a host running local time cannot
        # make a fresh roster look ancient or an old one look current.
        built_epoch = time.time() - (datetime.utcnow() - when).total_seconds()
        if newest > built_epoch + 1:      # 1s slack for same-second writes
            return True

    return False

# The agents that grade, retire, and reinstate everyone else. Every one of them
# is a MODULE agent — not one has ever had a registry row — so HR's review has
# never once scored its own chain of command. That is the point of listing them
# separately: "72 agents unreviewed" is a number, but "the officers who fire
# people are themselves unaudited" is the finding.
_GOVERNANCE_CHAIN = frozenset({
    "hr_agent", "hr_retirement_audit", "logic_officer", "logic_team",
    "logic_team_implementation_agent", "logic_team_trainer", "board_meeting",
    "deputy_board", "deputy_monitor", "apex_improvement_board",
    "chairman_decision_sla", "chairman_queue", "strategy_council",
    "chief_of_staff_agent", "irm_review_agent", "canary_agent",
})


def audit_roster_coverage() -> dict:
    """Does HR's review actually cover every agent the firm employs?

    THE GAP THIS CLOSES (Chairman directive, 2026-08-01: "hr needs to be on top
    of tracking all agents, including their own team ... we should never ever
    lose track of an agent because it could be disastrous if a key agent
    suddenly stops doing his job").

    HR reviews `agent_registry.json` — the Chief-of-Staff factory's roll of
    spawned research agents. On 2026-08-01 that file held 67 rows while the firm
    actually ran 139 agents: 72 of them are hand-written modules the factory
    never created, so no registry row exists, so HR has never evaluated them.
    Among the 72 are HR itself, both Logic Team agents, the board, the deputy
    board, the Chairman SLA and the Canary. The graders were ungraded.

    This audit measures the registry HR reviews against the canonical roster
    (agent_roster.py, merged across BOTH hosts — the Mac's registry had 37 rows
    and the VM's 66, and the union is what "every agent" means). It reports:

      unreviewed            — on the books, never scored by HRPO/HRDO
      governance_unreviewed — the subset that judges other agents (worst case)
      zombies               — retired on paper but still scheduled/loaded, i.e.
                              still trading. Seven of these exist right now.
      employed_unscheduled  — employed, importable, and run by nothing

    All four are MANAGEMENT findings about the roster, never agent ratings —
    same rule as attendance. An agent HR forgot to review has done nothing wrong.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    roster = _safe_load(os.path.join(here, ROSTER_PATH))
    rows = roster.get("agents", []) if isinstance(roster, dict) else []
    stale = _roster_is_stale(roster, base=here)

    if not rows or stale:
        # Never silently pass. A missing roster file is exactly the state in
        # which HR would report "all clear" while knowing nothing — build it
        # live rather than grade an empty set as OK.
        #
        # A *stale* roster is the same failure wearing a disguise. This block
        # used to ask only "does the file have rows?", which is a question a
        # snapshot from any date answers yes to, forever. data/agent_roster.json
        # is host-local, is not deployed, and is rewritten only by this host's
        # 11:25 UTC job; if that job dies the file freezes and HR keeps grading
        # a firm that no longer exists, reporting full coverage the whole time.
        # Every agent added after the freeze is not "unreviewed" — it is
        # invisible, which is the one outcome this audit exists to prevent.
        # Rebuilding costs a couple of seconds; missing an agent is the thing
        # Jack has called disastrous, so the threshold is tight.
        try:
            import agent_roster
            rows = agent_roster.build().get("agents", [])
        except Exception as e:
            return {
                "status": "ATTENTION",
                "checked": 0,
                "error": f"no roster to audit against: {type(e).__name__}: {e}"[:200],
                "note": ("HR cannot claim coverage without a roster. Run "
                         "`python3 agent_roster.py --merge-remote --adopt`."),
                "roster_was_stale": stale,
            }

    registry = _safe_load(os.path.join(here, REGISTRY_PATH))
    reviewed = {a.get("class_name") for a in (registry.get("agents", [])
                                              if isinstance(registry, dict) else [])
                if a.get("class_name")}

    unreviewed, governance, zombies, unscheduled = [], [], [], []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        if r.get("zombie"):
            zombies.append({
                "agent": name,
                "retirement_reason": r.get("retirement_reason"),
                "still_runs_via": r.get("runs_via"),
                "registry_status": r.get("registry_status"),
                "defect": ("retired on paper but still loaded/scheduled — the "
                           "retirement was recorded and never enforced"),
            })
        if r.get("employment") == "employed" and r.get("runs_via") == ["UNSCHEDULED"]:
            unscheduled.append({"agent": name, "file": r.get("file")})
        if name not in reviewed:
            entry = {"agent": name, "kind": r.get("kind"), "file": r.get("file"),
                     "runs_via": r.get("runs_via")}
            unreviewed.append(entry)
            if name in _GOVERNANCE_CHAIN:
                governance.append(entry)

    findings = {
        "checked": len(rows),
        "reviewed_by_hr": len(reviewed),
        "unreviewed": sorted(unreviewed, key=lambda x: x["agent"]),
        "governance_unreviewed": sorted(governance, key=lambda x: x["agent"]),
        "zombies": sorted(zombies, key=lambda x: x["agent"]),
        "employed_unscheduled": sorted(unscheduled, key=lambda x: x["agent"]),
        "roster_host": roster.get("host") if isinstance(roster, dict) else "built-live",
        "roster_generated_at": roster.get("generated_at") if isinstance(roster, dict) else None,
    }
    findings["status"] = "ATTENTION" if (zombies or governance) else (
        "WATCH" if unreviewed else "OK")
    findings["note"] = (
        f"{len(rows)} agents on the books; HR's registry review covers "
        f"{len(reviewed)}. Coverage gaps and zombies are MANAGEMENT findings — "
        "they never affect an agent's rating or PIP.")
    return findings


def run_hr_review(send_email: bool = False) -> dict:
    """
    Full HR review cycle. Both officers evaluate all agents, then cross-audit
    each other.

    Reporting chain (Chairman directive 2026-05-01):
    ─────────────────────────────────────────────────
    HR reports to the IRM, who bundles HR findings into his own Chairman email
    and decides when to send.  HR never emails the Chairman directly in the
    pipeline — only the IRM does.

    IRM self-evaluation:
    ─────────────────────────────────────────────────
    HR also evaluates the IRM, but that evaluation is SEALED.  The result is
    written base64-encoded to data/hr_irm_sealed.json.  The IRM code reads
    this file but never logs or renders its contents — it passes the opaque
    blob to the Chairman's email as a sealed annex.  The Chairman is the only
    person who can decode it:
        import base64, json
        json.loads(base64.b64decode("<sealed_b64 value>"))

    Outputs:
      data/hr_report.json      — all agents EXCEPT IRM (readable by IRM)
      data/hr_irm_sealed.json  — IRM evaluation only (sealed, base64-encoded)
    """
    logger.info("=" * 65)
    logger.info("  APEX CAPITAL — HR TEAM REVIEW")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("  Officers: HR Performance Officer · HR Development Officer")
    logger.info("  Scope: All registered agents + mutual cross-audit")
    logger.info("  Chain: HR → IRM → Chairman  |  IRM eval: SEALED")
    logger.info("=" * 65)

    # ── Load data ─────────────────────────────────────────────────────────
    registry     = _safe_load(REGISTRY_PATH)
    manager_data = _safe_load(MANAGER_LOG)
    research     = _safe_load(RESEARCH_RPT)
    system_perf  = _safe_load(SYSTEM_PERF_RPT)
    hr_history   = _load_hr_history()

    agents = registry.get("agents", [])
    logger.info(f"\n  Agents under review: {len(agents)}")

    # ── HRPO evaluation (all agents, including IRM) ───────────────────────
    logger.info("\n  [HRPO] Running quantitative evaluation...")
    hrpo = HRPerformanceOfficer()
    hrpo_ratings_all = hrpo.evaluate_all_agents(
        registry, manager_data, research, system_perf
    )

    # ── HRDO evaluation (all agents, including IRM) ───────────────────────
    logger.info("\n  [HRDO] Running qualitative development assessment...")
    hrdo = HRDevelopmentOfficer()
    hrdo_assessments_all = hrdo.evaluate_all_agents(registry, hr_history, hrpo_ratings_all)

    # ── Separate IRM from the rest (IRM evaluation is sealed) ────────────
    irm_hrpo = hrpo_ratings_all.pop(IRM_CLASS_NAME, None)
    irm_hrdo = hrdo_assessments_all.pop(IRM_CLASS_NAME, None)
    hrpo_ratings    = hrpo_ratings_all      # everything except IRM
    hrdo_assessments = hrdo_assessments_all  # everything except IRM

    hrpo_summary = {
        "PASS":    sum(1 for r in hrpo_ratings.values() if r["rating"] == "PASS"),
        "WATCH":   sum(1 for r in hrpo_ratings.values() if r["rating"] == "WATCH"),
        "NOTICE":  sum(1 for r in hrpo_ratings.values() if r["rating"] == "NOTICE"),
        "CONCERN": sum(1 for r in hrpo_ratings.values() if r["rating"] == "CONCERN"),
    }
    logger.info(f"  [HRPO] Complete — PASS:{hrpo_summary['PASS']} "
                f"WATCH:{hrpo_summary['WATCH']} NOTICE:{hrpo_summary['NOTICE']} "
                f"CONCERN:{hrpo_summary['CONCERN']} | IRM: sealed")
    hrdo_summary = {
        "DEVELOPING":    sum(1 for a in hrdo_assessments.values()
                             if a["development_rating"] == "DEVELOPING"),
        "WATCH":         sum(1 for a in hrdo_assessments.values()
                             if a["development_rating"] == "WATCH"),
        "NEEDS_SUPPORT": sum(1 for a in hrdo_assessments.values()
                             if a["development_rating"] == "NEEDS_SUPPORT"),
    }
    logger.info(f"  [HRDO] Complete — DEVELOPING:{hrdo_summary['DEVELOPING']} "
                f"WATCH:{hrdo_summary['WATCH']} NEEDS_SUPPORT:{hrdo_summary['NEEDS_SUPPORT']}")

    # ── Mutual cross-audit ────────────────────────────────────────────────
    logger.info("\n  [HRDO → HRPO] Cross-audit: checking for harshness bias...")
    cross_audit_of_hrpo = _cross_audit_hrpo(hrpo_ratings)
    logger.info(f"  [HRDO → HRPO] Verdict: {cross_audit_of_hrpo['verdict']}")

    logger.info("  [HRPO → HRDO] Cross-audit: checking for leniency bias...")
    cross_audit_of_hrdo = _cross_audit_hrdo(hrdo_assessments)
    logger.info(f"  [HRPO → HRDO] Verdict: {cross_audit_of_hrdo['verdict']}")

    # ── Non-IRM action required ───────────────────────────────────────────
    action_required = []
    for cn, r in hrpo_ratings.items():
        if r["rating"] in ("NOTICE", "CONCERN"):
            dev = hrdo_assessments.get(cn, {})
            action_required.append({
                "agent":            cn,
                "hrpo_rating":      r["rating"],
                "hrdo_rating":      dev.get("development_rating", "—"),
                "hrdo_trajectory":  dev.get("trajectory", "—"),
                "issues":           r.get("issues", []),
                "development_plan": dev.get("development_plan", []),
            })

    # ── Update HR history ─────────────────────────────────────────────────
    records = hr_history.get("agent_records", {})
    for cn, r in {**hrpo_ratings,
                  **({"IndependentRiskManager": irm_hrpo} if irm_hrpo else {})}.items():
        if cn not in records:
            records[cn] = {"rating_history": [], "first_evaluated": r["evaluated_at"]}
        records[cn]["rating_history"].append(r["rating"])
        records[cn]["last_evaluated"] = r["evaluated_at"]
        records[cn]["rating_history"] = records[cn]["rating_history"][-12:]
    hr_history["agent_records"]    = records
    hr_history["cycles_evaluated"] = hr_history.get("cycles_evaluated", 0) + 1
    _save_hr_history(hr_history)

    # ── Build non-IRM report (readable by IRM, relayed to Chairman) ───────
    firm_health = _compute_firm_health(hrpo_ratings, hrdo_assessments)
    report = {
        "generated_at":        datetime.now().isoformat(),
        "cycles_evaluated":    hr_history["cycles_evaluated"],
        "agents_reviewed":     len(agents),
        "reporting_chain":     "HR → IRM → Chairman",
        "irm_note":            "IRM evaluation is sealed in hr_irm_sealed.json — not in this file.",
        "hrpo_ratings":        hrpo_ratings,
        "hrdo_assessments":    hrdo_assessments,
        "hrpo_summary":        hrpo_summary,
        "hrdo_summary":        hrdo_summary,
        "cross_audit_of_hrpo": cross_audit_of_hrpo,
        "cross_audit_of_hrdo": cross_audit_of_hrdo,
        "action_required":     action_required,
        "firm_health_score":   firm_health,
    }

    os.makedirs("data", exist_ok=True)
    with open(HR_REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"\n  HR report saved → {HR_REPORT_PATH}")

    # ── Build and seal the IRM evaluation ────────────────────────────────
    # Base64-encode the IRM's evaluation so it is opaque to the IRM agent.
    # The IRM code reads this file and forwards it to the Chairman without
    # ever logging or rendering its contents.
    if irm_hrpo or irm_hrdo:
        irm_action = (irm_hrpo or {}).get("rating", "PASS") in ("NOTICE", "CONCERN")
        irm_eval_plain = {
            "generated_at":   datetime.now().isoformat(),
            "subject":        IRM_CLASS_NAME,
            "seal_notice":    (
                "SEALED — HR evaluation of the Internal Risk Monitor. "
                "This was produced independently by the HR team. "
                "The IRM has not seen this content."
            ),
            "hrpo_rating":    irm_hrpo,
            "hrdo_assessment": irm_hrdo,
            "action_required": irm_action,
        }
        sealed_json = json.dumps(irm_eval_plain, indent=2, default=str)
        sealed_b64  = base64.b64encode(sealed_json.encode("utf-8")).decode("ascii")

        sealed_output = {
            "generated_at": datetime.now().isoformat(),
            "seal_notice": (
                "SEALED HR ANNEX — IRM self-evaluation. "
                "Contents are base64-encoded. IRM pipeline passes this blob through "
                "to the Chairman without decoding. "
                "Decode: import base64,json; json.loads(base64.b64decode(d['sealed_b64']))"
            ),
            "sealed_b64": sealed_b64,
        }
        with open(HR_IRM_SEALED_PATH, "w") as f:
            json.dump(sealed_output, f, indent=2)
        # Log only that the sealed file was written — never the content
        irm_rating_hint = (irm_hrpo or {}).get("rating", "?")
        logger.info(
            f"  Sealed IRM evaluation written → {HR_IRM_SEALED_PATH} "
            f"[HRPO:{irm_rating_hint} — contents not logged]"
        )

    # ── Pipeline email: IRM handles relay — do not send directly ─────────
    # In standalone mode (python3 hr_agent.py), send_email=True fires a direct
    # email for debugging. In normal pipeline use, send_email=False (default)
    # and the IRM bundles HR findings into his own Chairman email.
    if send_email and action_required:
        _send_hr_email(report)

    logger.info(f"\n  Action required: {len(action_required)} agent(s)")
    logger.info(f"  Firm health score: {firm_health}/10")
    logger.info("=" * 65)

    # Broadcast workforce health to firm channel + report up to CIT Director
    try:
        from firm_channel import post as _fc_post, post_task_request as _fc_task
        _flagged = [a.get("agent","?") for a in action_required[:5]]
        _prio = "HIGH" if firm_health < 6 or len(action_required) > 5 else "NORMAL"
        _fc_post(
            "HR Agent", "PIPELINE_ALERT",
            f"Workforce check complete: {report.get('agents_reviewed',0)} agents evaluated. "
            f"Firm health {firm_health}/10. "
            f"Action required: {len(action_required)} agent(s)"
            + (f" [{', '.join(_flagged)}]" if _flagged else "") + ".",
            priority=_prio,
        )
        for ag in action_required[:3]:
            # BUG FIX: action_required items use "hrpo_rating" not "rating",
            # and "issues" (list) not "issue" (string)
            _rating = ag.get("hrpo_rating", "?")
            _issue_str = "; ".join(ag.get("issues", [])[:1])
            _fc_post(
                "HR Agent", "PIPELINE_ALERT",
                f"FLAGGED: {ag.get('agent','?')} — {_rating}: {_issue_str[:100]}",
                priority="HIGH" if _rating == "CONCERN" else "NORMAL",
            )

        # Report up to CIT Director (HR's supervisor)
        _flagged_str = (", ".join(_flagged) + " flagged for action. ") if _flagged else ""
        _hr_summary = (
            f"HR workforce review complete. "
            f"Firm health: {firm_health}/10. "
            f"{report.get('agents_reviewed', 0)} agents evaluated. "
            f"{_flagged_str}"
            f"Full report: data/hr_report.json"
        )
        _task_prio = "HIGH" if firm_health < 6 or len(action_required) > 3 else "NORMAL"
        _fc_task(
            "HR Agent", "CIT Director",
            _hr_summary,
            priority=_task_prio,
        )
    except Exception:
        pass

    # ── Domain Learning Phase ─────────────────────────────────────────────
    # HR reads each agent's recent outputs before writing coaching notes,
    # so notes reflect actual domain context rather than generic feedback.
    _domain_context = {}
    try:
        _domain_files = {
            "Manager Agent":        "data/manager_decisions.json",
            "Chief of Staff":       "data/chief_of_staff_report.json",
            "Risk Manager":         "data/risk_manager_report.json",
            "CIT Director":         "data/cit_morning_report.json",
            "R&D Team Leader":      "data/research_team_report.json",
            "Earnings Predictor":   "data/earnings_predictions.json",
            "International Analyst":"data/international_report.json",
            "Canary Agent":         "data/canary_state.json",
        }
        for _agent_name, _path in _domain_files.items():
            if os.path.exists(_path):
                try:
                    with open(_path) as _df:
                        _raw = json.load(_df)
                    # Extract domain-relevant summary for coaching context
                    if _agent_name == "Manager Agent":
                        _decisions = _raw if isinstance(_raw, list) else _raw.get("decisions", [])
                        _recent = _decisions[-10:] if isinstance(_decisions, list) else []
                        # BUG FIX: manager decisions use "manager_action"/"manager_decision" keys,
                        # not "action"/"decision" — wrong keys always produced count of 0
                        _domain_context[_agent_name] = {
                            "recent_decisions": len(_recent),
                            "buy_count":   sum(1 for d in _recent if d.get("manager_action") in ("BUY_NEW", "ADD")),
                            "trim_count":  sum(1 for d in _recent if d.get("manager_action") in ("TRIM", "SELL")),
                            "reject_count":sum(1 for d in _recent if d.get("manager_decision") == "REJECTED"),
                        }
                    elif _agent_name == "Risk Manager":
                        _domain_context[_agent_name] = {
                            "risk_level":    _raw.get("risk_level", "?"),
                            "risk_score":    _raw.get("risk_score", "?"),
                            "n_flags":       len(_raw.get("risk_flags_summary", [])),
                            "concentration": [f.get("ticker") for f in
                                              _raw.get("concentration", {}).get("flags", [])[:3]],
                        }
                    elif _agent_name == "R&D Team Leader":
                        _summ = _raw.get("summary", {})
                        _recs = _raw.get("recommendations", [])
                        _domain_context[_agent_name] = {
                            "total_picks":     len(_recs),
                            "high_conviction": sum(1 for r in _recs if r.get("conviction", 0) >= 4),
                            "sectors_covered": _summ.get("sectors_covered", "?"),
                            "top_picks":       [r.get("ticker") for r in _recs[:5]],
                        }
                    elif _agent_name == "CIT Director":
                        _board_q = _raw.get("board_queue", [])
                        _domain_context[_agent_name] = {
                            "proposals_pending": len(_board_q),
                            "high_priority":     sum(1 for p in _board_q if p.get("priority") == "HIGH"),
                            "sessions_run":      _raw.get("sessions_run", "?"),
                        }
                    else:
                        _domain_context[_agent_name] = {"loaded": True}
                except Exception:
                    pass
        logger.info(f"  [HR] Domain learning complete — context loaded for {len(_domain_context)} agents")
    except Exception as _dl_e:
        logger.debug(f"  [HR] Domain learning error (non-fatal): {_dl_e}")

    # ── Performance Ledger integration ────────────────────────────────────
    # Record HR self-evaluation and write coaching notes using domain context.
    try:
        from performance_ledger import record_self_evaluation, write_coaching_note, get_agent_stats

        # Record HR Agent's own self-evaluation (cycle summary)
        _hr_summary = (
            f"HR review cycle {hr_history.get('cycles_evaluated', 0)}. "
            f"Agents reviewed: {len(agents)}. Firm health: {firm_health}/10. "
            f"Action required: {len(action_required)} agent(s). "
            f"HRPO: {hrpo_summary}. HRDO: {hrdo_summary}."
        )
        record_self_evaluation("HR Agent", _hr_summary)

        # Write coaching notes for underperforming agents (NOTICE / CONCERN)
        for _cn, _r in hrpo_ratings.items():
            _rating = _r.get("rating", "PASS")
            if _rating in ("NOTICE", "CONCERN"):
                _issues = "; ".join(_r.get("issues", [])[:3])
                _dev = hrdo_assessments.get(_cn, {})
                _dev_plan = "; ".join(_dev.get("development_plan", [])[:2])
                # Enrich coaching note with actual domain context for this agent
                _ctx = _domain_context.get(_cn, {})
                _ctx_note = ""
                if _cn == "Manager Agent" and _ctx:
                    _ctx_note = (
                        f" Last cycle: {_ctx.get('buy_count',0)} buys, "
                        f"{_ctx.get('trim_count',0)} trims, "
                        f"{_ctx.get('reject_count',0)} rejected. "
                        f"Focus on position sizing discipline and stop-loss adherence."
                    )
                elif _cn == "Risk Manager" and _ctx:
                    # BUG FIX: _ctx['concentration'] is a list of ticker strings (already
                    # extracted as [f.get("ticker") for f in ...[:3]]), filter out None values
                    _conc_tickers = [t for t in _ctx.get("concentration", []) if t]
                    _ctx_note = (
                        f" Current risk level: {_ctx.get('risk_level','?')} "
                        f"({_ctx.get('n_flags',0)} active flags). "
                        f"Concentrated positions: {', '.join(_conc_tickers) or 'none'}."
                    )
                elif _cn == "R&D Team Leader" and _ctx:
                    _ctx_note = (
                        f" Last cycle: {_ctx.get('total_picks',0)} picks, "
                        f"{_ctx.get('high_conviction',0)} high-conviction (4★+). "
                        f"Top picks: {', '.join(_ctx.get('top_picks',[])[:3])}. "
                        f"Review conviction calibration against outcomes."
                    )
                _base_note = (
                    f"HR coaching ({datetime.now().strftime('%Y-%m-%d')}): "
                    f"Performance rating {_rating}. Issues: {_issues}."
                    + (f" Development plan: {_dev_plan}." if _dev_plan else "")
                    + _ctx_note
                )
                # Fable 5 LLM coaching note enrichment
                _note = _base_note
                try:
                    from apex_llm import reason as _llm_r, is_available as _llm_ok
                    if _llm_ok("morning_briefing"):
                        _raw = _llm_r(
                            "You are the Apex Capital HR Development Officer. You write "
                            "specific, actionable coaching notes for AI agents to improve "
                            "their performance. Be direct and concrete.",
                            f"Agent: {_cn} | Rating: {_rating}\n"
                            f"Issues: {_issues}\n"
                            f"Dev plan: {_dev_plan}\n"
                            f"Context: {_ctx_note or 'none'}\n\n"
                            "Write a 2-sentence coaching note: what specific behavior "
                            "needs to change and what metric will show improvement.",
                            agent_name="morning_briefing",
                            max_tokens=150,
                        )
                        if _raw:
                            _note = (
                                f"HR coaching ({datetime.now().strftime('%Y-%m-%d')}): "
                                f"Rating: {_rating}. {_raw.strip()}"
                            )
                except Exception:
                    pass
                write_coaching_note(_cn, _note)
                logger.info(f"  [HR] Coaching note written for {_cn} ({_rating})")
                # Log to issue tracker so underperformance gets tracked to resolution
                try:
                    import issue_tracker as _it
                    _it.log_issue(
                        reporter="HR Agent",
                        title=f"Agent underperformance: {_cn} rated {_rating}",
                        description=f"Issues: {_issues}. Development plan: {_dev_plan}",
                        severity="HIGH" if _rating == "CONCERN" else "MEDIUM",
                        category="PERFORMANCE",
                        auto_fix_hint=f"Review coaching note written for {_cn}. CoS to follow up.",
                    )
                except Exception as _it_err:
                    logger.debug(f"  [HR] Issue tracker log error (non-fatal): {_it_err}")

        # Also write notes for overperforming agents (PASS streak ≥ 5 cycles)
        for _cn, _rec in hr_history.get("agent_records", {}).items():
            _hist = _rec.get("rating_history", [])
            if len(_hist) >= 5 and all(r == "PASS" for r in _hist[-5:]):
                # Only write if no recent coaching note
                _stats = get_agent_stats(_cn)
                if not _stats.get("coaching_notes"):
                    write_coaching_note(
                        _cn,
                        f"HR coaching ({datetime.now().strftime('%Y-%m-%d')}): "
                        f"Excellent streak — 5+ consecutive PASS ratings. "
                        f"Consider broadening coverage or mentoring newer agents. "
                        f"Win rate: {_stats.get('win_rate', 0):.0%}."
                    )

    except Exception as _ledger_e:
        logger.debug(f"  [HR] Performance ledger update error (non-fatal): {_ledger_e}")

    # Add Performance Ledger section to report
    try:
        from performance_ledger import get_all_agent_stats
        report["performance_ledger"] = get_all_agent_stats()
    except Exception:
        pass

    # Attendance audit. HR historically audited its 39-name ROSTER, which is why an
    # agent that could not even import (international_analyst, for a month) was never
    # marked absent — it wasn't on the roster, so there was no one to mark absent.
    # This is a MANAGEMENT finding about wiring, never an agent rating: a module that
    # is never scheduled has done nothing wrong, so it must not touch PIP or ratings.
    try:
        report["attendance"] = audit_attendance()
    except Exception as _att_e:
        logger.debug(f"  [HR] Attendance audit error (non-fatal): {_att_e}")

    # Ratchet integrity: a shrink-only baseline (e.g. the orphan-agent list)
    # that suddenly loses entries needs independent corroboration, not a
    # rubber stamp from the same detector that cleared them. See
    # audit_ratchet_integrity() for the 2026-07-30 incident this closes.
    try:
        report["ratchet_integrity"] = audit_ratchet_integrity()
    except Exception as _ri_e:
        logger.debug(f"  [HR] Ratchet integrity audit error (non-fatal): {_ri_e}")

    # Retirement coverage: a retired agent's (sector, etf) lane going unfilled
    # is a coverage hole, not a headcount detail. See audit_retirement_coverage()
    # for the KOSPI precedent this generalizes.
    try:
        report["retirement_coverage"] = audit_retirement_coverage()
    except Exception as _rc_e:
        logger.debug(f"  [HR] Retirement coverage audit error (non-fatal): {_rc_e}")

    # Roster coverage: does this review cover every agent the firm employs? The
    # three audits above all read agent_registry.json, so all three inherit its
    # blind spot — 67 rows against a 139-agent firm. This one measures HR
    # against the canonical roster instead, and it is the only check in the
    # building that can see HR itself.
    try:
        report["roster_coverage"] = audit_roster_coverage()
    except Exception as _rz_e:
        logger.debug(f"  [HR] Roster coverage audit error (non-fatal): {_rz_e}")

    # Weekly firm sweep: HR's inbox. This is the ONLY consumer of
    # data/hr_action_queue.json — without it the sweep would be routing findings
    # into a file nobody opens, which is the exact bug class the sweep detects.
    try:
        report["sweep_queue"] = review_sweep_queue()
    except Exception as _sq_e:
        logger.debug(f"  [HR] Sweep queue review error (non-fatal): {_sq_e}")

    return report


def _compute_firm_health(hrpo_ratings: dict, hrdo_assessments: dict) -> float:
    """Weighted firm health score 0–10 across all agents."""
    total_weight = 0.0
    total_score  = 0.0
    rating_scores = {"PASS": 10, "WATCH": 7, "NOTICE": 4, "CONCERN": 1}

    for cn, r in hrpo_ratings.items():
        goals  = _get_goals(cn)
        weight = goals.get("weight", 1.0)
        score  = rating_scores.get(r["rating"], 5)
        total_weight += weight
        total_score  += score * weight

    return round(total_score / total_weight, 1) if total_weight > 0 else 10.0


def _send_hr_email(report: dict):
    """Email the Chairman when agents require HR action."""
    try:
        from email_sender import send_daily_report
        from creative_agent import polish_email

        action  = report["action_required"]
        health  = report["firm_health_score"]
        now     = datetime.now().strftime("%B %d, %Y at %H:%M")

        rows = ""
        for item in action:
            color = "#f85149" if item["hrpo_rating"] == "CONCERN" else "#f0883e"
            rows += f"""
            <tr>
              <td style="padding:8px 12px;color:#e6edf3;">{item['agent']}</td>
              <td style="padding:8px 12px;color:{color};font-weight:bold;">{item['hrpo_rating']}</td>
              <td style="padding:8px 12px;color:#8b949e;">{item['hrdo_rating']} / {item['hrdo_trajectory']}</td>
              <td style="padding:8px 12px;color:#c9d1d9;">{'; '.join(item['issues'][:2])[:100]}</td>
            </tr>"""

        cross_hrpo = report["cross_audit_of_hrpo"]
        cross_hrdo = report["cross_audit_of_hrdo"]
        hrpo_color = "#f85149" if "BIAS" in cross_hrpo["verdict"] else "#3fb950"
        hrdo_color = "#f85149" if "BIAS" in cross_hrdo["verdict"] else "#3fb950"

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:960px;margin:0 auto;">

  <div style="border-left:4px solid #f0883e;padding-left:20px;margin-bottom:24px;">
    <h1 style="color:#f0883e;margin:0 0 6px 0;font-size:26px;">📋 HR Team Review</h1>
    <p style="color:#8b949e;margin:0;font-size:13px;">{now} &nbsp;·&nbsp;
       {report['agents_reviewed']} agents reviewed &nbsp;·&nbsp;
       Firm health: {health}/10</p>
  </div>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:10px;">Agents Requiring Attention</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;margin-bottom:24px;">
    <thead>
      <tr style="border-bottom:1px solid #30363d;">
        <th style="padding:10px 12px;color:#8b949e;text-align:left;font-size:12px;">AGENT</th>
        <th style="padding:10px 12px;color:#8b949e;text-align:left;font-size:12px;">HRPO RATING</th>
        <th style="padding:10px 12px;color:#8b949e;text-align:left;font-size:12px;">HRDO / TRAJECTORY</th>
        <th style="padding:10px 12px;color:#8b949e;text-align:left;font-size:12px;">ISSUES</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:10px;">Mutual Cross-Audit</h2>
  <div style="display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap;">
    <div style="flex:1;min-width:280px;background:#161b22;border:1px solid #30363d;
                border-radius:8px;padding:14px 18px;">
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:6px;">
        HRDO → HRPO (harshness check)</div>
      <div style="color:{hrpo_color};font-weight:bold;">{cross_hrpo['verdict']}</div>
      <div style="color:#c9d1d9;font-size:13px;margin-top:6px;">
        {cross_hrpo['notes'][0][:160] if cross_hrpo['notes'] else '—'}</div>
    </div>
    <div style="flex:1;min-width:280px;background:#161b22;border:1px solid #30363d;
                border-radius:8px;padding:14px 18px;">
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:6px;">
        HRPO → HRDO (leniency check)</div>
      <div style="color:{hrdo_color};font-weight:bold;">{cross_hrdo['verdict']}</div>
      <div style="color:#c9d1d9;font-size:13px;margin-top:6px;">
        {cross_hrdo['notes'][0][:160] if cross_hrdo['notes'] else '—'}</div>
    </div>
  </div>

  <p style="color:#484f58;font-size:11px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital Intelligence — HR Team &nbsp;·&nbsp;
    HR Performance Officer · HR Development Officer &nbsp;·&nbsp; {now}
  </p>
</body></html>"""

        subject = (
            f"Apex Capital — HR Review | {len(action)} agent(s) flagged | "
            f"Firm health {health}/10 | {datetime.now().strftime('%b %d')}"
        )
        key_metrics = {
            "Agents Reviewed": report["agents_reviewed"],
            "Flagged":         len(action),
            "Firm Health":     f"{health}/10",
        }
        polished = polish_email(html, subject, "hr_team", key_metrics)
        send_daily_report(html_body=polished, subject=subject,
                          paper_day=0, sender_name="hr_team")
        logger.info("  ✅ HR report emailed to Chairman")
    except Exception as e:
        logger.warning(f"  HR email error: {e}")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    email = "--no-email" not in sys.argv
    result = run_hr_review(send_email=email)
    print(f"\n  Firm health score: {result['firm_health_score']}/10")
    print(f"  Agents requiring action: {len(result['action_required'])}")
    for item in result["action_required"]:
        print(f"    [{item['hrpo_rating']}] {item['agent']}: "
              f"{'; '.join(item['issues'][:1])[:80]}")

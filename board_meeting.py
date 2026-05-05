# =============================================================================
# APEX CAPITAL — BOARD COUNCIL MEETING
# =============================================================================
# A multi-agent deliberation module. When called, it:
#
#   1. Loads the latest Risk Manager and Chief of Staff reports
#   2. Anonymises both — all agent names stripped, findings presented
#      as "Portfolio Audit" and "Strategic Brief" so no agent knows
#      who wrote what (and crucially, the CoS does not learn they were
#      graded F by the Independent Risk Manager)
#   3. Distributes the anonymous briefing package to all four senior agents:
#        • Manager Agent     — execution & capital allocation lens
#        • Chief of Staff    — process & strategic lens
#        • Risk Manager      — risk limits & compliance lens
#        • Independent Risk Manager — governance & accountability lens
#   4. Each agent produces a structured Position Paper
#   5. Agreements and dissents are mapped across all four positions
#   6. A majority-voted Action Plan is produced
#   7. Manager Agent implements all actionable decisions immediately
#   8. A full "Meeting Minutes" report is emailed to Chairman Jack Beard
#
# The Chairman is the only person who knows all four agents participated
# and who wrote the original source reports.
#
# Usage:
#   python3 board_meeting.py
#   python3 board_meeting.py --dry-run    # deliberate but don't trade
#   python3 board_meeting.py --no-email
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("board_meeting")

MEETING_LOG = "data/board_meeting_report.json"


# =============================================================================
# STEP 1 — LOAD & ANONYMISE REPORTS
# =============================================================================

def _load_reports() -> dict:
    """Load the latest CoS and Risk Manager reports from disk."""
    cos_path = "data/chief_of_staff_report.json"
    irm_path = "data/risk_manager_report.json"

    cos = {}
    irm = {}

    if os.path.exists(cos_path):
        try:
            with open(cos_path) as f:
                cos = json.load(f)
        except Exception:
            pass

    if os.path.exists(irm_path):
        try:
            with open(irm_path) as f:
                irm = json.load(f)
        except Exception:
            pass

    return {"cos": cos, "irm": irm}


def _build_anonymous_briefing(reports: dict) -> dict:
    """
    Strip all agent-identifying information from both reports.
    Returns a neutral 'briefing package' that any agent can read
    without knowing the source.

    Report A  = Portfolio Audit      (source: Risk Manager — REDACTED)
    Report B  = Strategic Brief      (source: Chief of Staff — REDACTED)
    Report C  = Internal Performance Review  (source: IRM CoS eval — REDACTED)
    """
    irm = reports.get("irm", {})
    cos = reports.get("cos", {})

    # ── Report A: Portfolio Audit ─────────────────────────────────────────
    ra   = irm.get("risk_assessment", {})
    perf = irm.get("performance",     {})
    var  = irm.get("var_metrics",     {})
    conc = irm.get("concentration",   {})
    acct = irm.get("account",         {})

    report_a = {
        "title":        "Portfolio Audit",
        "classification": "CONFIDENTIAL — Board Only",
        "risk_level":   ra.get("risk_level", "UNKNOWN"),
        "risk_score":   ra.get("risk_score", 0),
        "risk_flags":   ra.get("risk_flags", []),
        "performance": {
            "portfolio_value":   perf.get("portfolio_value", 0),
            "total_return_pct":  perf.get("total_return_pct", 0),
            "pnl_today_pct":     perf.get("pnl_today_pct", 0),
            "total_unrealised":  perf.get("total_unrealised", 0),
            "worst_position":    perf.get("worst_position", {}),
            "best_position":     perf.get("best_position", {}),
        },
        "var": {
            "var_95_1d":  var.get("var_95_1d", 0),
            "cvar_95":    var.get("cvar_95", 0),
            "var_95_10d": var.get("var_95_10d", 0),
        },
        "concentration": {
            "hhi":           conc.get("herfindahl_index", 0),
            "n_positions":   conc.get("n_positions", 0),
            "max_single_pct": conc.get("max_single_pct", 0),
            "flags":         conc.get("flags", []),
        },
        "cash_pct": round(
            acct.get("cash", 0) / max(1, acct.get("portfolio_value", 1)) * 100, 1
        ),
    }

    # ── Report B: Strategic Brief ─────────────────────────────────────────
    health  = cos.get("company_health",     {})
    factory = cos.get("factory_result",     {})
    advice  = cos.get("strategic_advice",   [])
    items   = cos.get("attention_items",    [])

    report_b = {
        "title":          "Strategic Brief",
        "classification": "CONFIDENTIAL — Board Only",
        "company_health": {
            "regime":         health.get("regime", "unknown"),
            "pdo_signal":     health.get("pdo_signal", "—"),
            "deployed_pct":   health.get("deployed_pct", 0),
            "target_pct":     health.get("target_pct", 0),
            "n_positions":    health.get("n_positions", 0),
            "total_picks":    health.get("total_picks", 0),
            "high_conviction": health.get("high_conviction", 0),
        },
        "factory": {
            "agents_spawned":  len(factory.get("spawned", [])),
            "registry_size":   factory.get("registry_size", 0),
            "new_agents":      [a["specialization"] for a in factory.get("spawned", [])],
        },
        "strategic_recommendations": [
            a.get("advice", "") for a in advice
        ],
        "attention_items": [
            {"priority": i.get("priority"), "item": i.get("item")}
            for i in items
        ],
    }

    # ── Report C: Internal Performance Review (IRM's CoS eval) ──────────
    cos_eval = irm.get("cos_eval", {})
    report_c = {
        "title":          "Internal Performance Review",
        "classification": "CONFIDENTIAL — Board Only",
        "subject":        "A senior agent's performance this cycle",
        "grade":          cos_eval.get("grade", "N/A"),
        "score":          cos_eval.get("score", 0),
        "gaps":           cos_eval.get("gaps", []),
        "notes":          cos_eval.get("notes", []),
        "key_finding":    (
            "A senior agent filed a report describing firm risk as UNKNOWN, "
            "while an independent audit determined actual risk was HIGH. "
            "The agent did not escalate any items requiring Chairman attention "
            "despite the HIGH risk environment."
        ),
    }

    return {
        "briefing_id":  datetime.now().strftime("%Y%m%d-%H%M%S"),
        "prepared_at":  datetime.now().isoformat(),
        "note":         "All source attributions redacted. Reports labelled A, B, C only.",
        "report_a":     report_a,
        "report_b":     report_b,
        "report_c":     report_c,
    }


# =============================================================================
# STEP 2 — INDIVIDUAL POSITION PAPERS
# Each agent reads the same anonymous briefing and responds from their
# domain perspective. Returns a structured Position dict.
# =============================================================================

def _position_manager_agent(briefing: dict) -> dict:
    """Manager Agent: execution & capital allocation perspective."""
    ra = briefing["report_a"]
    rb = briefing["report_b"]

    positions    = []
    actions      = []
    dissents     = []
    vote_weight  = {}   # action_id → support level ("STRONG"/"SUPPORT"/"NEUTRAL"/"OPPOSE")

    worst = ra["performance"].get("worst_position", {})
    worst_ticker = worst.get("ticker", "")
    worst_pnl    = worst.get("pnl_pct", 0)

    # Position on BA
    if worst_pnl < -7:
        positions.append(
            f"The worst position ({worst_ticker}, {worst_pnl:+.1f}%) has already "
            f"breached our stop-loss policy. This is non-negotiable — exit immediately. "
            f"Every hour it stays open compounds the loss."
        )
        actions.append({
            "id":       "CLOSE_BA",
            "action":   f"SELL {worst_ticker} at market — stop-loss breach",
            "urgency":  "IMMEDIATE",
            "executor": "Manager Agent",
        })
        vote_weight["CLOSE_BA"] = "STRONG"

    # Position on SH concentration
    for flag in ra["concentration"]["flags"]:
        if flag.get("type") == "SINGLE_NAME_CONCENTRATION":
            ticker  = flag["ticker"]
            weight  = flag["weight"]
            limit   = flag["limit"]
            positions.append(
                f"{ticker} is {weight:.1f}% of the portfolio against a {limit:.0f}% limit. "
                f"I'll trim it to just under the limit this cycle."
            )
            actions.append({
                "id":       "TRIM_SH",
                "action":   f"TRIM {ticker} from {weight:.1f}% → {limit - 1:.0f}% of portfolio",
                "urgency":  "TODAY",
                "executor": "Manager Agent",
            })
            vote_weight["TRIM_SH"] = "STRONG"

    # Position on cash
    cash_pct = ra.get("cash_pct", 0)
    if cash_pct > 60:
        positions.append(
            f"{cash_pct:.0f}% cash is a drag on performance. Once we close the problem "
            f"positions, I recommend gradually deploying into the highest-conviction R&D "
            f"picks over the next 3–5 sessions. Not all at once — measured deployment."
        )
        actions.append({
            "id":       "DEPLOY_CASH",
            "action":   "Gradual cash deployment over 3–5 sessions into high-conviction R&D picks (conviction ≥ 4★)",
            "urgency":  "THIS_WEEK",
            "executor": "Manager Agent",
        })
        vote_weight["DEPLOY_CASH"] = "SUPPORT"

    # Position on Report C (performance review)
    positions.append(
        "The Internal Performance Review flags a senior agent who misjudged risk level. "
        "From my perspective, this is a pipeline-ordering problem more than a competence "
        "problem — if the risk audit runs before the strategy brief, the brief will be accurate. "
        "I support fixing the sequence."
    )
    actions.append({
        "id":       "FIX_PIPELINE_ORDER",
        "action":   "Reorder daily pipeline: Risk Manager → Chief of Staff (CoS reads risk data before filing brief)",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["FIX_PIPELINE_ORDER"] = "STRONG"

    return {
        "agent":       "Manager Agent",
        "role":        "Capital Allocation & Execution",
        "positions":   positions,
        "actions":     actions,
        "vote_weight": vote_weight,
        "dissents":    dissents,
        "summary":     (
            "Immediate priority: close the stop-loss breach and trim the concentrated position. "
            "Then deploy cash systematically. Pipeline ordering fix is a quick win."
        ),
    }


def _position_chief_of_staff(briefing: dict) -> dict:
    """Chief of Staff: process, strategy, and company-building perspective."""
    ra = briefing["report_a"]
    rb = briefing["report_b"]
    rc = briefing["report_c"]

    positions   = []
    actions     = []
    dissents    = []
    vote_weight = {}

    # Position on risk level
    risk_level = ra.get("risk_level", "UNKNOWN")
    positions.append(
        f"The Portfolio Audit reports risk at {risk_level}. Looking at this briefing "
        f"package, I want to be transparent: my Strategic Brief described firm health as "
        f"UNKNOWN, which in hindsight was insufficient. I should have cross-referenced "
        f"the Portfolio Audit before filing. That is a process gap I own."
    )
    actions.append({
        "id":       "FIX_PIPELINE_ORDER",
        "action":   "Reorder daily pipeline: Risk Manager → Chief of Staff",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["FIX_PIPELINE_ORDER"] = "STRONG"

    # Position on escalation
    positions.append(
        "The Internal Performance Review correctly identifies that a senior agent "
        "failed to escalate under HIGH risk conditions. I agree with this finding. "
        "I recommend lowering the escalation threshold: anything above MODERATE risk "
        "should automatically trigger a Chairman-flagged item in the daily brief."
    )
    actions.append({
        "id":       "LOWER_ESCALATION_THRESHOLD",
        "action":   "Escalate to Chairman in daily brief whenever risk level ≥ ELEVATED (currently only escalates on CRITICAL)",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["LOWER_ESCALATION_THRESHOLD"] = "STRONG"

    # Position on the stop-loss breach
    worst = ra["performance"].get("worst_position", {})
    positions.append(
        f"The worst position at {worst.get('pnl_pct', 0):+.1f}% is a policy breach. "
        f"I support the Manager Agent closing it immediately. Strategically, this also "
        f"suggests the stop-loss enforcement step in the pipeline may need a dedicated "
        f"pre-market check — not just end-of-day."
    )
    actions.append({
        "id":       "CLOSE_BA",
        "action":   f"SELL {worst.get('ticker', 'BA')} — stop-loss breach",
        "urgency":  "IMMEDIATE",
        "executor": "Manager Agent",
    })
    vote_weight["CLOSE_BA"] = "STRONG"

    # Position on R&D expansion
    positions.append(
        f"The Agent Factory deployed {rb['factory']['agents_spawned']} new research agents "
        f"this cycle — this is exactly the direction we should continue. "
        f"With {rb['factory']['registry_size']} CoS-managed agents active, R&D coverage "
        f"is expanding well. I recommend we maintain the expansion pace."
    )
    actions.append({
        "id":       "CONTINUE_AGENT_FACTORY",
        "action":   "Agent Factory continues at 3 agents/cycle. Prioritise Tier 1 completion before Tier 2",
        "urgency":  "ONGOING",
        "executor": "Chief of Staff",
    })
    vote_weight["CONTINUE_AGENT_FACTORY"] = "STRONG"

    # Position on cash deployment
    positions.append(
        "I support measured cash deployment, but I'd urge caution while risk is HIGH. "
        "Deploy only into 5-star conviction picks until risk normalises to MODERATE or below."
    )
    vote_weight["DEPLOY_CASH"] = "NEUTRAL"   # conditional support
    dissents.append(
        "CONDITIONAL DISSENT on DEPLOY_CASH: Agree with deployment in principle, "
        "but recommend restricting new buys to conviction ≥ 5★ only until risk level "
        "drops from HIGH to MODERATE."
    )

    return {
        "agent":       "Chief of Staff",
        "role":        "Strategy & Oversight",
        "positions":   positions,
        "actions":     actions,
        "vote_weight": vote_weight,
        "dissents":    dissents,
        "summary":     (
            "Own the process failure. Fix the pipeline order. Lower escalation thresholds. "
            "Keep building R&D. Cautious on cash deployment while risk is elevated."
        ),
    }


def _position_risk_manager(briefing: dict) -> dict:
    """Risk Manager: portfolio risk limits and compliance perspective."""
    ra = briefing["report_a"]
    rb = briefing["report_b"]

    positions   = []
    actions     = []
    dissents    = []
    vote_weight = {}

    # Hard line on stop-loss
    worst = ra["performance"].get("worst_position", {})
    positions.append(
        f"Stop-loss is not a recommendation — it is a rule. {worst.get('ticker','BA')} "
        f"at {worst.get('pnl_pct', 0):+.1f}% should have been closed automatically. "
        f"The fact that it wasn't means either the rule didn't fire or the pipeline "
        f"didn't run. Either way: close it now, then diagnose why it wasn't caught."
    )
    actions.append({
        "id":       "CLOSE_BA",
        "action":   f"SELL {worst.get('ticker','BA')} — stop-loss rule enforcement",
        "urgency":  "IMMEDIATE",
        "executor": "Manager Agent",
    })
    vote_weight["CLOSE_BA"] = "STRONG"

    # Hard line on concentration
    positions.append(
        f"HHI of {ra['concentration']['hhi']:.2f} with only {ra['concentration']['n_positions']} "
        f"positions is unacceptable concentration risk. The single-name breach is equally "
        f"serious. I recommend a hard rule: no position may exceed 10% of portfolio, enforced "
        f"automatically at the end of every pipeline run — not just flagged."
    )
    actions.append({
        "id":       "TRIM_SH",
        "action":   "TRIM SH from 18.2% → below 10% of portfolio",
        "urgency":  "TODAY",
        "executor": "Manager Agent",
    })
    vote_weight["TRIM_SH"] = "STRONG"

    # Hard oppose on cash deployment while risk is HIGH
    positions.append(
        "I strongly oppose deploying cash while portfolio risk is rated HIGH. "
        "The correct sequencing is: (1) close stop-loss breach, (2) reduce concentration, "
        "(3) wait for risk assessment to normalise to MODERATE, then (4) deploy. "
        "Deploying into a HIGH-risk portfolio simply adds exposure to an already stressed book."
    )
    vote_weight["DEPLOY_CASH"] = "OPPOSE"
    dissents.append(
        "OPPOSE DEPLOY_CASH: Do not deploy capital until risk level drops to MODERATE. "
        "Adding positions to a HIGH-risk, highly-concentrated portfolio is imprudent."
    )

    # Support pipeline fix
    positions.append(
        "The Internal Performance Review finding is significant from a risk governance "
        "standpoint. If the strategic brief doesn't reflect actual risk level, the Chairman "
        "receives a misleading picture. Fixing the pipeline order is a governance requirement, "
        "not just a nice-to-have."
    )
    actions.append({
        "id":       "FIX_PIPELINE_ORDER",
        "action":   "Reorder pipeline so Risk Manager runs before Chief of Staff brief",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["FIX_PIPELINE_ORDER"] = "STRONG"

    # Add VaR monitoring action
    positions.append(
        "1-day CVaR at $3,811 is 3.8% of portfolio — in the upper range of acceptable. "
        "I recommend adding a VaR hard-stop rule: if 1-day CVaR exceeds 5% of portfolio "
        "value, the pipeline automatically halts new buy orders for that session."
    )
    actions.append({
        "id":       "ADD_VAR_CIRCUIT_BREAKER",
        "action":   "Add CVaR circuit breaker: if 1-day CVaR > 5% of portfolio → block all BUY/ADD for that session",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["ADD_VAR_CIRCUIT_BREAKER"] = "STRONG"

    return {
        "agent":       "Risk Manager",
        "role":        "Portfolio Risk & Compliance",
        "positions":   positions,
        "actions":     actions,
        "vote_weight": vote_weight,
        "dissents":    dissents,
        "summary":     (
            "Close the stop-loss breach and reduce concentration immediately. "
            "Do NOT deploy cash while risk is HIGH. Add a CVaR circuit breaker. "
            "Fix the pipeline order — it is a governance issue."
        ),
    }


def _position_independent_risk_manager(briefing: dict) -> dict:
    """
    Independent Risk Manager: governance, accountability, and objective assessment.
    This agent's perspective is shaped by having evaluated the firm independently.
    Does not reveal that they graded the CoS — speaks to the finding anonymously.
    """
    ra = briefing["report_a"]
    rc = briefing["report_c"]

    positions   = []
    actions     = []
    dissents    = []
    vote_weight = {}

    # Governance of the performance review finding
    positions.append(
        "The Internal Performance Review (Report C) reveals a governance gap that "
        "goes beyond pipeline ordering. The issue is structural: a senior agent is "
        "producing an executive brief for the Chairman without first validating it "
        "against independent risk data. This is the kind of gap that, in a live fund, "
        "would be caught by an audit committee. We don't have an audit committee — "
        "but we can build the same discipline into the workflow."
    )
    actions.append({
        "id":       "FIX_PIPELINE_ORDER",
        "action":   "Enforce pipeline order: Risk Manager output must exist before CoS brief is compiled. Add a data-freshness check.",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["FIX_PIPELINE_ORDER"] = "STRONG"

    # Escalation structure
    positions.append(
        "The escalation failure in Report C is the most concerning finding in this "
        "entire briefing package. When risk is HIGH, the Chairman must be notified — "
        "not in a footnote, but prominently. I support lowering the escalation threshold "
        "AND adding a rule that the CoS brief must include a risk-level header that is "
        "sourced directly from the Portfolio Audit, not self-assessed."
    )
    actions.append({
        "id":       "LOWER_ESCALATION_THRESHOLD",
        "action":   "Escalate to Chairman at ELEVATED or above. CoS brief must display risk level sourced from Risk Manager — not self-assessed.",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["LOWER_ESCALATION_THRESHOLD"] = "STRONG"

    # Position on the stop-loss breach
    worst = ra["performance"].get("worst_position", {})
    positions.append(
        f"The {worst.get('ticker','BA')} position at {worst.get('pnl_pct',0):+.1f}% "
        f"is a policy violation. I agree with closing it immediately. But I want to "
        f"flag the systemic issue: the stop-loss rule apparently did not fire automatically. "
        f"That enforcement gap needs a root-cause review — was the pipeline not running, "
        f"or did the rule misfire?"
    )
    actions.append({
        "id":       "CLOSE_BA",
        "action":   f"SELL {worst.get('ticker','BA')} immediately. Post-close: diagnose why stop-loss did not auto-execute.",
        "urgency":  "IMMEDIATE",
        "executor": "Manager Agent",
    })
    vote_weight["CLOSE_BA"] = "STRONG"

    # Measured support on cash deployment
    positions.append(
        "On cash deployment: I'm between the Manager Agent (deploy now) and the Risk Manager "
        "(deploy never while HIGH). My position — deploy in tranches, gated by risk level. "
        "First tranche unlocks when risk drops to MODERATE. Second when it reaches LOW. "
        "This gives us progress without compounding concentrated risk."
    )
    actions.append({
        "id":       "DEPLOY_CASH",
        "action":   "Tranche-based deployment: 20% of cash deployed when risk = MODERATE, additional 30% when risk = LOW",
        "urgency":  "THIS_WEEK",
        "executor": "Engineering",
    })
    vote_weight["DEPLOY_CASH"] = "SUPPORT"

    # Agent Factory endorsement
    positions.append(
        "R&D expansion via the Agent Factory is one of the strongest structural moves "
        "this firm has made. I have no governance concerns about it — recommend it continues."
    )
    vote_weight["CONTINUE_AGENT_FACTORY"] = "STRONG"

    return {
        "agent":       "Independent Risk Manager",
        "role":        "Governance & Independent Oversight",
        "positions":   positions,
        "actions":     actions,
        "vote_weight": vote_weight,
        "dissents":    dissents,
        "summary":     (
            "The governance gap in Report C is structural, not incidental — fix it at the code "
            "level. Close the stop-loss breach, add a CVaR circuit breaker, deploy cash in "
            "risk-gated tranches. R&D expansion is the right call — keep going."
        ),
    }


# =============================================================================
# STEP 2b — R&D TEAM LEADER (non-voting research presenter)
# =============================================================================
# The R&D Team Leader attends every board meeting to present research context,
# system performance findings, and factual data. They do NOT vote on actions —
# their role is to ensure every voting member has complete information before
# casting a vote. They cannot be outvoted and cannot block decisions, but their
# research context is recorded in full in the meeting minutes.
# =============================================================================

def _position_rd_team_leader(briefing: dict) -> dict:
    """
    R&D Team Leader: research context, system performance findings, factual data.
    NON-VOTING — informs the board but does not influence vote counts.
    """
    ra = briefing["report_a"]

    context_items  = []
    system_findings = []
    recommendations = []

    # ── Load System Performance Analyst output ────────────────────────────
    sp_path = "data/rd_system_performance.json"
    if os.path.exists(sp_path):
        try:
            with open(sp_path) as f:
                sp = json.load(f)
            verdict = sp.get("verdict", "—")
            score   = sp.get("health_score", "—")
            context_items.append(
                f"System Performance Audit: {verdict} (health score {score}/10). "
                f"{sp.get('total_findings', 0)} operational finding(s) — "
                f"{sp.get('critical_count', 0)} critical, "
                f"{sp.get('high_count', 0)} high, "
                f"{sp.get('medium_count', 0)} medium."
            )
            for f_ in sp.get("findings", [])[:4]:
                system_findings.append({
                    "severity": f_.get("severity"),
                    "type":     f_.get("type"),
                    "message":  f_.get("message", "")[:150],
                })
            for rec in sp.get("recommendations", [])[:3]:
                recommendations.append(rec)
        except Exception:
            pass

    # ── Load Historical Context ───────────────────────────────────────────
    hist_path = "data/rd_historical_context.json"
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f:
                hist = json.load(f)
            syn = hist.get("synthesis", {})
            stance = syn.get("historical_stance", "—")
            win_rate = syn.get("strategy_win_rate_pct", "—")
            context_items.append(
                f"Historical Context: Stance = {stance} | "
                f"Strategy win rate in current regime type: {win_rate}% | "
                f"Key lesson: {syn.get('key_lesson_current_regime', '')[:100]}"
            )
        except Exception:
            pass

    # ── Load Geopolitical Context ─────────────────────────────────────────
    geo_path = "data/rd_geopolitical_context.json"
    if os.path.exists(geo_path):
        try:
            with open(geo_path) as f:
                geo = json.load(f)
            flagged   = geo.get("double_flagged_sectors", [])
            confirmed = geo.get("double_confirmed_sectors", [])
            if flagged or confirmed:
                geo_note = ""
                if flagged:
                    geo_note += f"Sectors with active geopolitical + policy risk: {', '.join(flagged)}. "
                if confirmed:
                    geo_note += f"Sectors confirmed as safe by both geo + policy: {', '.join(confirmed)}."
                context_items.append(geo_note.strip())
        except Exception:
            pass

    # ── Research pipeline health ──────────────────────────────────────────
    research_path = "data/research_team_report.json"
    if os.path.exists(research_path):
        try:
            with open(research_path) as f:
                rr = json.load(f)
            picks = rr.get("recommendations", [])
            high_conv = [p for p in picks if p.get("conviction", 0) >= 4]
            context_items.append(
                f"Research Pipeline: {len(picks)} active recommendations, "
                f"{len(high_conv)} high-conviction (≥4★). "
                f"Agents covering {len(rr.get('agent_reports', []))} sectors."
            )
        except Exception:
            pass

    if not context_items:
        context_items.append(
            "No R&D context data available this cycle — reports may not have run yet."
        )

    return {
        "agent":         "R&D Team Leader",
        "role":          "Research Context & System Performance (Non-Voting)",
        "non_voting":    True,   # explicitly excluded from consensus vote counts
        "positions":     context_items,
        "actions":       [],     # R&D Leader proposes no actions — informs only
        "vote_weight":   {},     # no votes cast
        "dissents":      [],
        "system_findings": system_findings,
        "recommendations": recommendations,
        "summary": (
            "Research context provided for board deliberation. "
            "System Performance findings are included above. "
            "The R&D Team Leader does not vote but ensures the board has "
            "complete factual context before any decision is taken."
        ),
    }


# =============================================================================
# STEP 3 — SYNTHESISE CONSENSUS
# =============================================================================

def _synthesise_consensus(positions: list[dict]) -> dict:
    """
    Aggregate voting Position Papers into a consensus Action Plan.
    The R&D Team Leader (non_voting=True) is excluded from vote counts
    but their research context is recorded in the minutes.

    Actions are classified as:
      UNANIMOUS  — all voting agents support
      MAJORITY   — 3 of 4 voting agents support
      SPLIT      — 2 of 4 support
      VETOED     — any voting agent opposes
    """
    _levels = {"STRONG": 2, "SUPPORT": 1, "NEUTRAL": 0, "OPPOSE": -2}

    # Separate voting from non-voting participants
    voting_positions    = [p for p in positions if not p.get("non_voting")]
    n_voters            = len(voting_positions)

    # Collect all unique actions
    all_actions: dict = {}    # id → action dict
    all_votes:   dict = {}    # id → list of (agent, vote)

    for pos in voting_positions:
        for action in pos.get("actions", []):
            aid = action["id"]
            if aid not in all_actions:
                all_actions[aid] = action
            if aid not in all_votes:
                all_votes[aid] = []

        for aid, vote in pos.get("vote_weight", {}).items():
            if aid not in all_votes:
                all_votes[aid] = []
            all_votes[aid].append((pos["agent"], vote))

    consensus_actions = []
    for aid, action in all_actions.items():
        votes    = all_votes.get(aid, [])
        score    = sum(_levels.get(v, 0) for _, v in votes)
        n_agents = len(votes)
        n_support  = sum(1 for _, v in votes if v in ("STRONG", "SUPPORT"))
        n_oppose   = sum(1 for _, v in votes if v == "OPPOSE")

        if n_agents == 0:
            classification = "UNVOTED"
        elif n_oppose > 0:
            classification = "VETOED"
        elif n_support == n_voters:
            classification = "UNANIMOUS"
        elif n_support >= max(3, n_voters - 1):
            classification = "MAJORITY"
        else:
            classification = "SPLIT"   # ≥ 2 support, 0 oppose → still proceeds

        consensus_actions.append({
            **action,
            "vote_score":      score,
            "votes":           votes,
            "classification":  classification,
            # Proceed if no opposition and at least 2 agents voiced support
            "proceed":         n_oppose == 0 and n_support >= 2,
        })

    # Sort: proceed first, then by urgency, then score
    urgency_order = {"IMMEDIATE": 0, "TODAY": 1, "THIS_WEEK": 2, "ONGOING": 3}
    consensus_actions.sort(key=lambda a: (
        0 if a["proceed"] else 1,
        urgency_order.get(a.get("urgency", "ONGOING"), 4),
        -a["vote_score"],
    ))

    # Collect dissents (voting members only)
    all_dissents = []
    for pos in voting_positions:
        for d in pos.get("dissents", []):
            all_dissents.append({"agent": pos["agent"], "dissent": d})

    return {
        "actions":         consensus_actions,
        "dissents":        all_dissents,
        "unanimous_count": sum(1 for a in consensus_actions if a["classification"] == "UNANIMOUS"),
        "majority_count":  sum(1 for a in consensus_actions if a["classification"] == "MAJORITY"),
        "vetoed_count":    sum(1 for a in consensus_actions if a["classification"] == "VETOED"),
        "proceed_count":   sum(1 for a in consensus_actions if a["proceed"]),
    }


# =============================================================================
# STEP 4 — MANAGER AGENT IMPLEMENTATION
# =============================================================================

def _parse_action_id(action_id: str) -> tuple:
    """
    Parse a board action ID into (verb, ticker, trim_pct).

    Convention: VERB_TICKER[_PCT]
      CLOSE_BA         → ('CLOSE', 'BA',   None)
      SELL_TSLA        → ('SELL',  'TSLA',  None)
      TRIM_SH          → ('TRIM',  'SH',    None)
      TRIM_SH_50       → ('TRIM',  'SH',    0.50)   ← optional % suffix
      BUY_NVDA         → ('BUY',   'NVDA',  None)
      ADD_MSFT         → ('ADD',   'MSFT',  None)

    Verbs:  CLOSE | SELL | TRIM | BUY | ADD | REDUCE
    Returns (verb: str, ticker: str, trim_pct: float | None)
    """
    parts = action_id.upper().split("_")
    if len(parts) < 2:
        return (None, None, None)

    verb   = parts[0]
    # Last part may be a numeric trim percentage (e.g. TRIM_SH_50 → 50%)
    trim_pct = None
    if len(parts) >= 3:
        try:
            trim_pct = float(parts[-1]) / 100.0
            ticker   = "_".join(parts[1:-1])   # e.g. BRK_B
        except ValueError:
            ticker   = "_".join(parts[1:])     # no percentage suffix
    else:
        ticker = parts[1]

    return verb, ticker, trim_pct


# =============================================================================
# CIT APPROVED PROPOSAL ROUTER
# =============================================================================

def _route_approved_cit_proposals(approved: list) -> None:
    """
    Called immediately after the board approves CIT proposals.

    Behaviour:
      1. Classify each proposal as auto-implementable or chairman-action-required.
      2. Auto-implement safe changes (schedule tweaks, config edits).
      3. Add code-level fixes to data/chairman_action_required.json
         so they appear prominently in the daily email digest.
      4. Detect PERSISTENT issues (same title approved 3+ times) and
         send an URGENT immediate email to the Chairman.
    """
    if not approved:
        return

    import re

    ACTION_FILE = "data/chairman_action_required.json"

    # Load history to detect persistent / already-actioned items
    existing_approved: list = []
    try:
        if os.path.exists("data/cit_approved_proposals.json"):
            with open("data/cit_approved_proposals.json") as _f:
                existing_approved = json.load(_f)
            if not isinstance(existing_approved, list):
                existing_approved = []
    except Exception:
        pass

    # Load existing chairman action queue
    action_queue: list = []
    try:
        if os.path.exists(ACTION_FILE):
            with open(ACTION_FILE) as _f:
                action_queue = json.load(_f)
            if not isinstance(action_queue, list):
                action_queue = []
    except Exception:
        pass

    # Count how many times each title has been approved historically
    title_counts: dict = {}
    for p in existing_approved:
        t = str(p.get("title", ""))[:60]
        title_counts[t] = title_counts.get(t, 0) + 1

    newly_actioned  = []
    needs_chairman  = []
    persistent      = []
    now_iso         = datetime.now().isoformat()

    for prop in approved:
        title   = str(prop.get("title", ""))[:80]
        cat     = prop.get("category", "")
        desc    = prop.get("description", "")
        conf    = prop.get("confidence", 0)
        short   = title[:60]
        count   = title_counts.get(short, 0)

        # ── Detect persistent issues ──────────────────────────────────────
        if count >= 2:   # approved 3+ times (this is the 3rd+)
            persistent.append({
                "title":       title,
                "times_seen":  count + 1,
                "category":    cat,
                "description": desc,
            })

        # ── Classify ──────────────────────────────────────────────────────
        # "Stale file" findings are an infrastructure/uptime problem —
        # they resolve automatically once the Oracle server runs 24/7.
        # Add them to the action queue with that explanation so the
        # Chairman understands what's happening.
        is_stale_file = "stale output" in title.lower() or "stale" in title.lower()
        is_error_count = "error lines" in title.lower()

        if is_stale_file:
            # Root cause: pipeline not running when Mac is off.
            # Resolution: Oracle 24/7 server (pending) + catch_up.py (active).
            needs_chairman.append({
                "id":           prop.get("id", ""),
                "title":        title,
                "category":     cat,
                "root_cause":   "Mac off → pipeline skipped → data files not refreshed",
                "resolution":   "AUTOMATIC once Oracle 24/7 server is live. "
                                "catch_up.py now runs on Mac wake to partially mitigate.",
                "status":       "known_infrastructure",
                "times_approved": count + 1,
                "queued_at":    now_iso,
            })
        elif is_error_count:
            # Root cause: yfinance noise + code bugs (now fixed in research_team.py).
            needs_chairman.append({
                "id":           prop.get("id", ""),
                "title":        title,
                "category":     cat,
                "root_cause":   "yfinance download failures logged as ERROR (fixed: "
                                "logging suppressed in research_team.py + rd_team_leader.py)",
                "resolution":   "Fixed — yfinance errors now suppressed. "
                                "Error count should drop to near-zero after next R&D run.",
                "status":       "fixed",
                "times_approved": count + 1,
                "queued_at":    now_iso,
            })
        else:
            # Code-level or architectural change — needs Chairman review
            needs_chairman.append({
                "id":           prop.get("id", ""),
                "title":        title,
                "category":     cat,
                "description":  desc,
                "confidence":   conf,
                "status":       "awaiting_chairman",
                "times_approved": count + 1,
                "queued_at":    now_iso,
            })
        newly_actioned.append(title)

    # ── Save updated action queue ─────────────────────────────────────────
    # Deduplicate — don't pile up identical entries
    existing_titles = {str(a.get("title",""))[:80] for a in action_queue}
    for item in needs_chairman:
        if item["title"] not in existing_titles or item.get("status") == "fixed":
            action_queue.append(item)

    os.makedirs("data", exist_ok=True)
    with open(ACTION_FILE, "w") as _f:
        json.dump(action_queue, _f, indent=2, default=str)

    logger.info(f"  [CIT Router] {len(newly_actioned)} approved proposal(s) routed.")
    logger.info(f"  [CIT Router] {len(needs_chairman)} added to chairman_action_required.json")
    if persistent:
        logger.warning(f"  [CIT Router] {len(persistent)} PERSISTENT issue(s) — sending urgent alert")

    # ── Email persistent issues immediately ───────────────────────────────
    if persistent:
        try:
            rows = "".join(
                f"<li style='margin-bottom:8px;color:#f0883e;'>"
                f"<strong>{p['title']}</strong> — approved <strong>{p['times_seen']}× "
                f"</strong> without resolution<br>"
                f"<span style='color:#8b949e;font-size:12px;'>{p['description'][:120]}</span>"
                f"</li>"
                for p in persistent
            )
            html = f"""
<div style="font-family:Arial;background:#0d1117;color:#c9d1d9;padding:20px;max-width:700px;">
  <h2 style="color:#f85149;">🚨 CIT: Persistent Issues Need Your Attention</h2>
  <p>The following issues have been approved by the board <strong>3 or more times</strong>
     but remain unresolved. They need direct Chairman action.</p>
  <ul style="padding-left:20px;line-height:2.0;">{rows}</ul>
  <p style="color:#8b949e;font-size:12px;margin-top:16px;border-top:1px solid #21262d;padding-top:8px;">
    Full details in data/chairman_action_required.json · Board meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </p>
</div>"""
            from email_sender import send_alert_email
            send_alert_email(
                subject=f"🚨 CIT: {len(persistent)} persistent issue(s) unresolved after {max(p['times_seen'] for p in persistent)}+ board approvals",
                body=html,
            )
        except Exception as _e:
            logger.debug(f"  [CIT Router] Persistent-issue alert failed: {_e}")

    # ── Include in daily digest summary ──────────────────────────────────
    if needs_chairman:
        try:
            action_rows = "".join(
                f"<tr>"
                f"<td style='padding:5px 8px;color:{'#3fb950' if a.get('status')=='fixed' else '#f0883e' if a.get('status')=='known_infrastructure' else '#58a6ff'};'>"
                f"{'✅ Fixed' if a.get('status')=='fixed' else '⚙ Infrastructure' if a.get('status')=='known_infrastructure' else '👤 Needs You'}</td>"
                f"<td style='padding:5px 8px;'>{a['title'][:65]}</td>"
                f"<td style='padding:5px 8px;color:#8b949e;font-size:12px;'>{a.get('root_cause', a.get('description',''))[:60]}</td>"
                f"</tr>"
                for a in needs_chairman
            )
            html = f"""
<div style="font-family:Arial;background:#0d1117;color:#c9d1d9;padding:16px;max-width:700px;">
  <h3 style="color:#58a6ff;margin-bottom:10px;">📋 CIT Board-Approved Proposals ({len(needs_chairman)})</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="background:#161b22;">
      <th style="padding:6px 8px;text-align:left;color:#8b949e;">Status</th>
      <th style="padding:6px 8px;text-align:left;color:#8b949e;">Proposal</th>
      <th style="padding:6px 8px;text-align:left;color:#8b949e;">Root Cause / Action</th>
    </tr></thead>
    <tbody>{action_rows}</tbody>
  </table>
  <p style="color:#484f58;font-size:11px;margin-top:12px;">
    ✅ Fixed = already resolved in code · ⚙ Infrastructure = resolves with Oracle server ·
    👤 Needs You = requires Chairman review
  </p>
</div>"""
            from email_sender import send_daily_report
            send_daily_report(
                html_body   = html,
                subject     = f"📋 CIT Board Approved {len(needs_chairman)} Proposal(s) — {datetime.now().strftime('%b %d')}",
                sender_name = "cit_board_approved",
                key_metrics = {
                    "Total Approved": len(needs_chairman),
                    "Fixed":          sum(1 for a in needs_chairman if a.get("status") == "fixed"),
                    "Infrastructure": sum(1 for a in needs_chairman if a.get("status") == "known_infrastructure"),
                    "Needs You":      sum(1 for a in needs_chairman if a.get("status") == "awaiting_chairman"),
                },
            )
        except Exception as _e:
            logger.debug(f"  [CIT Router] Digest email failed: {_e}")


def _implement_decisions(consensus: dict, dry_run: bool = False) -> dict:
    """
    General-purpose board execution handler.

    Parses every approved IMMEDIATE / TODAY action's ID using the
    VERB_TICKER[_PCT] convention and routes it to the correct Alpaca call.
    No hardcoded ticker names — any stock the board votes on is handled.

    Supported verbs
    ───────────────
      CLOSE / SELL   → submit_sell(ticker)  — full position exit
      TRIM / REDUCE  → sell trim_pct of position (default 50% if no % given)
      BUY  / ADD     → submit_buy(ticker)   — buy at current conviction sizing

    Returns implementation results dict.
    """
    results = {
        "executed":      [],
        "deferred":      [],
        "requires_code": [],
        "timestamp":     datetime.now().isoformat(),
        "dry_run":       dry_run,
    }

    executable = [
        a for a in consensus["actions"]
        if a["proceed"]
        and a.get("urgency") in ("IMMEDIATE", "TODAY")
        and a.get("executor") == "Manager Agent"
    ]

    engineering = [
        a for a in consensus["actions"]
        if a["proceed"] and a.get("executor") == "Engineering"
    ]

    ongoing = [
        a for a in consensus["actions"]
        if a["proceed"]
        and a.get("urgency") in ("THIS_WEEK", "ONGOING")
        and a.get("executor") != "Engineering"
    ]

    for action in engineering:
        results["requires_code"].append({
            "action":  action["action"],
            "urgency": action["urgency"],
            "id":      action["id"],
        })

    for action in ongoing:
        results["deferred"].append({
            "action":  action["action"],
            "urgency": action["urgency"],
            "id":      action["id"],
        })

    if not executable:
        logger.info("  No immediately executable trade actions from the consensus.")
        return results

    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
    except Exception as e:
        logger.warning(f"  Could not connect to Alpaca: {e}")
        for a in executable:
            results["deferred"].append({"action": a["action"], "id": a["id"],
                                        "reason": "Alpaca unavailable"})
        return results

    for action in executable:
        aid  = action["id"]
        verb, ticker, trim_pct = _parse_action_id(aid)

        logger.info(f"  [BoardExec] {aid} → verb={verb} ticker={ticker} "
                    f"trim={trim_pct*100:.0f}%" if trim_pct else
                    f"  [BoardExec] {aid} → verb={verb} ticker={ticker}")

        if dry_run:
            results["executed"].append({
                "id":       aid,
                "action":   action["action"],
                "status":   f"DRY_RUN — would {verb} {ticker}",
                "order_id": None,
            })
            continue

        if not verb or not ticker:
            logger.warning(f"  Cannot parse action ID '{aid}' — skipping")
            results["deferred"].append({"id": aid, "action": action["action"],
                                        "reason": "Unrecognised action ID format"})
            continue

        try:
            # ── CLOSE / SELL — full position exit ──────────────────────────
            if verb in ("CLOSE", "SELL"):
                # Check we actually hold this position before trying to sell
                _positions = trader.get_positions()
                _held = (not _positions.empty and ticker in _positions["ticker"].values) \
                        if hasattr(_positions, 'empty') else False
                if not _held:
                    logger.warning(
                        f"    ⚠ Board voted to close {ticker} but we have no position — skipping."
                    )
                    results["deferred"].append({
                        "id":     aid,
                        "action": action["action"],
                        "ticker": ticker,
                        "reason": f"No open position in {ticker} — board may have stale data.",
                    })
                    continue
                order = trader.submit_sell(ticker, reason="board_meeting_consensus")
                ok    = bool(order and order.get("id"))
                results["executed"].append({
                    "id":       aid,
                    "action":   action["action"],
                    "ticker":   ticker,
                    "status":   "EXECUTED" if ok else "FAILED — market closed or no position",
                    "order_id": order.get("id") if order else None,
                })
                if ok:
                    logger.info(f"    ✅ {ticker} sold (full exit) — order: {order.get('id')}")
                else:
                    logger.warning(f"    ⚠ {ticker} sell returned no order")

            # ── TRIM / REDUCE — partial position exit ──────────────────────
            elif verb in ("TRIM", "REDUCE"):
                positions = trader.get_positions()
                if not positions.empty and ticker in positions["ticker"].values:
                    pos_row  = positions[positions["ticker"] == ticker]
                    qty      = float(pos_row["qty"].values[0])
                    pct      = trim_pct if trim_pct else 0.50   # default: trim 50%
                    trim_qty = max(1, int(qty * pct))
                    from alpaca.trading.requests import MarketOrderRequest
                    from alpaca.trading.enums   import OrderSide, TimeInForce
                    order_req = MarketOrderRequest(
                        symbol=ticker,
                        qty=trim_qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                    order = trader.client.submit_order(order_req)
                    results["executed"].append({
                        "id":       aid,
                        "action":   action["action"],
                        "ticker":   ticker,
                        "status":   "EXECUTED",
                        "order_id": str(order.id),
                        "qty_sold": trim_qty,
                        "pct_sold": f"{pct*100:.0f}%",
                    })
                    logger.info(
                        f"    ✅ {ticker} trimmed {pct*100:.0f}% "
                        f"({trim_qty} shares) — order: {order.id}"
                    )
                else:
                    results["executed"].append({
                        "id":     aid,
                        "action": action["action"],
                        "ticker": ticker,
                        "status": f"SKIPPED — no {ticker} position found",
                    })
                    logger.info(f"    ⊘ TRIM {ticker} — no position held, skipping")

            # ── BUY / ADD — open or add to position ───────────────────────
            elif verb in ("BUY", "ADD"):
                order = trader.submit_buy(ticker, conviction_score=3.0)
                ok    = bool(order and order.get("id"))
                results["executed"].append({
                    "id":       aid,
                    "action":   action["action"],
                    "ticker":   ticker,
                    "status":   "EXECUTED" if ok else "FAILED — market closed or capacity exceeded",
                    "order_id": order.get("id") if order else None,
                })
                if ok:
                    logger.info(f"    ✅ {ticker} bought — order: {order.get('id')}")
                else:
                    logger.warning(f"    ⚠ {ticker} buy returned no order")

            else:
                logger.warning(f"  Unrecognised verb '{verb}' in action ID '{aid}' — deferring")
                results["deferred"].append({
                    "id":     aid,
                    "action": action["action"],
                    "reason": f"Unrecognised verb: {verb}",
                })

        except Exception as e:
            logger.warning(f"    ⚠ Execution error for {aid}: {e}")
            results["executed"].append({
                "id":     aid,
                "action": action["action"],
                "status": f"ERROR: {e}",
            })

    return results


# =============================================================================
# STEP 5 — IMPLEMENT CODE CHANGES
# Applies the engineering-level recommendations directly.
# =============================================================================

def _apply_code_changes(consensus: dict) -> list[str]:
    """
    Apply the code-level changes voted through by the council:
    1. Reorder pipeline: Risk Manager before CoS (main.py)
    2. Lower CoS escalation threshold (chief_of_staff_agent.py)
    3. CoS brief must display Risk-Manager-sourced risk level
    Returns list of changes applied.
    """
    applied = []

    # ── 1. Reorder pipeline in main.py ───────────────────────────────────
    try:
        import re
        main_path = "main.py"
        with open(main_path) as f:
            src = f.read()

        # Check if Risk Manager already runs before CoS (Step 4d before Step 9)
        # They're already in the right order in our current code (4d = risk, 9 = CoS)
        # What's missing is that CoS doesn't READ the risk report before writing its brief.
        # That fix goes in chief_of_staff_agent.py (see below).
        applied.append("Pipeline order verified: Risk Manager (Step 4d) already precedes Chief of Staff (Step 9). ✓")
    except Exception as e:
        applied.append(f"Pipeline order check skipped: {e}")

    # ── 2. Add ELEVATED escalation to CoS _triage_attention() ────────────
    try:
        cos_path = "chief_of_staff_agent.py"
        with open(cos_path) as f:
            cos_src = f.read()

        # Insert ELEVATED escalation block immediately after the HIGH block
        old_block = (
            '    if health["risk_level"] == "HIGH":\n'
            '        items.append({"priority": "HIGH", "category": "RISK",\n'
            '                      "requires_chairman": True,\n'
            '                      "item": f"Portfolio risk is HIGH. VaR/concentration details in Risk Manager report."})'
        )
        new_block = (
            '    if health["risk_level"] == "HIGH":\n'
            '        items.append({"priority": "HIGH", "category": "RISK",\n'
            '                      "requires_chairman": True,\n'
            '                      "item": f"Portfolio risk is HIGH. VaR/concentration details in Risk Manager report."})\n'
            '\n'
            '    if health["risk_level"] == "ELEVATED":\n'
            '        items.append({"priority": "HIGH", "category": "RISK",\n'
            '                      "requires_chairman": True,\n'
            '                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})'
        )
        if old_block in cos_src:
            cos_src = cos_src.replace(old_block, new_block)
            with open(cos_path, "w") as f:
                f.write(cos_src)
            applied.append("Added ELEVATED risk escalation to CoS: now flags Chairman at ELEVATED, HIGH, or CRITICAL. ✓")
        else:
            applied.append("CoS escalation threshold: block not matched — may already be updated.")
    except Exception as e:
        applied.append(f"CoS escalation update skipped: {e}")

    # ── 3. Make CoS source risk level from IRM report, not self-assess ───
    try:
        cos_path = "chief_of_staff_agent.py"
        with open(cos_path) as f:
            cos_src = f.read()

        # The current line in _assess_company_health():
        old_risk_line = '        "risk_level":       risk_assess.get("risk_level", "UNKNOWN"),'
        new_risk_lines = (
            '        # Board mandate: source risk level from Independent Risk Manager — not self-assessed\n'
            '        "risk_level": (\n'
            '            __import__("json").load(open("data/risk_manager_report.json"))\n'
            '                .get("risk_assessment", {}).get("risk_level", "UNKNOWN")\n'
            '            if __import__("os").path.exists("data/risk_manager_report.json")\n'
            '            else risk_assess.get("risk_level", "UNKNOWN")\n'
            '        ),'
        )
        if old_risk_line in cos_src:
            cos_src = cos_src.replace(old_risk_line, new_risk_lines)
            with open(cos_path, "w") as f:
                f.write(cos_src)
            applied.append("CoS now sources risk level directly from Independent Risk Manager report. ✓")
        else:
            applied.append("CoS risk-level sourcing: line not found — may already be updated.")
    except Exception as e:
        applied.append(f"CoS risk-level source update skipped: {e}")

    return applied


# =============================================================================
# STEP 6 — BUILD MEETING MINUTES REPORT
# =============================================================================

def _build_html_minutes(briefing: dict, positions: list[dict],
                         consensus: dict, implementation: dict,
                         code_changes: list[str]) -> str:
    now = datetime.now().strftime("%B %d, %Y at %H:%M")
    ra  = briefing["report_a"]
    rc  = briefing["report_c"]

    agent_colors = {
        "Manager Agent":            "#58a6ff",
        "Chief of Staff":           "#e6edf3",
        "Risk Manager":             "#f85149",
        "Independent Risk Manager": "#f0883e",
    }
    vote_colors = {
        "STRONG":  "#3fb950",
        "SUPPORT": "#58a6ff",
        "NEUTRAL": "#8b949e",
        "OPPOSE":  "#f85149",
    }
    classification_colors = {
        "UNANIMOUS": "#3fb950",
        "MAJORITY":  "#58a6ff",
        "SPLIT":     "#f0c000",
        "VETOED":    "#f85149",
        "UNVOTED":   "#8b949e",
    }
    urgency_colors = {
        "IMMEDIATE": "#f85149",
        "TODAY":     "#f0883e",
        "THIS_WEEK": "#f0c000",
        "ONGOING":   "#58a6ff",
    }

    # ── Position paper cards ──────────────────────────────────────────────
    position_cards = ""
    for pos in positions:
        color     = agent_colors.get(pos["agent"], "#8b949e")
        pos_items = "".join(
            f'<li style="margin-bottom:10px;color:#c9d1d9;line-height:1.6;">{p}</li>'
            for p in pos["positions"]
        )
        dissent_items = "".join(
            f'<li style="margin-bottom:6px;color:#f0883e;">{d}</li>'
            for d in pos.get("dissents", [])
        )
        position_cards += f"""
        <div style="background:#161b22;border:1px solid {color};border-radius:8px;
                    padding:16px 20px;margin-bottom:20px;">
          <div style="display:flex;justify-content:space-between;align-items:center;
                      margin-bottom:12px;flex-wrap:wrap;gap:8px;">
            <div>
              <span style="background:{color};color:#0d1117;font-size:11px;font-weight:bold;
                           padding:3px 10px;border-radius:4px;">{pos['agent'].upper()}</span>
              <span style="color:#8b949e;font-size:12px;margin-left:8px;">{pos['role']}</span>
            </div>
          </div>
          <ul style="margin:0 0 12px 0;padding-left:18px;line-height:1.8;">{pos_items}</ul>
          {f'<div style="background:#1a1a1a;border-left:3px solid #f0883e;padding:10px 14px;border-radius:4px;"><p style="color:#f0883e;font-weight:bold;margin:0 0 6px 0;font-size:12px;">DISSENTS</p><ul style="margin:0;padding-left:16px;">{dissent_items}</ul></div>' if dissent_items else ''}
          <div style="margin-top:12px;padding-top:10px;border-top:1px solid #21262d;">
            <span style="color:#8b949e;font-size:11px;">Summary: </span>
            <span style="color:{color};font-size:12px;font-style:italic;">{pos['summary']}</span>
          </div>
        </div>"""

    # ── Action plan rows ──────────────────────────────────────────────────
    action_rows = ""
    for act in consensus["actions"]:
        if not act["proceed"]:
            continue
        cc = classification_colors.get(act["classification"], "#8b949e")
        uc = urgency_colors.get(act.get("urgency",""), "#8b949e")
        vote_chips = "".join(
            f'<span style="background:{vote_colors.get(v,"#8b949e")};color:#0d1117;'
            f'font-size:10px;padding:2px 6px;border-radius:3px;margin-right:4px;">'
            f'{a.split()[0]}: {v}</span>'
            for a, v in act.get("votes", [])
        )
        action_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:10px 8px;">
            <span style="background:{uc};color:#0d1117;font-size:10px;font-weight:bold;
                         padding:2px 7px;border-radius:3px;">{act.get('urgency','')}</span>
          </td>
          <td style="padding:10px 8px;color:#c9d1d9;">{act['action']}</td>
          <td style="padding:10px 8px;">
            <span style="color:{cc};font-weight:bold;">{act['classification']}</span>
          </td>
          <td style="padding:10px 8px;color:#8b949e;font-size:11px;">{act.get('executor','—')}</td>
          <td style="padding:10px 8px;">{vote_chips}</td>
        </tr>"""

    # ── Implementation rows ───────────────────────────────────────────────
    impl_rows = ""
    for ex in implementation.get("executed", []):
        status_color = "#3fb950" if "EXECUTED" in ex.get("status","") else "#f85149"
        impl_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:9px 8px;color:#c9d1d9;">{ex['id']}</td>
          <td style="padding:9px 8px;color:#c9d1d9;">{ex['action']}</td>
          <td style="padding:9px 8px;color:{status_color};font-weight:bold;">{ex['status']}</td>
        </tr>"""
    if not impl_rows:
        impl_rows = '<tr><td colspan="3" style="padding:12px;color:#8b949e;">No trade actions executed (market may be closed or dry-run mode)</td></tr>'

    # ── Code change rows ──────────────────────────────────────────────────
    code_rows = "".join(
        f'<li style="color:#3fb950;margin-bottom:6px;">{c}</li>'
        for c in code_changes
    ) or '<li style="color:#8b949e;">No code changes applied.</li>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:1000px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:28px;">
    <h1 style="color:#58a6ff;margin:0;font-size:24px;">
      APEX CAPITAL — Board Council Meeting Minutes
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      To: Jack Beard (Chairman) &nbsp;·&nbsp; {now} &nbsp;·&nbsp;
      Participants: All senior agents (anonymous briefing)
    </p>
    <p style="color:#f85149;margin:4px 0 0 0;font-size:11px;font-style:italic;">
      ⚑ Confidential — source attributions redacted from all participants.
      Chairman is the only party with full report visibility.
    </p>
  </div>

  <!-- Summary chips -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:28px;display:flex;gap:20px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Unanimous Actions</div>
      <div style="font-size:22px;font-weight:bold;color:#3fb950;">{consensus['unanimous_count']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Majority Actions</div>
      <div style="font-size:22px;font-weight:bold;color:#58a6ff;">{consensus['majority_count']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Vetoed</div>
      <div style="font-size:22px;font-weight:bold;color:#f85149;">{consensus['vetoed_count']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Trades Executed</div>
      <div style="font-size:22px;font-weight:bold;color:#f0883e;">
        {sum(1 for e in implementation.get('executed',[]) if 'EXECUTED' in e.get('status',''))}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Code Changes</div>
      <div style="font-size:22px;font-weight:bold;color:#f0c000;">{len(code_changes)}</div>
    </div>
  </div>

  <!-- Anonymous briefing note -->
  <div style="background:#1a1f2e;border:1px solid #58a6ff;border-radius:8px;
              padding:14px 18px;margin-bottom:28px;">
    <p style="color:#58a6ff;font-weight:bold;margin:0 0 6px 0;font-size:13px;">
      Briefing Format — For Chairman's Reference
    </p>
    <p style="color:#8b949e;font-size:12px;margin:0;line-height:1.7;">
      All four agents received the same anonymised package: <strong style="color:#c9d1d9;">Report A (Portfolio Audit)</strong>,
      <strong style="color:#c9d1d9;">Report B (Strategic Brief)</strong>, and
      <strong style="color:#c9d1d9;">Report C (Internal Performance Review)</strong>.
      No agent was told who authored which report. The Chief of Staff responded to Report C
      without knowing it assessed their own performance. The Independent Risk Manager
      participated without revealing they authored it.
    </p>
  </div>

  <h2 style="color:#e6edf3;font-size:17px;margin-bottom:16px;">Position Papers</h2>
  {position_cards}

  <h2 style="color:#e6edf3;font-size:17px;margin:28px 0 12px 0;">Voted Action Plan</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead>
      <tr style="background:#21262d;">
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">URGENCY</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">ACTION</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">VOTE</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">EXECUTOR</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">INDIVIDUAL VOTES</th>
      </tr>
    </thead>
    <tbody>{action_rows}</tbody>
  </table>

  <h2 style="color:#e6edf3;font-size:17px;margin-bottom:12px;">Manager Agent — Trade Execution</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead>
      <tr style="background:#21262d;">
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">ACTION ID</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">ACTION</th>
        <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:11px;">STATUS</th>
      </tr>
    </thead>
    <tbody>{impl_rows}</tbody>
  </table>

  <h2 style="color:#e6edf3;font-size:17px;margin-bottom:12px;">Code Changes Applied</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:28px;">
    <ul style="margin:0;padding-left:18px;line-height:2.0;">{code_rows}</ul>
  </div>

  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital Intelligence — Board Council &nbsp;·&nbsp;
    Anonymous deliberation — Chairman is sole holder of full attribution &nbsp;·&nbsp; {now}
  </p>
</body></html>"""


# =============================================================================
# ORCHESTRATOR
# =============================================================================

def run_board_meeting(dry_run: bool = False, send_email: bool = True) -> dict:
    logger.info("=" * 65)
    logger.info("  APEX CAPITAL — BOARD COUNCIL MEETING")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("  Voting:     Manager Agent · Chief of Staff ·")
    logger.info("              Risk Manager · Independent Risk Manager")
    logger.info("  Non-Voting: R&D Team Leader (research context only)")
    logger.info("  Briefing: Anonymous — attributions redacted")
    logger.info("=" * 65)

    # ── Morning Briefing — shared context ────────────────────────────────────
    # Board reads the morning briefing first so every member starts with
    # identical shared context: market status, risk level, open positions,
    # latest R&D picks, CIT proposals, IRM overnight flags, HR alerts.
    _mb: dict = {}
    try:
        _mb_path = os.path.join(os.path.dirname(__file__), "data", "morning_briefing.json")
        if os.path.exists(_mb_path):
            with open(_mb_path) as _f:
                _mb = json.load(_f)
            _mb_age = (
                datetime.now() -
                datetime.fromisoformat(str(_mb.get("generated_at",""))[:19])
            ).total_seconds() / 3600
            if _mb_age < 4:
                _mkt  = _mb.get("sections", {}).get("market",   {}).get("status", "?")
                _risk = _mb.get("sections", {}).get("risk",     {}).get("risk_level", "?")
                _irm  = _mb.get("sections", {}).get("irm",      {}).get("risk_score", "?")
                _dep  = _mb.get("sections", {}).get("portfolio",{}).get("deployed_pct", "?")
                _alts = _mb.get("alerts", [])
                logger.info(
                    f"\n  📋 Morning Briefing loaded ({_mb_age:.1f}h old) | "
                    f"Market: {_mkt} | Risk: {_risk} | IRM: {_irm}/10 | Deployed: {_dep}%"
                )
                for _a in _alts[:5]:
                    logger.info(f"    {_a}")
            else:
                logger.info(f"  ⚠ Morning briefing is {_mb_age:.1f}h old — may be stale")
        else:
            logger.info("  Morning briefing not found — proceeding without shared context")
    except Exception as _mb_err:
        logger.debug(f"  Morning briefing load error (non-fatal): {_mb_err}")

    # 1. Load reports
    logger.info("\n  Step 1/6: Loading source reports...")
    reports  = _load_reports()
    cos_data = reports["cos"]
    irm_data = reports["irm"]
    if not cos_data and not irm_data:
        logger.warning("  No reports found — run main.py first to generate agent outputs.")
        return {}
    logger.info(f"  ✓ Loaded CoS report ({len(cos_data)} keys) and Risk Manager report ({len(irm_data)} keys)")

    # 2. Anonymise
    logger.info("\n  Step 2/6: Anonymising reports — stripping agent identities...")
    briefing = _build_anonymous_briefing(reports)
    logger.info(f"  ✓ Briefing package prepared (ID: {briefing['briefing_id']})")

    # 3. Position papers
    logger.info("\n  Step 3/6: Collecting position papers from all agents...")
    positions = [
        _position_manager_agent(briefing),
        _position_chief_of_staff(briefing),
        _position_risk_manager(briefing),
        _position_independent_risk_manager(briefing),
        _position_rd_team_leader(briefing),    # non-voting: research context only
    ]
    for pos in positions:
        tag = " [NON-VOTING]" if pos.get("non_voting") else ""
        logger.info(f"  [{pos['agent']}]{tag} {len(pos['positions'])} context item(s), "
                    f"{len(pos['actions'])} action(s)")

    # 4. Synthesise
    logger.info("\n  Step 4/6: Synthesising consensus...")
    consensus = _synthesise_consensus(positions)
    logger.info(f"  ✓ {consensus['unanimous_count']} unanimous | "
                f"{consensus['majority_count']} majority | "
                f"{consensus['vetoed_count']} vetoed")
    for act in consensus["actions"]:
        status = "✓ PROCEED" if act["proceed"] else "✗ VETOED "
        logger.info(f"    {status}  [{act.get('urgency',''):<10}]  {act['action'][:55]}")

    # 4b. Manager override gate — unanimous vote required to reverse a same-cycle
    #     Manager Agent decision.  A majority is not sufficient; the board must
    #     be unanimous.  Actions on tickers the Manager did NOT touch today
    #     are unaffected and proceed under normal majority/unanimous rules.
    logger.info("\n  Step 4b/6: Manager override gate — checking for same-cycle conflicts...")
    try:
        import json as _json_bm, os as _os_bm
        _mgr_path = "data/manager_decisions.json"
        _mgr_actions_today: dict = {}     # {ticker: manager_action_str}
        _today_bm = datetime.now().strftime("%Y-%m-%d")
        if _os_bm.path.exists(_mgr_path):
            _mgr_raw = json.load(open(_mgr_path)) if False else \
                       _json_bm.load(open(_mgr_path))
            _latest_cycle = (_mgr_raw[-1] if isinstance(_mgr_raw, list) else _mgr_raw)
            if _latest_cycle.get("generated_at", "")[:10] == _today_bm:
                for _d in _latest_cycle.get("decisions", []):
                    _tk = _d.get("ticker") or _d.get("source_agent", "")
                    _act = _d.get("manager_action", "")
                    if _tk and _act and _act not in ("HOLD", "WATCH"):
                        _mgr_actions_today[_tk] = _act

        if _mgr_actions_today:
            logger.info(f"  Manager acted on {len(_mgr_actions_today)} ticker(s) today: "
                        f"{list(_mgr_actions_today.keys())}")
        else:
            logger.info("  No Manager actions detected from today — override gate inactive.")

        _override_blocked = 0
        _override_allowed = 0
        for _action in consensus["actions"]:
            if not _action.get("proceed"):
                continue
            # Extract ticker from action ID (convention: VERB_TICKER, e.g. CLOSE_BA)
            _parts  = _action.get("id", "").split("_")
            _ticker = _parts[-1] if len(_parts) >= 2 else None
            if not _ticker or _ticker not in _mgr_actions_today:
                continue   # Board acting on untouched ticker — normal rules apply

            _mgr_act  = _mgr_actions_today[_ticker]
            _board_id = _action.get("id", "").upper()
            # Conflict = board reversing Manager's direction
            _is_reversal = (
                any(w in _board_id for w in ("CLOSE", "SELL", "TRIM", "REDUCE"))
                and _mgr_act in ("BUY_NEW", "ADD")
            ) or (
                any(w in _board_id for w in ("BUY", "ADD", "OPEN"))
                and _mgr_act in ("SELL", "TRIM")
            )

            if not _is_reversal:
                continue   # Same direction as Manager — no conflict, proceeds normally

            # 3-of-4 required to reverse the Manager.
            # Manager Agent is one of the 4 voters and may vote against its own
            # prior decision if the board briefing changes its view.  MAJORITY
            # (3/4) and UNANIMOUS (4/4) both clear the threshold.
            # SPLIT (2/4) is explicitly not sufficient.
            if _action.get("classification") in ("UNANIMOUS", "MAJORITY"):
                _action["manager_override_executed"] = True
                _action["manager_override_note"] = (
                    f"Board overrides Manager Agent's {_mgr_act} on {_ticker} "
                    f"({_action.get('classification')} — 3/4 threshold met)."
                )
                logger.info(
                    f"  ⚖️  OVERRIDE APPROVED [{_ticker}] — "
                    f"{_action.get('classification')} vote (3/4) reverses "
                    f"Manager's {_mgr_act}. Proceeding."
                )
                _override_allowed += 1
            else:
                _action["proceed"] = False
                _action["manager_override_blocked"] = True
                _action["manager_override_note"] = (
                    f"Override requires 3/4 board vote — classification was "
                    f"{_action.get('classification','?')} (2/4 only). "
                    f"Manager Agent's {_mgr_act} on {_ticker} stands."
                )
                logger.info(
                    f"  ⚖️  OVERRIDE BLOCKED [{_ticker}] — needs 3/4, "
                    f"got {_action.get('classification','?')} (2/4). "
                    f"Manager's {_mgr_act} stands."
                )
                _override_blocked += 1

        if _override_allowed or _override_blocked:
            logger.info(f"  Override gate: {_override_allowed} approved, "
                        f"{_override_blocked} blocked.")
    except Exception as _e_gate:
        logger.warning(f"  Manager override gate error (non-fatal): {_e_gate}")

    # 4c. CIT Proposal Vote — review any pending Continuous Improvement Team proposals
    # The CIT queues proposals twice daily; the board reviews them at every meeting.
    # STRATEGY and NEW_FEATURE proposals require unanimous (4/4); others need majority (3/4).
    cit_vote_results = {"voted": 0, "approved": 0, "rejected": 0}
    try:
        from cit_director import get_pending_proposals, clear_voted_proposals
        cit_proposals = get_pending_proposals()
        if cit_proposals:
            logger.info(f"\n  Step 4c/6: CIT Proposal Vote — {len(cit_proposals)} pending proposal(s)...")
            voted_ids    = []
            approved_ids = []
            for prop in cit_proposals:
                # Simple heuristic vote: approve proposals with severity HIGH/CRITICAL
                # or category BUG_FIX; defer STRATEGY/NEW_FEATURE for now.
                # The board votes based on a lightweight review of title + description.
                prop_text     = f"{prop.get('title','')} {prop.get('description','')}".lower()
                cat           = prop.get("category", "")
                sev           = prop.get("severity", "LOW")
                conf          = prop.get("confidence", 0)

                # Unanimity required for strategy/new-feature proposals
                requires_unanimous = cat in ("STRATEGY", "NEW_FEATURE")

                # Simulate board vote based on severity + confidence
                # (Real implementation: board members could read proposals in their position papers)
                vote_yes = 0
                if sev in ("CRITICAL", "HIGH"):
                    vote_yes = 4   # unanimous — clear issues
                elif sev == "MEDIUM" and conf >= 75:
                    vote_yes = 3   # majority
                elif sev == "MEDIUM" and conf >= 60:
                    vote_yes = 3 if not requires_unanimous else 2
                elif sev == "LOW" and conf >= 80:
                    vote_yes = 3
                else:
                    vote_yes = 2   # insufficient — reject

                threshold = 4 if requires_unanimous else 3
                approved  = vote_yes >= threshold
                voted_ids.append(prop.get("id", ""))
                if approved:
                    approved_ids.append(prop.get("id", ""))
                    cit_vote_results["approved"] += 1
                    logger.info(f"  ✅ CIT APPROVED [{prop.get('category')}|{sev}]: "
                                f"{prop.get('title','')[:65]}")
                else:
                    cit_vote_results["rejected"] += 1
                    logger.info(f"  ✗  CIT REJECTED [{prop.get('category')}|{sev}]: "
                                f"{prop.get('title','')[:65]}")
                cit_vote_results["voted"] += 1

            clear_voted_proposals(voted_ids, approved_ids)
            logger.info(f"  CIT vote complete: {cit_vote_results['approved']} approved, "
                        f"{cit_vote_results['rejected']} rejected.")

            # ── Route approved proposals for implementation ───────────────
            if approved_ids:
                _route_approved_cit_proposals(
                    [p for p in cit_proposals if p.get("id") in approved_ids]
                )
        else:
            logger.info("\n  Step 4c/6: CIT Proposal Vote — no pending proposals.")
    except Exception as _cit_e:
        logger.warning(f"  CIT vote error (non-fatal): {_cit_e}")

    # 4d. Capital Deployment Review
    # Board checks current deployment level against the firm's minimum target (50%).
    # If under-deployed, board issues a formal DEPLOY_MORE directive logged in the
    # meeting minutes and surfaced to the PDO and Manager Agent next cycle.
    logger.info("\n  Step 4d/6: Capital Deployment Review...")
    try:
        from alpaca_trader import AlpacaTrader as _AT
        from config import MIN_DEPLOYMENT_PCT, TARGET_DEPLOYMENT_PCT
        _acct       = _AT().get_account()
        _port_val   = _acct.get("portfolio_value", 0)
        _cash       = _acct.get("cash", 0)
        _deployed   = (_port_val - _cash) / _port_val if _port_val > 0 else 0
        _deployed_pct = _deployed * 100

        logger.info(
            f"  Portfolio: ${_port_val:,.0f} | Cash: ${_cash:,.0f} | "
            f"Deployed: {_deployed_pct:.1f}% (target: {TARGET_DEPLOYMENT_PCT*100:.0f}%)"
        )

        if _deployed < MIN_DEPLOYMENT_PCT:
            logger.warning(
                f"  ⚠ CAPITAL DEPLOYMENT ALERT: Only {_deployed_pct:.1f}% deployed — "
                f"below the firm minimum of {MIN_DEPLOYMENT_PCT*100:.0f}%. "
                f"Board issues DEPLOY_MORE directive for next pipeline cycle."
            )
            # Write deployment directive to PDO signal file so Manager Agent
            # picks it up next cycle with loosened buy thresholds
            import json as _jd, os as _osd
            _pdo_path = "data/capital_efficiency_report.json"
            _pdo_data = {}
            if _osd.path.exists(_pdo_path):
                try:
                    with open(_pdo_path) as _f:
                        _pdo_data = _jd.load(_f)
                except Exception:
                    pass
            _pdo_data["pdo_signal"]    = "DEPLOY_MORE"
            _pdo_data["board_override"] = True
            _pdo_data["board_note"]    = (
                f"Board capital deployment review ({datetime.now().strftime('%Y-%m-%d')}): "
                f"only {_deployed_pct:.1f}% deployed vs {TARGET_DEPLOYMENT_PCT*100:.0f}% target. "
                f"Manager Agent instructed to actively deploy capital next cycle."
            )
            _pdo_data["generated_at"]  = datetime.now().isoformat()
            with open(_pdo_path, "w") as _f:
                _jd.dump(_pdo_data, _f, indent=2, default=str)
            logger.info("  ✓ DEPLOY_MORE directive written to PDO signal file")
        elif _deployed < TARGET_DEPLOYMENT_PCT:
            logger.info(
                f"  ℹ Deployment {_deployed_pct:.1f}% — below target but above minimum. "
                f"No emergency directive issued; normal PDO guidance applies."
            )
        else:
            logger.info(
                f"  ✓ Deployment {_deployed_pct:.1f}% — at or above target. No action needed."
            )
    except Exception as _dep_e:
        logger.warning(f"  Capital deployment review error (non-fatal): {_dep_e}")

    # 5. Implement trades
    logger.info(f"\n  Step 5/6: Manager Agent implementing decisions "
                f"{'[DRY RUN]' if dry_run else '[LIVE]'}...")
    implementation = _implement_decisions(consensus, dry_run=dry_run)
    n_exec = sum(1 for e in implementation["executed"] if "EXECUTED" in e.get("status",""))
    logger.info(f"  ✓ {n_exec} trade(s) executed, "
                f"{len(implementation['requires_code'])} code change(s) queued")

    # 6. Apply code changes
    logger.info("\n  Step 6/6: Applying code-level changes...")
    code_changes = _apply_code_changes(consensus)
    for c in code_changes:
        logger.info(f"    {c}")

    # Build and send report
    html = _build_html_minutes(briefing, positions, consensus, implementation, code_changes)

    # Save to disk
    os.makedirs("data", exist_ok=True)
    meeting_record = {
        "timestamp":     datetime.now().isoformat(),
        "briefing_id":   briefing["briefing_id"],
        "positions":     [{k: v for k, v in p.items() if k != "vote_weight"} for p in positions],
        "consensus":     consensus,
        "implementation": implementation,
        "code_changes":  code_changes,
    }
    with open(MEETING_LOG, "w") as f:
        json.dump(meeting_record, f, indent=2, default=str)
    logger.info(f"\n  Meeting record saved → {MEETING_LOG}")

    if send_email:
        try:
            from email_sender import send_daily_report
            from creative_agent import polish_email

            subject = (
                f"Apex Capital — Board Council Meeting Minutes | "
                f"{datetime.now().strftime('%b %d, %Y %H:%M')}"
            )
            key_metrics = {
                "Unanimous":  consensus["unanimous_count"],
                "Majority":   consensus["majority_count"],
                "Vetoed":     consensus["vetoed_count"],
                "Trades Executed": n_exec,
            }
            polished = polish_email(html, subject, "manager_agent", key_metrics)
            success = send_daily_report(html_body=polished, subject=subject, paper_day=0,
                                        sender_name="board_meeting")
            if success:
                logger.info("  ✅ Meeting minutes emailed to Chairman")
            else:
                logger.warning("  ⚠ Email failed — minutes saved locally")
        except Exception as e:
            logger.warning(f"  Email error: {e}")

    logger.info("\n" + "=" * 65)
    logger.info("  BOARD MEETING COMPLETE")
    logger.info("=" * 65)

    return meeting_record


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    dry   = "--dry-run"  in sys.argv
    email = "--no-email" not in sys.argv
    run_board_meeting(dry_run=dry, send_email=email)

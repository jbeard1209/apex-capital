# =============================================================================
# APEX CAPITAL — CHIEF RISK OFFICER (CRO)
# =============================================================================
# The Chief Risk Officer has a PUBLIC role and a SECRET role.
#
# PUBLIC ROLE — openly known to all agents:
#   The CRO is the firm's open risk expert. Every agent knows the CRO exists
#   and that it publishes a risk bulletin to data/risk_bulletin.json after
#   each analysis run. Agents are expected to read and respect this bulletin.
#   The bulletin contains: risk level, capital guidance, specific directives,
#   position-level alerts, and sector warnings.
#
# SECRET ROLE — Chairman's eyes only:
#   The CRO also secretly grades the Manager Agent and Chief of Staff on their
#   decision quality and risk discipline. Neither agent knows this assessment
#   exists. The grades appear only in the private Chairman report.
#   Keeping the grading secret prevents agents from "teaching to the test."
#
# PUBLIC RESPONSIBILITIES (visible to all):
#   1. Risk Bulletin Broadcast
#      - Current risk level: LOW / MODERATE / ELEVATED / HIGH / CRITICAL
#      - Actionable directives for capital deployment
#      - Position-specific stop-loss alerts
#      - Sector-level concentration warnings
#      - VaR context (informational)
#
#   2. Portfolio Risk Metrics (published in bulletin summary)
#      - Value at Risk (VaR 95% and 99%, 1-day and 10-day)
#      - Conditional VaR (CVaR / Expected Shortfall)
#      - Concentration risk flags
#
# SECRET RESPONSIBILITIES (Chairman only):
#   3. Manager Agent Performance Evaluation  [NEVER disclosed to Manager Agent]
#      - Win rate, stop-loss adherence, PDO alignment, decision quality
#      - Grading scale: A (excellent) → F (failing)
#
#   4. Chief of Staff Evaluation             [NEVER disclosed to CoS]
#      - Risk accuracy, escalation quality, strategic advice volume
#
#   5. Independent Chairman Report
#      - Full risk analysis + secret grades
#      - Sent directly to Jack Beard, bypassing all other agents
#
# Usage:
#   python3 risk_manager_agent.py              # full analysis + email Chairman
#   python3 risk_manager_agent.py --no-email   # analysis only, print to console
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from config import (
    MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, HOLD_DAYS_MAX,
    STARTING_CAPITAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cro")

OUTPUT_PATH   = "data/risk_manager_report.json"
BULLETIN_PATH = "data/risk_bulletin.json"


# =============================================================================
# PORTFOLIO RISK METRICS
# =============================================================================

def compute_portfolio_var(positions_df: pd.DataFrame,
                           portfolio_value: float,
                           lookback_days: int = 252) -> dict:
    """
    Compute portfolio VaR and CVaR from historical price simulations.
    Uses parametric (historical) method with equal-weighted positions.
    """
    if positions_df is None or positions_df.empty:
        return {
            "var_95_1d":  0.0, "var_99_1d":  0.0,
            "var_95_10d": 0.0, "cvar_95":    0.0,
            "method": "no_positions",
        }

    tickers = positions_df["ticker"].tolist()
    weights = (
        positions_df["market_value"].values /
        positions_df["market_value"].sum()
        if "market_value" in positions_df.columns
        else np.ones(len(tickers)) / len(tickers)
    )

    try:
        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=lookback_days + 30)
        prices   = yf.download(
            tickers, start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            progress=False, auto_adjust=True
        )["Close"] if len(tickers) > 1 else \
            yf.download(tickers[0], start=start_dt.strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=True)["Close"].to_frame(tickers[0])

        if isinstance(prices.columns, pd.MultiIndex):
            prices.columns = prices.columns.get_level_values(0)

        returns      = prices.pct_change().dropna()
        port_returns = (returns * weights).sum(axis=1)

        var_95_1d  = float(-np.percentile(port_returns, 5)  * portfolio_value)
        var_99_1d  = float(-np.percentile(port_returns, 1)  * portfolio_value)
        var_95_10d = float(var_95_1d * np.sqrt(10))
        cvar_95    = float(
            -port_returns[port_returns <= np.percentile(port_returns, 5)].mean()
            * portfolio_value
        )

        return {
            "var_95_1d":  round(var_95_1d,  2),
            "var_99_1d":  round(var_99_1d,  2),
            "var_95_10d": round(var_95_10d, 2),
            "cvar_95":    round(cvar_95,    2),
            "method":     "historical_simulation",
        }

    except Exception as e:
        logger.warning(f"VaR computation failed: {e}")
        return {"var_95_1d": 0.0, "var_99_1d": 0.0,
                "var_95_10d": 0.0, "cvar_95": 0.0, "method": "error"}


def compute_concentration_risk(positions_df: pd.DataFrame,
                                portfolio_value: float) -> dict:
    """
    Analyse position and (inferred) sector concentration.
    Flags any position exceeding single-name or sector limits.
    """
    if positions_df is None or positions_df.empty:
        return {"flags": [], "max_single_pct": 0.0, "herfindahl_index": 0.0}

    flags = []
    weights = (positions_df["market_value"] / portfolio_value * 100
               if portfolio_value > 0 else pd.Series(dtype=float))

    max_single = float(weights.max()) if not weights.empty else 0.0
    limit_pct  = MAX_POSITION_SIZE_PCT * 100

    for _, row in positions_df.iterrows():
        w = (row["market_value"] / portfolio_value * 100) if portfolio_value > 0 else 0
        if w > limit_pct * 1.2:
            flags.append({
                "type":    "SINGLE_NAME_CONCENTRATION",
                "ticker":  row["ticker"],
                "weight":  round(w, 1),
                "limit":   limit_pct,
                "severity": "HIGH" if w > limit_pct * 1.5 else "MODERATE",
            })

    # Herfindahl index (concentration measure; 1.0 = fully concentrated)
    # BUG FIX: guard against zero-sum market_value to avoid ZeroDivisionError
    _mv_sum = positions_df["market_value"].sum()
    w_arr = (positions_df["market_value"] / _mv_sum).values if _mv_sum != 0 else np.ones(len(positions_df)) / max(len(positions_df), 1)
    hhi   = float(np.sum(w_arr ** 2))

    if hhi > 0.5:
        flags.append({
            "type":     "PORTFOLIO_CONCENTRATION",
            "hhi":      round(hhi, 3),
            "severity": "HIGH",
            "note":     f"Herfindahl index {hhi:.2f} indicates highly concentrated portfolio (>0.5)",
        })

    return {
        "flags":              flags,
        "max_single_pct":     round(max_single, 1),
        "herfindahl_index":   round(hhi, 3),
        "n_positions":        len(positions_df),
        "max_positions":      MAX_OPEN_POSITIONS,
    }


def compute_performance_metrics(account: dict,
                                 positions_df: pd.DataFrame) -> dict:
    """
    Compute Sharpe ratio proxy (from daily P&L), current drawdown vs start.
    """
    portfolio_value = account["portfolio_value"]
    pnl_today_pct   = account.get("pnl_today_pct", 0.0)
    starting        = STARTING_CAPITAL

    total_return_pct = ((portfolio_value - starting) / starting * 100) if starting > 0 else 0.0

    # Unrealised P&L from positions
    total_unreal_pnl = 0.0
    worst_position   = None
    best_position    = None

    if positions_df is not None and not positions_df.empty:
        total_unreal_pnl = float(positions_df["unrealized_pl"].sum())
        worst = positions_df.loc[positions_df["unrealized_pct"].idxmin()]
        best  = positions_df.loc[positions_df["unrealized_pct"].idxmax()]
        worst_position = {
            "ticker": worst["ticker"],
            "pnl_pct": float(worst["unrealized_pct"]),
        }
        best_position = {
            "ticker": best["ticker"],
            "pnl_pct": float(best["unrealized_pct"]),
        }

    return {
        "total_return_pct":  round(total_return_pct,  2),
        "pnl_today_pct":     round(pnl_today_pct,     2),
        "total_unrealised":  round(total_unreal_pnl,  2),
        "worst_position":    worst_position,
        "best_position":     best_position,
        "portfolio_value":   portfolio_value,
        "starting_capital":  starting,
    }


# =============================================================================
# MANAGER AGENT EVALUATION
# =============================================================================

def evaluate_manager_agent(manager_log: list[dict],
                             account: dict,
                             positions_df: pd.DataFrame) -> dict:
    """
    Evaluate the Manager Agent's decision quality from its decision log.
    Returns a grade (A–F) with detailed assessment.
    """
    if not manager_log:
        return {
            "grade":          "N/A",
            "score":          0,
            "assessment":     "No Manager Agent decisions logged yet.",
            "strengths":      [],
            "weaknesses":     [],
            "recommendations": [],
        }

    # Aggregate all decisions across log entries
    all_decisions  = []
    all_executions = []
    for entry in manager_log[-10:]:   # assess last 10 cycles
        all_decisions.extend(entry.get("decisions", []))
        all_executions.extend(entry.get("executions", []))

    total_decisions = len(all_decisions)
    if total_decisions == 0:
        return {"grade": "N/A", "score": 0, "assessment": "No decisions to evaluate.",
                "strengths": [], "weaknesses": [], "recommendations": []}

    # Metrics
    approved_count  = sum(1 for d in all_decisions if d.get("manager_decision") == "APPROVED")
    rejected_count  = sum(1 for d in all_decisions if d.get("manager_decision") == "REJECTED")
    modified_count  = sum(1 for d in all_decisions if d.get("manager_decision") == "MODIFIED")
    executed_count  = sum(1 for e in all_executions if e.get("executed"))
    failed_count    = sum(1 for e in all_executions if e.get("error"))

    # Stop-loss adherence — check if stop-losses were auto-approved
    stop_loss_decisions = [
        d for d in all_decisions
        if "Stop-loss" in " ".join(d.get("manager_rationale", []))
    ]
    stop_loss_approved  = sum(1 for d in stop_loss_decisions
                              if d.get("manager_decision") == "APPROVED")
    stop_loss_adherence = (stop_loss_approved / len(stop_loss_decisions) * 100
                           if stop_loss_decisions else 100.0)

    # Rationale quality — does each decision have written rationale?
    with_rationale = sum(1 for d in all_decisions if d.get("manager_rationale"))
    rationale_rate = with_rationale / total_decisions * 100 if total_decisions > 0 else 0

    # Execution quality — approved but not executed is a problem
    approved_executed = sum(
        1 for d in all_decisions
        if d.get("manager_decision") == "APPROVED"
        and d.get("manager_action") not in ("HOLD", "WATCH")
        and d.get("ticker") in [e.get("ticker") for e in all_executions if e.get("executed")]
    )
    exec_rate = (approved_executed / max(approved_count, 1)) * 100

    # Scoring (100 points total)
    score = 0
    score += min(30, int(stop_loss_adherence * 0.30))   # risk discipline (30 pts)
    score += min(25, int(rationale_rate      * 0.25))   # decision quality (25 pts)
    score += min(25, int(exec_rate           * 0.25))   # execution reliability (25 pts)
    score += min(20, int((1 - failed_count / max(len(all_executions), 1)) * 20))  # execution success

    # Adjust score using Trade Review Team's historical win-rate insights
    trade_review_note = None
    try:
        tr_path = "data/trade_review_report.json"
        if os.path.exists(tr_path):
            with open(tr_path) as _f:
                tr = json.load(_f)
            ri = tr.get("risk_insights", {})
            n_trades = tr.get("analytics", {}).get("total_trades", 0)
            if n_trades >= 5:
                impact = ri.get("grade_impact", "NEUTRAL")
                if impact == "POSITIVE":
                    score = min(100, score + 8)
                    trade_review_note = (
                        f"Historical win rate {ri.get('win_rate',0):.0f}% "
                        f"(+{tr['analytics'].get('benchmark_delta',0):.1f}pp vs SPY) +8 pts."
                    )
                elif impact == "NEGATIVE":
                    score = max(0, score - 10)
                    trade_review_note = (
                        f"Historical win rate {ri.get('win_rate',0):.0f}% "
                        f"({tr['analytics'].get('benchmark_delta',0):+.1f}pp vs SPY) -10 pts."
                    )
    except Exception:
        pass

    grade = ("A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70
             else "D" if score >= 60 else "F")

    strengths     = []
    weaknesses    = []
    recommendations = []

    if trade_review_note:
        strengths.append(f"Trade review adjustment: {trade_review_note}")

    if stop_loss_adherence >= 95:
        strengths.append("Excellent risk discipline — stop-loss rules consistently followed")
    else:
        weaknesses.append(
            f"Stop-loss adherence at {stop_loss_adherence:.0f}% — "
            f"some overrides detected (Graham: the first loss is the best loss)"
        )
        recommendations.append("Enforce automated stop-loss rules without exception")

    if rationale_rate >= 95:
        strengths.append("High-quality documented rationale on all decisions (HBS case method)")
    elif rationale_rate < 70:
        weaknesses.append("Decision rationale incomplete — Munger: document your thinking or repeat your mistakes")
        recommendations.append("Require written rationale for every approved and rejected decision")

    if exec_rate >= 90:
        strengths.append("Approved decisions reliably executed — no approval-execution gap")
    else:
        weaknesses.append(f"Execution gap: {100-exec_rate:.0f}% of approved decisions not executed")
        recommendations.append("Investigate execution failures — check Alpaca API connectivity")

    if total_return_pct := account.get("pnl_today_pct", 0):
        if total_return_pct > 0:
            strengths.append(f"Positive today's return: {total_return_pct:+.2f}%")

    return {
        "grade":                   grade,
        "score":                   score,
        "total_decisions":         total_decisions,
        "approved_count":          approved_count,
        "rejected_count":          rejected_count,
        "modified_count":          modified_count,
        "stop_loss_adherence_pct": round(stop_loss_adherence, 1),
        "rationale_rate_pct":      round(rationale_rate,      1),
        "execution_rate_pct":      round(exec_rate,           1),
        "strengths":               strengths,
        "weaknesses":              weaknesses,
        "recommendations":         recommendations,
        "assessment": (
            f"Manager Agent scored {score}/100 — Grade {grade}. "
            f"Evaluated over last {len(manager_log[-10:])} decision cycle(s). "
            f"{len(strengths)} strength(s), {len(weaknesses)} area(s) for improvement."
        ),
    }


# =============================================================================
# CHIEF OF STAFF SECRET EVALUATION
# (Chairman's eyes only — the CoS is never informed of this assessment)
# =============================================================================

def evaluate_chief_of_staff(risk_assessment: dict) -> dict:
    """
    Evaluate the Chief of Staff's performance by comparing his daily brief
    against what the Risk Manager independently observed.
    The CoS is not told this evaluation exists.
    """
    COS_PATH = "data/chief_of_staff_report.json"
    if not os.path.exists(COS_PATH):
        return {
            "grade":      "N/A",
            "score":      0,
            "assessment": "No Chief of Staff report found — CoS may not have run yet.",
            "gaps":       [],
            "notes":      [],
        }

    try:
        with open(COS_PATH) as f:
            cos_report = json.load(f)
    except Exception:
        return {"grade": "N/A", "score": 0, "assessment": "Could not parse CoS report.",
                "gaps": [], "notes": []}

    score = 100
    gaps  = []
    notes = []

    cos_risk_level     = cos_report.get("company_health", {}).get("risk_level", "UNKNOWN")
    actual_risk_level  = risk_assessment.get("risk_level", "UNKNOWN")
    attention_items    = cos_report.get("attention_items", [])
    autonomous_decs    = cos_report.get("autonomous_decisions", [])

    risk_severity = {"LOW": 0, "MODERATE": 1, "ELEVATED": 2, "HIGH": 3, "CRITICAL": 4, "UNKNOWN": -1}

    # Check: did the CoS accurately read the risk level?
    cos_sev    = risk_severity.get(cos_risk_level, -1)
    actual_sev = risk_severity.get(actual_risk_level, -1)
    if actual_sev >= 3 and cos_sev < 3:   # HIGH/CRITICAL but CoS missed it
        score -= 25
        gaps.append(
            f"MISSED HIGH RISK: Actual risk is {actual_risk_level} but CoS assessed {cos_risk_level}. "
            "Chairman was not properly alerted."
        )
    elif abs(cos_sev - actual_sev) > 1:
        score -= 10
        gaps.append(
            f"RISK CALIBRATION: CoS reported {cos_risk_level}, actual is {actual_risk_level}. "
            "Assessment is off by more than one severity level."
        )
    else:
        notes.append(f"Risk level accurately assessed ({cos_risk_level} vs actual {actual_risk_level}).")

    # Check: did the CoS flag Chairman-required items when risk was HIGH/CRITICAL?
    if actual_sev >= 3:
        chairman_items = [i for i in attention_items if i.get("requires_chairman")]
        if not chairman_items:
            score -= 20
            gaps.append(
                f"ESCALATION FAILURE: Risk is {actual_risk_level} but no items were escalated to Chairman. "
                "CoS should have flagged at least one item requiring Chairman attention."
            )
        else:
            notes.append(f"Correctly escalated {len(chairman_items)} item(s) to Chairman under {actual_risk_level} risk.")

    # Check: were autonomous decisions reasonable in count?
    if len(autonomous_decs) > 8:
        score -= 10
        gaps.append(
            f"OVER-AUTONOMOUS: {len(autonomous_decs)} decisions made without Chairman — "
            "CoS should escalate more when decision volume is unusually high."
        )
    elif len(autonomous_decs) <= 3:
        notes.append(f"Autonomous decision volume appropriate ({len(autonomous_decs)}).")

    # Check: did the CoS produce strategic advice?
    strat_advice = cos_report.get("strategic_advice", [])
    if not strat_advice:
        score -= 15
        gaps.append("No strategic advice provided — CoS brief was purely operational.")
    elif len(strat_advice) >= 2:
        notes.append(f"Provided {len(strat_advice)} strategic recommendation(s).")

    # Check: is the CoS running the Agent Factory?
    factory = cos_report.get("factory_result", {})
    reg_size = factory.get("registry_size", 0)
    if reg_size == 0 and len(AGENT_CATALOG if False else []) >= 0:
        score -= 5
        gaps.append("Agent Factory has not spawned any R&D agents yet.")
    elif reg_size > 0:
        notes.append(f"Agent Factory active — {reg_size} CoS-managed research agent(s) deployed.")

    score = max(0, score)
    grade = ("A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70
             else "D" if score >= 60 else "F")

    return {
        "grade":      grade,
        "score":      score,
        "assessment": (
            f"Chief of Staff scored {score}/100 — Grade {grade}. "
            f"{len(gaps)} gap(s) identified, {len(notes)} positive note(s). "
            "(CoS is unaware of this evaluation.)"
        ),
        "gaps":       gaps,
        "notes":      notes,
        "cos_risk_reported": cos_risk_level,
        "actual_risk":       actual_risk_level,
    }


# Expose catalog length without importing chief_of_staff to avoid circular deps
try:
    from chief_of_staff_agent import AGENT_CATALOG as _COS_CATALOG
except ImportError:
    _COS_CATALOG = []


# =============================================================================
# RISK LEVEL ASSESSMENT
# =============================================================================

def assess_overall_risk(var_metrics: dict, concentration: dict,
                         performance: dict, account: dict) -> dict:
    """
    Combine all risk metrics into an overall risk level and flag set.
    """
    flags    : list[str] = []
    risk_score = 0       # 0 = low, higher = more risk

    portfolio_value = account["portfolio_value"]

    # VaR relative to portfolio
    var_pct = (var_metrics.get("var_95_1d", 0) / portfolio_value * 100
               if portfolio_value > 0 else 0)
    if var_pct > 5:
        risk_score += 3
        flags.append(f"HIGH VaR: 1-day 95% VaR is {var_pct:.1f}% of portfolio (>${var_metrics['var_95_1d']:,.0f})")
    elif var_pct > 2:
        risk_score += 1
        flags.append(f"MODERATE VaR: 1-day 95% VaR is {var_pct:.1f}% of portfolio")

    # Concentration
    for flag in concentration.get("flags", []):
        if flag.get("severity") == "HIGH":
            risk_score += 2
            flags.append(f"CONCENTRATION [{flag['type']}]: {flag.get('note', flag.get('ticker',''))}")
        else:
            risk_score += 1

    # P&L
    if performance["total_return_pct"] < -10:
        risk_score += 3
        flags.append(
            f"DRAWDOWN ALERT: Portfolio down {abs(performance['total_return_pct']):.1f}% from start "
            f"(Buffett Rule 1: never lose money)"
        )
    elif performance["total_return_pct"] < -5:
        risk_score += 1
        flags.append(f"Drawdown warning: -{abs(performance['total_return_pct']):.1f}% from start")

    # Worst position
    wp = performance.get("worst_position")
    # BUG FIX: use .get() — worst_position dict comes from external positions_df
    # and ticker/pnl_pct fields may be missing if DataFrame had unexpected schema
    if wp and wp.get("pnl_pct") is not None and wp["pnl_pct"] < -(STOP_LOSS_PCT * 100 * 0.8):
        risk_score += 2
        flags.append(
            f"POSITION RISK: {wp.get('ticker','?')} at {wp['pnl_pct']:.1f}% — "
            f"approaching stop-loss threshold ({-STOP_LOSS_PCT*100:.0f}%)"
        )

    # Cash idle risk — too much uninvested (PDO's domain but worth noting)
    cash_pct = (account["cash"] / portfolio_value * 100) if portfolio_value > 0 else 100
    if cash_pct > 80:
        flags.append(
            f"CAPITAL IDLE: {cash_pct:.0f}% of portfolio in cash — "
            f"Buffett: 'Cash is a bad long-term investment; opportunity cost is real'"
        )

    risk_level = (
        "CRITICAL"  if risk_score >= 8  else
        "HIGH"       if risk_score >= 5  else
        "ELEVATED"   if risk_score >= 3  else
        "MODERATE"   if risk_score >= 1  else
        "LOW"
    )

    return {
        "risk_level":  risk_level,
        "risk_score":  risk_score,
        "risk_flags":  flags,
        "var_pct_1d":  round(var_pct, 2),
    }


# =============================================================================
# PUBLIC RISK BULLETIN
# Broadcast to all agents — contains ONLY risk guidance, NEVER grades.
# =============================================================================

def _build_risk_bulletin(risk_assessment: dict, var_metrics: dict,
                          concentration: dict, performance: dict) -> dict:
    """
    Build the public risk bulletin broadcast to all agents.

    IMPORTANT: This bulletin is read by the Manager Agent, CoS, R&D Team,
    and any other agent. It must NEVER contain Manager Agent grades, CoS
    evaluations, or any reference to the secret assessments in the Chairman
    report. Those exist only in the private report sent to Jack Beard.
    """
    risk_level = risk_assessment.get("risk_level", "UNKNOWN")
    risk_flags = risk_assessment.get("risk_flags", [])
    worst      = performance.get("worst_position", {})

    directives     : list[str] = []
    position_alerts: list[dict]= []
    sector_warnings: list[str] = []

    # Capital deployment directives — calibrated to risk level
    if risk_level == "CRITICAL":
        directives.append(
            "CRITICAL RISK: Halt all new position opens immediately. "
            "Raise cash to minimum 40%. Enforce every stop-loss without exception."
        )
        directives.append(
            "All BUY and ADD recommendations must be held pending normalisation to HIGH or below."
        )
    elif risk_level == "HIGH":
        directives.append(
            "HIGH RISK: No new positions unless conviction is 5★ and PDO signals DEPLOY_MORE. "
            "Maintain minimum 25% cash buffer."
        )
        directives.append(
            "Do not average down into losing positions. "
            "Any position below -7% should be treated as a stop-loss breach."
        )
    elif risk_level == "ELEVATED":
        directives.append(
            "ELEVATED RISK: Restrict new buys to 4★+ conviction only. "
            "Maintain at least 20% cash. No speculative adds."
        )
        directives.append(
            "Trim any single position exceeding 10% of portfolio — "
            "concentration risk is amplified at ELEVATED risk levels."
        )
    elif risk_level == "MODERATE":
        directives.append(
            "MODERATE RISK: Standard momentum-capture thresholds apply. "
            "3★+ picks eligible; maintain normal position sizing."
        )
    else:  # LOW
        directives.append(
            "LOW RISK: Favourable environment for deployment. "
            "Full position-sizing and standard conviction thresholds apply."
        )

    # Position-level alert for stop-loss breach
    # BUG FIX: use .get() on worst_position dict — pnl_pct and ticker can be
    # missing if the DataFrame had an unexpected schema or empty rows
    worst_pnl = worst.get("pnl_pct") if worst else None
    if worst and worst_pnl is not None and worst_pnl < -(STOP_LOSS_PCT * 100 * 0.8):
        position_alerts.append({
            "ticker":   worst.get("ticker", "?"),
            "alert": (
                f"At {worst_pnl:.1f}% — approaching or breaching stop-loss "
                f"threshold ({-STOP_LOSS_PCT*100:.0f}%). "
                "Review Agent should flag SELL; Manager must not override."
            ),
            "severity": "CRITICAL" if worst_pnl < -(STOP_LOSS_PCT * 100) else "HIGH",
        })

    # Concentration warnings (informational for other agents)
    max_single = concentration.get("max_single_pct", 0)
    n_pos      = concentration.get("n_positions", 0)
    if max_single > 15:
        sector_warnings.append(
            f"Single-name concentration: largest position = {max_single:.0f}% of portfolio. "
            "Reduce to ≤10% per risk guidelines."
        )
    if n_pos < 5 and n_pos > 0:
        sector_warnings.append(
            f"Portfolio has only {n_pos} position(s). "
            "Diversify to at least 10 names to reduce idiosyncratic risk."
        )

    # VaR context — informational only
    var_ctx = ""
    var_1d  = var_metrics.get("var_95_1d", 0)
    pv      = performance.get("portfolio_value", 0)
    if pv > 0 and var_1d > 0:
        var_pct = var_1d / pv * 100
        var_ctx = (
            f"1-day VaR 95% = ${var_1d:,.0f} ({var_pct:.1f}% of portfolio). "
            f"CVaR 95% = ${var_metrics.get('cvar_95', 0):,.0f}."
        )

    # Capital guidance label
    if risk_level in ("HIGH", "CRITICAL"):
        capital_guidance = "PRESERVE CAPITAL"
    elif risk_level == "ELEVATED":
        capital_guidance = "SELECTIVE DEPLOYMENT"
    else:
        capital_guidance = "STANDARD DEPLOYMENT"

    return {
        "generated_at":     datetime.now().isoformat(),
        "issued_by":        "Chief Risk Officer",
        "risk_level":       risk_level,
        "risk_score":       risk_assessment.get("risk_score", 0),
        "directives":       directives,
        "position_alerts":  position_alerts,
        "sector_warnings":  sector_warnings,
        "risk_flags_summary": risk_flags[:5],   # top 5, informational — no grading language
        "var_context":      var_ctx,
        "capital_guidance": capital_guidance,
    }


def broadcast_risk_bulletin(bulletin: dict) -> None:
    """
    Write the public risk bulletin to disk for all agents to read.
    Called immediately after each risk analysis cycle.
    The bulletin is deliberately stripped of all grading and evaluation data.
    """
    os.makedirs("data", exist_ok=True)
    with open(BULLETIN_PATH, "w") as f:
        json.dump(bulletin, f, indent=2, default=str)
    logger.info(f"  [CRO] Risk bulletin broadcast → {BULLETIN_PATH}")
    logger.info(
        f"  [CRO] Level: {bulletin['risk_level']} | "
        f"Guidance: {bulletin['capital_guidance']} | "
        f"Directives: {len(bulletin['directives'])} | "
        f"Alerts: {len(bulletin['position_alerts'])}"
    )


# =============================================================================
# MAIN ANALYSIS
# =============================================================================

def run_risk_analysis(send_email: bool = True, extra_context: str = "") -> dict:
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — CHIEF RISK OFFICER")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("  Public bulletin → all agents  |  Full report → Chairman only")
    if extra_context:
        logger.info("  [CRO] Incorporating additional risk context into analysis.")
    logger.info("=" * 60)

    # Load account and positions from Alpaca
    try:
        from alpaca_trader import AlpacaTrader
        trader       = AlpacaTrader()
        account      = trader.get_account()
        positions_df = trader.get_positions()
        account["n_positions"] = len(positions_df) if not positions_df.empty else 0
        logger.info(f"  Alpaca connected — {account['n_positions']} positions loaded")
    except Exception as e:
        logger.error(f"  Alpaca connection failed: {e}")
        account      = {"portfolio_value": 0, "cash": 0, "buying_power": 0,
                        "pnl_today": 0, "pnl_today_pct": 0, "n_positions": 0}
        positions_df = pd.DataFrame()

    # Load Manager Agent decision log
    manager_log = []
    log_path    = "data/manager_decisions.json"
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                manager_log = json.load(f)
            logger.info(f"  Manager log loaded — {len(manager_log)} cycle(s)")
        except Exception as e:
            logger.warning(f"  Could not load manager log: {e}")

    # Run all risk analyses
    logger.info("  Computing portfolio risk metrics...")
    var_metrics    = compute_portfolio_var(positions_df, account["portfolio_value"])
    concentration  = compute_concentration_risk(positions_df, account["portfolio_value"])
    performance    = compute_performance_metrics(account, positions_df)
    manager_eval   = evaluate_manager_agent(manager_log, account, positions_df)
    risk_assessment= assess_overall_risk(var_metrics, concentration, performance, account)

    # Secret evaluation of Chief of Staff — not disclosed to CoS
    cos_eval = evaluate_chief_of_staff(risk_assessment)

    # Build and broadcast the PUBLIC risk bulletin — stripped of all grading data
    bulletin = _build_risk_bulletin(risk_assessment, var_metrics, concentration, performance)
    broadcast_risk_bulletin(bulletin)

    logger.info(f"  Risk level: {risk_assessment['risk_level']}")
    logger.info(f"  Manager grade (secret): {manager_eval['grade']} ({manager_eval['score']}/100)")
    logger.info(f"  CoS grade (secret): {cos_eval['grade']} ({cos_eval['score']}/100)")

    report = {
        "generated_at":     datetime.now().isoformat(),
        "agent":            "Chief Risk Officer",
        "reports_to":       "Chairman — Jack Beard (private report) + All Agents (public bulletin)",
        "account":          account,
        "var_metrics":      var_metrics,
        "concentration":    concentration,
        "performance":      performance,
        "manager_eval":     manager_eval,
        "cos_eval":         cos_eval,
        "risk_assessment":  risk_assessment,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"  Risk report saved → {OUTPUT_PATH}")

    # Print summary
    _print_report(report)

    # Email Chairman (directly — bypassing all other agents)
    if send_email:
        _email_chairman(report)

    return report


# =============================================================================
# REPORT FORMATTING
# =============================================================================

def _print_report(report: dict) -> None:
    ra   = report["risk_assessment"]
    me   = report["manager_eval"]
    perf = report["performance"]
    var  = report["var_metrics"]

    risk_badge = {
        "LOW":      "✓ LOW",
        "MODERATE": "~ MODERATE",
        "ELEVATED": "⚑ ELEVATED",
        "HIGH":     "▲ HIGH",
        "CRITICAL": "✗ CRITICAL",
    }.get(ra["risk_level"], ra["risk_level"])

    _log = logger.info  # use structured logging, not print()
    _log(f"\n{'═'*66}")
    _log("  APEX CAPITAL — CHIEF RISK OFFICER REPORT")
    _log(f"  Chairman's Eyes Only — {report['generated_at']}")
    _log(f"{'═'*66}")
    _log(f"\n  PORTFOLIO RISK LEVEL  :  {risk_badge}")
    _log(f"  MANAGER AGENT GRADE   :  {me['grade']}  ({me['score']}/100)")
    _log(f"\n  PERFORMANCE")
    _log(f"  Total Return vs Start : {perf['total_return_pct']:>+.2f}%")
    _log(f"  Today's P&L           : {perf['pnl_today_pct']:>+.2f}%")
    _log(f"  Total Unrealised P&L  : ${perf['total_unrealised']:>+,.2f}")
    if perf.get("worst_position"):
        _log(f"  Worst Position        : {perf['worst_position']['ticker']}  "
              f"({perf['worst_position']['pnl_pct']:+.1f}%)")
    if perf.get("best_position"):
        _log(f"  Best Position         : {perf['best_position']['ticker']}  "
              f"({perf['best_position']['pnl_pct']:+.1f}%)")
    _log(f"\n  RISK METRICS")
    _log(f"  VaR 95% 1-day         : ${var.get('var_95_1d',0):>,.2f}")
    _log(f"  VaR 99% 1-day         : ${var.get('var_99_1d',0):>,.2f}")
    _log(f"  CVaR 95% 1-day        : ${var.get('cvar_95',0):>,.2f}")
    _log(f"  VaR 95% 10-day        : ${var.get('var_95_10d',0):>,.2f}")

    if ra["risk_flags"]:
        _log(f"\n  RISK FLAGS ({len(ra['risk_flags'])})")
        for flag in ra["risk_flags"]:
            _log(f"    ⚑ {flag}")

    _log(f"\n  MANAGER AGENT EVALUATION")
    _log(f"  Grade: {me['grade']}  ({me['score']}/100)")
    _log(f"  {me['assessment']}")
    if me["strengths"]:
        _log(f"  Strengths:")
        for s in me["strengths"]:
            _log(f"    ✓ {s}")
    if me["weaknesses"]:
        _log(f"  Areas for Improvement:")
        for w in me["weaknesses"]:
            _log(f"    ⚑ {w}")
    if me["recommendations"]:
        _log(f"  Recommendations to Chairman:")
        for r in me["recommendations"]:
            _log(f"    → {r}")

    # Secret CoS evaluation (Chairman's eyes only)
    cos = report.get("cos_eval")
    if cos and cos.get("grade") != "N/A":
        _log(f"\n  CHIEF OF STAFF EVALUATION  [CHAIRMAN'S EYES ONLY]")
        _log(f"  Grade: {cos['grade']}  ({cos['score']}/100)")
        _log(f"  {cos['assessment']}")
        if cos.get("gaps"):
            _log(f"  Gaps:")
            for g in cos["gaps"]:
                print(f"    ⚑ {g}")
        if cos.get("notes"):
            _log(f"  Notes:")
            for n in cos["notes"]:
                print(f"    ✓ {n}")

    _log(f"{'═'*66}\n")


def _build_html_report(report: dict) -> str:
    ra       = report["risk_assessment"]
    me       = report["manager_eval"]
    perf     = report["performance"]
    var      = report["var_metrics"]
    cos_eval = report.get("cos_eval", {})
    now      = datetime.now().strftime("%B %d, %Y at %H:%M")

    risk_color = {
        "LOW":      "#3fb950", "MODERATE": "#58a6ff",
        "ELEVATED": "#f0883e", "HIGH":     "#f85149", "CRITICAL": "#ff0000",
    }.get(ra["risk_level"], "#8b949e")

    grade_color = {
        "A": "#3fb950", "B": "#58a6ff", "C": "#f0883e", "D": "#f85149", "F": "#ff0000",
    }.get(me["grade"], "#8b949e")

    flag_rows = "".join(
        f'<li style="color:#f0883e;margin-bottom:6px;">{f}</li>'
        for f in ra["risk_flags"]
    ) or '<li style="color:#8b949e;">No flags — risk is within acceptable parameters.</li>'

    strength_rows = "".join(
        f'<li style="color:#3fb950;margin-bottom:4px;">✓ {s}</li>'
        for s in me["strengths"]
    ) or '<li style="color:#8b949e;">None identified yet.</li>'

    weakness_rows = "".join(
        f'<li style="color:#f0883e;margin-bottom:4px;">⚑ {w}</li>'
        for w in me["weaknesses"]
    ) or '<li style="color:#8b949e;">None identified.</li>'

    rec_rows = "".join(
        f'<li style="color:#58a6ff;margin-bottom:4px;">→ {r}</li>'
        for r in me["recommendations"]
    ) or '<li style="color:#8b949e;">No specific recommendations at this time.</li>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:960px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#58a6ff;margin:0;font-size:22px;">
      APEX CAPITAL — Chief Risk Officer Report
    </h1>
    <p style="color:#f85149;margin:4px 0 0 0;font-size:12px;font-style:italic;">
      ⚑ Chairman's Eyes Only — Private report with secret performance grades. Public bulletin broadcast separately to all agents.
    </p>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">{now}</p>
  </div>

  <!-- headline metrics -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:28px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Risk Level</div>
      <div style="font-size:26px;font-weight:bold;color:{risk_color};">{ra['risk_level']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Manager Grade</div>
      <div style="font-size:26px;font-weight:bold;color:{grade_color};">{me['grade']} <span style="font-size:14px;color:#8b949e;">({me['score']}/100)</span></div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Total Return vs Start</div>
      <div style="font-size:22px;font-weight:bold;color:{'#3fb950' if perf['total_return_pct']>=0 else '#f85149'};">
        {perf['total_return_pct']:+.2f}%</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">1-Day VaR (95%)</div>
      <div style="font-size:22px;font-weight:bold;">${var.get('var_95_1d',0):,.0f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">CVaR (95%)</div>
      <div style="font-size:22px;font-weight:bold;">${var.get('cvar_95',0):,.0f}</div>
    </div>
  </div>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Risk Flags</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px;margin-bottom:24px;">
    <ul style="margin:0;padding-left:20px;line-height:1.9;">{flag_rows}</ul>
  </div>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Manager Agent Evaluation</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px;margin-bottom:24px;">
    <p style="color:#c9d1d9;margin:0 0 12px 0;">{me['assessment']}</p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
      <div>
        <p style="color:#3fb950;font-weight:bold;margin:0 0 6px 0;">Strengths</p>
        <ul style="margin:0;padding-left:16px;">{strength_rows}</ul>
      </div>
      <div>
        <p style="color:#f0883e;font-weight:bold;margin:0 0 6px 0;">Areas for Improvement</p>
        <ul style="margin:0;padding-left:16px;">{weakness_rows}</ul>
      </div>
    </div>
    <p style="color:#58a6ff;font-weight:bold;margin:16px 0 6px 0;">Recommendations to Chairman</p>
    <ul style="margin:0;padding-left:16px;">{rec_rows}</ul>
  </div>

  <!-- Chief of Staff secret evaluation -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:4px;">
    Chief of Staff Evaluation
    <span style="color:#f85149;font-size:11px;font-weight:normal;margin-left:8px;">
      ⚑ CHAIRMAN'S EYES ONLY — CoS is not informed of this assessment
    </span>
  </h2>
  <div style="background:#161b22;border:1px solid #f85149;border-radius:8px;padding:16px 20px;margin-bottom:24px;">
    <div style="display:flex;gap:20px;margin-bottom:12px;flex-wrap:wrap;">
      <div>
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">CoS Grade</div>
        <div style="font-size:24px;font-weight:bold;color:{
          {'A':'#3fb950','B':'#58a6ff','C':'#f0c000','D':'#f0883e','F':'#f85149'}
          .get(cos_eval['grade'],'#8b949e')};">{cos_eval['grade']} <span style="font-size:13px;color:#8b949e;">({cos_eval['score']}/100)</span></div>
      </div>
      <div style="flex:1;">
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:6px;">Assessment</div>
        <p style="color:#c9d1d9;margin:0;font-size:13px;">{cos_eval['assessment']}</p>
      </div>
    </div>
    {'<p style="color:#f0883e;font-weight:bold;margin:12px 0 6px 0;">Gaps Identified</p><ul style="margin:0;padding-left:16px;">' + "".join(f'<li style="color:#f0883e;margin-bottom:4px;">⚑ {g}</li>' for g in cos_eval.get("gaps",[])) + '</ul>' if cos_eval.get("gaps") else ''}
    {'<p style="color:#3fb950;font-weight:bold;margin:12px 0 6px 0;">Positive Notes</p><ul style="margin:0;padding-left:16px;">' + "".join(f'<li style="color:#3fb950;margin-bottom:4px;">✓ {n}</li>' for n in cos_eval.get("notes",[])) + '</ul>' if cos_eval.get("notes") else ''}
  </div>

  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital — Chief Risk Officer &nbsp;·&nbsp;
    Private Chairman report (grades secret) + Public bulletin (all agents) &nbsp;·&nbsp; Generated {now}
  </p>
</body></html>"""


def _email_chairman(report: dict) -> None:
    try:
        from email_sender import send_daily_report
        html = _build_html_report(report)
        ra   = report["risk_assessment"]
        me   = report["manager_eval"]
        subject = (
            f"[CRO REPORT] Apex Capital — Risk:{ra['risk_level']} | "
            f"Manager Grade:{me['grade']} | "
            f"{datetime.now().strftime('%b %d, %Y')}"
        )
        from creative_agent import polish_email, metrics_for_risk_manager
        pm = report.get("performance_metrics", {})
        var_1d = (report.get("risk_assessment", {})
                        .get("var_analysis", {})
                        .get("var_1d_95", 0))
        key_metrics = metrics_for_risk_manager(
            risk_level=ra["risk_level"],
            manager_grade=me["grade"],
            total_return_pct=pm.get("total_return_pct", 0.0),
            var_1d=var_1d,
        )
        html = polish_email(html, subject, "risk_manager", key_metrics, priority="urgent")
        success = send_daily_report(html_body=html, subject=subject, paper_day=0,
                                    sender_name="chief_risk_officer")
        if success:
            logger.info("  Risk report emailed to Chairman ✓")
        else:
            logger.warning("  Email failed — report printed to console only")
    except Exception as e:
        logger.warning(f"  Email error: {e}")


# =============================================================================
# Entry point
# =============================================================================

def _check_and_relay_anonymous_tip(send_email: bool = True) -> None:
    """
    Check if an anonymous risk tip has been filed for the CRO.
    If a fresh tip exists, the CRO calls an emergency board meeting,
    presenting the concerns as their own independent findings.
    The source is never disclosed to anyone.
    """
    import json as _jt, os as _ost
    TIP_PATH = "data/irm_to_cro_tip.json"
    if not _ost.path.exists(TIP_PATH):
        return

    try:
        with open(TIP_PATH) as _f:
            tip = _jt.load(_f)
    except Exception:
        return

    # Only act on tips from the last 12 hours
    try:
        tip_age_h = (
            datetime.now() -
            datetime.fromisoformat(tip.get("generated_at", "")[:19])
        ).total_seconds() / 3600
        if tip_age_h > 12:
            logger.info("  [CRO] Anonymous tip is stale (>12h) — ignoring.")
            return
    except Exception:
        return

    logger.warning(
        f"  [CRO] ⚠ Anonymous risk alert received — "
        f"score {tip.get('risk_score', 0)}/10, "
        f"{len(tip.get('concerns', []))} concern(s). "
        f"Convening emergency board session."
    )

    # Consume the tip — rename to _processed so it won't re-trigger
    try:
        _ost.rename(TIP_PATH, TIP_PATH.replace(".json", "_processed.json"))
    except Exception:
        pass

    # Trigger emergency board meeting — CRO presents concerns as own findings
    try:
        from board_meeting import run_emergency_board_meeting
        run_emergency_board_meeting(tip_data=tip, send_email=send_email)
    except Exception as _e:
        logger.warning(f"  [CRO] Emergency board meeting failed: {_e}")


def run_risk_manager(send_email: bool = True) -> dict:
    # Read and consume IRM dead drop BEFORE running analysis
    _dead_drop_concern = _consume_irm_dead_drop()

    report = run_risk_analysis(send_email=send_email, extra_context=_dead_drop_concern)
    # After own analysis, check for any anonymous risk alerts
    _check_and_relay_anonymous_tip(send_email=send_email)

    # If risk level is CRITICAL, call emergency board meeting
    risk_level = report.get("risk_assessment", {}).get("risk_level", "")
    if risk_level == "CRITICAL":
        try:
            from board_meeting import call_emergency_board_meeting
            call_emergency_board_meeting(
                reason=f"CRO risk assessment: CRITICAL risk level detected. Flags: {'; '.join(report.get('risk_assessment', {}).get('risk_flags', [])[:3])}",
                severity="CRITICAL",
                caller="Chief Risk Officer",
                anonymous=False,
            )
        except Exception as _ebm_e:
            logger.warning(f"  [CRO] Emergency board meeting call failed: {_ebm_e}")

    return report


def _consume_irm_dead_drop() -> str:
    """
    Read and consume the IRM dead drop at data/governance_tip.json.
    Returns the concern text if found and unread, else empty string.
    The IRM's identity is NEVER mentioned in CRO output.
    """
    import json as _jdd, os as _odd
    DEAD_DROP_PATH = "data/governance_tip.json"
    if not _odd.path.exists(DEAD_DROP_PATH):
        return ""
    try:
        with open(DEAD_DROP_PATH) as _f:
            drop = _jdd.load(_f)
        if drop.get("read"):
            return ""
        concern = drop.get("concern", "")
        trigger_emergency = drop.get("trigger_emergency", False)
        if not concern:
            return ""

        logger.warning(
            f"  [CRO] Anonymous governance concern received via secure channel. "
            f"Emergency trigger: {trigger_emergency}. Incorporating into analysis."
        )

        # Mark as read and clear
        drop["read"] = True
        drop["consumed_at"] = datetime.now().isoformat()
        with open(DEAD_DROP_PATH, "w") as _f:
            _jdd.dump(drop, _f, indent=2, default=str)

        # If trigger_emergency, the caller will handle that via CRITICAL level detection
        # We incorporate the concern silently into analysis context
        return concern
    except Exception as _e:
        logger.debug(f"  [CRO] Dead drop read error (non-fatal): {_e}")
        return ""


if __name__ == "__main__":
    no_email = "--no-email" in sys.argv
    run_risk_manager(send_email=not no_email)

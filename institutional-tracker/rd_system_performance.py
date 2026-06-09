# =============================================================================
# APEX CAPITAL — R&D SYSTEM PERFORMANCE ANALYST
# =============================================================================
# Reports directly to the R&D Team Leader, who includes this report in the
# larger R&D briefing to the board.
#
# PURPOSE
# ────────
# Monitors the operational health of the investment system itself — not the
# portfolio, but the machinery that manages the portfolio. Catches patterns
# that no other agent is looking for:
#
#   • Take-profit targets reached but not exited (gains left on table)
#   • Stop-losses breached but not enforced (losses allowed to compound)
#   • Alert fatigue — same signal firing repeatedly without resolution
#   • Decision latency — gap between signal and execution
#   • Dead canary loops — canary fires, manager runs, position unchanged
#   • Pipeline failures — steps that errored silently and were skipped
#   • Signals issued but never actioned by the Manager Agent
#
# This agent was created following the board session of May 1, 2026, after
# AMD (+57%), AVGO (+33%), and MRVL (+49%) were held past take-profit targets
# while the canary fired 23 times on the same two positions without resolution.
#
# OUTPUT
# ───────
#   data/rd_system_performance.json  — structured findings for rd_team_leader
#
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("rd_system_performance")

# ── Data paths ──────────────────────────────────────────────────────────────
_DATA = {
    "manager":    "data/manager_decisions.json",
    "review":     "data/review_recommendations.json",
    "canary":     "data/canary_state.json",
    "canary_alert": "data/canary_alert.json",
    "risk":       "data/risk_bulletin.json",
    "cos":        "data/chief_of_staff_report.json",
    "board":      "data/board_meeting_report.json",
    "irm":        "data/irm_daily_check.json",
    "out":        "data/rd_system_performance.json",
}

# ── Thresholds ───────────────────────────────────────────────────────────────
TAKE_PROFIT_TARGET_PCT       = 20.0   # flag if position held > this past target
TAKE_PROFIT_CRITICAL_PCT     = 35.0   # critical if > this past target
STOP_LOSS_LIMIT_PCT          = -7.0   # flag if position exceeds this loss
STOP_LOSS_CRITICAL_PCT       = -10.0  # critical if worse than this
ALERT_FATIGUE_THRESHOLD      = 5      # flag if same trigger fires > N times
ALERT_FATIGUE_CRITICAL       = 15     # critical if > N identical triggers
DECISION_LATENCY_WARN_CYCLES = 2      # flag if SELL signal unactioned > N cycles


def _safe_load(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


# =============================================================================
# ANALYSIS MODULES
# =============================================================================

def _analyse_take_profit_leakage(review_data) -> list[dict]:
    """
    Detect positions that have exceeded their take-profit target but remain open.
    This represents unrealised gains at risk of mean-reverting before capture.
    """
    findings = []
    recs = review_data if isinstance(review_data, list) else review_data.get("recommendations", [])

    for r in recs:
        ticker  = r.get("ticker", "?")
        action  = r.get("action", "")
        pnl_pct = r.get("unrealized_pnl_pct", 0) or 0

        # A SELL recommendation on a profitable long = take-profit not captured
        if action == "SELL" and pnl_pct > TAKE_PROFIT_TARGET_PCT:
            severity = "CRITICAL" if pnl_pct > TAKE_PROFIT_CRITICAL_PCT else "HIGH"
            overshoot = pnl_pct - TAKE_PROFIT_TARGET_PCT
            findings.append({
                "type":     "TAKE_PROFIT_LEAKAGE",
                "severity": severity,
                "ticker":   ticker,
                "pnl_pct":  pnl_pct,
                "overshoot_pct": round(overshoot, 1),
                "message":  (
                    f"{ticker} is {pnl_pct:+.1f}% — {overshoot:.1f}% past the "
                    f"{TAKE_PROFIT_TARGET_PCT:.0f}% take-profit target. Review agent "
                    f"has flagged SELL but position remains open. Gains are at risk "
                    f"of mean-reverting before the next pipeline run."
                ),
            })
    return findings


def _analyse_stop_loss_enforcement(review_data) -> list[dict]:
    """
    Detect positions where the stop-loss has been breached and the position
    remains open — losses allowed to compound past the policy limit.
    """
    findings = []
    recs = review_data if isinstance(review_data, list) else review_data.get("recommendations", [])

    for r in recs:
        ticker  = r.get("ticker", "?")
        action  = r.get("action", "")
        pnl_pct = r.get("unrealized_pnl_pct", 0) or 0

        if action == "SELL" and pnl_pct < STOP_LOSS_LIMIT_PCT:
            severity = "CRITICAL" if pnl_pct < STOP_LOSS_CRITICAL_PCT else "HIGH"
            overshoot = abs(pnl_pct) - abs(STOP_LOSS_LIMIT_PCT)
            findings.append({
                "type":     "STOP_LOSS_BREACH_UNRESOLVED",
                "severity": severity,
                "ticker":   ticker,
                "pnl_pct":  pnl_pct,
                "breach_pct": round(overshoot, 1),
                "message":  (
                    f"{ticker} is {pnl_pct:+.1f}% — {overshoot:.1f}% past the "
                    f"{STOP_LOSS_LIMIT_PCT:.0f}% stop-loss limit. Position should "
                    f"have been closed automatically. Every additional cycle open "
                    f"compounds the loss beyond the policy-defined maximum."
                ),
            })
    return findings


def _analyse_alert_fatigue(canary_data: dict) -> list[dict]:
    """
    Detect alert fatigue: the same trigger firing repeatedly without the
    underlying position being resolved. This is a decision-loop failure —
    the system identifies the problem but the response doesn't close it.
    """
    findings = []
    triggers = canary_data.get("manager_triggers", [])

    if not triggers:
        return findings

    # Group consecutive triggers by their trigger text
    trigger_counts: dict[str, int] = {}
    for t in triggers:
        trig_list = t.get("triggers", [])
        for trig in trig_list:
            # Normalise: strip the specific % to group by ticker/type
            key = trig.split(" down ")[0] if " down " in trig else trig[:40]
            trigger_counts[key] = trigger_counts.get(key, 0) + 1

    for trigger_key, count in trigger_counts.items():
        if count >= ALERT_FATIGUE_THRESHOLD:
            severity = "CRITICAL" if count >= ALERT_FATIGUE_CRITICAL else "HIGH"
            findings.append({
                "type":     "ALERT_FATIGUE",
                "severity": severity,
                "trigger":  trigger_key,
                "count":    count,
                "message":  (
                    f"'{trigger_key}' fired {count} times without resolution. "
                    f"This is a decision-loop failure: the canary correctly identifies "
                    f"the problem, but the response pipeline is not closing the position. "
                    f"Repeated identical alerts without resolution erode the signal-to-noise "
                    f"ratio and indicate a gap between detection and execution."
                ),
            })
    return findings


def _analyse_signal_execution_gap(manager_data) -> list[dict]:
    """
    Detect cases where the Manager Agent received a clear SELL signal from
    the review agent but the position was still held the following cycle.
    Uses the manager decisions log to find approved HOLD/WATCH on positions
    that had SELL recommendations.
    """
    findings = []
    cycles = manager_data if isinstance(manager_data, list) else [manager_data]

    if len(cycles) < 2:
        return findings  # Need at least 2 cycles to detect a gap

    # Look at the most recent two cycles
    prev_cycle   = cycles[-2] if len(cycles) >= 2 else {}
    latest_cycle = cycles[-1]

    prev_decisions   = {d.get("ticker", d.get("source_agent","")): d
                        for d in prev_cycle.get("decisions", [])}
    latest_decisions = {d.get("ticker", d.get("source_agent","")): d
                        for d in latest_cycle.get("decisions", [])}

    for ticker, prev_d in prev_decisions.items():
        if prev_d.get("manager_action") == "SELL" and prev_d.get("manager_decision") == "APPROVED":
            # If same ticker appears in the next cycle and still has a position → gap
            latest_d = latest_decisions.get(ticker, {})
            if latest_d.get("manager_action") in ("HOLD", "WATCH", "ADD"):
                findings.append({
                    "type":     "SIGNAL_EXECUTION_GAP",
                    "severity": "MEDIUM",
                    "ticker":   ticker,
                    "message":  (
                        f"{ticker}: SELL was approved in the previous cycle but the "
                        f"position was not closed. The following cycle the Manager "
                        f"treated it as {latest_d.get('manager_action','?')}. "
                        f"This suggests an order was submitted but not confirmed, "
                        f"or the position was re-evaluated without checking execution status."
                    ),
                })
    return findings


def _analyse_pipeline_health(cos_data: dict) -> list[dict]:
    """
    Assess overall pipeline health from the CoS report — are agents
    producing outputs, are there repeated escalations, is the research
    team generating picks?
    """
    findings = []
    health = cos_data.get("company_health", {})
    advice = cos_data.get("strategic_advice", [])

    # Count chronic escalations (same advice for 3+ cycles)
    chronic = [a for a in advice if a.get("escalated")]
    if chronic:
        findings.append({
            "type":     "CHRONIC_UNRESOLVED_ISSUE",
            "severity": "MEDIUM",
            "count":    len(chronic),
            "issues":   [a.get("advice", "")[:80] for a in chronic[:3]],
            "message":  (
                f"{len(chronic)} strategic advice item(s) have been flagged for "
                f"3+ consecutive cycles without resolution. Chronic issues indicate "
                f"systemic problems that the routine pipeline is not addressing."
            ),
        })

    # Zero-pick cycles (research team not producing)
    total_picks = health.get("total_picks", 0)
    if total_picks == 0:
        findings.append({
            "type":     "ZERO_PICK_CYCLE",
            "severity": "MEDIUM",
            "message":  (
                "The research pipeline produced zero picks this cycle. "
                "This could indicate all agents are in a defensive/blocked state, "
                "or that the CVaR circuit breaker is filtering everything. "
                "Zero picks means no new opportunity identification — "
                "the system is running in pure maintenance mode."
            ),
        })

    return findings


# =============================================================================
# MAIN ANALYSER
# =============================================================================

class SystemPerformanceAnalyst:
    """
    Audits the operational health of the investment system itself.
    Finds patterns that indicate the machinery is not working as designed —
    independent of whether the market is up or down.
    """

    def analyse(self, quiet: bool = False) -> dict:
        """Run full system performance audit. Returns structured findings."""
        if not quiet:
            logger.info("[SystemPerformanceAnalyst] Running operational audit...")

        # Load data
        review_data  = _safe_load(_DATA["review"])
        canary_data  = _safe_load(_DATA["canary"])
        manager_data = _safe_load(_DATA["manager"])
        cos_data     = _safe_load(_DATA["cos"])

        # Run all analysis modules
        all_findings: list[dict] = []
        all_findings += _analyse_take_profit_leakage(review_data)
        all_findings += _analyse_stop_loss_enforcement(review_data)
        all_findings += _analyse_alert_fatigue(canary_data)
        all_findings += _analyse_signal_execution_gap(manager_data)
        all_findings += _analyse_pipeline_health(cos_data)

        # Severity counts
        critical = [f for f in all_findings if f.get("severity") == "CRITICAL"]
        high     = [f for f in all_findings if f.get("severity") == "HIGH"]
        medium   = [f for f in all_findings if f.get("severity") == "MEDIUM"]

        # Overall system health verdict
        if critical:
            verdict       = "SYSTEM DEGRADED"
            health_score  = max(0, 10 - len(critical) * 3 - len(high))
        elif high:
            verdict       = "OPERATIONAL GAPS DETECTED"
            health_score  = max(3, 10 - len(high) * 2 - len(medium))
        elif medium:
            verdict        = "MINOR INEFFICIENCIES"
            health_score   = max(6, 10 - len(medium))
        else:
            verdict        = "SYSTEM OPERATING NORMALLY"
            health_score   = 10

        # Recommendations
        recommendations: list[str] = []
        for f in critical + high:
            ftype = f.get("type", "")
            if ftype == "TAKE_PROFIT_LEAKAGE":
                recommendations.append(
                    f"Execute pending take-profit exit for {f['ticker']} "
                    f"({f['pnl_pct']:+.1f}%) immediately — do not wait for scheduled run."
                )
            elif ftype == "STOP_LOSS_BREACH_UNRESOLVED":
                recommendations.append(
                    f"Close stop-loss breach on {f['ticker']} ({f['pnl_pct']:+.1f}%) "
                    f"immediately. Consider adding pre-market stop enforcement check."
                )
            elif ftype == "ALERT_FATIGUE":
                recommendations.append(
                    f"Investigate why '{f['trigger']}' alert ({f['count']}x) did not "
                    f"resolve. Ensure emergency cascade closes the triggering position, "
                    f"not just runs the Manager Agent."
                )
            elif ftype == "SIGNAL_EXECUTION_GAP":
                recommendations.append(
                    f"Verify {f['ticker']} sell order was confirmed in Alpaca. "
                    f"Add post-execution confirmation to Manager Agent loop."
                )

        result = {
            "generated_at":      datetime.now().isoformat(),
            "analyst":           "System Performance Analyst",
            "verdict":           verdict,
            "health_score":      health_score,
            "total_findings":    len(all_findings),
            "critical_count":    len(critical),
            "high_count":        len(high),
            "medium_count":      len(medium),
            "findings":          all_findings,
            "recommendations":   recommendations,
            "summary": (
                f"{verdict} — {len(all_findings)} finding(s) "
                f"({len(critical)} critical, {len(high)} high, {len(medium)} medium). "
                f"System health score: {health_score}/10."
            ),
        }

        # Save to disk for rd_team_leader
        os.makedirs("data", exist_ok=True)
        with open(_DATA["out"], "w") as f:
            json.dump(result, f, indent=2, default=str)

        if not quiet:
            logger.info(
                f"[SystemPerformanceAnalyst] {verdict} | "
                f"score={health_score}/10 | findings={len(all_findings)} "
                f"(crit={len(critical)}, high={len(high)}, med={len(medium)})"
            )
        return result


# =============================================================================
# Standalone entry point
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s")
    analyst = SystemPerformanceAnalyst()
    result  = analyst.analyse()
    print(f"\n{'═'*60}")
    print(f"  SYSTEM PERFORMANCE AUDIT")
    print(f"{'═'*60}")
    print(f"  Verdict:      {result['verdict']}")
    print(f"  Health Score: {result['health_score']}/10")
    print(f"  Findings:     {result['total_findings']} "
          f"({result['critical_count']} critical, "
          f"{result['high_count']} high, "
          f"{result['medium_count']} medium)")
    print()
    for f in result["findings"]:
        sev = f.get("severity", "?")
        icon = "🔴" if sev == "CRITICAL" else "🟠" if sev == "HIGH" else "🟡"
        print(f"  {icon} [{sev}] {f.get('type','?')}")
        print(f"     {f.get('message','')[:110]}")
    if result["recommendations"]:
        print(f"\n  Recommendations:")
        for r in result["recommendations"]:
            print(f"    → {r[:110]}")
    print(f"{'═'*60}\n")

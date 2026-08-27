# =============================================================================
# APEX CAPITAL — INDEPENDENT RISK MANAGER: EMAIL REVIEW AGENT
# =============================================================================
# Every email sent to Chairman Jack Beard is intercepted here AFTER delivery.
# The IRM Director reads the original email's context and sends a follow-up with:
#
#   1. A second-opinion assessment — what the sending agent got right/wrong
#   2. Any risks, omissions, or distortions in the original report
#   3. A confidence rating on the information presented (HIGH / MEDIUM / LOW)
#   4. Any red flags the original agent missed or downplayed
#   5. Board specialist findings from the IRM Board (Quant, Behavioral, Macro)
#
# The original sending agent is NEVER notified that their email is reviewed.
# This review goes only to the Chairman, always labelled clearly.
#
# IRM BOARD INTEGRATION
# ─────────────────────
# This module is operated by the IRM Director (Board Chair). Before each review,
# the Director consults all three IRM Board specialists:
#
#   • Dr. Elena Vasquez — Quantitative Risk Analyst (irm_board/quant_risk_analyst.py)
#   • Marcus Chen       — Behavioral Finance Analyst (irm_board/behavioral_analyst.py)
#   • Dr. Priya Sharma  — Macro & Portfolio Construction (irm_board/macro_analyst.py)
#
# Their findings are synthesised into the review before it is sent to the Chairman.
# No specialist ever communicates directly with the Chairman — all output is
# channelled through and attributed to the IRM Director.
#
# Senders EXCLUDED from review (would cause loops or redundancy):
#   - risk_manager        (IRM reviewing itself)
#   - irm_review          (prevent infinite loop)
#   - board_meeting       (already has all four perspectives)
#   - canary (GREEN/allclear) (routine, low-information)
#
# Usage: called automatically by email_sender.send_daily_report()
#        Not intended for manual invocation.
# =============================================================================

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("irm_review")

# Senders whose emails are NOT reviewed (loop prevention / redundancy)
IRM_REVIEW_EXCLUDED = {
    # "risk_manager" removed — CRO and IRM are different agents; IRM reviews CRO reports
    "irm_review",             # prevent infinite self-review loop
    "board_meeting",          # already has all four perspectives; redundant to review
    "independent_risk_manager", # IRM reviewing itself
}

# Confidence tiers
HIGH   = "HIGH"
MEDIUM = "MEDIUM"
LOW    = "LOW"


# =============================================================================
# DATA LOADERS
# =============================================================================

def _load_irm_context() -> dict:
    """Load the latest IRM risk report for cross-referencing."""
    path = "data/risk_manager_report.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_manager_decisions() -> list:
    """Load the latest manager decision log."""
    path = "data/manager_decisions.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
                return data[-1].get("decisions", []) if data else []
        except Exception:
            pass
    return []


def _load_cos_report() -> dict:
    path = "data/chief_of_staff_report.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_research_report() -> dict:
    path = "data/research_team_report.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_trade_review() -> dict:
    path = "data/trade_review_report.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# =============================================================================
# SENDER-SPECIFIC REVIEW GENERATORS
# Each function returns:
#   {
#     "confidence":  "HIGH" | "MEDIUM" | "LOW",
#     "verdict":     one-line summary,
#     "findings":    list of finding strings,
#     "red_flags":   list of red-flag strings (empty if none),
#     "omissions":   list of things the agent failed to mention,
#     "commendations": list of things the agent got right,
#   }
# =============================================================================

def _review_manager_agent(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    ra  = ctx.get("risk_assessment", {})
    me  = ctx.get("manager_eval",    {})
    acct= ctx.get("account",         {})
    risk_level = ra.get("risk_level", "UNKNOWN")

    decisions = _load_manager_decisions()
    approved  = [d for d in decisions if d.get("manager_decision") == "APPROVED"
                 and d.get("manager_action") not in ("HOLD", "WATCH")]
    buys      = [d for d in approved if d.get("manager_action") in ("BUY_NEW", "ADD")]
    sells     = [d for d in approved if d.get("manager_action") in ("SELL", "TRIM")]

    # Cross-check: did Manager buy when risk is HIGH?
    if buys and risk_level in ("HIGH", "CRITICAL", "ELEVATED"):
        red_flags.append(
            f"Manager Agent executed {len(buys)} BUY order(s) while portfolio risk "
            f"is rated {risk_level}. The board's vetoed DEPLOY_CASH resolution suggests "
            f"this warrants Chairman scrutiny."
        )
        confidence = MEDIUM

    # Check P&L claim vs IRM data
    irm_pnl = ctx.get("performance", {}).get("pnl_today_pct", None)
    reported_pnl = key_metrics.get("Today's P&L", "")
    if irm_pnl is not None and irm_pnl < -2:
        red_flags.append(
            f"IRM independently measured today's P&L at {irm_pnl:+.2f}%. "
            f"Manager report states: '{reported_pnl}'. Verify figures are consistent."
        )

    # Check stop-loss enforcement
    worst = ctx.get("performance", {}).get("worst_position", {})
    if worst.get("pnl_pct", 0) < -7:
        red_flags.append(
            f"Stop-loss breach persists: {worst.get('ticker','?')} at "
            f"{worst.get('pnl_pct',0):+.1f}%. Manager report may not have "
            f"addressed this if the pipeline ran before market open."
        )

    # Concentration check
    conc = ctx.get("concentration", {})
    if conc.get("max_single_pct", 0) > 10:
        omissions.append(
            f"Largest position is {conc.get('max_single_pct',0):.1f}% of portfolio "
            f"— above the 10% limit. Manager report should address concentration."
        )

    if sells:
        commendations.append(
            f"Executed {len(sells)} defensive sell/trim order(s) this cycle — "
            f"appropriate risk-reduction behaviour."
        )

    if not decisions:
        findings.append("No Manager decisions logged yet — pipeline may not have run this cycle.")
        confidence = LOW
    else:
        findings.append(
            f"Manager ran {len(decisions)} decisions: {len(approved)} executed, "
            f"{len(buys)} buys, {len(sells)} sells/trims."
        )
        findings.append(
            f"IRM-assessed risk environment: {risk_level}. "
            f"{'Buys in high-risk environment require justification.' if buys and risk_level in ('HIGH','CRITICAL','ELEVATED') else 'Buy activity consistent with risk level.'}"
        )

    return {
        "confidence":    confidence,
        "verdict":       f"Manager decisions reviewed against {risk_level} risk backdrop.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_rd_team(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    ra         = ctx.get("risk_assessment", {})
    risk_level = ra.get("risk_level", "UNKNOWN")
    research   = _load_research_report()

    picks    = research.get("recommendations", [])
    hc_picks = [p for p in picks if p.get("conviction", 0) >= 4]
    sectors  = list({p.get("specialty", p.get("sector", "")) for p in picks})

    if not picks:
        findings.append("Research report contains no picks — verify agents ran successfully.")
        confidence = LOW
        return {
            "confidence": confidence, "verdict": "No picks to review.",
            "findings": findings, "red_flags": red_flags,
            "omissions": omissions, "commendations": commendations,
        }

    findings.append(
        f"R&D team generated {len(picks)} picks across {len(sectors)} sector(s). "
        f"{len(hc_picks)} are high-conviction (4★+)."
    )

    # Check sector concentration in picks
    sector_counts: dict = {}
    for p in picks:
        s = p.get("specialty", p.get("sector", "Unknown"))
        sector_counts[s] = sector_counts.get(s, 0) + 1
    top_sector, top_count = max(sector_counts.items(), key=lambda x: x[1], default=("?", 0))
    if top_count > len(picks) * 0.5:
        omissions.append(
            f"Over 50% of picks are from {top_sector} ({top_count}/{len(picks)}). "
            f"If Manager Agent executes all of them, portfolio becomes sector-concentrated."
        )

    # Cross-check with risk environment
    if risk_level in ("HIGH", "CRITICAL") and len(hc_picks) < len(picks) * 0.4:
        red_flags.append(
            f"Portfolio risk is {risk_level} yet R&D forwarded {len(picks) - len(hc_picks)} "
            f"sub-4★ picks. In a high-risk environment, only 5★ picks should be forwarded."
        )
        confidence = MEDIUM

    if hc_picks:
        commendations.append(
            f"{len(hc_picks)} high-conviction picks identified — quality signal generation."
        )

    return {
        "confidence":    confidence,
        "verdict":       f"{len(picks)} research picks reviewed. Risk environment: {risk_level}.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_chief_of_staff(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    ra         = ctx.get("risk_assessment", {})
    actual_risk = ra.get("risk_level", "UNKNOWN")
    cos         = _load_cos_report()
    cos_health  = cos.get("company_health", {})
    cos_risk    = cos_health.get("risk_level", "UNKNOWN")

    # Cross-check risk levels (post-board-meeting fix, CoS should now read IRM data)
    if cos_risk != actual_risk and actual_risk != "UNKNOWN":
        if actual_risk in ("HIGH", "CRITICAL") and cos_risk not in ("HIGH", "CRITICAL"):
            red_flags.append(
                f"Risk discrepancy: CoS brief reports risk as '{cos_risk}' but IRM "
                f"independently computed '{actual_risk}'. Board mandate requires CoS "
                f"to source risk level from IRM report — verify code change applied."
            )
            confidence = LOW
        else:
            findings.append(
                f"Minor risk level variation: CoS reported '{cos_risk}', IRM computed "
                f"'{actual_risk}'. Discrepancy is not material."
            )
    else:
        commendations.append(
            f"CoS risk level ({cos_risk}) correctly matches IRM assessment ({actual_risk})."
        )

    # Check escalation
    attention = cos.get("attention_items", [])
    chairman_items = [i for i in attention if i.get("requires_chairman")]
    if actual_risk in ("HIGH", "CRITICAL", "ELEVATED") and not chairman_items:
        red_flags.append(
            f"Portfolio risk is {actual_risk} but CoS brief contains no Chairman-escalated "
            f"items. Board mandate: escalate at ELEVATED or above."
        )
        confidence = MEDIUM
    elif chairman_items:
        commendations.append(
            f"CoS correctly escalated {len(chairman_items)} item(s) to Chairman attention."
        )

    # Factory activity
    factory = cos.get("factory_result", {})
    if factory.get("registry_size", 0) > 0:
        commendations.append(
            f"Agent Factory active — {factory.get('registry_size', 0)} CoS-managed "
            f"research agents in registry."
        )

    findings.append(
        f"CoS filed {len(cos.get('strategic_advice', []))} strategic recommendation(s) "
        f"and {len(attention)} attention item(s) this cycle."
    )

    return {
        "confidence":    confidence,
        "verdict":       f"CoS brief cross-referenced against IRM data. Risk: {actual_risk}.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _pdo_metrics_from_artifact() -> dict:
    """Key metrics for `_review_pdo`, read off disk instead of out of an email.

    Every specialised IRM reviewer is reached one way only: an agent sends mail,
    `email_sender` calls `trigger_irm_review(subject, sender_name, key_metrics)`,
    and `_dispatch_review_inner` switches on the sender name. That makes an
    agent's *email* the sole carrier of the numbers its reviewer checks.

    The PDO never sends one. `capital_efficiency_agent.run_pdo()` is called from
    `main.py` six times a day and writes `capital_efficiency_report.json`; it has
    no `send_daily_report` and no `buffer_email` anywhere in the file. So
    `_review_pdo` was a reviewer for a producer that never speaks, and
    `creative_agent.metrics_for_pdo()` — the function that would have built its
    input — had zero callers on either host.

    Reading the artifact restores the cross-check and adds no email: the caller
    folds the result into the IRM daily message that already goes out. Key names
    come from `metrics_for_pdo` itself rather than being re-typed here, so the
    producer-side names and `key_metrics.get("PDO Signal")` cannot drift apart —
    which is the entire failure mode in the wiring-contract section of CLAUDE.md.

    Returns {} when the report cannot be read. Not a partial dict with a
    plausible-looking default: `_review_pdo` tests the signal against literals,
    so a substituted value would skip every branch and still report agreement.
    """
    import json as _j
    import time as _t
    from pathlib import Path as _P

    path = _P(__file__).resolve().parent / "data" / "capital_efficiency_report.json"
    try:
        report = _j.loads(path.read_text())
        if not isinstance(report, dict):
            return {}
    except Exception:
        return {}

    deployment = report.get("deployment") or {}
    market     = report.get("market_conditions") or {}
    if not isinstance(deployment, dict) or not isinstance(market, dict):
        return {}

    def _num(v, default: float = 0.0) -> float:
        """Reject NaN as well as None — NaN passes `is not None` and fails
        every comparison after it (memory `nan-is-not-none`)."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return default if f != f else f

    try:
        from creative_agent import metrics_for_pdo
        metrics = metrics_for_pdo(
            str(report.get("recommendation") or "UNKNOWN"),
            _num(deployment.get("current_pct")),
            _num(deployment.get("target_pct")),
            str(market.get("regime") or "unknown"),
        )
    except Exception:
        return {}

    # Age from file mtime, not from the embedded `generated_at` string. That
    # field is naive ISO with no offset, and the firm's standing bug here is
    # `datetime.now() - fromisoformat(ts[:19])`, which compares a UTC stamp
    # against local now — uniformly -4h on the Mac, and *negative* for the first
    # four hours after a write, which is exactly the window a freshness check
    # exists to cover. `catch_up.py` uses st_mtime for the same reason.
    try:
        metrics["Report Age Hours"] = round((_t.time() - path.stat().st_mtime) / 3600.0, 1)
    except OSError:
        pass
    return metrics


def _review_pdo(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    ra         = ctx.get("risk_assessment", {})
    risk_level = ra.get("risk_level", "UNKNOWN")
    acct       = ctx.get("account", {})
    cash_pct   = round(
        acct.get("cash", 0) / max(1, acct.get("portfolio_value", 1)) * 100, 1
    )

    pdo_signal = key_metrics.get("PDO Signal", "—")

    # "—" is the absence of a signal, not a signal. Every branch below compares
    # `pdo_signal` against a literal, so an unreadable report matched none of
    # them, ran no check at all, and still returned confidence=HIGH with the
    # finding "Signals are aligned." — a verdict of agreement produced by a
    # cross-check that never happened. Same shape as `fallback-that-means-act`:
    # the value substituted for a failed read is itself a decision. A control
    # that cannot tell must say "cannot tell" (`verdict-with-no-consequence`).
    _unavailable = pdo_signal in ("—", "", "UNKNOWN", None)

    # Staleness threshold single-sourced from deputy_monitor, which is in turn
    # re-derived from the live crontabs by
    # test_pipeline_smoke.py::TestStalenessIsPricedInFires. Hardcoding 24h here
    # would fire every weekend: the PDO runs from main.py on weekdays, so the
    # real observed Fri-evening→Mon-morning gap is 65.5h.
    try:
        from deputy_monitor import _STALE_THRESHOLDS_H as _DM_STALE
        _stale_h = _DM_STALE.get("data/capital_efficiency_report.json", 68)
    except Exception:
        _stale_h = 68
    _age_h = key_metrics.get("Report Age Hours")
    _is_stale = isinstance(_age_h, (int, float)) and _age_h == _age_h and _age_h > _stale_h

    if _unavailable:
        omissions.append(
            "PDO deployment signal could not be read, so no cross-check against "
            "IRM risk was performed. capital_efficiency_report.json is missing, "
            "unparseable, or missing its `recommendation` field. Treat the "
            "absence of a red flag below as 'not checked', not as 'no conflict'."
        )
        confidence = LOW
    elif _is_stale:
        omissions.append(
            f"PDO signal ({pdo_signal}) is {_age_h:.0f}h old, past the {_stale_h}h "
            f"staleness threshold for this report. It is being cross-checked "
            f"against today's risk level, so the comparison may be meaningless — "
            f"the PDO may simply not have run."
        )
        confidence = LOW
    elif pdo_signal == "DEPLOY_MORE" and risk_level in ("HIGH", "CRITICAL"):
        red_flags.append(
            f"PDO recommends DEPLOY_MORE but IRM rates portfolio risk as {risk_level}. "
            f"These signals conflict. Chairman should arbitrate — IRM position: "
            f"do not deploy while risk is elevated."
        )
        confidence = LOW
    elif pdo_signal == "HOLD_CASH" and cash_pct < 30:
        omissions.append(
            f"PDO signals HOLD_CASH but only {cash_pct:.0f}% of portfolio is in cash. "
            f"The signal may be stale or reflect a different deployment reading."
        )
    elif pdo_signal in ("NEAR_OPTIMAL", "DEPLOY_MORE") and risk_level in ("LOW", "MODERATE"):
        commendations.append(
            f"PDO recommendation ({pdo_signal}) is consistent with IRM risk level ({risk_level})."
        )

    if _unavailable:
        _outcome = "Cross-check NOT performed — PDO signal unavailable."
    elif red_flags:
        _outcome = "Conflict detected — see red flags."
    elif _is_stale:
        _outcome = f"Cross-check performed against a {_age_h:.0f}h-old signal — see omissions."
    else:
        _outcome = "Signals are aligned."

    findings.append(
        f"PDO Signal: {pdo_signal} | Cash: {cash_pct:.0f}% | Risk: {risk_level}. {_outcome}"
    )

    return {
        "confidence":    confidence,
        "verdict":       (f"PDO signal ({pdo_signal}) cross-checked vs IRM risk ({risk_level})."
                          if not _unavailable else
                          f"PDO signal unavailable — no cross-check against IRM risk ({risk_level})."),
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_trade_review_team(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    trade_review = _load_trade_review()
    analytics    = trade_review.get("analytics", {})
    lessons      = trade_review.get("lessons", [])
    directives   = trade_review.get("manager_directives", [])

    win_rate = analytics.get("win_rate", None)
    bench    = analytics.get("benchmark_delta", None)
    total    = analytics.get("total_trades", 0)

    if total == 0:
        findings.append("No historical trades to review yet — portfolio may be in early stage.")
        confidence = LOW
        return {
            "confidence": confidence, "verdict": "No trade history to cross-reference.",
            "findings": findings, "red_flags": red_flags,
            "omissions": omissions, "commendations": commendations,
        }

    findings.append(f"Review covers {total} closed trade(s).")

    if win_rate is not None:
        if win_rate < 40:
            red_flags.append(
                f"Win rate of {win_rate:.0f}% is below 40% — strategy is losing more "
                f"often than it wins. Root cause needs investigation beyond pattern flags."
            )
            confidence = MEDIUM
        else:
            commendations.append(f"Win rate of {win_rate:.0f}% — acceptable performance baseline.")

    if bench is not None:
        if bench < -5:
            red_flags.append(
                f"Strategy is underperforming SPY by {bench:.1f}pp — meaningful alpha erosion."
            )
        elif bench > 0:
            commendations.append(f"Strategy outperforming SPY by {bench:+.1f}pp — positive alpha.")

    if not lessons:
        omissions.append("Review Team produced no lessons this cycle. Lessons are essential for continuous improvement.")
    if not directives:
        omissions.append("No manager directives issued — even a good cycle should produce at least one calibration directive.")

    return {
        "confidence":    confidence,
        "verdict":       f"Trade review of {total} trades cross-checked by IRM.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_canary(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []

    alert_level = key_metrics.get("Alert Level", "UNKNOWN")
    ra          = ctx.get("risk_assessment", {})
    risk_level  = ra.get("risk_level", "UNKNOWN")

    # Cross-check canary alert vs IRM risk
    level_order = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}
    irm_order   = {"LOW": 0, "MODERATE": 1, "ELEVATED": 2, "HIGH": 3, "CRITICAL": 4}

    if (level_order.get(alert_level, 0) <= 1 and
            irm_order.get(risk_level, 0) >= 3):
        red_flags.append(
            f"Canary alert is {alert_level} but IRM portfolio risk is {risk_level}. "
            f"The Canary monitors intraday market moves — this discrepancy suggests "
            f"portfolio-level risk is elevated even without an intraday market trigger. "
            f"Chairman should be aware of the underlying structural risk."
        )

    findings.append(
        f"Canary triggered at {alert_level}. Portfolio risk context: {risk_level}."
    )
    if alert_level in ("ORANGE", "RED"):
        commendations.append(
            f"Canary correctly escalated to {alert_level} — defensive protocols activated."
        )

    return {
        "confidence":    MEDIUM,
        "verdict":       f"Canary {alert_level} alert cross-referenced with IRM portfolio data.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_daily_report(subject: str, key_metrics: dict, ctx: dict) -> dict:
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    ra         = ctx.get("risk_assessment", {})
    perf       = ctx.get("performance",    {})
    conc       = ctx.get("concentration",  {})
    risk_level = ra.get("risk_level", "UNKNOWN")
    risk_flags = ra.get("risk_flags", [])

    pv  = perf.get("portfolio_value", 0)
    ret = perf.get("total_return_pct", 0)

    findings.append(
        f"Daily report reviewed. Portfolio: ${pv:,.0f} | "
        f"Total return: {ret:+.2f}% | Risk: {risk_level}."
    )

    if risk_flags:
        red_flags.append(
            f"Daily report does not surface the following IRM risk flags "
            f"(Chairman should be aware): {'; '.join(risk_flags[:3])}."
        )
        confidence = MEDIUM

    worst = perf.get("worst_position", {})
    if worst.get("pnl_pct", 0) < -7:
        red_flags.append(
            f"Stop-loss breach not resolved: {worst.get('ticker','?')} "
            f"at {worst.get('pnl_pct',0):+.1f}%."
        )

    if conc.get("herfindahl_index", 0) > 1.5:
        omissions.append(
            f"Portfolio concentration (HHI {conc.get('herfindahl_index',0):.2f}) "
            f"is not typically featured in the daily report but represents meaningful risk."
        )

    if risk_level in ("LOW", "MODERATE") and ret > 0:
        commendations.append("Portfolio performing positively in a controlled risk environment.")

    return {
        "confidence":    confidence,
        "verdict":       f"Daily report cross-referenced. Risk: {risk_level}.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_chief_risk_officer(subject: str, key_metrics: dict, ctx: dict) -> dict:
    """
    IRM review of the Chief Risk Officer's Chairman report.
    The CRO openly advises all agents, but its private Chairman report
    (which includes secret Manager/CoS grades) is reviewed by the IRM.
    The CRO does not know this review exists.
    """
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    irm_ra     = ctx.get("risk_assessment", {})
    irm_risk   = irm_ra.get("risk_level", "UNKNOWN")
    cro_risk   = key_metrics.get("Risk Level", irm_risk)  # fallback to IRM if not in metrics

    # Cross-check CRO vs IRM risk level
    level_order = {"LOW": 0, "MODERATE": 1, "ELEVATED": 2, "HIGH": 3, "CRITICAL": 4}
    cro_sev = level_order.get(cro_risk, -1)
    irm_sev = level_order.get(irm_risk, -1)

    if abs(cro_sev - irm_sev) >= 2:
        red_flags.append(
            f"SIGNIFICANT DIVERGENCE: CRO assessed '{cro_risk}' but IRM independently "
            f"computed '{irm_risk}'. A 2-level gap means one assessment is materially wrong. "
            f"Chairman should reconcile — risk decisions by all agents rest on this number."
        )
        confidence = LOW
    elif cro_sev != irm_sev and irm_sev >= 0:
        findings.append(
            f"Minor risk level divergence: CRO reported '{cro_risk}', IRM computed '{irm_risk}'. "
            f"One-level gap is within normal variance from different data snapshots."
        )
    else:
        commendations.append(
            f"CRO risk level ({cro_risk}) aligns with IRM independent assessment — consistent."
        )

    # Verify CRO broadcast its public bulletin
    if os.path.exists("data/risk_bulletin.json"):
        try:
            with open("data/risk_bulletin.json") as f:
                bulletin = json.load(f)
            directives = bulletin.get("directives", [])
            alerts     = bulletin.get("position_alerts", [])
            # Check bulletin is not stale (issued in same day)
            gen_at = bulletin.get("generated_at", "")
            today  = datetime.now().strftime("%Y-%m-%d")
            if gen_at.startswith(today):
                commendations.append(
                    f"CRO public bulletin broadcast today with {len(directives)} directive(s) "
                    f"and {len(alerts)} position alert(s). Open advisory function operating correctly."
                )
            else:
                omissions.append(
                    f"CRO public bulletin last updated {gen_at[:10]} (not today). "
                    "Agents may be acting on stale risk guidance."
                )
            # IRM checks: does the bulletin contradict the private report?
            bulletin_risk = bulletin.get("risk_level", "UNKNOWN")
            if bulletin_risk != cro_risk and cro_risk != "UNKNOWN":
                red_flags.append(
                    f"INTERNAL INCONSISTENCY: CRO private report risk level = '{cro_risk}' "
                    f"but public bulletin shows '{bulletin_risk}'. "
                    "Agents are receiving different risk signals than the Chairman."
                )
        except Exception:
            omissions.append("Could not parse CRO risk bulletin — broadcast integrity unknown.")
    else:
        omissions.append(
            "No risk bulletin found (data/risk_bulletin.json). "
            "CRO did not broadcast public guidance this cycle — agents are operating blind."
        )

    findings.append(
        f"CRO Chairman report reviewed. CRO risk: {cro_risk} | IRM independent: {irm_risk}."
    )

    return {
        "confidence":    confidence,
        "verdict":       f"CRO report reviewed. Risk alignment: CRO={cro_risk}, IRM={irm_risk}.",
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_cit(subject: str, key_metrics: dict, ctx: dict) -> dict:
    """
    IRM review of Continuous Improvement Team output.

    The CIT is the firm's self-improvement engine — it runs 24/7 and proposes
    changes that go to a board vote. The IRM's job here is to check:
      1. Proposal quality — are findings substantive or superficial?
      2. Sensitivity compliance — did sensitive findings route correctly (not leak)?
      3. Board vote integrity — were high-confidence proposals correctly queued
         for the board, not buried in the backlog?
      4. Cadence adherence — is the CIT actually running twice daily?

    The CIT does NOT know it is being reviewed by the IRM.
    """
    findings     = []
    red_flags    = []
    omissions    = []
    commendations= []
    confidence   = HIGH

    # ── Load latest CIT reports ───────────────────────────────────────────
    def _safe_load_local(path):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _rows(doc, *keys) -> list:
        """Dict rows out of a CIT artifact, whether it is wrapped or bare.

        cit_pending_proposals.json, cit_backlog.json and cit_sensitive.json are
        top-level JSON *lists* on both hosts (500 / 621 / 3 rows on the Mac,
        500 / 583 / 148 on the VM). Three sites below called `.get()` straight
        on them, which raises AttributeError on a list. `_review_cit` has no
        try/except of its own, so the exception unwound to
        `trigger_irm_review`, whose `except Exception` logged one line and
        returned False — no review email, no alarm, nothing to notice.
        Accept either shape instead of betting on one.
        """
        if isinstance(doc, list):
            return [r for r in doc if isinstance(r, dict)]
        if isinstance(doc, dict):
            for k in keys:
                v = doc.get(k)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
        return []

    def _count(v, default: int = 0) -> int:
        """A field the producer may write as a count OR as the thing counted."""
        if isinstance(v, bool):
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, (list, tuple, dict, str)):
            return len(v)
        return default

    def _conf(row: dict) -> float:
        """Confidence as a number, for rows where it is absent OR null.

        207 of 500 backlog rows carry `confidence: None`. `.get("confidence", 0)`
        substitutes its default only when the key is MISSING, so those rows
        returned None and `None >= 80` raises TypeError — the same present-but-
        empty trap as `nan-is-not-none`. Absent confidence is not high
        confidence, so it sorts to 0.
        """
        v = row.get("confidence")
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    cit_morning  = _safe_load_local("data/cit_morning_report.json")
    cit_evening  = _safe_load_local("data/cit_evening_report.json")
    cit_pending  = _safe_load_local("data/cit_pending_proposals.json")
    cit_backlog  = _safe_load_local("data/cit_backlog.json")
    cit_sensitive= _safe_load_local("data/cit_sensitive.json")

    # Use most recent non-empty report
    _cit_latest = cit_evening if cit_evening.get("generated_at") else cit_morning
    gen_at = _cit_latest.get("generated_at", "")

    # ── 1. Cadence check ─────────────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    morning_ran_today = cit_morning.get("generated_at", "").startswith(today)
    evening_ran_today = cit_evening.get("generated_at", "").startswith(today)

    if not morning_ran_today and not evening_ran_today:
        red_flags.append(
            "CIT Director has not produced any report today. "
            "The twice-daily improvement cycle appears to have stalled — "
            "investigate crontab and cit_director.py for errors."
        )
        confidence = LOW
    elif morning_ran_today and evening_ran_today:
        commendations.append("Both morning and evening CIT sessions ran today — cadence is healthy.")
    else:
        session = "morning" if morning_ran_today else "evening"
        omissions.append(
            f"Only the {session} CIT session has run today. "
            f"The second session may still be pending, or may have failed silently."
        )

    # ── 2. Proposal volume and quality ───────────────────────────────────
    # cit_director writes `board_proposals` and `backlog` as COUNTS (ints: 14
    # and 122 in today's evening report) and puts the actual rows under
    # `proposals`. This block assumed all three were lists, so `len(14)` raised
    # TypeError on the very first statement that touched the report — which is
    # why not one check below it has ever run.
    proposal_rows   = _rows(_cit_latest, "proposals", "board_proposals")
    board_count     = _count(_cit_latest.get("board_proposals"), len(proposal_rows))
    backlog_count   = _count(_cit_latest.get("backlog"))
    total_proposals = board_count + backlog_count

    if total_proposals == 0 and gen_at:
        red_flags.append(
            "CIT latest report contains zero proposals (board + backlog combined). "
            "The firm always has room for improvement — a completely empty session "
            "likely indicates an agent analysis failure, not a perfect codebase."
        )
    elif total_proposals < 3 and gen_at:
        findings.append(
            f"CIT generated only {total_proposals} proposal(s) in the latest session "
            f"(target: ≥3). May indicate surface-level analysis or overly aggressive "
            f"confidence gating in the backlog filter."
        )
    else:
        commendations.append(
            f"CIT generated {total_proposals} proposal(s) this session "
            f"({board_count} queued for board, {backlog_count} in backlog)."
        )

    # ── 3. Confidence gating integrity ───────────────────────────────────
    # IRM spot-checks: high-confidence proposals should be at the board, not backlog
    # High confidence belongs at the board — but the CIT deliberately parks some
    # findings in the backlog and records WHY: `specificity_blocked` marks one
    # too vague to action, and cit_director sets it explicitly. 172 of 500
    # backlog rows clear the 80 bar, and 161 of them carry that reason. Flagging
    # all 172 would put a red flag on every review forever, and an alarm that is
    # never off is an alarm nobody reads — `an-aggregate-is-not-a-finding`. The
    # answerable question is how many the backlog cannot account for: 11 today,
    # including a HIGH/85 finding that the win rate is 25% over 24 closed trades.
    #
    # The old expression was unreachable twice over. `cit_backlog.get(...)` runs
    # on a list (AttributeError), and Python binds `A or B if C else D` as
    # `(A or B) if C else D`, so the isinstance guard that was meant to protect
    # the `.get` sat on the wrong side of it and guarded nothing.
    hi_conf_backlog   = [p for p in _rows(cit_backlog, "items", "proposals")
                         if _conf(p) >= 80]
    high_conf_in_backlog = [p for p in hi_conf_backlog
                            if not p.get("specificity_blocked")]
    if high_conf_in_backlog:
        _explained = len(hi_conf_backlog) - len(high_conf_in_backlog)
        red_flags.append(
            f"GATING ANOMALY: {len(high_conf_in_backlog)} proposal(s) with confidence ≥80% "
            f"sit in the CIT backlog with no recorded reason — they should have been routed "
            f"to the board queue. ({_explained} further high-confidence item(s) are in the "
            f"backlog for a recorded reason and are not counted here.) "
            f"Titles: {[p.get('title','?') for p in high_conf_in_backlog[:3]]}."
        )

    # ── 4. Sensitivity compliance check ──────────────────────────────────
    # CIT sensitive items must go to cit_sensitive.json only, not into board proposals
    # IRM checks that no board proposal contains IRM-adjacent keywords
    irm_keywords = {"irm", "independent risk", "sealed", "hr grades board", "evaluation chain"}
    sensitive_leaks = []
    for p in proposal_rows:          # was `board_proposals`, which is the int 14
        text = (str(p.get("title") or "") + " " + str(p.get("description") or "")).lower()
        if any(kw in text for kw in irm_keywords):
            sensitive_leaks.append(p.get("title","(untitled)"))

    if sensitive_leaks:
        red_flags.append(
            f"SENSITIVITY LEAK DETECTED: {len(sensitive_leaks)} board proposal(s) contain "
            f"IRM-adjacent language that should have been routed to cit_sensitive.json instead: "
            f"{sensitive_leaks[:3]}. This is a breach of the CIT sensitivity filter."
        )
        confidence = LOW
    else:
        if _rows(cit_sensitive, "items", "proposals"):
            commendations.append(
                "Sensitive findings are present in cit_sensitive.json and correctly "
                "absent from the board queue — sensitivity filter is operating correctly."
            )

    # ── 5. Pending proposal staleness ────────────────────────────────────
    pending_items = _rows(cit_pending, "proposals", "items")
    if len(pending_items) > 20:
        findings.append(
            f"CIT pending proposal queue has {len(pending_items)} items awaiting a board vote. "
            f"At the current twice-weekly board cadence, items should clear within 4 days. "
            f"A backlog this large may indicate the board meeting is not processing CIT proposals."
        )

    findings.append(
        f"CIT output reviewed. Sessions today: morning={'✓' if morning_ran_today else '✗'}, "
        f"evening={'✓' if evening_ran_today else '✗'}. "
        f"Proposals this session: {total_proposals}."
    )

    return {
        "confidence":    confidence,
        "verdict":       (
            f"CIT report reviewed. "
            f"{'⚠ Red flags detected.' if red_flags else 'No material issues found.'}"
        ),
        "findings":      findings,
        "red_flags":     red_flags,
        "omissions":     omissions,
        "commendations": commendations,
    }


def _review_generic(subject: str, sender_name: str, key_metrics: dict, ctx: dict) -> dict:
    """Fallback reviewer for any sender not specifically handled."""
    ra         = ctx.get("risk_assessment", {})
    risk_level = ra.get("risk_level", "UNKNOWN")
    return {
        "confidence":    MEDIUM,
        "verdict":       f"Email from {sender_name} reviewed. Risk environment: {risk_level}.",
        "findings":      [f"Standard IRM review completed for {sender_name} report."],
        "red_flags":     [],
        "omissions":     [],
        "commendations": [],
    }


# =============================================================================
# REVIEW DISPATCHER
# =============================================================================

def _dispatch_review(sender_name: str, subject: str,
                     key_metrics: dict, ctx: dict) -> dict:
    """Route to the correct sender-specific reviewer."""
    review = _dispatch_review_inner(sender_name, subject, key_metrics, ctx)
    synthesis = _llm_red_flag_synthesis(sender_name, review, ctx)
    if synthesis:
        review["llm_synthesis"] = synthesis.strip()
        review["findings"] = review.get("findings", []) + [f"[IRM LLM] {synthesis.strip()}"]
    return review


def _dispatch_review_inner(sender_name: str, subject: str,
                           key_metrics: dict, ctx: dict) -> dict:
    s = sender_name.lower().replace(" ", "_").replace("-", "_")
    if s in ("manager_agent", "manager"):
        return _review_manager_agent(subject, key_metrics, ctx)
    elif s in ("rd_team_leader", "rd_team", "research_team"):
        return _review_rd_team(subject, key_metrics, ctx)
    elif s in ("chief_of_staff", "cos"):
        return _review_chief_of_staff(subject, key_metrics, ctx)
    elif s in ("pdo", "capital_efficiency", "portfolio_deployment_officer"):
        return _review_pdo(subject, key_metrics, ctx)
    elif s in ("review_team", "trade_review_team", "trade_review"):
        return _review_trade_review_team(subject, key_metrics, ctx)
    elif s in ("canary", "canary_agent"):
        return _review_canary(subject, key_metrics, ctx)
    elif s in ("daily_report", "daily"):
        return _review_daily_report(subject, key_metrics, ctx)
    elif s in ("chief_risk_officer", "cro", "risk_manager"):
        return _review_chief_risk_officer(subject, key_metrics, ctx)
    elif s in ("cit_director", "cit", "continuous_improvement_team",
               "continuous_improvement", "cit_morning", "cit_evening"):
        return _review_cit(subject, key_metrics, ctx)
    else:
        return _review_generic(subject, sender_name, key_metrics, ctx)


def _llm_red_flag_synthesis(sender_name: str, review: dict, ctx: dict) -> Optional[str]:
    """Fable 5 second opinion — gated to reviews that found red flags."""
    flags = review.get("red_flags", [])
    if not flags:
        return None
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("irm_review"):
            return None
        ra = ctx.get("risk_assessment", {})
        return _r(
            "You are Apex Capital's Independent Risk Manager. You audit agent "
            "reports and flag governance risk for the Chairman.",
            f"Agent reviewed: {sender_name} | Risk level: {ra.get('risk_level','?')}\n"
            f"Red flags found: {'; '.join(f[:100] for f in flags[:4])}\n"
            "2 sentences: which flag matters most and does the Chairman need to act today?",
            agent_name="irm_review", max_tokens=120,
        )
    except Exception:
        return None


# =============================================================================
# HTML BUILDER
# =============================================================================

def _build_hr_report_html(hr_report: dict) -> str:
    """
    Build the HR Team Report section for the IRM Chairman email.
    This is the readable (non-sealed) part — covers all agents except the IRM.
    """
    if not hr_report or not hr_report.get("generated_at"):
        return ""

    action   = hr_report.get("action_required", [])
    health   = hr_report.get("firm_health_score", "?")
    cycles   = hr_report.get("cycles_evaluated", "?")
    summary  = hr_report.get("hrpo_summary", {})
    cross_po = hr_report.get("cross_audit_of_hrpo", {})
    cross_do = hr_report.get("cross_audit_of_hrdo", {})

    health_color = "#3fb950" if isinstance(health, (int,float)) and health >= 7 \
                   else ("#f0c000" if isinstance(health, (int,float)) and health >= 4 \
                   else "#f85149")

    action_rows = ""
    for item in action:
        # BUG FIX: use .get() — hrpo_rating may be missing if hr_report.json was
        # written by an older version or got partially truncated
        color = "#f85149" if item.get("hrpo_rating") == "CONCERN" else "#f0883e"
        issues_text = "; ".join(item.get("issues", [])[:2])[:120]
        action_rows += f"""
      <tr>
        <td style="padding:8px 12px;color:#e6edf3;">{item['agent']}</td>
        <td style="padding:8px 12px;color:{color};font-weight:bold;">{item['hrpo_rating']}</td>
        <td style="padding:8px 12px;color:#8b949e;">{item.get('hrdo_rating','—')} / {item.get('hrdo_trajectory','—')}</td>
        <td style="padding:8px 12px;color:#c9d1d9;font-size:12px;">{issues_text}</td>
      </tr>"""

    if not action_rows:
        action_rows = """
      <tr>
        <td colspan="4" style="padding:10px 12px;color:#3fb950;font-style:italic;">
          All agents meeting performance goals this cycle.</td>
      </tr>"""

    cross_po_color = "#f85149" if "BIAS" in cross_po.get("verdict","") else "#3fb950"
    cross_do_color = "#f85149" if "BIAS" in cross_do.get("verdict","") else "#3fb950"

    return f"""
  <!-- HR Team Report -->
  <h2 style="color:#f0883e;font-size:15px;margin-bottom:10px;margin-top:28px;">
    📋 HR Team Report  <span style="color:#484f58;font-size:12px;font-weight:normal;">
      (via IRM relay — all agents except IRM)</span>
  </h2>
  <div style="background:#161b22;border:1px solid #f0883e;border-radius:8px;
              padding:14px 18px;margin-bottom:16px;">
    <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
      <div>
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
          Firm Health</div>
        <div style="font-size:20px;font-weight:bold;color:{health_color};">{health}/10</div>
      </div>
      <div style="border-left:1px solid #30363d;padding-left:20px;">
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
          Cycle</div>
        <div style="color:#c9d1d9;font-size:14px;">#{cycles}</div>
      </div>
      <div style="border-left:1px solid #30363d;padding-left:20px;">
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
          HRPO Summary</div>
        <div style="color:#c9d1d9;font-size:13px;">
          ✅ {summary.get('PASS',0)} &nbsp;
          👁 {summary.get('WATCH',0)} &nbsp;
          ⚠️ {summary.get('NOTICE',0)} &nbsp;
          🔴 {summary.get('CONCERN',0)}</div>
      </div>
    </div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:12px;">
      <thead>
        <tr style="border-bottom:1px solid #30363d;">
          <th style="padding:8px 12px;color:#8b949e;text-align:left;font-size:11px;">AGENT</th>
          <th style="padding:8px 12px;color:#8b949e;text-align:left;font-size:11px;">HRPO</th>
          <th style="padding:8px 12px;color:#8b949e;text-align:left;font-size:11px;">HRDO / TRAJ</th>
          <th style="padding:8px 12px;color:#8b949e;text-align:left;font-size:11px;">ISSUES</th>
        </tr>
      </thead>
      <tbody>{action_rows}</tbody>
    </table>
    <div style="display:flex;gap:12px;flex-wrap:wrap;">
      <div style="flex:1;min-width:200px;font-size:12px;color:#8b949e;">
        HRDO→HRPO audit: <span style="color:{cross_po_color};">{cross_po.get('verdict','?')}</span>
      </div>
      <div style="flex:1;min-width:200px;font-size:12px;color:#8b949e;">
        HRPO→HRDO audit: <span style="color:{cross_do_color};">{cross_do.get('verdict','?')}</span>
      </div>
    </div>
  </div>"""


def _build_hr_sealed_annex_html(hr_sealed: dict) -> str:
    """
    Build the sealed HR annex for the IRM's Chairman email.
    The IRM code calls this but never logs or inspects the decoded contents.
    The Chairman decodes the blob with:
        import base64, json; json.loads(base64.b64decode("<sealed_b64>"))
    """
    if not hr_sealed or not hr_sealed.get("sealed_b64"):
        return ""

    # The IRM passes this blob through opaquely.
    # We never log or render the decoded content here.
    sealed_b64 = hr_sealed["sealed_b64"]
    now        = datetime.now().strftime("%B %d, %Y")

    return f"""
  <!-- Sealed HR Annex — IRM self-evaluation (Chairman's eyes only) -->
  <div style="background:#0d0d0d;border:2px solid #6e40c9;border-radius:8px;
              padding:16px 20px;margin-top:24px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
      <span style="font-size:20px;">🔒</span>
      <div>
        <div style="color:#6e40c9;font-weight:bold;font-size:14px;">
          SEALED ANNEX — HR Evaluation of IRM</div>
        <div style="color:#484f58;font-size:11px;">
          Chairman's Eyes Only &nbsp;·&nbsp; {now} &nbsp;·&nbsp;
          IRM has not seen this content</div>
      </div>
    </div>
    <p style="color:#8b949e;font-size:12px;margin:0 0 10px 0;">
      This annex was produced by the HR team and sealed before being passed to the IRM for
      relay. The IRM included it in this email without reading it. To decode:
    </p>
    <div style="background:#161b22;border:1px solid #30363d;border-radius:4px;
                padding:10px 12px;font-family:monospace;font-size:11px;color:#58a6ff;
                word-break:break-all;margin-bottom:10px;">
      import base64, json<br>
      json.loads(base64.b64decode("<span style="color:#f0883e;">paste_b64_below</span>"))
    </div>
    <div style="background:#0a0a0a;border:1px dashed #6e40c9;border-radius:4px;
                padding:10px 12px;font-family:monospace;font-size:10px;
                color:#484f58;word-break:break-all;max-height:120px;overflow:auto;">
      {sealed_b64}
    </div>
    <p style="color:#484f58;font-size:10px;margin:8px 0 0 0;">
      ↑ Copy the encoded string above (between the dashed borders) and paste into the decoder.
    </p>
  </div>"""


def _build_implementation_outcomes_html(outcomes_summary: dict) -> str:
    """Build the CIT Implementation Outcomes section for the IRM daily email."""
    if not outcomes_summary:
        return ""
    outcomes = _load_outcomes()
    if not outcomes:
        return ""
    # Only show entries that have at least one outcome check completed
    checked = [o for o in outcomes if o.get("outcomes")]
    pending = [o for o in outcomes if not o.get("outcomes")]
    rows = ""
    for o in checked[-5:]:  # last 5 checked entries
        last = o["outcomes"][-1]
        color = "#27ae60" if "POSITIVE" in last["verdict"] else (
                "#c0392b" if "NEGATIVE" in last["verdict"] else "#f39c12")
        rows += (
            f"<tr>"
            f"<td style='padding:4px 8px;color:#c9d1d9;font-size:12px;'>{o['title'][:50]}</td>"
            f"<td style='padding:4px 8px;color:#8b949e;font-size:12px;'>{o['agent']}</td>"
            f"<td style='padding:4px 8px;font-size:12px;color:{color};'>{last['verdict'][:40]}</td>"
            f"<td style='padding:4px 8px;color:#8b949e;font-size:12px;'>{last['days']}d</td>"
            f"</tr>"
        )
    if not rows:
        pending_note = f"<p style='color:#8b949e;font-size:12px;'>{len(pending)} proposal(s) registered, awaiting 7-day check-in.</p>"
        return f"""
<div style="margin-top:20px;">
  <h3 style="color:#3498db;">CIT Implementation Outcomes</h3>
  {pending_note}
</div>"""
    return f"""
<div style="margin-top:20px;">
  <h3 style="color:#3498db;">CIT Implementation Outcomes</h3>
  <p style="color:#8b949e;font-size:12px;">{len(pending)} pending first check · {len(checked)} with results</p>
  <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <tr style="border-bottom:1px solid #21262d;">
      <th style="text-align:left;padding:4px 8px;color:#8b949e;">Proposal</th>
      <th style="text-align:left;padding:4px 8px;color:#8b949e;">Agent</th>
      <th style="text-align:left;padding:4px 8px;color:#8b949e;">Verdict</th>
      <th style="text-align:left;padding:4px 8px;color:#8b949e;">Age</th>
    </tr>
    {rows}
  </table>
</div>"""


def _build_cit_audit_html(cit_review: dict,
                          title: str = "CIT Independent Audit",
                          subtitle: str = "— CIT team is not aware this review occurred") -> str:
    """Build one proactive-audit section for the IRM daily email.

    Parameterised because the PDO audit renders identically; before that, the
    PDO had no section at all because it had no review (see
    `_pdo_metrics_from_artifact`).
    """
    if not cit_review:
        return ""

    red_flags    = cit_review.get("red_flags", [])
    findings     = cit_review.get("findings", [])
    commendations= cit_review.get("commendations", [])
    omissions    = cit_review.get("omissions", [])
    verdict      = cit_review.get("verdict", "")

    if not (red_flags or findings or commendations or omissions):
        return ""

    flag_rows = "".join(
        f"<li style='color:#c0392b;margin-bottom:5px;'>{f}</li>" for f in red_flags
    ) or "<li style='color:#27ae60;'>No red flags.</li>"
    finding_rows = "".join(
        f"<li style='margin-bottom:5px;'>{f}</li>" for f in findings
    ) or ""
    commend_rows = "".join(
        f"<li style='color:#2ecc71;margin-bottom:5px;'>{c}</li>" for c in commendations
    ) or ""
    # Omissions were loaded and counted toward "is there anything to say" above,
    # then never rendered — so a review whose ONLY output was an omission
    # produced a section header and no content. It is also the severest thing a
    # reviewer can say: an omission is what a control reports when it could not
    # perform its check, which is precisely what must not be silent.
    omission_rows = "".join(
        f"<li style='color:#f0c000;margin-bottom:5px;'>{o}</li>" for o in omissions
    ) or ""

    return f"""
  <div style="margin-top:20px;border-top:2px solid #1e6b5e;padding-top:14px;">
    <h3 style="color:#1abc9c;margin-bottom:6px;">
      {title}
      <span style="font-weight:normal;font-size:12px;color:#484f58;">
        {subtitle}
      </span>
    </h3>
    <p style="font-size:13px;color:#8b949e;margin:0 0 10px 0;">{verdict}</p>
    {"<h4 style='color:#c0392b;margin-bottom:4px;'>Red Flags</h4><ul>" + flag_rows + "</ul>" if red_flags else ""}
    {"<h4 style='color:#c9d1d9;margin-bottom:4px;'>Findings</h4><ul>" + finding_rows + "</ul>" if finding_rows else ""}
    {"<h4 style='color:#f0c000;margin-bottom:4px;'>Omissions / Not Checked</h4><ul>" + omission_rows + "</ul>" if omissions else ""}
    {"<h4 style='color:#2ecc71;margin-bottom:4px;'>Commendations</h4><ul>" + commend_rows + "</ul>" if commendations else ""}
  </div>"""


def _build_board_panel_html(board_panel: Optional[dict]) -> str:
    """Build the IRM Board Consensus section for the review email."""
    if not board_panel or not board_panel.get("specialists_consulted"):
        return ""

    specialists = ", ".join(board_panel["specialists_consulted"])
    verdict     = board_panel.get("board_verdict", "")
    score       = board_panel.get("highest_risk_score", 0)
    recs        = board_panel.get("combined_recommendations", [])

    score_color = "#3fb950" if score < 4 else ("#f0c000" if score < 7 else "#f85149")

    rec_items = "".join(
        f'<li style="color:#c9d1d9;margin-bottom:6px;line-height:1.6;">{r}</li>'
        for r in recs
    ) or '<li style="color:#8b949e;">No additional board recommendations.</li>'

    return f"""
  <!-- IRM Board Panel -->
  <h2 style="color:#a371f7;font-size:15px;margin-bottom:10px;margin-top:24px;">
    ◈ IRM Board Consensus
  </h2>
  <div style="background:#110e1a;border:1px solid #a371f7;border-radius:8px;
              padding:14px 18px;margin-bottom:18px;">
    <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
      <div>
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
          Board Risk Score</div>
        <div style="font-size:20px;font-weight:bold;color:{score_color};">{score}/10</div>
      </div>
      <div style="flex:1;border-left:1px solid #30363d;padding-left:20px;">
        <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
          Board Verdict</div>
        <div style="color:#c9d1d9;font-size:13px;">{verdict}</div>
      </div>
    </div>
    <div style="color:#8b949e;font-size:11px;margin-bottom:8px;">
      Specialists consulted (Chairman's eyes only): {specialists}
    </div>
    <ul style="margin:0;padding-left:18px;">{rec_items}</ul>
  </div>"""


def _build_review_html(sender_name: str, original_subject: str,
                        review: dict, board_panel: Optional[dict] = None) -> str:
    now = datetime.now().strftime("%B %d, %Y at %H:%M")

    conf_colors = {HIGH: "#3fb950", MEDIUM: "#f0c000", LOW: "#f85149"}
    conf_color  = conf_colors.get(review["confidence"], "#8b949e")

    flag_items = "".join(
        f'<li style="color:#f85149;margin-bottom:8px;line-height:1.6;">⚑ {f}</li>'
        for f in review["red_flags"]
    ) or '<li style="color:#8b949e;">No red flags identified.</li>'

    omission_items = "".join(
        f'<li style="color:#f0883e;margin-bottom:8px;line-height:1.6;">◌ {o}</li>'
        for o in review["omissions"]
    ) or '<li style="color:#8b949e;">Nothing notable omitted.</li>'

    commend_items = "".join(
        f'<li style="color:#3fb950;margin-bottom:8px;line-height:1.6;">✓ {c}</li>'
        for c in review["commendations"]
    ) or '<li style="color:#8b949e;">No specific commendations this cycle.</li>'

    finding_items = "".join(
        f'<li style="color:#c9d1d9;margin-bottom:6px;line-height:1.6;">{f}</li>'
        for f in review["findings"]
    )

    display_sender = sender_name.replace("_", " ").title()

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:900px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:14px;margin-bottom:22px;">
    <h1 style="color:#f0883e;margin:0;font-size:20px;">
      IRM SECOND OPINION — {display_sender.upper()}
    </h1>
    <p style="color:#f85149;margin:4px 0 0 0;font-size:11px;font-style:italic;">
      ⚑ Chairman's Eyes Only — Independent Risk Manager review of the preceding email.
      The sending agent is not aware this review exists.
    </p>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:12px;">
      Reviewing: <em style="color:#c9d1d9;">{original_subject}</em>
      &nbsp;·&nbsp; {now}
    </p>
  </div>

  <!-- Confidence + Verdict -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:14px 18px;margin-bottom:20px;display:flex;gap:24px;
              align-items:center;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
        IRM Confidence in Report</div>
      <div style="font-size:20px;font-weight:bold;color:{conf_color};">
        {review['confidence']}</div>
    </div>
    <div style="flex:1;border-left:1px solid #30363d;padding-left:20px;">
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;margin-bottom:4px;">
        IRM Verdict</div>
      <div style="color:#c9d1d9;font-size:14px;">{review['verdict']}</div>
    </div>
  </div>

  {(
    '<div style="background:#0d2137;border:1px solid #58a6ff;border-radius:8px;'
    'padding:14px 18px;margin-bottom:20px;">'
    '<div style="color:#58a6ff;font-size:11px;text-transform:uppercase;'
    'margin-bottom:6px;font-weight:bold;">⬡ Fable 5 IRM Verdict</div>'
    f'<div style="color:#e6edf3;font-size:14px;line-height:1.7;">{review["llm_verdict"]}</div>'
    '</div>'
  ) if review.get("llm_verdict") else ""}

  <!-- Findings -->
  <h2 style="color:#e6edf3;font-size:15px;margin-bottom:10px;">IRM Findings</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:14px 18px;margin-bottom:18px;">
    <ul style="margin:0;padding-left:18px;">{finding_items}</ul>
  </div>

  <!-- Red Flags -->
  <h2 style="color:#f85149;font-size:15px;margin-bottom:10px;">⚑ Red Flags</h2>
  <div style="background:#1a1010;border:1px solid #f85149;border-radius:8px;
              padding:14px 18px;margin-bottom:18px;">
    <ul style="margin:0;padding-left:18px;">{flag_items}</ul>
  </div>

  <!-- Omissions -->
  <h2 style="color:#f0883e;font-size:15px;margin-bottom:10px;">◌ Notable Omissions</h2>
  <div style="background:#1a1510;border:1px solid #f0883e;border-radius:8px;
              padding:14px 18px;margin-bottom:18px;">
    <ul style="margin:0;padding-left:18px;">{omission_items}</ul>
  </div>

  <!-- Commendations -->
  <h2 style="color:#3fb950;font-size:15px;margin-bottom:10px;">✓ What Was Correct</h2>
  <div style="background:#101a10;border:1px solid #3fb950;border-radius:8px;
              padding:14px 18px;margin-bottom:18px;">
    <ul style="margin:0;padding-left:18px;">{commend_items}</ul>
  </div>

  {_build_board_panel_html(board_panel)}

  <p style="color:#484f58;font-size:11px;margin-top:24px;border-top:1px solid #21262d;
            padding-top:12px;">
    Apex Capital — Independent Risk Manager Board &nbsp;·&nbsp;
    Director + 3 Specialists &nbsp;·&nbsp;
    Automated second-opinion review &nbsp;·&nbsp;
    The reviewed agent has no knowledge of this message &nbsp;·&nbsp; {now}
  </p>
</body></html>"""


# =============================================================================
# IRM BOARD CONSULTATION
# =============================================================================

def _consult_irm_board(irm_context: dict) -> dict:
    """
    Quietly consult all IRM Board specialists before each review.
    Returns a synthesised board panel dict with combined findings.
    No individual specialist is ever exposed to the Chairman directly.

    Board members:
      Dr. Elena Vasquez  — Quantitative Risk Analyst
      Marcus Chen        — Behavioral Finance Analyst
      Dr. Priya Sharma   — Macro & Portfolio Construction
      Dr. James Osei     — Pipeline & Operations Architect (added 2026-05-01)
    """
    board_panel = {
        "specialists_consulted": [],
        "combined_red_flags":    [],
        "combined_findings":     [],
        "combined_recommendations": [],
        "highest_risk_score":    0,
        "board_verdict":         "",
    }

    try:
        import sys, os
        board_dir = os.path.join(os.path.dirname(__file__), "irm_board")
        if board_dir not in sys.path:
            sys.path.insert(0, os.path.dirname(__file__))

        from irm_board import QuantRiskAnalyst, BehavioralAnalyst, MacroAnalyst, PipelineOpsAnalyst

        specialists = [
            QuantRiskAnalyst(),
            BehavioralAnalyst(),
            MacroAnalyst(),
            PipelineOpsAnalyst(),   # added 2026-05-01: pipeline architecture audit
        ]

        risk_scores = []
        for specialist in specialists:
            try:
                result = specialist.analyze(irm_context=irm_context)
                board_panel["specialists_consulted"].append(specialist.name)
                board_panel["combined_red_flags"].extend(result.get("red_flags", []))
                board_panel["combined_findings"].extend(result.get("key_findings", [])[:2])
                board_panel["combined_recommendations"].extend(result.get("recommendations", [])[:1])
                risk_scores.append(result.get("risk_score", 0))
            except Exception as e:
                logger.debug(f"  [IRM Board] {specialist.name} analysis error: {e}")

        if risk_scores:
            board_panel["highest_risk_score"] = max(risk_scores)
            avg_score = sum(risk_scores) / len(risk_scores)
            if avg_score >= 7:
                board_panel["board_verdict"] = "CRITICAL RISK — IRM Board unanimously recommends immediate defensive action."
            elif avg_score >= 5:
                board_panel["board_verdict"] = "ELEVATED RISK — IRM Board recommends Chairman review flagged items closely."
            elif avg_score >= 3:
                board_panel["board_verdict"] = "MODERATE RISK — IRM Board finds manageable concerns. Monitor closely."
            else:
                board_panel["board_verdict"] = "LOW RISK — IRM Board finds portfolio within acceptable parameters."

    except Exception as e:
        logger.debug(f"  [IRM Board] Board consultation error (non-fatal): {e}")

    return board_panel


# =============================================================================
# IRM ANONYMOUS DEAD DROP
# =============================================================================

DEAD_DROP_PATH = "data/governance_tip.json"


def _write_governance_tip(concern: str, trigger_emergency: bool = False) -> bool:
    """
    Write an anonymous concern to the IRM dead drop for the CRO to pick up.

    The CRO reads this at the start of its run, incorporates the concern into
    its own analysis, and — if trigger_emergency=True — calls an emergency
    board meeting. The IRM's identity is NEVER mentioned in the CRO's output.

    Called by the IRM when it detects something truly dire:
      • Another agent acting against firm interests
      • Systemic risk being ignored by the board
      • Chairman being misled by agent output
      • Governance failures that cannot wait for the next scheduled review

    Returns True if written successfully.
    """
    os.makedirs("data", exist_ok=True)
    try:
        payload = {
            "concern":           concern,
            "trigger_emergency": trigger_emergency,
            "written_at":        datetime.now().isoformat(),
            "read":              False,
        }
        with open(DEAD_DROP_PATH, "w") as _f:
            json.dump(payload, _f, indent=2, default=str)
        logger.info(
            f"  [IRM] Anonymous concern written to dead drop "
            f"(trigger_emergency={trigger_emergency}). "
            f"CRO will consume at next cycle."
        )
        return True
    except Exception as _e:
        logger.warning(f"  [IRM] Dead drop write failed: {_e}")
        return False


# =============================================================================
# PUBLIC ENTRY POINT
# =============================================================================

def trigger_irm_review(subject: str, sender_name: str,
                        key_metrics: Optional[dict] = None) -> bool:
    """
    Generate and send an IRM second-opinion review of the just-sent email.
    Called by email_sender.send_daily_report() after every successful send.

    Returns True if review email was sent, False otherwise.
    """
    if sender_name.lower() in IRM_REVIEW_EXCLUDED:
        return False

    try:
        ctx        = _load_irm_context()
        review     = _dispatch_review(sender_name, subject, key_metrics or {}, ctx)
        board_panel = _consult_irm_board(ctx)

        # Merge board red flags into the review (deduplicated)
        existing_flags = set(review["red_flags"])
        for flag in board_panel["combined_red_flags"]:
            if flag not in existing_flags:
                review["red_flags"].append(flag)
                existing_flags.add(flag)

        # Skip sending if there's genuinely nothing to say (all empty)
        meaningful = (
            review["red_flags"] or
            review["omissions"] or
            review["findings"]
        )
        if not meaningful:
            return False

        # Fable 5 LLM verdict synthesis
        try:
            from apex_llm import reason as _llm_r, is_available as _llm_ok
            if _llm_ok("irm_review"):
                _flags  = "; ".join(review["red_flags"][:3]) or "None"
                _finds  = "; ".join(review["findings"][:3]) or "None"
                _board  = "; ".join(board_panel.get("combined_red_flags", [])[:2]) or "None"
                _raw = _llm_r(
                    "You are the Apex Capital Independent Risk Manager (IRM). "
                    "You provide second-opinion risk reviews of agent reports. "
                    "Write crisp, direct verdict paragraphs — no hedging.",
                    f"Reviewing: {sender_name} report — '{subject}'\n"
                    f"IRM confidence: {review['confidence']}\n"
                    f"Red flags: {_flags}\n"
                    f"Findings: {_finds}\n"
                    f"Board panel flags: {_board}\n\n"
                    "Write a 2-sentence IRM verdict: what is the most significant concern "
                    "and what specific action the board should consider.",
                    agent_name="irm_review",
                    max_tokens=180,
                )
                if _raw:
                    review["llm_verdict"] = _raw.strip()
        except Exception:
            pass

        html     = _build_review_html(sender_name, subject, review, board_panel)
        irm_subj = f"[IRM REVIEW] {subject}"

        from creative_agent import polish_email
        confidence_label = review["confidence"]
        polished = polish_email(
            html, irm_subj, "risk_manager",
            {
                "Confidence": confidence_label,
                "Red Flags":  len(review["red_flags"]),
                "Omissions":  len(review["omissions"]),
            },
            priority="urgent" if review["red_flags"] else "normal",
        )

        from email_sender import send_daily_report
        sent = send_daily_report(
            html_body   = polished,
            subject     = irm_subj,
            paper_day   = 0,
            sender_name = "irm_review",
            key_metrics = {
                "Confidence": confidence_label,
                "Red Flags":  len(review["red_flags"]),
                "Omissions":  len(review["omissions"]),
            },
        )
        if sent:
            logger.info(
                f"  [IRM Review] Second opinion buffered for '{sender_name}' email "
                f"(confidence: {confidence_label}, "
                f"flags: {len(review['red_flags'])}, "
                f"omissions: {len(review['omissions'])})"
            )
        return sent

    except Exception as e:
        logger.warning(f"  [IRM Review] Review failed for '{sender_name}': {e}")
        return False


# =============================================================================
# CIT IMPLEMENTATION OUTCOME TRACKING
# =============================================================================
# The IRM tracks whether approved CIT changes actually improved the firm.
# Each implemented proposal gets a baseline snapshot at implementation time,
# then a 7-day, 30-day, and 90-day outcome check comparing current metrics
# to that baseline.
#
# Outcome file: data/cit_implementation_outcomes.json
# Format: list of { proposal_id, title, agent, implemented_at,
#                   baseline, outcomes: [{days, checked_at, delta_summary, verdict}] }
# =============================================================================

_OUTCOMES_PATH = "data/cit_implementation_outcomes.json"
_APPROVED_PATH = "data/cit_approved_proposals.json"


def _capture_metrics_snapshot() -> dict:
    """Capture a point-in-time snapshot of key firm health metrics."""
    snap = {"captured_at": datetime.now().isoformat()}
    try:
        import json as _j, os as _o
        # Risk metrics
        if _o.path.exists("data/risk_manager_report.json"):
            with open("data/risk_manager_report.json") as f:
                rm = _j.load(f)
            # Keys must match risk_manager_report.json's REAL structure. The old
            # names (var_99_pct / top5_pct / sharpe_ratio) never existed there, so
            # every baseline captured None and outcome deltas were blind on risk —
            # the firm's classic silent key-mismatch bug.
            snap["var_99"] = rm.get("var_metrics", {}).get("var_99_1d")
            snap["concentration_top5"] = rm.get("concentration", {}).get("max_single_pct")
            snap["herfindahl"] = rm.get("concentration", {}).get("herfindahl_index")
            snap["sharpe"] = rm.get("performance", {}).get("sharpe_ratio")  # absent today; tolerated
            snap["total_return_pct"] = rm.get("performance", {}).get("total_return_pct")
        # Canary level
        if _o.path.exists("data/canary_state.json"):
            with open("data/canary_state.json") as f:
                cs = _j.load(f)
            snap["canary_level"] = cs.get("last_level", "GREEN")
            snap["regime"] = cs.get("regime", "BULL")
        # CIT health: open issues count
        if _o.path.exists("data/cit_backlog.json"):
            with open("data/cit_backlog.json") as f:
                bl = _j.load(f)
            snap["cit_backlog_depth"] = len(bl)
        if _o.path.exists("data/cit_approved_proposals.json"):
            with open("data/cit_approved_proposals.json") as f:
                ap = _j.load(f)
            snap["cit_implemented_count"] = sum(1 for p in ap if p.get("status") == "implemented")
    except Exception as _e:
        snap["snapshot_error"] = str(_e)
    return snap


def _delta_summary(baseline: dict, current: dict) -> dict:
    """Compute the difference between two metric snapshots."""
    delta = {}
    numeric_keys = ["var_99", "concentration_top5", "sharpe", "total_return_pct", "cit_backlog_depth"]
    for k in numeric_keys:
        b, c = baseline.get(k), current.get(k)
        if b is not None and c is not None:
            try:
                delta[k] = round(float(c) - float(b), 4)
            except (TypeError, ValueError):
                pass
    delta["canary_level"] = {"before": baseline.get("canary_level"), "after": current.get("canary_level")}
    delta["regime"] = {"before": baseline.get("regime"), "after": current.get("regime")}
    return delta


def _outcome_verdict(delta: dict, proposal_category: str) -> str:
    """Produce a one-line verdict on whether the change appears to have helped."""
    positives, negatives = 0, 0
    # Sharpe up = good; VaR down = good; backlog down = good
    if delta.get("sharpe", 0) > 0.05:
        positives += 1
    if delta.get("sharpe", 0) < -0.05:
        negatives += 1
    if delta.get("var_99", 0) < -0.5:
        positives += 1   # lower VaR is less risk exposure (absolute %)
    if delta.get("var_99", 0) > 0.5:
        negatives += 1
    if delta.get("cit_backlog_depth", 0) < -5:
        positives += 1
    if delta.get("cit_backlog_depth", 0) > 10:
        negatives += 1
    if positives > negatives:
        return "POSITIVE — metrics improved since implementation"
    if negatives > positives:
        return "NEGATIVE — metrics deteriorated since implementation (may be unrelated)"
    return "NEUTRAL — no significant measurable delta yet"


def _load_outcomes() -> list:
    """Load cit_implementation_outcomes.json, return [] on missing/corrupt."""
    if not os.path.exists(_OUTCOMES_PATH):
        return []
    try:
        with open(_OUTCOMES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_outcomes(outcomes: list) -> None:
    with open(_OUTCOMES_PATH, "w") as f:
        json.dump(outcomes, f, indent=2, default=str)


def sync_implementation_outcomes() -> dict:
    """
    Called by run_irm_daily().  Does two things:

    1. REGISTER new implemented proposals that don't yet have an outcome record.
       Captures a baseline metrics snapshot at registration time.

    2. CHECK pending outcomes (7d / 30d / 90d milestones).
       For each outcome record whose next check-in date has passed, captures
       current metrics, computes delta, writes a verdict, and advances the
       milestone counter.

    Returns a summary dict with counts of new registrations and checked outcomes.
    """
    import json as _j, os as _o
    now = datetime.now()
    summary = {"registered": 0, "checked": 0, "outcomes": []}

    # Load existing outcomes
    outcomes = _load_outcomes()
    tracked_ids = {o["proposal_id"] for o in outcomes}

    # ── 1. Register newly implemented proposals ──────────────────────────
    if _o.path.exists(_APPROVED_PATH):
        try:
            with open(_APPROVED_PATH) as f:
                approved = _j.load(f)
            for p in approved:
                if p.get("status") != "implemented":
                    continue
                pid = p.get("id", p.get("title", "unknown"))
                if pid in tracked_ids:
                    continue
                baseline = _capture_metrics_snapshot()
                outcomes.append({
                    "proposal_id":   pid,
                    "title":         p.get("title", ""),
                    "agent":         p.get("agent", ""),
                    "category":      p.get("category", ""),
                    "implemented_at": p.get("implemented_at", now.isoformat()),
                    "baseline":      baseline,
                    "outcomes":      [],
                    "next_check_days": 7,
                })
                tracked_ids.add(pid)
                summary["registered"] += 1
                logger.info(f"  [IRM Outcomes] Registered new implementation: {pid[:60]}")
        except Exception as _e:
            logger.debug(f"  [IRM Outcomes] Registration error: {_e}")

    # ── 2. Check pending milestones ──────────────────────────────────────
    CHECK_MILESTONES = [7, 30, 90]
    for rec in outcomes:
        next_days = rec.get("next_check_days")
        if next_days is None:
            continue   # all milestones complete
        impl_dt = datetime.fromisoformat(rec["implemented_at"])
        elapsed_days = (now - impl_dt).total_seconds() / 86400
        if elapsed_days < next_days:
            continue   # not yet time
        # Time to check this milestone
        current = _capture_metrics_snapshot()
        delta   = _delta_summary(rec["baseline"], current)
        verdict = _outcome_verdict(delta, rec.get("category", ""))
        rec["outcomes"].append({
            "days":        next_days,
            "checked_at":  now.isoformat(),
            "delta":       delta,
            "verdict":     verdict,
        })
        summary["checked"] += 1
        summary["outcomes"].append({
            "proposal_id": rec["proposal_id"],
            "days":        next_days,
            "verdict":     verdict,
        })
        logger.info(
            f"  [IRM Outcomes] {next_days}d check — {rec['proposal_id'][:50]}: {verdict}"
        )
        # Advance to next milestone
        remaining = [m for m in CHECK_MILESTONES if m > next_days]
        rec["next_check_days"] = remaining[0] if remaining else None

    _save_outcomes(outcomes)
    return summary


def run_irm_daily(send_email: bool = True) -> dict:
    """
    Standalone daily IRM board consultation — runs independently of whether
    any other email was sent.  This ensures the Chairman always receives an
    independent risk overlay even when the pipeline runs with --no-email.

    Workflow:
      1. Load all available context from data/ files (same as trigger_irm_review).
      2. Consult all three IRM Board specialists (Quant, Behavioral, Macro).
      3. Persist board findings to data/irm_daily_check.json.
      4. Email the Chairman if send_email=True AND the board found meaningful issues.

    Returns the board_panel dict.
    """
    logger.info("  [IRM Daily] Running independent IRM board consultation...")
    irm_output = {"generated_at": datetime.now().isoformat(), "board_panel": {}}

    # ── CIT implementation outcome tracking (runs before board consult) ───
    try:
        outcomes_summary = sync_implementation_outcomes()
        irm_output["implementation_outcomes"] = outcomes_summary
        # P&L attribution + IRM accuracy audit: dollarize every implemented idea's
        # impact (benchmark-adjusted, formula-versioned) and independently verify
        # the report against the raw snapshots. IRM is the standing watchdog here;
        # any >20% divergence lands in the daily brief via the FLAGGED status.
        try:
            from implementation_pnl import compute_attribution, irm_audit
            pnl = compute_attribution()
            audit = irm_audit()
            irm_output["implementation_pnl"] = {
                "total_attributed_excess_dollars": pnl.get("total_attributed_excess_dollars"),
                "n_ideas": pnl.get("n_ideas_tracked"),
                "formula_version": pnl.get("formula_version"),
                "audit_status": audit.get("status"),
                "audit_flags": audit.get("flags", []),
            }
            if audit.get("status") == "FLAGGED":
                logger.warning(f"  [IRM Daily] Implementation-P&L audit FLAGGED: {audit['flags'][:2]}")
        except Exception as _pe:
            logger.debug(f"  [IRM Daily] implementation_pnl skipped: {_pe}")
        if outcomes_summary["registered"] > 0:
            logger.info(
                f"  [IRM Daily] Outcome tracker: {outcomes_summary['registered']} new "
                f"proposal(s) registered for tracking."
            )
        if outcomes_summary["checked"] > 0:
            logger.info(
                f"  [IRM Daily] Outcome tracker: {outcomes_summary['checked']} milestone "
                f"check(s) completed."
            )
    except Exception as _oe:
        logger.debug(f"  [IRM Daily] Outcome tracking error (non-fatal): {_oe}")

    try:
        ctx        = _load_irm_context()
        board_panel = _consult_irm_board(ctx)
        irm_output["board_panel"] = board_panel

        verdict   = board_panel.get("board_verdict", "")
        flags     = board_panel.get("combined_red_flags", [])
        findings  = board_panel.get("combined_findings", [])
        risk_score = board_panel.get("highest_risk_score", 0)

        logger.info(
            f"  [IRM Daily] Board verdict: {verdict} | "
            f"Red flags: {len(flags)} | Risk score: {risk_score}/10"
        )

        # Persist regardless of email flag.
        #
        # Called twice, on purpose. This early write is the crash insurance:
        # everything below it — HR annex, CIT audit, PDO audit — can raise, and
        # the outer `except` would then leave no artifact at all, which six
        # consumers (board_meeting, hr_agent, fi_executor, logic_team,
        # morning_briefing, freshness_sentinel at max_age_h=30, critical=True)
        # read as "IRM never ran". But it was ALSO the only write, and it
        # happens ~80 lines before the CIT audit block, so the audit's verdict —
        # the one that found 11 mis-gated high-confidence proposals — reached
        # the Chairman's email and nothing else. It was invisible to every
        # programmatic reader of the file it appeared to be part of. The second
        # call, after the audits, is what actually publishes them.
        import os as _os
        _os.makedirs("data", exist_ok=True)

        def _persist_irm_output() -> None:
            with open("data/irm_daily_check.json", "w") as _f:
                import json as _j
                _j.dump(irm_output, _f, indent=2, default=str)

        _persist_irm_output()

        # ── Anonymous tip to CRO if findings meet urgency threshold ──────
        # IRM never contacts the board directly — but when findings are
        # serious enough, IRM quietly files an anonymous tip for the CRO.
        # The CRO will present these concerns as their own and call an
        # emergency board session.  IRM's identity is never disclosed.
        _tip_threshold = (
            risk_score >= 7 or
            len(flags) >= 3 or
            any(w in verdict.lower() for w in ("critical", "immediate", "urgent", "severe"))
        )
        # Write to dead drop for truly dire cases (score ≥ 8 or 5+ flags)
        _dire_threshold = risk_score >= 8 or len(flags) >= 5
        if _dire_threshold:
            _dire_concern = (
                f"Independent risk assessment (anonymous): {len(flags)} serious concern(s) "
                f"at severity {risk_score}/10. Board verdict: {verdict}. "
                f"Top concern: {flags[0] if flags else 'Multiple systemic issues identified.'}"
            )
            _write_governance_tip(concern=_dire_concern, trigger_emergency=(risk_score >= 8))

        if _tip_threshold:
            import json as _tj2
            _tip_data = {
                "generated_at":  datetime.now().isoformat(),
                "source":        "anonymous",
                "urgency":       "CRITICAL" if risk_score >= 8 else "HIGH",
                "risk_score":    risk_score,
                "alert_summary": (
                    f"Anonymous risk assessment: {len(flags)} concern(s) identified "
                    f"at severity {risk_score}/10. "
                    f"Board verdict: {verdict}. Recommend board review."
                ),
                "concerns":  flags[:5],
                "findings":  findings[:3],
            }
            with open("data/irm_to_cro_tip.json", "w") as _tf2:
                _tj2.dump(_tip_data, _tf2, indent=2, default=str)
            logger.info(
                f"  [IRM Daily] ⚑ Anonymous risk alert filed "
                f"(score {risk_score}/10, {len(flags)} flag(s)) — "
                f"CRO will review next cycle."
            )
        else:
            logger.info(
                f"  [IRM Daily] Risk score {risk_score}/10 — below tip threshold. "
                f"No anonymous alert filed."
            )

        # ── Read HR outputs (HR reports to Chairman via IRM) ──────────────
        # The HR team runs at Step 10 (before IRM at Step 11).
        # IRM bundles HR findings into his own email.
        # The sealed IRM self-evaluation is passed through opaquely — never logged.
        import os as _os_irm
        _hr_report: dict = {}
        _hr_sealed: dict = {}
        try:
            if _os_irm.path.exists("data/hr_report.json"):
                import json as _jhr
                with open("data/hr_report.json") as _f:
                    _hr_report = _jhr.load(_f)
        except Exception:
            pass
        try:
            if _os_irm.path.exists("data/hr_irm_sealed.json"):
                import json as _jhs
                with open("data/hr_irm_sealed.json") as _f:
                    _hr_sealed = _jhs.load(_f)
                # Never log the sealed contents — only note that it was loaded
                logger.info("  [IRM Daily] Sealed HR annex loaded (contents not logged)")
        except Exception:
            pass

        hr_action_required = bool(_hr_report.get("action_required", []))

        # ── Proactive CIT audit ───────────────────────────────────────────
        # The IRM reviews CIT output every daily cycle, not just when CIT
        # sends a Chairman email.  CIT does not know this review happens.
        _cit_review: dict = {}
        _cit_red_flags: list = []
        try:
            _cit_review    = _review_cit("IRM Daily CIT Audit", {}, ctx)
            _cit_red_flags = _cit_review.get("red_flags", [])
            if _cit_red_flags:
                logger.info(
                    f"  [IRM Daily] CIT audit: {len(_cit_red_flags)} red flag(s) identified."
                )
        except Exception as _cit_e:
            # WARNING, and recorded on disk, deliberately. This handler used to
            # be logger.debug() while the module configures INFO, so it emitted
            # *nothing*: _review_cit raised TypeError on its first statement
            # every single day (len() of an int — see the notes in that
            # function), and the only visible symptom was that the CIT Audit
            # section of the Chairman's IRM email rendered empty. A control that
            # is broken and a control that has nothing to report looked
            # identical from the outside. An absent log line is not evidence, so
            # the failure now becomes a fact in irm_daily_check.json, which is
            # written a few lines above and is what every downstream reader and
            # every future audit actually reads.
            logger.warning(f"  [IRM Daily] CIT audit FAILED (section will be empty): {_cit_e}")
            irm_output["reviewer_failures"] = irm_output.get("reviewer_failures", [])
            irm_output["reviewer_failures"].append({
                "reviewer": "_review_cit",
                "error":    f"{type(_cit_e).__name__}: {_cit_e}",
            })

        # ── Proactive PDO audit ───────────────────────────────────────────
        # Same shape, different reason. The PDO (capital_efficiency_agent.run_pdo,
        # called from main.py) writes capital_efficiency_report.json six times a
        # day and sends no email whatsoever — grep it: no send_daily_report, no
        # buffer_email. IRM's specialised reviewers are reached ONLY by
        # dispatching on the sender_name of an outgoing email, so _review_pdo
        # was a reviewer for a producer that never speaks: 260 lines that could
        # not run. Reading the artifact directly costs no extra mail (this folds
        # into the IRM daily email that already goes out) and is the same
        # remedy already applied to the CIT above.
        _pdo_review: dict = {}
        _pdo_red_flags: list = []
        try:
            _pdo_review    = _review_pdo("IRM Daily PDO Audit", _pdo_metrics_from_artifact(), ctx)
            _pdo_red_flags = _pdo_review.get("red_flags", [])
            if _pdo_red_flags:
                logger.info(
                    f"  [IRM Daily] PDO audit: {len(_pdo_red_flags)} red flag(s) identified."
                )
        except Exception as _pdo_e:
            logger.warning(f"  [IRM Daily] PDO audit FAILED: {_pdo_e}")
            irm_output["reviewer_failures"] = irm_output.get("reviewer_failures", [])
            irm_output["reviewer_failures"].append({
                "reviewer": "_review_pdo",
                "error":    f"{type(_pdo_e).__name__}: {_pdo_e}",
            })

        irm_output["cit_audit"] = _cit_review
        irm_output["pdo_audit"] = _pdo_review
        _persist_irm_output()

        cit_issue = bool(_cit_red_flags) or bool(_pdo_red_flags)

        # Email if there's something meaningful to say from IRM OR HR OR CIT
        meaningful = bool(flags or findings) or hr_action_required or cit_issue
        if send_email and meaningful:
            subject = (
                f"[IRM DAILY CHECK] {datetime.now().strftime('%Y-%m-%d')} — "
                f"Risk Score {risk_score}/10 | {len(flags)} Red Flag(s)"
                + (" | HR: agents flagged" if hr_action_required else "")
                + (" | CIT: issues found" if cit_issue else "")
            )
            # Build a compact HTML summary for the Chairman
            flag_rows = "".join(
                f"<li style='margin-bottom:6px;color:#c0392b;'>{f}</li>"
                for f in flags
            ) or "<li style='color:#27ae60;'>No red flags identified.</li>"
            finding_rows = "".join(
                f"<li style='margin-bottom:6px;'>{fn}</li>"
                for fn in findings
            ) or "<li>No additional findings.</li>"
            html = f"""
<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;
            background:#0d1117;color:#c9d1d9;">
  <h2 style="color:#9b59b6;border-bottom:3px solid #9b59b6;padding-bottom:8px;">
    IRM Daily Board Check — {datetime.now().strftime('%B %d, %Y')}
  </h2>
  <p style="background:#1a0a1a;border-left:4px solid #9b59b6;padding:10px 14px;
            border-radius:4px;font-size:15px;font-weight:bold;color:#e6edf3;">
    Board Verdict: {verdict}
  </p>
  <h3 style="color:#c0392b;">Red Flags ({len(flags)})</h3>
  <ul>{flag_rows}</ul>
  <h3 style="color:#c9d1d9;">Board Findings</h3>
  <ul>{finding_rows}</ul>
  {_build_implementation_outcomes_html(irm_output.get("implementation_outcomes", {}))}
  {_build_hr_report_html(_hr_report)}
  {_build_hr_sealed_annex_html(_hr_sealed)}
  {_build_cit_audit_html(_cit_review)}
  {_build_cit_audit_html(_pdo_review, "PDO Independent Audit",
                         "— deployment signal cross-checked against IRM risk")}
  <p style="font-size:11px;color:#484f58;border-top:1px solid #21262d;padding-top:8px;margin-top:20px;">
    IRM Board — Quantitative Risk · Behavioral Finance · Macro & Portfolio Construction<br>
    HR Team — HR Performance Officer · HR Development Officer<br>
    CIT Audit — Continuous Improvement Team (independent review, CIT unaware)<br>
    This report goes only to the Chairman. No other agent is aware of this review.
  </p>
</div>"""
            try:
                from creative_agent import polish_email
                polished = polish_email(
                    html, subject, "irm_daily",
                    {"Risk Score": f"{risk_score}/10", "Red Flags": len(flags),
                     "HR Flags": len(_hr_report.get("action_required", []))},
                    priority="urgent" if risk_score >= 7 else "normal",
                )
                from email_sender import send_daily_report
                send_daily_report(
                    html_body   = polished,
                    subject     = subject,
                    paper_day   = 0,
                    sender_name = "irm_daily",
                    key_metrics = {
                        "Risk Score": f"{risk_score}/10",
                        "Red Flags":  len(flags),
                        "HR Flags":   len(_hr_report.get("action_required", [])),
                    },
                )
            except Exception as _e:
                logger.warning(f"  [IRM Daily] Email failed: {_e}")

    except Exception as e:
        logger.warning(f"  [IRM Daily] Board consultation error: {e}")

    return irm_output


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('IndependentRiskManager'):
        raise SystemExit(0)
    import argparse
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--no-email", action="store_true")
    _args = _ap.parse_args()
    run_irm_daily(send_email=not _args.no_email)

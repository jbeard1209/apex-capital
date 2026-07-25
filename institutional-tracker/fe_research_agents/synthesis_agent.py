#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE SYNTHESIS AGENT
============================================
Reads all 14 research memos and writes a final investment recommendation memo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Synthesis] Could not load {path}: {e}")
    return None


def run_synthesis(ticker: str) -> dict:
    """
    Reads all 14 research memos and writes a final investment recommendation memo.
    """
    ticker = ticker.upper().strip()
    out_dir  = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "synthesis_memo.json"

    # Load all component research (original 5)
    forensics  = _load_json(out_dir / "financial_forensics.json") or {}
    moat       = _load_json(out_dir / "moat_analysis.json") or {}
    management = _load_json(out_dir / "management_quality.json") or {}
    risk       = _load_json(out_dir / "risk_assessment.json") or {}
    valuation  = _load_json(out_dir / "valuation_models.json") or {}

    # Load 4 new deep-research memos
    exec_bg    = _load_json(out_dir / "executive_background.json") or {}
    comp_intel = _load_json(out_dir / "competitive_intelligence.json") or {}
    ind_dyn    = _load_json(out_dir / "industry_dynamics.json") or {}
    cap_struct = _load_json(out_dir / "capital_structure_deep.json") or {}

    # Load 5 additional deep-research memos (esg, earnings quality, business model, activist, supply chain)
    esg        = _load_json(out_dir / "esg_controversy.json") or {}
    eq_deep    = _load_json(out_dir / "earnings_quality_deep.json") or {}
    biz_model  = _load_json(out_dir / "business_model_analysis.json") or {}
    activist   = _load_json(out_dir / "activist_monitor.json") or {}
    supply     = _load_json(out_dir / "supply_chain_risk.json") or {}

    # Extract key scores -- original 5
    forensics_score   = forensics.get("forensics_score", 0)
    forensics_verdict = forensics.get("forensics_verdict", "UNKNOWN")
    moat_score        = moat.get("moat_score", 0)
    moat_width        = moat.get("moat_width", "NONE")
    management_score  = management.get("management_score", 0)
    mgmt_verdict      = management.get("management_verdict", "UNKNOWN")
    risk_score        = risk.get("risk_score", 0)
    risk_level        = risk.get("risk_level", "UNKNOWN")
    going_concern     = risk.get("going_concern_flag", False)

    mos            = valuation.get("margin_of_safety")
    composite_iv   = valuation.get("conservative_iv") or valuation.get("composite_iv")
    current_price  = valuation.get("current_price")
    val_verdict    = valuation.get("verdict", "UNKNOWN")
    target_entry   = valuation.get("target_entry_price_30pct_mos")

    # Extract key scores -- 4 new memos
    mgmt_integrity_score   = exec_bg.get("management_integrity_score", 0)
    mgmt_integrity_verdict = exec_bg.get("management_integrity_verdict", "UNKNOWN")

    competitive_score   = comp_intel.get("competitive_position_score", 0)
    competitive_verdict = comp_intel.get("competitive_verdict", "UNKNOWN")

    industry_score   = ind_dyn.get("industry_score", 0)
    industry_verdict = ind_dyn.get("industry_verdict", "UNKNOWN")
    cyclicality      = ind_dyn.get("cyclicality", "UNKNOWN")

    debt_score   = cap_struct.get("debt_quality_score", 0)
    debt_verdict = cap_struct.get("debt_verdict", "UNKNOWN")

    # Extract key scores -- 5 new deep-research memos
    esg_score   = esg.get("esg_score", 0)
    esg_verdict = esg.get("esg_verdict", "UNKNOWN")

    eq_score   = eq_deep.get("earnings_quality_score", 0)
    eq_verdict = eq_deep.get("earnings_quality_verdict", "UNKNOWN")

    biz_score   = biz_model.get("business_model_score", 0)
    biz_verdict = biz_model.get("business_model_verdict", "UNKNOWN")

    activist_present        = activist.get("activist_present", False)
    activist_names          = activist.get("activist_names", [])
    value_investor_present  = activist.get("value_investor_present", False)
    value_investor_names    = activist.get("value_investor_names", [])
    activist_signal         = activist.get("signal", "NEUTRAL")

    supply_score   = supply.get("supply_chain_risk_score", 0)
    supply_verdict = supply.get("supply_chain_verdict", "UNKNOWN")

    # Research quality: which agents completed
    components_done = {
        "financial_forensics":    bool(forensics and not forensics.get("error")),
        "moat":                   bool(moat and not moat.get("error")),
        "management":             bool(management and not management.get("error")),
        "risk":                   bool(risk and not risk.get("error")),
        "valuation":              bool(valuation and not valuation.get("error")),
        "executive_background":   bool(exec_bg and not exec_bg.get("error")),
        "competitive_intelligence": bool(comp_intel and not comp_intel.get("error")),
        "industry_dynamics":      bool(ind_dyn and not ind_dyn.get("error")),
        "capital_structure_deep": bool(cap_struct and not cap_struct.get("error")),
        "esg_controversy":        bool(esg and not esg.get("error")),
        "earnings_quality_deep":  bool(eq_deep and not eq_deep.get("error")),
        "business_model_analyzer": bool(biz_model and not biz_model.get("error")),
        "activist_monitor":       bool(activist and not activist.get("error")),
        "supply_chain_risk":      bool(supply and not supply.get("error")),
    }
    completion_count = sum(components_done.values())

    # Research quality score (avg of all available component scores)
    scores_for_avg = []
    if components_done["financial_forensics"]:
        scores_for_avg.append(forensics_score)
    if components_done["moat"]:
        scores_for_avg.append(moat_score)
    if components_done["management"]:
        scores_for_avg.append(management_score)
    if components_done["risk"]:
        scores_for_avg.append(risk_score)
    if components_done["valuation"] and mos is not None:
        # Convert MoS % to 0-100 score (100 = 50%+ MoS)
        mos_score = min(100, max(0, int((mos + 0.10) * 200)))
        scores_for_avg.append(mos_score)
    if components_done["executive_background"] and mgmt_integrity_score:
        scores_for_avg.append(mgmt_integrity_score)
    if components_done["competitive_intelligence"] and competitive_score:
        scores_for_avg.append(competitive_score)
    if components_done["industry_dynamics"] and industry_score:
        scores_for_avg.append(industry_score)
    if components_done["capital_structure_deep"] and debt_score:
        scores_for_avg.append(debt_score)
    if components_done["esg_controversy"] and esg_score:
        scores_for_avg.append(esg_score)
    if components_done["earnings_quality_deep"] and eq_score:
        scores_for_avg.append(eq_score)
    if components_done["business_model_analyzer"] and biz_score:
        scores_for_avg.append(biz_score)
    if components_done["supply_chain_risk"] and supply_score:
        scores_for_avg.append(supply_score)

    research_quality_score = sum(scores_for_avg) / len(scores_for_avg) if scores_for_avg else 0

    # --- Forced AVOID rules (new) ---
    force_avoid        = False
    force_avoid_reason = []

    if mgmt_integrity_verdict == "RED_FLAG":
        force_avoid = True
        force_avoid_reason.append(
            f"Management integrity verdict is RED_FLAG (score={mgmt_integrity_score})"
        )

    if competitive_verdict == "AT_RISK" and moat_width == "NONE":
        force_avoid = True
        force_avoid_reason.append(
            "Competitive verdict AT_RISK combined with no moat -- no defensible position"
        )

    if debt_verdict == "DISTRESSED":
        force_avoid = True
        force_avoid_reason.append(
            f"Capital structure verdict DISTRESSED (score={debt_score}) -- regardless of MoS"
        )

    # New forced AVOID rules for 5 additional agents
    if esg_verdict == "RED_FLAG":
        force_avoid = True
        force_avoid_reason.append(
            f"ESG verdict RED_FLAG (score={esg_score}) -- enforcement actions or severe governance failure"
        )

    if eq_verdict == "POOR" and forensics_verdict in ("POOR", "RED_FLAG", "CONCERNING"):
        force_avoid = True
        force_avoid_reason.append(
            f"Earnings quality POOR (score={eq_score}) combined with weak forensics ({forensics_verdict}) -- "
            "high risk of earnings manipulation or deterioration"
        )

    if supply_verdict == "CRITICAL":
        force_avoid = True
        force_avoid_reason.append(
            f"Supply chain verdict CRITICAL (score={supply_score}) -- existential concentration risk"
        )

    # Cyclicality flag for board
    cycle_adjusted_flag = (cyclicality == "HIGHLY_CYCLICAL")

    # --- Verdict logic ---
    mos_val = mos if mos is not None else -1.0

    if force_avoid:
        overall_verdict = "AVOID"
    else:
        # BUY conditions
        is_buy = (
            mos_val >= 0.25
            and forensics_score >= 60
            and moat_score >= 40
            and management_score >= 50
            and risk_score >= 50
            and not going_concern
        )

        # WATCH conditions
        is_watch = (
            not is_buy
            and (mos_val >= 0.10 or (forensics_score >= 65 and moat_score >= 50))
            and risk_score >= 40
            and not going_concern
        )

        # AVOID conditions
        is_avoid = (
            mos_val < 0.0
            or going_concern
            or forensics_score < 30
            or risk_score < 30
            or management_score < 25
        )

        if is_buy:
            overall_verdict = "BUY"
        elif is_avoid:
            overall_verdict = "AVOID"
        elif is_watch:
            overall_verdict = "WATCH"
        else:
            overall_verdict = "AVOID"

    # Conviction level
    high_conviction = (
        not force_avoid
        and mos_val >= 0.35
        and forensics_score >= 75
        and moat_score >= 65
        and management_score >= 65
        and risk_score >= 65
    )
    low_conviction = (
        force_avoid
        or forensics_score < 55
        or moat_score < 30
        or management_score < 45
        or risk_score < 45
    )
    if high_conviction:
        conviction_level = "HIGH"
    elif low_conviction:
        conviction_level = "LOW"
    else:
        conviction_level = "MEDIUM"

    # --- Investment thesis (structured) ---
    moat_type       = moat.get("moat_type", "unknown")
    moat_durability = moat.get("moat_durability", "UNKNOWN")

    thesis_parts = []

    if moat_width in ("WIDE", "NARROW") and moat_type not in ("NONE_DETECTED", "TEXT_UNAVAILABLE"):
        thesis_parts.append(
            f"{ticker} has a {moat_width.lower()} competitive moat ({moat_type.replace('_', ' ')}) "
            f"with {moat_durability.lower()} durability."
        )
    elif moat_width == "NONE":
        thesis_parts.append(f"{ticker} shows limited evidence of a durable competitive moat.")

    if forensics_verdict in ("EXCELLENT", "GOOD"):
        thesis_parts.append(
            f"Financial quality is {forensics_verdict.lower()}: "
            f"FCF conversion and ROIC trends support the business quality thesis."
        )
    elif forensics_verdict in ("POOR", "RED_FLAG"):
        thesis_parts.append(
            f"Financial forensics raises concerns: verdict is {forensics_verdict}."
        )

    if composite_iv and current_price:
        thesis_parts.append(
            f"At ${current_price:.2f}, the stock trades at a "
            f"{mos_val*100:.0f}% discount to conservative IV of ${composite_iv:.2f}."
        )

    if mgmt_verdict in ("EXCELLENT", "GOOD"):
        thesis_parts.append(f"Management quality is {mgmt_verdict.lower()}, supporting long-term value creation.")

    # New memo context
    if industry_verdict in ("EXCELLENT", "GOOD") and ind_dyn:
        sector = ind_dyn.get("sector", "")
        tailwind = ind_dyn.get("secular_tailwind", "")
        if sector and tailwind:
            thesis_parts.append(f"Industry backdrop is favorable ({sector}): {tailwind[:120]}.")

    if competitive_verdict in ("DOMINANT", "STRONG") and comp_intel:
        market_pos = comp_intel.get("market_position_claim", "")
        if market_pos and len(market_pos) > 10:
            thesis_parts.append(f"Competitive position: {market_pos[:150]}.")

    if debt_verdict in ("FORTRESS", "STRONG") and cap_struct:
        coverage = cap_struct.get("interest_coverage_current")
        if coverage:
            thesis_parts.append(f"Balance sheet is {debt_verdict.lower()} with {coverage:.1f}x interest coverage.")

    # Activist and value investor context
    if activist_present and activist_names:
        thesis_parts.append(
            f"Activist investor present: {', '.join(activist_names[:2])} (signal={activist_signal}) -- "
            "potential catalyst for value unlock."
        )
    if value_investor_present and value_investor_names and not activist_present:
        thesis_parts.append(
            f"Validated by known value investor(s): {', '.join(value_investor_names[:2])} -- external validation."
        )

    investment_thesis = " ".join(thesis_parts[:5]) if thesis_parts else (
        f"{ticker}: insufficient data to build complete investment thesis."
    )

    # Key strengths
    key_strengths = []
    if moat_width == "WIDE":
        key_strengths.append(f"Wide durable moat ({moat_type.replace('_', ' ')})")
    if forensics_score >= 75:
        key_strengths.append("Excellent financial quality -- high FCF conversion and strong ROIC")
    if management_score >= 70 or mgmt_integrity_score >= 75:
        key_strengths.append("Strong management with aligned incentives and disciplined capital allocation")
    if mos_val >= 0.30:
        key_strengths.append(f"Deep value: {mos_val*100:.0f}% margin of safety")
    if risk_score >= 70:
        key_strengths.append("Low risk profile -- no material red flags in filings")
    if debt_verdict in ("FORTRESS", "STRONG"):
        key_strengths.append(f"Strong balance sheet ({debt_verdict.lower()})")
    if competitive_verdict in ("DOMINANT", "STRONG"):
        key_strengths.append(f"Strong competitive position ({competitive_verdict.lower()})")

    key_strengths = key_strengths[:3]

    # Key risks
    key_risks_list  = risk.get("key_risks", [])[:2]
    mgmt_red_flags  = management.get("red_flags", [])[:1]
    exec_red_flags  = exec_bg.get("team_red_flags", [])[:1]
    esg_flags = esg.get("controversy_flags", [])[:1] if esg else []
    combined_risks  = key_risks_list + mgmt_red_flags + exec_red_flags + esg_flags
    if force_avoid_reason:
        combined_risks = force_avoid_reason + combined_risks
    if cycle_adjusted_flag:
        combined_risks.append("HIGHLY CYCLICAL: board should use cycle-adjusted earnings for valuation")
    if supply_verdict in ("HIGH", "CRITICAL") and supply:
        combined_risks.append(f"Supply chain risk {supply_verdict} (score={supply_score})")
    key_risks_out = combined_risks[:5]

    # Thesis break conditions
    thesis_breaks = []
    if moat_width in ("WIDE", "NARROW"):
        thesis_breaks.append("Gross margins declining >300bps for 2+ consecutive years")
    thesis_breaks.append("ROIC drops below cost of capital (10%) for 2+ years")
    if management.get("shares_trend") != "DILUTION":
        thesis_breaks.append("Management begins significant share issuance (dilution >5%/yr)")
    thesis_breaks.append("Going concern language appears in filings")
    thesis_breaks.append(
        f"Stock price reaches IV (${composite_iv:.2f}) -- MoS evaporates" if composite_iv
        else "Stock trades at or above IV"
    )
    if debt_verdict in ("ADEQUATE", "STRETCHED"):
        thesis_breaks.append("Debt/EBITDA rises above 4x or covenant waiver filed")

    # Monitoring checklist
    monitoring = [
        "Quarterly: verify FCF conversion >= 0.8x net income",
        "Quarterly: check gross margin trend (flag if declining >100bps)",
        "Semi-annual: re-run full valuation research if stock moves >20%",
        "Annual: re-run all 9 research agents (data > 90 days old triggers refresh)",
        "On 10-K filing: re-read risk factors section for new risk categories",
    ]
    if moat_width == "WIDE":
        monitoring.append("Annual: confirm moat keywords still present in MD&A")
    if cycle_adjusted_flag:
        monitoring.append("Cyclical: use normalized earnings (10yr average) for valuation -- not peak earnings")
    if debt_verdict in ("ADEQUATE", "STRETCHED"):
        monitoring.append("Quarterly: monitor covenant compliance and debt maturity schedule")

    # Research status tracking (all 9 required for 'complete')
    research_status = {
        "financial_forensics":      components_done["financial_forensics"],
        "moat":                     components_done["moat"],
        "management":               components_done["management"],
        "risk":                     components_done["risk"],
        "valuation":                components_done["valuation"],
        "executive_background":     components_done["executive_background"],
        "competitive_intelligence": components_done["competitive_intelligence"],
        "industry_dynamics":        components_done["industry_dynamics"],
        "capital_structure_deep":   components_done["capital_structure_deep"],
        "esg_controversy":          components_done["esg_controversy"],
        "earnings_quality_deep":    components_done["earnings_quality_deep"],
        "business_model_analyzer":  components_done["business_model_analyzer"],
        "activist_monitor":         components_done["activist_monitor"],
        "supply_chain_risk":        components_done["supply_chain_risk"],
    }
    research_complete = all(research_status.values())

    # Ready for board (updated: need >= 7/9 now, still allow partial)
    ready_for_board = (
        completion_count >= 9
        and overall_verdict in ("BUY", "WATCH")
        and not going_concern
        and not force_avoid
    )

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_verdict": overall_verdict,
        "conviction_level": conviction_level,
        "investment_thesis": investment_thesis,
        "key_strengths": key_strengths,
        "key_risks": key_risks_out,
        "margin_of_safety": round(mos_val, 4) if mos_val is not None else None,
        "composite_iv": round(composite_iv, 2) if composite_iv else None,
        "current_price": round(current_price, 2) if current_price else None,
        "target_entry_price": round(target_entry, 2) if target_entry else None,
        "thesis_break_conditions": thesis_breaks,
        "monitoring_checklist": monitoring,
        "research_quality_score": round(research_quality_score, 1),
        "ready_for_board": ready_for_board,
        "force_avoid": force_avoid,
        "force_avoid_reasons": force_avoid_reason,
        "cycle_adjusted_earnings_flag": cycle_adjusted_flag,
        "component_scores": {
            "financial_forensics":      forensics_score,
            "moat":                     moat_score,
            "management":               management_score,
            "risk":                     risk_score,
            "valuation_mos_pct":        round(mos_val * 100, 1) if mos_val is not None else None,
            "executive_background":     mgmt_integrity_score,
            "competitive_intelligence": competitive_score,
            "industry_dynamics":        industry_score,
            "capital_structure_deep":   debt_score,
            "esg_controversy":          esg_score,
            "earnings_quality_deep":    eq_score,
            "business_model_analyzer":  biz_score,
            "supply_chain_risk":        supply_score,
        },
        "components_completed": components_done,
        "completion_count": completion_count,
        "research_status": research_status,
        "research_complete": research_complete,
        "moat_width": moat_width,
        "moat_durability": moat_durability,
        "forensics_verdict": forensics_verdict,
        "management_verdict": mgmt_verdict,
        "management_integrity_verdict": mgmt_integrity_verdict,
        "competitive_verdict": competitive_verdict,
        "industry_verdict": industry_verdict,
        "cyclicality": cyclicality,
        "debt_verdict": debt_verdict,
        "risk_level": risk_level,
        "going_concern_flag": going_concern,
        "valuation_verdict": val_verdict,
        "esg_verdict": esg_verdict,
        "esg_score": esg_score,
        "earnings_quality_verdict": eq_verdict,
        "earnings_quality_score": eq_score,
        "business_model_verdict": biz_verdict,
        "business_model_score": biz_score,
        "activist_signal": activist_signal,
        "activist_present": activist_present,
        "value_investor_present": value_investor_present,
        "supply_chain_verdict": supply_verdict,
        "supply_chain_risk_score": supply_score,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Synthesis] {ticker}: verdict={overall_verdict} | conviction={conviction_level} | "
        f"MoS={mos_val*100:.1f}% | ready_for_board={ready_for_board} | "
        f"quality={research_quality_score:.0f} | force_avoid={force_avoid} | "
        f"components={completion_count}/14 | research_complete={research_complete}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_synthesis(tk)
        print(json.dumps(r, indent=2, default=str))

#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — R&D GEOPOLITICAL RESEARCH TEAM
# =============================================================================
# A two-analyst sub-team embedded inside the R&D department.
# They operate entirely in the CONTEXT layer — they do not generate stock picks.
# Their output is intelligence that helps other agents calibrate conviction,
# identify sector-level risk, and understand why certain price moves are happening.
#
# The two analysts:
#
#   ANALYST 1 — GEOPOLITICAL RISK ANALYST (Dr. Sofia Reyes)
#   ─────────────────────────────────────────────────────────
#   Tracks current and historical geopolitical events and maps their
#   documented impact on specific stock market sectors.
#   Examples:
#     - Trade wars (US-China 2018-19) → which sectors were hurt vs. helped?
#     - Russia-Ukraine 2022 → energy spike, defense rally, supply chain damage
#     - Brexit → UK financials, UK-listed multinationals
#     - COVID travel bans → airlines, hospitality, pharma
#   For each current R&D cycle, she identifies which ASSIGNED sectors have
#   historical geopolitical sensitivity and flags them accordingly.
#
#   ANALYST 2 — MACRO POLICY HISTORIAN (Dr. James Okafor)
#   ──────────────────────────────────────────────────────
#   Tracks the history of fiscal and monetary policy decisions and their
#   documented impact on sectors and growth/value factor rotation.
#   Examples:
#     - Fed rate cycles: who wins (financials, value) and who loses (growth, duration)
#     - Fiscal stimulus programs: infrastructure → materials/industrials
#     - Tax policy changes: corporate tax cuts → buybacks → tech/financials
#     - Regulatory shifts: bank deregulation, drug pricing policy, antitrust
#   For each R&D cycle, he identifies the current policy environment and
#   maps it to historical precedents for the assigned sectors.
#
# Combined output: a "geopolitical_context" report that is merged into the
# R&D Team Leader's Chairman briefing.
#
# Usage:
#   from rd_geopolitical_team import GeopoliticalResearchTeam
#   team   = GeopoliticalResearchTeam()
#   report = team.analyze(assignments, current_regime)
# =============================================================================

import json
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger("rd_geopolitical_team")

GEOPOLITICAL_CONTEXT_PATH = "data/rd_geopolitical_context.json"


# =============================================================================
# KNOWLEDGE BASE — Geopolitical Events & Sector Impacts
# =============================================================================
# Each entry: event → sectors_hurt, sectors_helped, duration, key_lesson.
# This serves as Dr. Reyes's reference library.

GEOPOLITICAL_EVENT_LIBRARY = [
    {
        "event":          "US-China Trade War (2018–2019)",
        "trigger":        "Tariff escalation on ~$550B in goods",
        "sectors_hurt":   ["Technology", "Semiconductors", "Materials", "Industrials",
                           "Consumer Discretionary", "Retail"],
        "sectors_helped": ["Defense & Aerospace"],
        "markets_affected": ["US", "China", "emerging markets"],
        "duration_months": 18,
        "max_impact_pct":  -20,   # approximate peak drawdown on affected names
        "resolved_by":    "Phase 1 deal Jan 2020 — partial, not full resolution",
        "key_lesson": (
            "Tech supply chains (NVDA, QCOM, AVGO) face direct revenue risk when "
            "China-US tension escalates. Semiconductor companies with China >30% "
            "revenue saw outsized drawdowns. Domestic-only businesses were insulated. "
            "Short-term pain created entry points in high-quality names — those who "
            "held NVDA through the trade war saw 400%+ gains by 2023."
        ),
        "sectors_with_sensitivity": {
            "Technology":     "HIGH — China revenue exposure in software/hardware",
            "Semiconductors": "CRITICAL — TSMC, NVDA, QCOM all have China supply chain links",
            "Industrials":    "MODERATE — tariff pass-through to end markets",
            "Retail":         "MODERATE — consumer goods cost inflation",
        },
    },
    {
        "event":          "Russia-Ukraine War (Feb 2022–ongoing)",
        "trigger":        "Russian invasion of Ukraine, NATO response, energy sanctions",
        "sectors_hurt":   ["Consumer Discretionary", "Retail", "Airlines",
                           "Real Estate", "Technology"],
        "sectors_helped": ["Defense & Aerospace", "Materials", "Cybersecurity"],
        "markets_affected": ["Europe", "Global energy markets", "Eastern Europe"],
        "duration_months": 30,   # ongoing
        "max_impact_pct":  -15,
        "resolved_by":    "Unresolved as of 2026",
        "key_lesson": (
            "Defense and cybersecurity were the clearest beneficiaries — NATO spending "
            "commitments jumped from 2% of GDP targets to 3%+ discussions. LMT, RTX, "
            "NOC all gained 30-60% in 2022-23 as defense budgets expanded. "
            "Cybersecurity saw sustained demand as nation-state attack risk rose. "
            "Energy sanctions created the 2022 energy supercycle that made XLE the "
            "only positive S&P sector in 2022."
        ),
        "sectors_with_sensitivity": {
            "Defense & Aerospace": "DIRECT BENEFICIARY — budget tailwind",
            "Cybersecurity":       "DIRECT BENEFICIARY — threat escalation",
            "Technology":          "LOW-MODERATE — limited direct exposure",
        },
    },
    {
        "event":          "COVID-19 Pandemic (2020)",
        "trigger":        "Global pandemic, lockdowns, supply chain collapse",
        "sectors_hurt":   ["Airlines", "Hospitality", "Retail", "Real Estate",
                           "Industrials", "Consumer Discretionary"],
        "sectors_helped": ["Technology", "Cloud & SaaS", "Biotech", "Healthcare",
                           "Consumer Staples"],
        "markets_affected": ["Global"],
        "duration_months": 12,
        "max_impact_pct":  -34,
        "resolved_by":    "Vaccine rollout + fiscal stimulus 2021",
        "key_lesson": (
            "Digital acceleration compressed 5 years of tech adoption into 12 months. "
            "Companies like Zoom, Shopify, Netflix, and cloud infrastructure providers "
            "saw extraordinary growth. Biotech (MRNA, BNTX) became mainstream. "
            "The rebound from the COVID low was the fastest in market history — "
            "5 months to full recovery. Stop-losses triggered near the bottom "
            "meant missing the entire rebound."
        ),
        "sectors_with_sensitivity": {
            "Healthcare":  "CRITICAL BENEFICIARY — pandemic response demand",
            "Biotech":     "DIRECT BENEFICIARY — vaccine/antiviral development",
            "Technology":  "MAJOR BENEFICIARY — remote work acceleration",
            "Cloud & SaaS": "DIRECT BENEFICIARY — SaaS adoption spike",
        },
    },
    {
        "event":          "China Tech Crackdown (2021)",
        "trigger":        "Chinese government antitrust/data regulations on tech sector",
        "sectors_hurt":   ["Technology (China-listed)", "Communication Services"],
        "sectors_helped": [],
        "markets_affected": ["China", "HK-listed ADRs"],
        "duration_months": 24,
        "max_impact_pct":  -70,   # Chinese tech (BABA, DIDI etc.)
        "resolved_by":    "Partial — regulatory pressure eased 2023 but not reversed",
        "key_lesson": (
            "Regulatory risk is existential for tech firms in markets without rule of law "
            "protections. US-listed Chinese ADRs fell 60-90% from peak. "
            "The lesson for our portfolio: avoid meaningful exposure to companies where "
            "a single government actor can alter the operating environment overnight. "
            "This is why we focus on US-domiciled companies."
        ),
        "sectors_with_sensitivity": {
            "Technology": "LOW — applies primarily to China-domiciled firms",
        },
    },
    {
        "event":          "2022 Fed Rate Hike Cycle (Mar–Dec 2022)",
        "trigger":        "Fed raised rates 425bps — fastest since 1980",
        "sectors_hurt":   ["Technology", "Cloud & SaaS", "Biotech", "Real Estate",
                           "Consumer Discretionary", "Semiconductors"],
        "sectors_helped": ["Financials", "Energy", "Consumer Staples", "Utilities"],
        "markets_affected": ["US", "Global"],
        "duration_months": 10,
        "max_impact_pct":  -35,   # NASDAQ -33%, high-growth names much worse
        "resolved_by":    "Fed pivot signals late 2023",
        "key_lesson": (
            "Duration risk is the hidden variable in growth portfolios. "
            "High P/E multiples are essentially long-duration assets: when "
            "the discount rate rises, the present value of future earnings "
            "collapses mathematically. NVDA fell from $346 to $108. MSFT -30%. "
            "Firms that pivoted to value/dividends/energy in Q1 2022 outperformed "
            "by 50-60 percentage points over the year."
        ),
        "sectors_with_sensitivity": {
            "Technology":  "CRITICAL — high-multiple duration sensitivity",
            "Cloud & SaaS": "CRITICAL — growth multiples compress fastest",
            "Biotech":     "HIGH — speculative premium evaporates",
            "Financials":  "POSITIVE — net interest margin expansion",
        },
    },
    {
        "event":          "AI Infrastructure Supercycle (2023–2025)",
        "trigger":        "ChatGPT launch Nov 2022, NVDA GPU demand explosion, "
                          "hyperscaler AI capex cycle",
        "sectors_hurt":   [],
        "sectors_helped": ["Semiconductors", "Technology", "Cloud & SaaS",
                           "Defense & Aerospace", "Industrials"],
        "markets_affected": ["US", "Global"],
        "duration_months": 30,   # ongoing
        "max_impact_pct":  None,
        "resolved_by":    "Ongoing",
        "key_lesson": (
            "Generational technology transitions create multi-year sector tailwinds. "
            "NVDA rose from $150 to $950+ (+533%). The 13F institutional-follow "
            "strategy worked perfectly here — institutions were accumulating before "
            "the public understood the scale of the AI buildout. "
            "Key risk going forward: AI capex may plateau when hyperscaler ROI becomes "
            "unclear. Watch NVDA quarterly guidance for demand signals."
        ),
        "sectors_with_sensitivity": {
            "Semiconductors": "DIRECT BENEFICIARY — GPU/HBM/advanced packaging demand",
            "Cloud & SaaS":   "BENEFICIARY — inference and API demand",
            "Technology":     "BROAD BENEFICIARY — AI integration across software",
        },
    },
    {
        "event":          "US-China Semiconductor Export Controls (2022–2025)",
        "trigger":        "BIS export controls on advanced semiconductors to China (Oct 2022, expanded 2023)",
        "sectors_hurt":   ["Semiconductors"],
        "sectors_helped": ["Defense & Aerospace"],
        "markets_affected": ["US", "China", "Taiwan"],
        "duration_months": 36,   # ongoing
        "max_impact_pct":  -15,  # estimated revenue impact on China-exposed names
        "resolved_by":    "Escalating — not resolved",
        "key_lesson": (
            "NVDA's China revenue (previously ~25% of data center revenue) "
            "was severely restricted. ASML and TSMC face ongoing export restrictions. "
            "This created a permanent structural risk for semiconductor companies with "
            "China exposure. However, the domestic AI demand more than offset the "
            "China restriction in 2023-2025. For future cycles: assess China revenue "
            "percentage when evaluating semiconductor conviction."
        ),
        "sectors_with_sensitivity": {
            "Semiconductors": "HIGH — ongoing China revenue headwind for NVDA, QCOM, etc.",
            "Technology":     "MODERATE — downstream supply chain complexity",
        },
    },
]


# =============================================================================
# POLICY KNOWLEDGE BASE — for Dr. Okafor
# =============================================================================

POLICY_ENVIRONMENT_HISTORY = [
    {
        "period":    "QE Era / Zero Rates (2009–2021)",
        "policy":    "Fed funds 0-0.25%, QE1/2/3/4, fiscal stimulus",
        "winners":   ["Technology", "Growth stocks", "Real Estate", "Consumer Discretionary"],
        "losers":    ["Financials (NIM compression)", "Consumer Staples (yield irrelevant)"],
        "lesson":    "Zero rates turbocharge growth multiples. Institutions piled into "
                     "high-multiple tech because the discount rate denominator was near zero. "
                     "This is the regime that made the 13F-follow strategy work so well.",
    },
    {
        "period":    "Rate Hike Cycle (2022–2023)",
        "policy":    "Fed tightening 425bps, QT, inflation fight",
        "winners":   ["Financials", "Energy", "Consumer Staples", "Healthcare"],
        "losers":    ["Technology", "Cloud/SaaS", "Biotech", "Real Estate", "Consumer Discretionary"],
        "lesson":    "The single biggest sector rotation in a generation. Duration matters. "
                     "Know your P/E multiples before entering.",
    },
    {
        "period":    "AI Fiscal Cycle (2023–2025)",
        "policy":    "Post-hike plateau, AI infrastructure private investment boom, "
                     "CHIPS Act industrial policy ($52B)",
        "winners":   ["Semiconductors", "Technology", "Industrials (domestic fab)"],
        "losers":    ["Traditional retail", "Legacy telecom"],
        "lesson":    "Industrial policy creates durable multi-year tailwinds. CHIPS Act "
                     "subsidies benefit domestic semiconductor manufacturing (Intel, TSMC US fab). "
                     "Follow the fiscal direction, not just the Fed.",
    },
    {
        "period":    "Trump 2.0 / Tariff Regime (2025–2026)",
        "policy":    "Broad tariff implementation, trade war 2.0, deglobalisation",
        "winners":   ["Defense & Aerospace", "Domestic industrials", "Consumer Staples"],
        "losers":    ["Technology (supply chain)", "Semiconductors (China exposure)", "Retail"],
        "lesson":    "Tariff regimes create durable supply chain re-shoring trends. "
                     "Companies with >20% China revenue face structural multiple compression. "
                     "Defense budgets rise with geopolitical tension.",
    },
]


# =============================================================================
# ANALYST CLASSES
# =============================================================================

class GeopoliticalRiskAnalyst:
    """
    Dr. Sofia Reyes — tracks geopolitical events and their sector impact history.
    """
    name = "Dr. Sofia Reyes — Geopolitical Risk Analyst"

    def analyze(self, assigned_sectors: list[str], regime: dict) -> dict:
        """
        For the assigned sectors, identify geopolitical sensitivity and
        the most relevant current geopolitical risk factors.
        """
        sector_flags:  dict[str, list[str]] = {}
        current_risks: list[dict]           = []

        # Check each assigned sector against event library
        for event in GEOPOLITICAL_EVENT_LIBRARY:
            sensitivity = event.get("sectors_with_sensitivity", {})
            for sector in assigned_sectors:
                if sector in sensitivity:
                    level   = sensitivity[sector]
                    if "BENEFICIARY" in level or "DIRECT" in level or level.startswith("HIGH") or level.startswith("CRITICAL"):
                        if sector not in sector_flags:
                            sector_flags[sector] = []
                        sector_flags[sector].append(
                            f"[{event['event']}] {level}"
                        )

        # Identify currently active geopolitical risks
        active_events = [e for e in GEOPOLITICAL_EVENT_LIBRARY
                         if e.get("resolved_by", "") in ("Unresolved as of 2026", "Ongoing",
                                                          "Escalating — not resolved")]
        for event in active_events:
            relevant_assigned = [s for s in assigned_sectors
                                 if s in event.get("sectors_with_sensitivity", {})]
            if relevant_assigned:
                current_risks.append({
                    "event":              event["event"],
                    "relevant_sectors":   relevant_assigned,
                    "key_lesson":         event["key_lesson"],
                })

        # Overall geopolitical risk assessment
        high_risk_sectors = [s for s, flags in sector_flags.items()
                             if any("CRITICAL" in f or "DIRECT" in f for f in flags)]
        moderate_risk     = [s for s, flags in sector_flags.items()
                             if s not in high_risk_sectors]

        return {
            "analyst":            self.name,
            "sector_geopolitical_flags": sector_flags,
            "active_risk_events": current_risks,
            "high_sensitivity_sectors": high_risk_sectors,
            "moderate_sensitivity_sectors": moderate_risk,
            "no_sensitivity_sectors": [s for s in assigned_sectors if s not in sector_flags],
            "top_risk_note": (
                f"Active geopolitical risks affecting {len(high_risk_sectors)} "
                f"assigned sector(s): {', '.join(high_risk_sectors) or 'None identified'}."
            ) if high_risk_sectors else (
                "No high-sensitivity geopolitical risks identified for assigned sectors. "
                "Current geopolitical environment has limited direct impact on selected research targets."
            ),
        }


class MacroPolicyHistorian:
    """
    Dr. James Okafor — tracks macro/fiscal/monetary policy history
    and its sector impact.
    """
    name = "Dr. James Okafor — Macro Policy Historian"

    def analyze(self, assigned_sectors: list[str], regime: dict) -> dict:
        """
        Map the current policy environment to historical precedents
        and identify which assigned sectors are most aligned / at risk.
        """
        # Read the current CRO bulletin for rate environment context
        cro_risk   = "UNKNOWN"
        rates_env  = regime.get("tlt_trend", "unknown")
        spy_trend  = regime.get("spy_trend", "bull")

        try:
            if os.path.exists("data/risk_bulletin.json"):
                with open("data/risk_bulletin.json") as f:
                    bulletin    = json.load(f)
                    cro_risk    = bulletin.get("risk_level", "UNKNOWN")
        except Exception:
            pass

        # Identify the most relevant current policy period
        if rates_env == "rising_rates":
            current_analogue = next(
                (p for p in POLICY_ENVIRONMENT_HISTORY if "Rate Hike" in p["period"]), None
            )
        elif cro_risk in ("HIGH", "CRITICAL"):
            current_analogue = next(
                (p for p in POLICY_ENVIRONMENT_HISTORY if "Tariff" in p["period"]), None
            )
        else:
            current_analogue = next(
                (p for p in POLICY_ENVIRONMENT_HISTORY if "AI Fiscal" in p["period"]), None
            )

        # Map assigned sectors to policy winners/losers
        policy_winners  = []
        policy_losers   = []
        policy_neutral  = []

        if current_analogue:
            for sector in assigned_sectors:
                in_winners = any(sector in w for w in current_analogue["winners"])
                in_losers  = any(sector in l for l in current_analogue["losers"])
                if in_winners:
                    policy_winners.append(sector)
                elif in_losers:
                    policy_losers.append(sector)
                else:
                    policy_neutral.append(sector)

        # Historical policy cycles affecting assigned sectors
        sector_policy_history = {}
        for sector in assigned_sectors:
            relevant = []
            for period in POLICY_ENVIRONMENT_HISTORY:
                in_w = any(sector in w for w in period["winners"])
                in_l = any(sector in l for l in period["losers"])
                if in_w or in_l:
                    relevant.append({
                        "period":  period["period"],
                        "status":  "WINNER" if in_w else "LOSER",
                        "lesson":  period["lesson"][:150] + "...",
                    })
            sector_policy_history[sector] = relevant

        return {
            "analyst":             self.name,
            "current_policy_analogue": current_analogue["period"] if current_analogue else "Unclear",
            "current_analogue_lesson": current_analogue["lesson"] if current_analogue else "",
            "policy_winners_assigned":  policy_winners,
            "policy_losers_assigned":   policy_losers,
            "policy_neutral_assigned":  policy_neutral,
            "sector_policy_history":    sector_policy_history,
            "rate_environment":         rates_env.replace("_", " "),
            "policy_note": (
                f"Current macro environment most closely resembles: "
                f"'{current_analogue['period'] if current_analogue else 'N/A'}'. "
                f"Assigned sectors aligned with this regime: "
                f"{', '.join(policy_winners) or 'None'}. "
                f"Sectors at risk: {', '.join(policy_losers) or 'None'}."
            ),
        }


# =============================================================================
# TEAM COORDINATOR
# =============================================================================

class GeopoliticalResearchTeam:
    """
    Coordinates the two-analyst geopolitical team.
    Called by the R&D Team Leader during the compile phase.
    """

    def __init__(self):
        self.dr_reyes  = GeopoliticalRiskAnalyst()
        self.dr_okafor = MacroPolicyHistorian()

    def analyze(self, assignments: list[dict], regime: Optional[dict] = None) -> dict:
        """
        Run both analysts and return a combined geopolitical context report.
        """
        if regime is None:
            regime = {}

        logger.info("  [Geopolitical Team] Dr. Reyes — scanning geopolitical risk events...")
        assigned_sectors = [a["sector_name"] for a in assignments]

        geo_analysis    = self.dr_reyes.analyze(assigned_sectors, regime)

        logger.info("  [Geopolitical Team] Dr. Okafor — mapping macro policy history...")
        policy_analysis = self.dr_okafor.analyze(assigned_sectors, regime)

        # Cross-reference: sectors flagged by both analysts as high-risk
        geo_high    = set(geo_analysis.get("high_sensitivity_sectors", []))
        policy_risk = set(policy_analysis.get("policy_losers_assigned", []))
        double_flagged = list(geo_high & policy_risk)

        # Sectors confirmed by both analysts as well-positioned
        geo_clear      = set(geo_analysis.get("no_sensitivity_sectors", []))
        policy_winners = set(policy_analysis.get("policy_winners_assigned", []))
        double_confirmed = list(geo_clear & policy_winners)

        synthesis_notes = []
        if double_flagged:
            synthesis_notes.append(
                f"⚠️  DOUBLE-FLAGGED (both geopolitical AND macro policy risk): "
                f"{', '.join(double_flagged)}. "
                f"Consider reducing conviction threshold for picks in these sectors."
            )
        if double_confirmed:
            synthesis_notes.append(
                f"✓  DOUBLE-CONFIRMED (geopolitically clean AND policy-aligned): "
                f"{', '.join(double_confirmed)}. "
                f"Highest-confidence sectors for the current environment."
            )

        # Team summary for Manager Agent
        combined = {
            "generated_at":       datetime.now().isoformat(),
            "team":               "R&D Geopolitical Research Team",
            "analysts":           [self.dr_reyes.name, self.dr_okafor.name],
            "assigned_sectors":   assigned_sectors,
            "geopolitical_analysis":  geo_analysis,
            "policy_analysis":        policy_analysis,
            "double_flagged_sectors": double_flagged,
            "double_confirmed_sectors": double_confirmed,
            "synthesis_notes":    synthesis_notes,
            "team_summary": (
                f"Geopolitical + macro policy review of {len(assigned_sectors)} assigned sector(s). "
                f"{len(double_confirmed)} sector(s) confirmed as well-positioned. "
                f"{len(double_flagged)} sector(s) carry elevated context risk. "
                f"Policy analogue: {policy_analysis.get('current_policy_analogue', 'N/A')}."
            ),
        }

        # ── Fable 5 geopolitical read (one call/run) ──────────────────────
        # The LLM has actual world knowledge the keyword-based analysts lack.
        try:
            from apex_llm import reason as _r, is_available
            if is_available("rd_geopolitical"):
                raw = _r(
                    "You are a geopolitical risk analyst for a US equity momentum fund.",
                    f"Sectors under research: {', '.join(assigned_sectors)}.\n"
                    f"Double-flagged risky: {double_flagged or 'none'}. "
                    f"Confirmed clean: {double_confirmed or 'none'}.\n"
                    f"Policy analogue: {policy_analysis.get('current_policy_analogue','N/A')}\n"
                    "2 sentences: the single most relevant current geopolitical/policy risk "
                    "for these sectors, and which sector it most affects.",
                    agent_name="rd_geopolitical", max_tokens=130,
                )
                if raw:
                    combined["llm_geo_read"] = raw.strip()
                    synthesis_notes.append(f"[LLM] {raw.strip()}")
        except Exception:
            pass

        logger.info(
            f"  [Geopolitical Team] Complete — "
            f"{len(double_confirmed)} confirmed | {len(double_flagged)} double-flagged"
        )

        os.makedirs("data", exist_ok=True)
        with open(GEOPOLITICAL_CONTEXT_PATH, "w") as f:
            json.dump(combined, f, indent=2, default=str)

        return combined


# =============================================================================
# Standalone entry point
# =============================================================================

if __name__ == "__main__":
    import json as _j
    team        = GeopoliticalResearchTeam()
    assignments = []
    if os.path.exists("data/sector_assignments.json"):
        with open("data/sector_assignments.json") as f:
            d = _j.load(f)
            assignments = d.get("assignments", [])
    result = team.analyze(assignments)
    print(f"\nGeopolitical Team Summary: {result['team_summary']}")
    if result["double_confirmed_sectors"]:
        print(f"✓ Confirmed sectors: {', '.join(result['double_confirmed_sectors'])}")
    if result["double_flagged_sectors"]:
        print(f"⚠ Double-flagged: {', '.join(result['double_flagged_sectors'])}")

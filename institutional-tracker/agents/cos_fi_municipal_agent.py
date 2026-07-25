#!/usr/bin/env python3
"""
APEX CAPITAL -- FI MUNICIPAL BOND AGENT
Lane: fixed_income | Tier: 1 | Team: r_and_d
Specialization: Municipal bonds — tax-free yield opportunities.

Municipal bonds (munis) are debt issued by US states, cities, and local
governments. Interest is exempt from federal income tax (and often state tax).
This agent fires in ALL regimes, not just crisis, because tax-equivalent
yields are often attractive even when corporate spreads are tight.

Key concept: Tax-Equivalent Yield (TEY).
  If a muni yields 3.5% tax-free and you're in the 32% bracket:
  TEY = 3.5% / (1 - 0.32) = 5.15% taxable equivalent.
  That beats most IG corporate bonds at current spreads.

Instruments:
  MUB  — iShares National Muni Bond ETF (broad, national, investment grade)
  VTEB — Vanguard Tax-Exempt Bond ETF (lower expense ratio than MUB)
  HYD  — VanEck High Yield Muni ETF (higher yield, higher risk)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research_team import SectorResearchAgent

# Approximate national muni yield vs 10yr Treasury ratio thresholds
# Muni-to-Treasury (M/T) ratio: normal = 85%, attractive = >90%, rich = <75%
MUNI_TEY_MIN_ADVANTAGE_BPS = 30   # TEY must beat 10yr by at least 30bps to recommend
MUNI_STRONG_ADVANTAGE_BPS  = 80   # STRONG_BUY if TEY beats 10yr by 80+ bps

# Assumed marginal tax bracket for Apex Capital (institutional rate)
# Note: paper trading uses this as a proxy for evaluating attractiveness
ASSUMED_TAX_RATE = 0.32


class FIMunicipalAgent(SectorResearchAgent):
    """
    Identifies tax-advantaged municipal bond ETF opportunities.
    Active in ALL market regimes — not dependent on credit spread width.
    Compares tax-equivalent muni yield vs comparable Treasury yield.
    """

    KNOWLEDGE_BASE = {
        "frameworks": [
            "Tax-Equivalent Yield (TEY) = Muni Yield / (1 - marginal_tax_rate). "
            "TEY > comparable Treasury → munis more attractive on after-tax basis.",
            "M/T ratio (Muni/Treasury) < 85% historically means munis are cheap vs Treasuries.",
            "High-yield munis (HYD) carry higher default risk but rarely default: "
            "historically <1% default rate vs 3-4% for corporate HY.",
            "Muni yields are driven by supply (issuance calendar) and demand (tax policy). "
            "Major supply months: Jan, Apr, Sep — can create short-term cheapening.",
            "TIPS vs munis: TIPS protect against inflation, munis offer tax efficiency. "
            "Different tools for different goals.",
        ],
        "watchlist_etfs": ["MUB", "VTEB", "HYD", "HYMB"],
        "signal_thresholds": {
            "mt_ratio_cheap":      90,    # M/T ratio above this = munis cheap
            "mt_ratio_very_cheap": 95,    # above this = very cheap, STRONG_BUY
            "mt_ratio_rich":       75,    # below this = munis expensive
        },
    }

    def __init__(self, agent_id: str = "fi_municipal_agent", spread_context: dict = None,
                 macro_context: dict = None):
        self._spread_context = spread_context or {}
        self._macro_context  = macro_context  or {}
        super().__init__({
            "agent_id":           agent_id,
            "sector_name":        "Fixed-Income-Municipal",
            "sector_etf":         "MUB",
            "description":        "Municipal bond tax-equivalent yield analysis agent",
            "watchlist":          ["MUB", "VTEB", "HYD"],
            "investment_horizon": "medium",
            "leader_rationale":   (
                "Buy municipal bond ETFs when their tax-equivalent yield exceeds "
                "comparable Treasuries by 30+ bps. Active in all market regimes."
            ),
            "lane":               "fixed_income",
            "tier":               1,
        })

    def run(self) -> dict:
        t10    = self._spread_context.get("treasury_10yr", 4.3)
        regime = self._spread_context.get("spread_regime", "NORMAL")
        macro  = (self._macro_context.get("market_regime") or
                  self._macro_context.get("regime") or "UNKNOWN").upper()
        recommendations = []

        # Approximate 10yr national AAA muni yield
        # M/T ratio historically 85%. In TIGHT credit, demand pushes munis rich (~78%).
        # In BEAR/stress, munis can cheapen (supply > demand) to 90-95%.
        if macro in ("BEAR", "DEFENSIVE", "RISK_OFF"):
            mt_ratio_est = 93.0   # munis tend to cheapen in risk-off
        elif regime in ("WIDE", "DISTRESSED"):
            mt_ratio_est = 91.0
        elif regime == "TIGHT":
            mt_ratio_est = 80.0   # munis tend to be rich when credit is in demand
        else:
            mt_ratio_est = 85.0   # NORMAL

        muni_yield_est = (t10 * mt_ratio_est / 100)

        # Tax-equivalent yield: what taxable bond must yield to match muni after-tax
        tey = muni_yield_est / (1 - ASSUMED_TAX_RATE)
        tey_advantage_bps = (tey - t10) * 100   # how much TEY beats the 10yr Treasury

        verdict = None
        conviction = 0

        if mt_ratio_est >= 95 or tey_advantage_bps >= MUNI_STRONG_ADVANTAGE_BPS:
            verdict    = "STRONG_BUY"
            conviction = 5
        elif tey_advantage_bps >= MUNI_TEY_MIN_ADVANTAGE_BPS:
            verdict    = "BUY"
            conviction = 3
        elif tey_advantage_bps >= 0:
            # TEY slightly above Treasury but below 30bps threshold — marginal
            verdict    = "HOLD"
            conviction = 1

        if verdict in ("STRONG_BUY", "BUY"):
            # Primary: broad national muni ETF
            recommendations.append({
                "ticker":     "VTEB",
                "category":   "etf",
                "conviction": conviction,
                "verdict":    verdict,
                "reason":     (
                    f"Muni TEY={tey:.2f}% vs 10yr Treasury={t10:.2f}% "
                    f"(+{tey_advantage_bps:.0f}bps tax advantage). "
                    f"Est M/T ratio={mt_ratio_est:.0f}%. Regime={regime}, Macro={macro}. "
                    f"Tax-free yield attractive on after-tax basis vs comparable Treasuries. "
                    f"VTEB: lower expense ratio (0.05%) vs MUB (0.07%)."
                ),
                "agent":      "FIMunicipalAgent",
                "muni_yield_est":     round(muni_yield_est, 3),
                "tax_equivalent_yield": round(tey, 3),
                "mt_ratio_est":       mt_ratio_est,
            })

            # In stress/BEAR add HYD for higher yield (if risk budget allows)
            if conviction >= 4 and macro in ("BEAR", "DEFENSIVE", "RISK_OFF"):
                recommendations.append({
                    "ticker":     "HYD",
                    "category":   "etf",
                    "conviction": 3,
                    "verdict":    "BUY",
                    "reason":     (
                        f"High-yield munis (HYD) cheapening in {macro} regime. "
                        f"Historical muni default rate <1% even in recessions — "
                        "far lower than corporate HY. Adds yield without proportional default risk."
                    ),
                    "agent":      "FIMunicipalAgent",
                })

        return {
            "agent":                  "FIMunicipalAgent",
            "lane":                   "fixed_income",
            "muni_yield_est":         round(muni_yield_est, 3),
            "tax_equivalent_yield":   round(tey, 3),
            "tey_advantage_bps":      round(tey_advantage_bps, 1),
            "mt_ratio_est":           mt_ratio_est,
            "treasury_10yr":          t10,
            "regime":                 regime,
            "macro":                  macro,
            "recommendations":        recommendations,
        }

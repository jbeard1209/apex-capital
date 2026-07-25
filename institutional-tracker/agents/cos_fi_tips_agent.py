#!/usr/bin/env python3
"""
APEX CAPITAL -- FI TIPS / REAL YIELD AGENT
Lane: fixed_income | Tier: 1 | Team: r_and_d
Specialization: TIPS (Treasury Inflation-Protected Securities) and real yield analysis.

TIPS are US Treasury bonds whose principal adjusts with CPI inflation.
The "real yield" = nominal yield minus inflation expectations.
A positive real yield means you're earning a return ABOVE inflation.

Key instruments:
  TIP  — iShares TIPS Bond ETF (7-8yr average duration)
  SCHP — Schwab U.S. TIPS ETF (lower cost, similar duration)
  STIP — Short-Term TIPS ETF (0-5yr, lower duration risk)

When to buy:
  Real yield > 1.5% = historically attractive entry (TIPS undervalued)
  Real yield > 2.0% = STRONG_BUY (rarely seen outside of hawkish Fed cycles)
  Real yield < 0.5% = HOLD/AVOID (TIPS expensive, market pricing high inflation)

Current context (May 2026):
  10yr nominal = 4.57%, implied inflation ~2.5% → real yield ~2.07%
  This is historically attractive. Post-2008, real yields were negative for years.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research_team import SectorResearchAgent

# Real yield thresholds
REAL_YIELD_STRONG_BUY  = 2.0   # %: exceptional — rarely seen outside crisis
REAL_YIELD_BUY         = 1.5   # %: attractive — historically good entry
REAL_YIELD_HOLD        = 0.5   # %: marginal — borderline
# Below HOLD threshold → TIPS are expensive (market expects high inflation)

# Inflation estimate used when FRED breakeven data unavailable
INFLATION_ESTIMATE_PCT = 2.5   # long-run Fed inflation target


class FITIPSAgent(SectorResearchAgent):
    """
    Identifies TIPS buy opportunities based on real yield.
    Active in all regimes. Particularly valuable during inflationary cycles
    and when nominal rates are elevated (real yields positive).
    """

    KNOWLEDGE_BASE = {
        "frameworks": [
            "Real yield = 10yr nominal yield - 10yr breakeven inflation rate "
            "(from FRED: T10YIE). Positive and rising real yields = TIPS attractive.",
            "TIPS protect purchasing power: if inflation is 4% but TIPS real yield is 2%, "
            "total return = 6%. If inflation 0%, return = 2%.",
            "Duration risk: TIP has ~7yr duration. When rates RISE 1%, TIP falls ~7%. "
            "STIP (short-term TIPS) has ~2.5yr duration — less rate risk.",
            "Historical real yield context: 2012-2021 real yields were negative (-1% to -0.5%). "
            "2022-2024 Fed tightening drove real yields to +2%+ = once-per-decade entry.",
            "TIPS vs nominal: buy TIPS when you expect inflation to exceed market expectations. "
            "Buy nominal Treasuries (IEF/TLT) when you expect inflation to undershoot.",
        ],
        "watchlist_etfs": ["TIP", "SCHP", "STIP"],
    }

    def __init__(self, agent_id: str = "fi_tips_agent", spread_context: dict = None,
                 macro_context: dict = None):
        self._spread_context = spread_context or {}
        self._macro_context  = macro_context  or {}
        super().__init__({
            "agent_id":           agent_id,
            "sector_name":        "Fixed-Income-TIPS",
            "sector_etf":         "TIP",
            "description":        "TIPS real yield and inflation protection analyst",
            "watchlist":          ["TIP", "SCHP", "STIP"],
            "investment_horizon": "long",
            "leader_rationale":   (
                "Buy TIPS when real yield > 1.5%. "
                "Strong buy when real yield > 2.0% — historically rare and attractive."
            ),
            "lane":               "fixed_income",
            "tier":               1,
        })

    def _fetch_real_yield(self) -> float:
        """Try to get 10yr TIPS real yield from FRED (DFII10). Fallback to estimate."""
        try:
            import requests, io, pandas as pd
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text), names=["date", "value"], header=0, na_values=".")
            val = df.dropna(subset=["value"])["value"].iloc[-1]
            return float(val)
        except Exception:
            # Fallback: estimate from nominal - inflation
            t10 = self._spread_context.get("treasury_10yr", 4.3)
            return t10 - INFLATION_ESTIMATE_PCT

    def run(self) -> dict:
        t10    = self._spread_context.get("treasury_10yr", 4.3)
        ff     = self._spread_context.get("fed_funds", 3.64)
        regime = self._spread_context.get("spread_regime", "NORMAL")
        macro  = (self._macro_context.get("market_regime") or
                  self._macro_context.get("regime") or "UNKNOWN").upper()

        real_yield = self._fetch_real_yield()
        # Infer breakeven inflation
        breakeven_inflation = t10 - real_yield

        recommendations = []

        if real_yield >= REAL_YIELD_STRONG_BUY:
            verdict    = "STRONG_BUY"
            conviction = 5
        elif real_yield >= REAL_YIELD_BUY:
            verdict    = "BUY"
            conviction = 4
        elif real_yield >= REAL_YIELD_HOLD:
            verdict    = "BUY"   # marginal but positive real yield still worth a small position
            conviction = 2
        else:
            verdict    = "HOLD"
            conviction = 0

        if conviction >= 2:
            reason_str = (
                f"10yr TIPS real yield = {real_yield:.2f}% "
                f"(nominal {t10:.2f}% minus est. breakeven inflation {breakeven_inflation:.2f}%). "
            )
            if real_yield >= REAL_YIELD_STRONG_BUY:
                reason_str += (
                    "EXCEPTIONAL real yield — historically seen only during peak Fed tightening cycles. "
                    "2022-2023 real yields at 2%+ preceded strong TIPS returns as inflation moderated."
                )
            else:
                reason_str += (
                    "Positive real yield means after-inflation return is locked in. "
                    "TIP provides inflation hedge + income in a single instrument."
                )
            reason_str += f" Regime={regime}, Macro={macro}."

            # Primary: broad TIPS ETF
            recommendations.append({
                "ticker":     "SCHP",
                "category":   "etf",
                "conviction": conviction,
                "verdict":    verdict,
                "reason":     reason_str + " SCHP chosen for lower expense ratio (0.03%) vs TIP (0.19%).",
                "agent":      "FITIPSAgent",
                "real_yield":          round(real_yield, 3),
                "breakeven_inflation": round(breakeven_inflation, 3),
            })

            # Short-term TIPS hedge in BEAR macro
            if macro in ("BEAR", "DEFENSIVE", "RISK_OFF") and conviction >= 3:
                recommendations.append({
                    "ticker":     "STIP",
                    "category":   "etf",
                    "conviction": 3,
                    "verdict":    "BUY",
                    "reason":     (
                        f"STIP (0-5yr TIPS) as low-duration inflation hedge in {macro} regime. "
                        "Shorter duration than SCHP means less price drop if rates rise further. "
                        f"Real yield still positive at ~{real_yield - 0.3:.2f}% short end."
                    ),
                    "agent":      "FITIPSAgent",
                    "real_yield": round(real_yield, 3),
                })

        return {
            "agent":                "FITIPSAgent",
            "lane":                 "fixed_income",
            "real_yield":           round(real_yield, 3),
            "breakeven_inflation":  round(breakeven_inflation, 3),
            "treasury_10yr":        t10,
            "fed_funds":            ff,
            "regime":               regime,
            "macro":                macro,
            "recommendations":      recommendations,
        }

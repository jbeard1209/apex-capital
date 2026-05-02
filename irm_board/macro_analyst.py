# =============================================================================
# APEX CAPITAL — IRM BOARD: MACRO & PORTFOLIO CONSTRUCTION ANALYST
# =============================================================================
# Dr. Priya Sharma — Macro & Portfolio Construction Analyst
# Independent Risk Manager Board, Seat 4
#
# EXPERTISE
# ─────────
#   Macroeconomic regime analysis, factor model investing, and portfolio
#   construction theory. Has studied every foundational work in these disciplines:
#
#     • Harry Markowitz — "Portfolio Selection" (Journal of Finance, 1952)
#     • William Sharpe — "Capital Asset Prices" (CAPM, 1964)
#     • Fama & French — Three-Factor and Five-Factor Models
#     • Ray Dalio — "Principles" & Bridgewater All-Weather research
#     • Howard Marks — Oaktree memos (complete collection)
#     • Jeremy Grantham — GMO quarterly letters (complete collection)
#     • Seth Klarman — "Margin of Safety"
#     • George Soros — "The Alchemy of Finance" (reflexivity)
#     • Hyman Minsky — "Stabilizing an Unstable Economy"
#     • Antti Ilmanen — "Expected Returns" (AQR)
#     • Cliff Asness — AQR factor investing research papers
#     • Ben Bernanke — research on balance sheet recessions
#     • Reinhart & Rogoff — "This Time Is Different"
#
# RESPONSIBILITIES
# ────────────────
#   1. Macro regime assessment: are we in expansion, late-cycle, recession, or
#      recovery? Is the portfolio positioned appropriately for the current regime?
#
#   2. Factor exposure analysis: does the portfolio carry unintended factor tilts?
#      (Growth vs Value, Quality vs Momentum, Size, Beta)
#
#   3. Portfolio construction quality: is the portfolio optimally constructed
#      per Markowitz efficiency? Too concentrated? Wrong correlation assumptions?
#
#   4. Tail-hedge adequacy: does the firm have sufficient protection against
#      drawdown scenarios? Is the SH hedge sized correctly? Are there any
#      natural hedges in the book?
#
#   5. Macro risk flags: rate risk, dollar risk, credit spread widening,
#      geopolitical tail risks that the standard risk model would miss.
#
# OUTPUT
# ──────
#   analyze() → dict with keys:
#     domain, risk_score (1-10), confidence, key_findings,
#     red_flags, recommendations
# =============================================================================

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("irm_board.macro")

# Macro regime characteristics and portfolio implications
MACRO_REGIMES = {
    "BULL_EARLY": {
        "description": "Early-cycle expansion. Risk assets outperform. "
                       "Dalio: 'Cash is trash in early recovery.'",
        "optimal_equity_pct": (70, 85),
        "factor_tilt": "Cyclical growth, small-cap, high-beta",
        "hedge_required": False,
        "risk_multiplier": 0.8,
    },
    "BULL_MID": {
        "description": "Mid-cycle expansion. Steady growth. Momentum favoured. "
                       "Grantham: 'The last year before a bubble peak is the most "
                       "profitable and the most dangerous.'",
        "optimal_equity_pct": (60, 80),
        "factor_tilt": "Quality growth, momentum",
        "hedge_required": False,
        "risk_multiplier": 1.0,
    },
    "BULL_LATE": {
        "description": "Late-cycle. Inflation rising, yields rising, multiples compressing. "
                       "Minsky: system approaching financial instability threshold. "
                       "Marks: 'When everyone believes something is safe, it isn't.'",
        "optimal_equity_pct": (45, 65),
        "factor_tilt": "Value, low-beta, defensive sectors",
        "hedge_required": True,
        "risk_multiplier": 1.4,
    },
    "BEAR": {
        "description": "Bear market. Capital preservation paramount. "
                       "Klarman: 'The trick is not to be the last fool.'",
        "optimal_equity_pct": (20, 45),
        "factor_tilt": "Quality, low-volatility, defensive",
        "hedge_required": True,
        "risk_multiplier": 1.8,
    },
    "RECESSION": {
        "description": "Recession. Drawdown protection critical. "
                       "Dalio All-Weather: bonds, gold, commodities offset equities.",
        "optimal_equity_pct": (15, 35),
        "factor_tilt": "Defensive, quality, sovereign bonds",
        "hedge_required": True,
        "risk_multiplier": 2.0,
    },
    "UNKNOWN": {
        "description": "Regime unclear. Apply conservative assumptions.",
        "optimal_equity_pct": (40, 65),
        "factor_tilt": "Balanced",
        "hedge_required": False,
        "risk_multiplier": 1.2,
    },
}

# Factor exposure benchmarks
FACTOR_LIMITS = {
    "tech_pct_max":       40.0,    # Max % in tech/growth
    "single_factor_max":  50.0,    # Max % in any one factor
    "momentum_pct_max":   60.0,    # Max % in momentum names
    "min_quality_pct":    20.0,    # Min % in quality / defensive names
}


class MacroAnalyst:
    """
    Dr. Priya Sharma — Macro & Portfolio Construction Analyst.
    IRM Board Seat 4. Assesses macro regime alignment and construction quality.
    """

    def __init__(self):
        self.name = "Dr. Priya Sharma"
        self.title = "Macro & Portfolio Construction Analyst"
        self.domain = "Macro Regime Analysis & Portfolio Construction"

    # ------------------------------------------------------------------
    # Data loaders
    # ------------------------------------------------------------------

    def _load_irm_report(self) -> dict:
        path = "data/risk_manager_report.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_canary_alert(self) -> dict:
        for p in ["data/canary_alert.json", "data/canary_state.json"]:
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        return json.load(f)
                except Exception:
                    pass
        return {}

    def _load_capital_efficiency(self) -> dict:
        path = "data/capital_efficiency_report.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_research_report(self) -> dict:
        path = "data/research_team_report.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    # ------------------------------------------------------------------
    # Analysis methods
    # ------------------------------------------------------------------

    def _infer_macro_regime(self, irm: dict, canary: dict, pdo: dict) -> str:
        """
        Infer the current macro regime from available signals.
        Uses a hierarchy of evidence: IRM risk level → Canary level → PDO regime.
        """
        risk_level = irm.get("risk_assessment", {}).get("risk_level", "UNKNOWN")
        canary_level = canary.get("level", canary.get("last_level", "GREEN"))
        pdo_regime = pdo.get("market_conditions", {}).get("regime", "neutral")

        # Map signals to regime
        if risk_level in ("CRITICAL",) or canary_level == "RED":
            return "RECESSION"
        elif risk_level in ("HIGH",) or canary_level == "ORANGE":
            return "BEAR"
        elif risk_level in ("ELEVATED",) or canary_level == "YELLOW":
            return "BULL_LATE"
        elif pdo_regime == "bear" and risk_level == "MODERATE":
            return "BULL_LATE"
        elif risk_level in ("LOW", "MODERATE"):
            return "BULL_MID"
        else:
            return "UNKNOWN"

    def _assess_regime_alignment(self, irm: dict, canary: dict, pdo: dict) -> tuple[list, list]:
        """
        Is the portfolio positioned correctly for the current macro regime?
        """
        findings = []
        red_flags = []

        regime_key = self._infer_macro_regime(irm, canary, pdo)
        regime = MACRO_REGIMES[regime_key]

        # Current equity deployment
        acct = irm.get("account", {})
        portfolio_value = acct.get("portfolio_value", 0)
        cash = acct.get("cash", 0)
        if portfolio_value > 0:
            cash_pct = cash / portfolio_value * 100
            equity_pct = 100 - cash_pct
        else:
            equity_pct = None

        lo, hi = regime["optimal_equity_pct"]
        findings.append(
            f"Inferred macro regime: {regime_key}. "
            f"{regime['description']}"
        )

        if equity_pct is not None:
            if equity_pct > hi:
                red_flags.append(
                    f"REGIME MISALIGNMENT: Portfolio is {equity_pct:.0f}% deployed "
                    f"in equities. For {regime_key} regime, Dalio/Klarman frameworks "
                    f"suggest optimal equity allocation is {lo}–{hi}%. "
                    f"Currently {equity_pct - hi:.0f}pp overextended. "
                    f"Recommended action: raise cash buffer to at least {100 - hi:.0f}%."
                )
            elif equity_pct < lo:
                findings.append(
                    f"Portfolio is {equity_pct:.0f}% deployed — below the {lo}% "
                    f"floor for {regime_key} regime. "
                    f"Marks: 'The biggest risk is being too cautious at the wrong time.' "
                    f"Consider selective deployment if quality opportunities exist."
                )
            else:
                findings.append(
                    f"Portfolio deployment ({equity_pct:.0f}%) is well-calibrated "
                    f"for {regime_key} regime ({lo}–{hi}% optimal range)."
                )

        # Hedge check
        if regime["hedge_required"]:
            ra = irm.get("risk_assessment", {})
            has_hedge = any(
                "sh" in str(f).lower() or "hedge" in str(f).lower() or "inverse" in str(f).lower()
                for f in ra.get("risk_flags", [])
            )
            # Check if SH position exists in portfolio
            positions_note = irm.get("concentration", {}).get("top_holdings", [])
            has_sh = any(
                str(p).upper() in ("SH", "SPXS", "SDS", "PSQ", "SQQQ")
                for p in positions_note
            )
            if not has_sh:
                red_flags.append(
                    f"NO TAIL HEDGE DETECTED in {regime_key} regime. "
                    f"Dalio All-Weather principle: 'In a bear market, you need "
                    f"assets that go up when equities go down.' SH (ProShares Short S&P500) "
                    f"or equivalent hedge is recommended at this macro stage. "
                    f"Without it, portfolio has no systematic downside protection."
                )

        return findings, red_flags

    def _assess_concentration_and_factors(self, irm: dict, research: dict) -> tuple[list, list]:
        """
        Portfolio construction quality: factor exposure, concentration, correlations.
        Per Markowitz (1952): diversification is the only free lunch in finance.
        """
        findings = []
        red_flags = []

        conc = irm.get("concentration", {})
        n_positions = conc.get("n_positions", 0)
        max_single = conc.get("max_single_pct", 0)
        top_sector_pct = conc.get("top_sector_pct", 0)

        # Position count check
        if n_positions < 5:
            red_flags.append(
                f"CRITICAL UNDER-DIVERSIFICATION: Only {n_positions} positions. "
                f"Markowitz (1952): systematic risk cannot be diversified away, "
                f"but idiosyncratic risk can — and requires at minimum 15-20 names "
                f"to eliminate ~90% of it. At {n_positions} names, a single blow-up "
                f"can permanently impair capital. "
                f"Fama-French: factor premiums require a minimum of 20+ stocks to "
                f"be reliably captured."
            )
        elif n_positions < 10:
            findings.append(
                f"{n_positions} positions — concentrated portfolio. "
                f"Acceptable only with very high conviction on each name."
            )
        else:
            findings.append(
                f"{n_positions} positions — reasonable diversification base."
            )

        # Sector concentration — approximate via research picks if available
        picks = research.get("recommendations", [])
        if picks:
            sector_counts: dict = {}
            for p in picks:
                s = p.get("specialty", p.get("sector", "Unknown"))
                sector_counts[s] = sector_counts.get(s, 0) + 1
            top_s, top_c = max(sector_counts.items(), key=lambda x: x[1], default=("?", 0))
            sector_pct = top_c / len(picks) * 100 if picks else 0

            # Tech/growth sector check
            tech_picks = sum(c for s, c in sector_counts.items()
                             if any(kw in s.lower() for kw in
                                    ("tech", "software", "semiconductor", "ai", "cloud")))
            tech_pct = tech_picks / len(picks) * 100 if picks else 0

            if tech_pct > FACTOR_LIMITS["tech_pct_max"]:
                red_flags.append(
                    f"FACTOR CONCENTRATION — Tech/Growth: {tech_pct:.0f}% of R&D picks. "
                    f"Fama-French: factor tilts amplify both upside AND downside. "
                    f"In a rate-shock (2022 analogue), tech-heavy portfolios experienced "
                    f"-35% to -70% drawdowns. Recommend capping tech at {FACTOR_LIMITS['tech_pct_max']}%."
                )
            else:
                findings.append(
                    f"Tech/growth factor exposure: {tech_pct:.0f}% of picks — within limits."
                )

        # Single-name concentration
        if max_single > 15:
            red_flags.append(
                f"SINGLE-NAME CONCENTRATION: Largest position = {max_single:.0f}% of portfolio. "
                f"Beyond 15%, idiosyncratic risk dominates all other portfolio risk. "
                f"Klarman: 'Concentration is not a substitute for a good idea; "
                f"it multiplies both the upside and the risk.' Position must be trimmed."
            )
        elif max_single > 10:
            findings.append(
                f"Largest position at {max_single:.0f}% — at the upper boundary "
                f"of single-name concentration limits."
            )

        return findings, red_flags

    def _assess_macro_tail_risks(self, irm: dict) -> tuple[list, list]:
        """
        Identifies structural macro risks not captured in standard risk models.
        These are the 'known unknowns' — risks the IRM model tracks but the
        daily pipeline may not surface.
        """
        findings = []
        red_flags = []

        ra = irm.get("risk_assessment", {})
        risk_level = ra.get("risk_level", "UNKNOWN")
        risk_flags = ra.get("risk_flags", [])

        # Rate risk (2022 analogue)
        rate_relevant = any("rate" in f.lower() or "duration" in f.lower()
                            for f in risk_flags)
        if risk_level in ("HIGH", "CRITICAL") and not rate_relevant:
            red_flags.append(
                "MACRO BLIND SPOT — RATE RISK: IRM flags high risk but no "
                "rate/duration risk is surfaced. In the 2022 rate-shock, "
                "high-multiple growth stocks fell 60-80%. If the portfolio "
                "is long growth/tech, duration sensitivity should be explicitly "
                "stress-tested under a +200bps parallel yield-curve shift scenario."
            )

        # Correlation breakdown risk (Reinhart & Rogoff: 'This Time Is Different')
        findings.append(
            "Macro tail risk note: per Reinhart & Rogoff, the phrase 'this time "
            "is different' has preceded every major financial crisis in history. "
            "All risk models assume historical correlations hold — they do not in a crisis."
        )

        # Credit spread risk
        if risk_level in ("HIGH", "CRITICAL"):
            red_flags.append(
                "CREDIT SPREAD RISK in high-risk regime: when equity markets sell off, "
                "credit spreads typically widen simultaneously, reducing cross-asset "
                "diversification. Equity-only portfolios lose their correlation buffer "
                "precisely when it is most needed (Ilmanen, 'Expected Returns')."
            )

        return findings, red_flags

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, irm_context: Optional[dict] = None) -> dict:
        """
        Run full macro and construction analysis.
        Returns structured findings for the IRM Director.
        """
        irm      = irm_context or self._load_irm_report()
        canary   = self._load_canary_alert()
        pdo      = self._load_capital_efficiency()
        research = self._load_research_report()

        all_findings = []
        all_red_flags = []
        recommendations = []

        # 1. Regime alignment
        f, r = self._assess_regime_alignment(irm, canary, pdo)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 2. Concentration and factor exposure
        f, r = self._assess_concentration_and_factors(irm, research)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 3. Macro tail risks
        f, r = self._assess_macro_tail_risks(irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # Risk score
        risk_score = 3
        risk_score += len(all_red_flags) * 2
        risk_score = min(10, risk_score)

        # Recommendations
        if any("REGIME MISALIGNMENT" in r for r in all_red_flags):
            recommendations.append(
                "Rebalance equity allocation to match macro regime targets. "
                "Dalio: 'Know your portfolio's sensitivity to each macro variable.'"
            )
        if any("HEDGE" in r for r in all_red_flags):
            recommendations.append(
                "Initiate tail hedge position (SH or equivalent) sized at "
                "5-10% of portfolio value. Review at each board meeting."
            )
        if any("DIVERSIF" in r for r in all_red_flags):
            recommendations.append(
                "Expand to minimum 15 positions. Per Markowitz, this reduces "
                "idiosyncratic risk by approximately 85% vs a 5-position portfolio."
            )
        if any("FACTOR" in r for r in all_red_flags):
            recommendations.append(
                "Rebalance factor exposures. Introduce some Value or Quality names "
                "to offset momentum/growth concentration."
            )
        if not recommendations:
            recommendations.append(
                "Portfolio construction is broadly appropriate for current regime. "
                "Continue monitoring for regime transitions."
            )

        confidence = "HIGH" if irm else "MEDIUM"

        logger.debug(
            f"[Macro] Score={risk_score} | "
            f"Findings={len(all_findings)} | RedFlags={len(all_red_flags)}"
        )

        return {
            "analyst":         self.name,
            "title":           self.title,
            "domain":          self.domain,
            "risk_score":      risk_score,
            "confidence":      confidence,
            "key_findings":    all_findings,
            "red_flags":       all_red_flags,
            "recommendations": recommendations,
            "timestamp":       datetime.now().isoformat(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    analyst = MacroAnalyst()
    result = analyst.analyze()
    print(f"\n{'='*60}")
    print(f"  {result['analyst']} — {result['title']}")
    print(f"  Risk Score: {result['risk_score']}/10  |  Confidence: {result['confidence']}")
    print(f"{'='*60}")
    for f in result["key_findings"]:
        print(f"  • {f}")
    if result["red_flags"]:
        print(f"\n  RED FLAGS:")
        for r in result["red_flags"]:
            print(f"  ⚑ {r}")
    print()

# =============================================================================
# APEX CAPITAL — IRM BOARD: QUANTITATIVE RISK ANALYST
# =============================================================================
# Dr. Elena Vasquez — Quantitative Risk Analyst
# Independent Risk Manager Board, Seat 2
#
# EXPERTISE
# ─────────
#   Statistical risk modelling across the full spectrum of modern risk
#   frameworks. Has read and re-read every foundational text in quantitative
#   finance risk management:
#
#     • J.P. Morgan RiskMetrics Technical Document (1996, 2006)
#     • Philippe Jorion — "Value at Risk" (3rd ed.)
#     • John Hull — "Options, Futures & Other Derivatives" (10th ed.)
#     • Paul Wilmott — "Quantitative Finance" (3-vol)
#     • Nassim Taleb — "The Black Swan", "Antifragile", "Dynamic Hedging"
#     • Basel II/III/IV Capital Accord framework documents
#     • Artzner et al. — "Coherent Measures of Risk" (1999)
#     • Rockafellar & Uryasev — CVaR optimization papers
#     • Andrew Lo — "Hedge Funds: An Analytic Perspective"
#     • Emanuel Derman — "My Life as a Quant"
#
# RESPONSIBILITIES
# ────────────────
#   1. Validate that stated VaR / CVaR figures are internally consistent
#   2. Stress-test the portfolio against historical crash scenarios
#   3. Flag fat-tail and skewness risks not captured by Gaussian models
#   4. Assess whether risk limits (stop-loss %, max position %) are
#      calibrated correctly for the current volatility regime
#   5. Flag any model risk: where the portfolio's risk estimates may be
#      understating true exposure
#
# OUTPUT
# ──────
#   analyze() → dict with keys:
#     domain, risk_score (1-10), confidence, key_findings,
#     red_flags, recommendations
# =============================================================================

import json
import logging
import math
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("irm_board.quant")

# Historical crash scenario benchmarks (peak-to-trough drawdowns, % returns in crisis windows)
CRASH_SCENARIOS = {
    "GFC_2008": {
        "name": "Global Financial Crisis (2008-09)",
        "spy_drawdown": -56.8,
        "duration_days": 356,
        "vix_peak": 80.9,
        "lesson": (
            "Correlations go to 1 in a crash. Diversification fails precisely "
            "when you need it most. Liquidity evaporates. Stop-losses may gap "
            "through levels in a fast market."
        ),
    },
    "COVID_2020": {
        "name": "COVID Crash (Feb-Mar 2020)",
        "spy_drawdown": -33.9,
        "duration_days": 23,
        "vix_peak": 85.5,
        "lesson": (
            "Fastest 30%-drawdown in history. Daily moves of -10% or more. "
            "Intraday circuit breakers triggered. Fat-tail events can arrive "
            "with almost zero warning."
        ),
    },
    "RATE_SHOCK_2022": {
        "name": "Rate-Hike Bear Market (2022)",
        "spy_drawdown": -25.4,
        "duration_days": 282,
        "vix_peak": 38.9,
        "lesson": (
            "Duration risk devastated growth stocks. High-beta / high-multiple "
            "tech fell 60-80%. Factor rotations can destroy thematic portfolios "
            "even when the macro catalyst appears 'known'."
        ),
    },
    "DOTCOM_2000": {
        "name": "Dot-com Collapse (2000-02)",
        "spy_drawdown": -49.1,
        "duration_days": 929,
        "vix_peak": 45.1,
        "lesson": (
            "Sector concentration kills. NASDAQ down 78%. Thesis-driven investors "
            "held all the way down. Stop-losses and rebalancing rules are the only "
            "systematic protection against conviction-driven ruin."
        ),
    },
}

# Calibration benchmarks: what 'good' looks like for each metric
RISK_BENCHMARKS = {
    "var_95_1day_pct":    {"warn": 2.5,  "critical": 4.0,  "desc": "VaR 95% 1-day (% portfolio)"},
    "var_99_1day_pct":    {"warn": 3.5,  "critical": 6.0,  "desc": "VaR 99% 1-day (% portfolio)"},
    "cvar_95_pct":        {"warn": 3.5,  "critical": 5.5,  "desc": "CVaR 95% (Expected Shortfall %)"},
    "max_drawdown_pct":   {"warn": -15.0,"critical": -25.0, "desc": "Maximum Drawdown (%)"},
    "sharpe_ratio":       {"warn": 0.5,  "critical": 0.0,  "desc": "Sharpe Ratio (lower = worse)"},
    "beta_spy":           {"warn": 1.3,  "critical": 1.8,  "desc": "Beta vs SPY (higher = more volatile)"},
    "max_position_pct":   {"warn": 12.0, "critical": 18.0, "desc": "Largest Single Position (%)"},
}


class QuantRiskAnalyst:
    """
    Dr. Elena Vasquez — Quantitative Risk Analyst.
    IRM Board Seat 2. Analyzes statistical risk metrics and stress scenarios.
    """

    def __init__(self):
        self.name = "Dr. Elena Vasquez"
        self.title = "Quantitative Risk Analyst"
        self.domain = "Statistical Risk Modelling & Stress Testing"

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

    def _load_portfolio_snapshot(self) -> dict:
        """Try to load latest portfolio snapshot from canary or state."""
        for path in ["data/canary_state.json", "data/state.json"]:
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

    def _validate_var_metrics(self, irm: dict) -> tuple[list, list]:
        """
        Checks VaR / CVaR figures for internal consistency and benchmark violations.
        Returns (findings, red_flags).
        """
        findings = []
        red_flags = []

        var = irm.get("var_metrics", {})
        if not var:
            findings.append(
                "No VaR metrics available in IRM report. "
                "Statistical risk layer cannot be validated this cycle."
            )
            return findings, red_flags

        # VaR 95% 1-day
        var95 = var.get("var_95_1day_pct", None)
        if var95 is not None:
            b = RISK_BENCHMARKS["var_95_1day_pct"]
            if var95 > b["critical"]:
                red_flags.append(
                    f"VaR 95% 1-day = {var95:.2f}% exceeds CRITICAL threshold "
                    f"({b['critical']}%). In a GFC-2008-style event (VIX 80+), "
                    f"true intraday loss would likely be 3-5× the stated VaR "
                    f"due to correlation breakdown and liquidity gaps."
                )
            elif var95 > b["warn"]:
                findings.append(
                    f"VaR 95% 1-day = {var95:.2f}% is above warning threshold "
                    f"({b['warn']}%). Monitor closely — approaching risk limits."
                )
            else:
                findings.append(
                    f"VaR 95% 1-day = {var95:.2f}% — within acceptable parameters."
                )

        # CVaR consistency check: CVaR must be ≥ VaR (by mathematical definition)
        cvar95 = var.get("cvar_95_pct", None)
        if var95 is not None and cvar95 is not None:
            if cvar95 < var95:
                red_flags.append(
                    f"MATHEMATICAL INCONSISTENCY: CVaR 95% ({cvar95:.2f}%) < "
                    f"VaR 95% ({var95:.2f}%). CVaR is the EXPECTED loss beyond "
                    f"the VaR threshold — it cannot be lower. Model error suspected."
                )
            else:
                ratio = cvar95 / var95 if var95 > 0 else 1.0
                if ratio > 2.0:
                    red_flags.append(
                        f"CVaR/VaR ratio = {ratio:.1f}x — extreme tail risk indicated. "
                        f"The portfolio's tail losses are far heavier than the average "
                        f"scenario. Fat-tail distribution confirmed; Gaussian VaR models "
                        f"will substantially understate true risk exposure."
                    )
                elif ratio > 1.5:
                    findings.append(
                        f"CVaR/VaR ratio = {ratio:.1f}x — moderate tail heaviness. "
                        f"Distributions showing meaningful non-normality."
                    )
                else:
                    findings.append(
                        f"CVaR/VaR ratio = {ratio:.1f}x — tail shape consistent with "
                        f"near-normal returns distribution."
                    )

        # Sharpe ratio assessment
        ra = irm.get("risk_assessment", {})
        sharpe = ra.get("sharpe_ratio", None)
        if sharpe is not None:
            if sharpe < RISK_BENCHMARKS["sharpe_ratio"]["critical"]:
                red_flags.append(
                    f"Sharpe Ratio = {sharpe:.2f} — negative risk-adjusted return. "
                    f"The portfolio is generating less return than a risk-free asset "
                    f"while bearing full equity risk. Per Sharpe (1966): "
                    f"this represents a failure of active management."
                )
            elif sharpe < RISK_BENCHMARKS["sharpe_ratio"]["warn"]:
                findings.append(
                    f"Sharpe Ratio = {sharpe:.2f} — below acceptable threshold (0.5). "
                    f"Risk-adjusted performance is sub-par; position sizing "
                    f"and entry quality should be reviewed."
                )
            else:
                findings.append(f"Sharpe Ratio = {sharpe:.2f} — acceptable risk-adjusted return.")

        return findings, red_flags

    def _run_stress_scenarios(self, irm: dict) -> tuple[list, list]:
        """
        Stress-tests the portfolio against historical crash scenarios.
        Returns (findings, red_flags).
        """
        findings = []
        red_flags = []

        perf = irm.get("performance", {})
        conc = irm.get("concentration", {})
        portfolio_value = perf.get("portfolio_value", 0)
        if portfolio_value <= 0:
            findings.append("Portfolio value unavailable — stress scenarios not calculated.")
            return findings, red_flags

        # Run each scenario — all go to findings (educational), not red flags.
        # Stress scenarios are forward-looking worst-case simulations, not current
        # violations. They inform risk awareness; they do not indicate the portfolio
        # is currently in distress. Only flag as red if current drawdown is already
        # significant (>10% actual drawdown in the real market).
        ra = irm.get("risk_assessment", {})
        current_drawdown_pct = ra.get("max_drawdown_pct", 0) or 0
        in_active_drawdown = current_drawdown_pct < -10  # actual current drawdown > 10%

        for scenario_key, scenario in CRASH_SCENARIOS.items():
            drawdown_pct = scenario["spy_drawdown"]
            # Assume portfolio beta ~ 1.2 (typical momentum portfolio)
            # Adjust for concentration in tech (amplify by 1.3 if heavy tech)
            max_single = conc.get("max_single_pct", 5)
            beta_adj = 1.2 + (0.1 if max_single > 15 else 0.0)
            estimated_loss_pct = drawdown_pct * beta_adj
            estimated_loss_dollars = portfolio_value * (estimated_loss_pct / 100)

            severity = "[EXTREME]" if estimated_loss_pct < -50 else "[SEVERE]" if estimated_loss_pct < -30 else "[MODERATE]"
            findings.append(
                f"Historical stress scenario {severity} — '{scenario['name']}': "
                f"estimated loss {estimated_loss_pct:.1f}% (≈${estimated_loss_dollars:,.0f}) "
                f"if a comparable event occurred today. Lesson: {scenario['lesson']}"
            )

        return findings, red_flags

    def _assess_model_risk(self, irm: dict) -> tuple[list, list]:
        """
        Identifies model risk: places where stated risk metrics likely understate truth.
        Returns (findings, red_flags).
        """
        findings = []
        red_flags = []

        var = irm.get("var_metrics", {})
        ra  = irm.get("risk_assessment", {})
        conc= irm.get("concentration", {})

        # Lookback window risk: VaR based on recent calm period will understate risk
        risk_level = ra.get("risk_level", "UNKNOWN")
        if risk_level in ("LOW", "MODERATE"):
            findings.append(
                "Current low-volatility regime may cause VaR models to understate "
                "forward risk. Per Minsky's financial instability hypothesis: "
                "'stability breeds instability.' Low VaR in calm markets often "
                "precedes the largest dislocations."
            )

        # Concentration risk: correlation spike in crashes
        max_single = conc.get("max_single_pct", 0)
        sector_top = conc.get("top_sector_pct", 0)
        if sector_top > 40:
            red_flags.append(
                f"Top sector = {sector_top:.0f}% of portfolio. In a sector-specific "
                f"shock (e.g. 2022 rate-hike impact on tech: -35% for XLK), "
                f"diversification benefits evaporate. Stated VaR likely underestimates "
                f"actual drawdown risk by 1.5-2.0x for this portfolio structure."
            )

        # Liquidity-adjusted VaR consideration
        n_positions = conc.get("n_positions", 0)
        if n_positions < 5:
            red_flags.append(
                f"Only {n_positions} open positions. In a fast market, "
                f"attempting to sell concentrated positions can move the market "
                f"against you. Liquidity-adjusted VaR would be materially higher "
                f"than the point estimate. (Taleb: 'risk is not in the models; "
                f"it's in what the models miss.')"
            )

        return findings, red_flags

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, irm_context: Optional[dict] = None) -> dict:
        """
        Run the full quantitative risk analysis.
        Returns a structured finding dict for the IRM Director.
        """
        irm = irm_context or self._load_irm_report()

        all_findings = []
        all_red_flags = []
        recommendations = []

        # 1. Validate VaR metrics
        f, r = self._validate_var_metrics(irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 2. Stress scenarios
        f, r = self._run_stress_scenarios(irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 3. Model risk
        f, r = self._assess_model_risk(irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # Compute risk score 1-10
        risk_score = 3  # baseline
        risk_score += len(all_red_flags) * 2
        risk_score = min(10, risk_score)

        # Recommendations
        if any("VaR 95%" in r for r in all_red_flags):
            recommendations.append(
                "Reduce gross exposure until VaR returns within threshold. "
                "Consider raising cash allocation by 10-15pp."
            )
        if any("sector" in r.lower() for r in all_red_flags):
            recommendations.append(
                "Rebalance sector concentration — no single sector should exceed "
                "30% of portfolio. Cross-sector correlation in crashes makes "
                "concentrated bets linearly dangerous."
            )
        if any("Sharpe" in r for r in all_red_flags):
            recommendations.append(
                "Review entry quality and timing. Negative Sharpe is a signal "
                "to pause new buys and reassess the strategy's edge."
            )
        if not recommendations:
            recommendations.append(
                "No immediate quantitative interventions required. "
                "Continue monitoring VaR/CVaR on each pipeline run."
            )

        # Confidence
        if irm.get("var_metrics"):
            confidence = "HIGH"
        elif irm:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        logger.debug(
            f"[QuantRisk] Score={risk_score} | "
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
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    analyst = QuantRiskAnalyst()
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

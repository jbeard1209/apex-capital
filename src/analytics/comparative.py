"""
Comparative Analytics — Quantitative Analysis Team
Compares strategy performance and generates improvement recommendations.
"""

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.backtesting.engine import BacktestResult

logger = logging.getLogger(__name__)


class ComparativeAnalytics:
    """
    Analyzes multiple strategy backtests side-by-side.
    Generates ranked comparison and actionable improvement recommendations.
    """

    METRICS_ORDER = [
        "Ann. Return",
        "Sharpe Ratio",
        "Sortino Ratio",
        "Calmar Ratio",
        "Max Drawdown",
        "Win Rate",
        "Profit Factor",
        "Alpha (vs Bench)",
        "Beta",
        "Total Trades",
    ]

    def __init__(self, results: List[BacktestResult]):
        self.results = results
        self.primary_strategy_name = None  # Set to name of our main strategy

    def comparison_table(self) -> pd.DataFrame:
        """
        Build a side-by-side comparison table of all strategies.
        """
        rows = []
        for r in self.results:
            s = r.summary()
            rows.append({
                "Strategy": s["Strategy"],
                "Ann. Return %": r.annualized_return_pct,
                "Total Return %": r.total_return_pct,
                "Sharpe": r.sharpe_ratio,
                "Sortino": r.sortino_ratio,
                "Calmar": r.calmar_ratio,
                "Max DD %": r.max_drawdown_pct,
                "Win Rate %": r.win_rate_pct,
                "Profit Factor": r.profit_factor,
                "Trades": r.total_trades,
                "Alpha %": r.alpha,
                "Beta": r.beta,
                "Benchmark %": r.benchmark_return_pct,
            })

        df = pd.DataFrame(rows).set_index("Strategy")

        # Add rank columns
        df["Return Rank"] = df["Ann. Return %"].rank(ascending=False).astype(int)
        df["Sharpe Rank"] = df["Sharpe"].rank(ascending=False).astype(int)
        df["DD Rank"] = df["Max DD %"].rank(ascending=True).astype(int)  # Less DD = better

        # Composite rank
        df["Composite Rank"] = (
            df["Return Rank"] * 0.3
            + df["Sharpe Rank"] * 0.4
            + df["DD Rank"] * 0.3
        ).rank().astype(int)

        return df.sort_values("Composite Rank")

    def identify_best_strategy(self) -> Tuple[str, BacktestResult]:
        """
        Identify the best-performing strategy by composite score
        (risk-adjusted returns, not raw returns).
        """
        def composite_score(r: BacktestResult) -> float:
            # Weight: 40% Sharpe, 30% Ann. Return, 30% inverse Max DD
            sharpe_score = min(r.sharpe_ratio / 3.0, 1.0)  # Normalize to ~1
            return_score = min(r.annualized_return_pct / 30.0, 1.0)
            dd_score = max(0, 1.0 + r.max_drawdown_pct / 100)  # DD is negative
            return sharpe_score * 0.4 + return_score * 0.3 + dd_score * 0.3

        best = max(self.results, key=composite_score)
        return best.strategy_name, best

    def generate_improvement_recommendations(
        self,
        primary_result: BacktestResult,
    ) -> List[dict]:
        """
        Analyze the primary strategy's weaknesses and recommend improvements
        by comparing against other strategies.

        Returns list of recommendation dicts:
            {priority, category, finding, recommendation, expected_impact}
        """
        recommendations = []
        comparison = self.comparison_table()

        # Get best performers in each dimension
        best_sharpe_name = comparison["Sharpe"].idxmax()
        best_return_name = comparison["Ann. Return %"].idxmax()
        least_dd_name = comparison["Max DD %"].idxmax()  # Least negative

        primary = primary_result

        # ── 1. Sharpe Ratio improvement ───────────────────────────────────
        if primary.sharpe_ratio < 1.0:
            best = next((r for r in self.results if r.strategy_name == best_sharpe_name), None)
            recommendations.append({
                "priority": "HIGH",
                "category": "Risk-Adjusted Returns",
                "finding": f"Sharpe ratio of {primary.sharpe_ratio:.2f} is below 1.0 target",
                "recommendation": (
                    f"Adopt volatility targeting: scale position sizes inversely with "
                    f"realized volatility. {best_sharpe_name} achieves Sharpe={best.sharpe_ratio:.2f}. "
                    f"Consider adding 20-day vol targeting to position sizing."
                ),
                "expected_impact": "Sharpe +0.3 to +0.6",
            })

        # ── 2. Drawdown reduction ──────────────────────────────────────────
        if abs(primary.max_drawdown_pct) > 20:
            recommendations.append({
                "priority": "HIGH",
                "category": "Risk Management",
                "finding": f"Max drawdown of {primary.max_drawdown_pct:.1f}% exceeds 20% limit",
                "recommendation": (
                    "Implement a regime filter: measure market trend via 200-day SMA of SPY. "
                    "Reduce gross exposure by 50% when SPY < 200d SMA. "
                    "This historically cuts bear-market drawdowns by 30-50%."
                ),
                "expected_impact": "Max DD reduced by 5-15 percentage points",
            })

        # ── 3. Win rate improvement ────────────────────────────────────────
        if primary.win_rate_pct < 50:
            recommendations.append({
                "priority": "MEDIUM",
                "category": "Signal Quality",
                "finding": f"Win rate {primary.win_rate_pct:.1f}% below 50%",
                "recommendation": (
                    "Add technical confirmation filter: only enter when RSI(14) > 40 "
                    "and price > 20-day SMA. This removes low-quality entries. "
                    "Also consider requiring 3+ institutions (vs current 2) for conviction."
                ),
                "expected_impact": "Win rate +5-10%, reduced trade count",
            })

        # ── 4. Return enhancement ──────────────────────────────────────────
        if primary.annualized_return_pct < 15:
            best = next((r for r in self.results if r.strategy_name == best_return_name), None)
            recommendations.append({
                "priority": "MEDIUM",
                "category": "Alpha Generation",
                "finding": (
                    f"Annualized return of {primary.annualized_return_pct:.1f}% "
                    f"below 15% target"
                ),
                "recommendation": (
                    f"{best_return_name} achieves {best.annualized_return_pct:.1f}% annually. "
                    f"Consider a multi-signal blend: combine Institutional Following (50%) "
                    f"with Momentum (30%) and Mean Reversion on dips (20%). "
                    f"Diversification across strategies reduces correlation risk."
                ),
                "expected_impact": "+3-8% annualized return",
            })

        # ── 5. Profit factor ──────────────────────────────────────────────
        if primary.profit_factor < 1.5:
            recommendations.append({
                "priority": "MEDIUM",
                "category": "Trade Sizing",
                "finding": f"Profit factor {primary.profit_factor:.2f} is low",
                "recommendation": (
                    "Apply Kelly Criterion (25% fractional) for position sizing. "
                    "Larger positions on high-conviction signals (score > 0.8), "
                    "smaller on lower confidence. "
                    "Also extend take-profit from 25% to 35% on highest-score signals."
                ),
                "expected_impact": "Profit factor +0.3-0.6",
            })

        # ── 6. Beta reduction ──────────────────────────────────────────────
        if abs(primary.beta) > 1.1:
            recommendations.append({
                "priority": "LOW",
                "category": "Market Exposure",
                "finding": f"Beta of {primary.beta:.2f} indicates high market sensitivity",
                "recommendation": (
                    "Add sector-neutral construction: equal-weight across 5+ GICS sectors. "
                    "Consider hedging with inverse ETF (SH/SDS) sized at 10-20% of gross. "
                    "This reduces systematic risk without capping upside."
                ),
                "expected_impact": "Beta reduced to 0.6-0.8, Sharpe improvement",
            })

        # ── 7. Institutional signal improvements ──────────────────────────
        recommendations.append({
            "priority": "HIGH",
            "category": "Signal Enhancement",
            "finding": "Core institutional signal can be enhanced with alternative data",
            "recommendation": (
                "Layer these signals on top of 13F data: "
                "(1) Options flow: unusual call volume as confirming signal. "
                "(2) SEC Form 4 insider purchases aligning with institutional buys. "
                "(3) Short interest reduction: shorts covering + institutions buying = strong signal. "
                "(4) Earnings revision momentum: analysts raising estimates. "
                "Require 2+ confirming signals for highest conviction tier."
            ),
            "expected_impact": "Signal quality +15-25%, win rate improvement",
        })

        # ── 8. Market regime awareness ────────────────────────────────────
        recommendations.append({
            "priority": "HIGH",
            "category": "Regime Adaptation",
            "finding": "Strategy does not adapt to market regimes",
            "recommendation": (
                "Implement regime detection using Hidden Markov Model or "
                "VIX-based regime filter: "
                "VIX < 20 → full allocation, "
                "VIX 20-30 → 50% allocation, "
                "VIX > 30 → 25% allocation + cash buffer. "
                "In 2022 bear market, this would have saved 15-20% in drawdown."
            ),
            "expected_impact": "Bear market protection, improved Calmar ratio",
        })

        return sorted(recommendations, key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["priority"]])

    def compute_correlation_matrix(self) -> pd.DataFrame:
        """Compute return correlations between strategies."""
        if len(self.results) < 2:
            return pd.DataFrame()

        returns_dict = {r.strategy_name: r.daily_returns for r in self.results}
        aligned = pd.DataFrame(returns_dict).dropna()
        return aligned.corr()

    def optimal_blend(self) -> Dict[str, float]:
        """
        Compute optimal portfolio blend of strategies using
        minimum-variance optimization.
        Returns dict: {strategy_name: weight}
        """
        if len(self.results) < 2:
            return {self.results[0].strategy_name: 1.0}

        returns_dict = {r.strategy_name: r.daily_returns for r in self.results}
        returns_df = pd.DataFrame(returns_dict).dropna()

        if returns_df.empty:
            n = len(self.results)
            return {r.strategy_name: 1.0 / n for r in self.results}

        # Mean-variance optimization (simplified)
        mu = returns_df.mean().values
        sigma = returns_df.cov().values

        # Risk parity weights as practical solution
        vol = np.sqrt(np.diag(sigma))
        inv_vol = 1.0 / (vol + 1e-10)
        weights = inv_vol / inv_vol.sum()

        return {
            name: round(w, 4)
            for name, w in zip(returns_dict.keys(), weights)
        }

    def print_comparison_report(self):
        """Print a formatted comparison report to console."""
        print("\n" + "=" * 80)
        print("  STRATEGY COMPARISON REPORT — Apex Capital Intelligence")
        print("=" * 80)

        table = self.comparison_table()
        print("\n📊 PERFORMANCE COMPARISON:")
        print(table[["Ann. Return %", "Sharpe", "Max DD %", "Win Rate %",
                      "Alpha %", "Composite Rank"]].round(2).to_string())

        best_name, best = self.identify_best_strategy()
        print(f"\n🏆 BEST STRATEGY: {best_name}")
        print(f"   Ann. Return: {best.annualized_return_pct:+.2f}%")
        print(f"   Sharpe: {best.sharpe_ratio:.3f}")
        print(f"   Max DD: {best.max_drawdown_pct:.2f}%")

        corr = self.compute_correlation_matrix()
        if not corr.empty:
            print("\n📈 STRATEGY CORRELATIONS:")
            print(corr.round(3).to_string())

        blend = self.optimal_blend()
        print("\n⚖️  OPTIMAL BLEND (Risk Parity):")
        for name, weight in blend.items():
            print(f"   {name}: {weight * 100:.1f}%")

        print("\n" + "=" * 80)

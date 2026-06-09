"""
Report Generator — Design Team
Creates professional performance reports and dashboards.
"""

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd

from src.backtesting.engine import BacktestResult
from src.analytics.comparative import ComparativeAnalytics
from config import REPORT_CONFIG

logger = logging.getLogger(__name__)

plt.style.use("dark_background")


class ReportGenerator:
    """
    Generates:
    - Equity curve charts
    - Strategy comparison charts
    - Drawdown analysis
    - Recommendation reports
    - Console summary tables
    """

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or REPORT_CONFIG["output_dir"]
        os.makedirs(self.output_dir, exist_ok=True)
        self.primary_color = REPORT_CONFIG["color_primary"]
        self.danger_color = REPORT_CONFIG["color_secondary"]
        self.neutral_color = REPORT_CONFIG["color_neutral"]

    def generate_equity_chart(
        self,
        results: List[BacktestResult],
        title: str = "Strategy Performance",
        save_path: Optional[str] = None,
    ) -> str:
        """Generate multi-strategy equity curve comparison chart."""
        fig, axes = plt.subplots(
            3, 1, figsize=(14, 12),
            gridspec_kw={"height_ratios": [3, 1.5, 1.5]}
        )
        fig.patch.set_facecolor("#0D1117")

        colors = [
            "#00FF88", "#FF6B35", "#4FC3F7", "#FFD700",
            "#FF4081", "#69F0AE", "#FF5252",
        ]

        ax1, ax2, ax3 = axes
        ax1.set_facecolor("#0D1117")
        ax2.set_facecolor("#0D1117")
        ax3.set_facecolor("#0D1117")

        # ── Equity Curves ─────────────────────────────────────────────────
        for i, result in enumerate(results):
            if result.equity_curve.empty:
                continue
            color = colors[i % len(colors)]
            norm_equity = result.equity_curve / result.initial_capital * 100

            ax1.plot(
                result.equity_curve.index,
                norm_equity,
                label=f"{result.strategy_name} ({result.total_return_pct:+.1f}%)",
                color=color,
                linewidth=1.8,
                alpha=0.9,
            )

        ax1.axhline(100, color="#555555", linestyle="--", linewidth=0.8, alpha=0.5)
        ax1.set_title(title, fontsize=14, color="white", fontweight="bold", pad=12)
        ax1.set_ylabel("Portfolio Value (indexed to 100)", color="#AAAAAA", fontsize=10)
        ax1.legend(loc="upper left", fontsize=9, facecolor="#1A1A2E")
        ax1.tick_params(colors="#AAAAAA")
        ax1.spines["bottom"].set_color("#333333")
        ax1.spines["left"].set_color("#333333")
        ax1.grid(True, alpha=0.1, color="#444444")

        # ── Drawdown ──────────────────────────────────────────────────────
        for i, result in enumerate(results):
            if result.equity_curve.empty:
                continue
            color = colors[i % len(colors)]
            eq = result.equity_curve
            rolling_max = eq.cummax()
            drawdown = (eq - rolling_max) / rolling_max * 100

            ax2.fill_between(
                drawdown.index,
                drawdown.values,
                0,
                alpha=0.3,
                color=color,
                label=result.strategy_name,
            )
            ax2.plot(drawdown.index, drawdown.values, color=color, linewidth=0.8, alpha=0.7)

        ax2.set_ylabel("Drawdown %", color="#AAAAAA", fontsize=10)
        ax2.set_ylim(top=5)
        ax2.axhline(0, color="#555555", linewidth=0.8)
        ax2.tick_params(colors="#AAAAAA")
        ax2.grid(True, alpha=0.1, color="#444444")

        # ── Monthly Returns (Primary Strategy) ────────────────────────────
        if results:
            primary = results[0]
            if not primary.daily_returns.empty:
                monthly_ret = primary.daily_returns.resample("ME").sum() * 100
                colors_bar = [
                    self.primary_color if v >= 0 else self.danger_color
                    for v in monthly_ret.values
                ]
                ax3.bar(
                    monthly_ret.index,
                    monthly_ret.values,
                    color=colors_bar,
                    alpha=0.8,
                    width=20,
                )
                ax3.set_ylabel(
                    f"{results[0].strategy_name}\nMonthly Returns %",
                    color="#AAAAAA", fontsize=9
                )
                ax3.axhline(0, color="#555555", linewidth=0.8)
                ax3.tick_params(colors="#AAAAAA")
                ax3.grid(True, alpha=0.1, color="#444444")

        plt.tight_layout(pad=2.0)

        path = save_path or os.path.join(
            self.output_dir,
            f"equity_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        plt.savefig(path, dpi=150, bbox_inches="tight", facecolor="#0D1117")
        plt.close()
        logger.info(f"Chart saved: {path}")
        return path

    def generate_comparison_bar_chart(
        self,
        results: List[BacktestResult],
        save_path: Optional[str] = None,
    ) -> str:
        """Generate side-by-side metric comparison chart."""
        fig, axes = plt.subplots(2, 3, figsize=(16, 9))
        fig.patch.set_facecolor("#0D1117")
        fig.suptitle(
            "Strategy Comparison — Apex Capital Intelligence",
            fontsize=14, color="white", fontweight="bold", y=0.98
        )

        metrics = [
            ("Ann. Return %", "annualized_return_pct", True),
            ("Sharpe Ratio", "sharpe_ratio", True),
            ("Max Drawdown %", "max_drawdown_pct", False),
            ("Win Rate %", "win_rate_pct", True),
            ("Profit Factor", "profit_factor", True),
            ("Alpha % (vs SPY)", "alpha", True),
        ]

        colors_palette = [
            "#00FF88", "#FF6B35", "#4FC3F7", "#FFD700", "#FF4081", "#69F0AE"
        ]

        for idx, (title, attr, higher_better) in enumerate(metrics):
            ax = axes[idx // 3][idx % 3]
            ax.set_facecolor("#0D1117")

            names = [r.strategy_name.split(" ")[0] for r in results]
            values = [getattr(r, attr, 0) for r in results]

            bar_colors = []
            for i, v in enumerate(values):
                if higher_better:
                    color = self.primary_color if v >= 0 else self.danger_color
                else:
                    color = self.primary_color if v > -15 else self.danger_color
                bar_colors.append(color)

            bars = ax.bar(names, values, color=bar_colors, alpha=0.85, width=0.6)

            # Value labels on bars
            for bar, val in zip(bars, values):
                ypos = bar.get_height() if val >= 0 else bar.get_height()
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    val + (max(values) - min(values)) * 0.02 if val >= 0 else val - (max(values) - min(values)) * 0.05,
                    f"{val:.2f}",
                    ha="center", va="bottom" if val >= 0 else "top",
                    color="white", fontsize=8, fontweight="bold",
                )

            ax.set_title(title, color="#AAAAAA", fontsize=10, pad=8)
            ax.axhline(0, color="#555555", linewidth=0.8)
            ax.tick_params(colors="#AAAAAA", labelsize=8)
            ax.spines["bottom"].set_color("#333333")
            ax.spines["left"].set_color("#333333")
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(True, alpha=0.08, color="#444444", axis="y")

        plt.tight_layout(pad=2.5)

        path = save_path or os.path.join(
            self.output_dir,
            f"comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        )
        plt.savefig(path, dpi=150, bbox_inches="tight", facecolor="#0D1117")
        plt.close()
        logger.info(f"Comparison chart saved: {path}")
        return path

    def print_signal_report(
        self, signals_df: pd.DataFrame, title: str = "INSTITUTIONAL SIGNALS"
    ):
        """Print a formatted signal report to console."""
        print(f"\n{'=' * 70}")
        print(f"  {title}")
        print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'=' * 70}")

        if signals_df.empty:
            print("  No signals generated.")
            return

        display_cols = [
            c for c in [
                "rank", "ticker", "composite_score", "tier",
                "institution_count", "entry_price", "signal_date", "expected_exit"
            ]
            if c in signals_df.columns
        ]

        print(signals_df[display_cols].to_string(index=False))
        print(f"{'=' * 70}\n")

    def print_backtest_summary(self, result: BacktestResult):
        """Print a formatted backtest summary."""
        summary = result.summary()
        print(f"\n{'─' * 55}")
        print(f"  BACKTEST: {result.strategy_name}")
        print(f"{'─' * 55}")
        for k, v in summary.items():
            label = k.ljust(25)
            print(f"  {label} {v}")
        print(f"{'─' * 55}\n")

    def print_recommendations(self, recommendations: List[dict]):
        """Print improvement recommendations."""
        print(f"\n{'=' * 70}")
        print("  STRATEGY IMPROVEMENT RECOMMENDATIONS")
        print("  Quantitative Analysis Team / Research Team")
        print(f"{'=' * 70}")

        for i, rec in enumerate(recommendations, 1):
            priority_symbol = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(
                rec["priority"], "⚪"
            )
            print(f"\n[{i}] {priority_symbol} {rec['priority']} | {rec['category']}")
            print(f"    FINDING:     {rec['finding']}")
            print(f"    RECOMMEND:   {rec['recommendation']}")
            print(f"    IMPACT:      {rec['expected_impact']}")

        print(f"\n{'=' * 70}\n")

    def save_full_report(
        self,
        results: List[BacktestResult],
        recommendations: List[dict],
        signals_df: Optional[pd.DataFrame] = None,
    ) -> str:
        """Save a full text report to file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.output_dir, f"full_report_{timestamp}.txt")

        analytics = ComparativeAnalytics(results)
        comparison = analytics.comparison_table()
        blend = analytics.optimal_blend()

        with open(path, "w") as f:
            f.write("=" * 80 + "\n")
            f.write("APEX CAPITAL INTELLIGENCE — FULL STRATEGY REPORT\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n\n")

            f.write("STRATEGY PERFORMANCE COMPARISON\n")
            f.write("-" * 80 + "\n")
            f.write(comparison.to_string() + "\n\n")

            f.write("OPTIMAL PORTFOLIO BLEND (Risk Parity)\n")
            f.write("-" * 80 + "\n")
            for name, weight in blend.items():
                f.write(f"  {name}: {weight * 100:.1f}%\n")
            f.write("\n")

            f.write("INDIVIDUAL STRATEGY SUMMARIES\n")
            f.write("-" * 80 + "\n")
            for r in results:
                for k, v in r.summary().items():
                    f.write(f"  {k.ljust(30)} {v}\n")
                f.write("\n")

            f.write("IMPROVEMENT RECOMMENDATIONS\n")
            f.write("-" * 80 + "\n")
            for i, rec in enumerate(recommendations, 1):
                f.write(f"\n[{i}] [{rec['priority']}] {rec['category']}\n")
                f.write(f"  Finding:        {rec['finding']}\n")
                f.write(f"  Recommendation: {rec['recommendation']}\n")
                f.write(f"  Expected Impact:{rec['expected_impact']}\n")

            if signals_df is not None and not signals_df.empty:
                f.write("\n\nCURRENT INSTITUTIONAL SIGNALS\n")
                f.write("-" * 80 + "\n")
                f.write(signals_df.to_string() + "\n")

        logger.info(f"Full report saved: {path}")
        return path

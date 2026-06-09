"""
Apex Capital Intelligence — Main Orchestration
Chairman: Jack Beard | President: Claude (AI)

Usage:
    python main.py                    # Full run (signals + backtest + report)
    python main.py --mode signals     # Live signal scan only
    python main.py --mode backtest    # Backtest all strategies
    python main.py --mode org         # Print org chart
    python main.py --tickers AAPL NVDA MSFT  # Custom ticker set
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("apex.main")

# ── Internal imports ──────────────────────────────────────────────────────────
from config import (
    ORG, INSTITUTIONS, STOCK_UNIVERSE, BACKTEST_CONFIG, SEC_CONFIG, REPORT_CONFIG
)
from org_structure import print_org_chart

from src.data.sec_fetcher import SECFetcher
from src.data.market_data import MarketDataProvider
from src.detection.block_detector import BlockDetector
from src.detection.institutional_tracker import InstitutionalTracker
from src.strategies.institutional_following import InstitutionalFollowingStrategy
from src.strategies.momentum import MomentumStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.value_strategy import ValueStrategy
from src.backtesting.engine import BacktestEngine
from src.analytics.comparative import ComparativeAnalytics
from src.risk.risk_manager import RiskManager
from src.reporting.report_generator import ReportGenerator


def banner():
    print("\n" + "█" * 70)
    print(f"  {ORG['company'].upper()}")
    print(f"  Chairman: {ORG['chairman']}  |  President: {ORG['president']}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("█" * 70 + "\n")


# ── MODULE: Signal Scanner ─────────────────────────────────────────────────────
def run_signal_scan(
    tickers: Optional[List[str]] = None,
    use_sec: bool = True,
    days_back: int = 45,
) -> pd.DataFrame:
    """
    Scan for institutional accumulation signals.

    Steps:
    1. Fetch 13F filing deltas from SEC EDGAR
    2. Run block detection on price/volume data
    3. Combine into ranked signal list
    """
    logger.info("━━━ SIGNAL SCAN: Starting ━━━")
    market = MarketDataProvider()
    detector = BlockDetector()
    tracker = InstitutionalTracker(market, detector)
    reporter = ReportGenerator()

    filing_delta = pd.DataFrame()

    # ── SEC 13F Data ────────────────────────────────────────────────────────
    if use_sec:
        logger.info("Fetching SEC EDGAR 13F filing deltas...")
        fetcher = SECFetcher(user_agent=SEC_CONFIG["user_agent"])
        try:
            filing_delta = fetcher.get_all_institutions_signals(INSTITUTIONS)
            if not filing_delta.empty:
                logger.info(f"13F delta: {len(filing_delta)} institutional position changes found")
            else:
                logger.warning("13F data empty — falling back to market-only mode")
        except Exception as e:
            logger.error(f"SEC fetch failed: {e} — using market-only mode")

    # ── Generate Signals ────────────────────────────────────────────────────
    signals = tracker.generate_signals(
        filing_delta, lookback_days=days_back
    )

    if not signals:
        logger.info("No 13F signals — running live market scan...")
        scan_result = tracker.simulate_live_scan(
            tickers=tickers or STOCK_UNIVERSE[:30],
            days_back=days_back,
        )
        reporter.print_signal_report(scan_result, "MARKET BLOCK SCAN — TOP SIGNALS")
        return scan_result

    ranked = tracker.rank_signals(signals, top_n=15)
    reporter.print_signal_report(ranked, "INSTITUTIONAL ACCUMULATION SIGNALS")

    logger.info("━━━ SIGNAL SCAN: Complete ━━━")
    return ranked


# ── MODULE: Backtester ─────────────────────────────────────────────────────────
def run_backtest(
    tickers: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    walk_forward: bool = False,
) -> List:
    """
    Backtest all strategies and generate comparative analytics.
    """
    logger.info("━━━ BACKTEST: Starting ━━━")

    start = start_date or BACKTEST_CONFIG["start_date"]
    end = end_date or BACKTEST_CONFIG["end_date"]
    universe = tickers or STOCK_UNIVERSE[:25]   # Use 25 stocks for speed

    reporter = ReportGenerator()
    risk_mgr = RiskManager()

    # ── Fetch price data ─────────────────────────────────────────────────────
    logger.info(f"Fetching price data: {len(universe)} stocks, {start} → {end}")
    market = MarketDataProvider()
    prices = market.get_close_prices(universe, start, end)

    if prices.empty:
        logger.error("Failed to fetch price data. Check internet connection.")
        return []

    logger.info(f"Price data loaded: {prices.shape[0]} days × {prices.shape[1]} stocks")

    # ── Benchmark (SPY) ──────────────────────────────────────────────────────
    benchmark_prices = market.get_close_prices(
        [BACKTEST_CONFIG["benchmark"]], start, end
    )
    spy = (
        benchmark_prices[BACKTEST_CONFIG["benchmark"]]
        if not benchmark_prices.empty else None
    )

    # ── Strategies ───────────────────────────────────────────────────────────
    strategies = [
        InstitutionalFollowingStrategy(),
        MomentumStrategy(),
        MeanReversionStrategy(),
        ValueStrategy(),
    ]

    engine = BacktestEngine()
    results = []

    for strategy in strategies:
        logger.info(f"Backtesting: {strategy.name}...")
        try:
            if walk_forward:
                wf_results = engine.run_walk_forward(strategy, prices, benchmark_prices=spy)
                # Aggregate walk-forward results
                if wf_results:
                    # Use last fold as representative result
                    result = wf_results[-1]
                    result.strategy_name = f"{strategy.name} (WF)"
                    results.append(result)
                    reporter.print_backtest_summary(result)
            else:
                signals = strategy.generate_signals(prices)
                result = engine.run(strategy, prices, signals, spy)
                results.append(result)
                reporter.print_backtest_summary(result)

        except Exception as e:
            logger.error(f"Backtest failed for {strategy.name}: {e}", exc_info=True)

    if not results:
        logger.error("All backtests failed.")
        return []

    # ── Comparative Analytics ────────────────────────────────────────────────
    logger.info("Running comparative analytics...")
    analytics = ComparativeAnalytics(results)
    analytics.print_comparison_report()

    # ── Improvement Recommendations ──────────────────────────────────────────
    primary_result = next(
        (r for r in results if "Institutional" in r.strategy_name), results[0]
    )
    recommendations = analytics.generate_improvement_recommendations(primary_result)
    reporter.print_recommendations(recommendations)

    # ── Charts ───────────────────────────────────────────────────────────────
    logger.info("Generating charts...")
    try:
        equity_path = reporter.generate_equity_chart(
            results,
            title=f"Apex Capital — Strategy Performance {start} → {end}",
        )
        comparison_path = reporter.generate_comparison_bar_chart(results)
        report_path = reporter.save_full_report(results, recommendations)

        print(f"\n📁 Reports saved:")
        print(f"   Equity Chart:    {equity_path}")
        print(f"   Comparison Chart:{comparison_path}")
        print(f"   Full Report:     {report_path}")
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")

    # ── Risk Report ──────────────────────────────────────────────────────────
    portfolio_val = BACKTEST_CONFIG["initial_capital"]
    var_report = risk_mgr.compute_var(primary_result.daily_returns, portfolio_val)
    print(f"\n🔒 RISK — {var_report['1_day_var_interpretation']}")

    logger.info("━━━ BACKTEST: Complete ━━━")
    return results


# ── MODULE: Combined Full Run ──────────────────────────────────────────────────
def run_full(tickers: Optional[List[str]] = None):
    """Full pipeline: signals → backtest → recommendations → reports."""
    banner()
    logger.info("Running full pipeline...")

    # 1. Signal scan
    signals_df = run_signal_scan(tickers=tickers, use_sec=True)

    # 2. Backtest
    results = run_backtest(tickers=tickers)

    if results:
        # 3. Final summary
        analytics = ComparativeAnalytics(results)
        best_name, best = analytics.identify_best_strategy()
        blend = analytics.optimal_blend()

        print("\n" + "█" * 70)
        print("  EXECUTIVE SUMMARY — Apex Capital Intelligence")
        print("█" * 70)
        print(f"\n  Best Strategy:  {best_name}")
        print(f"  Ann. Return:    {best.annualized_return_pct:+.2f}%")
        print(f"  Sharpe Ratio:   {best.sharpe_ratio:.3f}")
        print(f"  Max Drawdown:   {best.max_drawdown_pct:.2f}%")
        print(f"  Alpha vs SPY:   {best.alpha:+.2f}%\n")
        print("  Optimal Portfolio Blend:")
        for name, weight in blend.items():
            bar = "█" * int(weight * 20)
            print(f"    {name[:35].ljust(35)} {bar} {weight * 100:.0f}%")
        print("\n" + "█" * 70 + "\n")


# ── Entry Point ────────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Apex Capital Intelligence — Institutional Block Tracker"
    )
    parser.add_argument(
        "--mode",
        choices=["full", "signals", "backtest", "org"],
        default="full",
        help="Operation mode (default: full)",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Custom ticker list (e.g. --tickers AAPL NVDA MSFT)",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Backtest start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Backtest end date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Use walk-forward validation in backtest",
    )
    parser.add_argument(
        "--no-sec",
        action="store_true",
        help="Skip SEC EDGAR fetching (faster, market-only mode)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.mode == "org":
        print_org_chart()

    elif args.mode == "signals":
        banner()
        run_signal_scan(
            tickers=args.tickers,
            use_sec=not args.no_sec,
        )

    elif args.mode == "backtest":
        banner()
        run_backtest(
            tickers=args.tickers,
            start_date=args.start,
            end_date=args.end,
            walk_forward=args.walk_forward,
        )

    else:  # full
        run_full(tickers=args.tickers)

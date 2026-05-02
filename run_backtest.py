#!/usr/bin/env python3
# =============================================================================
# RUN BACKTEST — Standalone 5-Year Backtest Runner
# =============================================================================
# Run this once to see the full historical backtest results.
# Outputs: console summary + HTML report saved to reports/
# =============================================================================

import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    print("\n" + "=" * 65)
    print("   INSTITUTIONAL TRACKER — 15-YEAR BACKTEST")
    print("   Strategy: Institutional Block Purchase Confluence")
    print("   Period:   January 2010 – December 2024")
    print("=" * 65 + "\n")
    print("⏳ Downloading historical price data (this takes ~30 seconds)...\n")

    from backtester import run_backtest
    results = run_backtest()

    daily_df   = results["daily_df"]
    monthly_df = results["monthly_df"]
    yearly_df  = results["yearly_df"]
    trade_df   = results["trade_df"]

    # -----------------------------------------------------------------------
    # Console Output — Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("   OVERALL PERFORMANCE SUMMARY  (v4 — Institution Weights + Regime + Earnings)")
    print("=" * 65)
    print(f"   Starting Capital:    ${results['starting_capital']:>10,.2f}")
    print(f"   Final Value:         ${results['final_value']:>10,.2f}")
    print(f"   Total Return:        {results['total_return_pct']:>+10.1f}%")
    print(f"   CAGR:                {results['cagr']:>+10.1f}%")
    print(f"   Sharpe Ratio:        {results['sharpe_ratio']:>10.2f}")
    print(f"   Max Drawdown:        {results['max_drawdown_pct']:>+10.1f}%")
    print(f"   Win Rate:            {results['win_rate']:>10.1f}%")
    print(f"   Avg Winning Trade:   {results['avg_win_pct']:>+10.1f}%")
    print(f"   Avg Losing Trade:    {results['avg_loss_pct']:>+10.1f}%")
    print(f"   Total Trades:        {results['n_trades']:>10d}")
    print("-" * 65)
    print(f"   SPY Total Return:    {results['spy_total_return']:>+10.1f}%")
    print(f"   SPY CAGR:            {results['spy_cagr']:>+10.1f}%")
    print(f"   ALPHA (vs SPY):      {results['alpha']:>+10.1f}% per year")
    print("=" * 65)
    print(f"\n   FILTER SUMMARY")
    print(f"   Signals blocked by regime filter:   {results['n_regime_blocked']:>4d}")
    print(f"   Signals blocked by earnings filter: {results['n_earnings_blocked']:>4d}")
    total_sigs = len(results.get("filter_log", [])) + results["n_regime_blocked"] + results["n_earnings_blocked"]
    passed = total_sigs - results["n_regime_blocked"] - results["n_earnings_blocked"]
    print(f"   Signals that passed all filters:    {passed:>4d}")
    print("=" * 65)

    if results["n_regime_blocked"] > 0:
        print("\n   REGIME-BLOCKED TRADES (protected capital from downtrend buys):")
        print(f"   {'Date':<12} {'Ticker':<8} {'Conviction':<12} Reason")
        print("   " + "-" * 60)
        for b in results["regime_blocked"]:
            print(f"   {b['date']:<12} {b['ticker']:<8} {b['conviction']:<12.1f} {b['reason']}")

    if results["n_earnings_blocked"] > 0:
        print("\n   EARNINGS-BLOCKED TRADES (avoided weak fundamental signals):")
        print(f"   {'Date':<12} {'Ticker':<8} {'Conviction':<12} Reason")
        print("   " + "-" * 60)
        for b in results["earnings_blocked"]:
            print(f"   {b['date']:<12} {b['ticker']:<8} {b['conviction']:<12.1f} {b['reason']}")

    # -----------------------------------------------------------------------
    # Yearly Results
    # -----------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("   YEARLY RESULTS")
    print("=" * 65)
    print(f"   {'Year':<10} {'End Value':>14} {'Annual Return':>16}")
    print("-" * 45)
    for idx, row in yearly_df.iterrows():
        year = str(idx)[:4]
        val  = row.get("end_value", 0)
        ret  = row.get("annual_return_pct", 0)
        sign = "▲" if ret >= 0 else "▼"
        print(f"   {year:<10} ${val:>13,.2f}   {sign} {abs(ret):>6.1f}%")
    print("=" * 65)

    # -----------------------------------------------------------------------
    # Monthly Results Table
    # -----------------------------------------------------------------------
    print("\n" + "=" * 65)
    print("   MONTHLY RETURNS (all 60 months)")
    print("=" * 65)
    print(f"   {'Month':<18} {'Portfolio Value':>16} {'Monthly Return':>16}")
    print("-" * 55)
    for idx, row in monthly_df.iterrows():
        dt  = str(idx)[:7]
        val = row.get("portfolio_value", 0)
        ret = row.get("monthly_return_pct", 0)
        sign = "▲" if ret >= 0 else "▼"
        print(f"   {dt:<18} ${val:>15,.2f}   {sign} {abs(ret):>6.1f}%")
    print("=" * 65)

    # -----------------------------------------------------------------------
    # Trade Log
    # -----------------------------------------------------------------------
    if not trade_df.empty:
        sells = trade_df[trade_df["action"] == "SELL"].copy()
        if not sells.empty:
            print("\n" + "=" * 65)
            print("   TRADE HISTORY (Completed Trades)")
            print("=" * 65)
            print(f"   {'Date':<12} {'Ticker':<8} {'Shares':>8} {'Price':>10} "
                  f"{'Value':>12} {'P&L %':>10} {'Reason':<15}")
            print("-" * 75)
            for _, row in sells.iterrows():
                pnl_pct = row.get("pnl_pct", 0)
                sign    = "+" if pnl_pct >= 0 else ""
                print(f"   {str(row['date'])[:10]:<12} "
                      f"{row['ticker']:<8} "
                      f"{int(row['shares']):>8} "
                      f"${row['price']:>9.2f} "
                      f"${row['value']:>11,.2f} "
                      f"{sign}{pnl_pct:>8.1f}%  "
                      f"{row.get('reason',''):<15}")
            print("=" * 65)

    # -----------------------------------------------------------------------
    # Generate HTML Report
    # -----------------------------------------------------------------------
    print("\n⏳ Generating HTML report with charts...")
    try:
        from report_generator import build_html_report, save_report

        # Build a fake snapshot for the report header
        final_val  = results["final_value"]
        pnl_total  = final_val - results["starting_capital"]
        snapshot   = {
            "portfolio_value":  final_val,
            "cash":             final_val,
            "buying_power":     final_val,
            "total_invested":   0,
            "unrealized_pnl":   0,
            "pnl_today":        pnl_total,
            "pnl_today_pct":    results["total_return_pct"],
            "n_positions":      0,
            "positions":        [],
            "return_vs_start":  results["total_return_pct"],
        }

        html = build_html_report(
            snapshot=snapshot,
            backtest_results=results,
            paper_day=0,
            report_type="backtest",
        )

        fname = f"backtest_5year_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path  = save_report(html, fname)
        print(f"\n✅ HTML report saved to:\n   {path}")
        print("\n💡 Open this file in your browser to view the full interactive report.")

    except Exception as e:
        print(f"\n⚠️  Could not generate HTML report: {e}")
        print("   Console results above are still valid.")

    print("\n✅ Backtest complete!\n")


if __name__ == "__main__":
    main()

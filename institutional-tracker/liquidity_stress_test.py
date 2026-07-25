#!/usr/bin/env python3
"""
liquidity_stress_test.py — Liquidity Stress Test Agent (#23)
=============================================================
Monthly: "if we needed to liquidate 50% of the portfolio in 3 days, 
what's the expected slippage?"

Reports to IRM. Flags positions where estimated slippage > 1.5%.
Runs: 1st of each month, 9 AM ET.

Slippage model: simple Amihud illiquidity proxy
  slippage ≈ (position_value / ADTV) * 0.5   [% of position value]
where ADTV = avg daily dollar volume (30d).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("liquidity_stress_test")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STRESS_REPORT = os.path.join(DATA_DIR, "liquidity_stress_report.json")

LIQUIDATION_PCT = 0.50  # liquidate 50% of portfolio
TARGET_DAYS     = 3     # target: complete in 3 days
SLIPPAGE_WARN   = 1.5   # flag if estimated slippage > 1.5%


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except: pass
        raise


def get_adtv(ticker: str, days: int = 30) -> float:
    """Return average daily dollar volume (ADTV)."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period=f"{days+5}d", progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        if df.empty:
            return 0.0
        df["dv"] = df["Close"] * df["Volume"]
        return float(df["dv"].tail(days).mean())
    except Exception:
        return 0.0


def run_liquidity_stress() -> dict:
    """Run the liquidity stress test on current portfolio."""
    logger.info("Running monthly liquidity stress test...")
    
    positions = []
    portfolio_value = 0.0
    
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        account = trader.get_account()
        portfolio_value = float(account.get("portfolio_value", 0) or 0)
        pos_df = trader.get_positions()
        if pos_df is not None and not pos_df.empty:
            for _, row in pos_df.iterrows():
                positions.append({
                    "ticker": str(row.get("symbol","")).upper(),
                    "market_value": float(row.get("market_value", 0)),
                })
    except Exception as e:
        logger.warning(f"  Alpaca unavailable: {e} — using placeholder data")

    if not positions:
        return {"positions_tested": 0, "flagged": [], "computed_at": datetime.now(timezone.utc).isoformat()}

    liquidation_target = portfolio_value * LIQUIDATION_PCT
    results = []
    flagged = []

    for pos in positions:
        ticker = pos["ticker"]
        value  = pos["market_value"]
        liq_value = value * LIQUIDATION_PCT  # liquidate 50% of each position
        
        adtv = get_adtv(ticker)
        if adtv <= 0:
            est_days = 999
            est_slippage_pct = 5.0  # worst case if no data
        else:
            est_days = liq_value / adtv
            # Amihud-style slippage: more illiquid = more slippage
            # slippage ≈ sqrt(participation_rate) * 0.1
            # participation_rate = daily_sell_vol / ADTV
            daily_sell_vol = liq_value / max(est_days, TARGET_DAYS)
            participation_rate = daily_sell_vol / adtv
            est_slippage_pct = min(participation_rate ** 0.5 * 10, 10.0)

        result = {
            "ticker":           ticker,
            "market_value":     round(value, 2),
            "liquidation_50pct":round(liq_value, 2),
            "adtv":             round(adtv, 0),
            "est_days_to_fill": round(est_days, 1),
            "est_slippage_pct": round(est_slippage_pct, 2),
            "flagged":          est_slippage_pct > SLIPPAGE_WARN,
        }
        results.append(result)
        if result["flagged"]:
            flagged.append(result)
            logger.warning(f"  FLAGGED: {ticker} slippage={est_slippage_pct:.1f}% days={est_days:.1f}")

    portfolio_slippage = sum(r["est_slippage_pct"] * r["market_value"] for r in results) / max(sum(r["market_value"] for r in results), 1)

    report = {
        "computed_at":           datetime.now(timezone.utc).isoformat(),
        "portfolio_value":       round(portfolio_value, 2),
        "liquidation_scenario":  f"50% of portfolio in {TARGET_DAYS} days",
        "positions_tested":      len(results),
        "flagged_count":         len(flagged),
        "portfolio_avg_slippage":round(portfolio_slippage, 2),
        "results":               results,
        "flagged_positions":     flagged,
    }
    _save_json(STRESS_REPORT, report)

    # Post to IRM
    lines = [
        f"💧 MONTHLY LIQUIDITY STRESS TEST — {report['computed_at'][:10]}",
        f"  Scenario: liquidate 50% of ${portfolio_value/1e3:.0f}K portfolio in 3 days",
        f"  Avg expected slippage: {portfolio_slippage:.2f}%",
        f"  Positions tested: {len(results)} | Flagged (>1.5% slippage): {len(flagged)}",
    ]
    if flagged:
        lines.append("  FLAGGED positions:")
        for f in flagged[:5]:
            lines.append(f"    {f['ticker']}: slippage={f['est_slippage_pct']:.1f}%, ADTV=${f['adtv']/1e6:.1f}M, days={f['est_days_to_fill']:.1f}")
    
    summary = "\n".join(lines)
    try:
        from agent_meeting import request_info, post_bulletin
        request_info(
            from_agent  = "LiquidityStressTest",
            to_agent    = "IndependentRiskManager",
            topic       = "Monthly Liquidity Stress Test Report",
            context     = summary,
            urgency     = "HIGH" if flagged else "NORMAL",
            callback_hint="Review illiquid positions and consider sizing adjustments",
        )
        if flagged:
            post_bulletin(
                author    = "LiquidityStressTest",
                topic     = "risk",
                title     = "Liquidity Stress Test: Flagged Positions",
                body      = summary,
                urgency   = "HIGH",
                ttl_hours = 168.0,
            )
    except Exception as e:
        logger.debug(f"  Alert dispatch error: {e}")

    logger.info(f"  Report: {len(flagged)} flagged, portfolio slippage={portfolio_slippage:.2f}%")
    return report


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("LiquidityStressTest")
        sys.exit(0)
    result = run_liquidity_stress()
    print(f"\nLiquidity stress: {result['flagged_count']} flagged of {result['positions_tested']}")
    print(f"Avg slippage: {result['portfolio_avg_slippage']:.2f}%")

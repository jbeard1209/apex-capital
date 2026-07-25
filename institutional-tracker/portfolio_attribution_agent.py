#!/usr/bin/env python3
"""
portfolio_attribution_agent.py — Portfolio Attribution Agent (#22)
===================================================================
Weekly, decomposes portfolio returns into:
  - Which picks from which signal source (ML, R&D, FI, FE) generated alpha
  - Which picks lost money
  - Alpha vs benchmark (SPY)
  
Feeds back to each pod as a performance review input.
Posts weekly attribution report to board and firm_channel.
Runs every Monday 9:30 AM ET.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("portfolio_attribution")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

ATTRIBUTION_LOG   = os.path.join(DATA_DIR, "attribution_log.json")
ATTRIBUTION_REPORT = os.path.join(DATA_DIR, "attribution_report.json")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
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


def get_spy_return(days: int = 7) -> float:
    """Get SPY return over last N days as benchmark."""
    try:
        import yfinance as yf
        df = yf.download("SPY", period=f"{days+3}d", progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if len(close) < 2:
            return 0.0
        return float((close.iloc[-1] / close.iloc[-min(days, len(close)-1)] - 1) * 100)
    except Exception:
        return 0.0


def get_position_returns(days: int = 7) -> list[dict]:
    """
    Get returns for current + recently closed positions.
    Uses Alpaca positions + performance_ledger if available.
    """
    positions = []
    
    # Try Alpaca for current positions
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        pos_df = trader.get_positions()
        if pos_df is not None and not pos_df.empty:
            for _, row in pos_df.iterrows():
                ticker  = str(row.get("symbol", "")).upper()
                unrealized_pnl = float(row.get("unrealized_pl", 0))
                unrealized_pct = float(row.get("unrealized_plpc", 0)) * 100
                positions.append({
                    "ticker":    ticker,
                    "pnl":       unrealized_pnl,
                    "pnl_pct":  round(unrealized_pct, 2),
                    "current":   float(row.get("current_price", 0)),
                    "entry":     float(row.get("avg_entry_price", 0)),
                    "status":    "open",
                })
    except Exception as e:
        logger.debug(f"  Alpaca positions error: {e}")

    # Try manager_decisions.json for signal source attribution
    decisions = _load_json(os.path.join(DATA_DIR, "manager_decisions.json"), [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    decision_map = {}
    for d in decisions:
        if d.get("timestamp", "") >= cutoff:
            t = d.get("ticker","")
            if t and d.get("action") in ("BUY_NEW","ADD"):
                decision_map[t] = {
                    "source": d.get("source", d.get("signal_source", "Unknown")),
                    "pod":    _infer_pod(d),
                }

    for pos in positions:
        t = pos["ticker"]
        if t in decision_map:
            pos["source"] = decision_map[t]["source"]
            pos["pod"]    = decision_map[t]["pod"]
        else:
            pos["source"] = "Unknown"
            pos["pod"]    = "Unknown"

    return positions


def _infer_pod(decision: dict) -> str:
    """Infer which pod a decision came from based on source/metadata."""
    source = str(decision.get("source", decision.get("signal_source", ""))).lower()
    if "ml" in source or "model" in source or "shortterm" in source or "longterm" in source:
        return "ML Pod"
    if "rd" in source or "research" in source or "r&d" in source:
        return "R&D Pod"
    if "fi" in source or "fixed" in source or "fundamental" in source:
        return "FI Pod"
    if "fe" in source or "equity" in source:
        return "FE Pod"
    return "Unknown"


def compute_attribution(days: int = 7) -> dict:
    """
    Compute weekly attribution report.
    Returns full attribution dict.
    """
    spy_return = get_spy_return(days)
    positions  = get_position_returns(days)

    # Group by pod
    pod_stats = {}
    for pos in positions:
        pod = pos.get("pod", "Unknown")
        if pod not in pod_stats:
            pod_stats[pod] = {"tickers": [], "total_pnl": 0.0, "winners": 0, "losers": 0, "avg_return": []}
        pod_stats[pod]["tickers"].append(pos["ticker"])
        pod_stats[pod]["total_pnl"] += pos.get("pnl", 0)
        pod_stats[pod]["avg_return"].append(pos.get("pnl_pct", 0))
        if pos.get("pnl_pct", 0) > 0:
            pod_stats[pod]["winners"] += 1
        else:
            pod_stats[pod]["losers"] += 1

    # Compute pod averages
    for pod in pod_stats:
        returns = pod_stats[pod]["avg_return"]
        pod_stats[pod]["avg_return_pct"] = round(sum(returns)/len(returns), 2) if returns else 0.0
        pod_stats[pod]["alpha_vs_spy"]   = round(pod_stats[pod]["avg_return_pct"] - spy_return, 2)
        del pod_stats[pod]["avg_return"]

    # Top winners and losers
    sorted_pos = sorted(positions, key=lambda x: x.get("pnl_pct", 0), reverse=True)
    top_winners = sorted_pos[:5]
    top_losers  = sorted_pos[-5:] if len(sorted_pos) >= 5 else sorted_pos[::-1][:5]

    total_pnl   = sum(p.get("pnl", 0) for p in positions)
    total_pnl_pct = sum(p.get("pnl_pct", 0) for p in positions) / max(len(positions), 1)

    report = {
        "computed_at":    datetime.now(timezone.utc).isoformat(),
        "period_days":    days,
        "spy_return":     round(spy_return, 2),
        "portfolio_avg_return": round(total_pnl_pct, 2),
        "portfolio_alpha": round(total_pnl_pct - spy_return, 2),
        "total_pnl":      round(total_pnl, 2),
        "positions_count":len(positions),
        "winners":        sum(1 for p in positions if p.get("pnl_pct", 0) > 0),
        "losers":         sum(1 for p in positions if p.get("pnl_pct", 0) <= 0),
        "pod_attribution":pod_stats,
        "top_winners":    [{"ticker": p["ticker"], "pnl_pct": p.get("pnl_pct"), "pod": p.get("pod")} for p in top_winners],
        "top_losers":     [{"ticker": p["ticker"], "pnl_pct": p.get("pnl_pct"), "pod": p.get("pod")} for p in top_losers if p.get("pnl_pct", 0) < 0],
    }

    _save_json(ATTRIBUTION_REPORT, report)
    
    # Append to log
    log = _load_json(ATTRIBUTION_LOG, [])
    log.append({"report_date": datetime.now(timezone.utc).isoformat(), "summary": {k: v for k, v in report.items() if k != "pod_attribution"}})
    log = log[-52:]  # keep 1 year of weekly reports
    _save_json(ATTRIBUTION_LOG, log)

    return report


def post_attribution_report(report: dict) -> None:
    """Post the attribution report to firm_channel and send to pod leads."""
    lines = [
        f"📊 WEEKLY PORTFOLIO ATTRIBUTION — {report['computed_at'][:10]}",
        f"  Portfolio return: {report['portfolio_avg_return']:+.2f}% | SPY: {report['spy_return']:+.2f}% | Alpha: {report['portfolio_alpha']:+.2f}%",
        f"  Winners: {report['winners']} | Losers: {report['losers']} | Total P&L: ${report['total_pnl']:,.0f}",
        "",
        "  Pod Attribution:",
    ]
    for pod, stats in report["pod_attribution"].items():
        lines.append(f"    {pod}: avg={stats['avg_return_pct']:+.2f}% alpha={stats['alpha_vs_spy']:+.2f}% W/L={stats['winners']}/{stats['losers']}")
    
    if report["top_winners"]:
        lines.append("  Top Winners: " + ", ".join(f"{w['ticker']}({w['pnl_pct']:+.1f}%)" for w in report["top_winners"]))
    if report["top_losers"]:
        lines.append("  Top Losers: " + ", ".join(f"{l['ticker']}({l['pnl_pct']:+.1f}%)" for l in report["top_losers"]))

    summary = "\n".join(lines)

    try:
        from agent_meeting import post_bulletin, request_info
        post_bulletin(
            author   = "PortfolioAttributionAgent",
            topic    = "performance",
            title    = f"Weekly Attribution Report — {report['computed_at'][:10]}",
            body     = summary,
            urgency  = "NORMAL",
            ttl_hours= 168.0,
        )
        # Send to pod leads
        for target in ["MLShortTermBoard", "RDTeamLeader", "FIBoard", "FEBoard"]:
            try:
                request_info(
                    from_agent  = "PortfolioAttributionAgent",
                    to_agent    = target,
                    topic       = "Weekly Attribution Report",
                    context     = summary,
                    urgency     = "NORMAL",
                    callback_hint="Review your pod's attribution and adjust strategy if needed",
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"  Post error: {e}")
    
    try:
        from firm_channel import post
        post("PortfolioAttributionAgent", "ATTRIBUTION", summary, priority="NORMAL")
    except Exception:
        pass

    logger.info(f"  Attribution report posted: alpha={report['portfolio_alpha']:+.2f}%")


def run_attribution():
    """Main entrypoint."""
    logger.info("Running weekly portfolio attribution...")
    report = compute_attribution(days=7)
    post_attribution_report(report)
    return report


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("PortfolioAttributionAgent")
        sys.exit(0)
    
    report = run_attribution()
    print(f"\nAttribution: alpha={report['portfolio_alpha']:+.2f}% vs SPY={report['spy_return']:+.2f}%")
    print(f"Pods: {json.dumps(report['pod_attribution'], indent=2)}")

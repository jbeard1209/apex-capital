#!/usr/bin/env python3
"""
position_aging_agent.py — Research Freshness Enforcement
=========================================================
Tracks how long each open position has been held WITHOUT fresh R&D coverage.
If a position has no R&D coverage in 14+ days: file MEDIUM issue + alert CoS.
If 30+ days without coverage: file HIGH issue + request immediate R&D research.

This enforces the "no research = no hold" rule across the portfolio.

Schedule: daily 08:00 ET via crontab
"""

from __future__ import annotations   # PEP 604 (`str | None`) on Python 3.9 (Mac host)

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("position_aging")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "position_aging_report.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def get_open_positions() -> list[dict]:
    """Get open positions from Alpaca."""
    try:
        sys.path.insert(0, BASE_DIR)
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        pos = trader.get_positions()
        if pos.empty:
            return []
        return pos.to_dict("records")
    except Exception as e:
        logger.warning(f"  Could not load positions: {e}")
        return []


def get_last_research_date(ticker: str, research_report: dict) -> str | None:
    """Find most recent R&D coverage — checks watchlists, picks, sector assignments."""
    report_date = research_report.get("generated_at")
    if not report_date:
        return None
    # 1. agent_reports — top_picks, watchlists, tickers_analysed
    for agent in research_report.get("agent_reports", []):
        if not isinstance(agent, dict):
            continue
        if ticker in agent.get("watchlist", []):
            return report_date
        tickers_analysed = agent.get("tickers_analysed", [])
        if isinstance(tickers_analysed, int): tickers_analysed = []
        for pick in (agent.get("top_picks", []) + agent.get("recommendations", [])
                     + tickers_analysed):
            if isinstance(pick, dict) and pick.get("ticker") == ticker:
                return report_date
            if isinstance(pick, str) and pick == ticker:
                return report_date
    # 2. Direct picks list
    for pick in research_report.get("picks", []) + research_report.get("recommendations", []):
        if isinstance(pick, dict) and pick.get("ticker") == ticker:
            return report_date
    # 3. Sector assignments watchlists
    import os as _os, json as _j
    # Resolved from this file, not a host literal: on the Mac the old absolute
    # path simply did not exist, so this lookup silently returned nothing and the
    # agent under-reported position age — no error, just a quieter wrong answer.
    sa_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "data", "sector_assignments.json")
    if _os.path.exists(sa_path):
        try:
            sa = _j.load(open(sa_path))
            for a in sa.get("assignments", []):
                if isinstance(a, dict) and ticker in a.get("watchlist", []):
                    return sa.get("generated_at", report_date)
        except Exception:
            pass
    return None



# ETFs / inverse ETFs / sector ETFs do not need individual R&D research coverage
ETF_TICKERS = frozenset([
    "SH", "SPY", "QQQ", "IWM", "XLK", "XLE", "XLY", "XLF", "XLV", "XLI",
    "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "IBB", "IEF", "TLT", "LQD",
    "HYG", "GLD", "SLV", "USO", "VNQ", "VEA", "VWO", "EFA", "EEM", "MDY",
    "BNDX", "MCHI", "EWJ",
])

def check_position_research_freshness() -> list[dict]:
    """Check research freshness for all open positions."""
    positions = get_open_positions()
    if not positions:
        logger.info("  No open positions to check")
        return []

    # Load research report
    research = _load_json(os.path.join(DATA_DIR, "research_team_report.json"), {})
    rd_report_age_h = 999
    if research.get("generated_at"):
        try:
            rd_ts = datetime.fromisoformat(research["generated_at"].replace("Z", "+00:00"))
            rd_report_age_h = (datetime.now(timezone.utc) - rd_ts).total_seconds() / 3600
        except Exception:
            pass

    # Load entry dates from state
    state = _load_json(os.path.join(DATA_DIR, "state.json"), {})
    entry_dates = state.get("entry_dates", {})

    results = []
    now = datetime.now(timezone.utc)

    for pos in positions:
        ticker = pos.get("ticker", "?")
        if ticker in ETF_TICKERS:
            logger.debug(f"  {ticker}: ETF — skipping R&D coverage check")
            results.append({"ticker": ticker, "hold_days": 0,
                            "pnl_pct": round(float(pos.get("unrealized_pct", 0)), 2),
                            "market_value": round(abs(float(pos.get("market_value", 0))), 2),
                            "days_since_research": 0, "last_research": "ETF",
                            "status": "ETF_NO_RESEARCH_NEEDED", "alert_level": None})
            continue
        # A missing entry date used to leave hold_days at 0 — indistinguishable
        # from a position opened this morning, and therefore never old enough to
        # age. `None` keeps "we don't know" separate from "brand new"; the
        # aging verdict below skips rather than clearing the position.
        entry_date_str = entry_dates.get(ticker)
        hold_days = None
        if entry_date_str:
            try:
                entry_dt = datetime.fromisoformat(entry_date_str)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                hold_days = (now - entry_dt).days
            except Exception:
                pass

        # Find last research coverage
        last_rd_str = get_last_research_date(ticker, research)
        days_since_research = 999
        if last_rd_str:
            try:
                _rd_str = last_rd_str.replace("Z", "+00:00")
                rd_dt = datetime.fromisoformat(_rd_str)
                if rd_dt.tzinfo is None:
                    rd_dt = rd_dt.replace(tzinfo=timezone.utc)
                days_since_research = (now - rd_dt).total_seconds() / (3600 * 24)
            except Exception as _e:
                logger.debug(f"  Date parse failed for {ticker}: {_e}")
        else:
            # No research coverage found at all
            days_since_research = hold_days or 999

        pnl_pct = float(pos.get("unrealized_pct", 0))
        market_value = float(pos.get("market_value", 0))

        result = {
            "ticker": ticker,
            "hold_days": hold_days,
            "pnl_pct": round(pnl_pct, 2),
            "market_value": round(abs(market_value), 2),
            "days_since_research": round(days_since_research, 1),
            "last_research": last_rd_str[:10] if last_rd_str else "NEVER",
            "status": "OK",
            "alert_level": None,
        }

        if days_since_research >= 30:
            result["status"] = "STALE_RESEARCH_HIGH"
            result["alert_level"] = "HIGH"
            logger.warning(f"  {ticker}: {days_since_research:.0f} days without research (HIGH)")
        elif days_since_research >= 14:
            result["status"] = "STALE_RESEARCH_MEDIUM"
            result["alert_level"] = "MEDIUM"
            logger.info(f"  {ticker}: {days_since_research:.0f} days without research (MEDIUM)")
        else:
            logger.info(f"  {ticker}: research {days_since_research:.0f}d old — OK")

        results.append(result)

    return results


def _hold_str(hold_days) -> str:
    """Holding period for display. `None` means the ledger could not date the
    position — printing it as "None" or coercing it to 0 both read as facts."""
    return "unknown" if hold_days is None else str(hold_days)


def file_issues_and_alerts(staleness_results: list[dict]):
    """File issue tracker entries for stale research positions."""
    try:
        sys.path.insert(0, BASE_DIR)
        import issue_tracker as it_mod
        from datetime import datetime as dt_mod, timezone as tz_mod
    except ImportError as e:
        logger.warning(f"  Cannot import issue_tracker: {e}")
        return

    for result in staleness_results:
        if not result.get("alert_level"):
            continue
        ticker = result["ticker"]
        days = result["days_since_research"]
        severity = result["alert_level"]

        title = f"Position {ticker}: {days:.0f} days without R&D coverage (hold {_hold_str(result['hold_days'])}d, PnL {result['pnl_pct']:+.1f}%)"
        description = (
            f"Open position {ticker} has not had fresh R&D research in {days:.0f} days. "
            f"Last research: {result['last_research']}. "
            f"Hold duration: {_hold_str(result['hold_days'])} days. "
            f"P&L: {result['pnl_pct']:+.1f}%. "
            f"Action required: {'Emergency R&D request filed.' if severity == 'HIGH' else 'R&D coverage needed within 48h.'}"
        )
        try:
            issue_id = it_mod.log_issue(
                title=title,
                description=description,
                severity=severity,
                category="PORTFOLIO_RISK",
                reporter="PositionAgingAgent",
            )
            logger.info(f"  Filed {severity} issue {issue_id}: {ticker} stale research")
        except Exception as e:
            logger.debug(f"  Issue filing non-fatal: {e}")

        # Request R&D research if HIGH
        if severity == "HIGH":
            try:
                from agent_meeting import manager_request_research
                manager_request_research(
                    topic=f"Urgent research update: {ticker} position held {_hold_str(result['hold_days'])}d without fresh coverage",
                    context=(
                        f"{ticker}: held {_hold_str(result['hold_days'])} days, PnL {result['pnl_pct']:+.1f}%, "
                        f"no R&D coverage in {days:.0f} days. Need fresh thesis review to justify continued hold."
                    ),
                    urgency="HIGH",
                )
                logger.info(f"  Filed urgent R&D research request for {ticker}")
            except Exception as e:
                logger.debug(f"  R&D request non-fatal: {e}")


def run_position_aging():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — POSITION AGING AGENT")
    logger.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info("=" * 60)

    results = check_position_research_freshness()
    if not results:
        logger.info("  No positions to check.")
        return

    high_alerts = [r for r in results if r.get("alert_level") == "HIGH"]
    med_alerts = [r for r in results if r.get("alert_level") == "MEDIUM"]
    ok = [r for r in results if not r.get("alert_level")]

    logger.info(f"\n  Results: {len(ok)} OK, {len(med_alerts)} MEDIUM alerts, {len(high_alerts)} HIGH alerts")

    file_issues_and_alerts(results)

    # Save report
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "positions_checked": len(results),
        "high_alerts": len(high_alerts),
        "medium_alerts": len(med_alerts),
        "results": results,
    }
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, indent=2)
        os.replace(tmp, OUTPUT_PATH)
        logger.info(f"  Report saved → {OUTPUT_PATH}")
    except Exception as e:
        logger.warning(f"  Could not save report: {e}")

    logger.info("\n  POSITION AGING AGENT COMPLETE")


if __name__ == "__main__":
    run_position_aging()

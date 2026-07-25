#!/usr/bin/env python3
"""
short_interest_monitor.py — Short Interest Change Monitor (#21)
==============================================================
Tracks *changes* in short interest for positions + watchlist.
Flags:
  - Short interest doubled in 2 weeks → squeeze setup OR fundamental deterioration
  - Short interest dropped >40% in 2 weeks → short covering signal

Data: yfinance short_ratio + short_percent_of_float (updated by exchanges biweekly)
Alerts: CROAgent on extreme moves. Updates alt_data_signals.json with si_change feature.
Runs: Daily 8:30 AM ET (pre-market).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("short_interest_monitor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

SI_HISTORY    = os.path.join(DATA_DIR, "short_interest_history.json")
SI_ALERTS     = os.path.join(DATA_DIR, "short_interest_alerts.json")


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


def get_short_interest(tickers: list[str]) -> dict:
    """
    Fetch short interest data for list of tickers via yfinance.
    Returns {ticker: {short_ratio, short_pct_float, shares_short}} 
    """
    import yfinance as yf
    result = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            result[ticker] = {
                "short_ratio":    info.get("shortRatio"),
                "short_pct_float": info.get("shortPercentOfFloat"),
                "shares_short":   info.get("sharesShort"),
                "shares_short_prior": info.get("sharesShortPriorMonth"),
                "fetched_at":     datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.debug(f"  SI fetch error for {ticker}: {e}")
    return result


def get_watch_tickers() -> list[str]:
    """Get all tickers to monitor (positions + watchlist + research picks)."""
    tickers = set()
    
    # Current positions
    try:
        from alpaca_trader import AlpacaTrader
        pos_df = AlpacaTrader().get_positions()
        if pos_df is not None and not pos_df.empty:
            tickers.update(str(t).upper() for t in pos_df.get("symbol", []) if t)
    except Exception:
        pass

    # FE watchlist
    watchlist = _load_json(os.path.join(DATA_DIR, "fe_watchlist.json"), [])
    for item in (watchlist if isinstance(watchlist, list) else []):
        t = item.get("ticker","") or item if isinstance(item, str) else ""
        if t:
            tickers.add(str(t).upper())

    # ML picks (last board report)
    ml_report = _load_json(os.path.join(DATA_DIR, "ml_shortterm_board_report.json"), {})
    for pick in ml_report.get("top_picks", ml_report.get("picks", [])):
        t = pick.get("ticker", pick) if isinstance(pick, dict) else str(pick)
        if t:
            tickers.add(str(t).upper())

    return list(tickers)[:50]  # cap at 50 to avoid rate limits


def compute_si_changes(history: dict, current: dict) -> list[dict]:
    """
    Compare current SI to 2-week-ago snapshot.
    Returns list of notable changes.
    """
    alerts = []
    for ticker, cur in current.items():
        prev = history.get(ticker, {})
        if not prev:
            continue

        cur_pct  = cur.get("short_pct_float") or cur.get("short_ratio")
        prev_pct = prev.get("short_pct_float") or prev.get("short_ratio")
        
        if cur_pct is None or prev_pct is None or prev_pct <= 0:
            continue
        
        pct_change = (cur_pct - prev_pct) / prev_pct * 100

        if pct_change >= 100:  # doubled
            alerts.append({
                "ticker":     ticker,
                "alert_type": "SI_DOUBLED",
                "severity":   "HIGH",
                "message":    f"Short interest doubled: {prev_pct:.1%} → {cur_pct:.1%} (+{pct_change:.0f}%)",
                "prev_si":    prev_pct,
                "curr_si":    cur_pct,
                "pct_change": round(pct_change, 1),
                "alerted_at": datetime.now(timezone.utc).isoformat(),
            })
        elif pct_change >= 50:  # up 50%
            alerts.append({
                "ticker":     ticker,
                "alert_type": "SI_RISING",
                "severity":   "MEDIUM",
                "message":    f"Short interest rising fast: {prev_pct:.1%} → {cur_pct:.1%} (+{pct_change:.0f}%)",
                "prev_si":    prev_pct,
                "curr_si":    cur_pct,
                "pct_change": round(pct_change, 1),
                "alerted_at": datetime.now(timezone.utc).isoformat(),
            })
        elif pct_change <= -40:  # short covering
            alerts.append({
                "ticker":     ticker,
                "alert_type": "SI_COVERING",
                "severity":   "MEDIUM",
                "message":    f"Short covering signal: {prev_pct:.1%} → {cur_pct:.1%} ({pct_change:.0f}%)",
                "prev_si":    prev_pct,
                "curr_si":    cur_pct,
                "pct_change": round(pct_change, 1),
                "alerted_at": datetime.now(timezone.utc).isoformat(),
            })

    return alerts


def run_si_monitor():
    """Main entry point."""
    logger.info("Running short interest change monitor...")
    
    tickers = get_watch_tickers()
    if not tickers:
        logger.info("  No tickers to monitor. Run again after positions are built.")
        return {"tickers": 0, "alerts": []}

    logger.info(f"  Monitoring {len(tickers)} tickers: {', '.join(tickers[:10])}...")
    
    # Load 2-week-old snapshot (latest available)
    history = _load_json(SI_HISTORY, {})
    
    # Fetch current SI
    current = get_short_interest(tickers)
    
    # Compute changes
    alerts = compute_si_changes(history, current)
    
    # Save current as new snapshot
    for ticker, data in current.items():
        history[ticker] = {**data, "snapshot_at": datetime.now(timezone.utc).isoformat()}
    _save_json(SI_HISTORY, history)

    # Log alerts
    if alerts:
        all_alerts = _load_json(SI_ALERTS, [])
        all_alerts.extend(alerts)
        all_alerts = all_alerts[-200:]
        _save_json(SI_ALERTS, all_alerts)

        # Notify CROAgent
        try:
            from agent_meeting import request_info
            alert_summary = f"SHORT INTEREST ALERTS ({len(alerts)}):\n" + "\n".join(
                f"  {a['ticker']}: {a['message']}" for a in alerts
            )
            request_info(
                from_agent   = "ShortInterestMonitor",
                to_agent     = "CROAgent",
                topic        = f"Short Interest Alerts: {len(alerts)} tickers",
                context      = alert_summary,
                urgency      = "HIGH" if any(a["severity"] == "HIGH" for a in alerts) else "NORMAL",
                callback_hint= "Review SI alerts and update risk posture if needed",
            )
        except Exception as e:
            logger.debug(f"  Alert dispatch error: {e}")

    # Update alt_data_signals.json with si_doubled flags
    try:
        alt_signals = _load_json(os.path.join(DATA_DIR, "alt_data_signals.json"), {})
        for ticker, data in current.items():
            if ticker not in alt_signals:
                alt_signals[ticker] = {}
            doubled_alert = next((a for a in alerts if a["ticker"] == ticker and a["alert_type"] == "SI_DOUBLED"), None)
            alt_signals[ticker]["si_doubled_flag"]   = 1 if doubled_alert else 0
            alt_signals[ticker]["short_pct_float"]   = data.get("short_pct_float", 0)
            alt_signals[ticker]["si_updated_at"]     = datetime.now(timezone.utc).isoformat()
        _save_json(os.path.join(DATA_DIR, "alt_data_signals.json"), alt_signals)
    except Exception as e:
        logger.debug(f"  alt_data update error: {e}")

    logger.info(f"  Done. {len(current)} fetched, {len(alerts)} alerts: {[a['ticker']+':'+a['alert_type'] for a in alerts]}")
    return {"tickers": len(current), "alerts": alerts}


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("ShortInterestMonitor")
        sys.exit(0)
    result = run_si_monitor()
    print(f"\nMonitored {result['tickers']} tickers, {len(result['alerts'])} alerts")

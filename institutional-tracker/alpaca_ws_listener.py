#!/usr/bin/env python3
"""
alpaca_ws_listener.py — Real-Time WebSocket Price Listener (#31 Phase 1)
Subscribes to Alpaca's WebSocket for current positions.
Writes data/intraday_alerts.json when any position moves >2% in <30 min.
Canary agent reads this file for real-time triggers.
Run: kept alive by @reboot crontab. Use --daemon flag.
"""
import json, logging, os, sys, signal, time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("ws_listener")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ALERTS_FILE = os.path.join(DATA_DIR, "intraday_alerts.json")
LOCK_FILE   = "/tmp/alpaca_ws_listener.lock"

MOVE_THRESHOLD_PCT = 2.0   # alert if position moves >2%
MAX_ALERTS         = 100   # keep last N alerts

def _save(p, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)

def _load(p, d):
    try: return json.load(open(p)) if os.path.exists(p) else d
    except: return d

def get_position_tickers():
    try:
        from alpaca_trader import AlpacaTrader
        pos_df = AlpacaTrader().get_positions()
        if pos_df is not None and not pos_df.empty:
            # `symbol` raised KeyError into a bare `except: pass`, so this
            # returned [] and the listener subscribed to no position at all.
            return list(pos_df["ticker"].str.upper())
    except: pass
    return []

def get_entry_prices():
    """Return {ticker: avg_entry_price} for current positions."""
    try:
        from alpaca_trader import AlpacaTrader
        pos_df = AlpacaTrader().get_positions()
        if pos_df is not None and not pos_df.empty:
            return {str(row["symbol"]).upper(): float(row.get("avg_entry_price",0))
                    for _, row in pos_df.iterrows()}
    except: pass
    return {}

def add_alert(ticker, pct_move, current_price, entry_price):
    alerts = _load(ALERTS_FILE, [])
    alerts.append({
        "ticker":        ticker,
        "pct_move":      round(pct_move, 2),
        "current_price": round(current_price, 4),
        "entry_price":   round(entry_price, 4),
        "direction":     "UP" if pct_move > 0 else "DOWN",
        "alerted_at":    datetime.now(timezone.utc).isoformat(),
        "consumed":      False,
    })
    alerts = alerts[-MAX_ALERTS:]
    _save(ALERTS_FILE, alerts)
    logger.warning(f"  ALERT: {ticker} {pct_move:+.1f}% from entry (${entry_price:.2f} → ${current_price:.2f})")

def run_polling_fallback(interval_s=60):
    """
    Polling fallback when WebSocket is unavailable.
    Checks positions every N seconds for significant moves.
    """
    logger.info(f"  Polling mode: checking positions every {interval_s}s")
    entry_prices = get_entry_prices()

    while True:
        try:
            import yfinance as yf
            tickers = list(entry_prices.keys())
            if not tickers:
                entry_prices = get_entry_prices()
                time.sleep(interval_s); continue

            for ticker in tickers:
                try:
                    info  = yf.Ticker(ticker).fast_info
                    price = float(info.get("last_price") or info.get("regularMarketPrice") or 0)
                    entry = entry_prices.get(ticker, 0)
                    if price > 0 and entry > 0:
                        pct = (price - entry) / entry * 100
                        if abs(pct) >= MOVE_THRESHOLD_PCT:
                            add_alert(ticker, pct, price, entry)
                except Exception: pass
        except Exception as e:
            logger.debug(f"  Polling error: {e}")
        time.sleep(interval_s)

def run_websocket():
    """Try Alpaca WebSocket stream; fall back to polling if unavailable."""
    try:
        from alpaca.data.live import StockDataStream
        from alpaca.data.enums import DataFeed
        import os as _os
        api_key    = _os.environ.get("ALPACA_API_KEY","")
        api_secret = _os.environ.get("ALPACA_SECRET_KEY","")
        if not api_key:
            # Try loading from config
            try:
                from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
                api_key, api_secret = ALPACA_API_KEY, ALPACA_SECRET_KEY
            except: pass
        if not api_key:
            logger.info("  No Alpaca keys found for WebSocket — using polling fallback")
            run_polling_fallback(); return

        tickers = get_position_tickers()
        if not tickers:
            logger.info("  No positions to monitor — waiting 5min and retrying")
            time.sleep(300); run_websocket(); return

        entry_prices = get_entry_prices()
        last_prices  = {}

        wss = StockDataStream(api_key, api_secret, feed=DataFeed.IEX)

        async def on_bar(bar):
            ticker = bar.symbol.upper()
            price  = float(bar.close)
            entry  = entry_prices.get(ticker, 0)
            if entry > 0:
                pct = (price - entry) / entry * 100
                if abs(pct) >= MOVE_THRESHOLD_PCT:
                    prev_alerted = last_prices.get(ticker, 0)
                    if abs(pct - prev_alerted) > 0.5:  # avoid re-alerting same move
                        add_alert(ticker, pct, price, entry)
                        last_prices[ticker] = pct

        wss.subscribe_bars(on_bar, *tickers)
        logger.info(f"  WebSocket subscribed: {', '.join(tickers)}")
        wss.run()
    except ImportError:
        logger.info("  alpaca-py not available for streaming — using polling fallback")
        run_polling_fallback()
    except Exception as e:
        logger.warning(f"  WebSocket error: {e} — falling back to polling")
        run_polling_fallback()

if __name__ == "__main__":
    # Was `main_inbox_only("WSListener")` — a hardcoded name that ignores the
    # `--as` wake_agent passes. AGENT_REGISTRY knows this script as
    # "AlpacaWSListener", so a wake for AlpacaWSListener opened WSListener's
    # inbox instead: the exact silent identifier mismatch CLAUDE.md names as
    # this system's main bug class, one layer below where we usually look for it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('WSListener'):
        raise SystemExit(0)

    if "--status" in sys.argv:
        alerts = _load(ALERTS_FILE, [])
        recent = [a for a in alerts if not a.get("consumed")]
        print(f"Intraday alerts: {len(alerts)} total, {len(recent)} unconsumed")
        for a in recent[-5:]: print(f"  {a['ticker']} {a['pct_move']:+.1f}% at {a['alerted_at'][:16]}")
        sys.exit(0)

    # Single-instance guard
    import fcntl
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("WS listener already running — exiting")
        sys.exit(0)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    logger.info("Alpaca WebSocket listener starting...")
    run_websocket()

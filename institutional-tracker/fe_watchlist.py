#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_watchlist.py -- Watchlist system (research now, buy when price drops to target)
"""

import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
WATCHLIST_FILE = str(BASE_DIR / "data" / "fe_watchlist.json")


def _load() -> dict:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"watchlist": []}


def _save(data: dict):
    Path(WATCHLIST_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_to_watchlist(ticker, target_entry_price, current_iv, notes=""):
    """Add ticker to watchlist with target entry price (where MoS >= 25%)."""
    ticker = ticker.upper().strip()
    data = _load()

    # Check if already on watchlist
    for item in data["watchlist"]:
        if item["ticker"] == ticker:
            item["target_entry_price"] = float(target_entry_price)
            item["current_iv"] = float(current_iv)
            item["notes"] = notes
            item["status"] = "watching"
            _save(data)
            logger.info(f"[WATCHLIST] Updated {ticker}")
            return {"action": "updated", "ticker": ticker}

    entry = {
        "ticker": ticker,
        "target_entry_price": float(target_entry_price),
        "current_iv": float(current_iv),
        "added_date": str(date.today()),
        "notes": notes,
        "status": "watching",
    }
    data["watchlist"].append(entry)
    _save(data)
    logger.info(f"[WATCHLIST] Added {ticker} at target {target_entry_price}")
    return {"action": "added", "ticker": ticker, "entry": entry}


def check_watchlist_alerts() -> list:
    """Check current prices vs targets. Returns list of triggered alerts."""
    data = _load()
    alerts = []

    if not data["watchlist"]:
        return []

    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        logger.error("[WATCHLIST] yfinance not installed")
        return []

    for item in data["watchlist"]:
        if item.get("status") != "watching":
            continue
        ticker = item["ticker"]
        target = item["target_entry_price"]
        try:
            info = yf.Ticker(ticker).info or {}
            current_price = info.get("currentPrice") or info.get("regularMarketPrice")
            if current_price is None:
                hist = yf.Ticker(ticker).history(period="2d")
                if not hist.empty:
                    current_price = float(hist["Close"].iloc[-1])
            if current_price is None:
                continue
            current_price = float(current_price)
            if current_price <= target:
                alerts.append({
                    "ticker": ticker,
                    "current_price": current_price,
                    "target_entry_price": target,
                    "current_iv": item.get("current_iv"),
                    "notes": item.get("notes", ""),
                    "discount_to_iv": round((item.get("current_iv", 0) - current_price) / item.get("current_iv", 1) * 100, 1) if item.get("current_iv") else None,
                })
        except Exception as e:
            logger.warning(f"[WATCHLIST] Price check failed for {ticker}: {e}")

    return alerts


def get_watchlist() -> list:
    """Return full watchlist."""
    return _load().get("watchlist", [])


def remove_from_watchlist(ticker):
    """Remove ticker from watchlist."""
    ticker = ticker.upper().strip()
    data = _load()
    original_len = len(data["watchlist"])
    data["watchlist"] = [item for item in data["watchlist"] if item["ticker"] != ticker]
    removed = original_len - len(data["watchlist"])
    _save(data)
    return {"action": "removed", "ticker": ticker, "removed": removed}


if __name__ == "__main__":
    import sys, json
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "add" and len(sys.argv) >= 4:
        print(json.dumps(add_to_watchlist(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]) if len(sys.argv) > 4 else 0), indent=2))
    elif cmd == "check":
        print(json.dumps(check_watchlist_alerts(), indent=2))
    elif cmd == "remove" and len(sys.argv) >= 3:
        print(json.dumps(remove_from_watchlist(sys.argv[2]), indent=2))
    else:
        print(json.dumps(get_watchlist(), indent=2))

#!/usr/bin/env python3
"""
quiver_signal_agent.py — Quiver Quantitative Signals (#32)
Phase 1: Congressional trades → congress_net_buys_90d feature
Phase 2: Government contract awards
Phase 3: Lobbying + insider combo

Requires QUIVER_API_KEY env var. Get free key at: https://api.quiverquant.com/
Set with: echo 'export QUIVER_API_KEY="your_key"' >> ~/.bashrc
"""
import json, logging, os, sys, time
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("quiver_signal")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
QUIVER_SIGNALS = os.path.join(DATA_DIR, "quiver_signals.json")
QUIVER_BASE    = "https://api.quiverquant.com/beta"

def _save(p, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)

def _load(p, d):
    try: return json.load(open(p)) if os.path.exists(p) else d
    except: return d

def get_api_key():
    key = os.environ.get("QUIVER_API_KEY", "")
    if not key:
        logger.warning("  QUIVER_API_KEY not set. Get free key at https://api.quiverquant.com/")
        logger.warning("  Run: echo 'export QUIVER_API_KEY=\"your_key\"' >> ~/.bashrc")
        logger.warning("  Then: source ~/.bashrc && crontab -e (add QUIVER_API_KEY=your_key line)")
    return key

def fetch_congress_trades(ticker, api_key, days_back=90):
    """Congressional stock trades for a ticker."""
    try:
        import requests
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = f"{QUIVER_BASE}/historical/congresstrading/{ticker}"
        r = requests.get(url, headers={"Authorization": f"Token {api_key}"}, timeout=15)
        if r.status_code == 401: logger.error("  Invalid API key"); return []
        if r.status_code != 200: return []
        trades = r.json()
        recent = [t for t in trades if t.get("Date","") >= cutoff]
        return recent
    except Exception as e:
        logger.debug(f"  Congress fetch error for {ticker}: {e}")
        return []

def fetch_gov_contracts(ticker, api_key, days_back=90):
    """Government contract awards for a ticker."""
    try:
        import requests
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = f"{QUIVER_BASE}/historical/govcontracts/{ticker}"
        r = requests.get(url, headers={"Authorization": f"Token {api_key}"}, timeout=15)
        if r.status_code != 200: return []
        contracts = r.json()
        return [c for c in contracts if c.get("Date","") >= cutoff]
    except Exception as e:
        logger.debug(f"  GovContracts fetch error for {ticker}: {e}")
        return []

def compute_congress_score(trades):
    """
    Returns (net_buys, buy_count, sell_count, total_value_k).
    Net positive = more congressional buying = bullish signal.
    """
    buys = [t for t in trades if "purchase" in str(t.get("Transaction","")).lower()
                               or "buy" in str(t.get("Transaction","")).lower()]
    sells= [t for t in trades if "sale" in str(t.get("Transaction","")).lower()
                               or "sell" in str(t.get("Transaction","")).lower()]
    def _parse_val(t):
        try:
            rng = str(t.get("Range", t.get("Amount","0")))
            # e.g. "$15,001 - $50,000" -> take midpoint
            nums = [int(x.replace("$","").replace(",","")) for x in rng.split("-") if x.strip().replace("$","").replace(",","").isdigit()]
            return sum(nums) / len(nums) if nums else 0
        except: return 0
    buy_val  = sum(_parse_val(t) for t in buys)
    sell_val = sum(_parse_val(t) for t in sells)
    return len(buys) - len(sells), len(buys), len(sells), round((buy_val - sell_val) / 1000, 1)

def get_watch_tickers():
    tickers = set()
    try:
        from alpaca_trader import AlpacaTrader
        pos_df = AlpacaTrader().get_positions()
        if pos_df is not None and not pos_df.empty:
            tickers.update(str(t).upper() for t in pos_df.get("symbol",[]) if t)
    except: pass
    for src in ["fe_watchlist.json", "fe_picks_queue.json"]:
        items = _load(os.path.join(DATA_DIR, src), [])
        for item in (items if isinstance(items, list) else []):
            t = item.get("ticker","") if isinstance(item, dict) else str(item)
            if t: tickers.add(str(t).upper())
    return list(tickers)[:40]

def run_quiver_signals():
    logger.info("Running Quiver Quantitative signal fetch...")
    api_key = get_api_key()
    if not api_key:
        # Write placeholder so feature_engineer doesn't crash
        _save(QUIVER_SIGNALS, {"updated_at": datetime.now(timezone.utc).isoformat(),
                                "status": "NO_API_KEY", "tickers": {}})
        return {"status": "NO_API_KEY"}

    tickers = get_watch_tickers()
    logger.info(f"  Fetching Quiver signals for {len(tickers)} tickers...")
    results = {}

    for ticker in tickers:
        # Congress trades
        trades = fetch_congress_trades(ticker, api_key)
        net_buys, buy_ct, sell_ct, net_val_k = compute_congress_score(trades)

        # Gov contracts
        contracts = fetch_gov_contracts(ticker, api_key)
        contract_val_m = sum(float(c.get("Amount", 0) or 0) for c in contracts) / 1e6

        results[ticker] = {
            "congress_net_buys_90d":  net_buys,
            "congress_buy_count":     buy_ct,
            "congress_sell_count":    sell_ct,
            "congress_net_value_k":   net_val_k,
            "congress_buy_flag":      int(net_buys > 0),
            "govcontract_award_90d_m":round(contract_val_m, 2),
            "govcontract_flag":       int(contract_val_m > 50),  # >$50M = notable
            "updated_at":             datetime.now(timezone.utc).isoformat(),
        }
        if net_buys > 0 or contract_val_m > 50:
            logger.info(f"  {ticker}: congress_net={net_buys} contracts=${contract_val_m:.0f}M")
        time.sleep(0.3)  # rate limit courtesy

    _save(QUIVER_SIGNALS, {"updated_at": datetime.now(timezone.utc).isoformat(),
                            "status": "OK", "tickers": results})
    # Flag notable signals
    notable = [t for t, v in results.items() if v["congress_buy_flag"] or v["govcontract_flag"]]
    if notable:
        try:
            from agent_meeting import post_bulletin
            post_bulletin("QuiverSignalAgent", "research",
                f"Quiver Notable Signals: {', '.join(notable)}",
                "\n".join(f"  {t}: congress_net={results[t]['congress_net_buys_90d']} contracts=${results[t]['govcontract_award_90d_m']:.0f}M" for t in notable),
                urgency="NORMAL", ttl_hours=168.0)
        except: pass

    logger.info(f"  Done: {len(results)} tickers, {len(notable)} notable")
    return {"status": "OK", "tickers_analyzed": len(results), "notable": notable}

if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("QuiverSignalAgent"); sys.exit(0)
    r = run_quiver_signals()
    if r.get("status") == "NO_API_KEY":
        print("\nQuiver: waiting for API key. Register at https://api.quiverquant.com/")
    else:
        print(f"\nQuiver: {r['tickers_analyzed']} tickers, {len(r['notable'])} notable")

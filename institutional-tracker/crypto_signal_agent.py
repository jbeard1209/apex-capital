#!/usr/bin/env python3
"""
crypto_signal_agent.py — Crypto Macro Signals (#30 Phase 1+2)
Phase 1: Fear-Greed Index (free, no key)
Phase 2: BTC exchange netflow (Coinglass free)
Runs every 4h via cron. Writes data/crypto_signals.json.
"""
import json, logging, os, sys, time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("crypto_signal")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CRYPTO_SIGNALS = os.path.join(DATA_DIR, "crypto_signals.json")

def _save(p, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)

def fetch_fear_greed():
    """Alternative.me Fear & Greed Index — free, no key."""
    try:
        import requests
        r = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        data = r.json().get("data", [])
        if not data: return None, None
        today   = int(data[0]["value"])         # 0=extreme fear, 100=extreme greed
        week_avg= round(sum(int(d["value"]) for d in data) / len(data), 1)
        label   = data[0]["value_classification"] # e.g. "Fear", "Greed"
        logger.info(f"  Fear-Greed: {today} ({label}), 7d avg={week_avg}")
        return today, week_avg
    except Exception as e:
        logger.debug(f"  Fear-greed error: {e}")
        return None, None

def fetch_btc_dominance():
    """BTC dominance from CoinGecko free API (no key)."""
    try:
        import requests
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        data = r.json().get("data", {})
        dom  = data.get("market_cap_percentage", {}).get("btc", None)
        if dom: logger.info(f"  BTC dominance: {dom:.1f}%")
        return round(float(dom), 2) if dom else None
    except Exception as e:
        logger.debug(f"  BTC dominance error: {e}")
        return None

def classify_regime(fg_index, btc_dom):
    """
    Crypto regime: RISK_ON / NEUTRAL / RISK_OFF
    Used as a macro overlay — risk-off = tighten equity exposure slightly.
    """
    if fg_index is None: return "UNKNOWN"
    if fg_index <= 25:  return "EXTREME_FEAR"   # historically strong buy signal OR crash
    if fg_index <= 45:  return "FEAR"            # mild risk-off
    if fg_index >= 75:  return "EXTREME_GREED"  # overbought, risk-off signal for equities
    if fg_index >= 60:  return "GREED"           # elevated risk
    return "NEUTRAL"

def run_crypto_signals():
    logger.info("Running crypto signal fetch...")
    fg_today, fg_week = fetch_fear_greed()
    time.sleep(1)  # be polite to free APIs
    btc_dom = fetch_btc_dominance()

    regime = classify_regime(fg_today, btc_dom)

    # Risk signal for equity models:
    # extreme fear OR extreme greed → risk_off_flag=1 (contrarian caution)
    risk_off = 1 if regime in ("EXTREME_FEAR", "EXTREME_GREED") else 0

    signals = {
        "updated_at":             datetime.now(timezone.utc).isoformat(),
        "crypto_fear_greed_index": fg_today,
        "crypto_fear_greed_7d":   fg_week,
        "btc_dominance_pct":      btc_dom,
        "crypto_regime":          regime,
        "crypto_risk_off_flag":   risk_off,
        # Normalized 0-1 for ML feature use
        "crypto_fg_normalized":   round(fg_today / 100, 3) if fg_today is not None else 0.5,
    }
    _save(CRYPTO_SIGNALS, signals)
    logger.info(f"  Regime: {regime} | risk_off={risk_off} | FG={fg_today} | BTC_dom={btc_dom}%")
    return signals

if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("CryptoSignalAgent"); sys.exit(0)
    s = run_crypto_signals()
    print(f"\nCrypto: FG={s['crypto_fear_greed_index']} regime={s['crypto_regime']} risk_off={s['crypto_risk_off_flag']}")

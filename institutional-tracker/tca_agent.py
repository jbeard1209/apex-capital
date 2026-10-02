#!/usr/bin/env python3
"""
tca_agent.py — Transaction Cost Analysis (#33)
Phase 1: Fill quality report (slippage vs NBBO midpoint)
Phase 2: Size-aware slippage model fed back to compliance
"""
import json, logging, os, sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("tca_agent")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TCA_REPORT = os.path.join(DATA_DIR, "tca_report.json")
TCA_MODEL  = os.path.join(DATA_DIR, "tca_slippage_model.json")

def _load(p, d):
    try: return json.load(open(p)) if os.path.exists(p) else d
    except: return d

def _save(p, data):
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)

def get_fills_from_alpaca(days_back=90):
    """Fetch filled orders from Alpaca API."""
    fills = []
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        # Try to get order history
        try:
            orders = trader.client.get_orders(status="filled", limit=500,
                after=(datetime.now(timezone.utc)-timedelta(days=days_back)).isoformat())
            for o in orders:
                fills.append({
                    "order_id":   str(o.id),
                    "ticker":     str(o.symbol),
                    "side":       str(o.side),
                    "qty":        float(o.qty or 0),
                    "fill_price": float(o.filled_avg_price or 0),
                    "filled_at":  str(o.filled_at or ""),
                    "order_type": str(o.order_type),
                    "notional":   float(o.qty or 0) * float(o.filled_avg_price or 0),
                })
        except Exception:
            # Fallback: read execution_quality.json
            eq = _load(os.path.join(DATA_DIR, "execution_quality.json"), [])
            for e in eq:
                if e.get("fill_price") and e.get("decision_price"):
                    fills.append({
                        "order_id":   e.get("order_id",""),
                        "ticker":     e.get("ticker",""),
                        "side":       "buy" if e.get("action","") in ("BUY_NEW","ADD") else "sell",
                        "fill_price": float(e["fill_price"]),
                        "decision_price": float(e["decision_price"]),
                        "filled_at":  e.get("timestamp",""),
                        "notional":   float(e.get("fill_price",0)) * float(e.get("qty",1)),
                    })
    except Exception as e:
        logger.debug(f"  Alpaca fills error: {e}")
    return fills

def get_midpoint_at_fill(ticker, filled_at):
    """Approximate NBBO midpoint using yfinance 1-min data around fill time."""
    try:
        import yfinance as yf, pandas as pd
        from datetime import datetime as dt
        ts = pd.Timestamp(filled_at)
        start = (ts - pd.Timedelta(minutes=5)).strftime("%Y-%m-%d")
        end   = (ts + pd.Timedelta(minutes=5)).strftime("%Y-%m-%d")
        df = yf.download(ticker, start=start, end=end, interval="1m", progress=False, auto_adjust=True)
        if df.empty: return None
        if hasattr(df.columns,"levels"): df.columns = df.columns.get_level_values(0)
        # Midpoint = (high+low)/2 on the bar closest to fill time
        df.index = pd.to_datetime(df.index, utc=True)
        idx = (df.index - ts.tz_localize("UTC")).abs().argmin()
        row = df.iloc[idx]
        return float((row["High"] + row["Low"]) / 2)
    except Exception:
        return None

def compute_slippage(fills):
    """Compute slippage in bps for each fill vs midpoint."""
    results = []
    for f in fills:
        fill_px = f.get("fill_price", 0)
        mid_px  = f.get("decision_price") or get_midpoint_at_fill(f["ticker"], f.get("filled_at",""))
        if not fill_px or not mid_px or mid_px <= 0:
            results.append({**f, "midpoint": None, "slippage_bps": None})
            continue
        side = f.get("side","buy").lower()
        # For buys: positive slippage = paid above midpoint (bad)
        # For sells: positive slippage = received below midpoint (bad)
        if side in ("buy","buy_new","add"):
            slip_bps = (fill_px - mid_px) / mid_px * 10000
        else:
            slip_bps = (mid_px - fill_px) / mid_px * 10000
        results.append({**f, "midpoint": round(mid_px, 4), "slippage_bps": round(slip_bps, 2)})
        if abs(slip_bps) > 5:
            logger.info(f"  {f['ticker']} {side}: fill={fill_px:.4f} mid={mid_px:.4f} slippage={slip_bps:.1f}bps")
    return results

def build_slippage_model(results):
    """Build size-bucket slippage model for pretrade_compliance."""
    import numpy as np
    buckets = {"<$5K": [], "$5K-$20K": [], "$20K-$50K": [], ">$50K": []}
    for r in results:
        if r.get("slippage_bps") is None: continue
        n = r.get("notional", 0)
        s = r["slippage_bps"]
        if n < 5000:     buckets["<$5K"].append(s)
        elif n < 20000:  buckets["$5K-$20K"].append(s)
        elif n < 50000:  buckets["$20K-$50K"].append(s)
        else:            buckets[">$50K"].append(s)
    model = {}
    for bucket, vals in buckets.items():
        if vals:
            model[bucket] = {"avg_bps": round(float(np.mean(vals)),2),
                             "p95_bps": round(float(np.percentile(vals,95)),2),
                             "n":       len(vals)}
    return model

def run_tca(days_back=90):
    logger.info("Running TCA analysis...")
    fills   = get_fills_from_alpaca(days_back)
    logger.info(f"  {len(fills)} fills fetched")

    if not fills:
        logger.info("  No fills yet (paper trading) — seeding with placeholder report")
        report = {"computed_at": datetime.now(timezone.utc).isoformat(),
                  "fills_analyzed": 0, "note": "No live fills yet — report will populate once live trading begins",
                  "avg_slippage_bps": None, "flagged_count": 0, "slippage_model": {}}
        _save(TCA_REPORT, report)
        return report

    results = compute_slippage(fills)
    valid   = [r for r in results if r.get("slippage_bps") is not None]
    flagged = [r for r in valid if r["slippage_bps"] > 10]

    import numpy as np
    avg_slip = float(np.mean([r["slippage_bps"] for r in valid])) if valid else None
    model    = build_slippage_model(results)
    _save(TCA_MODEL, {"updated_at": datetime.now(timezone.utc).isoformat(), "buckets": model})

    report = {
        "computed_at":      datetime.now(timezone.utc).isoformat(),
        "period_days":      days_back,
        "fills_analyzed":   len(results),
        "fills_with_mid":   len(valid),
        "avg_slippage_bps": round(avg_slip, 2) if avg_slip is not None else None,
        "flagged_high_slip": len(flagged),
        "flagged": [{"ticker": r["ticker"], "slippage_bps": r["slippage_bps"],
                     "notional": r.get("notional")} for r in flagged[:10]],
        "slippage_model":   model,
        "all_results":      results[-50:],
    }
    _save(TCA_REPORT, report)

    # Alert IRM if avg > 5bps
    if avg_slip and avg_slip > 5:
        try:
            from agent_meeting import request_info
            request_info("TCAAgent", "IndependentRiskManager",
                "TCA: High Average Slippage",
                f"Avg slippage {avg_slip:.1f}bps across {len(valid)} fills. {len(flagged)} trades > 10bps.",
                urgency="HIGH")
        except Exception as e:
            logger.warning(f"IRM slippage alert failed to send: {e}")

    logger.info(f"  Done: avg={avg_slip:.1f}bps, {len(flagged)} flagged >10bps" if avg_slip else "  Done")
    return report

if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("TCAAgent"); sys.exit(0)
    r = run_tca()
    print(f"\nTCA: {r['fills_analyzed']} fills, avg slippage={r['avg_slippage_bps']}bps, flagged={r['flagged_count'] if 'flagged_count' in r else len(r.get('flagged',[]))}")

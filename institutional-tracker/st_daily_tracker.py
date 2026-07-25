#!/usr/bin/env python3
"""
st_daily_tracker.py — Runs daily at 16:30 ET (20:30 UTC)
Updates prices in st_live_test_20d.json, checks if 20 days elapsed.
"""
import sys, json, os
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/st_live_test_20d.json")

def main():
    if not os.path.exists(TEST_PATH):
        print("No live test found")
        return

    with open(TEST_PATH) as f:
        test = json.load(f)

    if test.get("report"):
        print("Test already complete — report generated")
        return

    entries = test.get("entries", [])
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Get current prices
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
    except Exception as e:
        print(f"Alpaca unavailable: {e}")
        return

    prices = {}
    for entry in entries:
        t = entry["ticker"]
        if t not in prices:
            try:
                p = trader.get_latest_price(t)
                if p: prices[t] = p
            except Exception:
                pass

    # Take daily snapshot
    snapshot = {
        "date": today,
        "prices": {t: round(p, 2) for t, p in prices.items()},
    }
    test.setdefault("daily_snapshots", []).append(snapshot)

    # Check if we've reached 20 trading days
    entry_dt = datetime.fromisoformat(test["started_at"])
    elapsed_days = (now - entry_dt).days
    
    # Count trading days (rough: elapsed calendar days * 5/7)
    trading_days_elapsed = int(elapsed_days * 5 / 7)
    print(f"Day {trading_days_elapsed} of 20 | Prices updated: {len(prices)}/{len(entries)}")

    if trading_days_elapsed >= 20 or today >= test["test_parameters"]["expiry_date"]:
        print("20 trading days elapsed — generating final report...")
        _generate_report(test, prices)
    
    with open(TEST_PATH, "w") as f:
        json.dump(test, f, indent=2)
    print(f"Updated {TEST_PATH}")

def _generate_report(test, current_prices):
    """Generate the 20-day analysis report."""
    import numpy as np
    entries = test.get("entries", [])
    
    results = []
    for entry in entries:
        t = entry["ticker"]
        entry_p = entry.get("entry_price")
        current_p = current_prices.get(t)
        
        if entry_p and current_p and entry_p > 0:
            ret = (current_p - entry_p) / entry_p
            entry["day_20_price"] = round(current_p, 2)
            entry["day_20_return"] = round(ret, 4)
            entry["status"] = "CLOSED"
            results.append({
                "ticker": t,
                "st_score": entry["st_score_at_entry"],
                "return": ret,
                "prediction": entry["model_prediction"],
                "sector": entry["sector"],
            })
    
    if not results:
        return
    
    # Analysis
    rets = [r["return"] for r in results]
    median_ret = float(np.median(rets))
    
    # Did model predictions work?
    beat_preds = [r for r in results if r["prediction"] == "BEAT"]
    under_preds = [r for r in results if r["prediction"] == "UNDERPERFORM"]
    
    beat_correct = len([r for r in beat_preds if r["return"] > median_ret])
    under_correct = len([r for r in under_preds if r["return"] < median_ret])
    
    beat_acc = beat_correct / len(beat_preds) if beat_preds else 0
    under_acc = under_correct / len(under_preds) if under_preds else 0
    
    # Correlation: st_score vs return
    if len(results) > 5:
        scores = [r["st_score"] for r in results]
        returns = [r["return"] for r in results]
        corr = float(np.corrcoef(scores, returns)[0, 1])
    else:
        corr = 0

    # Top/bottom performers
    results.sort(key=lambda x: x["return"], reverse=True)
    
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test_duration_days": 20,
        "n_tickers_tracked": len(results),
        "summary": {
            "median_return": round(median_ret, 4),
            "avg_return": round(float(np.mean(rets)), 4),
            "best_return": round(max(rets), 4),
            "worst_return": round(min(rets), 4),
        },
        "model_accuracy": {
            "beat_prediction_accuracy": round(beat_acc, 3),
            "underperform_prediction_accuracy": round(under_acc, 3),
            "score_return_correlation": round(corr, 4),
            "interpretation": (
                "STRONG ALPHA" if corr > 0.2 else
                "WEAK ALPHA" if corr > 0.05 else
                "NO ALPHA" if corr > -0.05 else
                "INVERSE — model needs retraining"
            ),
        },
        "top_5_winners": [{"ticker": r["ticker"], "return": round(r["return"],4), "st_score": r["st_score"]} for r in results[:5]],
        "bottom_5_losers": [{"ticker": r["ticker"], "return": round(r["return"],4), "st_score": r["st_score"]} for r in results[-5:]],
        "model_improvements": _suggest_improvements(results, corr, beat_acc),
    }
    
    test["report"] = report
    print(f"Report generated: correlation={corr:.3f}, BEAT accuracy={beat_acc:.1%}")

def _suggest_improvements(results, corr, beat_acc):
    suggestions = []
    if corr < 0.05:
        suggestions.append("Low correlation — retrain model on rank_percentile_20d target with full 70K row pipeline")
    if beat_acc < 0.55:
        suggestions.append("BEAT accuracy below 55% — current heuristic insufficient, deploy proper trained model")
    
    # Sector analysis
    sector_rets = {}
    for r in results:
        s = r.get("sector","?")
        sector_rets.setdefault(s, []).append(r["return"])
    
    import numpy as np
    best_sector = max(sector_rets, key=lambda s: np.mean(sector_rets[s]))
    worst_sector = min(sector_rets, key=lambda s: np.mean(sector_rets[s]))
    suggestions.append(f"Best performing sector: {best_sector} — increase weighting")
    suggestions.append(f"Worst performing sector: {worst_sector} — reduce weighting or add sector-specific model")
    
    return suggestions

if __name__ == "__main__":
    main()

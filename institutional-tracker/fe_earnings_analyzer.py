#!/usr/bin/env python3
"""
fe_earnings_analyzer.py -- First Eagle Earnings Track Record Analyzer
Analyzes earnings beat rate, EPS surprise, revenue trend, and earnings quality
using yfinance data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def analyze_earnings_track_record(ticker: str) -> dict:
    """
    Analyze the earnings track record for a given ticker using yfinance.

    Returns:
        dict with keys: ticker, quarters_analyzed, eps_beat_rate,
        avg_eps_surprise_pct, revenue_trend, earnings_quality, note
    """
    ticker = ticker.upper().strip()
    now    = datetime.now(timezone.utc).isoformat()

    result = {
        "ticker":               ticker,
        "analyzed_at":          now,
        "quarters_analyzed":    0,
        "eps_beat_rate":        None,
        "avg_eps_surprise_pct": None,
        "revenue_trend":        "UNKNOWN",
        "earnings_quality":     "UNKNOWN",
        "note":                 "",
    }

    try:
        import yfinance as yf
    except ImportError:
        result["note"] = "yfinance not installed"
        return result

    try:
        stock = yf.Ticker(ticker)

        # --- EPS beat rate ---
        beats        = 0
        total        = 0
        surprise_sum = 0.0
        hist         = None

        try:
            hist = stock.earnings_history
        except Exception:
            hist = None

        if hist is not None and not hist.empty:
            for _, row in hist.iterrows():
                actual   = row.get("epsActual")
                estimate = row.get("epsEstimate")
                if actual is None or estimate is None:
                    continue
                try:
                    actual   = float(actual)
                    estimate = float(estimate)
                except (TypeError, ValueError):
                    continue
                total += 1
                if actual > estimate:
                    beats += 1
                if estimate != 0:
                    surprise_sum += (actual - estimate) / abs(estimate) * 100.0

            if total > 0:
                result["quarters_analyzed"]    = total
                result["eps_beat_rate"]        = round(beats / total, 4)
                result["avg_eps_surprise_pct"] = round(surprise_sum / total, 2)

        # --- Revenue trend from quarterly_earnings ---
        try:
            qe = stock.quarterly_earnings
        except Exception:
            qe = None

        if qe is not None and not qe.empty and "Revenue" in qe.columns:
            revenues = qe["Revenue"].dropna().tolist()
            if len(revenues) >= 2:
                # Most recent quarters first from yfinance
                recent  = revenues[:2]
                older   = revenues[2:] if len(revenues) > 2 else revenues[1:]
                avg_recent = sum(recent) / len(recent)
                avg_older  = sum(older)  / len(older) if older else avg_recent

                if avg_older > 0:
                    change_pct = (avg_recent - avg_older) / abs(avg_older) * 100
                    if change_pct > 5:
                        result["revenue_trend"] = "GROWING"
                    elif change_pct < -5:
                        result["revenue_trend"] = "DECLINING"
                    else:
                        result["revenue_trend"] = "STABLE"
                else:
                    result["revenue_trend"] = "STABLE"
            elif len(revenues) == 1:
                result["revenue_trend"] = "STABLE"

        # --- Earnings quality ---
        beat_rate = result["eps_beat_rate"]
        if beat_rate is not None:
            if beat_rate > 0.70:
                result["earnings_quality"] = "HIGH"
            elif beat_rate >= 0.50:
                result["earnings_quality"] = "MEDIUM"
            else:
                result["earnings_quality"] = "LOW"
        else:
            result["earnings_quality"] = "UNKNOWN"
            result["note"] = "No earnings history data available from yfinance"

    except Exception as e:
        result["note"] = f"Error analyzing earnings: {str(e)}"

    return result


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(json.dumps(analyze_earnings_track_record(t), indent=2))

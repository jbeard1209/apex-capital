#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_scoring_suite.py -- Run all FE scoring modules on a ticker, return combined summary
"""

import logging

logger = logging.getLogger(__name__)

POSITIVE_SIGNALS = {
    "piotroski":    lambda r: r.get("category") == "STRONG",
    "altman":       lambda r: r.get("zone") == "SAFE",
    "reverse_dcf":  lambda r: "PESSIMISM" in str(r.get("assessment", "")),
    "value_trap":   lambda r: r.get("verdict") == "LOOKS OKAY",
    "smart_money":  lambda r: r.get("smart_money_signal") == "BULLISH",
    "peer_compare": lambda r: "CHEAP" in str(r.get("verdict", "")),
    "cycle_earn":   lambda r: r.get("cycle_position") in ("TROUGH", "MID") or not r.get("cyclical", True),
    "altman_not_distress": lambda r: r.get("zone") != "DISTRESS",
}

NEGATIVE_SIGNALS = {
    "piotroski":    lambda r: r.get("category") == "WEAK",
    "altman":       lambda r: r.get("zone") == "DISTRESS",
    "reverse_dcf":  lambda r: "OPTIMISM" in str(r.get("assessment", "")),
    "value_trap":   lambda r: r.get("verdict") == "LIKELY TRAP",
    "smart_money":  lambda r: r.get("smart_money_signal") == "BEARISH",
    "peer_compare": lambda r: "EXPENSIVE" in str(r.get("verdict", "")),
    "cycle_earn":   lambda r: r.get("cyclical") and r.get("cycle_position") == "PEAK",
}


def full_score(ticker) -> dict:
    """Run all FE scoring modules on a ticker. Returns combined dict."""
    ticker = ticker.upper().strip()
    results = {"ticker": ticker}

    # 1. Piotroski F-Score
    try:
        from fe_piotroski import piotroski_score
        results["piotroski"] = piotroski_score(ticker)
    except Exception as e:
        results["piotroski"] = {"error": str(e)}

    # 2. Altman Z-Score
    try:
        from fe_altman import altman_zscore
        results["altman"] = altman_zscore(ticker)
    except Exception as e:
        results["altman"] = {"error": str(e)}

    # 3. Reverse DCF
    try:
        from fe_reverse_dcf import reverse_dcf
        results["reverse_dcf"] = reverse_dcf(ticker)
    except Exception as e:
        results["reverse_dcf"] = {"error": str(e)}

    # 4. Value Trap Detector
    try:
        from fe_value_trap import detect_value_trap
        results["value_trap"] = detect_value_trap(ticker)
    except Exception as e:
        results["value_trap"] = {"error": str(e)}

    # 5. Smart Money
    try:
        from fe_smart_money import check_smart_money
        results["smart_money"] = check_smart_money(ticker)
    except Exception as e:
        results["smart_money"] = {"error": str(e)}

    # 6. Peer Comparison
    try:
        from fe_peer_comparison import peer_comparison
        results["peer_comparison"] = peer_comparison(ticker)
    except Exception as e:
        results["peer_comparison"] = {"error": str(e)}

    # 7. Cycle-Adjusted Earnings
    try:
        from fe_cycle_earnings import cycle_adjusted_earnings
        results["cycle_earnings"] = cycle_adjusted_earnings(ticker)
    except Exception as e:
        results["cycle_earnings"] = {"error": str(e)}

    # 8. Watchlist (just return current watchlist status)
    try:
        from fe_watchlist import get_watchlist
        wl = get_watchlist()
        on_watchlist = next((item for item in wl if item["ticker"] == ticker), None)
        results["watchlist"] = on_watchlist if on_watchlist else {"status": "not_on_watchlist"}
    except Exception as e:
        results["watchlist"] = {"error": str(e)}

    # --- OVERALL SIGNAL ---
    positives = 0
    negatives = 0

    # Piotroski
    p = results.get("piotroski", {})
    if not p.get("error"):
        cat = p.get("category")
        if cat == "STRONG": positives += 1
        elif cat == "WEAK": negatives += 1

    # Altman
    a = results.get("altman", {})
    if not a.get("error"):
        zone = a.get("zone")
        if zone == "SAFE": positives += 1
        elif zone == "DISTRESS": negatives += 1

    # Reverse DCF
    rd = results.get("reverse_dcf", {})
    if not rd.get("error"):
        assessment = rd.get("assessment", "")
        if "PESSIMISM" in assessment: positives += 1
        elif "OPTIMISM" in assessment: negatives += 1

    # Value Trap
    vt = results.get("value_trap", {})
    if not vt.get("error"):
        verdict = vt.get("verdict", "")
        if verdict == "LOOKS OKAY": positives += 1
        elif verdict == "LIKELY TRAP": negatives += 1

    # Smart Money
    sm = results.get("smart_money", {})
    if not sm.get("error"):
        sig = sm.get("smart_money_signal", "")
        if sig == "BULLISH": positives += 1
        elif sig == "BEARISH": negatives += 1

    # Peer Comparison
    pc = results.get("peer_comparison", {})
    if not pc.get("error"):
        verdict = pc.get("verdict", "")
        if "CHEAP" in verdict: positives += 1
        elif "EXPENSIVE" in verdict: negatives += 1

    # Cycle Earnings
    ce = results.get("cycle_earnings", {})
    if not ce.get("error"):
        if not ce.get("cyclical", False):
            pass  # neutral -- not cyclical
        elif ce.get("cycle_position") == "TROUGH":
            positives += 1
        elif ce.get("cycle_position") == "PEAK":
            negatives += 1

    if positives >= 4:
        overall_signal = "STRONG BUY CANDIDATE"
    elif positives >= 2 and negatives == 0:
        overall_signal = "FAVORABLE"
    elif negatives >= 4:
        overall_signal = "AVOID"
    elif negatives >= 2 and positives == 0:
        overall_signal = "UNFAVORABLE"
    else:
        overall_signal = "MIXED -- FURTHER RESEARCH REQUIRED"

    results["overall_signal"] = overall_signal
    results["positive_signals"] = positives
    results["negative_signals"] = negatives

    return results


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = full_score(ticker)
    print(json.dumps({k: v for k, v in result.items() if k != "ticker"}, indent=2, default=str))

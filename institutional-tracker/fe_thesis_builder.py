#!/usr/bin/env python3
"""
fe_thesis_builder.py -- First Eagle Investment Thesis Builder
Synthesizes IV analysis, web research, and earnings data into a structured thesis.
Plain ASCII only (no em dashes or special chars).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


GENERIC_RISKS = ["Market risk", "Competition", "Macro headwinds"]

STANDARD_BREAK_CONDITIONS = [
    "Sustained negative FCF for 2+ consecutive years",
    "Net debt/EBITDA exceeds 4.0x",
    "ROIC falls below cost of capital for 2+ years",
    "Beneish M-Score above -1.78 (accounting manipulation signal)",
    "Management integrity failure",
]

MONITORING_TRIGGERS = [
    "Quarterly earnings vs estimate",
    "Free cash flow generation",
    "Debt levels",
    "Management changes",
    "Competitive position news",
]


def build_investment_thesis(
    ticker: str,
    iv_result: dict | None,
    web_research: dict | None,
    earnings: dict | None,
) -> dict:
    """
    Synthesize IV analysis, web research, and earnings data into a structured
    investment thesis for First Eagle-style fundamental value investing.

    Args:
        ticker:       Stock ticker symbol.
        iv_result:    Output from fe_intrinsic_value.calculate_iv(ticker).
        web_research: Output from fe_web_research.get_company_research(ticker).
        earnings:     Output from fe_earnings_analyzer.analyze_earnings_track_record(ticker).

    Returns:
        dict with thesis_statement, key_risks, thesis_break_conditions,
        monitoring_triggers, generated_at.
    """
    ticker = ticker.upper().strip()
    now    = datetime.now(timezone.utc).isoformat()

    # --- Safe field extraction ---
    iv_result    = iv_result    or {}
    web_research = web_research or {}
    earnings     = earnings     or {}

    price        = iv_result.get("current_price")
    cons_iv      = iv_result.get("conservative_iv")
    mos          = iv_result.get("margin_of_safety")
    beat_rate    = earnings.get("eps_beat_rate")
    sentiment    = web_research.get("headline_sentiment", "NEUTRAL")
    bullish      = web_research.get("bullish_signals", [])
    red_flags    = web_research.get("red_flags", [])

    # --- Thesis statement ---
    parts = []

    if price is not None and cons_iv is not None and mos is not None:
        mos_pct = round(mos * 100, 1)
        parts.append(
            f"{ticker} trades at ${price:.2f} vs conservative IV of ${cons_iv:.2f},"
            f" a {mos_pct}% margin of safety."
        )
    else:
        parts.append(
            f"{ticker} is under review for First Eagle margin-of-safety criteria."
        )

    if beat_rate is not None and beat_rate > 0.70:
        beat_pct = round(beat_rate * 100, 0)
        parts.append(
            f"Earnings track record is strong with a {beat_pct:.0f}% EPS beat rate,"
            " indicating reliable execution by management."
        )

    if bullish and sentiment == "POSITIVE":
        top_positive = bullish[0] if bullish else ""
        if top_positive:
            parts.append(
                f"Recent news is positive, with signals including '{top_positive}'."
            )
    elif sentiment == "NEGATIVE" and red_flags:
        parts.append(
            f"Recent news carries caution signals: {red_flags[0]}."
        )

    thesis_statement = " ".join(parts) if parts else (
        f"{ticker} is being evaluated under First Eagle absolute-return criteria."
    )

    # --- Key risks ---
    key_risks = red_flags[:5] if red_flags else list(GENERIC_RISKS)

    # --- Thesis break conditions ---
    ticker_specific_breaks = []
    if red_flags:
        for flag in red_flags[:2]:
            condition = f"Confirmed escalation of '{flag}' risk factor"
            if condition not in STANDARD_BREAK_CONDITIONS:
                ticker_specific_breaks.append(condition)

    thesis_break_conditions = list(STANDARD_BREAK_CONDITIONS) + ticker_specific_breaks

    return {
        "ticker":                  ticker,
        "thesis_statement":        thesis_statement,
        "key_risks":               key_risks,
        "thesis_break_conditions": thesis_break_conditions,
        "monitoring_triggers":     list(MONITORING_TRIGGERS),
        "generated_at":            now,
    }


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    dummy_iv       = {"current_price": 185.0, "conservative_iv": 250.0, "margin_of_safety": 0.26}
    dummy_research = {"headline_sentiment": "POSITIVE", "bullish_signals": ["beat"], "red_flags": []}
    dummy_earnings = {"eps_beat_rate": 0.80, "avg_eps_surprise_pct": 4.2}
    print(json.dumps(build_investment_thesis(t, dummy_iv, dummy_research, dummy_earnings), indent=2))

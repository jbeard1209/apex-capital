#!/usr/bin/env python3
"""
APEX CAPITAL — FI DATA LAYER
==============================
Provides bond opportunity scoring for the Fixed Income pipeline.

get_bond_opportunity_score(ticker, spreads) → dict with:
  opportunity_score: 0-100
  verdict: STRONG_BUY / BUY / WATCH / HOLD / AVOID
  credit_quality: {credit_tier, credit_score, yield_estimate, rationale}

For bond ETFs and individual credit instruments, we score based on:
  1. Current yield vs. 10yr treasury (spread premium)
  2. Duration risk given current yield curve regime
  3. Credit quality (IG vs HY, known sector risks)
  4. Price vs. 52-week range (relative value)
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("fi_data_layer")

# ── Credit tier classification ────────────────────────────────────────────────
# Known ETFs and their credit characteristics
CREDIT_PROFILE = {
    # Investment Grade
    "LQD":  {"tier": "IG",         "duration": "long",   "base_score": 70},
    "VCIT": {"tier": "IG",         "duration": "medium", "base_score": 72},
    "VCSH": {"tier": "IG",         "duration": "short",  "base_score": 74},
    "AGG":  {"tier": "IG_BLEND",   "duration": "medium", "base_score": 68},
    "BND":  {"tier": "IG_BLEND",   "duration": "medium", "base_score": 68},
    "IGSB": {"tier": "IG",         "duration": "short",  "base_score": 73},
    "SLQD": {"tier": "IG",         "duration": "short",  "base_score": 73},

    # High Yield
    "HYG":  {"tier": "HY",         "duration": "medium", "base_score": 62},
    "JNK":  {"tier": "HY",         "duration": "medium", "base_score": 60},
    "USHY": {"tier": "HY",         "duration": "medium", "base_score": 63},
    "HYLB": {"tier": "HY",         "duration": "medium", "base_score": 61},
    "SHYG": {"tier": "HY",         "duration": "short",  "base_score": 65},
    "SJNK": {"tier": "HY",         "duration": "short",  "base_score": 64},

    # Treasuries
    "TLT":  {"tier": "TREASURY",   "duration": "long",   "base_score": 65},
    "IEF":  {"tier": "TREASURY",   "duration": "medium", "base_score": 70},
    "SHY":  {"tier": "TREASURY",   "duration": "short",  "base_score": 75},
    "BIL":  {"tier": "T_BILL",     "duration": "ultra",  "base_score": 72},
    "VGSH": {"tier": "TREASURY",   "duration": "short",  "base_score": 75},

    # Emerging Markets
    "EMB":  {"tier": "EM_SOVR",    "duration": "long",   "base_score": 58},
    "VWOB": {"tier": "EM_SOVR",    "duration": "long",   "base_score": 57},
    "FEMB": {"tier": "EM_LOCAL",   "duration": "medium", "base_score": 55},

    # Convertibles / Preferred
    "CWB":  {"tier": "CONVERT",    "duration": "medium", "base_score": 60},
    "PFF":  {"tier": "PREFERRED",  "duration": "perpetual", "base_score": 62},
    "PFFD": {"tier": "PREFERRED",  "duration": "perpetual", "base_score": 62},

    # Duration-specific
    "ZROZ": {"tier": "TREASURY",   "duration": "ultra_long", "base_score": 55},
    "EDV":  {"tier": "TREASURY",   "duration": "ultra_long", "base_score": 55},
}

DURATION_REGIME_ADJUSTMENT = {
    # (duration, regime) → score adjustment
    ("short",       "TIGHT"):      +5,
    ("short",       "NORMAL"):     +3,
    ("short",       "WIDE"):       +2,
    ("short",       "DISTRESSED"): +2,
    ("medium",      "TIGHT"):      +2,
    ("medium",      "NORMAL"):     +3,
    ("medium",      "WIDE"):       +5,
    ("medium",      "DISTRESSED"): +4,
    ("long",        "TIGHT"):     -5,
    ("long",        "NORMAL"):     0,
    ("long",        "WIDE"):      +8,
    ("long",        "DISTRESSED"):+10,
    ("ultra_long",  "TIGHT"):    -10,
    ("ultra_long",  "NORMAL"):    -3,
    ("ultra_long",  "WIDE"):     +12,
    ("ultra_long",  "DISTRESSED"):+15,
}

TIER_SPREAD_ADJUSTMENT = {
    # High yield thrives when spreads are wide (WIDE/DISTRESSED)
    "HY": {"WIDE": +15, "DISTRESSED": +20, "NORMAL": 0, "TIGHT": -10},
    "IG": {"WIDE":  +8, "DISTRESSED": +5,  "NORMAL": 0, "TIGHT":  -5},
    "TREASURY": {"WIDE": +5, "DISTRESSED": +10, "NORMAL": 0, "TIGHT": -3},
    "IG_BLEND": {"WIDE": +5, "DISTRESSED": +5,  "NORMAL": 0, "TIGHT": -5},
    "EM_SOVR":  {"WIDE": +8, "DISTRESSED": +5,  "NORMAL": -3, "TIGHT": -8},
    "EM_LOCAL": {"WIDE": +5, "DISTRESSED": +3,  "NORMAL": -5, "TIGHT": -10},
    "PREFERRED": {"WIDE": +5, "DISTRESSED": +3, "NORMAL": 0,  "TIGHT": -5},
    "CONVERT":  {"WIDE": 0,  "DISTRESSED": -5,  "NORMAL": +3, "TIGHT": +5},
    "T_BILL":   {"WIDE": +5, "DISTRESSED": +8,  "NORMAL": +2, "TIGHT": +5},
}


def _get_live_yield(ticker: str) -> float | None:
    """Try to fetch the ETF's SEC yield or trailing yield from yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}
        # Try multiple yield fields yfinance exposes
        for field in ("yield", "dividendYield", "trailingAnnualDividendYield"):
            val = info.get(field)
            if val and val > 0:
                return float(val) * 100  # convert to %
    except Exception:
        pass
    return None


def get_bond_opportunity_score(ticker: str, spreads: dict) -> dict:
    """
    Score a bond instrument/ETF given current credit market conditions.

    Returns:
        {
          "ticker": str,
          "opportunity_score": int (0-100),
          "verdict": str,
          "credit_quality": {
              "credit_tier": str,
              "credit_score": int,
              "yield_estimate": float | None,
              "rationale": str,
          }
        }
    """
    regime   = spreads.get("spread_regime", "NORMAL")
    hy_bps   = spreads.get("hy_spread_bps", 350)
    ig_bps   = spreads.get("ig_spread_bps", 120)
    rate_10y = spreads.get("treasury_10yr", 4.5)

    profile  = CREDIT_PROFILE.get(ticker.upper(), {})
    tier     = profile.get("tier", "UNKNOWN")
    duration = profile.get("duration", "medium")
    base     = profile.get("base_score", 55)

    # For unknown tickers, try to infer from name
    if tier == "UNKNOWN":
        tu = ticker.upper()
        if any(x in tu for x in ["HY", "JNK", "HIGH"]):
            tier, duration, base = "HY", "medium", 60
        elif any(x in tu for x in ["IG", "CORP", "LQD"]):
            tier, duration, base = "IG", "medium", 68
        elif any(x in tu for x in ["TLT", "IEF", "GOVT", "TSY"]):
            tier, duration, base = "TREASURY", "medium", 68
        else:
            tier, duration, base = "IG", "medium", 60

    # Apply duration × regime adjustment
    dur_adj = DURATION_REGIME_ADJUSTMENT.get((duration, regime), 0)

    # Apply tier × spread regime adjustment
    tier_adjs = TIER_SPREAD_ADJUSTMENT.get(tier, {})
    tier_adj  = tier_adjs.get(regime, 0)

    # High rate environment bonus for short-duration
    rate_adj = 0
    if rate_10y > 5.0 and duration in ("short", "ultra"):
        rate_adj = +5
    elif rate_10y > 4.5 and duration == "medium":
        rate_adj = +3

    # Inversion penalty for long duration
    yield_curve_slope = spreads.get("yield_curve_slope", 0)
    inversion_adj = -8 if yield_curve_slope < -0.3 and duration in ("long", "ultra_long") else 0

    final_score = min(100, max(0, base + dur_adj + tier_adj + rate_adj + inversion_adj))

    # Verdict thresholds
    if final_score >= 80:
        verdict = "STRONG_BUY"
    elif final_score >= 68:
        verdict = "BUY"
    elif final_score >= 55:
        verdict = "WATCH"
    elif final_score >= 40:
        verdict = "HOLD"
    else:
        verdict = "AVOID"

    # Try to get live yield
    yield_est = _get_live_yield(ticker)

    rationale_parts = [
        f"regime={regime}",
        f"tier={tier}",
        f"duration={duration}",
        f"base={base}",
    ]
    if dur_adj: rationale_parts.append(f"dur_adj={dur_adj:+d}")
    if tier_adj: rationale_parts.append(f"tier_adj={tier_adj:+d}")
    if rate_adj: rationale_parts.append(f"rate_adj={rate_adj:+d}")
    if inversion_adj: rationale_parts.append(f"inversion_adj={inversion_adj:+d}")

    return {
        "ticker":            ticker,
        "opportunity_score": final_score,
        "verdict":           verdict,
        "credit_quality": {
            "credit_tier":   tier,
            "credit_score":  final_score,
            "yield_estimate": yield_est,
            "rationale":     " | ".join(rationale_parts),
            "hy_spread_bps": hy_bps,
            "ig_spread_bps": ig_bps,
            "rate_10yr":     rate_10y,
        },
        "scored_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    from fi_credit_monitor import run_credit_monitor
    market  = run_credit_monitor()
    spreads = market["spreads"]
    print(f"Regime: {spreads['spread_regime']}")
    for ticker in ["HYG", "LQD", "JNK", "IEF", "SHY", "TLT", "EMB"]:
        r = get_bond_opportunity_score(ticker, spreads)
        print(f"  {ticker:6s}: score={r['opportunity_score']:3d}  {r['verdict']:12s}  tier={r['credit_quality']['credit_tier']}")

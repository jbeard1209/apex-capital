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

# yfinance logs every failed symbol lookup at ERROR level via its OWN logger.
# The FI universe includes individual preferred shares (e.g. MET-A, PRU-B) that
# Yahoo can't resolve, so each run produced 800-2500 "HTTP Error 404" ERROR lines
# — flooding the logs, tripping the log-error alarm, and spawning board proposals
# that were approved 4x but never fixable (the noise WAS the bug). Silence
# yfinance's logger; we already handle missing data gracefully (yield is optional).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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


#: The key fi_board.py / fe_board.py actually write into *_approved_picks.json.
#: Every consumer read `"picks"` instead, so `.get("picks", [])` returned [] on a
#: file that was never empty. fi_executor.py logged "No approved picks to
#: execute" and returned — for every board session it has ever seen. The FE
#: correlation check in fi_risk_monitor never fired for the same reason. One
#: constant, read through one loader, so a future rename breaks loudly.
APPROVED_PICKS_KEY = "approved"


def load_approved_picks(path) -> list:
    """Read a *_approved_picks.json file and return the approved picks.

    Raises KeyError if the file exists with content but carries neither the
    current key nor a recognised legacy one — a silent [] here is exactly how
    the FI executor stayed quiet for months (memory `wiring_contract_bugs`).
    """
    import json
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise KeyError(f"{path.name}: expected an object, got {type(data).__name__}")
    if APPROVED_PICKS_KEY in data:
        return data[APPROVED_PICKS_KEY] or []
    if "picks" in data:                      # legacy shape, still honoured
        return data["picks"] or []
    raise KeyError(
        f"{path.name} has no '{APPROVED_PICKS_KEY}' key (found: {sorted(data)}). "
        f"The board's writer and this reader have diverged — do NOT paper over "
        f"this with an empty list."
    )


def load_approved_envelope(path) -> dict:
    """The approved-picks file minus its pick list — `generated_at`, `regime`, …

    Kept alongside load_approved_picks so a caller that needs the metadata does
    not go back to a raw open()/json.load and re-invent the key handling this
    module exists to own.
    """
    import json
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k not in (APPROVED_PICKS_KEY, "picks")}


def get_credit_spreads() -> dict:
    """Current credit spreads — the `spreads` sub-dict of the credit monitor.

    fi_risk_monitor.py and fi_position_monitor.py have imported this name since
    they were written; it never existed here, so both crashed on the import line
    (`ImportError: cannot import name 'get_credit_spreads'`) every single time
    they were invoked. Neither is on any scheduler, so nothing ever saw the
    traceback — including the *risk* monitor for a $30k pod. See memory
    `orphan_agents_blindness`: an agent nothing runs cannot report that it is
    broken.

    Values may be None when a feed is down; `spread_regime` is then "UNKNOWN".
    Callers must abstain rather than substitute a default — that substitution is
    the bug this whole module was just audited for.
    """
    from fi_credit_monitor import run_credit_monitor
    return run_credit_monitor().get("spreads", {})


def get_issuer_credit_quality(ticker: str) -> dict:
    """Credit quality for a single instrument, independent of market spreads.

    Second name both monitors import and this module never defined. Returns the
    static profile, which is what the callers actually use (`credit_score`,
    `credit_tier`, `leverage_trend`).

    `leverage_trend` is honestly "UNKNOWN": the firm has no issuer-level
    fundamentals feed for bond ETFs, and fi_position_monitor only acts on the
    value when it equals "WORSENING". Returning a plausible "STABLE" here would
    be the same lie as a 350bps default — it would read as a measurement.
    """
    profile = CREDIT_PROFILE.get(ticker.upper(), {})
    if not profile:
        return {"ticker": ticker.upper(), "credit_tier": "UNKNOWN",
                "credit_score": 50, "leverage_trend": "UNKNOWN",
                "known_instrument": False}
    return {
        "ticker":           ticker.upper(),
        "credit_tier":      profile.get("tier", "UNKNOWN"),
        "credit_score":     profile.get("base_score", 50),
        "duration_bucket":  profile.get("duration", "medium"),
        "leverage_trend":   "UNKNOWN",   # no issuer fundamentals feed — see above
        "known_instrument": True,
    }


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
    # No substituting for a spread we could not measure. fi_credit_monitor
    # now emits None (not 350/120) when a feed is down, and the regime is
    # UNKNOWN rather than NORMAL — scoring a bond against an invented spread
    # produces a confident number with nothing behind it. Refuse instead.
    regime   = spreads.get("spread_regime") or "UNKNOWN"
    hy_bps   = spreads.get("hy_spread_bps")
    ig_bps   = spreads.get("ig_spread_bps")
    rate_10y = spreads.get("treasury_10yr")
    _missing = [n for n, v in (("hy_spread_bps", hy_bps), ("ig_spread_bps", ig_bps),
                               ("treasury_10yr", rate_10y)) if v is None]
    if regime == "UNKNOWN" or _missing:
        return {
            "ticker":            ticker,
            "opportunity_score": 0,
            "verdict":           "ABSTAIN",
            "abstained":         True,
            "credit_quality": {
                "credit_tier":    "UNKNOWN",
                "credit_score":   0,
                "yield_estimate": None,
                "rationale":      (f"credit read unavailable — regime={regime}, "
                                   f"missing={_missing or ['(regime)']}. A score "
                                   f"computed against an invented spread is a "
                                   f"number with nothing behind it."),
                "hy_spread_bps":  hy_bps,
                "ig_spread_bps":  ig_bps,
                "rate_10yr":      rate_10y,
            },
            "scored_at": datetime.now().isoformat(),
        }

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

    # Inversion penalty for long duration. The slope is secondary enrichment —
    # unlike the spreads above, a missing one does not invalidate the score. It
    # does mean the penalty cannot be assessed, so say so in the rationale
    # rather than letting a silent 0 read as "curve checked, not inverted".
    yield_curve_slope = spreads.get("yield_curve_slope")
    slope_unknown     = yield_curve_slope is None
    inversion_adj = (0 if slope_unknown else
                     (-8 if yield_curve_slope < -0.3
                      and duration in ("long", "ultra_long") else 0))

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

    # Try to get live yield — but only for tickers in CREDIT_PROFILE (the ETFs
    # Yahoo can actually price). Individual preferreds/bonds aren't resolvable on
    # yfinance and the yield is optional enrichment, so we skip the doomed lookup
    # (avoids 404s and a slow per-ticker network round-trip).
    yield_est = _get_live_yield(ticker) if ticker.upper() in CREDIT_PROFILE else None

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
    if slope_unknown: rationale_parts.append("curve=unknown (IEF/SHY feed down — "
                                             "inversion penalty not assessed)")

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

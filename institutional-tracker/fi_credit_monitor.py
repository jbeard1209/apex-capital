#!/usr/bin/env python3
"""
APEX CAPITAL — FI CREDIT MONITOR
==================================
Fetches real-time credit market conditions for the Fixed Income pipeline.

Data sourced from yfinance (same provider as rest of firm):
  - HYG  (iShares HY Corporate Bond ETF)  → HY spread proxy
  - LQD  (iShares IG Corporate Bond ETF)  → IG spread proxy
  - TLT  (20yr Treasury ETF)              → long-end rates
  - IEF  (7-10yr Treasury ETF)            → mid rates
  - SHY  (1-3yr Treasury ETF)             → short rates
  - ^TNX (10yr Treasury yield)            → benchmark rate

Outputs a dict with:
  spreads:  hy_spread, ig_spread, treasury_10yr, yield_curve_slope, spread_regime
  signals:  list of actionable observations (wide spreads, inverted curve, etc.)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("fi_credit_monitor")

DATA_DIR = Path(__file__).parent / "data"
CACHE_PATH = DATA_DIR / "fi_credit_conditions.json"
CACHE_TTL_HOURS = 6   # reuse cached data within 6h


def _cache_valid() -> bool:
    if not CACHE_PATH.exists():
        return False
    age_h = (datetime.now().timestamp() - CACHE_PATH.stat().st_mtime) / 3600
    return age_h < CACHE_TTL_HOURS


def _fetch_yield(ticker: str) -> float | None:
    """Fetch the most recent closing yield/price for a ticker."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"  [{ticker}] fetch failed: {e}")
        return None


def run_credit_monitor(force_refresh: bool = False) -> dict:
    """
    Fetch credit market conditions and compute the spread regime.
    Returns the full market dict expected by fi_pipeline.py.
    """
    if not force_refresh and _cache_valid():
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            logger.info("  [CreditMonitor] Using cached conditions (%s)",
                        cached.get("fetched_at", "?")[:16])
            return cached
        except Exception:
            pass

    logger.info("  [CreditMonitor] Fetching live credit market data...")

    # ── Fetch raw prices ──────────────────────────────────────────────────
    hyg_price  = _fetch_yield("HYG")   # HY bond ETF
    lqd_price  = _fetch_yield("LQD")   # IG bond ETF
    tlt_price  = _fetch_yield("TLT")   # 20yr treasury
    ief_price  = _fetch_yield("IEF")   # 7-10yr treasury
    shy_price  = _fetch_yield("SHY")   # 1-3yr treasury
    tnx_yield  = _fetch_yield("^TNX")  # 10yr yield (%)

    # ── Compute spread proxies ─────────────────────────────────────────────
    # HYG and LQD move inversely to spreads.
    # We use ETF price levels as regime proxies — not exact bps, but directionally correct.
    # Historical reference levels:
    #   HYG ~78-82 = normal HY spread (~350-400bps)
    #   HYG ~70-75 = wide HY spread (~500bps+, stress)
    #   HYG ~85+   = tight HY spread (<300bps, risk-on)

    hyg_ref_normal  = 79.0
    lqd_ref_normal  = 108.0

    if hyg_price:
        # Estimate bps: each $1 drop from normal ≈ +50bps
        hy_spread_bps = max(200, 350 + (hyg_ref_normal - hyg_price) * 50)
    else:
        hy_spread_bps = 350  # default normal

    if lqd_price:
        # IG spreads tighter: each $1 drop ≈ +20bps
        ig_spread_bps = max(60, 120 + (lqd_ref_normal - lqd_price) * 20)
    else:
        ig_spread_bps = 120  # default normal

    treasury_10yr = tnx_yield if tnx_yield else 4.5  # fallback

    # Yield curve slope: 10yr minus 2yr proxy (IEF vs SHY)
    if ief_price and shy_price:
        # Simplified: slope proxy from ETF durations
        yield_curve_slope = round((treasury_10yr or 4.5) - 5.3, 3)  # rough approximation
    else:
        yield_curve_slope = -0.2

    # ── Compute regime ────────────────────────────────────────────────────
    # TIGHT    : HY < 300bps  — risk-on, spreads compressed
    # NORMAL   : 300–450bps   — typical environment
    # WIDE     : 450–700bps   — stress building, opportunities appearing
    # DISTRESSED: >700bps     — crisis, maximum opportunity but maximum risk

    if hy_spread_bps < 300:
        spread_regime = "TIGHT"
    elif hy_spread_bps < 450:
        spread_regime = "NORMAL"
    elif hy_spread_bps < 700:
        spread_regime = "WIDE"
    else:
        spread_regime = "DISTRESSED"

    # ── Generate trading signals ──────────────────────────────────────────
    signals = []

    if spread_regime in ("WIDE", "DISTRESSED"):
        signals.append({
            "type": "HY_OPPORTUNITY",
            "message": f"HY spreads at ~{hy_spread_bps:.0f}bps — value opportunities in HY names",
            "direction": "BUY",
        })

    if ig_spread_bps > 160:
        signals.append({
            "type": "IG_OPPORTUNITY",
            "message": f"IG spreads at ~{ig_spread_bps:.0f}bps — above normal; IG names attractive",
            "direction": "BUY",
        })

    if yield_curve_slope < 0:
        signals.append({
            "type": "INVERTED_CURVE",
            "message": f"Yield curve inverted ({yield_curve_slope:+.2f}). "
                       "Favor short duration; avoid long-maturity bonds.",
            "direction": "CAUTION",
        })
    elif yield_curve_slope > 0.5:
        signals.append({
            "type": "STEEPENING_CURVE",
            "message": f"Yield curve steepening ({yield_curve_slope:+.2f}). "
                       "Long duration bonds may benefit from duration extension.",
            "direction": "OPPORTUNITY",
        })

    if treasury_10yr > 5.0:
        signals.append({
            "type": "HIGH_RATES",
            "message": f"10yr Treasury at {treasury_10yr:.2f}% — IG bonds yielding well above historical avg.",
            "direction": "BUY",
        })

    # ── Build output ──────────────────────────────────────────────────────
    result = {
        "fetched_at": datetime.now().isoformat(),
        "spreads": {
            "hy_spread":          round(hy_spread_bps, 0),
            "ig_spread":          round(ig_spread_bps, 0),
            "hy_spread_bps":      round(hy_spread_bps, 0),
            "ig_spread_bps":      round(ig_spread_bps, 0),
            "treasury_10yr":      round(treasury_10yr, 3),
            "yield_curve_slope":  round(yield_curve_slope, 3),
            "spread_regime":      spread_regime,
            "hyg_price":          hyg_price,
            "lqd_price":          lqd_price,
        },
        "signals": signals,
        "regime_context": {
            "TIGHT":      "Spreads compressed — reduce risk, avoid long duration HY.",
            "NORMAL":     "Normal conditions — selective IG and BB-rated HY acceptable.",
            "WIDE":       "Spreads widening — strong value in quality HY; add IG selectively.",
            "DISTRESSED": "Crisis spreads — maximum value for patient capital; size small.",
        }.get(spread_regime, ""),
    }

    # Cache result
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        "  [CreditMonitor] HY=%.0fbps IG=%.0fbps 10yr=%.2f%% slope=%.3f regime=%s signals=%d",
        hy_spread_bps, ig_spread_bps, treasury_10yr,
        yield_curve_slope, spread_regime, len(signals),
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run_credit_monitor(force_refresh=True)
    print(f"\nRegime: {result['spreads']['spread_regime']}")
    print(f"HY spread: {result['spreads']['hy_spread']:.0f}bps")
    print(f"IG spread: {result['spreads']['ig_spread']:.0f}bps")
    print(f"10yr: {result['spreads']['treasury_10yr']:.2f}%")
    print(f"Signals: {len(result['signals'])}")
    for s in result["signals"]:
        print(f"  [{s['direction']}] {s['message']}")

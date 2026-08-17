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
    if age_h >= CACHE_TTL_HOURS:
        return False
    # A degraded read must never be served from cache. One transient yfinance
    # failure would otherwise pin the firm's credit regime for six hours —
    # and because a missing HY feed used to resolve to exactly 350bps, which
    # is exactly the middle of the NORMAL band, the poisoned value was the
    # one value guaranteed to look unremarkable. Re-fetch instead; the retry
    # is one HTTP call and the feed usually comes back on the next attempt.
    try:
        with open(CACHE_PATH) as f:
            if json.load(f).get("data_quality") != "LIVE":
                logger.info("  [CreditMonitor] cached read is DEGRADED — refetching")
                return False
    except Exception:
        return False
    return True


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

    # ── A spread we could not measure is None, not a plausible number ─────────
    # These used to fall back to 350 and 120. Those are not neutral defaults:
    # 350 sits dead-centre in the NORMAL band (300–450) and the regime below is
    # computed from hy_spread ALONE, so a failed HYG fetch did not degrade the
    # credit read — it asserted, with full confidence, that credit conditions
    # were normal. On 2026-08-14T18:00 exactly that happened: hyg_price was
    # null, hy_spread was logged as 350, regime NORMAL, and the whole FI pod
    # ran on it. HYG was fine ninety minutes later at 79.69 (= 315bps). The
    # tell was in the output the whole time — every real value was a float and
    # the fabricated one was an int.
    #
    # See memory `nan-is-not-none` and `presence-is-not-currency`: the failure
    # is not the missing datum, it is that the missing datum is unreadable as
    # missing. Downstream can handle "we don't know"; it cannot handle a lie.
    degraded: list[str] = []

    if hyg_price:
        # Estimate bps: each $1 drop from normal ≈ +50bps
        hy_spread_bps = max(200, 350 + (hyg_ref_normal - hyg_price) * 50)
    else:
        hy_spread_bps = None
        degraded.append("HYG (high-yield spread proxy)")

    if lqd_price:
        # IG spreads tighter: each $1 drop ≈ +20bps
        ig_spread_bps = max(60, 120 + (lqd_ref_normal - lqd_price) * 20)
    else:
        ig_spread_bps = None
        degraded.append("LQD (investment-grade spread proxy)")

    if tnx_yield:
        treasury_10yr = tnx_yield
    else:
        treasury_10yr = None
        degraded.append("^TNX (10yr Treasury benchmark)")

    # Yield curve slope: 10yr minus 2yr proxy (IEF vs SHY)
    if ief_price and shy_price and treasury_10yr is not None:
        # Simplified: slope proxy from ETF durations
        yield_curve_slope = round(treasury_10yr - 5.3, 3)  # rough approximation
    else:
        yield_curve_slope = None
        if "^TNX (10yr Treasury benchmark)" not in degraded:
            degraded.append("IEF/SHY (yield-curve slope proxy)")

    # ── Compute regime ────────────────────────────────────────────────────
    # TIGHT    : HY < 300bps  — risk-on, spreads compressed
    # NORMAL   : 300–450bps   — typical environment
    # WIDE     : 450–700bps   — stress building, opportunities appearing
    # DISTRESSED: >700bps     — crisis, maximum opportunity but maximum risk
    #
    # The regime is derived from hy_spread alone, so no HY reading means no
    # regime. UNKNOWN is a real answer here — cos_fi_base and fi_pipeline both
    # treat it as "abstain", which is what the firm should do when it cannot
    # see the credit market. It must never resolve to NORMAL by default.

    if hy_spread_bps is None:
        spread_regime = "UNKNOWN"
    elif hy_spread_bps < 300:
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

    if ig_spread_bps is not None and ig_spread_bps > 160:
        signals.append({
            "type": "IG_OPPORTUNITY",
            "message": f"IG spreads at ~{ig_spread_bps:.0f}bps — above normal; IG names attractive",
            "direction": "BUY",
        })

    if yield_curve_slope is None:
        pass
    elif yield_curve_slope < 0:
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

    if treasury_10yr is not None and treasury_10yr > 5.0:
        signals.append({
            "type": "HIGH_RATES",
            "message": f"10yr Treasury at {treasury_10yr:.2f}% — IG bonds yielding well above historical avg.",
            "direction": "BUY",
        })

    # ── Build output ──────────────────────────────────────────────────────
    _round_or_none = lambda v, n: None if v is None else round(v, n)
    result = {
        "fetched_at": datetime.now().isoformat(),
        # Read this before reading `spreads`. LIVE means every proxy came back
        # and every number below was computed from a real quote. DEGRADED means
        # at least one is None and `degraded` names which feed failed — no
        # consumer may substitute a value for it.
        "data_quality": "LIVE" if not degraded else "DEGRADED",
        "degraded":     degraded,
        "spreads": {
            "hy_spread":          _round_or_none(hy_spread_bps, 0),
            "ig_spread":          _round_or_none(ig_spread_bps, 0),
            "hy_spread_bps":      _round_or_none(hy_spread_bps, 0),
            "ig_spread_bps":      _round_or_none(ig_spread_bps, 0),
            "treasury_10yr":      _round_or_none(treasury_10yr, 3),
            "yield_curve_slope":  _round_or_none(yield_curve_slope, 3),
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
            "UNKNOWN":    ("Credit read unavailable — " + ", ".join(degraded) +
                           " did not return. No FI positioning opinion is valid "
                           "on this read; the pipeline abstains."),
        }.get(spread_regime, ""),
    }

    # ── Fable 5 credit read — gated to non-NORMAL regimes or 2+ signals ───
    # Also gated on LIVE: asking a model to interpret numbers we do not have
    # produces a fluent paragraph about a credit market nobody measured
    # (memory `plausible-is-not-validated`).
    try:
        from apex_llm import reason as _r, is_available
        if (not degraded and is_available("fi_credit")
                and (spread_regime != "NORMAL" or len(signals) >= 2)):
            sigs = "; ".join(s["message"][:80] for s in signals[:3]) or "none"
            raw = _r(
                "You are a fixed-income strategist for a $30k bond ETF pod.",
                f"Regime {spread_regime} | HY {hy_spread_bps:.0f}bps, IG {ig_spread_bps:.0f}bps, "
                f"10yr {treasury_10yr:.2f}%, curve slope {yield_curve_slope:+.2f}\n"
                f"Signals: {sigs}\n"
                "2 sentences: what is the credit market actually telling us, and the "
                "single best FI positioning move right now?",
                agent_name="fi_credit", max_tokens=120,
            )
            if raw:
                result["llm_credit_read"] = raw.strip()
    except Exception:
        pass

    # Cache result
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(result, f, indent=2)

    _f = lambda v, fmt: "n/a" if v is None else format(v, fmt)
    logger.info(
        "  [CreditMonitor] HY=%sbps IG=%sbps 10yr=%s%% slope=%s regime=%s signals=%d [%s]",
        _f(hy_spread_bps, ".0f"), _f(ig_spread_bps, ".0f"), _f(treasury_10yr, ".2f"),
        _f(yield_curve_slope, ".3f"), spread_regime, len(signals),
        result["data_quality"],
    )
    if degraded:
        logger.error(
            "  [CreditMonitor] DEGRADED — no reading from: %s. Regime is UNKNOWN, "
            "not NORMAL. Downstream must abstain, not substitute a default.",
            ", ".join(degraded),
        )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run_credit_monitor(force_refresh=True)
    s = result["spreads"]
    fmt = lambda v, f: "n/a (feed down)" if v is None else format(v, f)
    print(f"\nData quality: {result['data_quality']}", end="")
    print(f"  — missing: {', '.join(result['degraded'])}" if result["degraded"] else "")
    print(f"Regime: {s['spread_regime']}")
    print(f"HY spread: {fmt(s['hy_spread'], '.0f')}bps")
    print(f"IG spread: {fmt(s['ig_spread'], '.0f')}bps")
    print(f"10yr: {fmt(s['treasury_10yr'], '.2f')}%")
    print(f"Signals: {len(result['signals'])}")
    for s in result["signals"]:
        print(f"  [{s['direction']}] {s['message']}")

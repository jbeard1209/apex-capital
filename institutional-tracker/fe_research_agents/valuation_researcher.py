#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE VALUATION RESEARCHER
=================================================
Multi-method valuation with bull/base/bear scenarios.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

DISCOUNT_RATE   = 0.10
TERMINAL_GROWTH_BASE = 0.030
TERMINAL_GROWTH_BEAR = 0.020
TERMINAL_GROWTH_BULL = 0.035
TAX_RATE        = 0.25
EPV_WACC        = 0.10
DCF_YEARS       = 10


def _nonull(lst) -> list:
    return [v for v in (lst or []) if v is not None]


def _avg(lst) -> Optional[float]:
    n = _nonull(lst)
    return sum(n) / len(n) if n else None


def _cagr(start: float, end: float, years: int) -> Optional[float]:
    if start is None or end is None or years <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def _dcf(fcf: float, growth: float, terminal_growth: float, discount: float, years: int, shares: float) -> Optional[float]:
    """Standard DCF returning IV per share."""
    if fcf <= 0 or shares <= 0:
        return None
    pv = 0.0
    cf = fcf
    for yr in range(1, years + 1):
        # Decay growth in back half
        g = growth if yr <= 5 else max(terminal_growth, growth * 0.5)
        cf *= (1 + g)
        pv += cf / ((1 + discount) ** yr)
    terminal_cf  = cf * (1 + terminal_growth)
    terminal_val = terminal_cf / (discount - terminal_growth)
    pv_terminal  = terminal_val / ((1 + discount) ** years)
    return (pv + pv_terminal) / shares


def _reverse_dcf(price: float, fcf: float, shares: float, discount: float = DISCOUNT_RATE, terminal_growth: float = TERMINAL_GROWTH_BASE) -> Optional[float]:
    """Binary search for implied FCF growth rate at current price."""
    if not price or not fcf or not shares or fcf <= 0:
        return None
    target_iv = price * shares

    lo, hi = 0.00, 0.25
    for _ in range(40):  # bisection
        mid = (lo + hi) / 2
        pv = 0.0
        cf = fcf
        for yr in range(1, DCF_YEARS + 1):
            g = mid if yr <= 5 else max(terminal_growth, mid * 0.5)
            cf *= (1 + g)
            pv += cf / ((1 + discount) ** yr)
        tv = cf * (1 + terminal_growth) / (discount - terminal_growth)
        pv += tv / ((1 + discount) ** DCF_YEARS)
        if pv > target_iv:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def run_valuation_research(ticker: str) -> dict:
    """
    Multi-method valuation with bull/base/bear scenarios.
    """
    ticker = ticker.upper().strip()
    out_dir  = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "valuation_models.json"

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals, get_current_price
        f = get_edgar_fundamentals(ticker)
        current_price = get_current_price(ticker)
    except Exception as e:
        logger.error(f"[Valuation] EDGAR/price fetch failed for {ticker}: {e}")
        result = {"ticker": ticker, "error": str(e), "verdict": "DATA_ERROR",
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    if not f or not current_price:
        result = {"ticker": ticker, "error": "No data", "verdict": "DATA_ERROR",
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    revenue  = f.get("revenue", [])
    ocf      = f.get("operating_cf", [])
    capex_r  = f.get("capex", [])
    ebit     = f.get("ebit", [])
    da       = f.get("da", [])
    assets   = f.get("total_assets", [])
    liab     = f.get("total_liabilities", [])
    ni       = f.get("net_income", [])
    shares_l = f.get("shares_outstanding", [])
    ltd      = f.get("long_term_debt", [])

    # Get shares (most recent)
    shares_nn = [(i, v) for i, v in enumerate(shares_l) if v is not None]
    shares = shares_nn[-1][1] if shares_nn else None
    if not shares or shares <= 0:
        result = {"ticker": ticker, "error": "No shares data", "verdict": "DATA_ERROR",
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    # FCF series
    fcf_list = []
    for i in range(len(revenue)):
        o = ocf[i] if i < len(ocf) else None
        c = capex_r[i] if i < len(capex_r) else None
        if o is not None and c is not None:
            fcf_list.append(o - c)
        elif o is not None:
            fcf_list.append(o)
        else:
            fcf_list.append(None)

    fcf_nn = _nonull(fcf_list)
    avg_fcf_3 = _avg(fcf_nn[-3:]) if len(fcf_nn) >= 3 else _avg(fcf_nn)
    avg_fcf = avg_fcf_3

    # Revenue CAGR (5yr)
    rev_nn = _nonull(revenue)
    rev_cagr_5 = None
    if len(rev_nn) >= 6:
        rev_cagr_5 = _cagr(rev_nn[-6], rev_nn[-1], 5)

    # --- a) DCF -- 3 scenarios ---
    bear_growth = max(0.0, (rev_cagr_5 or 0.04) - 0.04)
    base_growth = min(0.08, rev_cagr_5 or 0.04)
    bull_growth = min(0.12, (rev_cagr_5 or 0.04) + 0.02)

    dcf_bear = None
    dcf_base = None
    dcf_bull = None

    if avg_fcf and avg_fcf > 0:
        dcf_bear = _dcf(avg_fcf, bear_growth, TERMINAL_GROWTH_BEAR, DISCOUNT_RATE, DCF_YEARS, shares)
        dcf_base = _dcf(avg_fcf, base_growth, TERMINAL_GROWTH_BASE, DISCOUNT_RATE, DCF_YEARS, shares)
        dcf_bull = _dcf(avg_fcf, bull_growth, TERMINAL_GROWTH_BULL, DISCOUNT_RATE, DCF_YEARS, shares)

    # --- b) EPV ---
    ebit_nn = _nonull(ebit[-3:])
    avg_ebit_3 = _avg(ebit_nn)
    epv = None
    if avg_ebit_3 and avg_ebit_3 > 0:
        nopat = avg_ebit_3 * (1 - TAX_RATE)
        epv   = (nopat / EPV_WACC) / shares

    # --- c) Graham Number ---
    ni_nn = _nonull(ni)
    latest_ni = ni_nn[-1] if ni_nn else None
    eps = latest_ni / shares if latest_ni and shares else None

    assets_nn = _nonull(assets)
    liab_nn   = _nonull(liab)
    latest_assets = assets_nn[-1] if assets_nn else None
    latest_liab   = liab_nn[-1] if liab_nn else None
    bvps = ((latest_assets - latest_liab) / shares) if (latest_assets and latest_liab and shares) else None

    graham_number = None
    if eps and bvps and eps > 0 and bvps > 0:
        graham_number = math.sqrt(22.5 * eps * bvps)

    # --- d) Asset-based value ---
    asset_value = bvps  # book value per share

    # --- e) Reverse DCF ---
    implied_growth = None
    if avg_fcf and avg_fcf > 0 and current_price:
        implied_growth = _reverse_dcf(current_price, avg_fcf, shares)

    # --- f) EV/EBITDA ---
    market_cap = current_price * shares if current_price and shares else None
    latest_ltd = _nonull(ltd)[-1] if _nonull(ltd) else 0
    cash_approx = None
    if latest_assets and latest_liab:
        cash_approx = max(0, latest_assets - latest_liab)

    ev = None
    if market_cap is not None:
        ev = market_cap + (latest_ltd or 0)  # simplified EV
        if cash_approx:
            ev -= cash_approx * 0.1  # rough cash adjustment

    da_nn = _nonull(da[-3:])
    avg_da_3 = _avg(da_nn)
    avg_ebitda_3 = None
    if avg_ebit_3 and avg_da_3:
        avg_ebitda_3 = avg_ebit_3 + avg_da_3

    ev_ebitda = None
    if ev and avg_ebitda_3 and avg_ebitda_3 > 0:
        ev_ebitda = ev / avg_ebitda_3

    # --- g) Composite IV ---
    # Weight: EPV 30%, DCF base 40%, Graham 15%, Asset 15%
    components = []
    weights    = []

    if epv and epv > 0:
        components.append(epv)
        weights.append(0.30)
    if dcf_base and dcf_base > 0:
        components.append(dcf_base)
        weights.append(0.40)
    if graham_number and graham_number > 0:
        components.append(graham_number)
        weights.append(0.15)
    if asset_value and asset_value > 0:
        components.append(asset_value)
        weights.append(0.15)

    if components:
        total_weight = sum(weights)
        composite_iv = sum(c * w for c, w in zip(components, weights)) / total_weight
        conservative_iv = min(v for v in [epv, dcf_base] if v and v > 0) if any(v and v > 0 for v in [epv, dcf_base]) else composite_iv
    else:
        composite_iv    = None
        conservative_iv = None

    # MoS calculation
    mos = None
    if conservative_iv and conservative_iv > 0 and current_price:
        mos = (conservative_iv - current_price) / conservative_iv

    # Verdict
    if mos is None:
        verdict = "DATA_ERROR"
    elif mos >= 0.25:
        verdict = "BUY"
    elif mos >= 0.10:
        verdict = "WATCH"
    elif mos >= 0.0:
        verdict = "HOLD"
    else:
        verdict = "OVERVALUED"

    # Target entry (30% MoS)
    target_entry = (conservative_iv * 0.70) if conservative_iv else None

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_price": round(current_price, 2) if current_price else None,
        # DCF scenarios
        "dcf_bear": round(dcf_bear, 2) if dcf_bear else None,
        "dcf_base": round(dcf_base, 2) if dcf_base else None,
        "dcf_bull": round(dcf_bull, 2) if dcf_bull else None,
        "dcf_growth_assumptions": {
            "bear": round(bear_growth, 4),
            "base": round(base_growth, 4),
            "bull": round(bull_growth, 4),
        },
        # EPV
        "epv": round(epv, 2) if epv else None,
        "avg_ebit_3yr": round(avg_ebit_3, 0) if avg_ebit_3 else None,
        # Graham
        "graham_number": round(graham_number, 2) if graham_number else None,
        "eps_used": round(eps, 4) if eps else None,
        "bvps_used": round(bvps, 4) if bvps else None,
        # Asset
        "asset_value": round(asset_value, 2) if asset_value else None,
        # Reverse DCF
        "implied_growth_rate": round(implied_growth, 4) if implied_growth else None,
        # EV/EBITDA
        "ev_ebitda": round(ev_ebitda, 2) if ev_ebitda else None,
        "avg_ebitda_3yr": round(avg_ebitda_3, 0) if avg_ebitda_3 else None,
        # Composite
        "composite_iv": round(composite_iv, 2) if composite_iv else None,
        "conservative_iv": round(conservative_iv, 2) if conservative_iv else None,
        "margin_of_safety": round(mos, 4) if mos is not None else None,
        "target_entry_price_30pct_mos": round(target_entry, 2) if target_entry else None,
        "verdict": verdict,
        "revenue_cagr_5yr": round(rev_cagr_5, 4) if rev_cagr_5 else None,
        "avg_fcf_3yr": round(avg_fcf, 0) if avg_fcf else None,
        "scenario_summary": {
            "bear": {"dcf": dcf_bear, "growth": bear_growth, "terminal": TERMINAL_GROWTH_BEAR},
            "base": {"dcf": dcf_base, "epv": epv, "growth": base_growth, "terminal": TERMINAL_GROWTH_BASE},
            "bull": {"dcf": dcf_bull, "growth": bull_growth, "terminal": TERMINAL_GROWTH_BULL},
        },
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Valuation] {ticker}: price=${current_price:.2f} | IV_conservative=${conservative_iv:.2f} | "
        f"MoS={mos*100:.1f}% | verdict={verdict}"
        if composite_iv else f"[Valuation] {ticker}: failed to compute IV"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_valuation_research(tk)
        print(json.dumps(r, indent=2, default=str))

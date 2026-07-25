#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE FINANCIAL FORENSICS AGENT
======================================================
Deep 10-year financial analysis using EDGAR XBRL data.
Produces financial_forensics.json for each company.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"

RESEARCH_DIR.mkdir(parents=True, exist_ok=True)


def _safe(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if (f != f) else f
    except (TypeError, ValueError):
        return None


def _cagr(start: float, end: float, years: int) -> Optional[float]:
    if start is None or end is None or years <= 0 or start <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def _nonull(lst) -> list:
    return [v for v in (lst or []) if v is not None]


def _avg(lst) -> Optional[float]:
    n = _nonull(lst)
    return sum(n) / len(n) if n else None


def _std(lst) -> Optional[float]:
    n = _nonull(lst)
    if len(n) < 2:
        return None
    avg = sum(n) / len(n)
    variance = sum((x - avg) ** 2 for x in n) / len(n)
    return math.sqrt(variance)


def _trend(lst) -> str:
    """Return EXPANDING, STABLE, or CONTRACTING based on linear slope."""
    n = _nonull(lst)
    if len(n) < 3:
        return "UNKNOWN"
    xs = list(range(len(n)))
    avg_x = sum(xs) / len(xs)
    avg_y = sum(n) / len(n)
    num = sum((xs[i] - avg_x) * (n[i] - avg_y) for i in range(len(n)))
    den = sum((xs[i] - avg_x) ** 2 for i in range(len(n)))
    if den == 0:
        return "STABLE"
    slope = num / den
    rel_slope = slope / abs(avg_y) if avg_y != 0 else 0
    if rel_slope > 0.005:
        return "EXPANDING"
    elif rel_slope < -0.005:
        return "CONTRACTING"
    return "STABLE"


def run_financial_forensics(ticker: str) -> dict:
    """
    Deep 10-year financial analysis using EDGAR XBRL.
    Returns comprehensive financial profile saved to data/company_research/{ticker}/financial_forensics.json
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "financial_forensics.json"

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
    except Exception as e:
        logger.error(f"[Forensics] EDGAR fetch failed for {ticker}: {e}")
        result = {"ticker": ticker, "error": str(e), "forensics_score": 0,
                  "forensics_verdict": "RED_FLAG", "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    if not f:
        result = {"ticker": ticker, "error": "No EDGAR data", "forensics_score": 0,
                  "forensics_verdict": "RED_FLAG", "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    years     = f.get("years", [])
    revenue   = f.get("revenue", [])
    net_inc   = f.get("net_income", [])
    ocf       = f.get("operating_cf", [])
    capex_raw = f.get("capex", [])
    assets    = f.get("total_assets", [])
    liab      = f.get("total_liabilities", [])
    ltd       = f.get("long_term_debt", [])
    shares    = f.get("shares_outstanding", [])
    gp        = f.get("gross_profit", [])
    ebit      = f.get("ebit", [])
    da        = f.get("da", [])
    rec       = f.get("receivables", [])

    n = len(years)

    # --- a) Revenue quality ---
    rev_nn = _nonull(revenue)
    rev_cagr_3 = None
    rev_cagr_5 = None
    rev_cagr_10 = None

    if len(rev_nn) >= 4:
        rev_cagr_3 = _cagr(rev_nn[-4], rev_nn[-1], 3)
    if len(rev_nn) >= 6:
        rev_cagr_5 = _cagr(rev_nn[-6], rev_nn[-1], 5)
    if len(rev_nn) >= 10:
        rev_cagr_10 = _cagr(rev_nn[-10], rev_nn[-1], 10)

    yoy_rev_growth = []
    for i in range(1, n):
        if revenue[i] and revenue[i - 1] and revenue[i - 1] > 0:
            yoy_rev_growth.append((revenue[i] - revenue[i - 1]) / revenue[i - 1])

    rev_volatility = _std(yoy_rev_growth)

    # Revenue vs gross profit divergence
    gp_nn = _nonull(gp)
    rev_gp_divergence = None
    if len(gp_nn) >= 6 and len(rev_nn) >= 6:
        gp_cagr_5  = _cagr(gp_nn[-6], gp_nn[-1], 5) if len(gp_nn) >= 6 else None
        if rev_cagr_5 is not None and gp_cagr_5 is not None:
            rev_gp_divergence = rev_cagr_5 - gp_cagr_5

    # Large YoY jumps (acquisitions proxy): >25% single-year jump
    acquisition_years = []
    for i in range(1, n):
        if revenue[i] and revenue[i - 1] and revenue[i - 1] > 0:
            g = (revenue[i] - revenue[i - 1]) / revenue[i - 1]
            if g > 0.25:
                acquisition_years.append(years[i] if i < len(years) else str(i))

    # --- b) Profitability trends ---
    gross_margins = []
    for i in range(n):
        if gp[i] is not None and revenue[i] and revenue[i] > 0:
            gross_margins.append(gp[i] / revenue[i])
        else:
            gross_margins.append(None)

    ebit_margins = []
    for i in range(n):
        if ebit[i] is not None and revenue[i] and revenue[i] > 0:
            ebit_margins.append(ebit[i] / revenue[i])
        else:
            ebit_margins.append(None)

    net_margins = []
    earnings_without_cash_years = []
    for i in range(n):
        if net_inc[i] is not None and revenue[i] and revenue[i] > 0:
            net_margins.append(net_inc[i] / revenue[i])
        else:
            net_margins.append(None)
        # Flag: net income > operating CF
        if net_inc[i] is not None and ocf[i] is not None and net_inc[i] > 0 and net_inc[i] > ocf[i]:
            earnings_without_cash_years.append(years[i] if i < len(years) else str(i))

    gross_margin_trend = _trend(gross_margins)
    ebit_margin_trend  = _trend(ebit_margins)
    net_margin_trend   = _trend(net_margins)

    # --- c) FCF analysis ---
    fcf_list = []
    for i in range(n):
        o = ocf[i]
        c = capex_raw[i]
        if o is not None and c is not None:
            fcf_list.append(o - c)
        elif o is not None:
            fcf_list.append(o)
        else:
            fcf_list.append(None)

    fcf_margins = []
    for i in range(n):
        if fcf_list[i] is not None and revenue[i] and revenue[i] > 0:
            fcf_margins.append(fcf_list[i] / revenue[i])
        else:
            fcf_margins.append(None)

    fcf_conversion = []  # FCF / net income
    for i in range(n):
        if fcf_list[i] is not None and net_inc[i] and abs(net_inc[i]) > 0:
            fcf_conversion.append(fcf_list[i] / net_inc[i])
        else:
            fcf_conversion.append(None)

    capex_intensity = []  # capex / revenue
    for i in range(n):
        c = capex_raw[i]
        if c is not None and revenue[i] and revenue[i] > 0:
            capex_intensity.append(abs(c) / revenue[i])
        else:
            capex_intensity.append(None)

    fcf_nn = _nonull(fcf_list)
    avg_fcf_3  = _avg(fcf_nn[-3:]) if len(fcf_nn) >= 3 else _avg(fcf_nn)
    avg_fcf_5  = _avg(fcf_nn[-5:]) if len(fcf_nn) >= 5 else _avg(fcf_nn)
    avg_fcf_10 = _avg(fcf_nn)

    avg_fcf_conversion = _avg(_nonull(fcf_conversion))

    # --- d) Balance sheet health ---
    debt_assets = []
    for i in range(n):
        if ltd[i] is not None and assets[i] and assets[i] > 0:
            debt_assets.append(ltd[i] / assets[i])
        else:
            debt_assets.append(None)

    ebitda_approx = []
    for i in range(n):
        e = ebit[i]
        d = da[i]
        if e is not None and d is not None:
            ebitda_approx.append(e + d)
        elif e is not None:
            ebitda_approx.append(e)
        else:
            ebitda_approx.append(None)

    debt_ebitda = []
    for i in range(n):
        if ltd[i] is not None and ebitda_approx[i] and ebitda_approx[i] > 0:
            debt_ebitda.append(ltd[i] / ebitda_approx[i])
        else:
            debt_ebitda.append(None)

    # Flag: debt growing faster than EBIT for 3+ consecutive years
    debt_faster_than_ebit = 0
    for i in range(1, n):
        if (ltd[i] is not None and ltd[i - 1] is not None and ltd[i - 1] > 0 and
                ebit[i] is not None and ebit[i - 1] is not None and ebit[i - 1] > 0):
            debt_growth = (ltd[i] - ltd[i - 1]) / abs(ltd[i - 1])
            ebit_growth = (ebit[i] - ebit[i - 1]) / abs(ebit[i - 1])
            if debt_growth > ebit_growth:
                debt_faster_than_ebit += 1
            else:
                debt_faster_than_ebit = 0

    # --- e) Returns on capital ---
    roic_list = []
    for i in range(n):
        ni = net_inc[i]
        a  = assets[i]
        cl_approx = liab[i] * 0.4 if liab[i] is not None else None  # rough current liab
        if ni is not None and a is not None and cl_approx is not None:
            inv_cap = a - cl_approx
            if inv_cap > 0:
                roic_list.append(ni / inv_cap)
            else:
                roic_list.append(None)
        else:
            roic_list.append(None)

    roa_list = []
    for i in range(n):
        if net_inc[i] is not None and assets[i] and assets[i] > 0:
            roa_list.append(net_inc[i] / assets[i])
        else:
            roa_list.append(None)

    roic_nn = _nonull(roic_list)
    roic_trend = _trend(roic_list)
    years_roic_above_10 = sum(1 for v in roic_nn if v > 0.10)

    # --- f) Anomaly detection ---
    accruals = []
    persistent_accrual_flag = False
    for i in range(n):
        if net_inc[i] is not None and ocf[i] is not None and assets[i] and assets[i] > 0:
            acc = (net_inc[i] - ocf[i]) / assets[i]
            accruals.append(acc)
        else:
            accruals.append(None)

    acc_nn = _nonull(accruals)
    high_accrual_count = sum(1 for v in acc_nn[-5:] if v > 0.05)
    if high_accrual_count >= 3:
        persistent_accrual_flag = True

    # Revenue/receivables divergence
    rec_rev_divergence_flag = False
    for i in range(1, n):
        if (rec[i] is not None and rec[i - 1] is not None and rec[i - 1] > 0 and
                revenue[i] is not None and revenue[i - 1] is not None and revenue[i - 1] > 0):
            rec_growth = (rec[i] - rec[i - 1]) / abs(rec[i - 1])
            rev_growth = (revenue[i] - revenue[i - 1]) / abs(revenue[i - 1])
            if rec_growth - rev_growth > 0.15:
                rec_rev_divergence_flag = True
                break

    # D&A as % of assets declining while assets grow
    da_depreciation_flag = False
    da_nn = [(i, v) for i, v in enumerate(da) if v is not None]
    assets_nn = [(i, v) for i, v in enumerate(assets) if v is not None]
    if len(da_nn) >= 2 and len(assets_nn) >= 2:
        da_pct_now   = da_nn[-1][1] / max(assets_nn[-1][1], 1)
        da_pct_prev  = da_nn[-2][1] / max(assets_nn[-2][1], 1)
        asset_growth = (assets_nn[-1][1] / max(assets_nn[-2][1], 1)) - 1
        if da_pct_now < da_pct_prev * 0.85 and asset_growth > 0.05:
            da_depreciation_flag = True

    # --- g) Shareholder returns ---
    shares_nn = [(i, v) for i, v in enumerate(shares) if v is not None]
    shares_5yr_change = None
    if len(shares_nn) >= 6:
        shares_5yr_change = (shares_nn[-1][1] - shares_nn[-6][1]) / abs(shares_nn[-6][1])

    shares_trend = "UNKNOWN"
    if shares_5yr_change is not None:
        if shares_5yr_change < -0.03:
            shares_trend = "BUYBACK"
        elif shares_5yr_change > 0.03:
            shares_trend = "DILUTION"
        else:
            shares_trend = "STABLE"

    # --- Forensics score ---
    anomaly_count = sum([persistent_accrual_flag, rec_rev_divergence_flag, da_depreciation_flag])
    anomaly_count += min(len(earnings_without_cash_years), 3)

    score = 100

    # Revenue consistency
    if rev_volatility is not None:
        if rev_volatility > 0.30:
            score -= 20
        elif rev_volatility > 0.15:
            score -= 10

    # FCF conversion
    if avg_fcf_conversion is not None:
        if avg_fcf_conversion < 0.5:
            score -= 20
        elif avg_fcf_conversion < 0.8:
            score -= 10

    # ROIC level
    if roic_nn:
        avg_roic = _avg(roic_nn)
        if avg_roic is not None:
            if avg_roic < 0.05:
                score -= 20
            elif avg_roic < 0.10:
                score -= 10

    # ROIC trend
    if roic_trend == "CONTRACTING":
        score -= 10

    # Debt trend
    if debt_faster_than_ebit >= 3:
        score -= 15

    # Anomalies
    score -= anomaly_count * 10

    # Dilution
    if shares_trend == "DILUTION":
        score -= 10

    score = max(0, min(100, score))

    if score >= 80:
        verdict = "EXCELLENT"
    elif score >= 65:
        verdict = "GOOD"
    elif score >= 50:
        verdict = "FAIR"
    elif score >= 30:
        verdict = "POOR"
    else:
        verdict = "RED_FLAG"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "years": years,
        # Revenue quality
        "revenue_cagr_3yr": rev_cagr_3,
        "revenue_cagr_5yr": rev_cagr_5,
        "revenue_cagr_10yr": rev_cagr_10,
        "revenue_volatility_std": rev_volatility,
        "revenue_vs_gp_divergence_5yr": rev_gp_divergence,
        "possible_acquisition_years": acquisition_years,
        # Profitability
        "gross_margins_by_year": gross_margins,
        "ebit_margins_by_year": ebit_margins,
        "net_margins_by_year": net_margins,
        "gross_margin_trend": gross_margin_trend,
        "ebit_margin_trend": ebit_margin_trend,
        "net_margin_trend": net_margin_trend,
        "earnings_without_cash_years": earnings_without_cash_years,
        # FCF
        "fcf_by_year": fcf_list,
        "fcf_margin_by_year": fcf_margins,
        "fcf_conversion_by_year": fcf_conversion,
        "avg_fcf_conversion": avg_fcf_conversion,
        "capex_intensity_by_year": capex_intensity,
        "avg_fcf_3yr": avg_fcf_3,
        "avg_fcf_5yr": avg_fcf_5,
        "avg_fcf_10yr": avg_fcf_10,
        # Balance sheet
        "debt_assets_ratio_by_year": debt_assets,
        "long_term_debt_by_year": [ltd[i] for i in range(n)],
        "debt_ebitda_by_year": debt_ebitda,
        "debt_growing_faster_than_ebit_years": debt_faster_than_ebit,
        # Returns on capital
        "roic_by_year": roic_list,
        "roa_by_year": roa_list,
        "roic_trend": roic_trend,
        "years_roic_above_10pct": years_roic_above_10,
        "total_years_analyzed": len(roic_nn),
        # Anomalies
        "accruals_by_year": accruals,
        "persistent_accrual_flag": persistent_accrual_flag,
        "receivables_revenue_divergence_flag": rec_rev_divergence_flag,
        "da_depreciation_policy_flag": da_depreciation_flag,
        "anomaly_count": anomaly_count,
        # Shareholder returns
        "shares_5yr_change": shares_5yr_change,
        "shares_trend": shares_trend,
        # Verdict
        "forensics_score": score,
        "forensics_verdict": verdict,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Forensics] {ticker}: score={score} | verdict={verdict} | "
        f"anomalies={anomaly_count} | roic_trend={roic_trend}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_financial_forensics(tk)
        print(json.dumps(r, indent=2, default=str))

#!/usr/bin/env python3
"""
APEX CAPITAL -- EARNINGS QUALITY DEEP AGENT
=============================================
Deep earnings quality analysis beyond basic accruals.
Saves output to data/company_research/{ticker}/earnings_quality_deep.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {
    "User-Agent": "ApexCapital research@apexcapital.com",
    "Accept": "application/json",
}
EDGAR_RATE_LIMIT = 0.1


def _edgar_get(url: str) -> Optional[dict]:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[EQD] EDGAR fetch failed {url}: {e}")
        return None


def _get_cik(ticker: str) -> Optional[str]:
    import urllib.request
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            tickers_data = json.loads(resp.read().decode("utf-8"))
        for key, val in tickers_data.items():
            if val.get("ticker", "").upper() == ticker.upper():
                return str(val["cik_str"]).zfill(10)
    except Exception as e:
        logger.debug(f"[EQD] CIK lookup failed: {e}")
    return None


def _get_company_facts(cik: str) -> Optional[dict]:
    """Fetch all company financial facts from EDGAR."""
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK" + cik + ".json"
    return _edgar_get(url)


def _extract_annual_series(facts: dict, concept: str, namespace: str = "us-gaap") -> dict:
    """Extract annual (10-K) values for a concept. Returns {year: value}."""
    try:
        units = facts.get("facts", {}).get(namespace, {}).get(concept, {}).get("units", {})
        # Try USD then pure numbers
        for unit_key in ["USD", "pure", "shares"]:
            if unit_key in units:
                annual = {}
                for entry in units[unit_key]:
                    if entry.get("form") in ("10-K", "10-K/A") and entry.get("fp") == "FY":
                        year = int(entry.get("end", "0000")[:4])
                        if year > 2000:
                            annual[year] = entry.get("val", 0)
                if annual:
                    return annual
    except Exception:
        pass
    return {}


def _cagr(series: dict, years: int = 10) -> Optional[float]:
    """Compute CAGR over last N years from a year->value dict."""
    if len(series) < 2:
        return None
    sorted_years = sorted(series.keys())
    end_yr = sorted_years[-1]
    start_yr = max(sorted_years[0], end_yr - years)
    if start_yr == end_yr:
        return None
    start_val = series.get(start_yr)
    end_val = series.get(end_yr)
    if not start_val or not end_val or start_val <= 0 or end_val <= 0:
        return None
    n = end_yr - start_yr
    return (end_val / start_val) ** (1 / n) - 1


def _earnings_persistence(earnings_series: dict) -> float:
    """
    Autocorrelation of earnings year-over-year.
    High = earnings persist (quality). Returns 0-100.
    """
    if len(earnings_series) < 4:
        return 50.0
    years = sorted(earnings_series.keys())
    vals = [earnings_series[y] for y in years]
    # Year-over-year changes
    changes = [1 if vals[i] > vals[i-1] else -1 for i in range(1, len(vals))]
    if len(changes) < 2:
        return 50.0
    # Autocorrelation: same direction two years in a row
    same_direction = sum(1 for i in range(1, len(changes)) if changes[i] == changes[i-1])
    persistence = same_direction / (len(changes) - 1)
    return round(persistence * 100, 1)


def run_earnings_quality_deep(ticker: str) -> dict:
    """
    Deep earnings quality analysis.
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "earnings_quality_deep.json"

    logger.info(f"[EQD] Starting earnings quality deep analysis for {ticker}")

    # Get CIK and facts
    cik = _get_cik(ticker)
    if not cik:
        result = {"ticker": ticker, "error": "CIK not found", "earnings_quality_score": 50,
                  "earnings_quality_verdict": "MEDIUM"}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2)
        return result

    facts = _get_company_facts(cik)
    if not facts:
        result = {"ticker": ticker, "error": "EDGAR facts unavailable", "earnings_quality_score": 50,
                  "earnings_quality_verdict": "MEDIUM"}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2)
        return result

    # Extract key financial series
    revenue = _extract_annual_series(facts, "Revenues")
    if not revenue:
        revenue = _extract_annual_series(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    if not revenue:
        revenue = _extract_annual_series(facts, "SalesRevenueNet")

    receivables = _extract_annual_series(facts, "AccountsReceivableNetCurrent")
    net_income = _extract_annual_series(facts, "NetIncomeLoss")
    ocf = _extract_annual_series(facts, "NetCashProvidedByUsedInOperatingActivities")
    goodwill = _extract_annual_series(facts, "Goodwill")
    total_assets = _extract_annual_series(facts, "Assets")
    deferred_revenue = _extract_annual_series(facts, "DeferredRevenueCurrent")
    ebit = _extract_annual_series(facts, "OperatingIncomeLoss")
    extraordinary = _extract_annual_series(facts, "DiscontinuedOperationIncomeLossFromDiscontinuedOperationDuringPhaseOutPeriodBeforeIncomeTax")

    # 1. Revenue recognition risk: receivables CAGR vs revenue CAGR
    rev_cagr = _cagr(revenue, 10)
    rec_cagr = _cagr(receivables, 10)
    revenue_recognition_flag = False
    if rev_cagr is not None and rec_cagr is not None and rev_cagr > 0:
        if rec_cagr > rev_cagr * 2:
            revenue_recognition_flag = True
            logger.info(f"[EQD] {ticker}: Revenue recognition flag -- receivables CAGR {rec_cagr:.1%} > 2x revenue CAGR {rev_cagr:.1%}")

    # 2. Channel stuffing: Q4 spike pattern
    # We can approximate from available annual data -- look for large swings
    channel_stuffing_flag = False
    if len(revenue) >= 5:
        rev_vals = [revenue[y] for y in sorted(revenue.keys())[-5:]]
        rev_diffs = [abs(rev_vals[i] - rev_vals[i-1]) / max(rev_vals[i-1], 1) for i in range(1, len(rev_vals))]
        if max(rev_diffs) > 0.25 and sum(1 for d in rev_diffs if d > 0.20) >= 3:
            channel_stuffing_flag = True

    # 3. One-time items frequency
    one_time_count = len([y for y in extraordinary if extraordinary[y] != 0])
    one_time_frequency = one_time_count

    # 4. Goodwill as % of total assets (most recent)
    goodwill_pct_assets = None
    if goodwill and total_assets:
        recent_yr = max(set(goodwill.keys()) & set(total_assets.keys()), default=None)
        if recent_yr:
            ta = total_assets[recent_yr]
            gw = goodwill[recent_yr]
            if ta > 0:
                goodwill_pct_assets = round(gw / ta * 100, 1)

    # 5. Deferred revenue trend (positive if growing faster than revenue)
    def_rev_cagr = _cagr(deferred_revenue, 5)
    deferred_revenue_positive = False
    if def_rev_cagr is not None and rev_cagr is not None:
        if def_rev_cagr > rev_cagr:
            deferred_revenue_positive = True  # Cash received in advance -- quality signal

    # 6. Operating leverage: revenue vs EBIT sensitivity
    operating_leverage = None
    if len(revenue) >= 3 and len(ebit) >= 3:
        rev_years = sorted(set(revenue.keys()) & set(ebit.keys()))
        if len(rev_years) >= 3:
            recent = rev_years[-3:]
            rev_chg = (revenue[recent[-1]] - revenue[recent[0]]) / max(abs(revenue[recent[0]]), 1)
            ebit_chg = (ebit[recent[-1]] - ebit[recent[0]]) / max(abs(ebit[recent[0]]), 1)
            if abs(rev_chg) > 0.01:
                operating_leverage = round(ebit_chg / rev_chg, 2)

    # 7. Earnings persistence
    persistence_score = _earnings_persistence(net_income)

    # 8. Cash earnings ratio (OCF / Net Income avg over 10yr)
    cash_earnings_ratio_avg = None
    if ocf and net_income:
        common_years = sorted(set(ocf.keys()) & set(net_income.keys()))[-10:]
        ratios = []
        for yr in common_years:
            ni = net_income[yr]
            op = ocf[yr]
            if ni and abs(ni) > 1000:
                ratios.append(op / ni)
        if ratios:
            cash_earnings_ratio_avg = round(sum(ratios) / len(ratios), 3)

    # --- Scoring ---
    eq_score = 50  # baseline

    # Cash earnings ratio (most important)
    if cash_earnings_ratio_avg is not None:
        if cash_earnings_ratio_avg > 1.2:
            eq_score += 25
        elif cash_earnings_ratio_avg > 1.0:
            eq_score += 15
        elif cash_earnings_ratio_avg > 0.8:
            eq_score += 5
        elif cash_earnings_ratio_avg < 0.5:
            eq_score -= 30
        else:
            eq_score -= 15

    # Revenue recognition flag
    if revenue_recognition_flag:
        eq_score -= 20

    # Channel stuffing
    if channel_stuffing_flag:
        eq_score -= 15

    # Goodwill accumulation
    if goodwill_pct_assets is not None:
        if goodwill_pct_assets > 40:
            eq_score -= 10
        elif goodwill_pct_assets > 30:
            eq_score -= 5

    # One-time items
    if one_time_frequency >= 5:
        eq_score -= 10
    elif one_time_frequency >= 3:
        eq_score -= 5

    # Deferred revenue (positive signal)
    if deferred_revenue_positive:
        eq_score += 8

    # Earnings persistence
    eq_score += int((persistence_score - 50) * 0.3)

    eq_score = max(0, min(100, eq_score))

    if eq_score >= 75:
        verdict = "HIGH"
    elif eq_score >= 55:
        verdict = "MEDIUM"
    elif eq_score >= 35:
        verdict = "LOW"
    else:
        verdict = "POOR"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cash_earnings_ratio_avg": cash_earnings_ratio_avg,
        "revenue_recognition_flag": revenue_recognition_flag,
        "channel_stuffing_flag": channel_stuffing_flag,
        "goodwill_pct_assets": goodwill_pct_assets,
        "one_time_items_frequency": one_time_frequency,
        "deferred_revenue_growing_faster_than_revenue": deferred_revenue_positive,
        "operating_leverage": operating_leverage,
        "earnings_persistence_score": round(persistence_score, 1),
        "earnings_quality_score": eq_score,
        "earnings_quality_verdict": verdict,
        "revenue_cagr_10yr": round(rev_cagr * 100, 2) if rev_cagr is not None else None,
        "receivables_cagr_10yr": round(rec_cagr * 100, 2) if rec_cagr is not None else None,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[EQD] {ticker}: score={eq_score} verdict={verdict} "
        f"cash_ratio={cash_earnings_ratio_avg} rec_flag={revenue_recognition_flag}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSCI"
    r = run_earnings_quality_deep(ticker)
    print(json.dumps(r, indent=2, default=str))

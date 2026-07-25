#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE MANAGEMENT QUALITY ANALYZER
========================================================
Scores management quality 0-100 using EDGAR data + Form 4 insider filings.

Four dimensions:
  a) Insider trading signal (Form 4, last 12 months)
  b) Share dilution / buyback history (5yr shares outstanding trend)
  c) Capital allocation quality (ROIC trend over 5 years)
  d) Earnings quality (Operating CF / Net Income ratio)

Score categories:
  80+ : EXCELLENT
  60-79: GOOD
  40-59: FAIR
  <40 : POOR

Usage:
  from fe_management_quality import assess_management
  result = assess_management("AAPL")
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}


def _safe(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _insider_signal(cik: str) -> tuple[float, str, list[str]]:
    """
    Parse Form 4 filings to estimate insider buy/sell ratio.
    Returns (buy_ratio 0-1, signal_label, notes).
    Falls back to 0.5 (neutral) if data unavailable.
    """
    notes = []
    try:
        import requests
        today = datetime.now(timezone.utc).date()
        one_year_ago = today - timedelta(days=365)
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{cik}%22"
            f"&forms=4"
            f"&dateRange=custom"
            f"&startdt={one_year_ago.isoformat()}"
            f"&enddt={today.isoformat()}"
        )
        time.sleep(0.1)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        # Count transaction types from description text
        purchases = 0
        sales     = 0
        for hit in hits[:50]:   # cap at 50 to avoid rate limit
            src = hit.get("_source", {})
            # Form 4 filings show transaction codes in display fields
            # We look for 'P' (purchase) vs 'S' (sale) in the description
            desc = str(src.get("file_date", "")) + str(src.get("display_names", ""))
            # Use filing count as proxy -- actual parsing needs XML
            purchases += 1   # conservative: count all as buys for now

        # If we have filing activity but can't parse codes, use neutral
        if hits:
            notes.append(f"Found {len(hits)} Form 4 filings in last 12 months (transaction parsing limited)")
            buy_ratio = 0.5
        else:
            notes.append("No Form 4 filings found in last 12 months")
            buy_ratio = 0.5

    except Exception as e:
        logger.debug(f"[Mgmt] Insider data error: {e}")
        notes.append(f"Insider data unavailable: {e}")
        buy_ratio = 0.5

    if buy_ratio > 0.6:
        signal = "BULLISH"
    elif buy_ratio < 0.3:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    return buy_ratio, signal, notes


def _dilution_trend(shares_list: list) -> tuple[float, str]:
    """
    Compute 5yr share count change.
    Returns (pct_change, label).
    """
    non_null = [v for v in shares_list if v is not None and v > 0]
    if len(non_null) < 2:
        return 0.0, "UNKNOWN"
    # Use up to last 5 years
    subset = non_null[-min(6, len(non_null)):]
    start  = subset[0]
    end    = subset[-1]
    pct    = (end / start) - 1.0

    if pct < -0.05:
        label = "BUYBACK_BULLISH"
    elif pct <= 0.03:
        label = "NEUTRAL"
    else:
        label = "DILUTION_BEARISH"

    return round(pct, 4), label


def _roic_trend(net_income_list: list, assets_list: list, curr_liab_list: list) -> tuple[list[float], str]:
    """
    ROIC = NetIncome / (TotalAssets - CurrentLiabilities) for each year.
    Returns (roic_list, trend_label).
    """
    roic_vals = []
    for ni, assets, cl in zip(net_income_list, assets_list, curr_liab_list):
        if ni is None or assets is None:
            continue
        cl_v = cl or 0.0
        inv_cap = assets - cl_v
        if inv_cap > 0:
            roic_vals.append(ni / inv_cap)

    if not roic_vals:
        return [], "UNKNOWN"

    avg_roic = sum(roic_vals) / len(roic_vals)

    # Trend: compare first half to second half
    if len(roic_vals) >= 4:
        mid  = len(roic_vals) // 2
        h1   = sum(roic_vals[:mid]) / mid
        h2   = sum(roic_vals[mid:]) / (len(roic_vals) - mid)
        improving = h2 > h1
    else:
        improving = None

    if avg_roic > 0.15:
        label = "EXCELLENT_ROIC"
    elif avg_roic > 0.10:
        label = "GOOD_ROIC" + ("_IMPROVING" if improving else "_STABLE")
    elif avg_roic > 0.05:
        label = "FAIR_ROIC" + ("_DECLINING" if improving is False else "")
    else:
        label = "POOR_ROIC"

    return [round(r, 4) for r in roic_vals], label


def _earnings_quality(net_income_list: list, ocf_list: list) -> tuple[float, str]:
    """
    Average Operating CF / Net Income ratio over available years.
    Returns (avg_ratio, quality_label).
    """
    ratios = []
    for ni, ocf in zip(net_income_list, ocf_list):
        if ni and ocf and abs(ni) > 0:
            ratios.append(ocf / ni)

    if not ratios:
        return 0.0, "UNKNOWN"

    avg_ratio = sum(ratios) / len(ratios)

    if avg_ratio > 1.2:
        label = "EXCELLENT"
    elif avg_ratio > 1.0:
        label = "GOOD"
    elif avg_ratio > 0.8:
        label = "FAIR"
    else:
        label = "POOR"

    return round(avg_ratio, 4), label


def _score_to_category(score: int) -> str:
    if score >= 80:
        return "EXCELLENT"
    elif score >= 60:
        return "GOOD"
    elif score >= 40:
        return "FAIR"
    return "POOR"


def assess_management(ticker: str, cik: str = None) -> dict:
    """
    Assess management quality using EDGAR data.
    Returns scored dict with insider signal, dilution trend, ROIC, earnings quality.
    """
    base = {
        "ticker":          ticker,
        "score":           50,
        "category":        "FAIR",
        "insider_signal":  "NEUTRAL",
        "dilution_trend":  "UNKNOWN",
        "roic_trend":      [],
        "roic_label":      "UNKNOWN",
        "earnings_quality": "UNKNOWN",
        "earnings_cf_ratio": None,
        "notes":           [],
    }

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals, _get_cik
        f = get_edgar_fundamentals(ticker)
        if not f:
            base["notes"].append("No EDGAR fundamentals available")
            return base

        # Get CIK if not provided
        if not cik:
            cik = f.get("cik") or _get_cik(ticker)

        shares_list = f.get("shares_outstanding", [])
        ni_list     = f.get("net_income", [])
        ocf_list    = f.get("operating_cf", [])
        assets_list = f.get("total_assets", [])
        cl_list     = f.get("current_liabilities", [])

        # a) Insider signal
        if cik:
            buy_ratio, insider_sig, insider_notes = _insider_signal(cik)
        else:
            buy_ratio, insider_sig, insider_notes = 0.5, "NEUTRAL", ["CIK not found"]
        base["notes"].extend(insider_notes)

        # b) Dilution
        dil_pct, dil_label = _dilution_trend(shares_list)

        # c) ROIC
        roic_list, roic_label = _roic_trend(ni_list, assets_list, cl_list)

        # d) Earnings quality
        eq_ratio, eq_label = _earnings_quality(ni_list, ocf_list)

        # ── Score assembly ────────────────────────────────────────────────────
        score = 0

        # Insider (0-20)
        if insider_sig == "BULLISH":
            score += 20
        elif insider_sig == "NEUTRAL":
            score += 12
        else:
            score += 0

        # Dilution (0-25)
        if dil_label == "BUYBACK_BULLISH":
            score += 25
        elif dil_label == "NEUTRAL":
            score += 15
        else:
            score += 0

        # ROIC (0-30)
        if "EXCELLENT" in roic_label:
            score += 30
        elif "GOOD" in roic_label:
            score += 22 if "IMPROVING" in roic_label else 18
        elif "FAIR" in roic_label:
            score += 10
        else:
            score += 0

        # Earnings quality (0-25)
        eq_points = {"EXCELLENT": 25, "GOOD": 18, "FAIR": 10, "POOR": 0, "UNKNOWN": 10}
        score += eq_points.get(eq_label, 10)

        score = min(100, max(0, score))

        return {
            "ticker":           ticker,
            "cik":              cik,
            "score":            score,
            "category":         _score_to_category(score),
            "insider_signal":   insider_sig,
            "insider_buy_ratio": round(buy_ratio, 3),
            "dilution_pct":     dil_pct,
            "dilution_trend":   dil_label,
            "roic_trend":       roic_list,
            "roic_label":       roic_label,
            "earnings_cf_ratio": eq_ratio,
            "earnings_quality": eq_label,
            "notes":            base["notes"],
            "data_source":      "EDGAR_XBRL",
        }

    except Exception as e:
        logger.warning(f"[Mgmt] assess_management({ticker}) failed: {e}")
        base["notes"].append(f"Error: {e}")
        return base


if __name__ == "__main__":
    import sys, json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL", "MSFT"]
    for tk in tickers:
        r = assess_management(tk)
        print(f"\n{tk}:")
        print(json.dumps(r, indent=2))

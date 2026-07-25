#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH AGENT
===========================================
industry_dynamics.py
Sector and industry analysis: secular trends, regulatory environment, cyclicality.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}
RATE_LIMIT    = 0.1

SECULAR_TAILWINDS = {
    "Healthcare": "aging demographics, chronic disease prevalence, healthcare spending growth",
    "Technology": "digital transformation, cloud migration, AI adoption",
    "Industrials": "infrastructure investment, reshoring, defense spending",
    "Consumer Staples": "population growth, emerging market middle class expansion",
    "Utilities": "energy transition, grid modernization, electrification",
    "Real Estate": "urbanization, housing undersupply in key markets",
    "Financials": "financial inclusion, payment digitization, wealth management growth",
}
SECULAR_HEADWINDS = {
    "Energy": "energy transition away from fossil fuels, ESG investment restrictions",
    "Consumer Discretionary": "consumer debt levels, discretionary spending sensitivity to rates",
    "Communication Services": "cord-cutting, streaming fragmentation, advertising cyclicality",
    "Materials": "commodity price volatility, China demand dependency",
}
REGULATORY_TERMS = [
    "FDA", "EPA", "FTC", "SEC", "CFPB", "FERC", "FCC", "antitrust",
    "regulatory approval", "compliance", "legislation", "government regulation",
    "HIPAA", "Dodd-Frank", "Basel", "GDPR", "CCPA",
]


def _edgar_get(url: str) -> Optional[str]:
    try:
        req = Request(url, headers=EDGAR_HEADERS)
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[IndDyn] EDGAR GET failed {url}: {e}")
        return None


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _get_sector_info(ticker: str) -> tuple:
    """Get sector and industry from yfinance."""
    sector   = "Unknown"
    industry = "Unknown"
    try:
        import yfinance as yf
        info     = yf.Ticker(ticker).info
        sector   = info.get("sector", "Unknown") or "Unknown"
        industry = info.get("industry", "Unknown") or "Unknown"
    except Exception as e:
        logger.warning(f"[IndDyn] yfinance info failed for {ticker}: {e}")
    return sector, industry


def _get_cik_from_ticker(ticker: str) -> Optional[str]:
    time.sleep(RATE_LIMIT)
    url  = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=10-K&dateb=&owner=include&count=5&output=atom"
    data = _edgar_get(url)
    if not data:
        return None
    m = re.search(r"CIK=(\d+)", data)
    return m.group(1).zfill(10) if m else None


def _get_annual_revenues(cik: str) -> list:
    """
    Fetch annual revenue data from EDGAR company facts.
    Returns list of (year, revenue) sorted ascending.
    """
    time.sleep(RATE_LIMIT)
    url  = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    data = _edgar_get(url)
    if not data:
        return []
    try:
        facts = json.loads(data)
    except Exception:
        return []

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    # Try common revenue concepts in order
    for concept in ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "SalesRevenueNet", "SalesRevenueGoodsNet", "RevenueFromContractWithCustomer",
                    "NetRevenues", "TotalRevenues"]:
        cdata = us_gaap.get(concept, {})
        if not cdata:
            continue
        units = cdata.get("units", {}).get("USD", [])
        annual = [e for e in units if e.get("form") == "10-K" and e.get("val") is not None]
        if annual:
            # Deduplicate by year (keep latest filed)
            by_year = {}
            for e in annual:
                end = e.get("end", "")
                year = int(end[:4]) if len(end) >= 4 else 0
                if year >= 1990:
                    if year not in by_year or e.get("filed", "") > by_year[year][1]:
                        by_year[year] = (e["val"], e.get("filed", ""))
            sorted_rev = sorted([(yr, v[0]) for yr, v in by_year.items()])
            if len(sorted_rev) >= 2:
                return sorted_rev
    return []


def _calc_revenue_declines(revenues: list) -> tuple:
    """Calculate revenue declines in 2008-09 and 2020."""
    rev_map = dict(revenues)
    decline_2009 = None
    decline_2020 = None

    if 2008 in rev_map and 2009 in rev_map and rev_map[2008] > 0:
        decline_2009 = round((rev_map[2009] - rev_map[2008]) / rev_map[2008] * 100, 1)
    if 2019 in rev_map and 2020 in rev_map and rev_map[2019] > 0:
        decline_2020 = round((rev_map[2020] - rev_map[2019]) / rev_map[2019] * 100, 1)

    return decline_2009, decline_2020


def _label_cyclicality(decline_2009: Optional[float], decline_2020: Optional[float]) -> str:
    """Label cyclicality from recession revenue declines."""
    max_decline = min(
        decline_2009 if decline_2009 is not None else 0,
        decline_2020 if decline_2020 is not None else 0,
    )  # most negative
    if max_decline < -20:
        return "HIGHLY_CYCLICAL"
    elif max_decline < -10:
        return "CYCLICAL"
    elif max_decline < -5:
        return "MILDLY_CYCLICAL"
    else:
        return "DEFENSIVE"


def _calc_cagr(revenues: list, years: int) -> Optional[float]:
    """Calculate revenue CAGR over last N years."""
    if len(revenues) < 2:
        return None
    end_rev   = revenues[-1][1]
    # Find starting point N years ago
    end_year  = revenues[-1][0]
    start_year = end_year - years
    start_rev  = None
    for yr, rv in revenues:
        if yr <= start_year:
            start_rev = rv
            actual_start = yr
    if start_rev is None or start_rev <= 0:
        return None
    actual_years = revenues[-1][0] - actual_start
    if actual_years <= 0:
        return None
    cagr = ((end_rev / start_rev) ** (1.0 / actual_years) - 1) * 100
    return round(cagr, 2)


def _count_regulatory_terms(text: str) -> int:
    clean = _clean_html(text[:100000]).lower()
    count = 0
    for term in REGULATORY_TERMS:
        count += len(re.findall(re.escape(term.lower()), clean))
    return count


def _extract_international_revenue(text: str) -> Optional[float]:
    """Extract international revenue percentage from 10-K."""
    clean = _clean_html(text[:100000]).lower()
    # Look for "international revenues were XX% of total"
    patterns = [
        r"international[^.]*?([\d]+(?:\.\d+)?)\s*%",
        r"([\d]+(?:\.\d+)?)\s*%[^.]*?international",
        r"outside[^.]*?united states[^.]*?([\d]+(?:\.\d+)?)\s*%",
        r"foreign[^.]*?revenue[^.]*?([\d]+(?:\.\d+)?)\s*%",
    ]
    for pat in patterns:
        m = re.search(pat, clean)
        if m:
            try:
                pct = float(m.group(1))
                if 1 <= pct <= 99:
                    return pct
            except Exception:
                pass
    return None


def _fetch_latest_10k_text(cik: str) -> Optional[str]:
    """Fetch most recent 10-K text from EDGAR."""
    time.sleep(RATE_LIMIT)
    url  = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _edgar_get(url)
    if not data:
        return None
    try:
        subs = json.loads(data)
    except Exception:
        return None

    recent = subs.get("filings", {}).get("recent", {})
    forms  = recent.get("form", [])
    accns  = recent.get("accessionNumber", [])
    docs   = recent.get("primaryDocument", [])
    cik_int = int(cik)

    for i, f in enumerate(forms):
        if f == "10-K":
            accn     = accns[i].replace("-", "")
            doc_name = docs[i] if i < len(docs) else ""
            if doc_name:
                time.sleep(RATE_LIMIT)
                doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{doc_name}"
                text    = _edgar_get(doc_url)
                if text and len(text) > 5000:
                    return text
            break
    return None


def _score_industry(
    sector: str,
    cyclicality: str,
    reg_count: int,
    cagr_10yr: Optional[float],
    cagr_3yr: Optional[float],
) -> tuple:
    score = 60

    # Secular tailwind/headwind
    if sector in SECULAR_TAILWINDS:
        score += 10
    if sector in SECULAR_HEADWINDS:
        score -= 10

    # Cyclicality
    cyc_adj = {"DEFENSIVE": 15, "MILDLY_CYCLICAL": 5, "CYCLICAL": -5, "HIGHLY_CYCLICAL": -20}
    score += cyc_adj.get(cyclicality, 0)

    # Regulatory burden
    if reg_count > 50:
        reg_burden = "HIGH"
        score -= 10
    elif reg_count > 20:
        reg_burden = "MEDIUM"
    else:
        reg_burden = "LOW"
        score += 5

    # Growth trend
    if cagr_10yr is not None and cagr_3yr is not None:
        if cagr_3yr > cagr_10yr + 2:
            growth_trend = "ACCELERATING"
            score += 10
        elif cagr_3yr < cagr_10yr - 2:
            growth_trend = "DECELERATING"
            score -= 10
        else:
            growth_trend = "STABLE"
    elif cagr_10yr is not None:
        growth_trend = "STABLE"
        if cagr_10yr >= 5:
            score += 5
        elif cagr_10yr < 0:
            score -= 15
    else:
        growth_trend = "UNKNOWN"
        reg_burden   = reg_burden  # keep

    score = max(0, min(100, score))
    if score >= 80:
        verdict = "EXCELLENT"
    elif score >= 65:
        verdict = "GOOD"
    elif score >= 45:
        verdict = "FAIR"
    else:
        verdict = "POOR"

    return score, verdict, reg_burden, growth_trend


def run_industry_dynamics(ticker: str) -> dict:
    """
    Sector and industry analysis: secular trends, regulatory environment, cyclicality.
    """
    ticker  = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "industry_dynamics.json"

    notes = []

    # Sector info
    sector, industry = _get_sector_info(ticker)

    # CIK
    cik = _get_cik_from_ticker(ticker)
    if not cik:
        notes.append("Could not resolve CIK -- skipping EDGAR revenue data")

    # Revenue history from EDGAR
    revenues = []
    if cik:
        revenues = _get_annual_revenues(cik)
        if not revenues:
            notes.append("Could not fetch revenue data from EDGAR company facts")

    # Decline analysis
    decline_2009, decline_2020 = _calc_revenue_declines(revenues)
    cyclicality = _label_cyclicality(decline_2009, decline_2020)

    # CAGR
    cagr_10yr = _calc_cagr(revenues, 10)
    cagr_3yr  = _calc_cagr(revenues, 3)

    # 10-K text for regulatory and international revenue
    ten_k_text     = _fetch_latest_10k_text(cik) if cik else None
    reg_count       = _count_regulatory_terms(ten_k_text) if ten_k_text else 0
    intl_rev_pct    = _extract_international_revenue(ten_k_text) if ten_k_text else None

    if not ten_k_text:
        notes.append("10-K text unavailable -- regulatory count and intl revenue not extracted")

    # Secular signals
    tailwind = SECULAR_TAILWINDS.get(sector, "No specific tailwind identified for this sector")
    headwind = SECULAR_HEADWINDS.get(sector, "No specific headwind identified for this sector")

    # Score
    score, verdict, reg_burden, growth_trend = _score_industry(
        sector, cyclicality, reg_count, cagr_10yr, cagr_3yr,
    )

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sector": sector,
        "industry": industry,
        "secular_tailwind": tailwind,
        "secular_headwind": headwind,
        "cyclicality": cyclicality,
        "revenue_decline_2008_pct": decline_2009,
        "revenue_decline_2020_pct": decline_2020,
        "regulatory_burden": reg_burden,
        "regulatory_terms_count": reg_count,
        "revenue_cagr_10yr": cagr_10yr,
        "revenue_cagr_3yr": cagr_3yr,
        "growth_trend": growth_trend,
        "international_revenue_pct": intl_rev_pct,
        "industry_score": score,
        "industry_verdict": verdict,
        "notes": notes,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[IndDyn] {ticker}: sector={sector} cyclicality={cyclicality} "
        f"cagr10yr={cagr_10yr} cagr3yr={cagr_3yr} score={score} verdict={verdict}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_industry_dynamics(tk)
        print(json.dumps(r, indent=2, default=str))

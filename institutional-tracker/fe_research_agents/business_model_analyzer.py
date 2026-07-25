#!/usr/bin/env python3
"""
APEX CAPITAL -- BUSINESS MODEL ANALYZER AGENT
===============================================
Classifies business model and assesses durability.
Saves output to data/company_research/{ticker}/business_model_analysis.json
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

REVENUE_MODEL_KEYWORDS = {
    "SUBSCRIPTION_SAAS": ["subscription", "recurring", "saas", "software-as-a-service", "annual contract"],
    "LICENSE_ROYALTY": ["license", "royalty", "licensing revenue", "intellectual property"],
    "TRANSACTION_VOLUME": ["transaction", "volume-based", "per-transaction", "interchange"],
    "PROJECT_BASED": ["project-based", "backlog", "milestone", "project revenue"],
    "PRODUCT_SALES": ["product sales", "unit sales", "hardware", "inventory"],
}


def _edgar_get(url: str) -> Optional[dict]:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[BMA] EDGAR fetch failed {url}: {e}")
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
        logger.debug(f"[BMA] CIK lookup failed: {e}")
    return None


def _get_company_facts(cik: str) -> Optional[dict]:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK" + cik + ".json"
    return _edgar_get(url)


def _extract_annual_series(facts: dict, concept: str, namespace: str = "us-gaap") -> dict:
    try:
        units = facts.get("facts", {}).get(namespace, {}).get(concept, {}).get("units", {})
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


def _get_10k_text_snippet(cik: str) -> str:
    """Fetch a portion of the most recent 10-K for keyword analysis."""
    import urllib.request
    try:
        cik_clean = cik.lstrip("0")
        subs_url = "https://data.sec.gov/submissions/CIK" + cik + ".json"
        req = urllib.request.Request(subs_url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            subs = json.loads(resp.read().decode("utf-8"))

        filings = subs.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])

        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A"):
                acc = accessions[i].replace("-", "")
                index_url = "https://www.sec.gov/Archives/edgar/data/" + cik_clean + "/" + acc + "/"
                req2 = urllib.request.Request(index_url, headers=EDGAR_HEADERS)
                time.sleep(EDGAR_RATE_LIMIT)
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    html = resp2.read().decode("utf-8", errors="ignore").lower()
                return html[:50000]  # First 50KB should cover business description

    except Exception as e:
        logger.debug(f"[BMA] 10-K text fetch failed: {e}")
    return ""


def _classify_revenue_model(text: str) -> tuple:
    """Returns (model_type, recurring_estimate)."""
    text = text.lower()
    hits = {}
    for model, keywords in REVENUE_MODEL_KEYWORDS.items():
        count = sum(text.count(kw) for kw in keywords)
        if count > 0:
            hits[model] = count

    if not hits:
        return "UNKNOWN", "MEDIUM"

    best_model = max(hits, key=hits.get)

    recurring_map = {
        "SUBSCRIPTION_SAAS": "HIGH",
        "LICENSE_ROYALTY": "HIGH",
        "TRANSACTION_VOLUME": "MEDIUM",
        "PROJECT_BASED": "LOW",
        "PRODUCT_SALES": "LOW",
    }
    return best_model, recurring_map.get(best_model, "MEDIUM")


def run_business_model_analysis(ticker: str) -> dict:
    """
    Classify and assess business model durability.
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "business_model_analysis.json"

    logger.info(f"[BMA] Starting business model analysis for {ticker}")

    # Try yfinance for quick info
    gross_margin_recent = None
    capex_revenue_recent = None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        gross_margin_recent = info.get("grossMargins")
    except Exception:
        pass

    # Get CIK and EDGAR data
    cik = _get_cik(ticker)
    facts = None
    if cik:
        facts = _get_company_facts(cik)

    # Extract financial series
    revenue = {}
    gross_profit = {}
    sga = {}
    capex = {}
    employees = {}

    if facts:
        revenue = _extract_annual_series(facts, "Revenues")
        if not revenue:
            revenue = _extract_annual_series(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
        if not revenue:
            revenue = _extract_annual_series(facts, "SalesRevenueNet")

        gross_profit = _extract_annual_series(facts, "GrossProfit")
        sga = _extract_annual_series(facts, "SellingGeneralAndAdministrativeExpense")
        capex = _extract_annual_series(facts, "PaymentsToAcquirePropertyPlantAndEquipment")
        employees = _extract_annual_series(facts, "EntityNumberOfEmployees")

    # Gross margin calculation
    if gross_margin_recent is None and gross_profit and revenue:
        common = sorted(set(gross_profit.keys()) & set(revenue.keys()))
        if common:
            latest = common[-1]
            rev = revenue[latest]
            gp = gross_profit[latest]
            if rev > 0:
                gross_margin_recent = gp / rev

    # Capex / Revenue
    capex_revenue_series = {}
    if capex and revenue:
        for yr in sorted(set(capex.keys()) & set(revenue.keys())):
            if revenue[yr] > 0:
                capex_revenue_series[yr] = abs(capex[yr]) / revenue[yr]

    if capex_revenue_series:
        recent_yrs = sorted(capex_revenue_series.keys())[-5:]
        capex_revenue_recent = sum(capex_revenue_series[y] for y in recent_yrs) / len(recent_yrs)

    # Capital intensity classification
    if capex_revenue_recent is not None:
        if capex_revenue_recent < 0.03:
            capital_intensity = "LOW"
        elif capex_revenue_recent < 0.10:
            capital_intensity = "MEDIUM"
        else:
            capital_intensity = "HIGH"
    else:
        capital_intensity = "UNKNOWN"

    # Scalability: gross margin > 50% AND capex/revenue < 5%
    scalable_gm = gross_margin_recent is not None and gross_margin_recent > 0.50
    scalable_capex = capex_revenue_recent is not None and capex_revenue_recent < 0.05
    if scalable_gm and scalable_capex:
        scalability = "HIGH"
    elif scalable_gm or scalable_capex:
        scalability = "MEDIUM"
    else:
        scalability = "LOW"

    # SGA rising faster than revenue (CAC signal)
    rising_cac_flag = False
    if sga and revenue and len(revenue) >= 3:
        sga_rev_yrs = sorted(set(sga.keys()) & set(revenue.keys()))[-4:]
        if len(sga_rev_yrs) >= 3:
            sga_ratios = [sga[y] / revenue[y] for y in sga_rev_yrs if revenue[y] > 0]
            if len(sga_ratios) >= 3:
                # Rising SGA/revenue ratio = worsening CAC
                if all(sga_ratios[i] > sga_ratios[i-1] for i in range(1, min(4, len(sga_ratios)))):
                    rising_cac_flag = True

    # Revenue model from 10-K text
    revenue_model_type = "UNKNOWN"
    recurring_revenue_estimate = "MEDIUM"
    if cik:
        text = _get_10k_text_snippet(cik)
        if text:
            revenue_model_type, recurring_revenue_estimate = _classify_revenue_model(text)

    # Business model score (0-100)
    bm_score = 0

    # Recurring revenue component (0-30 pts)
    recurring_pts = {"HIGH": 30, "MEDIUM": 18, "LOW": 6, "UNKNOWN": 12}
    bm_score += recurring_pts.get(recurring_revenue_estimate, 12)

    # Scalability component (0-25 pts)
    scalability_pts = {"HIGH": 25, "MEDIUM": 15, "LOW": 5, "UNKNOWN": 10}
    bm_score += scalability_pts.get(scalability, 10)

    # Pricing power evidence via gross margin (0-25 pts)
    if gross_margin_recent is not None:
        if gross_margin_recent > 0.60:
            bm_score += 25
        elif gross_margin_recent > 0.40:
            bm_score += 18
        elif gross_margin_recent > 0.25:
            bm_score += 10
        else:
            bm_score += 3
    else:
        bm_score += 10

    # Capital intensity inverse (0-20 pts)
    capital_pts = {"LOW": 20, "MEDIUM": 12, "HIGH": 4, "UNKNOWN": 10}
    bm_score += capital_pts.get(capital_intensity, 10)

    # Deductions
    if rising_cac_flag:
        bm_score -= 10

    bm_score = max(0, min(100, bm_score))

    if bm_score >= 80:
        bm_verdict = "EXCEPTIONAL"
    elif bm_score >= 60:
        bm_verdict = "STRONG"
    elif bm_score >= 40:
        bm_verdict = "ADEQUATE"
    else:
        bm_verdict = "WEAK"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "revenue_model_type": revenue_model_type,
        "recurring_revenue_estimate": recurring_revenue_estimate,
        "scalability": scalability,
        "capital_intensity": capital_intensity,
        "gross_margin_recent": round(gross_margin_recent, 4) if gross_margin_recent is not None else None,
        "capex_revenue_avg_5yr": round(capex_revenue_recent, 4) if capex_revenue_recent is not None else None,
        "rising_cac_flag": rising_cac_flag,
        "business_model_score": bm_score,
        "business_model_verdict": bm_verdict,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[BMA] {ticker}: score={bm_score} verdict={bm_verdict} "
        f"model={revenue_model_type} recurring={recurring_revenue_estimate} "
        f"scalability={scalability} capex_intensity={capital_intensity}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSCI"
    r = run_business_model_analysis(ticker)
    print(json.dumps(r, indent=2, default=str))

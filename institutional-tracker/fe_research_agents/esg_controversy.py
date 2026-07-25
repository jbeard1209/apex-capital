#!/usr/bin/env python3
"""
APEX CAPITAL -- ESG CONTROVERSY AGENT
======================================
Screens for ESG controversies and governance failures.
Saves output to data/company_research/{ticker}/esg_controversy.json
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {
    "User-Agent": "ApexCapital research@apexcapital.com",
    "Accept": "application/json",
}
EDGAR_RATE_LIMIT = 0.1  # seconds between requests

CONTROVERSY_TERMS = [
    "environmental violation", "EPA penalty", "OSHA", "workplace safety",
    "discrimination", "harassment", "class action", "product recall",
    "data breach", "cybersecurity incident", "bribery", "corruption",
    "FCPA", "money laundering", "tax evasion", "sanctions violation",
]


def _edgar_get(url: str) -> Optional[dict]:
    """HTTP GET from EDGAR with rate limiting and User-Agent."""
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[ESG] EDGAR fetch failed {url}: {e}")
        return None


def _get_cik(ticker: str) -> Optional[str]:
    """Lookup CIK from EDGAR company tickers JSON."""
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            tickers_data = json.loads(resp.read().decode("utf-8"))
        for key, val in tickers_data.items():
            if val.get("ticker", "").upper() == ticker.upper():
                cik_raw = str(val["cik_str"])
                return cik_raw.zfill(10)
    except Exception as e:
        logger.debug(f"[ESG] CIK lookup failed: {e}")
    return None


def _search_edgar_enforcement(ticker: str, cik: Optional[str]) -> int:
    """Search EDGAR EFTS for enforcement actions and 8-K crisis filings."""
    hits = 0
    query = urllib.parse.quote(ticker)
    two_yr_ago = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Search 8-K filings for controversy terms
    for term in CONTROVERSY_TERMS[:6]:
        encoded = urllib.parse.quote('"' + term + '"')
        url = (
            "https://efts.sec.gov/LATEST/search-index?q="
            + encoded + "%20%22" + query + "%22"
            + "&forms=8-K&dateRange=custom&startdt=" + two_yr_ago + "&enddt=" + today
        )
        data = _edgar_get(url)
        if data:
            count = data.get("hits", {}).get("total", {}).get("value", 0)
            hits += count
            if count > 0:
                logger.info(f"[ESG] {ticker}: Found {count} 8-K hits for '{term}'")

    return hits


def _check_web_controversies(ticker: str) -> list:
    """Search Yahoo RSS for controversy terms related to ticker."""
    flags = []
    try:
        url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=" + ticker + "&region=US&lang=en-US"
        req = urllib.request.Request(url, headers={"User-Agent": "ApexCapital research@apexcapital.com"})
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="ignore").lower()
        for term in CONTROVERSY_TERMS:
            if term.lower() in content:
                flags.append("Yahoo RSS: '" + term + "' found in " + ticker + " headlines")
    except Exception:
        pass
    return flags[:5]


def _analyze_proxy_governance(ticker: str, cik: Optional[str]) -> dict:
    """
    From DEF 14A proxy: check board independence and audit committee qualification.
    """
    result = {
        "board_independence_pct": None,
        "audit_committee_qualified": None,
        "related_party_material": False,
        "proxy_parsed": False,
    }

    if not cik:
        return result

    two_yr_ago = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cik_clean = cik.lstrip("0")

    # Get submissions to find proxy filing
    try:
        subs_url = "https://data.sec.gov/submissions/CIK" + cik + ".json"
        req = urllib.request.Request(subs_url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            subs = json.loads(resp.read().decode("utf-8"))

        filings = subs.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        dates = filings.get("filingDate", [])

        proxy_accession = None
        for i, form in enumerate(forms):
            if form == "DEF 14A" and i < len(dates):
                proxy_accession = accessions[i].replace("-", "")
                break

        if not proxy_accession:
            return result

        # Fetch filing index
        index_url = (
            "https://www.sec.gov/Archives/edgar/data/"
            + cik_clean + "/" + proxy_accession + "/"
        )
        req = urllib.request.Request(index_url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            index_html = resp.read().decode("utf-8", errors="ignore").lower()

        # Count independent director references
        ind_count = index_html.count("independent director")
        total_director = max(index_html.count("director"), 1)
        pct = min(1.0, ind_count / (total_director * 0.5))
        result["board_independence_pct"] = round(pct, 2)

        # Audit committee qualification
        audit_keywords = ["cpa", "certified public accountant", "chief financial officer", "cfo",
                          "accounting", "controller", "treasurer"]
        audit_idx = index_html.find("audit committee")
        if audit_idx >= 0:
            audit_section = index_html[audit_idx:audit_idx + 2000]
            result["audit_committee_qualified"] = any(kw in audit_section for kw in audit_keywords)

        # Related party transactions
        rp_idx = index_html.find("related party")
        if rp_idx >= 0:
            context = index_html[rp_idx:rp_idx + 500]
            if re.search(r'\$[0-9,]+\s*million|material|significant', context):
                result["related_party_material"] = True

        result["proxy_parsed"] = True

    except Exception as e:
        logger.debug(f"[ESG] Proxy parse failed: {e}")

    return result


def run_esg_controversy(ticker: str, cik: Optional[str] = None) -> dict:
    """
    Main ESG controversy screening agent.
    Returns structured ESG assessment.
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "esg_controversy.json"

    logger.info(f"[ESG] Starting ESG controversy screen for {ticker}")

    if not cik:
        cik = _get_cik(ticker)
        logger.info(f"[ESG] {ticker} CIK: {cik}")

    sec_hits = _search_edgar_enforcement(ticker, cik)
    web_flags = _check_web_controversies(ticker)
    governance = _analyze_proxy_governance(ticker, cik)

    board_independence_pct = governance.get("board_independence_pct")
    audit_qualified = governance.get("audit_committee_qualified")
    related_party = governance.get("related_party_material", False)

    controversy_flags = list(web_flags)
    if sec_hits > 5:
        controversy_flags.append("High SEC filing controversy count: " + str(sec_hits) + " hits")
    if board_independence_pct is not None and board_independence_pct < 0.50:
        controversy_flags.append(
            "Board independence below 50% (estimated " + str(round(board_independence_pct * 100)) + "%)")
    if audit_qualified is False:
        controversy_flags.append("Audit committee may lack accounting expertise")
    if related_party:
        controversy_flags.append("Material related party transactions detected in proxy")

    # ESG Score (100 = clean, 0 = severe issues)
    esg_score = 100
    if sec_hits > 20:
        esg_score -= 40
    elif sec_hits > 10:
        esg_score -= 25
    elif sec_hits > 5:
        esg_score -= 15
    elif sec_hits > 0:
        esg_score -= 8

    esg_score -= min(30, len(web_flags) * 8)

    if board_independence_pct is not None and board_independence_pct < 0.50:
        esg_score -= 15
    if audit_qualified is False:
        esg_score -= 10
    if related_party:
        esg_score -= 10

    esg_score = max(0, min(100, esg_score))

    if esg_score >= 80:
        esg_verdict = "CLEAN"
    elif esg_score >= 60:
        esg_verdict = "MINOR_ISSUES"
    elif esg_score >= 40:
        esg_verdict = "SIGNIFICANT_ISSUES"
    else:
        esg_verdict = "RED_FLAG"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "controversy_flags": controversy_flags,
        "sec_enforcement_hits": sec_hits,
        "board_independence_pct": board_independence_pct,
        "audit_committee_qualified": audit_qualified,
        "related_party_material": related_party,
        "proxy_parsed": governance.get("proxy_parsed", False),
        "esg_score": esg_score,
        "esg_verdict": esg_verdict,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[ESG] {ticker}: score={esg_score} verdict={esg_verdict} "
        f"sec_hits={sec_hits} flags={len(controversy_flags)}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSCI"
    r = run_esg_controversy(ticker)
    print(json.dumps(r, indent=2, default=str))

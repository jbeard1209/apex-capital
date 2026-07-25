#!/usr/bin/env python3
"""
APEX CAPITAL -- SUPPLY CHAIN RISK AGENT
=========================================
Supply chain and input cost risk assessment.
Saves output to data/company_research/{ticker}/supply_chain_risk.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
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

COMMODITY_INPUTS = [
    "steel", "aluminum", "copper", "oil", "natural gas", "lithium",
    "rare earth", "cotton", "wheat", "cobalt", "nickel", "palladium",
    "polysilicon", "resin", "chemicals",
]

DISRUPTION_TERMS = ["supply chain", "shortage", "disruption", "delay", "supply constraint"]

SINGLE_SUPPLIER_TERMS = ["sole supplier", "single source", "primary supplier", "sole source"]

CHINA_TERMS = ["china", "chinese supplier", "prc", "shenzhen", "guangdong", "manufactured in china"]

VERTICAL_TERMS = ["vertically integrated", "owns the", "acquired the supplier",
                   "in-house manufacturing", "backward integration"]


def _edgar_get(url: str) -> Optional[dict]:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[SCR] EDGAR fetch failed {url}: {e}")
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
        logger.debug(f"[SCR] CIK lookup failed: {e}")
    return None


def _get_company_facts(cik: str) -> Optional[dict]:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK" + cik + ".json"
    return _edgar_get(url)


def _extract_annual_series(facts: dict, concept: str, namespace: str = "us-gaap") -> dict:
    try:
        units = facts.get("facts", {}).get(namespace, {}).get(concept, {}).get("units", {})
        for unit_key in ["USD", "pure"]:
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


def _get_filing_text(cik: str, form_type: str, max_chars: int = 60000) -> str:
    """Fetch text of most recent filing of given type."""
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

        for i, f in enumerate(forms):
            if f in (form_type, form_type + "/A"):
                acc = accessions[i].replace("-", "")
                index_url = "https://www.sec.gov/Archives/edgar/data/" + cik_clean + "/" + acc + "/"
                req2 = urllib.request.Request(index_url, headers=EDGAR_HEADERS)
                time.sleep(EDGAR_RATE_LIMIT)
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    html = resp2.read().decode("utf-8", errors="ignore").lower()
                return html[:max_chars]

    except Exception as e:
        logger.debug(f"[SCR] Filing text fetch failed: {e}")
    return ""


def _search_8k_disruptions(ticker: str, cik: str) -> list:
    """Search 8-K filings for supply chain disruption terms in last 3 years."""
    import urllib.parse
    three_yr_ago = (datetime.now(timezone.utc) - timedelta(days=1095)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    disruptions_found = []
    for term in DISRUPTION_TERMS[:3]:
        encoded = urllib.parse.quote('"' + term + '"')
        query_ticker = urllib.parse.quote(ticker)
        url = (
            "https://efts.sec.gov/LATEST/search-index?q="
            + encoded + "%20%22" + query_ticker + "%22"
            + "&forms=8-K&dateRange=custom&startdt=" + three_yr_ago + "&enddt=" + today
        )
        data = _edgar_get(url)
        if data:
            count = data.get("hits", {}).get("total", {}).get("value", 0)
            if count > 0:
                disruptions_found.append(
                    "8-K filings mention '" + term + "': " + str(count) + " instances in last 3 years"
                )

    return disruptions_found


def run_supply_chain_risk(ticker: str, cik: Optional[str] = None) -> dict:
    """
    Main supply chain risk assessment agent.
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "supply_chain_risk.json"

    logger.info(f"[SCR] Starting supply chain risk assessment for {ticker}")

    if not cik:
        cik = _get_cik(ticker)
        logger.info(f"[SCR] {ticker} CIK: {cik}")

    # Get EDGAR facts for inventory analysis
    facts = None
    if cik:
        facts = _get_company_facts(cik)

    # Get 10-K text for qualitative analysis
    tenk_text = ""
    if cik:
        tenk_text = _get_filing_text(cik, "10-K")

    # 1. Single supplier dependency
    sole_supplier_flag = any(term in tenk_text for term in SINGLE_SUPPLIER_TERMS)

    # 2. China dependency
    china_dependency = any(term in tenk_text for term in CHINA_TERMS)

    # 3. Commodity exposures
    commodity_exposures = [c for c in COMMODITY_INPUTS if c in tenk_text]

    # 4. Inventory risk: inventory growing faster than revenue
    inventory_build_flag = False
    if facts:
        revenue = _extract_annual_series(facts, "Revenues")
        if not revenue:
            revenue = _extract_annual_series(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
        inventory = _extract_annual_series(facts, "InventoryNet")
        if not inventory:
            inventory = _extract_annual_series(facts, "InventoryFinishedGoods")

        if inventory and revenue and len(inventory) >= 3:
            common_yrs = sorted(set(inventory.keys()) & set(revenue.keys()))[-3:]
            if len(common_yrs) >= 2:
                inv_chg = (inventory[common_yrs[-1]] - inventory[common_yrs[0]]) / max(abs(inventory[common_yrs[0]]), 1)
                rev_chg = (revenue[common_yrs[-1]] - revenue[common_yrs[0]]) / max(abs(revenue[common_yrs[0]]), 1)
                if inv_chg > rev_chg + 0.15 and inv_chg > 0.20:
                    inventory_build_flag = True
                    logger.info(f"[SCR] {ticker}: Inventory build flag -- inventory grew {inv_chg:.1%} vs revenue {rev_chg:.1%}")

    # 5. Supply chain disruption history from 8-Ks
    disruption_history = []
    if cik:
        disruption_history = _search_8k_disruptions(ticker, cik)

    # 6. Vertical integration check
    vertically_integrated = any(term in tenk_text for term in VERTICAL_TERMS)

    # --- Risk Scoring ---
    risk_score = 20  # baseline (low risk)

    if sole_supplier_flag:
        risk_score += 25
    if china_dependency:
        risk_score += 20
    if len(commodity_exposures) > 3:
        risk_score += 15
    elif len(commodity_exposures) > 0:
        risk_score += len(commodity_exposures) * 4
    if inventory_build_flag:
        risk_score += 15
    if len(disruption_history) > 2:
        risk_score += 20
    elif disruption_history:
        risk_score += 10

    # Vertical integration mitigates risk
    if vertically_integrated:
        risk_score -= 10

    risk_score = max(0, min(100, risk_score))

    if risk_score <= 25:
        verdict = "LOW"
    elif risk_score <= 50:
        verdict = "MEDIUM"
    elif risk_score <= 75:
        verdict = "HIGH"
    else:
        verdict = "CRITICAL"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sole_supplier_flag": sole_supplier_flag,
        "china_dependency": china_dependency,
        "commodity_exposures": commodity_exposures,
        "inventory_build_flag": inventory_build_flag,
        "disruption_history": disruption_history,
        "vertically_integrated": vertically_integrated,
        "supply_chain_risk_score": risk_score,
        "supply_chain_verdict": verdict,
        "text_analyzed": len(tenk_text) > 0,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[SCR] {ticker}: score={risk_score} verdict={verdict} "
        f"sole_supplier={sole_supplier_flag} china={china_dependency} "
        f"commodities={len(commodity_exposures)}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSCI"
    r = run_supply_chain_risk(ticker)
    print(json.dumps(r, indent=2, default=str))

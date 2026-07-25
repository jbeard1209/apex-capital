#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RISK RESEARCHER
============================================
Systematic risk assessment from 10-K risk factor sections.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}
SEC_RATE_LIMIT = 0.1

RISK_KEYWORDS = {
    "customer_concentration": [
        "significant customer", "major customer", "10% of revenue",
        "single customer", "concentrated customer", "customer concentration"
    ],
    "regulatory": [
        "regulatory approval", "fda", "sec investigation", "antitrust",
        "gdpr", "compliance", "regulatory scrutiny"
    ],
    "debt_covenant": [
        "covenant", "default", "waiver", "credit facility", "maturity",
        "refinancing risk", "debt maturity"
    ],
    "litigation": [
        "material litigation", "class action", "lawsuit", "legal proceedings",
        "pending litigation", "securities litigation"
    ],
    "technology_disruption": [
        "disruptive", "technological change", "obsolescence",
        "competition from technology", "artificial intelligence", "digitization"
    ],
    "key_person": [
        "key personnel", "depends on", "loss of", "key employee",
        "key executive", "retention of"
    ],
    "macro": [
        "interest rate", "recession", "inflation", "foreign currency",
        "commodity price", "macroeconomic", "economic downturn"
    ],
    "going_concern": [
        "going concern", "substantial doubt", "ability to continue",
        "liquidity concerns"
    ],
}

OFF_BALANCE_KEYWORDS = [
    "operating lease", "off-balance sheet", "guarantee",
    "contingent liability", "purchase obligation"
]


def _fetch_json(url: str, desc: str = "") -> Optional[dict]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[Risk] Failed to fetch {desc or url}: {e}")
        return None


def _fetch_text(url: str, max_chars: int = 80000) -> Optional[str]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.text[:max_chars]
    except Exception as e:
        logger.warning(f"[Risk] Failed to fetch text {url}: {e}")
        return None


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean)


def _get_10k_text(cik: str, max_filings: int = 3) -> list:
    """Fetch last N 10-K filing texts from EDGAR. Returns list of (year, text)."""
    results = []
    try:
        cik_int    = int(cik)
        cik_padded = str(cik).zfill(10)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        subs = _fetch_json(sub_url, f"submissions CIK {cik}")
        if not subs:
            return results

        recent = subs.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        accns  = recent.get("accessionNumber", [])
        dates  = recent.get("filingDate", [])

        ten_k_filings = []
        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A"):
                d = dates[i] if i < len(dates) else ""
                a = accns[i] if i < len(accns) else None
                if a:
                    ten_k_filings.append((d, a))

        ten_k_filings.sort(reverse=True)
        ten_k_filings = ten_k_filings[:max_filings]

        for filing_date, accn in ten_k_filings:
            accn_dashes = accn.replace("-", "")
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_dashes}/index.json"
            idx = _fetch_json(idx_url, "filing index")
            if not idx:
                continue

            items = idx.get("directory", {}).get("item", []) if idx else []
            primary_doc = None
            for item in items:
                name = item.get("name", "")
                if name.endswith(".htm") and "ex" not in name.lower() and "index" not in name.lower():
                    primary_doc = name
                    break

            if not primary_doc:
                continue

            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_dashes}/{primary_doc}"
            text = _fetch_text(doc_url, max_chars=80000)
            if text:
                year = filing_date[:4] if filing_date else "unknown"
                results.append((year, _strip_html(text)))

    except Exception as e:
        logger.warning(f"[Risk] 10-K text fetch error for CIK {cik}: {e}")

    return results


def _find_risk_factors_section(text: str) -> str:
    """Extract the risk factors section from 10-K text."""
    text_lower = text.lower()

    # Common section markers
    start_markers = [
        "item 1a. risk factors",
        "item 1a risk factors",
        "risk factors",
    ]
    end_markers = [
        "item 1b",
        "item 2.",
        "unresolved staff comments",
        "properties",
    ]

    start_pos = -1
    for marker in start_markers:
        pos = text_lower.find(marker)
        if pos != -1:
            start_pos = pos
            break

    if start_pos == -1:
        return text[:20000]  # fallback: first portion

    end_pos = len(text)
    for marker in end_markers:
        pos = text_lower.find(marker, start_pos + 1000)
        if pos != -1:
            end_pos = min(end_pos, pos)

    return text[start_pos:end_pos][:30000]


def _count_kw(text: str, keywords: list) -> int:
    text_lower = text.lower()
    return sum(text_lower.count(kw.lower()) for kw in keywords)


def _count_risk_factors(text: str) -> int:
    """Rough count of individual risk factors (paragraph headers in all caps or numbered)."""
    sections = re.findall(r"\n[A-Z][A-Z\s,]{20,80}\n", text)
    return max(len(sections), text.lower().count("risk") // 3)


def _nonull(lst) -> list:
    return [v for v in (lst or []) if v is not None]


def _avg(lst) -> Optional[float]:
    n = _nonull(lst)
    return sum(n) / len(n) if n else None


def run_risk_research(ticker: str, cik: str = None) -> dict:
    """
    Systematic risk assessment from 10-K risk factor sections.
    """
    ticker = ticker.upper().strip()
    out_dir  = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "risk_assessment.json"

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
    except Exception as e:
        logger.error(f"[Risk] EDGAR fetch failed for {ticker}: {e}")
        result = {"ticker": ticker, "error": str(e), "risk_score": 40,
                  "risk_level": "HIGH", "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    if f:
        if not cik:
            cik = f.get("cik")
    else:
        f = {}

    years = f.get("years", [])
    ltd   = f.get("long_term_debt", [])
    assets = f.get("total_assets", [])
    n = len(years)

    # --- Fetch 10-K texts ---
    filing_texts = []
    if cik:
        try:
            filing_texts = _get_10k_text(cik, max_filings=3)
        except Exception as e:
            logger.warning(f"[Risk] Could not fetch 10-K texts: {e}")

    # --- Risk factor analysis per filing ---
    risk_sections = []
    for year, text in filing_texts:
        section = _find_risk_factors_section(text)
        risk_sections.append((year, section, text))

    # Per-category risk detection (most recent filing)
    category_flags = {}
    risk_details   = {}
    going_concern_flag = False
    customer_concentration_risk = False
    debt_covenant_risk = False
    litigation_risk    = False

    if risk_sections:
        latest_year, latest_risk_text, latest_full_text = risk_sections[0]
        for category, keywords in RISK_KEYWORDS.items():
            count = _count_kw(latest_risk_text, keywords)
            category_flags[category] = count > 0
            if count > 0:
                risk_details[category] = f"Found {count} references in risk factors section"

        going_concern_flag         = category_flags.get("going_concern", False)
        customer_concentration_risk = category_flags.get("customer_concentration", False)
        debt_covenant_risk          = category_flags.get("debt_covenant", False)
        litigation_risk             = category_flags.get("litigation", False)

        # Off-balance sheet items
        off_bs_count = _count_kw(latest_full_text, OFF_BALANCE_KEYWORDS)
        if off_bs_count > 5:
            risk_details["off_balance_sheet"] = f"Found {off_bs_count} references to off-balance sheet items"
    else:
        latest_risk_text = ""
        latest_full_text = ""
        off_bs_count = 0

    # --- New risks vs prior year ---
    new_risks_vs_prior = []
    if len(risk_sections) >= 2:
        prev_year, prev_risk_text, _ = risk_sections[1]
        for category, keywords in RISK_KEYWORDS.items():
            curr_count = _count_kw(latest_risk_text, keywords) if risk_sections else 0
            prev_count = _count_kw(prev_risk_text, keywords)
            if curr_count > 0 and prev_count == 0:
                new_risks_vs_prior.append(
                    f"{category}: NEW risk category detected vs prior year ({prev_year})"
                )
            elif curr_count > prev_count * 1.5 and curr_count > 2:
                new_risks_vs_prior.append(
                    f"{category}: escalating -- {curr_count} refs vs {prev_count} prior year"
                )

    # --- Debt maturity analysis ---
    debt_trend = "UNKNOWN"
    ltd_nn = _nonull(ltd)
    if len(ltd_nn) >= 3:
        if ltd_nn[-1] > ltd_nn[-3] * 1.3:
            debt_trend = "INCREASING"
        elif ltd_nn[-1] < ltd_nn[-3] * 0.8:
            debt_trend = "DECREASING"
        else:
            debt_trend = "STABLE"

    # Search for maturity mentions
    debt_maturity_notes = []
    if latest_full_text:
        # Find "due in 20XX" patterns
        maturity_matches = re.findall(r"due (?:in |on )?(20\d\d)", latest_full_text.lower())
        if maturity_matches:
            unique_years = sorted(set(maturity_matches))
            debt_maturity_notes = [f"Debt maturities mentioned: {', '.join(unique_years)}"]

    # --- Customer/supplier concentration ---
    concentration_customers = []
    if latest_full_text:
        matches = re.findall(
            r"(?:represented?|accounted? for|approximated?)\s+(?:approximately\s+)?(\d+)%\s+(?:of\s+)?(?:our\s+)?(?:net\s+)?(?:revenue|sales)",
            latest_full_text.lower()
        )
        for pct in matches:
            try:
                if int(pct) >= 10:
                    concentration_customers.append(f"Customer represents ~{pct}% of revenue")
            except ValueError:
                pass

    # --- Risk score (0-100, higher = lower risk) ---
    score = 80

    # Penalize for risk categories detected
    high_weight_risks = ["going_concern", "debt_covenant"]
    medium_weight_risks = ["customer_concentration", "litigation", "key_person"]
    low_weight_risks = ["regulatory", "technology_disruption", "macro"]

    for cat in high_weight_risks:
        if category_flags.get(cat):
            score -= 20

    for cat in medium_weight_risks:
        if category_flags.get(cat):
            score -= 10

    for cat in low_weight_risks:
        if category_flags.get(cat):
            score -= 5

    # New risks escalating
    score -= len(new_risks_vs_prior) * 5

    # Debt trend
    if debt_trend == "INCREASING":
        score -= 10

    # Customer concentration > 30%
    for note in concentration_customers:
        pct_match = re.search(r"~?(\d+)%", note)
        if pct_match:
            try:
                pct = int(pct_match.group(1))
                if pct >= 30:
                    score -= 15
                elif pct >= 20:
                    score -= 8
            except ValueError:
                pass

    # Off-balance sheet
    if off_bs_count > 10:
        score -= 10

    score = max(0, min(100, score))

    if score >= 70:
        risk_level = "LOW"
    elif score >= 50:
        risk_level = "MEDIUM"
    elif score >= 30:
        risk_level = "HIGH"
    else:
        risk_level = "CRITICAL"

    # Compile key risks list
    key_risks = []
    for cat, detected in category_flags.items():
        if detected:
            key_risks.append(f"{cat}: {risk_details.get(cat, 'detected')}")

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filings_analyzed": len(filing_texts),
        "risk_score": score,
        "risk_level": risk_level,
        "key_risks": key_risks,
        "category_flags": category_flags,
        "risk_details": risk_details,
        "new_risks_vs_prior_year": new_risks_vs_prior,
        "customer_concentration_risk": customer_concentration_risk,
        "customer_concentration_details": concentration_customers[:5],
        "debt_covenant_risk": debt_covenant_risk,
        "litigation_risk": litigation_risk,
        "going_concern_flag": going_concern_flag,
        "debt_trend": debt_trend,
        "debt_maturity_notes": debt_maturity_notes,
        "off_balance_sheet_references": off_bs_count,
        "notes": list(risk_details.values())[:10],
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Risk] {ticker}: score={score} | level={risk_level} | "
        f"going_concern={going_concern_flag} | new_risks={len(new_risks_vs_prior)}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_risk_research(tk)
        print(json.dumps(r, indent=2, default=str))

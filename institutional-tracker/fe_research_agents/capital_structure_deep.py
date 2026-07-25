#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH AGENT
===========================================
capital_structure_deep.py
Deep capital structure analysis: debt maturity, covenants, off-balance-sheet,
pension obligations, interest coverage, stress test.
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

COVENANT_TERMS = [
    "covenant", "financial covenant", "leverage ratio", "interest coverage ratio",
    "net debt to EBITDA", "minimum liquidity", "default", "waiver",
    "amendment", "cross-default",
]
OBS_TERMS = [
    "operating lease", "off-balance-sheet", "variable interest entity", "VIE",
    "guarantee", "contingent liability", "purchase obligation", "take-or-pay",
]
PENSION_TERMS = [
    "pension", "defined benefit", "postretirement", "OPEB", "actuarial",
    "unfunded", "underfunded",
]


def _edgar_get(url: str) -> Optional[str]:
    try:
        req = Request(url, headers=EDGAR_HEADERS)
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[CapStr] EDGAR GET failed {url}: {e}")
        return None


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _get_cik_submissions(ticker: str, cik: str = None) -> Optional[dict]:
    if not cik:
        time.sleep(RATE_LIMIT)
        url  = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=10-K&dateb=&owner=include&count=5&output=atom"
        data = _edgar_get(url)
        if data:
            m = re.search(r"CIK=(\d+)", data)
            if m:
                cik = m.group(1).zfill(10)
    if not cik:
        return None
    time.sleep(RATE_LIMIT)
    url  = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _edgar_get(url)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def _fetch_latest_filing_text(subs: dict, form: str = "10-K") -> Optional[str]:
    """Fetch most recent filing text for a given form type."""
    recent  = subs.get("filings", {}).get("recent", {})
    forms   = recent.get("form", [])
    accns   = recent.get("accessionNumber", [])
    docs    = recent.get("primaryDocument", [])
    cik_int = int(str(subs.get("cik", "0")).lstrip("0") or "0")

    for i, f in enumerate(forms):
        if f == form:
            accn     = accns[i].replace("-", "")
            doc_name = docs[i] if i < len(docs) else ""
            if doc_name:
                time.sleep(RATE_LIMIT)
                url  = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{doc_name}"
                text = _edgar_get(url)
                if text and len(text) > 5000:
                    return text
            # Try index
            time.sleep(RATE_LIMIT)
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{accn}-index.htm"
            idx     = _edgar_get(idx_url)
            if idx:
                links = re.findall(r'href="([^"]+\.htm[l]?)"', idx, re.IGNORECASE)
                for lnk in links:
                    if "index" not in lnk.lower():
                        doc_url = f"https://www.sec.gov{lnk}" if lnk.startswith("/") else lnk
                        time.sleep(RATE_LIMIT)
                        t = _edgar_get(doc_url)
                        if t and len(t) > 5000:
                            return t
            break
    return None


def _get_company_facts(cik: str) -> Optional[dict]:
    time.sleep(RATE_LIMIT)
    url  = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    data = _edgar_get(url)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def _get_latest_annual_value(facts: dict, concept: str) -> Optional[float]:
    """Get the most recent annual value for an XBRL concept."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    cdata   = us_gaap.get(concept, {})
    if not cdata:
        return None
    units = cdata.get("units", {}).get("USD", [])
    annual = [e for e in units if e.get("form") == "10-K" and e.get("val") is not None]
    if not annual:
        return None
    annual.sort(key=lambda x: x.get("filed", ""), reverse=True)
    return float(annual[0]["val"])


def _extract_maturity_schedule(text: str) -> dict:
    """Build approximate debt maturity schedule from 10-K text."""
    clean    = _clean_html(text[:150000])
    schedule = {}
    current_year = datetime.now().year

    # Patterns: "$XXX million due in 20XX", "maturing in 20XX", etc.
    patterns = [
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|billion)?\s*(?:aggregate principal|principal amount|of notes)?[^.]*?(?:due|mature[sd]?|maturing)[^.]*?(?:in\s+)?(20\d{2})",
        r"(20\d{2})[^.]*?(?:due|mature[sd]?|maturing)[^.]*?\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|billion)?",
        r"(?:due|mature[sd]?|maturing)\s+(?:in\s+)?(20\d{2})[^.]*?\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|billion)?",
    ]
    for pat in patterns:
        matches = re.findall(pat, clean, re.IGNORECASE)
        for m in matches:
            try:
                if len(m) == 2:
                    # Determine which group is amount vs year
                    g0, g1 = m
                    if re.match(r"20\d{2}", g0):
                        year    = int(g0)
                        amt_str = g1.replace(",", "")
                    else:
                        amt_str = g0.replace(",", "")
                        year    = int(g1)
                    if not (current_year <= year <= current_year + 30):
                        continue
                    amt = float(amt_str)
                    # Check for billion
                    context_start = max(0, clean.find(m[0]) - 50)
                    context       = clean[context_start:context_start + 200].lower()
                    if "billion" in context:
                        amt *= 1000  # convert to millions
                    schedule[str(year)] = schedule.get(str(year), 0) + amt
            except Exception:
                continue

    return schedule


def _check_refinancing_cliff(schedule: dict, total_debt: Optional[float]) -> bool:
    """Flag if any single year has >30% of total debt maturing."""
    if not schedule or not total_debt or total_debt <= 0:
        return False
    for yr, amt in schedule.items():
        if amt / (total_debt / 1_000_000) > 0.30:  # total_debt in USD, schedule in millions
            return True
    return False


def _extract_covenant_signals(text: str) -> tuple:
    """Extract covenant-related sentences and flag waivers."""
    clean   = _clean_html(text[:150000])
    signals = []
    waiver  = False

    for term in COVENANT_TERMS:
        pattern = rf"[^.]*{re.escape(term)}[^.]*\."
        matches = re.findall(pattern, clean, re.IGNORECASE)
        for m in matches[:2]:
            snippet = m.strip()[:200]
            if snippet and snippet not in signals:
                signals.append(snippet)
            if "waiver" in m.lower() or "amendment" in m.lower():
                waiver = True
            # Red flag: "default" not preceded by "no" or "not in"
            if "default" in m.lower():
                context = m.lower()
                if not re.search(r"no\s+default|not\s+in\s+default|no\s+event\s+of\s+default", context):
                    waiver = True  # treat as covenant stress

    return signals[:8], waiver


def _extract_obs_items(text: str) -> list:
    """Extract off-balance-sheet exposure descriptions."""
    clean = _clean_html(text[:150000])
    items = []
    seen  = set()

    for term in OBS_TERMS:
        pattern = rf"[^.]*{re.escape(term)}[^.]*\."
        matches = re.findall(pattern, clean, re.IGNORECASE)
        for m in matches[:2]:
            snippet = m.strip()[:200]
            if snippet and snippet not in seen:
                seen.add(snippet)
                items.append(snippet)

    return items[:6]


def _extract_pension_info(text: str) -> tuple:
    """Check for pension/OPEB obligations."""
    clean      = _clean_html(text[:150000])
    clean_lower = clean.lower()
    has_pension = any(t.lower() in clean_lower for t in PENSION_TERMS)
    context     = ""

    if has_pension:
        for term in ["underfunded", "unfunded", "pension obligation", "defined benefit"]:
            m = re.search(rf"[^.]*{re.escape(term)}[^.]*\.", clean, re.IGNORECASE)
            if m:
                context = m.group(0).strip()[:300]
                break

    return has_pension, context


def _calc_interest_coverage(facts: dict) -> tuple:
    """Calculate interest coverage ratio."""
    # EBIT = operating income (or net income + interest + taxes)
    ebit = _get_latest_annual_value(facts, "OperatingIncomeLoss")
    if ebit is None:
        net_income = _get_latest_annual_value(facts, "NetIncomeLoss") or 0
        taxes      = _get_latest_annual_value(facts, "IncomeTaxExpenseBenefit") or 0
        ebit       = net_income + taxes  # rough EBIT without interest

    interest = _get_latest_annual_value(facts, "InterestExpense")
    if interest is None:
        interest = _get_latest_annual_value(facts, "InterestAndDebtExpense")

    if interest and interest > 0 and ebit is not None:
        coverage          = round(ebit / interest, 2)
        stressed_ebit     = ebit * 0.70
        stressed_coverage = round(stressed_ebit / interest, 2)
        return coverage, stressed_coverage, ebit, interest
    return None, None, ebit, interest


def _calc_debt_ebitda(facts: dict) -> tuple:
    """Calculate Debt/EBITDA."""
    ebit  = _get_latest_annual_value(facts, "OperatingIncomeLoss")
    da    = _get_latest_annual_value(facts, "DepreciationDepletionAndAmortization")
    if da is None:
        da = _get_latest_annual_value(facts, "DepreciationAndAmortization") or 0
    ebitda = (ebit or 0) + da if ebit is not None else None

    ltd = _get_latest_annual_value(facts, "LongTermDebt")
    if ltd is None:
        ltd = _get_latest_annual_value(facts, "LongTermDebtAndCapitalLeaseObligations")

    if ebitda and ebitda > 0 and ltd is not None:
        ratio          = round(ltd / ebitda, 2)
        stressed_ebitda = ebitda * 0.70
        stressed_ratio  = round(ltd / stressed_ebitda, 2)
        return ratio, stressed_ratio, ltd, ebitda
    return None, None, ltd, ebitda


def _score_debt_quality(
    coverage: Optional[float],
    coverage_stressed: Optional[float],
    debt_ebitda: Optional[float],
    debt_ebitda_stressed: Optional[float],
    covenant_signals: list,
    waiver: bool,
    schedule: dict,
    cliff_risk: bool,
    obs_items: list,
) -> tuple:
    score = 0

    # Interest coverage
    if coverage is not None:
        if coverage > 5:
            score += 25
        elif coverage >= 3:
            score += 15
        elif coverage >= 1.5:
            score += 5

    # Debt/EBITDA
    if debt_ebitda is not None:
        if debt_ebitda < 1.5:
            score += 25
        elif debt_ebitda <= 3.0:
            score += 15
        elif debt_ebitda <= 4.0:
            score += 5

    # Covenant stress
    if not covenant_signals:
        score += 20
    elif not waiver:
        score += 10
    # waiver = 0 pts

    # Maturity schedule
    if not schedule:
        score += 7  # unknown, give partial credit
    elif not cliff_risk:
        score += 15
    else:
        pass  # cliff risk = 0 pts, apply penalty
        score -= 5

    # Off-balance-sheet
    if not obs_items:
        score += 15
    elif len(obs_items) <= 2:
        score += 7

    score = max(0, min(100, score))

    if score >= 80:
        verdict = "FORTRESS"
    elif score >= 65:
        verdict = "STRONG"
    elif score >= 45:
        verdict = "ADEQUATE"
    elif score >= 25:
        verdict = "STRETCHED"
    else:
        verdict = "DISTRESSED"

    return score, verdict


def run_capital_structure_deep(ticker: str, cik: str = None) -> dict:
    """
    Deep capital structure analysis: debt maturity, covenants, off-balance-sheet,
    pension obligations, interest coverage, stress test.
    """
    ticker  = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "capital_structure_deep.json"

    notes = []

    # Get EDGAR submissions
    subs = _get_cik_submissions(ticker, cik)
    if not subs:
        notes.append("Could not fetch EDGAR submissions -- very limited analysis")

    resolved_cik = None
    if subs:
        resolved_cik = str(subs.get("cik", "")).zfill(10)

    # Company facts for financial metrics
    facts = _get_company_facts(resolved_cik) if resolved_cik else None
    if not facts:
        notes.append("Could not fetch XBRL company facts -- financial ratios unavailable")

    # 10-K text for qualitative analysis
    ten_k_text = _fetch_latest_filing_text(subs) if subs else None
    if not ten_k_text:
        notes.append("10-K text unavailable -- text-based analysis skipped")

    # Also try 10-Q for more recent data
    ten_q_text = _fetch_latest_filing_text(subs, "10-Q") if subs else None

    combined_text = (ten_k_text or "") + " " + (ten_q_text or "")

    # Long-term debt
    ltd = None
    if facts:
        ltd = _get_latest_annual_value(facts, "LongTermDebt")
        if ltd is None:
            ltd = _get_latest_annual_value(facts, "LongTermDebtAndCapitalLeaseObligations")

    # Debt maturity schedule
    schedule    = _extract_maturity_schedule(combined_text) if combined_text.strip() else {}
    cliff_risk  = _check_refinancing_cliff(schedule, ltd)

    if not schedule:
        notes.append("Could not parse debt maturity schedule from filings")

    # Covenant analysis
    covenant_signals, waiver_flag = _extract_covenant_signals(combined_text) if combined_text.strip() else ([], False)
    if waiver_flag:
        notes.append("Covenant waiver or amendment language detected -- potential covenant stress")

    # Off-balance-sheet
    obs_items = _extract_obs_items(combined_text) if combined_text.strip() else []

    # Pension
    pension_flag, pension_context = _extract_pension_info(combined_text) if combined_text.strip() else (False, "")

    # Interest coverage and stress test
    coverage, coverage_stressed, ebit, interest_exp = (None, None, None, None)
    if facts:
        coverage, coverage_stressed, ebit, interest_exp = _calc_interest_coverage(facts)

    # Debt/EBITDA
    debt_ebitda, debt_ebitda_stressed, ltd2, ebitda = (None, None, None, None)
    if facts:
        debt_ebitda, debt_ebitda_stressed, ltd2, ebitda = _calc_debt_ebitda(facts)
        if ltd is None and ltd2 is not None:
            ltd = ltd2

    if coverage is None:
        notes.append("Interest coverage could not be computed -- EBIT or interest expense unavailable in XBRL")
    if debt_ebitda is None:
        notes.append("Debt/EBITDA could not be computed -- EBITDA or LTD unavailable in XBRL")

    # Score
    score, verdict = _score_debt_quality(
        coverage, coverage_stressed, debt_ebitda, debt_ebitda_stressed,
        covenant_signals, waiver_flag, schedule, cliff_risk, obs_items,
    )

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_long_term_debt": ltd,
        "approximate_maturity_schedule": schedule,
        "refinancing_cliff_risk": cliff_risk,
        "covenant_stress_signals": covenant_signals,
        "covenant_waiver_flag": waiver_flag,
        "off_balance_sheet_items": obs_items,
        "pension_flag": pension_flag,
        "pension_context": pension_context,
        "interest_coverage_current": coverage,
        "interest_coverage_stressed": coverage_stressed,
        "debt_ebitda_current": debt_ebitda,
        "debt_ebitda_stressed": debt_ebitda_stressed,
        "debt_quality_score": score,
        "debt_verdict": verdict,
        "notes": notes,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[CapStr] {ticker}: score={score} verdict={verdict} "
        f"coverage={coverage} debt_ebitda={debt_ebitda} cliff={cliff_risk} pension={pension_flag}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_capital_structure_deep(tk)
        print(json.dumps(r, indent=2, default=str))

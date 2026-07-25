#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH AGENT
===========================================
competitive_intelligence.py
Deep competitive landscape analysis using EDGAR filings + web research.
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
from urllib.error import URLError

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}
RATE_LIMIT    = 0.1

CONCERN_WORDS = [
    "intensifying", "increasing competition", "new entrants", "disruptive",
    "significant competition", "pricing pressure", "highly competitive",
    "compete aggressively",
]
POSITIVE_WORDS = [
    "market leader", "dominant", "barriers to entry", "switching costs",
    "consolidated", "leading position", "market share gains", "pricing power",
]
DISRUPT_TERMS = [
    "artificial intelligence", "automation", "technology disruption",
    "digital transformation", "obsolescence", "new technology", "platform shift",
    "generative AI", "machine learning disruption",
]


def _edgar_get(url: str) -> Optional[str]:
    try:
        req = Request(url, headers=EDGAR_HEADERS)
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[CompInt] EDGAR GET failed {url}: {e}")
        return None


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _get_cik_submissions(ticker: str) -> Optional[dict]:
    # Try CIK lookup via search endpoint
    time.sleep(RATE_LIMIT)
    url  = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=10-K&dateb=&owner=include&count=5&output=atom"
    data = _edgar_get(url)
    cik  = None
    if data:
        m = re.search(r"CIK=(\d+)", data)
        if m:
            cik = m.group(1).zfill(10)
    if not cik:
        return None
    time.sleep(RATE_LIMIT)
    url2 = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data2 = _edgar_get(url2)
    if not data2:
        return None
    try:
        return json.loads(data2)
    except Exception:
        return None


def _fetch_10k_text(subs: dict) -> Optional[str]:
    """Fetch the most recent 10-K primary document text."""
    recent = subs.get("filings", {}).get("recent", {})
    forms  = recent.get("form", [])
    accns  = recent.get("accessionNumber", [])
    docs   = recent.get("primaryDocument", [])
    cik    = str(subs.get("cik", "")).zfill(10)

    for i, f in enumerate(forms):
        if f == "10-K":
            accn     = accns[i].replace("-", "")
            doc_name = docs[i] if i < len(docs) else ""
            if doc_name:
                time.sleep(RATE_LIMIT)
                url  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{doc_name}"
                text = _edgar_get(url)
                if text and len(text) > 5000:
                    return text
            # Try index
            time.sleep(RATE_LIMIT)
            idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{accn}-index.htm"
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


def _fetch_10k_texts(subs: dict, max_years: int = 3) -> list:
    """Fetch text of the last N 10-Ks."""
    recent = subs.get("filings", {}).get("recent", {})
    forms  = recent.get("form", [])
    accns  = recent.get("accessionNumber", [])
    docs   = recent.get("primaryDocument", [])
    cik    = str(subs.get("cik", "")).zfill(10)
    texts  = []
    count  = 0

    for i, f in enumerate(forms):
        if f == "10-K" and count < max_years:
            accn     = accns[i].replace("-", "")
            doc_name = docs[i] if i < len(docs) else ""
            if doc_name:
                time.sleep(RATE_LIMIT)
                url  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/{doc_name}"
                text = _edgar_get(url)
                if text and len(text) > 5000:
                    texts.append(text)
                    count += 1
    return texts


def _extract_competitors(text: str) -> list:
    """Extract named competitors from 10-K business section."""
    clean = _clean_html(text)
    competitors = []
    seen = set()

    # Look for "we compete with X, Y, and Z" patterns
    patterns = [
        r"(?:our competitors include|we compete with|competition from|competitors include)\s+([^.]{10,200})",
        r"(?:competitive landscape|compete against)[^.]*?includes?\s+([^.]{10,200})",
        r"(?:principal competitors)[^.]*?are\s+([^.]{10,200})",
    ]
    for pat in patterns:
        m = re.search(pat, clean[:80000], re.IGNORECASE)
        if m:
            snippet = m.group(1)
            # Extract company-name-like tokens (capitalized words/phrases)
            names = re.findall(r"[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,3}", snippet)
            for name in names:
                name = name.strip()
                if len(name) > 3 and name not in seen and name not in {"The", "Our", "These", "Such", "We"}:
                    seen.add(name)
                    competitors.append(name)

    return competitors[:20]


def _extract_market_position(text: str) -> str:
    """Extract market position claim."""
    clean = _clean_html(text[:50000])
    for pos in ["largest", "market leader", "leading provider", "number one",
                 "dominant", "significant player", "among the largest"]:
        if pos in clean.lower():
            m = re.search(rf"([^.]*{re.escape(pos)}[^.]*\.)", clean, re.IGNORECASE)
            if m:
                return m.group(1).strip()[:200]
    return "No explicit market position claim found"


def _detect_competition_trend(texts: list) -> tuple:
    """Check if competitive language is intensifying across 10-Ks."""
    if len(texts) < 2:
        return False, []

    concern_counts = []
    for text in texts:
        clean = _clean_html(text[:80000]).lower()
        count = sum(1 for w in CONCERN_WORDS if w.lower() in clean)
        concern_counts.append(count)

    # Intensifying if latest has more concern words than oldest
    intensifying = concern_counts[0] > concern_counts[-1]

    # New competitors: look for company names in latest not in oldest
    new_competitors = []
    if len(texts) >= 2:
        latest_comps = set(_extract_competitors(texts[0]))
        old_comps    = set(_extract_competitors(texts[-1]))
        new_competitors = list(latest_comps - old_comps)[:5]

    return intensifying, new_competitors


def _extract_customer_concentration(text: str) -> list:
    """Extract customers representing significant revenue share."""
    clean = _clean_html(text[:100000])
    concentrations = []
    seen = set()

    patterns = [
        r"([A-Z][a-zA-Z ]+?)\s+accounted for\s+([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:our\s+)?(?:net\s+)?revenue",
        r"([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:our\s+)?(?:net\s+)?revenue[^.]*?([A-Z][a-zA-Z ]{3,40})",
        r"represented\s+([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:net\s+)?revenue[^.]*?([A-Z][a-zA-Z ]{3,40})",
    ]
    for pat in patterns:
        matches = re.findall(pat, clean, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple) and len(m) == 2:
                try:
                    customer = m[0].strip()[:50] if not m[0].replace(".", "").isdigit() else m[1].strip()[:50]
                    pct_str  = m[1].strip() if m[0].replace(".", "").isdigit() else m[0].strip()
                    # Re-parse
                    num_m = re.search(r"([\d]+(?:\.\d+)?)", pct_str if pct_str.replace(".", "").isdigit() else m[0])
                    if num_m:
                        pct = float(num_m.group(1))
                        if 5 <= pct <= 100 and customer not in seen and len(customer) > 2:
                            seen.add(customer)
                            concentrations.append({"customer": customer, "revenue_pct": pct})
                except Exception:
                    pass

    # Simpler fallback
    simple = re.findall(r"([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:our\s+)?(?:net\s+)?revenue", clean, re.IGNORECASE)
    for pct_str in simple[:5]:
        try:
            pct = float(pct_str)
            if pct >= 10 and not any(c["revenue_pct"] == pct for c in concentrations):
                concentrations.append({"customer": "unnamed customer", "revenue_pct": pct})
        except Exception:
            pass

    return concentrations[:5]


def _extract_supplier_concentration(text: str) -> list:
    """Extract suppliers representing significant cost share."""
    clean = _clean_html(text[:100000])
    suppliers = []
    seen = set()

    patterns = [
        r"([A-Z][a-zA-Z ]+?)\s+accounted for\s+([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:our\s+)?(?:total\s+)?(?:cost|COGS|purchases|supply)",
        r"([\d]+(?:\.\d+)?)\s*%\s*of\s*(?:our\s+)?(?:total\s+)?(?:cost|COGS|purchases)[^.]*?([A-Z][a-zA-Z ]{3,40})",
    ]
    for pat in patterns:
        matches = re.findall(pat, clean, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple) and len(m) == 2:
                try:
                    num_m = re.search(r"([\d]+(?:\.\d+)?)", m[1] if m[0].replace(".", "").isdigit() else m[0])
                    if num_m:
                        pct      = float(num_m.group(1))
                        supplier = (m[1] if m[0].replace(".", "").isdigit() else m[0]).strip()[:50]
                        if 5 <= pct <= 100 and supplier not in seen:
                            seen.add(supplier)
                            suppliers.append({"supplier": supplier, "cogs_pct": pct})
                except Exception:
                    pass

    return suppliers[:5]


def _check_pricing_power(text: str) -> bool:
    """Check for pricing power signals in 10-K."""
    clean = _clean_html(text[:100000]).lower()
    positive_signals = [
        "price increase", "pricing power", "raised prices", "increased prices",
        "pass through", "passthrough", "price realization",
    ]
    return any(sig in clean for sig in positive_signals)


def _check_disruption_risk(text: str) -> list:
    """Find disruption risk terms in 10-K risk factors / MD&A."""
    clean = _clean_html(text[:100000]).lower()
    found = []
    for term in DISRUPT_TERMS:
        if term.lower() in clean:
            found.append(term)
    return found


def _score_competitive_position(
    competitors: list,
    market_pos: str,
    intensifying: bool,
    customer_conc: list,
    pricing_power: bool,
    disruption: list,
    new_competitors: list,
) -> tuple:
    score = 60  # base

    mp_lower = market_pos.lower()
    if "market leader" in mp_lower or "largest" in mp_lower or "dominant" in mp_lower:
        score += 20
    elif "significant" in mp_lower or "leading" in mp_lower:
        score += 10

    if intensifying:
        score -= 10
    if new_competitors:
        score -= 5

    # Customer concentration risk
    high_conc = [c for c in customer_conc if c["revenue_pct"] >= 20]
    if high_conc:
        score -= 15
    elif [c for c in customer_conc if c["revenue_pct"] >= 10]:
        score -= 7

    if pricing_power:
        score += 10

    if len(disruption) >= 3:
        disruption_risk = "HIGH"
        score -= 20
    elif len(disruption) >= 1:
        disruption_risk = "MEDIUM"
        score -= 8
    else:
        disruption_risk = "LOW"

    score = max(0, min(100, score))

    if score >= 80:
        verdict = "DOMINANT"
    elif score >= 65:
        verdict = "STRONG"
    elif score >= 50:
        verdict = "ADEQUATE"
    elif score >= 35:
        verdict = "WEAK"
    else:
        verdict = "AT_RISK"

    return score, verdict, disruption_risk


def run_competitive_intelligence(ticker: str, cik: str = None) -> dict:
    """
    Deep competitive landscape analysis using EDGAR filings + web research.
    """
    ticker  = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "competitive_intelligence.json"

    notes = []

    # Fetch EDGAR submissions
    subs = _get_cik_submissions(ticker)
    if not subs:
        notes.append("Could not fetch EDGAR submissions -- limited analysis")

    # Fetch 10-K texts (last 3)
    texts = []
    if subs:
        texts = _fetch_10k_texts(subs, max_years=3)
        if not texts:
            notes.append("Could not fetch 10-K text")

    latest_text = texts[0] if texts else ""

    # Extract data
    named_competitors     = _extract_competitors(latest_text) if latest_text else []
    market_pos            = _extract_market_position(latest_text) if latest_text else "No data"
    intensifying, new_comps = _detect_competition_trend(texts) if len(texts) >= 2 else (False, [])
    customer_conc         = _extract_customer_concentration(latest_text) if latest_text else []
    supplier_conc         = _extract_supplier_concentration(latest_text) if latest_text else []
    pricing_power         = _check_pricing_power(latest_text) if latest_text else False
    disruption_terms      = _check_disruption_risk(latest_text) if latest_text else []

    if not latest_text:
        notes.append("No 10-K text available -- all fields defaulted")

    score, verdict, disruption_risk = _score_competitive_position(
        named_competitors, market_pos, intensifying, customer_conc,
        pricing_power, disruption_terms, new_comps,
    )

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "named_competitors": named_competitors,
        "market_position_claim": market_pos,
        "competition_intensifying": intensifying,
        "new_competitors_vs_prior_year": new_comps,
        "customer_concentration": customer_conc,
        "supplier_concentration": supplier_conc,
        "pricing_power_confirmed": pricing_power,
        "disruption_risk_terms": disruption_terms,
        "disruption_risk": disruption_risk,
        "competitive_position_score": score,
        "competitive_verdict": verdict,
        "notes": notes,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[CompInt] {ticker}: score={score} verdict={verdict} "
        f"competitors={len(named_competitors)} disruption={disruption_risk}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_competitive_intelligence(tk)
        print(json.dumps(r, indent=2, default=str))

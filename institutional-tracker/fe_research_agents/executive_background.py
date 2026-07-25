#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH AGENT
===========================================
executive_background.py
Deep background research on CEO, CFO, and other named C-suite executives.
Sources: DEF 14A proxy (EDGAR), SEC enforcement database, web search patterns.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}
RATE_LIMIT    = 0.1  # seconds between EDGAR calls

RED_FLAG_TERMS = [
    "resign", "fired", "investigation", "fraud", "lawsuit", "indicted",
    "arrested", "misconduct", "SEC charges", "DOJ", "criminal", "embezzl",
    "restated", "restatement", "accounting irregularity",
]

COMP_ALIGNMENT_GOOD  = ["ROIC", "return on invested capital", "free cash flow", "FCF",
                         "economic profit", "intrinsic value"]
COMP_ALIGNMENT_OKAY  = ["EPS", "earnings per share", "net income", "EBITDA",
                         "revenue growth", "operating income"]


def _edgar_get(url: str) -> Optional[str]:
    try:
        req = Request(url, headers=EDGAR_HEADERS)
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[ExecBg] EDGAR GET failed {url}: {e}")
        return None


def _get_cik_from_ticker(ticker: str) -> Optional[str]:
    time.sleep(RATE_LIMIT)
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=&CIK={ticker}&type=DEF+14A&dateb=&owner=include&count=5&search_text=&output=atom"
    data = _edgar_get(url)
    if not data:
        return None
    m = re.search(r"CIK=(\d+)", data)
    if m:
        return m.group(1).zfill(10)
    return None


def _get_cik_submissions(cik: str) -> Optional[dict]:
    time.sleep(RATE_LIMIT)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _edgar_get(url)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def _fetch_proxy_text(cik: str) -> Optional[str]:
    """Fetch most recent DEF 14A proxy document text."""
    subs = _get_cik_submissions(cik)
    if not subs:
        return None

    # Look in recent filings
    recent = subs.get("filings", {}).get("recent", {})
    forms  = recent.get("form", [])
    accns  = recent.get("accessionNumber", [])
    docs   = recent.get("primaryDocument", [])

    proxy_accn = None
    proxy_doc  = None
    for i, f in enumerate(forms):
        if f in ("DEF 14A", "DEF14A"):
            proxy_accn = accns[i].replace("-", "")
            proxy_doc  = docs[i] if i < len(docs) else ""
            break

    if not proxy_accn:
        # Try EFTS search
        three_yr_ago = (datetime.now() - timedelta(days=1095)).strftime("%Y-%m-%d")
        today        = datetime.now().strftime("%Y-%m-%d")
        time.sleep(RATE_LIMIT)
        url  = (f"https://efts.sec.gov/LATEST/search-index?q=%22{cik}%22"
                f"&forms=DEF+14A&dateRange=custom&startdt={three_yr_ago}&enddt={today}")
        data = _edgar_get(url)
        if data:
            m = re.search(r'"_id"\s*:\s*"([^"]+)"', data)
            if m:
                proxy_accn = m.group(1).replace("-", "")

    if not proxy_accn:
        return None

    time.sleep(RATE_LIMIT)
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{proxy_accn}/{proxy_accn}-index.htm"
    idx     = _edgar_get(idx_url)
    if not idx and proxy_doc:
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{proxy_accn}/{proxy_doc}"
        time.sleep(RATE_LIMIT)
        return _edgar_get(doc_url)

    # Try to find primary doc from index
    if idx:
        links = re.findall(r'href="([^"]+\.htm[l]?)"', idx, re.IGNORECASE)
        for lnk in links:
            if "index" not in lnk.lower():
                doc_url = f"https://www.sec.gov{lnk}" if lnk.startswith("/") else lnk
                time.sleep(RATE_LIMIT)
                text = _edgar_get(doc_url)
                if text and len(text) > 5000:
                    return text

    if proxy_doc:
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{proxy_accn}/{proxy_doc}"
        time.sleep(RATE_LIMIT)
        return _edgar_get(doc_url)

    return None


def _clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_executives_from_proxy(text: str) -> list:
    """Extract executive names and titles from proxy text."""
    text = _clean_html(text)
    executives = []
    seen_names = set()

    TITLE_PATTERNS = [
        r"(Chief Executive Officer|CEO)",
        r"(Chief Financial Officer|CFO)",
        r"(Chief Operating Officer|COO)",
        r"(Chief Technology Officer|CTO)",
        r"(Chief Legal Officer|CLO|General Counsel)",
        r"(Chief Marketing Officer|CMO)",
        r"(Chief Administrative Officer|CAO)",
        r"(President)",
        r"(Executive Vice President|EVP)",
        r"(Senior Vice President|SVP)",
    ]

    # Look for name + title patterns
    for title_pat in TITLE_PATTERNS:
        # Pattern: "Name, Age XX, Title" or "Name serves as Title" or "Name, Title"
        patterns = [
            rf"([A-Z][a-z]+ (?:[A-Z]\. )?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)[,\s]+(?:age\s+\d+[,\s]+)?{title_pat}",
            rf"{title_pat}[,\s]+([A-Z][a-z]+ (?:[A-Z]\. )?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        ]
        for pat in patterns:
            matches = re.findall(pat, text[:50000], re.IGNORECASE)
            for m in matches:
                if isinstance(m, tuple):
                    name  = m[0] if len(m[0]) > 3 else m[1]
                    title = m[1] if len(m[0]) > 3 else m[0]
                else:
                    name  = m
                    title = title_pat.strip("()").split("|")[0]
                name = name.strip()
                if len(name) < 4 or name in seen_names:
                    continue
                # filter out false positives
                if any(w in name.lower() for w in ["the ", "this ", "our ", "his ", "her "]):
                    continue
                seen_names.add(name)

                # Age
                age = None
                age_m = re.search(rf"{re.escape(name)}[^.]*?age\s+(\d{{2}})", text[:50000], re.IGNORECASE)
                if age_m:
                    age = int(age_m.group(1))

                # Tenure -- look for "served since YYYY" or "joined in YYYY"
                tenure_years = None
                tenure_m = re.search(
                    rf"{re.escape(name)}[^.]*?(?:served since|has served since|joined in|joined as[^.]*?in)\s+(\d{{4}})",
                    text[:50000], re.IGNORECASE)
                if tenure_m:
                    year = int(tenure_m.group(1))
                    tenure_years = round((datetime.now().year - year), 1)

                # Prior employers
                prior_employers = []
                prior_m = re.search(
                    rf"{re.escape(name)}[^.]*?(?:previously|prior to|before joining|formerly)[^.]*?at\s+([A-Z][^\.,;]+)",
                    text[:50000], re.IGNORECASE)
                if prior_m:
                    prior_employers.append(prior_m.group(1).strip()[:60])

                executives.append({
                    "name": name,
                    "title": title.strip(),
                    "age": age,
                    "tenure_years": tenure_years,
                    "prior_employers": prior_employers,
                    "sec_enforcement_hits": 0,
                    "enforcement_details": [],
                    "red_flag_headlines": [],
                    "ownership_pct": None,
                    "compensation_usd": None,
                    "comp_alignment": "UNKNOWN",
                })

    return executives[:10]


def _check_sec_enforcement(name: str) -> tuple:
    """Search EDGAR for enforcement actions involving an executive."""
    hits = 0
    details = []
    encoded = name.replace(" ", "+")
    time.sleep(RATE_LIMIT)
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{encoded}%22+%22cease+and+desist%22"
    data = _edgar_get(url)
    if data:
        try:
            j = json.loads(data)
            total = j.get("hits", {}).get("total", {}).get("value", 0)
            if total > 0:
                hits += total
                details.append(f"cease-and-desist search: {total} hits")
        except Exception:
            if '"hits"' in data and '"total"' in data:
                m = re.search(r'"value"\s*:\s*(\d+)', data)
                if m and int(m.group(1)) > 0:
                    hits += int(m.group(1))
                    details.append(f"cease-and-desist search: {m.group(1)} hits")
    return hits, details


def _scan_rss_red_flags(ticker: str, name: str) -> list:
    """Scan Yahoo Finance RSS for red-flag headlines about the executive."""
    flagged = []
    urls = [
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    ]
    for url in urls:
        try:
            req = Request(url, headers={"User-Agent": "ApexCapital research@apexcapital.com"})
            with urlopen(req, timeout=10) as r:
                rss = r.read().decode("utf-8", errors="replace")
            items = re.findall(r"<item>(.*?)</item>", rss, re.DOTALL)
            for item in items:
                title_m = re.search(r"<title><!\[CDATA\[([^\]]+)\]\]></title>", item)
                if not title_m:
                    title_m = re.search(r"<title>([^<]+)</title>", item)
                if not title_m:
                    continue
                headline = _clean_html(title_m.group(1))
                headline_lower = headline.lower()
                for term in RED_FLAG_TERMS:
                    if term.lower() in headline_lower:
                        flagged.append(headline[:200])
                        break
        except Exception as e:
            logger.debug(f"[ExecBg] RSS fetch failed for {url}: {e}")
    return flagged[:10]


def _parse_compensation(text: str, exec_name: str) -> tuple:
    """Extract total compensation and alignment signal from proxy text."""
    comp_usd = None
    alignment = "UNKNOWN"

    # Look for compensation table: name followed by dollar amounts
    name_short = exec_name.split()[-1]  # last name
    comp_m = re.search(
        rf"{re.escape(name_short)}[^$]*?\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|thousand)?",
        text[:100000], re.IGNORECASE)
    if comp_m:
        raw = comp_m.group(1).replace(",", "")
        try:
            val = float(raw)
            if "million" in comp_m.group(0).lower():
                val *= 1_000_000
            elif "thousand" in comp_m.group(0).lower():
                val *= 1_000
            elif val < 1000:
                val *= 1_000_000  # assume millions in table
            comp_usd = val
        except Exception:
            pass

    # Alignment check
    text_lower = text[:100000].lower()
    good_hits = sum(1 for w in COMP_ALIGNMENT_GOOD if w.lower() in text_lower)
    okay_hits = sum(1 for w in COMP_ALIGNMENT_OKAY if w.lower() in text_lower)
    if good_hits >= 2:
        alignment = "HIGH"
    elif okay_hits >= 2:
        alignment = "MEDIUM"
    else:
        alignment = "LOW"

    return comp_usd, alignment


def _parse_ownership(text: str, exec_name: str) -> Optional[float]:
    """Extract ownership percentage from proxy security ownership table."""
    name_short = exec_name.split()[-1]
    pct_m = re.search(
        rf"{re.escape(name_short)}[^%]*?([\d]+\.?\d*)\s*%",
        text[:100000], re.IGNORECASE)
    if pct_m:
        try:
            return float(pct_m.group(1))
        except Exception:
            pass
    return None


def run_executive_background(ticker: str, cik: str = None) -> dict:
    """
    Deep background research on CEO, CFO, and other named C-suite executives.
    Sources: DEF 14A proxy (EDGAR), SEC enforcement database, web search patterns.
    """
    ticker  = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "executive_background.json"

    notes = []

    # Resolve CIK
    if not cik:
        cik = _get_cik_from_ticker(ticker)
    if cik:
        cik = str(cik).zfill(10)

    if not cik:
        notes.append("Could not resolve CIK from ticker -- limited EDGAR access")

    # Fetch proxy text
    proxy_text = None
    if cik:
        proxy_text = _fetch_proxy_text(cik)
        if not proxy_text:
            notes.append("DEF 14A proxy not found or could not be fetched")
    else:
        notes.append("Skipping EDGAR proxy fetch -- no CIK")

    # Parse executives
    executives = []
    if proxy_text:
        executives = _parse_executives_from_proxy(proxy_text)
        if not executives:
            notes.append("Could not parse executive roster from proxy text")
    else:
        notes.append("No proxy text available -- executive roster empty")

    # Enrich each executive
    team_red_flags = []
    team_sec_actions = 0
    ceo_name = None
    ceo_tenure_years = None
    ceo_ownership_pct = None
    ceo_comp_usd = None
    ceo_comp_to_fcf_ratio = None

    for exec_rec in executives:
        name  = exec_rec["name"]
        title = exec_rec["title"]

        # Detect CEO/CFO
        is_ceo = bool(re.search(r"chief executive|ceo\b|president", title, re.IGNORECASE))
        is_cfo = bool(re.search(r"chief financial|cfo\b", title, re.IGNORECASE))

        if is_ceo and not ceo_name:
            ceo_name = name
            ceo_tenure_years = exec_rec.get("tenure_years")

        # SEC enforcement check (CEO + CFO mandatory; others sampled)
        if is_ceo or is_cfo:
            hits, details = _check_sec_enforcement(name)
            exec_rec["sec_enforcement_hits"] = hits
            exec_rec["enforcement_details"]  = details
            team_sec_actions += hits
            if hits > 0:
                team_red_flags.append(f"SEC enforcement hits for {name}: {hits}")

        # RSS red flag scan (CEO + CFO)
        if is_ceo or is_cfo:
            flags = _scan_rss_red_flags(ticker, name)
            exec_rec["red_flag_headlines"] = flags
            if flags:
                team_red_flags.extend([f"{name}: {h[:100]}" for h in flags[:3]])

        # Compensation
        if proxy_text:
            comp_usd, alignment = _parse_compensation(proxy_text, name)
            exec_rec["compensation_usd"]  = comp_usd
            exec_rec["comp_alignment"]    = alignment
            if is_ceo:
                ceo_comp_usd = comp_usd

        # Ownership
        if proxy_text:
            own_pct = _parse_ownership(proxy_text, name)
            exec_rec["ownership_pct"] = own_pct
            if is_ceo:
                ceo_ownership_pct = own_pct

    # CEO ownership signal
    if ceo_ownership_pct is not None:
        if ceo_ownership_pct < 0.1:
            team_red_flags.append(f"Weak CEO ownership: {ceo_ownership_pct:.2f}%")
        elif ceo_ownership_pct >= 1.0:
            notes.append(f"Strong CEO ownership: {ceo_ownership_pct:.2f}%")

    # Management integrity score
    score = 70  # base

    if team_sec_actions > 0:
        score -= min(40, team_sec_actions * 15)
    if len(team_red_flags) > 3:
        score -= 15
    elif len(team_red_flags) > 0:
        score -= 5

    if ceo_tenure_years is not None:
        if ceo_tenure_years >= 7:
            score += 10
        elif ceo_tenure_years < 2:
            score -= 5

    if ceo_ownership_pct is not None:
        if ceo_ownership_pct >= 1.0:
            score += 10
        elif ceo_ownership_pct < 0.1:
            score -= 10

    # Comp alignment
    alignments = [e.get("comp_alignment", "UNKNOWN") for e in executives if e.get("comp_alignment") != "UNKNOWN"]
    if alignments:
        high_cnt = alignments.count("HIGH")
        low_cnt  = alignments.count("LOW")
        if high_cnt > low_cnt:
            overall_comp_alignment = "HIGH"
            score += 5
        elif low_cnt > high_cnt:
            overall_comp_alignment = "LOW"
            score -= 10
        else:
            overall_comp_alignment = "MEDIUM"
    else:
        overall_comp_alignment = "UNKNOWN"

    if not executives:
        score = 50
        notes.append("Score defaulted to 50 due to missing executive data")

    score = max(0, min(100, score))

    if score >= 80:
        integrity_verdict = "EXCELLENT"
    elif score >= 65:
        integrity_verdict = "GOOD"
    elif score >= 50:
        integrity_verdict = "FAIR"
    elif score >= 30:
        integrity_verdict = "POOR"
    else:
        integrity_verdict = "RED_FLAG"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executives": executives,
        "ceo_name": ceo_name,
        "ceo_tenure_years": ceo_tenure_years,
        "ceo_ownership_pct": ceo_ownership_pct,
        "ceo_comp_to_fcf_ratio": ceo_comp_to_fcf_ratio,
        "team_red_flags": team_red_flags,
        "team_sec_actions": team_sec_actions,
        "compensation_alignment": overall_comp_alignment,
        "management_integrity_score": score,
        "management_integrity_verdict": integrity_verdict,
        "notes": notes,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[ExecBg] {ticker}: score={score} verdict={integrity_verdict} "
        f"ceo={ceo_name} execs={len(executives)} flags={len(team_red_flags)}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_executive_background(tk)
        print(json.dumps({k: v for k, v in r.items() if k != "executives"}, indent=2, default=str))

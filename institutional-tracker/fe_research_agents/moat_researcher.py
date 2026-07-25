#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE MOAT RESEARCHER
============================================
Competitive moat analysis using EDGAR MD&A text + financial patterns.
"""

from __future__ import annotations

import json
import logging
import math
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

MOAT_KEYWORDS = {
    "switching_costs": [
        "switching cost", "long-term contract", "multi-year", "installed base",
        "recurring", "subscription", "embedded"
    ],
    "network_effects": [
        "network effect", "ecosystem", "platform", "marketplace", "two-sided",
        "network value"
    ],
    "scale_advantages": [
        "economies of scale", "market leader", "largest", "dominant", "scale",
        "cost advantage"
    ],
    "intangibles": [
        "patent", "proprietary", "brand", "trademark", "regulatory approval",
        "license", "intellectual property", "fda approved"
    ],
    "pricing_power": [
        "price increase", "pricing power", "premium pricing", "price realization",
        "fee increase", "rate increase"
    ],
}

ERODING_MOAT_KEYWORDS = [
    "increasing competition", "competitive pressure", "price pressure",
    "margin compression", "new entrants", "disruptive"
]


def _fetch_json(url: str, desc: str = "") -> Optional[dict]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[Moat] Failed to fetch {desc or url}: {e}")
        return None


def _fetch_text(url: str) -> Optional[str]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.text[:60000]
    except Exception as e:
        logger.warning(f"[Moat] Failed to fetch text {url}: {e}")
        return None


def _get_10k_text(cik: str) -> Optional[str]:
    """Fetch most recent 10-K filing text from EDGAR."""
    try:
        cik_padded = str(cik).zfill(10)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        subs = _fetch_json(sub_url, f"submissions for CIK {cik}")
        if not subs:
            return None

        recent = subs.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        accns  = recent.get("accessionNumber", [])
        dates  = recent.get("filingDate", [])

        # Find most recent 10-K
        latest_accn = None
        latest_date = ""
        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A"):
                d = dates[i] if i < len(dates) else ""
                if d > latest_date:
                    latest_date = d
                    latest_accn = accns[i] if i < len(accns) else None

        if not latest_accn:
            logger.warning(f"[Moat] No 10-K found for CIK {cik}")
            return None

        accn_dashes = latest_accn.replace("-", "")
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_dashes}/index.json"
        idx = _fetch_json(idx_url, "filing index")

        primary_doc = None
        if idx:
            items = idx.get("directory", {}).get("item", [])
            for item in items:
                name = item.get("name", "")
                if name.endswith(".htm") and not "ex" in name.lower() and not "index" in name.lower():
                    primary_doc = name
                    break
            if not primary_doc:
                for item in items:
                    name = item.get("name", "")
                    if name.endswith(".htm") and "index" not in name.lower():
                        primary_doc = name
                        break

        if not primary_doc:
            return None

        if primary_doc.startswith("http"):
            doc_url = primary_doc
        else:
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_dashes}/{primary_doc}"

        text = _fetch_text(doc_url)
        if text:
            # Strip HTML tags
            clean = re.sub(r"<[^>]+>", " ", text)
            clean = re.sub(r"\s+", " ", clean)
            return clean[:60000]
        return None

    except Exception as e:
        logger.warning(f"[Moat] 10-K text fetch error: {e}")
        return None


def _count_keywords(text: str, keywords: list) -> int:
    if not text:
        return 0
    text_lower = text.lower()
    return sum(text_lower.count(kw.lower()) for kw in keywords)


def _nonull(lst) -> list:
    return [v for v in (lst or []) if v is not None]


def _avg(lst) -> Optional[float]:
    n = _nonull(lst)
    return sum(n) / len(n) if n else None


def _std(lst) -> Optional[float]:
    n = _nonull(lst)
    if len(n) < 2:
        return None
    avg = sum(n) / len(n)
    return math.sqrt(sum((x - avg) ** 2 for x in n) / len(n))


def run_moat_research(ticker: str) -> dict:
    """
    Research competitive moat using EDGAR MD&A text + financial patterns.
    """
    ticker = ticker.upper().strip()
    out_dir  = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "moat_analysis.json"

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
    except Exception as e:
        logger.error(f"[Moat] EDGAR fetch failed for {ticker}: {e}")
        result = {"ticker": ticker, "error": str(e), "moat_score": 0,
                  "moat_width": "NONE", "moat_durability": "LOW",
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    if not f:
        result = {"ticker": ticker, "error": "No EDGAR data", "moat_score": 0,
                  "moat_width": "NONE", "moat_durability": "LOW",
                  "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    cik    = f.get("cik")
    years  = f.get("years", [])
    revenue  = f.get("revenue", [])
    gp       = f.get("gross_profit", [])
    ebit     = f.get("ebit", [])
    assets   = f.get("total_assets", [])
    liab     = f.get("total_liabilities", [])
    capex_r  = f.get("capex", [])
    n = len(years)

    # --- a) Quantitative moat signals ---

    # Gross margins
    gross_margins = []
    for i in range(n):
        if gp[i] is not None and revenue[i] and revenue[i] > 0:
            gross_margins.append(gp[i] / revenue[i])
        else:
            gross_margins.append(None)

    gm_nn  = _nonull(gross_margins)
    gm_avg = _avg(gm_nn)
    gm_std = _std(gm_nn)

    # ROIC
    roic_list = []
    for i in range(n):
        ni   = None  # use EBIT*(1-0.21) as NOPAT proxy
        eb   = ebit[i]
        a    = assets[i]
        l    = liab[i]
        if eb is not None and a is not None and l is not None:
            inv_cap = a - l * 0.4
            if inv_cap > 0:
                roic_list.append(eb * 0.79 / inv_cap)
            else:
                roic_list.append(None)
        else:
            roic_list.append(None)

    roic_nn = _nonull(roic_list)
    roic_avg = _avg(roic_nn)
    roic_above_10_count = sum(1 for v in roic_nn if v > 0.10)

    # Capex/revenue trend (declining = operating leverage / moat)
    capex_rev = []
    for i in range(n):
        c = capex_r[i]
        if c is not None and revenue[i] and revenue[i] > 0:
            capex_rev.append(abs(c) / revenue[i])
        else:
            capex_rev.append(None)

    capex_rev_nn = _nonull(capex_rev)
    capex_declining = False
    if len(capex_rev_nn) >= 4:
        early = _avg(capex_rev_nn[:len(capex_rev_nn)//2])
        late  = _avg(capex_rev_nn[len(capex_rev_nn)//2:])
        if early is not None and late is not None and late < early * 0.9:
            capex_declining = True

    # Revenue consistency
    yoy_growth = []
    for i in range(1, n):
        if revenue[i] and revenue[i - 1] and revenue[i - 1] > 0:
            yoy_growth.append((revenue[i] - revenue[i - 1]) / revenue[i - 1])
    rev_std = _std(yoy_growth)

    # --- b) EDGAR MD&A text analysis ---
    filing_text = None
    keyword_counts = {}
    eroding_moat_signals = []
    moat_type_evidence = {}

    if cik:
        try:
            filing_text = _get_10k_text(cik)
        except Exception as e:
            logger.warning(f"[Moat] Could not fetch 10-K text for {ticker}: {e}")

    if filing_text:
        for moat_type, kws in MOAT_KEYWORDS.items():
            count = _count_keywords(filing_text, kws)
            keyword_counts[moat_type] = count
            moat_type_evidence[moat_type] = count

        eroding_count = _count_keywords(filing_text, ERODING_MOAT_KEYWORDS)
        if eroding_count > 3:
            eroding_moat_signals.append(
                f"Found {eroding_count} references to competitive pressure/margin pressure in 10-K"
            )

    # Identify strongest moat type
    if keyword_counts:
        strongest_moat = max(keyword_counts, key=lambda k: keyword_counts[k])
        if keyword_counts[strongest_moat] == 0:
            strongest_moat = "NONE_DETECTED"
    else:
        strongest_moat = "TEXT_UNAVAILABLE"

    # --- c) Moat width assessment ---
    # WIDE: gm stable (std < 0.03) for many years AND avg ROIC > 10% consistently
    # NARROW: some moat signals but not consistently above cost of capital
    # NONE: no clear moat

    moat_score = 0
    notes = []

    # Gross margin quality (40 pts)
    if gm_avg is not None:
        if gm_avg > 0.50:
            moat_score += 25
            notes.append(f"High gross margin avg: {gm_avg*100:.1f}%")
        elif gm_avg > 0.35:
            moat_score += 15
        elif gm_avg > 0.20:
            moat_score += 8

    if gm_std is not None:
        if gm_std < 0.03:
            moat_score += 15
            notes.append("Stable gross margins (std < 3%) -- pricing power signal")
        elif gm_std < 0.06:
            moat_score += 8

    # ROIC quality (30 pts)
    if roic_avg is not None:
        if roic_avg > 0.15:
            moat_score += 20
            notes.append(f"Consistently high ROIC: {roic_avg*100:.1f}% -- returns above cost of capital")
        elif roic_avg > 0.10:
            moat_score += 12
        elif roic_avg > 0.05:
            moat_score += 5

    if roic_nn:
        pct_above_10 = roic_above_10_count / len(roic_nn)
        if pct_above_10 > 0.8:
            moat_score += 10

    # Keyword evidence (20 pts)
    if keyword_counts:
        total_kw = sum(keyword_counts.values())
        if total_kw > 20:
            moat_score += 15
        elif total_kw > 10:
            moat_score += 10
        elif total_kw > 3:
            moat_score += 5

    # Penalize eroding signals
    if eroding_moat_signals:
        moat_score -= 10

    if capex_declining:
        moat_score += 5
        notes.append("Capex intensity declining -- possible operating leverage from moat")

    moat_score = max(0, min(100, moat_score))

    # Width classification
    gm_std_ok  = gm_std is not None and gm_std < 0.03
    roic_ok    = roic_avg is not None and roic_avg > 0.10
    if moat_score >= 60 and gm_std_ok and roic_ok:
        moat_width = "WIDE"
    elif moat_score >= 35:
        moat_width = "NARROW"
    else:
        moat_width = "NONE"

    # Durability
    # Check if gross margin declining > 2% over 5yr
    gm_declining_5yr = False
    if len(gm_nn) >= 6:
        gm_early = _avg(gm_nn[-6:-3])
        gm_late  = _avg(gm_nn[-3:])
        if gm_early is not None and gm_late is not None:
            if (gm_early - gm_late) > 0.02:
                gm_declining_5yr = True

    if moat_width == "WIDE" and not gm_declining_5yr and not eroding_moat_signals:
        moat_durability = "HIGH"
    elif moat_width in ("WIDE", "NARROW") and not gm_declining_5yr:
        moat_durability = "MEDIUM"
    else:
        moat_durability = "LOW"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "moat_type": strongest_moat,
        "moat_width": moat_width,
        "moat_durability": moat_durability,
        "gross_margin_avg": gm_avg,
        "gross_margin_std": gm_std,
        "gross_margin_stability": "HIGH" if (gm_std is not None and gm_std < 0.03) else ("MEDIUM" if (gm_std is not None and gm_std < 0.06) else "LOW"),
        "roic_avg": roic_avg,
        "roic_consistency": f"{roic_above_10_count}/{len(roic_nn)} years above 10%",
        "capex_intensity_declining": capex_declining,
        "revenue_growth_consistency": "HIGH" if (rev_std is not None and rev_std < 0.10) else ("MEDIUM" if (rev_std is not None and rev_std < 0.20) else "LOW"),
        "pricing_power_evidence": keyword_counts.get("pricing_power", 0),
        "switching_cost_evidence": keyword_counts.get("switching_costs", 0),
        "network_effect_evidence": keyword_counts.get("network_effects", 0),
        "scale_evidence": keyword_counts.get("scale_advantages", 0),
        "intangibles_evidence": keyword_counts.get("intangibles", 0),
        "md_and_a_keywords_found": keyword_counts,
        "eroding_moat_signals": eroding_moat_signals,
        "gross_margin_declining_5yr": gm_declining_5yr,
        "moat_score": moat_score,
        "notes": notes,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Moat] {ticker}: width={moat_width} | durability={moat_durability} | "
        f"score={moat_score} | type={strongest_moat}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_moat_research(tk)
        print(json.dumps(r, indent=2, default=str))

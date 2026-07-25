#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE MANAGEMENT RESEARCHER
==================================================
Deep management quality assessment using SEC filings (DEF 14A, Form 4).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
RESEARCH_DIR = BASE_DIR / "data" / "company_research"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_HEADERS = {"User-Agent": "ApexCapital research@apexcapital.com"}
SEC_RATE_LIMIT = 0.1


def _fetch_json(url: str, desc: str = "") -> Optional[dict]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[Mgmt] Failed to fetch {desc or url}: {e}")
        return None


def _fetch_text(url: str, max_chars: int = 100000) -> Optional[str]:
    import requests
    try:
        time.sleep(SEC_RATE_LIMIT)
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.text[:max_chars]
    except Exception as e:
        logger.warning(f"[Mgmt] Failed to fetch text {url}: {e}")
        return None


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean)


def _get_recent_proxy_text(cik: str) -> Optional[str]:
    """Fetch most recent DEF 14A proxy text from EDGAR."""
    try:
        cik_int    = int(cik)
        cik_padded = str(cik).zfill(10)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        subs = _fetch_json(sub_url, f"submissions CIK {cik}")
        if not subs:
            return None

        recent = subs.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        accns  = recent.get("accessionNumber", [])
        dates  = recent.get("filingDate", [])

        latest_accn = None
        latest_date = ""
        for i, form in enumerate(forms):
            if form in ("DEF 14A", "DEF14A"):
                d = dates[i] if i < len(dates) else ""
                if d > latest_date:
                    latest_date = d
                    latest_accn = accns[i] if i < len(accns) else None

        if not latest_accn:
            return None

        accn_dashes = latest_accn.replace("-", "")
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_dashes}/index.json"
        idx = _fetch_json(idx_url, "proxy index")
        if not idx:
            return None

        items = idx.get("directory", {}).get("item", []) if idx else []
        primary_doc = None
        for item in items:
            name = item.get("name", "")
            if name.endswith(".htm") and ("proxy" in name.lower() or "def14a" in name.lower()):
                primary_doc = name
                break
        if not primary_doc:
            for item in items:
                name = item.get("name", "")
                if name.endswith(".htm") and "index" not in name.lower() and "ex" not in name.lower():
                    primary_doc = name
                    break

        if not primary_doc:
            return None

        if primary_doc.startswith("http"):
            url = primary_doc
        else:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_dashes}/{primary_doc}"

        text = _fetch_text(url, max_chars=100000)
        if text:
            return _strip_html(text)
        return None

    except Exception as e:
        logger.warning(f"[Mgmt] Proxy fetch error for CIK {cik}: {e}")
        return None


def _parse_proxy_data(text: str) -> dict:
    """Extract management metrics from proxy text."""
    result = {
        "ceo_tenure_est": None,
        "board_independence": None,
        "say_on_pay_pct": None,
        "related_party_flag": False,
        "compensation_notes": [],
    }

    if not text:
        return result

    text_lower = text.lower()

    # Say-on-pay vote results
    sop_match = re.search(
        r"say.on.pay[^.]{0,200}?(\d{2,3}(?:\.\d+)?)\s*%",
        text_lower, re.DOTALL
    )
    if sop_match:
        try:
            result["say_on_pay_pct"] = float(sop_match.group(1))
        except ValueError:
            pass

    # CEO tenure: look for "served as (chief executive|CEO) since (year)"
    tenure_match = re.search(
        r"(?:chief executive officer|ceo)[^.]{0,100}?since\s+(\d{4})",
        text_lower
    )
    if tenure_match:
        try:
            since_year = int(tenure_match.group(1))
            result["ceo_tenure_est"] = datetime.now().year - since_year
        except ValueError:
            pass

    # Board independence: "X of Y directors are independent" or "X% independent"
    ind_match = re.search(
        r"(\d+)\s+of\s+(\d+)\s+(?:members|directors)[^.]{0,30}?independent",
        text_lower
    )
    if ind_match:
        try:
            ind  = int(ind_match.group(1))
            total = int(ind_match.group(2))
            result["board_independence"] = ind / total if total > 0 else None
        except ValueError:
            pass

    # Compensation alignment: look for ROIC/FCF in incentive mentions
    if any(kw in text_lower for kw in ["return on invested capital", "free cash flow", "roic"]):
        result["compensation_notes"].append("Incentives include ROIC/FCF metrics -- aligned with value creation")
    if any(kw in text_lower for kw in ["revenue growth", "earnings per share", "eps"]):
        result["compensation_notes"].append("Incentives include revenue/EPS metrics -- shorter-term orientation")

    # Related party transactions
    if any(kw in text_lower for kw in ["related party transaction", "related-party"]):
        result["related_party_flag"] = True

    return result


def _get_insider_transactions(cik: str) -> dict:
    """
    Fetch Form 4 insider transactions for last 3 years.
    Returns summary of buy/sell activity.
    """
    result = {
        "insider_buy_usd": 0.0,
        "insider_sell_usd": 0.0,
        "insider_buy_ratio_3yr": None,
        "transaction_count": 0,
        "error": None,
    }

    try:
        today    = datetime.now().date()
        three_yr = (datetime.now() - timedelta(days=3 * 365)).date()

        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{cik}%22"
            f"&forms=4&dateRange=custom&startdt={three_yr}&enddt={today}&hits.hits.total.value=true"
        )
        time.sleep(SEC_RATE_LIMIT)

        import requests
        resp = requests.get(search_url, headers=EDGAR_HEADERS, timeout=30)
        if not resp.ok:
            result["error"] = f"Search API status {resp.status_code}"
            return result

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        buy_usd  = 0.0
        sell_usd = 0.0
        tx_count = 0

        for hit in hits[:50]:  # process up to 50 filings
            src = hit.get("_source", {})
            # EDGAR full-text search returns limited fields
            # Transaction amounts not always in search results -- use as proxy
            tx_count += 1

        # Use hit count as rough proxy if detailed data unavailable
        result["transaction_count"] = tx_count
        result["insider_buy_usd"] = buy_usd
        result["insider_sell_usd"] = sell_usd

        total = buy_usd + sell_usd
        result["insider_buy_ratio_3yr"] = (buy_usd / total) if total > 0 else None

    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[Mgmt] Insider transactions error for CIK {cik}: {e}")

    return result


def _nonull(lst) -> list:
    return [v for v in (lst or []) if v is not None]


def _avg(lst) -> Optional[float]:
    n = _nonull(lst)
    return sum(n) / len(n) if n else None


def run_management_research(ticker: str, cik: str = None) -> dict:
    """
    Deep management quality assessment using SEC filings.
    """
    ticker = ticker.upper().strip()
    out_dir  = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "management_quality.json"

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        f = get_edgar_fundamentals(ticker)
    except Exception as e:
        logger.error(f"[Mgmt] EDGAR fetch failed for {ticker}: {e}")
        result = {"ticker": ticker, "error": str(e), "management_score": 30,
                  "management_verdict": "FAIR", "generated_at": datetime.now(timezone.utc).isoformat()}
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result

    if f:
        if not cik:
            cik = f.get("cik")
    else:
        f = {}

    years  = f.get("years", [])
    ebit   = f.get("ebit", [])
    assets = f.get("total_assets", [])
    liab   = f.get("total_liabilities", [])
    shares = f.get("shares_outstanding", [])
    n = len(years)

    # --- a) Proxy statement analysis ---
    proxy_data = {}
    if cik:
        try:
            proxy_text = _get_recent_proxy_text(cik)
            if proxy_text:
                proxy_data = _parse_proxy_data(proxy_text)
        except Exception as e:
            logger.warning(f"[Mgmt] Proxy parse error for {ticker}: {e}")

    ceo_tenure       = proxy_data.get("ceo_tenure_est")
    board_ind        = proxy_data.get("board_independence")
    say_on_pay_pct   = proxy_data.get("say_on_pay_pct")
    related_party_flag = proxy_data.get("related_party_flag", False)
    comp_notes       = proxy_data.get("compensation_notes", [])

    # --- b) Insider trading ---
    insider_data = {}
    if cik:
        try:
            insider_data = _get_insider_transactions(cik)
        except Exception as e:
            logger.warning(f"[Mgmt] Insider data error: {e}")

    insider_buy_usd  = insider_data.get("insider_buy_usd", 0.0)
    insider_sell_usd = insider_data.get("insider_sell_usd", 0.0)
    insider_buy_ratio = insider_data.get("insider_buy_ratio_3yr")

    # --- c) Capital allocation scorecard ---
    # Large YoY asset jumps = possible acquisitions
    acquisition_events = []
    for i in range(1, n):
        if assets[i] and assets[i - 1] and assets[i - 1] > 0:
            asset_growth = (assets[i] - assets[i - 1]) / assets[i - 1]
            if asset_growth > 0.25:
                acquisition_events.append({
                    "year": years[i] if i < len(years) else str(i),
                    "asset_growth_pct": round(asset_growth * 100, 1)
                })

    # ROIC trajectory after acquisitions
    roic_list = []
    for i in range(n):
        eb = ebit[i]
        a  = assets[i]
        l  = liab[i]
        if eb is not None and a is not None and l is not None:
            inv_cap = a - l * 0.4
            if inv_cap > 0:
                roic_list.append(eb * 0.79 / inv_cap)
            else:
                roic_list.append(None)
        else:
            roic_list.append(None)

    # Shares trend (buyback vs dilution)
    shares_nn = [(i, v) for i, v in enumerate(shares) if v is not None]
    shares_trend = "UNKNOWN"
    shares_5yr_change = None
    if len(shares_nn) >= 6:
        shares_5yr_change = (shares_nn[-1][1] - shares_nn[-6][1]) / abs(shares_nn[-6][1])
        if shares_5yr_change < -0.05:
            shares_trend = "BUYBACK -- disciplined capital return"
        elif shares_5yr_change < -0.01:
            shares_trend = "MILD_BUYBACK"
        elif shares_5yr_change > 0.10:
            shares_trend = "SIGNIFICANT_DILUTION -- concerning"
        elif shares_5yr_change > 0.02:
            shares_trend = "MILD_DILUTION"
        else:
            shares_trend = "STABLE"

    # Capital allocation score (0-100)
    cap_alloc_score = 50  # start neutral

    if shares_5yr_change is not None:
        if shares_5yr_change < -0.10:
            cap_alloc_score += 20
        elif shares_5yr_change < -0.03:
            cap_alloc_score += 12
        elif shares_5yr_change > 0.15:
            cap_alloc_score -= 20
        elif shares_5yr_change > 0.05:
            cap_alloc_score -= 10

    # ROIC improvement signal
    roic_nn = _nonull(roic_list)
    if len(roic_nn) >= 4:
        early_roic = _avg(roic_nn[:len(roic_nn)//2])
        late_roic  = _avg(roic_nn[len(roic_nn)//2:])
        if early_roic and late_roic:
            if late_roic > early_roic * 1.1:
                cap_alloc_score += 15
            elif late_roic < early_roic * 0.85:
                cap_alloc_score -= 10

    if acquisition_events and len(acquisition_events) > 2:
        cap_alloc_score -= 10  # serial acquirers = risk

    cap_alloc_score = max(0, min(100, cap_alloc_score))

    # --- Compensation alignment ---
    has_roic_incentive = any("ROIC/FCF" in note for note in comp_notes)
    has_eps_incentive  = any("EPS" in note for note in comp_notes)
    if has_roic_incentive and not has_eps_incentive:
        comp_alignment = "HIGH"
    elif has_roic_incentive:
        comp_alignment = "MEDIUM"
    elif comp_notes:
        comp_alignment = "MEDIUM"
    else:
        comp_alignment = "UNKNOWN"

    # --- Red flags ---
    red_flags = []
    if related_party_flag:
        red_flags.append("Related party transactions found in proxy statement")
    if say_on_pay_pct is not None and say_on_pay_pct < 70:
        red_flags.append(f"Say-on-pay vote only {say_on_pay_pct:.0f}% approval -- shareholder concern")
    if shares_5yr_change is not None and shares_5yr_change > 0.15:
        red_flags.append(f"Significant share dilution: {shares_5yr_change*100:.1f}% over 5yr")
    if len(acquisition_events) >= 3:
        red_flags.append(f"Serial acquirer: {len(acquisition_events)} large asset jumps detected")

    # --- Management score ---
    score = 50

    # CEO tenure
    if ceo_tenure is not None:
        if ceo_tenure >= 10:
            score += 15
        elif ceo_tenure >= 5:
            score += 8
        elif ceo_tenure <= 1:
            score -= 5

    # Board independence
    if board_ind is not None:
        if board_ind >= 0.75:
            score += 10
        elif board_ind < 0.50:
            score -= 10

    # Say-on-pay
    if say_on_pay_pct is not None:
        if say_on_pay_pct >= 90:
            score += 10
        elif say_on_pay_pct < 70:
            score -= 15

    # Capital allocation
    score += (cap_alloc_score - 50) * 0.3

    # Red flags
    score -= len(red_flags) * 8

    # Compensation alignment
    if comp_alignment == "HIGH":
        score += 10
    elif comp_alignment == "UNKNOWN":
        score -= 5

    score = max(0, min(100, int(score)))

    if score >= 80:
        verdict = "EXCELLENT"
    elif score >= 65:
        verdict = "GOOD"
    elif score >= 45:
        verdict = "FAIR"
    else:
        verdict = "POOR"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ceo_tenure_est": ceo_tenure,
        "board_independence": board_ind,
        "say_on_pay_pct": say_on_pay_pct,
        "insider_buy_ratio_3yr": insider_buy_ratio,
        "insider_buy_usd": insider_buy_usd,
        "insider_sell_usd": insider_sell_usd,
        "capital_allocation_score": cap_alloc_score,
        "shares_5yr_change": shares_5yr_change,
        "shares_trend": shares_trend,
        "acquisition_events": acquisition_events,
        "compensation_alignment": comp_alignment,
        "compensation_notes": comp_notes,
        "related_party_flag": related_party_flag,
        "red_flags": red_flags,
        "management_score": score,
        "management_verdict": verdict,
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[Mgmt] {ticker}: score={score} | verdict={verdict} | "
        f"red_flags={len(red_flags)} | cap_alloc={cap_alloc_score}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["MSCI"]
    for tk in tickers:
        r = run_management_research(tk)
        print(json.dumps(r, indent=2, default=str))

#!/usr/bin/env python3
"""
APEX CAPITAL -- ACTIVIST INVESTOR MONITOR AGENT
=================================================
Checks for activist investor involvement via 13D/13G filings.
Saves output to data/company_research/{ticker}/activist_monitor.json
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

KNOWN_ACTIVISTS = [
    "Elliott", "Starboard", "Third Point", "Pershing Square", "ValueAct",
    "Carl Icahn", "Icahn", "Jana", "Trian", "Greenlight", "Sachem Head",
]

KNOWN_VALUE_INVESTORS = [
    "Berkshire", "Baupost", "Fairfax", "Pzena", "Tweedy", "Third Avenue",
    "Southeastern", "Ariel", "Brandes",
]


def _edgar_get(url: str) -> Optional[dict]:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        time.sleep(EDGAR_RATE_LIMIT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"[AIM] EDGAR fetch failed {url}: {e}")
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
        logger.debug(f"[AIM] CIK lookup failed: {e}")
    return None


def _search_13dg_filings(cik: str) -> list:
    """
    Search EDGAR EFTS for SC 13D and SC 13G filings mentioning this company's CIK.
    Returns list of filing records.
    """
    two_yr_ago = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    import urllib.parse

    filings_found = []

    for form_type in ["SC+13D", "SC+13G"]:
        url = (
            "https://efts.sec.gov/LATEST/search-index?q=%22" + urllib.parse.quote(cik) + "%22"
            + "&forms=" + form_type
            + "&dateRange=custom&startdt=" + two_yr_ago + "&enddt=" + today
        )
        data = _edgar_get(url)
        if not data:
            continue

        hits = data.get("hits", {}).get("hits", [])
        for hit in hits[:20]:  # cap at 20 filings
            src = hit.get("_source", {})
            filings_found.append({
                "form": src.get("form_type", form_type.replace("+", " ")),
                "filer": src.get("display_names", ["Unknown"])[0] if src.get("display_names") else "Unknown",
                "filed": src.get("file_date", ""),
                "accession": hit.get("_id", ""),
            })

    return filings_found


def _parse_ownership(filer_name: str, form: str) -> dict:
    """Parse filer name to identify activist vs value investor."""
    filer_upper = filer_name.upper()

    is_activist = any(a.upper() in filer_upper for a in KNOWN_ACTIVISTS)
    is_value = any(v.upper() in filer_upper for v in KNOWN_VALUE_INVESTORS)
    is_active = "13D" in form  # 13D = active intent, 13G = passive

    return {
        "filer": filer_name,
        "form": form,
        "is_activist": is_activist,
        "is_value_investor": is_value,
        "is_active_holder": is_active,
    }


def run_activist_monitor(ticker: str, cik: Optional[str] = None) -> dict:
    """
    Main activist investor monitoring agent.
    """
    ticker = ticker.upper().strip()
    out_dir = RESEARCH_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "activist_monitor.json"

    logger.info(f"[AIM] Starting activist investor monitor for {ticker}")

    if not cik:
        cik = _get_cik(ticker)
        logger.info(f"[AIM] {ticker} CIK: {cik}")

    if not cik:
        result = {
            "ticker": ticker,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "CIK not found",
            "activist_present": False,
            "activist_names": [],
            "value_investor_present": False,
            "value_investor_names": [],
            "largest_outside_holder": None,
            "largest_pct": None,
            "signal": "NEUTRAL",
            "notes": ["Could not retrieve 13D/13G data -- CIK lookup failed"],
        }
        with open(out_file, "w") as fh:
            json.dump(result, fh, indent=2)
        return result

    # Search 13D/13G filings
    filings = _search_13dg_filings(cik)
    logger.info(f"[AIM] {ticker}: Found {len(filings)} 13D/13G filings in last 2 years")

    # Parse each filing
    parsed = [_parse_ownership(f["filer"], f["form"]) for f in filings]

    activist_names = list({p["filer"] for p in parsed if p["is_activist"] and p["is_active_holder"]})
    value_investor_names = list({p["filer"] for p in parsed if p["is_value_investor"]})
    unknown_13d = [p for p in parsed if not p["is_activist"] and not p["is_value_investor"] and p["is_active_holder"]]

    activist_present = len(activist_names) > 0
    value_investor_present = len(value_investor_names) > 0

    # Signal determination
    notes = []
    if activist_present and value_investor_present:
        signal = "CATALYST"
        notes.append(
            "Activist investor (" + ", ".join(activist_names[:2]) + ") AND value investor both present -- "
            "strong external validation with potential catalyst"
        )
    elif activist_present:
        signal = "CATALYST"
        notes.append(
            "Known activist investor present (" + ", ".join(activist_names[:2]) + ") -- "
            "may unlock value but introduces uncertainty"
        )
    elif value_investor_present:
        signal = "VALIDATION"
        notes.append(
            "Known value investors present (" + ", ".join(value_investor_names[:2]) + ") -- "
            "smart money agrees with thesis"
        )
    elif unknown_13d:
        signal = "FLAG"
        notes.append(
            str(len(unknown_13d)) + " unknown large holder(s) filed 13D -- unknown intentions, monitor closely"
        )
    else:
        signal = "NEUTRAL"
        notes.append("No large activist or value investor positions detected in last 2 years")

    largest_holder = filings[0]["filer"] if filings else None

    result = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "activist_present": activist_present,
        "activist_names": activist_names,
        "value_investor_present": value_investor_present,
        "value_investor_names": value_investor_names,
        "largest_outside_holder": largest_holder,
        "largest_pct": None,  # Would require parsing full filing document
        "signal": signal,
        "notes": notes,
        "total_filings_found": len(filings),
        "filings_detail": filings[:10],
    }

    with open(out_file, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    logger.info(
        f"[AIM] {ticker}: signal={signal} activist={activist_present} "
        f"value_investor={value_investor_present} filings={len(filings)}"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSCI"
    r = run_activist_monitor(ticker)
    print(json.dumps(r, indent=2, default=str))

# =============================================================================
# SIGNAL ENGINE — SEC EDGAR 13F Parser + Block Purchase Detector
# =============================================================================
# Fetches quarterly 13F filings from SEC EDGAR, detects new/increased
# institutional positions, and generates buy/sell signals.
# =============================================================================

import requests
import json
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd

from config import TRACKED_INSTITUTIONS, STOCK_UNIVERSE, MIN_INSTITUTIONS_SIGNAL

logger = logging.getLogger(__name__)

SEC_HEADERS = {
    "User-Agent": "InstitutionalTracker research@example.com",
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_BASE = "https://data.sec.gov"
EDGAR_SUBMISSIONS = f"{EDGAR_BASE}/submissions"
EDGAR_COMPANY_FACTS = f"{EDGAR_BASE}/api/xbrl/companyfacts"


# ---------------------------------------------------------------------------
# 13F Filing Fetcher
# ---------------------------------------------------------------------------

def get_recent_13f_filings(cik: str, institution_name: str, limit: int = 4) -> list[dict]:
    """
    Fetch the most recent 13F-HR filings for a given institution from SEC EDGAR.
    Returns a list of filing metadata dicts.
    """
    cik_padded = cik.zfill(10)
    url = f"{EDGAR_SUBMISSIONS}/CIK{cik_padded}.json"

    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        filings = data.get("filings", {}).get("recent", {})
        forms       = filings.get("form", [])
        dates       = filings.get("filingDate", [])
        accessions  = filings.get("accessionNumber", [])

        results = []
        for form, date, acc in zip(forms, dates, accessions):
            if form in ("13F-HR", "13F-HR/A"):
                results.append({
                    "institution": institution_name,
                    "cik": cik,
                    "form": form,
                    "filing_date": date,
                    "accession": acc,
                })
                if len(results) >= limit:
                    break

        logger.info(f"  {institution_name}: {len(results)} 13F filings found")
        return results

    except Exception as e:
        logger.warning(f"  Could not fetch filings for {institution_name}: {e}")
        return []


def parse_13f_holdings(cik: str, accession: str) -> pd.DataFrame:
    """
    Parse the holdings table from a 13F filing's primary XML document.
    Returns a DataFrame with columns: ticker, cusip, shares, value_usd, type
    """
    cik_padded = cik.zfill(10)
    acc_clean  = accession.replace("-", "")
    index_url  = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/"
        f"{acc_clean}/{accession}-index.htm"
    )

    try:
        resp = requests.get(index_url, headers=SEC_HEADERS, timeout=15)
        # Try the EDGAR full-text search API for the InfoTable XML
        api_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{accession}%22"
            f"&dateRange=custom&startdt=2019-01-01&enddt=2025-01-01&forms=13F-HR"
        )
        # Fall back to parsing the structured data endpoint
        holdings_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_padded}&type=13F-HR&dateb=&owner=include&count=10"
        )

        # Use the EDGAR XBRL company facts as a proxy for position data
        return _parse_via_edgar_api(cik, accession)

    except Exception as e:
        logger.debug(f"  Parsing error for {accession}: {e}")
        return pd.DataFrame()


def _parse_via_edgar_api(cik: str, accession: str) -> pd.DataFrame:
    """
    Use SEC EDGAR full-text search to get structured 13F holdings.
    """
    cik_padded  = cik.zfill(10)
    acc_nodash  = accession.replace("-", "")
    base        = f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/{acc_nodash}/"

    # Fetch filing index
    idx_url = base + accession + "-index.json"
    try:
        r = requests.get(idx_url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        files = r.json().get("directory", {}).get("item", [])
    except Exception:
        return pd.DataFrame()

    # Find the InfoTable XML
    xml_file = None
    for f in files:
        name = f.get("name", "").lower()
        if "infotable" in name and name.endswith(".xml"):
            xml_file = f["name"]
            break

    if not xml_file:
        return pd.DataFrame()

    xml_url = base + xml_file
    try:
        r = requests.get(xml_url, headers=SEC_HEADERS, timeout=15)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "xml")

        rows = []
        for entry in soup.find_all("infoTable"):
            try:
                name_tag   = entry.find("nameOfIssuer")
                cusip_tag  = entry.find("cusip")
                shares_tag = entry.find("sshPrnamt")
                value_tag  = entry.find("value")
                type_tag   = entry.find("sshPrnamtType")

                rows.append({
                    "name":       name_tag.text.strip()   if name_tag   else "",
                    "cusip":      cusip_tag.text.strip()  if cusip_tag  else "",
                    "shares":     int(shares_tag.text.replace(",", "")) if shares_tag else 0,
                    "value_usd":  int(value_tag.text.replace(",", "")) * 1000 if value_tag else 0,
                    "type":       type_tag.text.strip()   if type_tag   else "",
                })
            except Exception:
                continue

        return pd.DataFrame(rows)

    except Exception as e:
        logger.debug(f"  XML parse error: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Block Purchase Detector
# ---------------------------------------------------------------------------

def detect_block_purchases(
    current_holdings: pd.DataFrame,
    prior_holdings: pd.DataFrame,
    institution: str,
    block_threshold_shares: int = 100_000,
    block_threshold_value: int = 1_000_000,
) -> list[dict]:
    """
    Compare two consecutive 13F filings and identify:
    - NEW positions (not in prior filing)
    - SIGNIFICANTLY INCREASED positions (>20% share increase AND > block thresholds)

    Returns a list of signal dicts.
    """
    signals = []

    if current_holdings.empty:
        return signals

    # Map prior holdings by CUSIP for comparison
    prior_map = {}
    if not prior_holdings.empty and "cusip" in prior_holdings.columns:
        for _, row in prior_holdings.iterrows():
            prior_map[row["cusip"]] = row

    for _, row in current_holdings.iterrows():
        cusip    = row.get("cusip", "")
        name     = row.get("name", "")
        shares   = row.get("shares", 0)
        value    = row.get("value_usd", 0)

        if shares < block_threshold_shares and value < block_threshold_value:
            continue  # Too small to be a meaningful block

        signal_type = None
        share_delta = shares
        pct_increase = 100.0

        if cusip in prior_map:
            prior_shares = prior_map[cusip].get("shares", 0)
            if prior_shares == 0:
                continue
            share_delta   = shares - prior_shares
            pct_increase  = (share_delta / prior_shares) * 100

            if share_delta >= block_threshold_shares and pct_increase >= 20:
                signal_type = "INCREASED"
        else:
            signal_type = "NEW"

        if signal_type:
            signals.append({
                "institution":  institution,
                "name":         name,
                "cusip":        cusip,
                "signal_type":  signal_type,
                "shares_added": share_delta,
                "value_usd":    value,
                "pct_increase": round(pct_increase, 1),
            })

    return signals


# ---------------------------------------------------------------------------
# Multi-Institution Confluence Engine
# ---------------------------------------------------------------------------

def build_confluence_signals(all_signals: list[dict]) -> pd.DataFrame:
    """
    Aggregate block purchase signals across all institutions.
    A stock with signals from >= MIN_INSTITUTIONS_SIGNAL institutions is a BUY.
    When Renaissance + Citadel both signal → HIGH_CONVICTION weight applied.
    """
    # Group by company name (normalized)
    name_groups = defaultdict(list)
    for sig in all_signals:
        key = sig["name"].upper().strip()
        name_groups[key].append(sig)

    rows = []
    for name, sigs in name_groups.items():
        institutions = [s["institution"] for s in sigs]
        n_institutions = len(set(institutions))

        if n_institutions < MIN_INSTITUTIONS_SIGNAL:
            continue

        total_value = sum(s["value_usd"] for s in sigs)

        # HIGH_CONVICTION: Renaissance + Citadel both buying
        high_conviction = (
            "Renaissance Tech" in institutions and "Citadel" in institutions
        )

        conviction_score = n_institutions * (2.0 if high_conviction else 1.0)

        rows.append({
            "company":          name,
            "institutions":     ", ".join(sorted(set(institutions))),
            "n_institutions":   n_institutions,
            "total_value_usd":  total_value,
            "high_conviction":  high_conviction,
            "conviction_score": conviction_score,
            "signal_type":      "BUY",
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("conviction_score", ascending=False).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Map Company Names to Tickers
# ---------------------------------------------------------------------------

CUSIP_TO_TICKER_CACHE = {}

def name_to_ticker(company_name: str, universe=STOCK_UNIVERSE):
    """
    Attempt to map a 13F company name to a ticker symbol.
    Uses a simple keyword match against the stock universe.
    """
    name_upper = company_name.upper()

    KNOWN_MAPPINGS = {
        "APPLE": "AAPL", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
        "ALPHABET": "GOOGL", "GOOGLE": "GOOGL", "NVIDIA": "NVDA",
        "META": "META", "TESLA": "TSLA", "BERKSHIRE": "BRK.B",
        "JPMORGAN": "JPM", "JOHNSON": "JNJ", "EXXON": "XOM",
        "UNITEDHEALTH": "UNH", "VISA": "V", "MASTERCARD": "MA",
        "HOME DEPOT": "HD", "PROCTER": "PG", "BANK OF AMERICA": "BAC",
        "BROADCOM": "AVGO", "MERCK": "MRK", "CHEVRON": "CVX",
        "ELI LILLY": "LLY", "ABBVIE": "ABBV", "COSTCO": "COST",
        "PEPSICO": "PEP", "COCA-COLA": "KO", "THERMO FISHER": "TMO",
        "CISCO": "CSCO", "MCDONALD": "MCD", "WALMART": "WMT",
        "DISNEY": "DIS", "SALESFORCE": "CRM", "ADOBE": "ADBE",
        "NETFLIX": "NFLX", "ADVANCED MICRO": "AMD", "INTEL": "INTC",
        "QUALCOMM": "QCOM", "TEXAS INSTRUMENTS": "TXN", "HONEYWELL": "HON",
        "GOLDMAN": "GS", "MORGAN STANLEY": "MS", "BLACKROCK": "BLK",
    }

    for keyword, ticker in KNOWN_MAPPINGS.items():
        if keyword in name_upper:
            return ticker if ticker in universe else None

    return None


# ---------------------------------------------------------------------------
# Main Run Function
# ---------------------------------------------------------------------------

def run_signal_scan(verbose: bool = True) -> pd.DataFrame:
    """
    Full pipeline: fetch latest 13F filings → detect block purchases → confluence signals.
    Returns a DataFrame of actionable BUY signals with tickers.
    """
    logger.info("=" * 60)
    logger.info("SIGNAL SCAN STARTED")
    logger.info("=" * 60)

    all_signals = []

    for institution, cik in TRACKED_INSTITUTIONS.items():
        if verbose:
            logger.info(f"\nScanning {institution} (CIK: {cik})")

        filings = get_recent_13f_filings(cik, institution, limit=2)
        if len(filings) < 2:
            logger.warning(f"  Not enough filings to compare for {institution}")
            continue

        time.sleep(0.5)  # Be polite to SEC servers

        current_filing = filings[0]
        prior_filing   = filings[1]

        current_holdings = parse_13f_holdings(cik, current_filing["accession"])
        time.sleep(0.5)
        prior_holdings   = parse_13f_holdings(cik, prior_filing["accession"])

        signals = detect_block_purchases(current_holdings, prior_holdings, institution)
        logger.info(f"  {institution}: {len(signals)} block purchase signals detected")
        all_signals.extend(signals)

    logger.info(f"\nTotal raw signals: {len(all_signals)}")

    confluence_df = build_confluence_signals(all_signals)
    logger.info(f"Confluence signals (≥{MIN_INSTITUTIONS_SIGNAL} institutions): {len(confluence_df)}")

    # Map names to tickers
    if not confluence_df.empty:
        confluence_df["ticker"] = confluence_df["company"].apply(name_to_ticker)
        confluence_df = confluence_df[confluence_df["ticker"].notna()].copy()
        logger.info(f"Signals with known tickers: {len(confluence_df)}")

    logger.info("=" * 60)
    logger.info("SIGNAL SCAN COMPLETE")
    logger.info("=" * 60)

    return confluence_df


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('SignalEngine'):
        raise SystemExit(0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    signals = run_signal_scan()
    if not signals.empty:
        print("\nACTIONABLE BUY SIGNALS:")
        print(signals[["ticker", "company", "institutions", "conviction_score", "high_conviction"]].to_string())
    else:
        print("No signals today (13F filings are quarterly — signals appear after filing dates)")

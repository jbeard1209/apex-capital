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
            # Originals only. An amendment (13F-HR/A) is often a partial
            # filing — a few late positions — so diffing this quarter against
            # one made every other holding look NEW.
            if form == "13F-HR":
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

    # Fetch filing index. The directory listing is `<folder>/index.json`.
    # This read `<accession>-index.json` until 2026-11-19 — a path EDGAR
    # answers with an HTML page and status 200, so `.json()` raised, the
    # bare except returned an empty frame, and the scanner reported
    # "0 block purchase signals" for every institution on every run: 860
    # logged runs on the Mac, zero signals, ever. An empty result here is a
    # parser failure until proven otherwise, so it now says so.
    idx_url = base + "index.json"
    try:
        r = requests.get(idx_url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            logger.warning(f"  13F index {idx_url} -> HTTP {r.status_code}")
            return pd.DataFrame()
        files = r.json().get("directory", {}).get("item", [])
    except Exception as e:
        logger.warning(f"  13F index {idx_url} unreadable: {e}")
        return pd.DataFrame()

    xml_file = _pick_infotable(files)

    if not xml_file:
        logger.warning(f"  13F {accession}: no information-table XML in {idx_url}")
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
                # An option row is not a share purchase, and a PUT is a bet
                # the other way. Counting them made a bought put look like
                # a block buy of the stock.
                if entry.find("putCall") is not None:
                    continue

                rows.append({
                    "name":       name_tag.text.strip()   if name_tag   else "",
                    "cusip":      cusip_tag.text.strip()  if cusip_tag  else "",
                    "shares":     int(shares_tag.text.replace(",", "")) if shares_tag else 0,
                    # Whole dollars. SEC moved the <value> field from
                    # thousands to dollars for filings from 2023-01-03; the
                    # old `* 1000` made every holding 1000x too large, so the
                    # $1M block threshold passed everything.
                    "value_usd":  int(value_tag.text.replace(",", "")) if value_tag else 0,
                    "type":       type_tag.text.strip()   if type_tag   else "",
                })
            except Exception:
                continue

        return _aggregate_by_cusip(pd.DataFrame(rows))

    except Exception as e:
        logger.warning(f"  13F XML parse error for {accession}: {e}")
        return pd.DataFrame()


# A 13F-HR is due 45 days after quarter end, so a filer that is current has
# a latest filing no older than ~one quarter + 45 days. Past that, the newest
# filing describes a quarter the firm has already traded through.
MAX_13F_AGE_DAYS = 140


def _filing_is_stale(filing_date: str, today=None) -> bool:
    from datetime import date
    try:
        filed = date.fromisoformat(str(filing_date)[:10])
    except ValueError:
        return True          # no usable date is not evidence of currency
    return ((today or date.today()) - filed).days > MAX_13F_AGE_DAYS


def _pick_infotable(files: list):
    """The information-table XML. Most filers name it *infotable*.xml; some
    use a numeric name. primary_doc.xml is the cover page, never the table."""
    xmls = [f.get("name", "") for f in files
            if f.get("name", "").lower().endswith(".xml")]
    for n in xmls:
        if "infotable" in n.lower():
            return n
    rest = [n for n in xmls if n.lower() != "primary_doc.xml"]
    return rest[0] if len(rest) == 1 else None


def _aggregate_by_cusip(df: pd.DataFrame) -> pd.DataFrame:
    """One row per CUSIP. A large filer reports the same security on many
    rows (one per manager / discretion type); comparing row-by-row against a
    dict keyed by CUSIP kept only the LAST row of the prior filing, so a
    position split across managers looked like a giant increase."""
    if df.empty:
        return df
    df = df[df["type"].isin(["SH", ""])] if "type" in df.columns else df
    if df.empty:
        return df
    return (df.groupby("cusip", as_index=False)
              .agg(name=("name", "first"), shares=("shares", "sum"),
                   value_usd=("value_usd", "sum"), type=("type", "first")))


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
    # Group by CUSIP, not by name. The issuer name is typed by each FILER, so
    # the same security reaches us as "BANK OF AMER CORP" from one fund and
    # "BANK OF AMERICA CORP" from another; grouping on the string split one
    # stock's confluence into two sub-threshold halves. The CUSIP is the
    # security's identity. Name is the fallback only for a row with no CUSIP.
    name_groups = defaultdict(list)
    for sig in all_signals:
        key = (sig.get("cusip") or "").strip().upper() or sig["name"].upper().strip()
        name_groups[key].append(sig)

    rows = []
    for key, sigs in name_groups.items():
        name = sigs[0]["name"].upper().strip()
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
            "cusip":            sigs[0].get("cusip", ""),
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

# CUSIP -> ticker for the universe's 13F-reportable names. Every entry was
# checked against the issuer name in a live 13F-HR information table
# (Citadel / Renaissance / Millennium / Point72 / D.E. Shaw, filed 2026) on
# 2026-11-19, and every check digit validates (`_cusip_check_digit`).
#
# This replaced a substring match on the issuer name ("META" in name), which
# mapped METALLUS, KENNAMETAL, COMMERCIAL METALS and ARDAGH METAL to META,
# APPLE HOSPITALITY REIT to AAPL, JOHNSON CONTROLS to JNJ, INTELLIGENT-anything
# to INTC, HONEYWELL AEROSPACE to HON, and BLACKROCK ETF TRUST to BLK — and
# MISSED the real Bank of America and Coca-Cola, which filers write as
# "BANK OF AMER CORP" and "COCA COLA CO". A confluence signal is a BUY, so a
# false mapping is an order in the wrong company. An unknown CUSIP maps to
# None: no signal is the safe failure, a wrong ticker is not.
CUSIP_TO_TICKER = {
    "037833100": "AAPL",  "594918104": "MSFT",  "023135106": "AMZN",
    "02079K305": "GOOGL", "02079K107": "GOOGL", "67066G104": "NVDA",
    "30303M102": "META",  "88160R101": "TSLA",  "084670702": "BRK.B",
    "46625H100": "JPM",   "478160104": "JNJ",
    "30233Q108": "XOM",   # filed as EXXONMOBIL HOLDINGS CORP
    "91324P102": "UNH",   "92826C839": "V",     "57636Q104": "MA",
    "437076102": "HD",    "742718109": "PG",    "060505104": "BAC",
    "11135F101": "AVGO",  "58933Y105": "MRK",   "166764100": "CVX",
    "532457108": "LLY",   "00287Y109": "ABBV",  "22160K105": "COST",
    "713448108": "PEP",   "191216100": "KO",    "883556102": "TMO",
    "17275R102": "CSCO",  "580135101": "MCD",   "931142103": "WMT",
    "254687106": "DIS",   "79466L302": "CRM",   "00724F101": "ADBE",
    "64110L106": "NFLX",  "007903107": "AMD",   "458140100": "INTC",
    "747525103": "QCOM",  "882508104": "TXN",
    "438516205": "HON",   # HONEYWELL INTL INC (not 43849R105, HONEYWELL AEROSPACE)
    "38141G104": "GS",    "617446448": "MS",    "09290D101": "BLK",
    "911312106": "UPS",   "149123101": "CAT",   "78409V104": "SPGI",
    "H1467J104": "CB",    "571748102": "MMC",   "025816109": "AXP",
    "172967424": "C",
}


def _cusip_check_digit(first8: str) -> str:
    """The standard CUSIP modulus-10 'double-add-double' check digit."""
    total = 0
    for i, ch in enumerate(first8.upper()):
        v = int(ch) if ch.isdigit() else ord(ch) - 55
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def name_to_ticker(company_name: str, universe=STOCK_UNIVERSE, cusip: str = ""):
    """Map a 13F holding to a ticker in the universe — by CUSIP only.

    `company_name` is kept for the call signature and log lines; it is no
    longer matched on (see CUSIP_TO_TICKER for why).
    """
    ticker = CUSIP_TO_TICKER.get((cusip or "").strip().upper())
    return ticker if ticker and ticker in universe else None


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

        if _filing_is_stale(current_filing.get("filing_date", "")):
            # A fund that stopped filing still has two "recent" 13Fs; diffing
            # them re-reports a year-old quarter as this quarter's buying
            # (Two Sigma's tracked CIK last filed 2025-11-03).
            logger.warning(f"  {institution}: latest 13F-HR filed "
                           f"{current_filing.get('filing_date')} — older than "
                           f"{MAX_13F_AGE_DAYS}d, skipped as stale")
            continue

        current_holdings = parse_13f_holdings(cik, current_filing["accession"])
        time.sleep(0.5)
        prior_holdings   = parse_13f_holdings(cik, prior_filing["accession"])
        if current_holdings.empty or prior_holdings.empty:
            # A 13F-HR always has holdings. Zero is the parser failing, and
            # it must not be reported as "0 signals", which reads as a quiet
            # quarter.
            logger.warning(f"  {institution}: parsed 0 holdings from "
                           f"{current_filing['accession'] if current_holdings.empty else prior_filing['accession']}"
                           f" — 13F parser failure, not a quiet quarter")

        signals = detect_block_purchases(current_holdings, prior_holdings, institution)
        logger.info(f"  {institution}: {len(signals)} block purchase signals detected")
        all_signals.extend(signals)

    logger.info(f"\nTotal raw signals: {len(all_signals)}")

    confluence_df = build_confluence_signals(all_signals)
    logger.info(f"Confluence signals (≥{MIN_INSTITUTIONS_SIGNAL} institutions): {len(confluence_df)}")

    # Map names to tickers
    if not confluence_df.empty:
        confluence_df["ticker"] = [
            name_to_ticker(n, cusip=c)
            for n, c in zip(confluence_df["company"], confluence_df["cusip"])]
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

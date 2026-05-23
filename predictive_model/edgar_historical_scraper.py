#!/usr/bin/env python3
"""
APEX CAPITAL — EDGAR HISTORICAL SCRAPER
========================================
Pulls the full 13F filing history for all tracked institutions from SEC EDGAR
and stores it in a local SQLite database (data/historical_13f.db).

This is the foundation of the predictive model — without historical data there
is nothing to train on.

Schema
------
  filings  — one row per 13F filing (institution, date, accession, etc.)
  holdings — one row per holding per filing
  cusip_map — CUSIP → ticker resolution cache

Usage
-----
  python3 predictive_model/edgar_historical_scraper.py [--full] [--institution NAME]

  --full          scrape all available history (slow, ~2-4 hours first run)
  --institution   scrape only one institution by name (e.g. "Citadel")
  (no flags)      incremental update — only new filings since last scrape
"""

import sys
from typing import Optional
import os
import re as _re
import json as _json
import sqlite3
import time
import logging
import requests
from datetime import datetime, date
from pathlib import Path

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TRACKED_INSTITUTIONS  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("edgar_scraper")

SEC_HEADERS = {
    "User-Agent": "ApexCapital research@apexcapital.internal",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

DB_PATH = Path(__file__).parent.parent / "data" / "historical_13f.db"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions"

# Polite rate-limit: SEC asks for no more than 10 req/sec
_SEC_DELAY = 0.15

# Set to True by the label builder if the SEC returns HTTP 429 (rate-blocked).
# Other modules check this flag to abort early rather than hammering the API.
_SEC_BLOCKED: bool = False


# =============================================================================
# DATABASE SETUP
# =============================================================================

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            institution     TEXT    NOT NULL,
            cik             TEXT    NOT NULL,
            accession       TEXT    NOT NULL UNIQUE,
            form            TEXT    NOT NULL,
            filing_date     TEXT    NOT NULL,
            period_of_report TEXT,
            scraped_at      TEXT    NOT NULL,
            n_holdings      INTEGER DEFAULT 0,
            total_value_usd INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filing_id   INTEGER NOT NULL REFERENCES filings(id),
            institution TEXT    NOT NULL,
            filing_date TEXT    NOT NULL,
            cusip       TEXT    NOT NULL,
            name        TEXT,
            ticker      TEXT,
            shares      INTEGER DEFAULT 0,
            value_usd   INTEGER DEFAULT 0,
            hold_type   TEXT
        );

        CREATE TABLE IF NOT EXISTS cusip_map (
            cusip   TEXT PRIMARY KEY,
            ticker  TEXT,
            name    TEXT,
            updated TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_holdings_cusip        ON holdings(cusip);
        CREATE INDEX IF NOT EXISTS idx_holdings_institution  ON holdings(institution);
        CREATE INDEX IF NOT EXISTS idx_holdings_date         ON holdings(filing_date);
        CREATE INDEX IF NOT EXISTS idx_filings_institution   ON filings(institution);
        CREATE INDEX IF NOT EXISTS idx_filings_date          ON filings(filing_date);
    """)
    conn.commit()


# =============================================================================
# EDGAR API HELPERS
# =============================================================================

def _get(url: str, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            time.sleep(_SEC_DELAY)
            r = requests.get(url, headers=SEC_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                global _SEC_BLOCKED
                logger.warning(f"  Rate limited (429) — sleeping 60s then retrying")
                time.sleep(60)
                if attempt == retries - 1:
                    _SEC_BLOCKED = True
        except Exception as e:
            logger.debug(f"  GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


def _get_raw(url: str, retries: int = 3) -> Optional[bytes]:
    for attempt in range(retries):
        try:
            time.sleep(_SEC_DELAY)
            r = requests.get(url, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=30)
            if r.status_code == 200:
                return r.content
            if r.status_code == 429:
                time.sleep(60)
        except Exception as e:
            logger.debug(f"  Raw GET attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


def fetch_all_13f_accessions(cik: str, institution: str) -> list[dict]:
    """
    Fetch every 13F-HR accession number on record for this institution.
    EDGAR returns up to 40 recent filings in the 'recent' block; older ones
    require fetching paginated older submission files.
    """
    cik_padded = cik.zfill(10)
    base_url = f"{EDGAR_SUBMISSIONS}/CIK{cik_padded}.json"
    data = _get(base_url)
    if not data:
        logger.warning(f"  Could not fetch submissions for {institution}")
        return []

    results = []

    def _extract(block: dict):
        forms       = block.get("form",          [])
        dates       = block.get("filingDate",     [])
        accessions  = block.get("accessionNumber",[])
        periods     = block.get("reportDate",     [])
        for form, date_, acc, period in zip(forms, dates, accessions,
                                             periods if periods else [""] * len(forms)):
            if form in ("13F-HR", "13F-HR/A"):
                results.append({
                    "institution":      institution,
                    "cik":              cik,
                    "form":             form,
                    "filing_date":      date_,
                    "period_of_report": period,
                    "accession":        acc,
                })

    _extract(data.get("filings", {}).get("recent", {}))

    # Older filings stored in separate paginated files
    for older_file in data.get("filings", {}).get("files", []):
        fname = older_file.get("name", "")
        if not fname:
            continue
        older_data = _get(f"{EDGAR_SUBMISSIONS}/{fname}")
        if older_data:
            _extract(older_data)

    logger.info(f"  {institution}: {len(results)} 13F filings in EDGAR history")
    return results


def parse_holdings_from_accession(cik: str, accession: str) -> list[dict]:
    """
    Download and parse the InfoTable XML for a given accession number.
    Returns list of holding dicts.

    Strategy: try known InfoTable filename patterns directly before falling
    back to parsing the directory index (which often returns 404 on EDGAR).
    """
    cik_int    = str(int(cik))          # EDGAR uses un-padded int in archive URLs
    acc_nodash = accession.replace("-", "")
    base       = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"

    # Try the most common InfoTable filenames first (avoids a directory round-trip)
    candidate_names = [
        "infotable.xml",
        "form13fInfoTable.xml",
        "form13finfotable.xml",
        "13finfotable.xml",
        "primary_doc.xml",
        "information_table.xml",
    ]

    for fname in candidate_names:
        content = _get_raw(base + fname)
        if content and b"infoTable" in content:
            return _parse_infotable_xml(content)

    # Fallback: fetch the HTML directory listing and find the XML
    try:
        time.sleep(_SEC_DELAY)
        r = requests.get(base, headers={**SEC_HEADERS, "Accept": "text/html"}, timeout=15)
        if r.status_code == 200:
            import re
            xml_files = re.findall(r'href="([^"]+\.xml)"', r.text, re.IGNORECASE)
            for xml_href in xml_files:
                name = xml_href.split("/")[-1].lower()
                if "xsl" in name or "label" in name:
                    continue
                content = _get_raw(base + xml_href.split("/")[-1])
                if content and b"infoTable" in content:
                    return _parse_infotable_xml(content)
    except Exception:
        pass

    return []


def _strip_ns(content: bytes) -> bytes:
    """Strip XML namespace prefixes (e.g. n1:) so lxml can find tags by local name."""
    text = content.decode("utf-8", errors="replace")
    text = _re.sub(r' xmlns(?::\w+)?="[^"]*"', "", text)
    text = _re.sub(r"<(\w+):", "<", text)
    text = _re.sub(r"</(\w+):", "</", text)
    return text.encode("utf-8")


def _parse_infotable_xml(content: bytes) -> list[dict]:
    """
    Parse 13F InfoTable XML. Handles all namespace variants (default xmlns,
    prefixed n1:, or none) by stripping namespace prefixes before parsing.
    """
    try:
        import warnings
        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        content = _strip_ns(content)
        soup = BeautifulSoup(content, "lxml")
        rows = []

        entries = soup.find_all("infotable") or soup.find_all("infoTable")
        if not entries:
            entries = [t for t in soup.find_all(True)
                       if t.name and "infotable" in t.name.lower()]

        for entry in entries:
            try:
                name_tag   = entry.find("nameofissuer") or entry.find("nameOfIssuer")
                cusip_tag  = entry.find("cusip")
                shares_tag = entry.find("sshprnamt")  or entry.find("sshPrnamt")
                value_tag  = entry.find("value")
                type_tag   = entry.find("sshprnamttype") or entry.find("sshPrnamtType")

                rows.append({
                    "name":      name_tag.text.strip()  if name_tag  else "",
                    "cusip":     cusip_tag.text.strip()  if cusip_tag else "",
                    "shares":    _parse_int(shares_tag.text) if shares_tag else 0,
                    "value_usd": _parse_int(value_tag.text) * 1000 if value_tag else 0,
                    "hold_type": type_tag.text.strip()   if type_tag  else "",
                })
            except Exception:
                continue

        return rows
    except Exception as e:
        logger.debug(f"  XML parse error: {e}")
        return []


def _parse_int(text: str) -> int:
    try:
        return int(text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


def _tag(entry, name: str) -> str:
    t = entry.find(name)
    return t.text.strip() if t else ""


def _int_tag(entry, name: str) -> int:
    t = entry.find(name)
    if not t:
        return 0
    try:
        return int(t.text.replace(",", "").strip())
    except ValueError:
        return 0


# =============================================================================
# CUSIP → TICKER RESOLUTION
# =============================================================================

def resolve_ticker(cusip: str, name: str, conn: sqlite3.Connection) -> str:
    """
    Try to resolve a CUSIP to a ticker symbol.
    Uses cached cusip_map first, then falls back to name-based heuristics.
    """
    if not cusip:
        return ""

    row = conn.execute("SELECT ticker FROM cusip_map WHERE cusip = ?", (cusip,)).fetchone()
    if row and row["ticker"]:
        return row["ticker"]

    # Name-based heuristic: some names are unambiguous
    ticker = _name_to_ticker_heuristic(name)

    conn.execute(
        "INSERT OR REPLACE INTO cusip_map (cusip, ticker, name, updated) VALUES (?,?,?,?)",
        (cusip, ticker, name, datetime.now().isoformat())
    )
    conn.commit()
    return ticker


_SEC_TICKER_CACHE = Path(__file__).parent.parent / "data" / "sec_ticker_map.json"
_SEC_TICKER_MAP: dict = {}
_SEC_TICKER_MAP_LOADED = False

# Fallback map for top names (used when SEC JSON download fails)
_NAME_MAP = {
    "apple": "AAPL", "microsoft": "MSFT", "amazon": "AMZN", "alphabet": "GOOGL",
    "nvidia": "NVDA", "meta platform": "META", "tesla": "TSLA", "berkshire": "BRK.B",
    "jpmorgan": "JPM", "visa": "V", "johnson & johnson": "JNJ", "exxon": "XOM",
    "unitedhealth": "UNH", "mastercard": "MA", "home depot": "HD", "procter": "PG",
    "bank of america": "BAC", "broadcom": "AVGO", "merck": "MRK", "chevron": "CVX",
    "eli lilly": "LLY", "abbvie": "ABBV", "costco": "COST", "pepsico": "PEP",
    "coca-cola": "KO", "thermo fisher": "TMO", "cisco": "CSCO", "mcdonald": "MCD",
    "walmart": "WMT", "disney": "DIS", "salesforce": "CRM", "adobe": "ADBE",
    "netflix": "NFLX", "advanced micro": "AMD", "intel": "INTC", "qualcomm": "QCOM",
    "texas instruments": "TXN", "honeywell": "HON", "united parcel": "UPS",
    "caterpillar": "CAT", "goldman sachs": "GS", "morgan stanley": "MS",
    "blackrock": "BLK", "s&p global": "SPGI", "american express": "AXP",
    "citigroup": "C",
}

_NS_STRIP_RE = _re.compile(
    r"\b(inc|corp|co|ltd|llc|lp|plc|group|holdings|international|trust|"
    r"fund|class|com|incorporated|corporation|limited|technologies|technology|"
    r"systems|solutions|services|enterprises|partners|capital|financial)\b"
)


def _norm_name(name: str) -> str:
    """Normalize company name for lookup matching."""
    n = name.lower()
    n = _re.sub(r"[.,'\"/&\\-]", " ", n)
    n = _NS_STRIP_RE.sub(" ", n)
    return " ".join(n.split())


def _load_sec_ticker_map() -> dict:
    global _SEC_TICKER_MAP, _SEC_TICKER_MAP_LOADED
    if _SEC_TICKER_MAP_LOADED:
        return _SEC_TICKER_MAP

    # Use cached file if fresh (same day)
    if _SEC_TICKER_CACHE.exists():
        try:
            cached = _json.loads(_SEC_TICKER_CACHE.read_text())
            if cached.get("date") == date.today().isoformat():
                _SEC_TICKER_MAP = cached.get("map", {})
                _SEC_TICKER_MAP_LOADED = True
                return _SEC_TICKER_MAP
        except Exception:
            pass

    # Download from SEC
    try:
        time.sleep(_SEC_DELAY)
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=SEC_HEADERS, timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            name_map: dict = {}
            for entry in data.values():
                ticker = entry.get("ticker", "")
                title  = entry.get("title", "")
                if ticker and title:
                    norm = _norm_name(title)
                    if norm and norm not in name_map:
                        name_map[norm] = ticker
            _SEC_TICKER_CACHE.parent.mkdir(exist_ok=True)
            _SEC_TICKER_CACHE.write_text(
                _json.dumps({"date": date.today().isoformat(), "map": name_map})
            )
            _SEC_TICKER_MAP = name_map
            logger.info(f"  SEC ticker map: {len(name_map)} companies loaded")
    except Exception as e:
        logger.debug(f"  SEC ticker map download failed: {e}")

    _SEC_TICKER_MAP_LOADED = True
    return _SEC_TICKER_MAP


def _name_to_ticker_heuristic(name: str) -> str:
    # 1) Fast hardcoded map for top names
    name_lower = name.lower()
    for fragment, ticker in _NAME_MAP.items():
        if fragment in name_lower:
            return ticker

    # 2) SEC full company list (~10K companies)
    sec_map = _load_sec_ticker_map()
    norm = _norm_name(name)
    if norm and norm in sec_map:
        return sec_map[norm]

    # 3) Partial prefix match (handles trailing garbage in 13F names)
    if norm and len(norm) >= 5:
        for key, ticker in sec_map.items():
            if len(key) >= 5 and (norm.startswith(key) or key.startswith(norm)):
                return ticker

    return ""


# =============================================================================
# MAIN SCRAPE LOGIC
# =============================================================================

def scrape_institution(
    institution: str,
    cik: str,
    conn: sqlite3.Connection,
    full: bool = False,
) -> int:
    """Scrape all (or new) 13F filings for one institution. Returns rows inserted."""

    # Find accessions already in DB
    existing = {
        row[0] for row in
        conn.execute("SELECT accession FROM filings WHERE institution = ?", (institution,))
    }

    all_accessions = fetch_all_13f_accessions(cik, institution)
    if not all_accessions:
        return 0

    new_accessions = [a for a in all_accessions if a["accession"] not in existing]
    if not full:
        # Incremental: only fetch what's new
        to_scrape = new_accessions
    else:
        to_scrape = all_accessions

    if not to_scrape:
        logger.info(f"  {institution}: up to date ({len(existing)} filings already stored)")
        return 0

    logger.info(f"  {institution}: scraping {len(to_scrape)} filing(s)")
    inserted = 0

    for meta in to_scrape:
        acc = meta["accession"]
        logger.info(f"    {meta['filing_date']}  {acc}")

        holdings = parse_holdings_from_accession(cik, acc)
        if not holdings:
            logger.debug(f"    No holdings parsed for {acc}")

        total_value = sum(h.get("value_usd", 0) for h in holdings)

        # Insert filing record
        try:
            conn.execute(
                """INSERT OR IGNORE INTO filings
                   (institution, cik, accession, form, filing_date, period_of_report,
                    scraped_at, n_holdings, total_value_usd)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    institution, cik, acc, meta["form"],
                    meta["filing_date"], meta.get("period_of_report", ""),
                    datetime.now().isoformat(), len(holdings), total_value,
                )
            )
            filing_id = conn.execute(
                "SELECT id FROM filings WHERE accession = ?", (acc,)
            ).fetchone()["id"]
        except sqlite3.IntegrityError:
            continue

        # Insert holdings
        for h in holdings:
            ticker = resolve_ticker(h.get("cusip", ""), h.get("name", ""), conn)
            conn.execute(
                """INSERT INTO holdings
                   (filing_id, institution, filing_date, cusip, name, ticker,
                    shares, value_usd, hold_type)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    filing_id, institution, meta["filing_date"],
                    h.get("cusip", ""), h.get("name", ""), ticker,
                    h.get("shares", 0), h.get("value_usd", 0), h.get("hold_type", ""),
                )
            )

        conn.commit()
        inserted += 1

    return inserted


def run_scraper(full: bool = False, institution_filter: Optional[str] = None) -> dict:
    logger.info("=" * 60)
    logger.info("  EDGAR HISTORICAL SCRAPER")
    logger.info(f"  Mode: {'FULL' if full else 'INCREMENTAL'}")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    conn = get_db()
    total_inserted = 0
    results = {}

    institutions = TRACKED_INSTITUTIONS
    if institution_filter:
        institutions = {k: v for k, v in institutions.items() if institution_filter.lower() in k.lower()}
        if not institutions:
            logger.warning(f"  No institution matching '{institution_filter}'")
            return {"status": "NO_MATCH"}

    for institution, cik in institutions.items():
        logger.info(f"\n  [{institution}]")
        try:
            n = scrape_institution(institution, cik, conn, full=full)
            results[institution] = {"inserted": n, "status": "OK"}
            total_inserted += n
        except Exception as e:
            logger.error(f"  {institution} failed: {e}")
            results[institution] = {"inserted": 0, "status": f"ERROR: {e}"}

    conn.close()

    summary = {
        "scraped_at":      datetime.now().isoformat(),
        "mode":            "FULL" if full else "INCREMENTAL",
        "total_inserted":  total_inserted,
        "institutions":    results,
    }
    logger.info(f"\n  Done — {total_inserted} new filing(s) stored in {DB_PATH}")
    return summary


def get_db_stats() -> dict:
    """Return row counts and date ranges for monitoring."""
    conn = get_db()
    stats = {}
    for table in ("filings", "holdings", "cusip_map"):
        row = conn.execute(f"SELECT COUNT(*) as n FROM {table}").fetchone()
        stats[table] = row["n"]
    date_range = conn.execute(
        "SELECT MIN(filing_date), MAX(filing_date) FROM filings"
    ).fetchone()
    stats["earliest_filing"] = date_range[0]
    stats["latest_filing"]   = date_range[1]
    conn.close()
    return stats


if __name__ == "__main__":
    full_mode   = "--full" in sys.argv
    inst_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--institution" and i + 1 < len(sys.argv):
            inst_filter = sys.argv[i + 1]

    report = run_scraper(full=full_mode, institution_filter=inst_filter)

    import json
    print(json.dumps(report, indent=2, default=str))

    stats = get_db_stats()
    print(f"\nDB stats: {stats}")

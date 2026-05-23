#!/usr/bin/env python3
"""
APEX CAPITAL — FORM 4 COLLECTOR (Insider Buying)
==================================================
SEC Form 4 is filed within 2 business days whenever a corporate insider
(director, officer, 10%+ holder) buys or sells shares. When insiders buy
their own stock at the same time institutional funds are adding positions,
that's a very strong confirmation signal.

This module:
  1. Searches SEC EDGAR full-text for Form 4 filings for our tracked tickers
  2. Stores insider transactions in the `form4_signals` table
  3. Exposes get_insider_buy_flag(ticker, days=30) for feature_engineer.py

Data is free — Form 4s are public filings on SEC EDGAR.

Schema
------
  form4_signals:
    ticker, insider_name, role, transaction_date, shares_acquired,
    shares_disposed, value_usd, filing_date, accession

Usage
-----
  python3 predictive_model/form4_collector.py [--ticker AAPL] [--days 90]
  from predictive_model.form4_collector import get_insider_buy_flag, run_form4_update
"""

import sys
import time
import json
import sqlite3
import logging
import requests
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import get_db, SEC_HEADERS, _SEC_DELAY

logger = logging.getLogger(__name__)

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


# =============================================================================
# SCHEMA
# =============================================================================

def _ensure_form4_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS form4_signals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker            TEXT    NOT NULL,
            issuer_name       TEXT,
            insider_name      TEXT,
            role              TEXT,
            transaction_date  TEXT,
            shares_acquired   INTEGER DEFAULT 0,
            shares_disposed   INTEGER DEFAULT 0,
            price_per_share   REAL,
            value_usd         REAL,
            filing_date       TEXT,
            accession         TEXT    UNIQUE,
            scraped_at        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_form4_ticker ON form4_signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_form4_date   ON form4_signals(transaction_date);
    """)
    conn.commit()


# =============================================================================
# EDGAR FORM 4 SEARCH
# =============================================================================

def _search_form4(ticker: str, days_back: int = 90) -> list:
    """
    Search SEC EDGAR full-text search for Form 4 filings mentioning a ticker.
    Returns list of filing metadata dicts.
    """
    start_date = (date.today() - timedelta(days=days_back)).isoformat()
    end_date   = date.today().isoformat()

    url = (
        f"https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{ticker}%22&dateRange=custom"
        f"&startdt={start_date}&enddt={end_date}&forms=4"
    )

    try:
        time.sleep(_SEC_DELAY)
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        results = []
        for hit in hits[:50]:
            src = hit.get("_source", {})
            results.append({
                "accession":    src.get("accession_no", "").replace("-", ""),
                "filing_date":  src.get("file_date", ""),
                "cik":          src.get("entity_id", ""),
                "issuer_name":  src.get("display_names", [""])[0] if src.get("display_names") else "",
            })
        return results
    except Exception as e:
        logger.debug(f"  form4 search error for {ticker}: {e}")
        return []


def _parse_form4_xml(accession_clean: str, cik: str) -> Optional[dict]:
    """
    Parse a Form 4 XML to extract transaction details.
    Returns dict with insider info and transaction data.
    """
    cik_int = str(int(cik)) if cik.isdigit() else cik
    base    = f"{EDGAR_ARCHIVE}/{cik_int}/{accession_clean}/"

    # Get index
    try:
        time.sleep(_SEC_DELAY)
        idx_url = base + accession_clean[:18] + "-index.json"
        r = requests.get(idx_url, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=10)
        if r.status_code != 200:
            return None
        files = r.json().get("directory", {}).get("item", [])
    except Exception:
        return None

    # Find Form 4 XML
    xml_file = None
    for f in files:
        name = f.get("name", "").lower()
        if name.endswith(".xml") and "form4" not in name and "xsl" not in name:
            xml_file = f["name"]
            break
    if not xml_file:
        for f in files:
            if f.get("name", "").endswith(".xml"):
                xml_file = f["name"]
                break
    if not xml_file:
        return None

    try:
        time.sleep(_SEC_DELAY)
        r = requests.get(base + xml_file, headers={**SEC_HEADERS, "Accept": "*/*"}, timeout=15)
        if r.status_code != 200:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "xml")

        issuer   = soup.find("issuerName")
        insider  = soup.find("rptOwnerName")
        role_tag = soup.find("officerTitle") or soup.find("isDirector")

        # Transaction table
        shares_acq  = 0
        shares_disp = 0
        price_ps    = None
        txn_date    = None

        for txn in soup.find_all("nonDerivativeTransaction"):
            acq_disp = txn.find("transactionAcquiredDisposedCode")
            shares   = txn.find("transactionShares")
            price    = txn.find("transactionPricePerShare")
            d_tag    = txn.find("transactionDate") or txn.find("periodOfReport")

            if not acq_disp or not shares:
                continue
            code = acq_disp.find("value")
            code = code.text.strip() if code else ""
            try:
                sh = int(float(shares.find("value").text.replace(",", "")))
            except Exception:
                continue

            if code == "A":
                shares_acq += sh
            elif code == "D":
                shares_disp += sh

            if price and price.find("value"):
                try:
                    price_ps = float(price.find("value").text.replace(",", ""))
                except Exception:
                    pass
            if d_tag and d_tag.find("value") and not txn_date:
                txn_date = d_tag.find("value").text.strip()[:10]

        if shares_acq == 0 and shares_disp == 0:
            return None

        value = (shares_acq or shares_disp) * (price_ps or 0)

        return {
            "issuer_name":     issuer.text.strip() if issuer else "",
            "insider_name":    insider.text.strip() if insider else "",
            "role":            role_tag.text.strip() if role_tag else "",
            "transaction_date": txn_date or "",
            "shares_acquired":  shares_acq,
            "shares_disposed":  shares_disp,
            "price_per_share":  price_ps,
            "value_usd":        value,
        }
    except Exception as e:
        logger.debug(f"  form4 parse error: {e}")
        return None


# =============================================================================
# MAIN UPDATE
# =============================================================================

def run_form4_update(tickers: list = None, days_back: int = 90) -> dict:
    """
    Fetch Form 4 insider transactions for all tickers (or a subset).
    Stores results in form4_signals table.
    """
    from config import STOCK_UNIVERSE  # type: ignore

    conn = get_db()
    _ensure_form4_table(conn)

    target_tickers = tickers or STOCK_UNIVERSE
    inserted = 0
    errors   = 0

    for ticker in target_tickers:
        logger.info(f"  Form 4: {ticker}")
        filings = _search_form4(ticker, days_back=days_back)

        for meta in filings:
            acc = meta.get("accession", "")
            if not acc:
                continue

            # Skip if already stored
            exists = conn.execute(
                "SELECT id FROM form4_signals WHERE accession = ?", (acc,)
            ).fetchone()
            if exists:
                continue

            details = _parse_form4_xml(acc, meta.get("cik", ""))
            if not details:
                continue

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO form4_signals
                       (ticker, issuer_name, insider_name, role, transaction_date,
                        shares_acquired, shares_disposed, price_per_share, value_usd,
                        filing_date, accession, scraped_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ticker,
                        details["issuer_name"],
                        details["insider_name"],
                        details["role"],
                        details["transaction_date"],
                        details["shares_acquired"],
                        details["shares_disposed"],
                        details["price_per_share"],
                        details["value_usd"],
                        meta["filing_date"],
                        acc,
                        datetime.now().isoformat(),
                    )
                )
                conn.commit()
                inserted += 1
            except Exception as e:
                logger.debug(f"  Form 4 insert error: {e}")
                errors += 1

    conn.close()
    logger.info(f"  Form 4 update done: {inserted} new transactions, {errors} errors")
    return {"inserted": inserted, "errors": errors, "tickers": len(target_tickers)}


# =============================================================================
# FEATURE HELPERS (called by feature_engineer.py)
# =============================================================================

def get_insider_buy_flag(ticker: str, lookback_days: int = 30,
                          conn: sqlite3.Connection = None) -> int:
    """
    Returns 1 if there was net insider buying for ticker in the last N days.
    Net buying = shares_acquired > shares_disposed across all insiders.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_db()
        _ensure_form4_table(conn)

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    row = conn.execute(
        """SELECT SUM(shares_acquired) as acq, SUM(shares_disposed) as disp
           FROM form4_signals
           WHERE ticker = ? AND transaction_date >= ?""",
        (ticker, cutoff)
    ).fetchone()

    if close_conn:
        conn.close()

    if not row or not row["acq"]:
        return 0
    acq  = row["acq"]  or 0
    disp = row["disp"] or 0
    return 1 if acq > disp else 0


def get_insider_net_shares(ticker: str, lookback_days: int = 30,
                            conn: sqlite3.Connection = None) -> int:
    """Net shares acquired by insiders in the last N days."""
    close_conn = conn is None
    if conn is None:
        conn = get_db()
        _ensure_form4_table(conn)

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    row = conn.execute(
        """SELECT SUM(shares_acquired) - SUM(shares_disposed) as net
           FROM form4_signals
           WHERE ticker = ? AND transaction_date >= ?""",
        (ticker, cutoff)
    ).fetchone()

    if close_conn:
        conn.close()

    if not row or row["net"] is None:
        return 0
    return int(row["net"])


def get_insider_buy_dollars(ticker: str, lookback_days: int = 30,
                             conn: sqlite3.Connection = None) -> float:
    """
    Returns log10(net_buy_dollars + 1) for insider transactions in the last N days.
    Net buy dollars = sum(value_usd for acquisitions) - sum(value_usd for disposals).
    Capped at log10($50M) = 7.699.
    Returns 0.0 if no data or net selling.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_db()
        _ensure_form4_table(conn)

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """SELECT shares_acquired, shares_disposed, price_per_share, value_usd
           FROM form4_signals
           WHERE ticker = ? AND transaction_date >= ?""",
        (ticker, cutoff)
    ).fetchall()

    if close_conn:
        conn.close()

    import math
    net_dollars = 0.0
    for row in rows:
        acq  = row["shares_acquired"]  or 0
        disp = row["shares_disposed"]  or 0
        val  = row["value_usd"]        or 0.0
        pps  = row["price_per_share"]  or 0.0
        if acq > 0 or disp > 0:
            # value_usd stores (acq or disp) * price — compute net signed value
            if pps > 0:
                net_dollars += (acq - disp) * pps
            else:
                # fallback: value_usd is unsigned, infer sign from direction
                net_dollars += val if acq > disp else -val

    if net_dollars <= 0:
        return 0.0
    capped = min(net_dollars, 50_000_000.0)
    return round(math.log10(capped + 1), 4)


def get_insider_is_ceo_cfo(ticker: str, lookback_days: int = 30,
                            conn: sqlite3.Connection = None) -> int:
    """
    Returns 1 if any net-buying insider in the last N days is CEO, CFO, or President.
    Checks role field for common officer title keywords.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_db()
        _ensure_form4_table(conn)

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """SELECT role, shares_acquired, shares_disposed
           FROM form4_signals
           WHERE ticker = ? AND transaction_date >= ?""",
        (ticker, cutoff)
    ).fetchall()

    if close_conn:
        conn.close()

    CEO_CFO_KEYWORDS = {
        "chief executive", "ceo", "chief financial", "cfo", "president",
    }

    for row in rows:
        acq  = row["shares_acquired"]  or 0
        disp = row["shares_disposed"]  or 0
        role = (row["role"] or "").lower()
        if acq > disp:  # net buyer
            if any(kw in role for kw in CEO_CFO_KEYWORDS):
                return 1

    return 0


def get_form4_stats() -> dict:
    conn = get_db()
    _ensure_form4_table(conn)
    total = conn.execute("SELECT COUNT(*) FROM form4_signals").fetchone()[0]
    tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM form4_signals").fetchone()[0]
    latest  = conn.execute("SELECT MAX(filing_date) FROM form4_signals").fetchone()[0]
    conn.close()
    return {"total_transactions": total, "distinct_tickers": tickers, "latest_filing": latest}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    ticker_filter = None
    days_arg = 90
    for i, arg in enumerate(sys.argv):
        if arg == "--ticker" and i + 1 < len(sys.argv):
            ticker_filter = [sys.argv[i + 1]]
        if arg == "--days" and i + 1 < len(sys.argv):
            days_arg = int(sys.argv[i + 1])

    result = run_form4_update(tickers=ticker_filter, days_back=days_arg)
    print(json.dumps(result, indent=2, default=str))
    print(json.dumps(get_form4_stats(), indent=2, default=str))

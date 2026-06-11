#!/usr/bin/env python3
"""
APEX CAPITAL — FE EDGAR FUNDAMENTALS
=======================================
Provides financial fundamentals for the First Eagle IV calculator.

Primary source: SEC EDGAR XBRL API (free, no key required)
Fallback:       yfinance financial statements

get_edgar_fundamentals(ticker) → dict with multi-year time series:
  revenue, operating_cf, capex, ebit, shares_outstanding,
  long_term_debt, total_assets, total_liabilities, net_income, tax_rate

get_current_price(ticker) → float
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fe_edgar_fundamentals")

DATA_DIR   = Path(__file__).parent / "data"
CACHE_DIR  = DATA_DIR / "fe_fundamentals_cache"
CACHE_TTL  = timedelta(hours=24)   # fundamentals change slowly

EDGAR_BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"

# ── Module-level ticker→CIK cache ────────────────────────────────────────────
_TICKER_CIK_CACHE: dict[str, str] = {}


def _load_cache(ticker: str) -> Optional[dict]:
    path = CACHE_DIR / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > CACHE_TTL:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(ticker: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker.upper()}.json"
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _get_cik(ticker: str) -> Optional[str]:
    """Look up SEC CIK for a ticker symbol."""
    global _TICKER_CIK_CACHE
    tu = ticker.upper()

    if tu in _TICKER_CIK_CACHE:
        return _TICKER_CIK_CACHE[tu]

    # Try local cache first
    cik_cache_path = DATA_DIR / "fe_cik_map.json"
    if cik_cache_path.exists():
        try:
            cik_map = json.load(open(cik_cache_path))
            if tu in cik_map:
                _TICKER_CIK_CACHE[tu] = cik_map[tu]
                return cik_map[tu]
        except Exception:
            pass

    # Fetch from SEC
    try:
        import urllib.request
        headers = {"User-Agent": "ApexCapital research@apexcapital.com"}
        req = urllib.request.Request(EDGAR_TICKERS, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        # Build map and save
        cik_map = {}
        for entry in data.values():
            t = str(entry.get("ticker", "")).upper()
            c = str(entry.get("cik_str", "")).zfill(10)
            if t:
                cik_map[t] = c
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(cik_cache_path, "w") as f:
            json.dump(cik_map, f)
        _TICKER_CIK_CACHE.update(cik_map)
        return cik_map.get(tu)
    except Exception as e:
        logger.debug(f"  [EDGAR] CIK lookup failed for {ticker}: {e}")
        return None


def _extract_concept(facts: dict, *concept_names: str) -> list[float]:
    """
    Pull a financial concept from EDGAR facts JSON.
    Returns list of annual values, oldest first.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for name in concept_names:
        concept = us_gaap.get(name, {})
        units = concept.get("units", {})
        # Try USD first
        for unit_key in ("USD", "shares"):
            entries = units.get(unit_key, [])
            # Filter to annual 10-K filings only
            annual = [
                e for e in entries
                if e.get("form") in ("10-K", "20-F", "40-F")
                and e.get("val") is not None
            ]
            if annual:
                # Sort by end date, deduplicate by fiscal year
                annual.sort(key=lambda x: x.get("end", ""))
                seen_years = {}
                for e in annual:
                    year = e.get("end", "")[:4]
                    seen_years[year] = e.get("val")
                return [float(v) for v in seen_years.values()]
    return []


def _fetch_edgar_fundamentals(ticker: str, cik: str) -> Optional[dict]:
    """Fetch and parse EDGAR XBRL facts for a given CIK."""
    try:
        import urllib.request
        url = EDGAR_BASE.format(cik=cik)
        headers = {"User-Agent": "ApexCapital research@apexcapital.com"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            facts = json.loads(resp.read())
        time.sleep(0.1)  # respectful rate limiting
    except Exception as e:
        logger.debug(f"  [EDGAR] Facts fetch failed for {ticker} (CIK={cik}): {e}")
        return None

    return {
        "revenue":             _extract_concept(facts, "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
        "operating_cf":        _extract_concept(facts, "NetCashProvidedByUsedInOperatingActivities"),
        "capex":               _extract_concept(facts, "PaymentsToAcquirePropertyPlantAndEquipment", "CapitalExpendituresIncurredButNotYetPaid"),
        "ebit":                _extract_concept(facts, "OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"),
        "net_income":          _extract_concept(facts, "NetIncomeLoss"),
        "shares_outstanding":  _extract_concept(facts, "CommonStockSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"),
        "long_term_debt":      _extract_concept(facts, "LongTermDebt", "LongTermDebtNoncurrent"),
        "total_assets":        _extract_concept(facts, "Assets"),
        "total_liabilities":   _extract_concept(facts, "Liabilities"),
        "source":              "EDGAR",
        "cik":                 cik,
    }


def _fetch_yfinance_fundamentals(ticker: str) -> Optional[dict]:
    """Fallback: use yfinance financial statements for fundamentals."""
    try:
        import yfinance as yf
        import pandas as pd
        t = yf.Ticker(ticker)

        inc   = t.financials      # income statement (annual)
        cf    = t.cashflow        # cash flow statement
        bal   = t.balance_sheet   # balance sheet
        info  = t.info or {}

        def _series(df, *keys):
            if df is None or df.empty:
                return []
            for k in keys:
                if k in df.index:
                    vals = df.loc[k].dropna().tolist()
                    return [float(v) for v in reversed(vals) if v == v]
            return []

        return {
            "revenue":            _series(inc, "Total Revenue"),
            "operating_cf":       _series(cf,  "Operating Cash Flow", "Total Cash From Operating Activities"),
            "capex":              [abs(v) for v in _series(cf, "Capital Expenditures")],
            "ebit":               _series(inc, "EBIT", "Operating Income"),
            "net_income":         _series(inc, "Net Income"),
            "shares_outstanding": [info.get("sharesOutstanding", 0)] if info.get("sharesOutstanding") else [],
            "long_term_debt":     _series(bal, "Long Term Debt"),
            "total_assets":       _series(bal, "Total Assets"),
            "total_liabilities":  _series(bal, "Total Liabilities Net Minority Interest", "Total Liabilities"),
            "source":             "YFINANCE",
            "cik":                None,
        }
    except Exception as e:
        logger.debug(f"  [EDGAR] yfinance fallback failed for {ticker}: {e}")
        return None


def get_edgar_fundamentals(ticker: str) -> Optional[dict]:
    """
    Get multi-year financial fundamentals for a ticker.
    Tries EDGAR XBRL first, falls back to yfinance statements.
    Returns None if both sources fail.
    """
    cached = _load_cache(ticker)
    if cached:
        logger.debug(f"  [EDGAR] Using cached fundamentals for {ticker}")
        return cached

    # Try EDGAR first
    cik = _get_cik(ticker)
    result = None
    if cik:
        logger.debug(f"  [EDGAR] Fetching EDGAR facts for {ticker} (CIK={cik})")
        result = _fetch_edgar_fundamentals(ticker, cik)

    # Fall back to yfinance
    if not result or not any(v for v in result.values() if isinstance(v, list) and v):
        logger.debug(f"  [EDGAR] Falling back to yfinance for {ticker}")
        result = _fetch_yfinance_fundamentals(ticker)

    if result:
        _save_cache(ticker, result)

    return result


def get_current_price(ticker: str) -> Optional[float]:
    """Fetch current market price for a ticker."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if not hist.empty:
            closes = hist["Close"].dropna()
            if not closes.empty:
                return float(closes.iloc[-1])
        info = t.info or {}
        for field in ("regularMarketPrice", "currentPrice", "previousClose"):
            v = info.get(field)
            if v:
                return float(v)
    except Exception as e:
        logger.debug(f"  [EDGAR] Price fetch failed for {ticker}: {e}")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for ticker in ["CVS", "WBA", "PFE", "JNJ", "KO"]:
        fund = get_edgar_fundamentals(ticker)
        price = get_current_price(ticker)
        if fund:
            rev = fund.get("revenue", [])
            ocf = fund.get("operating_cf", [])
            print(f"{ticker:6s}: price=${price or 0:.2f}  revenue_yrs={len(rev)}  ocf_yrs={len(ocf)}  source={fund.get('source','?')}")
        else:
            print(f"{ticker:6s}: fundamentals unavailable")

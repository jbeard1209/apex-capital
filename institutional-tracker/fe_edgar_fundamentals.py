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
    data = {**data, "fetched_at": datetime.now().isoformat(timespec="seconds")}
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


def _annual_span_days(e: dict) -> Optional[int]:
    """Days between an entry's start and end. None if it isn't a duration fact."""
    s, en = e.get("start"), e.get("end")
    if not s or not en:
        return None
    try:
        d0 = datetime.strptime(s, "%Y-%m-%d")
        d1 = datetime.strptime(en, "%Y-%m-%d")
        return (d1 - d0).days
    except Exception:
        return None


def _extract_concept(facts: dict, *concept_names: str, flow: bool = True) -> list[float]:
    """
    Pull a financial concept from EDGAR facts JSON. Returns annual values, oldest first.

    CRITICAL: a 10-K's XBRL carries the full-year fact *and* embedded quarterly /
    comparative-period facts, ALL tagged form="10-K". Filtering by form alone lets a
    3-month Q4 value overwrite the 12-month one (period-mixing bug — produced ABBV
    P/E 256, INCY debt/assets 2.74). So for `flow` (duration) concepts we additionally
    require the period span to be ~annual (330-400 days). `instant` concepts (shares,
    debt, assets — balance-sheet snapshots) have no start date and are kept as-is.
    """
    for name in concept_names:
        annual = _collect_annual(facts, name, flow)
        if annual:
            return _dedup_by_year(annual)
    return []


def _collect_annual(facts: dict, name: str, flow: bool) -> list[dict]:
    """Gather annual 10-K entries for one concept, searching BOTH the us-gaap and
    dei taxonomies. Many issuers (Visa, Kellanova, banks) report shares outstanding
    only on the cover page as dei:EntityCommonStockSharesOutstanding, absent from
    us-gaap — searching us-gaap alone silently returns zero shares → undefined P/E."""
    taxes = facts.get("facts", {})
    annual = []
    for tax in ("us-gaap", "dei"):
        units = taxes.get(tax, {}).get(name, {}).get("units", {})
        if not units:
            continue
        for unit_key in ("USD", "shares"):
            for e in units.get(unit_key, []):
                if e.get("form") not in ("10-K", "20-F", "40-F") or e.get("val") is None:
                    continue
                if flow:
                    span = _annual_span_days(e)
                    if span is None or not (330 <= span <= 400):
                        continue  # skip quarterly / non-annual periods
                annual.append(e)
        if annual:
            break  # first taxonomy that has the concept wins
    return annual


def _dedup_by_year(annual: list[dict]) -> list[float]:
    """Newest filing wins per fiscal year; return values oldest-first."""
    annual.sort(key=lambda x: (x.get("end", ""), x.get("filed", "")))
    seen_years = {}
    for e in annual:
        seen_years[e.get("end", "")[:4]] = e.get("val")
    return [float(v) for v in seen_years.values()]


def _extract_by_year(facts: dict, *concept_names: str, flow: bool = True) -> dict:
    """Like _extract_concept but returns {fiscal_year: value} so callers can ALIGN
    series across metrics. The unlabeled-array form silently misaligns when metrics
    have different history lengths (INCY: 16 asset-years vs 8 debt-years → [-1] of
    each is a different year). Keyed output makes cross-metric ratios year-safe."""
    for name in concept_names:
        annual = _collect_annual(facts, name, flow)
        if annual:
            annual.sort(key=lambda x: (x.get("end", ""), x.get("filed", "")))
            return {e.get("end", "")[:4]: float(e.get("val")) for e in annual}
    return {}


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

    specs = {
        "revenue":            (("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"), True),
        "operating_cf":       (("NetCashProvidedByUsedInOperatingActivities",), True),
        "capex":              (("PaymentsToAcquirePropertyPlantAndEquipment", "CapitalExpendituresIncurredButNotYetPaid"), True),
        "ebit":               (("OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"), True),
        "net_income":         (("NetIncomeLoss",), True),
        "shares_outstanding": (("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"), False),
        "long_term_debt":     (("LongTermDebt", "LongTermDebtNoncurrent"), False),
        "total_assets":       (("Assets",), False),
        "total_liabilities":  (("Liabilities",), False),
    }
    out = {"source": "EDGAR", "cik": cik, "by_year": {}}
    for field, (names, flow) in specs.items():
        by_year = _extract_by_year(facts, *names, flow=flow)
        out[field] = [by_year[y] for y in sorted(by_year)]  # legacy oldest-first list
        out["by_year"][field] = by_year                     # year-keyed for alignment
    return out


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

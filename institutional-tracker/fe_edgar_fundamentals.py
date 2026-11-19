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
# Bump when the meaning of a cached field changes. Entries without this version
# are refetched: before v2 (2026-11-19) shares_outstanding could be a stale,
# other-class count (BRK-B: 943,242 Class A shares from 2011), and every FE
# consumer would keep reading it for up to CACHE_TTL after a deploy.
CACHE_SCHEMA = 2
# A field whose latest fiscal year lags the filing's latest year by more than
# this is a concept the issuer stopped using, not a current number.
MAX_FIELD_LAG_YEARS = 1
# If even the freshest core field is older than this, EDGAR has stopped
# covering the issuer in us-gaap (AEM moved to IFRS after 2013) -> yfinance.
MAX_EDGAR_AGE_YEARS = 2
# EDGAR vs market-implied share count disagreeing by more than this ratio is a
# units problem (share class, ADR ratio), not buybacks.
SHARE_UNITS_TOLERANCE = 1.5

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
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema", 0) < CACHE_SCHEMA:
        return None
    return data


def _save_cache(ticker: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker.upper()}.json"
    data = {**data, "schema": CACHE_SCHEMA,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}
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
    by_year = _extract_by_year(facts, *concept_names, flow=flow)
    return [by_year[y] for y in sorted(by_year)]


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


def _extract_by_year(facts: dict, *concept_names: str, flow: bool = True) -> dict:
    """Like _extract_concept but returns {fiscal_year: value} so callers can ALIGN
    series across metrics. The unlabeled-array form silently misaligns when metrics
    have different history lengths (INCY: 16 asset-years vs 8 debt-years → [-1] of
    each is a different year). Keyed output makes cross-metric ratios year-safe.

    Concepts are MERGED per year in priority order (2026-11-19). This used to
    return the first concept that had any data at all, however old: issuers switch
    tags, so MCO's revenue stopped at 2017 ("Revenues" → RevenueFromContract...),
    Berkshire's EBIT at 2012, and those stale years were valued next to current
    ones. Now each year takes the highest-priority concept that reports it."""
    merged: dict = {}
    for name in concept_names:
        annual = _collect_annual(facts, name, flow)
        if not annual:
            continue
        annual.sort(key=lambda x: (x.get("end", ""), x.get("filed", "")))
        this = {e.get("end", "")[:4]: float(e.get("val")) for e in annual}
        for y, v in this.items():
            merged.setdefault(y, v)
    return dict(sorted(merged.items()))


_CORE_FIELDS = ("revenue", "operating_cf", "net_income", "ebit", "total_assets")


def _drop_stale_fields(out: dict) -> dict:
    """Empty any field whose newest year lags the filing's newest year.

    A per-share value is only meaningful if numerator and share count describe
    the same year. BRK-B's share count was a 2011 cover-page number divided into
    2025 earnings ($144,538 'intrinsic value' vs a $571 price). Missing is
    honest: every consumer already handles an empty field as 'unknown'."""
    by_year = out.get("by_year", {})
    core_latest = [int(max(by_year[f])) for f in _CORE_FIELDS if by_year.get(f)]
    if not core_latest:
        out["latest_fiscal_year"] = None
        return out
    anchor = max(core_latest)
    out["latest_fiscal_year"] = anchor
    stale = {}
    for field, series in list(by_year.items()):
        if not series:
            continue
        last = int(max(series))
        if last < anchor - MAX_FIELD_LAG_YEARS:
            stale[field] = last
            by_year[field] = {}
            out[field] = []
    if stale:
        out["stale_fields"] = stale
        logger.info(f"  [EDGAR] {out.get('cik')}: dropped stale fields {stale} (filing year {anchor})")
    return out


def _edgar_is_stale(result: Optional[dict], today: Optional[datetime] = None) -> bool:
    """EDGAR financials that stopped years ago (IFRS switch, delisting) are not a
    basis for a current valuation. AEM: us-gaap ends 2013 → IV $3.51 vs $159."""
    if not result or result.get("source") != "EDGAR":
        return False
    year = result.get("latest_fiscal_year")
    today = today or datetime.now()
    return year is None or year < today.year - MAX_EDGAR_AGE_YEARS


def _market_implied_shares(ticker: str) -> Optional[float]:
    """Whole-company share count expressed in units of THIS ticker's price.

    marketCap / price is the one count that is by construction consistent with
    the price we compare IV against: for BRK-B it is ~2.12B B-equivalents (A
    shares x 1,500 + B shares), for GOOGL both classes, for an ADR the ADR
    count. yfinance's `sharesOutstanding` is a single class (BRK-B 1.40B,
    GOOGL 5.9B of 12.3B) and is the wrong denominator for multi-class issuers."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.debug(f"  [EDGAR] market-implied shares unavailable for {ticker}: {e}")
        return None
    mcap = info.get("marketCap")
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    try:
        if mcap and price and float(price) > 0:
            return float(mcap) / float(price)
        implied = info.get("impliedSharesOutstanding") or info.get("sharesOutstanding")
        return float(implied) if implied else None
    except (TypeError, ValueError):
        return None


def _reconcile_shares(result: dict, ticker: str) -> dict:
    """Make shares_outstanding[-1] the count for the class being priced.

    - EDGAR has no current share count (dropped as stale) → use market-implied.
    - EDGAR's count disagrees with market-implied by > SHARE_UNITS_TOLERANCE →
      a units problem (share class / ADR ratio): rescale the WHOLE series by the
      ratio so year-over-year dilution checks still see the real trend.
    - Otherwise keep EDGAR (it is the audited number) and record agreement.
    No market data → keep what EDGAR has, flagged unverified; the valuation's
    plausibility guard is the backstop."""
    market = _market_implied_shares(ticker)
    series = result.get("shares_outstanding") or []
    by_year = result.setdefault("by_year", {}).get("shares_outstanding") or {}
    latest = next((v for v in reversed(series) if v and v > 0), None)
    if not market or market <= 0:
        result["shares_source"] = "EDGAR_UNVERIFIED" if latest else "NONE"
        return result
    if not latest:
        year = str(result.get("latest_fiscal_year") or datetime.now().year)
        result["shares_outstanding"] = [market]
        result["by_year"]["shares_outstanding"] = {year: market}
        result["shares_source"] = "MARKET_IMPLIED"
        return result
    ratio = market / latest
    if ratio > SHARE_UNITS_TOLERANCE or ratio < 1.0 / SHARE_UNITS_TOLERANCE:
        result["shares_outstanding"] = [v * ratio if v else v for v in series]
        result["by_year"]["shares_outstanding"] = {y: v * ratio for y, v in by_year.items()}
        result["shares_source"] = "EDGAR_RESCALED_TO_QUOTED_CLASS"
        result["shares_scale"] = round(ratio, 6)
        logger.info(f"  [EDGAR] {ticker}: EDGAR shares {latest:,.0f} vs market-implied "
                    f"{market:,.0f} — rescaled x{ratio:,.4f} (share class / ADR units)")
    else:
        result["shares_source"] = "EDGAR"
    return result


def _fetch_facts_json(ticker: str, cik: str) -> Optional[dict]:
    """Raw SEC companyfacts JSON for a CIK (network only, no parsing)."""
    try:
        import urllib.request
        url = EDGAR_BASE.format(cik=cik)
        headers = {"User-Agent": "ApexCapital research@apexcapital.com"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            facts = json.loads(resp.read())
        time.sleep(0.1)  # respectful rate limiting
        return facts
    except Exception as e:
        logger.debug(f"  [EDGAR] Facts fetch failed for {ticker} (CIK={cik}): {e}")
        return None


def _fetch_edgar_fundamentals(ticker: str, cik: str) -> Optional[dict]:
    """Fetch and parse EDGAR XBRL facts for a given CIK."""
    facts = _fetch_facts_json(ticker, cik)
    if not facts:
        return None

    specs = {
        "revenue":            (("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"), True),
        "operating_cf":       (("NetCashProvidedByUsedInOperatingActivities",), True),
        "capex":              (("PaymentsToAcquirePropertyPlantAndEquipment", "CapitalExpendituresIncurredButNotYetPaid"), True),
        # Pre-tax income tags are the fallback for issuers that report no
        # operating-income line (JNJ, banks, insurers); the long names are what
        # those issuers actually use — without them JNJ's EBIT ended in 2014.
        "ebit":               (("OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
                                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"), True),
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
    return _drop_stale_fields(out)


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

        _mcap = info.get("marketCap")
        _px = info.get("currentPrice") or info.get("regularMarketPrice")
        _shares = (float(_mcap) / float(_px)) if (_mcap and _px) else (
            info.get("impliedSharesOutstanding") or info.get("sharesOutstanding"))

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
            # yfinance renamed the row to the singular; with only the old name,
            # capex was always [] and FCF silently became raw operating cash flow.
            "capex":              [abs(v) for v in _series(cf, "Capital Expenditure", "Capital Expenditures")],
            "ebit":               _series(inc, "EBIT", "Operating Income"),
            "net_income":         _series(inc, "Net Income"),
            # marketCap/price, not sharesOutstanding: the latter is ONE class
            # (BRK-B 1.40B of 2.12B B-equivalents) — see _market_implied_shares.
            "shares_outstanding": [_shares] if _shares else [],
            "long_term_debt":     _series(bal, "Long Term Debt"),
            "total_assets":       _series(bal, "Total Assets"),
            "total_liabilities":  _series(bal, "Total Liabilities Net Minority Interest", "Total Liabilities"),
            "source":             "YFINANCE",
            "cik":                None,
            # Statements are in this currency; prices are USD. Consumers must
            # refuse non-USD (SBS/Sabesp 2026-07-03; TSM reports in TWD).
            "financial_currency": str(info.get("financialCurrency") or "USD").upper(),
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

    if _edgar_is_stale(result):
        logger.info(f"  [EDGAR] {ticker}: EDGAR financials end {result.get('latest_fiscal_year')} "
                    "— stale (IFRS switch?), falling back to yfinance")
        result = None

    # Fall back to yfinance
    if not result or not any(v for v in result.values() if isinstance(v, list) and v):
        logger.debug(f"  [EDGAR] Falling back to yfinance for {ticker}")
        result = _fetch_yfinance_fundamentals(ticker)
        if result:
            result.setdefault("shares_source", "MARKET_IMPLIED")
    elif result.get("source") == "EDGAR":
        result = _reconcile_shares(result, ticker)

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

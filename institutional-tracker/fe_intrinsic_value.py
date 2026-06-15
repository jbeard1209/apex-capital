#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE INTRINSIC VALUE CALCULATOR
=======================================================
Two-method IV calculation using SEC EDGAR XBRL fundamentals.

Method 1 -- DCF:
  - 3yr avg FCF (operating_cf - capex) from EDGAR
  - Growth rate: 5yr revenue CAGR capped at 8%
  - Discount rate: 10% (First Eagle required return)
  - Terminal value: FCF_year10 * (1.03) / (0.10 - 0.03)
  - IV = sum(discounted FCFs) + discounted terminal value / shares outstanding

Method 2 -- Earnings Power Value (EPV):
  - Normalized EBIT * (1 - tax_rate) / WACC
  - Conservative -- assumes no growth, just steady-state earnings

Conservative IV = min(DCF IV, EPV IV) -- First Eagle uses the lower.
Margin of Safety = (conservative_iv - current_price) / conservative_iv

Usage:
  from fe_intrinsic_value import calculate_iv
  result = calculate_iv("FICO")
"""

from __future__ import annotations

import logging
import warnings
from typing import Optional

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)

# First Eagle standard parameters
DISCOUNT_RATE      = 0.10
TERMINAL_GROWTH    = 0.03
MAX_GROWTH_RATE    = 0.08
DCF_YEARS          = 10
MOS_BUY_THRESHOLD  = 0.25
MOS_HOLD_THRESHOLD = 0.00
# Above this discount, an estimate is more likely a model error or special situation
# than a real bargain — flag REVIEW (human eyes), don't auto-BUY.
REVIEW_CEILING     = 0.50


def _safe_float(val, default=None) -> Optional[float]:
    if val is None:
        return default
    try:
        f = float(val)
        return default if f != f else f   # NaN check
    except (TypeError, ValueError):
        return default


def _dcf_iv(fcf: float, growth_rate: float, shares: float, net_debt: float = 0.0) -> Optional[float]:
    if fcf <= 0 or shares <= 0:
        return None
    pv_total = 0.0
    cf = fcf
    for yr in range(1, 6):
        cf *= (1 + growth_rate)
        pv_total += cf / ((1 + DISCOUNT_RATE) ** yr)
    decay = growth_rate * 0.5
    for yr in range(6, DCF_YEARS + 1):
        cf *= (1 + decay)
        pv_total += cf / ((1 + DISCOUNT_RATE) ** yr)
    terminal_fcf = cf * (1 + TERMINAL_GROWTH)
    terminal_val = terminal_fcf / (DISCOUNT_RATE - TERMINAL_GROWTH)
    pv_terminal  = terminal_val / ((1 + DISCOUNT_RATE) ** DCF_YEARS)
    # The discounted cash flows value the whole ENTERPRISE. Equity owners are last
    # in line — subtract what the company owes (net debt) before dividing per share,
    # or a heavily-indebted firm looks far cheaper than the equity is actually worth.
    equity_val = (pv_total + pv_terminal) - max(0.0, net_debt)
    return equity_val / shares if equity_val > 0 else None


def _epv_iv(ebit: float, tax_rate: float, wacc: float, shares: float, net_debt: float = 0.0) -> Optional[float]:
    if ebit <= 0 or shares <= 0 or wacc <= 0:
        return None
    nopat = ebit * (1 - max(0.10, min(0.40, tax_rate)))
    # nopat/wacc is the ENTERPRISE earnings-power value; subtract net debt for equity.
    equity_val = (nopat / wacc) - max(0.0, net_debt)
    return equity_val / shares if equity_val > 0 else None


def _revenue_cagr(revenue_list: list, years: int = 5) -> float:
    """Compute revenue CAGR from last N years of data."""
    non_null = [(i, v) for i, v in enumerate(revenue_list) if v and v > 0]
    if len(non_null) < 2:
        return 0.04   # conservative default
    # Use the most recent `years` span
    subset = non_null[-max(2, min(years + 1, len(non_null))):]
    start_val = subset[0][1]
    end_val   = subset[-1][1]
    n_years   = subset[-1][0] - subset[0][0]
    if n_years <= 0 or start_val <= 0:
        return 0.04
    cagr = (end_val / start_val) ** (1.0 / n_years) - 1.0
    return max(0.01, min(cagr, MAX_GROWTH_RATE))


def _calculate_iv_yfinance_fallback(ticker: str, current_price: float) -> Optional[dict]:
    """
    Fallback IV calculation using yfinance for tickers without EDGAR coverage
    (OTC ADRs, international stocks, etc.).  Uses trailing EPS for EPV and
    freeCashflow / sharesOutstanding for DCF when available.
    Results are marked data_source='YFINANCE_FALLBACK' and treated as
    supplementary — board still applies normal MoS filter.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
    except Exception as e:
        logger.warning(f"[IV] yfinance fallback failed for {ticker}: {e}")
        return None

    shares   = _safe_float(info.get("sharesOutstanding"))
    eps      = _safe_float(info.get("trailingEps"))
    fcf_raw  = _safe_float(info.get("freeCashflow"))
    pb       = _safe_float(info.get("priceToBook"))
    book     = _safe_float(info.get("bookValue"))

    if not shares or shares <= 0:
        logger.warning(f"[IV] yfinance fallback: no shares for {ticker}")
        return None

    # EPV from trailing EPS (normalized earnings power, no-growth assumption)
    epv_iv = None
    if eps and eps > 0:
        # NOPAT proxy: EPS * (1 - 0.25 tax) / WACC
        nopat_per_share = eps * (1 - 0.25)
        epv_iv = nopat_per_share / DISCOUNT_RATE

    # DCF from yfinance freeCashflow aggregate
    dcf_iv = None
    if fcf_raw and fcf_raw > 0:
        # Conservative growth: assume 3% (terminal growth = floor, no analyst estimates)
        dcf_iv = _dcf_iv(fcf_raw, TERMINAL_GROWTH, shares)

    # Book value floor: for financial companies (banks/insurers), P/B < 1.5 is value territory
    book_iv = None
    if book and book > 0:
        book_iv = book * 1.2   # slight premium to book for franchise value

    # Keep only non-data-error methods (>10% of price); same elite corroboration
    # rule as the EDGAR path so the fallback isn't a loophole.
    methods = {k: v for k, v in {"dcf": dcf_iv, "epv": epv_iv, "book": book_iv}.items()
               if v and v > 0.10 * current_price}
    if not methods:
        logger.warning(f"[IV] yfinance fallback: could not compute any IV for {ticker}")
        return None

    conservative_iv = min(methods.values())   # cautious anchor
    _d, _e = methods.get("dcf"), methods.get("epv")
    have_two = _d is not None and _e is not None
    agree = have_two and min(_d, _e) >= 0.60 * max(_d, _e)

    if conservative_iv > 5 * current_price:
        logger.warning(f"[IV] {ticker}: IV ${conservative_iv:,.0f} > 5x price "
                       f"${current_price:.2f} — implausible, likely a data error; skipping")
        return None

    mos = (conservative_iv - current_price) / conservative_iv if conservative_iv > 0 else -1.0

    if agree and MOS_BUY_THRESHOLD <= mos < REVIEW_CEILING:
        verdict = "BUY"
    elif mos >= MOS_BUY_THRESHOLD:
        verdict = "REVIEW"
    elif mos >= MOS_HOLD_THRESHOLD:
        verdict = "HOLD"
    else:
        verdict = "OVERVALUED"

    fcf_yield = (fcf_raw / (current_price * shares)) if (fcf_raw and shares and current_price) else None

    result = {
        "ticker":           ticker,
        "current_price":    round(current_price, 2),
        "dcf_iv":           round(dcf_iv, 2) if dcf_iv else None,
        "epv_iv":           round(epv_iv, 2) if epv_iv else None,
        "conservative_iv":  round(conservative_iv, 2),
        "margin_of_safety": round(mos, 4),
        "verdict":          verdict,
        "fcf_yield":        round(fcf_yield, 4) if fcf_yield is not None else None,
        "roic":             None,
        "net_debt_ebitda":  None,
        "growth_rate_used": TERMINAL_GROWTH,
        "avg_fcf_used":     round(fcf_raw, 0) if fcf_raw else None,
        "data_source":      "YFINANCE_FALLBACK",
    }

    logger.info(
        f"[IV] {ticker}: price=${current_price:.2f} | IV=${conservative_iv:.2f} | "
        f"MoS={mos*100:.1f}% | {verdict} | YFINANCE_FALLBACK"
    )
    return result


def calculate_iv(ticker: str) -> Optional[dict]:
    """
    Calculate intrinsic value using EDGAR XBRL fundamentals + yfinance price.
    Falls back to yfinance-only calculation for tickers without EDGAR coverage
    (international ADRs, OTC stocks).
    Returns result dict or None on failure.
    """
    # Import EDGAR data layer
    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals, get_current_price
    except ImportError as e:
        logger.error(f"[IV] Could not import fe_edgar_fundamentals: {e}")
        return None

    # Fetch EDGAR fundamentals
    try:
        fundamentals = get_edgar_fundamentals(ticker)
    except Exception as e:
        logger.warning(f"[IV] EDGAR fetch failed for {ticker}: {e}")
        fundamentals = None

    # Current price via yfinance
    try:
        current_price = get_current_price(ticker)
    except Exception as e:
        logger.warning(f"[IV] Price fetch failed for {ticker}: {e}")
        current_price = None

    if not current_price:
        logger.warning(f"[IV] No price for {ticker}")
        return None

    if not fundamentals:
        logger.info(f"[IV] No EDGAR data for {ticker} — trying yfinance fallback")
        return _calculate_iv_yfinance_fallback(ticker, current_price)

    rev_list  = fundamentals.get("revenue", [])
    ocf_list  = fundamentals.get("operating_cf", [])
    capex_list = fundamentals.get("capex", [])
    ebit_list  = fundamentals.get("ebit", [])
    shares_list = fundamentals.get("shares_outstanding", [])
    debt_list  = fundamentals.get("long_term_debt", [])
    assets_list = fundamentals.get("total_assets", [])
    liab_list  = fundamentals.get("total_liabilities", [])

    # ── Shares outstanding ────────────────────────────────────────────────────
    shares = None
    for v in reversed(shares_list):
        if v and v > 0:
            shares = float(v)
            break
    if not shares:
        logger.warning(f"[IV] No shares outstanding for {ticker}")
        return None

    # ── 3yr avg FCF (operating_cf - capex) ───────────────────────────────────
    fcf_vals = []
    for ocf, capex in zip(ocf_list, capex_list):
        if ocf is not None and capex is not None:
            fcf_vals.append(ocf - capex)
        elif ocf is not None:
            fcf_vals.append(ocf)   # capex unknown, use OCF conservatively
    recent_fcf = [v for v in fcf_vals[-3:] if v is not None]
    avg_fcf = sum(recent_fcf) / len(recent_fcf) if recent_fcf else None

    # ── 5yr revenue CAGR ─────────────────────────────────────────────────────
    growth_rate = _revenue_cagr(rev_list, years=5)

    # ── Net debt — equity owners are paid after creditors ─────────────────────
    # Subtract what the company owes in every method, else a heavily-indebted firm
    # (cable, casinos) looks far cheaper than the equity is actually worth.
    net_debt = next((v for v in reversed(debt_list) if v is not None), 0.0) or 0.0

    # ── DCF IV (debt-adjusted) ────────────────────────────────────────────────
    dcf_iv = None
    if avg_fcf and avg_fcf > 0:
        dcf_iv = _dcf_iv(avg_fcf, growth_rate, shares, net_debt)

    # ── EPV IV (debt-adjusted, no-growth earnings power) ──────────────────────
    epv_iv = None
    recent_ebit = [v for v in ebit_list[-3:] if v is not None]
    avg_ebit = sum(recent_ebit) / len(recent_ebit) if recent_ebit else None
    if avg_ebit and avg_ebit > 0:
        epv_iv = _epv_iv(avg_ebit, 0.21, DISCOUNT_RATE, shares, net_debt)

    # ── Method corroboration (ELITE standard) ─────────────────────────────────
    # Keep only methods that aren't an absolute data error (>10% of price — catches
    # collapsed-DCF $1 cases without discarding a legitimately-low, cautious EPV).
    methods = {k: v for k, v in {"dcf": dcf_iv, "epv": epv_iv}.items()
               if v and v > 0.10 * current_price}
    book_iv = None
    _a = next((v for v in reversed(assets_list) if v), None)
    _l = next((v for v in reversed(liab_list) if v), None)
    if _a and _l and _a > _l and shares:
        book_iv = (_a - _l) / shares     # tangible-ish net worth per share, no premium
    # Book value is a single-method fallback for financials only — it can corroborate
    # but cannot, alone, clear the BUY bar (it's not an earnings-based valuation).
    if book_iv and book_iv > 0.10 * current_price:
        methods["book"] = book_iv

    if not methods:
        logger.warning(f"[IV] Could not compute IV for {ticker}")
        return None

    dcf_iv = methods.get("dcf"); epv_iv = methods.get("epv")
    # Anchor on the CAUTIOUS estimate (lowest credible method).
    conservative_iv = min(methods.values())

    # CORROBORATION — the heart of the elite standard. The two earnings-based methods
    # (DCF = with growth, EPV = no-growth) must AGREE within a sane band. If they
    # disagree wildly we genuinely don't know the value — that's not a buy, it's a
    # question mark. This single rule rejects both failure modes seen in testing:
    #   GM  dcf $278 vs epv $73  (peak-cycle DCF fantasy)   -> disagree -> no buy
    #   KHC dcf $41  vs epv $3.5 (debt-crushed EPV artifact) -> disagree -> no buy
    have_two = dcf_iv is not None and epv_iv is not None
    agree = have_two and min(dcf_iv, epv_iv) >= 0.60 * max(dcf_iv, epv_iv)

    # Upper sanity guard — >5x price is almost always a units/data error.
    if conservative_iv > 5 * current_price:
        logger.warning(f"[IV] {ticker}: IV ${conservative_iv:,.0f} > 5x price "
                       f"${current_price:.2f} — implausible, likely a data error; skipping")
        return None

    mos = (conservative_iv - current_price) / conservative_iv if conservative_iv > 0 else -1.0

    # ── ELITE verdict tiers ───────────────────────────────────────────────────
    # BUY only when DCF & EPV corroborate AND the discount is a believable 25-50%.
    # Anything cheap-but-uncorroborated, single-method, or implausibly deep is REVIEW
    # (a human decides), never auto-bought. Few names pass — that's the point.
    if agree and MOS_BUY_THRESHOLD <= mos < REVIEW_CEILING:
        verdict = "BUY"
    elif mos >= MOS_BUY_THRESHOLD:
        verdict = "REVIEW"      # cheap, but methods disagree / single-method / too deep
    elif mos >= MOS_HOLD_THRESHOLD:
        verdict = "HOLD"
    else:
        verdict = "OVERVALUED"

    # ── Supplementary metrics ─────────────────────────────────────────────────
    # FCF yield approximated
    recent_ocf  = next((v for v in reversed(ocf_list) if v is not None), None)
    recent_capex = next((v for v in reversed(capex_list) if v is not None), None)
    latest_fcf  = (recent_ocf - (recent_capex or 0)) if recent_ocf else None
    market_cap  = current_price * shares if current_price and shares else None
    fcf_yield   = (latest_fcf / market_cap) if (latest_fcf and market_cap and market_cap > 0) else None

    # ROIC
    roic = None
    recent_assets = next((v for v in reversed(assets_list) if v is not None), None)
    recent_liab   = next((v for v in reversed(liab_list) if v is not None), None)
    if avg_ebit and recent_assets and recent_liab:
        inv_capital = recent_assets - recent_liab
        if inv_capital > 0:
            roic = (avg_ebit * (1 - 0.21)) / inv_capital

    # Net Debt / EBITDA (approximated)
    net_debt_ebitda = None
    recent_debt = next((v for v in reversed(debt_list) if v is not None), None)
    if recent_debt is not None and avg_ebit and avg_ebit > 0:
        net_debt_ebitda = recent_debt / avg_ebit   # simplified

    result = {
        "ticker":           ticker,
        "current_price":    round(current_price, 2),
        "dcf_iv":           round(dcf_iv, 2) if dcf_iv else None,
        "epv_iv":           round(epv_iv, 2) if epv_iv else None,
        "conservative_iv":  round(conservative_iv, 2),
        "margin_of_safety": round(mos, 4),
        "verdict":          verdict,
        "fcf_yield":        round(fcf_yield, 4) if fcf_yield is not None else None,
        "roic":             round(roic, 4) if roic is not None else None,
        "net_debt_ebitda":  round(net_debt_ebitda, 2) if net_debt_ebitda is not None else None,
        "growth_rate_used": round(growth_rate, 4),
        "avg_fcf_used":     round(avg_fcf, 0) if avg_fcf else None,
        "data_source":      "EDGAR_XBRL",
    }

    logger.info(
        f"[IV] {ticker}: price=${current_price:.2f} | IV=${conservative_iv:.2f} | "
        f"MoS={mos*100:.1f}% | {verdict} | EDGAR"
    )
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["FICO", "MSCI", "NEM", "BRK-B"]
    for tk in tickers:
        r = calculate_iv(tk)
        if r:
            print(f"\n{tk}:")
            for k, v in r.items():
                print(f"  {k}: {v}")
        else:
            print(f"\n{tk}: FAILED")

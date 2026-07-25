#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_reverse_dcf.py -- Reverse DCF (what growth rate does the current price imply?)
"""

import logging

logger = logging.getLogger(__name__)

DISCOUNT_RATE    = 0.10
TERMINAL_GROWTH  = 0.03
PROJECTION_YEARS = 10


def _dcf_value(base_fcf, growth_rate, discount_rate=DISCOUNT_RATE, terminal_growth=TERMINAL_GROWTH, years=PROJECTION_YEARS):
    """Compute DCF intrinsic value given a base FCF and assumed growth rate."""
    if base_fcf <= 0:
        return 0.0
    pv = 0.0
    fcf = base_fcf
    for t in range(1, years + 1):
        fcf = fcf * (1 + growth_rate)
        pv += fcf / ((1 + discount_rate) ** t)
    terminal_value = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv += terminal_value / ((1 + discount_rate) ** years)
    return pv


def reverse_dcf(ticker) -> dict:
    """
    Binary-search for the implied growth rate where DCF(g) == current_price * shares.
    Returns: {current_price, implied_growth_rate, actual_5yr_growth, assessment}
    """
    ticker = ticker.upper().strip()

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals, get_current_price
        data = get_edgar_fundamentals(ticker)
        price = get_current_price(ticker)
    except ImportError:
        data = None
        price = None

    if data is None or price is None:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            if price is None:
                price = info.get("currentPrice") or info.get("regularMarketPrice")
            if data is None:
                data = _yfinance_fallback(ticker, tk)
        except Exception as e:
            return {"current_price": None, "implied_growth_rate": None,
                    "actual_5yr_growth": None, "assessment": f"Data fetch failed: {e}"}

    if data is None or price is None:
        return {"current_price": price, "implied_growth_rate": None,
                "actual_5yr_growth": None, "assessment": "Insufficient data"}

    years = data.get("years", [])
    operating_cf = data.get("operating_cf", [])
    capex = data.get("capex", [])
    shares = data.get("shares_outstanding", [])
    revenue = data.get("revenue", [])

    # Compute base FCF: last 3yr average of (operating_cf - capex)
    fcf_series = []
    n = len(years)
    for i in range(n):
        ocf = operating_cf[i] if operating_cf and i < len(operating_cf) and operating_cf[i] is not None else None
        cpx = capex[i] if capex and i < len(capex) and capex[i] is not None else 0.0
        if ocf is not None:
            fcf_series.append(ocf - abs(cpx))

    if not fcf_series:
        return {"current_price": price, "implied_growth_rate": None,
                "actual_5yr_growth": None, "assessment": "No FCF data available"}

    recent_fcf = fcf_series[-3:] if len(fcf_series) >= 3 else fcf_series
    base_fcf = sum(recent_fcf) / len(recent_fcf)

    # Get shares outstanding for per-share DCF
    shares_val = None
    if shares:
        for v in reversed(shares):
            if v is not None:
                shares_val = float(v)
                break
    if shares_val is None:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            mc = (yf.Ticker(ticker).info or {}).get("marketCap")
            if mc and price:
                shares_val = mc / price
        except Exception:
            pass

    if base_fcf <= 0 or shares_val is None or shares_val <= 0:
        # Still attempt with total market cap
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            mc = (yf.Ticker(ticker).info or {}).get("marketCap")
            if mc and base_fcf > 0:
                market_cap_total = float(mc)
                implied_g = _binary_search_growth(base_fcf, market_cap_total)
                actual_g = _calc_actual_growth(revenue, years)
                assessment = _assess(implied_g, actual_g)
                return {
                    "current_price": price,
                    "implied_growth_rate": round(implied_g, 4) if implied_g is not None else None,
                    "implied_growth_pct": f"{implied_g*100:.1f}%" if implied_g is not None else None,
                    "actual_5yr_growth": round(actual_g, 4) if actual_g is not None else None,
                    "actual_5yr_growth_pct": f"{actual_g*100:.1f}%" if actual_g is not None else None,
                    "base_fcf": round(base_fcf),
                    "assessment": assessment,
                    "note": "Used total market cap (no per-share breakdown)"
                }
        except Exception:
            pass
        return {"current_price": price, "implied_growth_rate": None,
                "actual_5yr_growth": None, "assessment": "Negative/zero base FCF -- reverse DCF not meaningful"}

    market_cap_total = price * shares_val
    implied_g = _binary_search_growth(base_fcf, market_cap_total)
    actual_g = _calc_actual_growth(revenue, years)
    assessment = _assess(implied_g, actual_g)

    return {
        "current_price": price,
        "implied_growth_rate": round(implied_g, 4) if implied_g is not None else None,
        "implied_growth_pct": f"{implied_g*100:.1f}%" if implied_g is not None else None,
        "actual_5yr_growth": round(actual_g, 4) if actual_g is not None else None,
        "actual_5yr_growth_pct": f"{actual_g*100:.1f}%" if actual_g is not None else None,
        "base_fcf": round(base_fcf),
        "assessment": assessment,
    }


def _binary_search_growth(base_fcf, target_market_cap, lo=0.0, hi=0.20, iterations=50):
    """Binary search for implied growth rate where DCF == target_market_cap."""
    if _dcf_value(base_fcf, hi) < target_market_cap:
        return hi  # market pricing >20% growth
    if _dcf_value(base_fcf, lo) > target_market_cap:
        return lo  # market pricing very low growth

    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        val = _dcf_value(base_fcf, mid)
        if abs(val - target_market_cap) / max(abs(target_market_cap), 1) < 0.0001:
            return mid
        if val < target_market_cap:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _calc_actual_growth(revenue, years):
    """Compute actual 5yr revenue CAGR."""
    try:
        rev_vals = [(y, v) for y, v in zip(years, revenue) if v is not None and v > 0]
        if len(rev_vals) < 2:
            return None
        recent = rev_vals[-min(6, len(rev_vals)):]
        start_val = recent[0][1]
        end_val   = recent[-1][1]
        n_years   = len(recent) - 1
        if n_years <= 0 or start_val <= 0:
            return None
        return (end_val / start_val) ** (1.0 / n_years) - 1.0
    except Exception:
        return None


def _assess(implied_g, actual_g):
    if implied_g is None:
        return "UNABLE TO ASSESS -- insufficient data"
    if actual_g is None:
        return f"IMPLIED GROWTH: {implied_g*100:.1f}% -- actual growth data unavailable for comparison"
    if implied_g < actual_g - 0.03:
        return "MARKET PRICING PESSIMISM -- potential opportunity"
    elif implied_g > actual_g + 0.03:
        return "MARKET PRICING OPTIMISM -- limited margin of safety"
    else:
        return "MARKET FAIRLY PRICING GROWTH EXPECTATIONS"


def _yfinance_fallback(ticker, tk):
    try:
        bs = tk.balance_sheet
        cf = tk.cashflow
        inc = tk.income_stmt

        def _series(df, *keys):
            for k in keys:
                if df is not None and k in df.index:
                    vals = df.loc[k].dropna()
                    if len(vals) >= 1:
                        return list(reversed([float(v) for v in vals.values.tolist()]))
            return []

        ocf = _series(cf, "Operating Cash Flow")
        cpx = _series(cf, "Capital Expenditure")
        rev = _series(inc, "Total Revenue")
        shr = _series(bs, "Ordinary Shares Number", "Share Issued")
        n = max(len(ocf), len(rev))
        years = [str(2024 - (n - 1 - i)) for i in range(n)]

        def _pad(lst, length):
            return lst + [None] * (length - len(lst))

        return {
            "ticker": ticker,
            "years": years,
            "operating_cf": _pad(ocf, n),
            "capex": _pad(cpx, n),
            "revenue": _pad(rev, n),
            "shares_outstanding": _pad(shr, n),
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = reverse_dcf(ticker)
    print(json.dumps(result, indent=2, default=str))

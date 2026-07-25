#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_cycle_earnings.py -- Cyclically-adjusted earnings for capital-intensive sectors
"""

import logging

logger = logging.getLogger(__name__)

CYCLICAL_SECTORS = [
    "Energy", "Materials", "Industrials", "Consumer Discretionary", "Financials"
]


def cycle_adjusted_earnings(ticker) -> dict:
    """
    Pull last 7-10yr net income from EDGAR, compute average, compare to most recent year.
    Returns: {cyclical, current_earnings, avg_earnings_7yr, cycle_position,
              normalized_pe_vs_current_pe, recommendation}
    """
    ticker = ticker.upper().strip()

    # Get sector
    sector = None
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector", "")
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        shares_out = info.get("sharesOutstanding")
    except Exception as e:
        logger.warning(f"[CYCLE] yfinance failed for {ticker}: {e}")
        sector = None
        current_price = None
        shares_out = None

    # Check if cyclical
    is_cyclical = False
    if sector:
        for cs in CYCLICAL_SECTORS:
            if cs.lower() in sector.lower():
                is_cyclical = True
                break

    if not is_cyclical:
        return {
            "cyclical": False,
            "sector": sector or "Unknown",
            "note": "Not a cyclical sector, standard earnings apply",
        }

    # Fetch earnings history
    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        data = get_edgar_fundamentals(ticker)
    except ImportError:
        data = None

    if data is None:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            data = _yfinance_fallback(ticker)
        except Exception as e:
            return {"cyclical": True, "sector": sector, "note": f"Data fetch failed: {e}"}

    if data is None:
        return {"cyclical": True, "sector": sector, "note": "No earnings data available"}

    years      = data.get("years", [])
    net_income = data.get("net_income", [])

    # Pull last 7-10 years of valid net income
    ni_pairs = [(y, ni) for y, ni in zip(years, net_income) if ni is not None]
    ni_pairs = ni_pairs[-10:]  # cap at 10 years

    if len(ni_pairs) < 3:
        return {
            "cyclical": True,
            "sector": sector,
            "note": f"Insufficient earnings history ({len(ni_pairs)} years)",
        }

    current_earnings = ni_pairs[-1][1]
    avg_earnings = sum(v for _, v in ni_pairs) / len(ni_pairs)
    years_used = [y for y, _ in ni_pairs]

    # Cycle position
    if avg_earnings == 0:
        cycle_position = "MID"
        pct_vs_avg = 0.0
    else:
        pct_vs_avg = (current_earnings - avg_earnings) / abs(avg_earnings)
        if pct_vs_avg > 0.30:
            cycle_position = "PEAK"
        elif pct_vs_avg < -0.30:
            cycle_position = "TROUGH"
        else:
            cycle_position = "MID"

    # Normalized P/E vs current P/E (per-share)
    normalized_pe = None
    current_pe    = None
    if current_price and shares_out and shares_out > 0:
        curr_eps = current_earnings / shares_out
        avg_eps  = avg_earnings    / shares_out
        if curr_eps and curr_eps != 0:
            current_pe = round(current_price / curr_eps, 2)
        if avg_eps and avg_eps != 0:
            normalized_pe = round(current_price / avg_eps, 2)

    if cycle_position == "PEAK":
        recommendation = "NEAR CYCLE PEAK -- normalize downward; true earnings power lower than reported"
    elif cycle_position == "TROUGH":
        recommendation = "NEAR CYCLE TROUGH -- normalize upward; true earnings power higher than reported"
    else:
        recommendation = "MID-CYCLE -- current earnings reasonably representative of normalized power"

    return {
        "cyclical": True,
        "sector": sector,
        "current_earnings": round(current_earnings),
        "avg_earnings_7yr": round(avg_earnings),
        "pct_vs_avg": round(pct_vs_avg * 100, 1),
        "cycle_position": cycle_position,
        "current_pe": current_pe,
        "normalized_pe": normalized_pe,
        "normalized_pe_vs_current_pe": f"Normalized P/E {normalized_pe} vs Current P/E {current_pe}" if normalized_pe and current_pe else "N/A",
        "years_analyzed": years_used,
        "recommendation": recommendation,
    }


def _yfinance_fallback(ticker):
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
        inc = tk.income_stmt

        if inc is None or "Net Income" not in inc.index:
            return None

        vals = inc.loc["Net Income"].dropna()
        ni_list = list(reversed([float(v) for v in vals.values.tolist()]))
        years = [str(2024 - (len(ni_list) - 1 - i)) for i in range(len(ni_list))]

        return {
            "ticker": ticker,
            "years": years,
            "net_income": ni_list,
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "XOM"
    result = cycle_adjusted_earnings(ticker)
    print(json.dumps(result, indent=2, default=str))

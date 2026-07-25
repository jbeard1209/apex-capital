#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_piotroski.py -- Piotroski F-Score (9-point financial health score)
"""

import logging

logger = logging.getLogger(__name__)


def piotroski_score(ticker) -> dict:
    """
    Compute Piotroski F-Score for a ticker.
    Returns: {score, category, signals, note}
    """
    ticker = ticker.upper().strip()

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals
        data = get_edgar_fundamentals(ticker)
    except ImportError:
        data = None

    # Fallback to yfinance if EDGAR unavailable
    if data is None:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            info = yf.Ticker(ticker).info or {}
            # Build a minimal data dict from yfinance info
            data = _yfinance_fallback(ticker, info)
        except Exception as e:
            return {"score": 0, "category": "ERROR", "signals": {}, "note": f"Data fetch failed: {e}"}

    if data is None:
        return {"score": 0, "category": "ERROR", "signals": {}, "note": "No data available"}

    signals = {}
    score = 0

    years = data.get("years", [])
    net_income = data.get("net_income", [])
    operating_cf = data.get("operating_cf", [])
    total_assets = data.get("total_assets", [])
    long_term_debt = data.get("long_term_debt", [])
    shares_outstanding = data.get("shares_outstanding", [])
    gross_profit = data.get("gross_profit", [])
    revenue = data.get("revenue", [])
    current_assets = data.get("current_assets", [])
    current_liabilities = data.get("current_liabilities", [])
    total_liabilities = data.get("total_liabilities", [])

    n = len(years)
    if n < 2:
        return {"score": 0, "category": "ERROR", "signals": {}, "note": f"Insufficient data: only {n} year(s)"}

    def safe(lst, idx, default=None):
        try:
            if lst and lst[idx] is not None:
                return float(lst[idx])
        except (IndexError, TypeError):
            pass
        return default

    # Use last 2 years
    cur = -1
    prev = -2

    assets_cur  = safe(total_assets, cur)
    assets_prev = safe(total_assets, prev)
    ni_cur      = safe(net_income, cur)
    ni_prev     = safe(net_income, prev)
    ocf_cur     = safe(operating_cf, cur)
    rev_cur     = safe(revenue, cur)
    rev_prev    = safe(revenue, prev)
    gp_cur      = safe(gross_profit, cur)
    gp_prev     = safe(gross_profit, prev)
    ltd_cur     = safe(long_term_debt, cur)
    ltd_prev    = safe(long_term_debt, prev)
    ca_cur      = safe(current_assets, cur)
    cl_cur      = safe(current_liabilities, cur)
    ca_prev     = safe(current_assets, prev)
    cl_prev     = safe(current_liabilities, prev)
    tl_cur      = safe(total_liabilities, cur)
    tl_prev     = safe(total_liabilities, prev)
    sh_cur      = safe(shares_outstanding, cur)
    sh_prev     = safe(shares_outstanding, prev)

    # -- PROFITABILITY --

    # 1. ROA > 0
    roa_cur = (ni_cur / assets_cur) if (ni_cur is not None and assets_cur and assets_cur > 0) else None
    if roa_cur is not None and roa_cur > 0:
        signals["roa_positive"] = "PASS"
        score += 1
    elif roa_cur is not None:
        signals["roa_positive"] = "FAIL"
    else:
        signals["roa_positive"] = "N/A"

    # 2. Operating CF > 0
    if ocf_cur is not None and ocf_cur > 0:
        signals["operating_cf_positive"] = "PASS"
        score += 1
    elif ocf_cur is not None:
        signals["operating_cf_positive"] = "FAIL"
    else:
        signals["operating_cf_positive"] = "N/A"

    # 3. ROA improving YoY
    roa_prev = (ni_prev / assets_prev) if (ni_prev is not None and assets_prev and assets_prev > 0) else None
    if roa_cur is not None and roa_prev is not None and roa_cur > roa_prev:
        signals["roa_improving"] = "PASS"
        score += 1
    elif roa_cur is not None and roa_prev is not None:
        signals["roa_improving"] = "FAIL"
    else:
        signals["roa_improving"] = "N/A"

    # 4. Accruals: OCF/assets > NI/assets (cash-backed earnings)
    if ocf_cur is not None and ni_cur is not None and assets_cur and assets_cur > 0:
        if (ocf_cur / assets_cur) > (ni_cur / assets_cur):
            signals["accruals_cash_backed"] = "PASS"
            score += 1
        else:
            signals["accruals_cash_backed"] = "FAIL"
    else:
        signals["accruals_cash_backed"] = "N/A"

    # -- LEVERAGE / LIQUIDITY --

    # 5. Long-term debt ratio decreased YoY
    ltd_ratio_cur  = (ltd_cur / assets_cur) if (ltd_cur is not None and assets_cur and assets_cur > 0) else None
    ltd_ratio_prev = (ltd_prev / assets_prev) if (ltd_prev is not None and assets_prev and assets_prev > 0) else None
    if ltd_ratio_cur is not None and ltd_ratio_prev is not None:
        if ltd_ratio_cur < ltd_ratio_prev:
            signals["ltd_ratio_decreased"] = "PASS"
            score += 1
        else:
            signals["ltd_ratio_decreased"] = "FAIL"
    else:
        signals["ltd_ratio_decreased"] = "N/A"

    # 6. Current ratio improved YoY (use current_assets/current_liabilities; fallback to assets/liabilities)
    if ca_cur is not None and cl_cur is not None and cl_cur > 0 and ca_prev is not None and cl_prev is not None and cl_prev > 0:
        cr_cur  = ca_cur / cl_cur
        cr_prev = ca_prev / cl_prev
    elif assets_cur and tl_cur and tl_cur > 0 and assets_prev and tl_prev and tl_prev > 0:
        cr_cur  = assets_cur / tl_cur
        cr_prev = assets_prev / tl_prev
    else:
        cr_cur = cr_prev = None

    if cr_cur is not None and cr_prev is not None:
        if cr_cur > cr_prev:
            signals["current_ratio_improved"] = "PASS"
            score += 1
        else:
            signals["current_ratio_improved"] = "FAIL"
    else:
        signals["current_ratio_improved"] = "N/A"

    # 7. No new shares issued (shares_outstanding not increased >2%)
    if sh_cur is not None and sh_prev is not None and sh_prev > 0:
        share_change = (sh_cur - sh_prev) / sh_prev
        if share_change <= 0.02:
            signals["no_dilution"] = "PASS"
            score += 1
        else:
            signals["no_dilution"] = f"FAIL (shares +{share_change:.1%})"
    else:
        signals["no_dilution"] = "N/A"

    # -- OPERATING EFFICIENCY --

    # 8. Gross margin improved YoY
    gm_cur  = (gp_cur / rev_cur)   if (gp_cur is not None and rev_cur and rev_cur > 0) else None
    gm_prev = (gp_prev / rev_prev) if (gp_prev is not None and rev_prev and rev_prev > 0) else None
    if gm_cur is not None and gm_prev is not None:
        if gm_cur > gm_prev:
            signals["gross_margin_improved"] = "PASS"
            score += 1
        else:
            signals["gross_margin_improved"] = "FAIL"
    else:
        signals["gross_margin_improved"] = "N/A"

    # 9. Asset turnover improved YoY (revenue/assets)
    at_cur  = (rev_cur / assets_cur)   if (rev_cur is not None and assets_cur and assets_cur > 0) else None
    at_prev = (rev_prev / assets_prev) if (rev_prev is not None and assets_prev and assets_prev > 0) else None
    if at_cur is not None and at_prev is not None:
        if at_cur > at_prev:
            signals["asset_turnover_improved"] = "PASS"
            score += 1
        else:
            signals["asset_turnover_improved"] = "FAIL"
    else:
        signals["asset_turnover_improved"] = "N/A"

    if score >= 7:
        category = "STRONG"
    elif score >= 4:
        category = "NEUTRAL"
    else:
        category = "WEAK"

    valid_signals = [k for k, v in signals.items() if v not in ("N/A",)]
    n_valid = len(valid_signals)
    note = f"Scored on {n_valid}/9 signals using years {years[prev]}-{years[cur]}"

    return {
        "score": score,
        "category": category,
        "signals": signals,
        "note": note,
    }


def _yfinance_fallback(ticker, info):
    """Build a minimal fundamentals dict from yfinance info for fallback."""
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        bs = tk.balance_sheet
        cf = tk.cashflow
        inc = tk.income_stmt

        def _last2(df, *keys):
            for k in keys:
                if df is not None and k in df.index:
                    vals = df.loc[k].dropna()
                    if len(vals) >= 1:
                        return list(reversed(vals.values[:2].tolist()))
            return [None, None]

        net_income = _last2(inc, "Net Income")
        operating_cf = _last2(cf, "Operating Cash Flow")
        total_assets = _last2(bs, "Total Assets")
        total_liabilities = _last2(bs, "Total Liabilities Net Minority Interest", "Total Liabilities")
        long_term_debt = _last2(bs, "Long Term Debt")
        shares_outstanding = _last2(bs, "Ordinary Shares Number", "Share Issued")
        gross_profit = _last2(inc, "Gross Profit")
        revenue = _last2(inc, "Total Revenue")
        current_assets = _last2(bs, "Current Assets")
        current_liabilities = _last2(bs, "Current Liabilities")

        return {
            "ticker": ticker,
            "years": ["prev", "cur"],
            "net_income": net_income,
            "operating_cf": operating_cf,
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "long_term_debt": long_term_debt,
            "shares_outstanding": shares_outstanding,
            "gross_profit": gross_profit,
            "revenue": revenue,
            "current_assets": current_assets,
            "current_liabilities": current_liabilities,
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = piotroski_score(ticker)
    print(json.dumps(result, indent=2, default=str))

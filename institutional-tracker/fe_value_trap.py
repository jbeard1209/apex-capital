#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_value_trap.py -- Value trap detector (cheap for a reason)
"""

import logging

logger = logging.getLogger(__name__)


def detect_value_trap(ticker) -> dict:
    """
    Scan for red flags that suggest a stock is cheap because it's deteriorating.
    Returns: {trap_flags, trap_score, verdict}
    """
    ticker = ticker.upper().strip()

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
            return {"trap_flags": [], "trap_score": 0, "verdict": f"Data fetch failed: {e}"}

    if data is None:
        return {"trap_flags": [], "trap_score": 0, "verdict": "No data available"}

    years       = data.get("years", [])
    net_income  = data.get("net_income", [])
    revenue     = data.get("revenue", [])
    gross_profit= data.get("gross_profit", [])
    operating_cf= data.get("operating_cf", [])
    capex       = data.get("capex", [])
    long_term_debt = data.get("long_term_debt", [])
    ebit        = data.get("ebit", [])
    da          = data.get("da", [])

    n = len(years)
    trap_flags = []

    def val(lst, idx):
        if lst and 0 <= idx < len(lst) and lst[idx] is not None:
            return float(lst[idx])
        return None

    def safe_divide(a, b):
        if a is not None and b is not None and b != 0:
            return a / b
        return None

    # -- FLAG 1: ROIC declining 3+ consecutive years
    # Approximate ROIC as EBIT / (total_assets - current_liabilities)
    # Use NI / assets as proxy if ebit not available
    roic_series = []
    total_assets = data.get("total_assets", [])
    for i in range(n):
        eb = val(ebit, i)
        ni = val(net_income, i)
        ta = val(total_assets, i)
        if ta and ta > 0:
            numerator = eb if eb is not None else ni
            if numerator is not None:
                roic_series.append(numerator / ta)
            else:
                roic_series.append(None)
        else:
            roic_series.append(None)

    if len(roic_series) >= 4:
        recent = roic_series[-4:]
        valid_recent = [v for v in recent if v is not None]
        if len(valid_recent) >= 3:
            declining = all(valid_recent[i] > valid_recent[i+1] for i in range(len(valid_recent)-1))
            if declining:
                trap_flags.append(f"ROIC declining {len(valid_recent)} consecutive years: {[round(v*100,1) for v in valid_recent]}%")

    # -- FLAG 2: Revenue shrinking 2+ years (not just cyclical)
    rev_series = [val(revenue, i) for i in range(n) if val(revenue, i) is not None]
    if len(rev_series) >= 3:
        recent_rev = rev_series[-3:]
        if all(recent_rev[i] > recent_rev[i+1] for i in range(len(recent_rev)-1)):
            pct = round((recent_rev[-1] - recent_rev[0]) / recent_rev[0] * 100, 1)
            trap_flags.append(f"Revenue shrinking 2+ consecutive years (total {pct}% decline)")

    # -- FLAG 3: FCF positive but net income much higher (earnings possibly funded by debt/asset sales)
    fcf_series = []
    for i in range(n):
        ocf = val(operating_cf, i)
        cpx = val(capex, i)
        if ocf is not None:
            fcf_series.append(ocf - abs(cpx) if cpx is not None else ocf)
        else:
            fcf_series.append(None)

    if n >= 1:
        last_fcf = fcf_series[-1] if fcf_series else None
        last_ni  = val(net_income, -1)
        if last_fcf is not None and last_ni is not None and last_fcf > 0 and last_ni > last_fcf * 1.5:
            ratio = round(last_ni / last_fcf, 2)
            trap_flags.append(f"Net income ({last_ni/1e6:.0f}M) far exceeds FCF ({last_fcf/1e6:.0f}M) -- ratio {ratio}x, earnings may not be cash-backed")

    # -- FLAG 4: Gross margin declining 3+ years
    gm_series = []
    for i in range(n):
        gp = val(gross_profit, i)
        rv = val(revenue, i)
        if gp is not None and rv and rv > 0:
            gm_series.append(gp / rv)
        else:
            gm_series.append(None)

    valid_gm = [(y, v) for y, v in zip(years, gm_series) if v is not None]
    if len(valid_gm) >= 4:
        recent_gm = [v for _, v in valid_gm[-4:]]
        if all(recent_gm[i] > recent_gm[i+1] for i in range(len(recent_gm)-1)):
            trap_flags.append(f"Gross margin declining 3+ consecutive years: {[round(v*100,1) for v in recent_gm]}%")

    # -- FLAG 5: Debt/EBITDA increasing while EBITDA declining
    ebitda_series = []
    debt_ebitda_series = []
    for i in range(n):
        eb = val(ebit, i)
        d  = val(da, i)
        ltd = val(long_term_debt, i)
        if eb is not None and d is not None:
            ebitda = eb + d
        elif eb is not None:
            ebitda = eb * 1.15  # rough DA proxy
        else:
            ebitda = None
        ebitda_series.append(ebitda)
        if ebitda and ebitda > 0 and ltd is not None:
            debt_ebitda_series.append(ltd / ebitda)
        else:
            debt_ebitda_series.append(None)

    valid_de = [(y, de) for y, de in zip(years, debt_ebitda_series) if de is not None]
    valid_eb = [(y, e)  for y, e  in zip(years, ebitda_series)      if e  is not None]
    if len(valid_de) >= 3 and len(valid_eb) >= 3:
        recent_de = [v for _, v in valid_de[-3:]]
        recent_eb = [v for _, v in valid_eb[-3:]]
        de_rising  = all(recent_de[i] < recent_de[i+1] for i in range(len(recent_de)-1))
        eb_falling = all(recent_eb[i] > recent_eb[i+1] for i in range(len(recent_eb)-1))
        if de_rising and eb_falling:
            trap_flags.append(f"Debt/EBITDA rising {[round(v,1) for v in recent_de]}x while EBITDA declining -- leverage trap")

    # -- FLAG 6: Dividend payout ratio > 100% of FCF (unsustainable)
    # Use yfinance for dividend data
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        info = yf.Ticker(ticker).info or {}
        div_rate  = info.get("dividendRate") or 0
        shares_out = info.get("sharesOutstanding") or 0
        if div_rate and shares_out:
            total_divs = div_rate * shares_out
            if fcf_series and fcf_series[-1] is not None and fcf_series[-1] > 0:
                payout_ratio = total_divs / fcf_series[-1]
                if payout_ratio > 1.0:
                    trap_flags.append(f"Dividend payout {payout_ratio*100:.0f}% of FCF -- likely unsustainable (div ${div_rate:.2f}/share)")
    except Exception:
        pass

    trap_score = len(trap_flags)

    if trap_score >= 4:
        verdict = "LIKELY TRAP"
    elif trap_score >= 2:
        verdict = "WATCH CAREFULLY"
    else:
        verdict = "LOOKS OKAY"

    return {
        "trap_flags": trap_flags,
        "trap_score": trap_score,
        "verdict": verdict,
        "years_analyzed": years,
    }


def _yfinance_fallback(ticker):
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
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

        net_income   = _series(inc, "Net Income")
        revenue      = _series(inc, "Total Revenue")
        gross_profit = _series(inc, "Gross Profit")
        operating_cf = _series(cf, "Operating Cash Flow")
        capex        = _series(cf, "Capital Expenditure")
        long_term_debt = _series(bs, "Long Term Debt")
        ebit         = _series(inc, "EBIT", "Operating Income")
        total_assets = _series(bs, "Total Assets")
        da           = _series(cf, "Depreciation And Amortization", "Depreciation Amortization Depletion")

        n = max(len(net_income), len(revenue), 1)
        years = [str(2024 - (n - 1 - i)) for i in range(n)]

        def _pad(lst, length):
            return lst + [None] * (length - len(lst))

        return {
            "ticker": ticker,
            "years": years,
            "net_income": _pad(net_income, n),
            "revenue": _pad(revenue, n),
            "gross_profit": _pad(gross_profit, n),
            "operating_cf": _pad(operating_cf, n),
            "capex": _pad(capex, n),
            "long_term_debt": _pad(long_term_debt, n),
            "ebit": _pad(ebit, n),
            "total_assets": _pad(total_assets, n),
            "da": _pad(da, n),
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = detect_value_trap(ticker)
    print(json.dumps(result, indent=2, default=str))

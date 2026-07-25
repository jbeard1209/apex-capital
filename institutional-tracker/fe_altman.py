#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_altman.py -- Altman Z-Score (bankruptcy predictor)
"""

import logging

logger = logging.getLogger(__name__)


def altman_zscore(ticker) -> dict:
    """
    Compute Altman Z-Score for a ticker.
    Returns: {zscore, zone, bankruptcy_risk, components}
    """
    ticker = ticker.upper().strip()

    try:
        from fe_edgar_fundamentals import get_edgar_fundamentals, get_current_price
        data = get_edgar_fundamentals(ticker)
        price = get_current_price(ticker)
    except ImportError:
        data = None
        price = None

    # yfinance fallback
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
            return {"zscore": None, "zone": "ERROR", "bankruptcy_risk": "UNKNOWN",
                    "components": {}, "note": f"Data fetch failed: {e}"}

    if data is None:
        return {"zscore": None, "zone": "ERROR", "bankruptcy_risk": "UNKNOWN",
                "components": {}, "note": "No fundamental data available"}

    years = data.get("years", [])
    if not years:
        return {"zscore": None, "zone": "ERROR", "bankruptcy_risk": "UNKNOWN",
                "components": {}, "note": "No years in data"}

    def safe_last(lst, default=None):
        if lst:
            for v in reversed(lst):
                if v is not None:
                    return float(v)
        return default

    net_income    = safe_last(data.get("net_income", []))
    operating_cf  = safe_last(data.get("operating_cf", []))
    total_assets  = safe_last(data.get("total_assets", []))
    total_liab    = safe_last(data.get("total_liabilities", []))
    current_assets = safe_last(data.get("current_assets", []))
    current_liab  = safe_last(data.get("current_liabilities", []))
    revenue       = safe_last(data.get("revenue", []))
    ebit          = safe_last(data.get("ebit", []))
    long_term_debt = safe_last(data.get("long_term_debt", []))

    if total_assets is None or total_assets <= 0:
        return {"zscore": None, "zone": "ERROR", "bankruptcy_risk": "UNKNOWN",
                "components": {}, "note": "Total assets unavailable or zero"}

    # X1: Working Capital / Total Assets
    if current_assets is not None and current_liab is not None:
        wc = current_assets - current_liab
    else:
        wc = total_assets * 0.1  # neutral proxy
    x1 = wc / total_assets

    # X2: Retained Earnings / Total Assets -- approximate as cumulative net income / assets
    ni_list = [v for v in (data.get("net_income") or []) if v is not None]
    if ni_list:
        retained = sum(ni_list)
    elif net_income is not None:
        retained = net_income
    else:
        retained = 0.0
    x2 = retained / total_assets

    # X3: EBIT / Total Assets
    if ebit is not None:
        x3 = ebit / total_assets
    elif net_income is not None:
        x3 = net_income / total_assets  # rough proxy
    else:
        x3 = 0.0

    # X4: Market Cap / Total Liabilities
    market_cap = None
    if price is not None:
        shares = safe_last(data.get("shares_outstanding", []))
        if shares and shares > 0:
            market_cap = price * shares
    if market_cap is None:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            mc = (yf.Ticker(ticker).info or {}).get("marketCap")
            if mc:
                market_cap = float(mc)
        except Exception:
            pass
    if market_cap is None:
        market_cap = total_assets * 0.5  # rough fallback

    x4 = market_cap / total_liab if (total_liab and total_liab > 0) else 0.0

    # X5: Revenue / Total Assets
    x5 = (revenue / total_assets) if (revenue is not None) else 0.0

    # Determine sector for manufacturing vs non-manufacturing
    is_manufacturing = True
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        sector = (yf.Ticker(ticker).info or {}).get("sector", "")
        non_mfg = ["Financial Services", "Real Estate", "Technology",
                   "Communication Services", "Healthcare"]
        if any(s in sector for s in non_mfg):
            is_manufacturing = False
    except Exception:
        pass

    if is_manufacturing:
        zscore = 1.2*x1 + 1.4*x2 + 3.3*x3 + 0.6*x4 + 1.0*x5
        model = "Original Z-Score (manufacturing)"
        safe_threshold = 2.99
        distress_threshold = 1.81
    else:
        zscore = 6.56*x1 + 3.26*x2 + 6.72*x3 + 1.05*x4
        model = "Modified Z'' (non-manufacturing)"
        safe_threshold = 2.60
        distress_threshold = 1.10

    if zscore > safe_threshold:
        zone = "SAFE"
        bankruptcy_risk = "LOW"
    elif zscore >= distress_threshold:
        zone = "GREY"
        bankruptcy_risk = "MEDIUM"
    else:
        zone = "DISTRESS"
        bankruptcy_risk = "HIGH"

    return {
        "zscore": round(zscore, 3),
        "zone": zone,
        "bankruptcy_risk": bankruptcy_risk,
        "model": model,
        "components": {
            "x1_working_capital_ratio": round(x1, 4),
            "x2_retained_earnings_ratio": round(x2, 4),
            "x3_ebit_ratio": round(x3, 4),
            "x4_market_cap_to_liabilities": round(x4, 4),
            "x5_asset_turnover": round(x5, 4) if is_manufacturing else "N/A (non-mfg model)",
        },
        "note": f"Using {model}. Thresholds: SAFE>{safe_threshold}, DISTRESS<{distress_threshold}",
    }


def _yfinance_fallback(ticker, tk):
    try:
        bs = tk.balance_sheet
        cf = tk.cashflow
        inc = tk.income_stmt

        def _last(df, *keys):
            for k in keys:
                if df is not None and k in df.index:
                    vals = df.loc[k].dropna()
                    if len(vals) >= 1:
                        return float(vals.iloc[0])
            return None

        def _all(df, *keys):
            for k in keys:
                if df is not None and k in df.index:
                    vals = df.loc[k].dropna()
                    if len(vals) >= 1:
                        return [float(v) for v in reversed(vals.values.tolist())]
            return []

        return {
            "ticker": ticker,
            "years": ["hist"],
            "net_income": [_last(inc, "Net Income")],
            "operating_cf": [_last(cf, "Operating Cash Flow")],
            "total_assets": [_last(bs, "Total Assets")],
            "total_liabilities": [_last(bs, "Total Liabilities Net Minority Interest", "Total Liabilities")],
            "current_assets": [_last(bs, "Current Assets")],
            "current_liabilities": [_last(bs, "Current Liabilities")],
            "revenue": [_last(inc, "Total Revenue")],
            "ebit": [_last(inc, "EBIT", "Operating Income")],
            "long_term_debt": [_last(bs, "Long Term Debt")],
            "shares_outstanding": [_last(bs, "Ordinary Shares Number", "Share Issued")],
            "gross_profit": [_last(inc, "Gross Profit")],
        }
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = altman_zscore(ticker)
    print(json.dumps(result, indent=2, default=str))

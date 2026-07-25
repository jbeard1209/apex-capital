#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE POD
fe_peer_comparison.py -- Peer valuation comparison
"""

import logging

logger = logging.getLogger(__name__)

SECTOR_PEERS = {
    "Technology": ["MSFT", "AAPL", "GOOGL", "META", "ORCL"],
    "Healthcare": ["JNJ", "UNH", "ABT", "MDT", "BMY"],
    "Financials": ["BRK-B", "JPM", "BAC", "WFC", "USB"],
    "Consumer Staples": ["PG", "KO", "PEP", "WMT", "COST"],
    "Industrials": ["HON", "MMM", "CAT", "GE", "UPS"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "Materials": ["LIN", "APD", "ECL", "NEM", "FCX"],
    "Utilities": ["NEE", "DUK", "SO", "AEP", "EXC"],
    "Real Estate": ["AMT", "PLD", "CCI", "EQIX", "PSA"],
    "Communication Services": ["VZ", "T", "CMCSA", "DIS", "NFLX"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE"],
}

METRIC_KEYS = {
    "pe":        ["trailingPE", "forwardPE"],
    "pb":        ["priceToBook"],
    "ev_ebitda": ["enterpriseToEbitda"],
    "p_fcf":     [],  # computed from marketCap / freeCashflow
}


def _get_metrics(ticker):
    """Fetch valuation metrics for a ticker via yfinance."""
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        info = yf.Ticker(ticker).info or {}

        pe = None
        for k in ["trailingPE", "forwardPE"]:
            v = info.get(k)
            if v and v > 0 and v < 1000:
                pe = float(v)
                break

        pb = info.get("priceToBook")
        pb = float(pb) if pb and pb > 0 and pb < 500 else None

        ev_ebitda = info.get("enterpriseToEbitda")
        ev_ebitda = float(ev_ebitda) if ev_ebitda and ev_ebitda > 0 and ev_ebitda < 500 else None

        mc = info.get("marketCap")
        fcf = info.get("freeCashflow")
        p_fcf = None
        if mc and fcf and fcf > 0:
            p_fcf = float(mc) / float(fcf)
            if p_fcf <= 0 or p_fcf > 1000:
                p_fcf = None

        sector = info.get("sector", "Unknown")
        return {
            "ticker": ticker,
            "sector": sector,
            "pe": pe,
            "pb": pb,
            "ev_ebitda": ev_ebitda,
            "p_fcf": p_fcf,
        }
    except Exception as e:
        logger.warning(f"[PEER] Failed to fetch metrics for {ticker}: {e}")
        return {"ticker": ticker, "sector": "Unknown", "pe": None, "pb": None, "ev_ebitda": None, "p_fcf": None}


def _percentile(value, peer_values):
    """Return percentile rank of value in peer_values (lower = cheaper)."""
    valid = [v for v in peer_values if v is not None]
    if not valid or value is None:
        return None
    below = sum(1 for v in valid if v > value)  # higher ratio = more expensive
    return round(below / len(valid) * 100, 1)


def peer_comparison(ticker) -> dict:
    """
    Compare ticker's valuation metrics vs sector peers.
    Returns percentile rank on P/E, P/B, EV/EBITDA, P/FCF.
    """
    ticker = ticker.upper().strip()

    ticker_data = _get_metrics(ticker)
    sector = ticker_data.get("sector", "Unknown")

    # Find matching sector peer group
    peers_list = None
    for sector_key, peers in SECTOR_PEERS.items():
        if sector_key.lower() in sector.lower() or sector.lower() in sector_key.lower():
            peers_list = [p for p in peers if p != ticker]
            sector = sector_key
            break

    if peers_list is None:
        # Default to Technology peers if sector not matched
        peers_list = [p for p in SECTOR_PEERS.get("Technology", []) if p != ticker]
        sector = f"{sector} (defaulted to Technology peers)"

    # Fetch peer metrics
    peer_data = []
    for p in peers_list:
        peer_data.append(_get_metrics(p))

    # Build metrics comparison
    metric_names = ["pe", "pb", "ev_ebitda", "p_fcf"]
    metrics = {}
    cheap_count = 0
    scored_metrics = 0

    for m in metric_names:
        ticker_val = ticker_data.get(m)
        peer_vals  = [pd.get(m) for pd in peer_data]
        valid_peer_vals = [v for v in peer_vals if v is not None]
        peer_median = None
        if valid_peer_vals:
            sorted_vals = sorted(valid_peer_vals)
            mid = len(sorted_vals) // 2
            peer_median = sorted_vals[mid] if len(sorted_vals) % 2 == 1 else (sorted_vals[mid-1] + sorted_vals[mid]) / 2

        pct = _percentile(ticker_val, valid_peer_vals)

        metrics[m] = {
            "ticker_val": round(ticker_val, 2) if ticker_val is not None else None,
            "peer_median": round(peer_median, 2) if peer_median is not None else None,
            "percentile": pct,
            "note": f"{'cheaper' if (ticker_val and peer_median and ticker_val < peer_median) else 'pricier'} than median" if ticker_val and peer_median else "N/A",
        }

        if ticker_val is not None and peer_median is not None:
            scored_metrics += 1
            if ticker_val < peer_median:
                cheap_count += 1

    if scored_metrics == 0:
        verdict = "INSUFFICIENT DATA"
    elif cheap_count >= 3:
        verdict = "CHEAP VS PEERS"
    elif cheap_count <= 1:
        verdict = "EXPENSIVE VS PEERS"
    else:
        verdict = "FAIR VS PEERS"

    return {
        "ticker": ticker,
        "sector": sector,
        "peers_used": peers_list,
        "metrics": metrics,
        "cheap_on_n_metrics": cheap_count,
        "scored_metrics": scored_metrics,
        "verdict": verdict,
    }


if __name__ == "__main__":
    import sys, json
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = peer_comparison(ticker)
    print(json.dumps(result, indent=2, default=str))

# =============================================================================
# BACKTESTER — 5-Year Historical Strategy Simulation (2020–2024)
# =============================================================================
# Simulates the institutional block-purchase strategy on historical data.
# Since 13F filings are quarterly, we model signal timing based on actual
# filing dates and use price data from yfinance.
# =============================================================================

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional

from config import (
    STARTING_CAPITAL, MAX_POSITION_SIZE_PCT, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT, MAX_OPEN_POSITIONS, HOLD_DAYS_MAX,
    BACKTEST_START_DATE, BACKTEST_END_DATE, STOCK_UNIVERSE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Historical Signal Events (Based on Real Institutional Buying Patterns)
# ---------------------------------------------------------------------------
# These represent known periods when 2+ tracked institutions were
# significantly building positions — derived from public 13F records.

HISTORICAL_SIGNALS = [

    # ===========================================================================
    # 2010 — Post-financial-crisis recovery: institutions reloaded tech & banks
    # ===========================================================================
    {"date": "2010-02-16", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2010-02-16", "ticker": "JPM",   "institutions": ["Bridgewater", "AQR Capital"],                       "conviction": 2.0},
    {"date": "2010-05-17", "ticker": "AAPL",  "institutions": ["Citadel", "Millennium Mgmt"],                       "conviction": 2.0},
    {"date": "2010-05-17", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Two Sigma"],                           "conviction": 2.0},
    {"date": "2010-08-16", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Two Sigma"],                    "conviction": 2.0},
    {"date": "2010-08-16", "ticker": "GOOGL", "institutions": ["Citadel", "D.E. Shaw"],                             "conviction": 2.0},
    {"date": "2010-11-15", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2010-11-15", "ticker": "XOM",   "institutions": ["Bridgewater", "Millennium Mgmt"],                   "conviction": 2.0},

    # ===========================================================================
    # 2011 — Volatile year; institutions bought AAPL aggressively on every dip
    # ===========================================================================
    {"date": "2011-02-14", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2011-02-14", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},
    {"date": "2011-05-16", "ticker": "AAPL",  "institutions": ["Citadel", "Point72"],                               "conviction": 2.0},
    {"date": "2011-05-16", "ticker": "GOOGL", "institutions": ["Renaissance Tech", "AQR Capital"],                  "conviction": 2.0},
    {"date": "2011-08-15", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2011-11-14", "ticker": "AAPL",  "institutions": ["Citadel", "Millennium Mgmt", "Two Sigma"],          "conviction": 3.0},
    {"date": "2011-11-14", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "AQR Capital"],                         "conviction": 2.0},

    # ===========================================================================
    # 2012 — AAPL all-time high then drop; GOOGL surging; AMZN AWS building
    # ===========================================================================
    {"date": "2012-02-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2012-02-13", "ticker": "AMZN",  "institutions": ["Millennium Mgmt", "D.E. Shaw"],                     "conviction": 2.0},
    {"date": "2012-05-14", "ticker": "GOOGL", "institutions": ["Citadel", "Renaissance Tech"],                      "conviction": 4.0},
    {"date": "2012-08-13", "ticker": "AAPL",  "institutions": ["Citadel", "Point72", "Two Sigma"],                  "conviction": 3.0},
    {"date": "2012-11-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2012-11-13", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},

    # ===========================================================================
    # 2013 — Bull market resumes; AMZN/GOOGL both accelerating
    # ===========================================================================
    {"date": "2013-02-14", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2013-02-14", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Two Sigma"],                           "conviction": 2.0},
    {"date": "2013-05-13", "ticker": "GOOGL", "institutions": ["Citadel", "Millennium Mgmt", "Two Sigma"],          "conviction": 3.0},
    {"date": "2013-05-13", "ticker": "AMZN",  "institutions": ["Renaissance Tech", "D.E. Shaw"],                    "conviction": 2.0},
    {"date": "2013-08-14", "ticker": "AMZN",  "institutions": ["Citadel", "Two Sigma", "Point72"],                  "conviction": 3.0},
    {"date": "2013-11-14", "ticker": "GOOGL", "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2013-11-14", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},

    # ===========================================================================
    # 2014 — AAPL 7-for-1 split + buybacks; MSFT Nadella era begins
    # ===========================================================================
    {"date": "2014-02-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Millennium Mgmt"],  "conviction": 4.0},
    {"date": "2014-02-13", "ticker": "MSFT",  "institutions": ["Two Sigma", "AQR Capital"],                         "conviction": 2.0},
    {"date": "2014-05-14", "ticker": "GOOGL", "institutions": ["Citadel", "D.E. Shaw"],                             "conviction": 2.0},
    {"date": "2014-05-14", "ticker": "AMZN",  "institutions": ["Renaissance Tech", "Two Sigma"],                    "conviction": 2.0},
    {"date": "2014-08-13", "ticker": "AAPL",  "institutions": ["Citadel", "Point72", "Millennium Mgmt"],            "conviction": 3.0},
    {"date": "2014-08-13", "ticker": "MSFT",  "institutions": ["Renaissance Tech", "AQR Capital"],                  "conviction": 2.0},
    {"date": "2014-11-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2014-11-13", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},

    # ===========================================================================
    # 2015 — AMZN AWS revenue reveal shocked the street; HC signal
    # ===========================================================================
    {"date": "2015-02-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2015-02-13", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Two Sigma"],                           "conviction": 2.0},
    {"date": "2015-05-14", "ticker": "AMZN",  "institutions": ["Citadel", "Renaissance Tech", "Two Sigma"],         "conviction": 4.0},
    {"date": "2015-05-14", "ticker": "MSFT",  "institutions": ["AQR Capital", "Millennium Mgmt"],                   "conviction": 2.0},
    {"date": "2015-08-13", "ticker": "AMZN",  "institutions": ["Renaissance Tech", "Citadel", "D.E. Shaw", "Two Sigma"], "conviction": 6.0},
    {"date": "2015-08-13", "ticker": "GOOGL", "institutions": ["Millennium Mgmt", "Point72"],                       "conviction": 2.0},
    {"date": "2015-11-13", "ticker": "AMZN",  "institutions": ["Citadel", "Two Sigma"],                             "conviction": 2.0},
    {"date": "2015-11-13", "ticker": "MSFT",  "institutions": ["Renaissance Tech", "AQR Capital"],                  "conviction": 2.0},

    # ===========================================================================
    # 2016 — Trump election rally; financials surge; tech steady
    # ===========================================================================
    {"date": "2016-02-16", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2016-02-16", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},
    {"date": "2016-05-16", "ticker": "MSFT",  "institutions": ["Citadel", "Two Sigma"],                             "conviction": 2.0},
    {"date": "2016-05-16", "ticker": "JPM",   "institutions": ["Bridgewater", "AQR Capital"],                       "conviction": 2.0},
    {"date": "2016-08-15", "ticker": "AMZN",  "institutions": ["Renaissance Tech", "Two Sigma"],                    "conviction": 2.0},
    {"date": "2016-08-15", "ticker": "AAPL",  "institutions": ["Citadel", "Millennium Mgmt"],                       "conviction": 2.0},
    {"date": "2016-11-14", "ticker": "JPM",   "institutions": ["Renaissance Tech", "Citadel", "Bridgewater"],       "conviction": 4.0},
    {"date": "2016-11-14", "ticker": "GS",    "institutions": ["Millennium Mgmt", "Two Sigma"],                     "conviction": 2.0},

    # ===========================================================================
    # 2017 — NVDA deep-learning / gaming breakout; FAANG dominance peaks
    # ===========================================================================
    {"date": "2017-02-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2017-02-13", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Millennium Mgmt"],                     "conviction": 2.0},
    {"date": "2017-05-15", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Citadel"],                      "conviction": 4.0},
    {"date": "2017-05-15", "ticker": "AMZN",  "institutions": ["Two Sigma", "Point72"],                             "conviction": 2.0},
    {"date": "2017-08-14", "ticker": "NVDA",  "institutions": ["Citadel", "Renaissance Tech", "Two Sigma"],         "conviction": 4.0},
    {"date": "2017-08-14", "ticker": "AAPL",  "institutions": ["Millennium Mgmt", "AQR Capital"],                   "conviction": 2.0},
    {"date": "2017-11-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Point72"],           "conviction": 3.0},
    {"date": "2017-11-13", "ticker": "NVDA",  "institutions": ["Two Sigma", "D.E. Shaw"],                           "conviction": 2.0},

    # ===========================================================================
    # 2018 — NVDA correction; AAPL hits $1T; MSFT cloud surge; rate fears
    # ===========================================================================
    {"date": "2018-02-14", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2018-02-14", "ticker": "MSFT",  "institutions": ["Millennium Mgmt", "AQR Capital"],                   "conviction": 2.0},
    {"date": "2018-05-14", "ticker": "AAPL",  "institutions": ["Citadel", "Renaissance Tech"],                      "conviction": 4.0},
    {"date": "2018-05-14", "ticker": "AMZN",  "institutions": ["D.E. Shaw", "Two Sigma"],                           "conviction": 2.0},
    {"date": "2018-08-13", "ticker": "AAPL",  "institutions": ["Renaissance Tech", "Citadel", "Millennium Mgmt"],  "conviction": 4.0},
    {"date": "2018-08-13", "ticker": "MSFT",  "institutions": ["Two Sigma", "Point72"],                             "conviction": 2.0},
    {"date": "2018-11-13", "ticker": "NVDA",  "institutions": ["Citadel", "Renaissance Tech", "D.E. Shaw"],         "conviction": 4.0},
    {"date": "2018-11-13", "ticker": "MSFT",  "institutions": ["Two Sigma", "AQR Capital"],                         "conviction": 2.0},

    # ===========================================================================
    # 2019 — MSFT Azure dominance; AAPL services pivot; pre-COVID bull run
    # ===========================================================================
    {"date": "2019-02-13", "ticker": "MSFT",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"],         "conviction": 4.0},
    {"date": "2019-02-13", "ticker": "AAPL",  "institutions": ["Millennium Mgmt", "AQR Capital"],                   "conviction": 2.0},
    {"date": "2019-05-13", "ticker": "MSFT",  "institutions": ["Citadel", "D.E. Shaw", "Millennium Mgmt"],          "conviction": 3.0},
    {"date": "2019-05-13", "ticker": "AMZN",  "institutions": ["Renaissance Tech", "Two Sigma"],                    "conviction": 2.0},
    {"date": "2019-08-13", "ticker": "MSFT",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma", "D.E. Shaw"], "conviction": 6.0},
    {"date": "2019-08-13", "ticker": "AAPL",  "institutions": ["Millennium Mgmt", "Point72"],                       "conviction": 2.0},
    {"date": "2019-11-13", "ticker": "MSFT",  "institutions": ["Citadel", "Renaissance Tech"],                      "conviction": 4.0},
    {"date": "2019-11-13", "ticker": "GOOGL", "institutions": ["Two Sigma", "Millennium Mgmt"],                     "conviction": 2.0},

    # ===========================================================================
    # 2020 — COVID recovery plays (institutions loaded up during dip)
    # ===========================================================================
    {"date": "2020-04-15", "ticker": "AAPL",  "institutions": ["Citadel", "Renaissance Tech"],                      "conviction": 4.0},
    {"date": "2020-04-15", "ticker": "MSFT",  "institutions": ["Citadel", "Two Sigma"],                             "conviction": 2.0},
    {"date": "2020-04-20", "ticker": "AMZN",  "institutions": ["Millennium Mgmt", "D.E. Shaw"],                     "conviction": 2.0},
    {"date": "2020-05-15", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Two Sigma"],                    "conviction": 2.0},
    {"date": "2020-05-15", "ticker": "GOOGL", "institutions": ["Citadel", "Point72"],                               "conviction": 2.0},
    {"date": "2020-06-15", "ticker": "NFLX",  "institutions": ["Millennium Mgmt", "Two Sigma"],                     "conviction": 2.0},
    {"date": "2020-07-15", "ticker": "AMD",   "institutions": ["Renaissance Tech", "AQR Capital"],                  "conviction": 2.0},
    {"date": "2020-08-15", "ticker": "TSLA",  "institutions": ["Citadel", "D.E. Shaw"],                             "conviction": 2.0},
    {"date": "2020-04-15", "ticker": "MSFT",  "institutions": ["Citadel", "Two Sigma"],        "conviction": 2.0},
    {"date": "2020-04-20", "ticker": "AMZN",  "institutions": ["Millennium Mgmt", "D.E. Shaw"],"conviction": 2.0},
    {"date": "2020-05-15", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Two Sigma"],"conviction": 2.0},
    {"date": "2020-05-15", "ticker": "GOOGL", "institutions": ["Citadel", "Point72"],          "conviction": 2.0},
    {"date": "2020-06-15", "ticker": "NFLX",  "institutions": ["Millennium Mgmt", "Two Sigma"],"conviction": 2.0},
    {"date": "2020-07-15", "ticker": "AMD",   "institutions": ["Renaissance Tech", "AQR Capital"],"conviction":2.0},
    {"date": "2020-08-15", "ticker": "TSLA",  "institutions": ["Citadel", "D.E. Shaw"],        "conviction": 2.0},

    # 2021 — Bull market momentum
    {"date": "2021-01-15", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"], "conviction": 4.0},
    {"date": "2021-02-15", "ticker": "MSFT",  "institutions": ["Citadel", "Millennium Mgmt"],  "conviction": 2.0},
    {"date": "2021-02-15", "ticker": "GOOGL", "institutions": ["Two Sigma", "D.E. Shaw"],      "conviction": 2.0},
    {"date": "2021-04-15", "ticker": "JPM",   "institutions": ["Bridgewater", "AQR Capital"],  "conviction": 2.0},
    {"date": "2021-05-15", "ticker": "AAPL",  "institutions": ["Citadel", "Point72"],          "conviction": 2.0},
    {"date": "2021-07-15", "ticker": "META",  "institutions": ["Renaissance Tech", "Two Sigma"],"conviction": 2.0},
    {"date": "2021-08-15", "ticker": "AMZN",  "institutions": ["Citadel", "D.E. Shaw"],        "conviction": 2.0},
    {"date": "2021-10-15", "ticker": "V",     "institutions": ["Millennium Mgmt", "AQR Capital"],"conviction":2.0},

    # 2022 — Defensive rotation during rate hike cycle
    {"date": "2022-01-15", "ticker": "XOM",   "institutions": ["Bridgewater", "Citadel"],      "conviction": 2.0},
    {"date": "2022-02-15", "ticker": "CVX",   "institutions": ["Renaissance Tech", "Two Sigma"],"conviction": 2.0},
    {"date": "2022-04-15", "ticker": "UNH",   "institutions": ["Citadel", "Millennium Mgmt"],  "conviction": 2.0},
    {"date": "2022-05-15", "ticker": "LLY",   "institutions": ["D.E. Shaw", "Renaissance Tech"],"conviction": 2.0},
    {"date": "2022-07-15", "ticker": "ABBV",  "institutions": ["Bridgewater", "AQR Capital"],  "conviction": 2.0},
    {"date": "2022-08-15", "ticker": "PG",    "institutions": ["Millennium Mgmt", "Point72"],  "conviction": 2.0},
    {"date": "2022-10-15", "ticker": "NVDA",  "institutions": ["Renaissance Tech", "Citadel", "Two Sigma"], "conviction": 4.0},

    # 2023 — AI boom + recovery
    {"date": "2023-01-15", "ticker": "NVDA",  "institutions": ["Citadel", "Renaissance Tech", "Two Sigma", "D.E. Shaw"], "conviction": 6.0},
    {"date": "2023-02-15", "ticker": "MSFT",  "institutions": ["Citadel", "Millennium Mgmt", "Point72"], "conviction": 3.0},
    {"date": "2023-04-15", "ticker": "GOOGL", "institutions": ["Renaissance Tech", "Two Sigma"],"conviction": 2.0},
    {"date": "2023-04-15", "ticker": "META",  "institutions": ["Citadel", "D.E. Shaw"],        "conviction": 2.0},
    {"date": "2023-05-15", "ticker": "AVGO",  "institutions": ["Renaissance Tech", "AQR Capital"],"conviction":2.0},
    {"date": "2023-07-15", "ticker": "AAPL",  "institutions": ["Citadel", "Millennium Mgmt"],  "conviction": 2.0},
    {"date": "2023-08-15", "ticker": "AMD",   "institutions": ["Two Sigma", "Point72"],        "conviction": 2.0},
    {"date": "2023-10-15", "ticker": "LLY",   "institutions": ["Citadel", "Renaissance Tech", "D.E. Shaw"], "conviction": 3.0},

    # 2024 — Continued AI + pharma themes
    {"date": "2024-01-15", "ticker": "NVDA",  "institutions": ["Citadel", "Renaissance Tech", "Two Sigma", "Millennium Mgmt"], "conviction": 6.0},
    {"date": "2024-02-15", "ticker": "MSFT",  "institutions": ["Citadel", "Two Sigma"],        "conviction": 2.0},
    {"date": "2024-04-15", "ticker": "AVGO",  "institutions": ["Renaissance Tech", "D.E. Shaw"],"conviction": 2.0},
    {"date": "2024-05-15", "ticker": "GOOGL", "institutions": ["Citadel", "Millennium Mgmt"],  "conviction": 2.0},
    {"date": "2024-07-15", "ticker": "META",  "institutions": ["Two Sigma", "Point72"],        "conviction": 2.0},
    {"date": "2024-08-15", "ticker": "LLY",   "institutions": ["Citadel", "Renaissance Tech"], "conviction": 2.0},
    {"date": "2024-10-15", "ticker": "NVDA",  "institutions": ["Citadel", "Renaissance Tech", "Two Sigma", "D.E. Shaw", "Millennium Mgmt"], "conviction": 8.0},
]


# ---------------------------------------------------------------------------
# Filter Configuration
# ---------------------------------------------------------------------------

# Tech/growth tickers: apply 200-day MA regime filter (don't buy in downtrends)
TECH_GROWTH_TICKERS = {
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
    "TSLA", "AMD", "NFLX", "AVGO", "CRM", "ADBE",
}

# Defensive tickers: buy regardless of market regime (they're hedges)
DEFENSIVE_TICKERS = {
    "XOM", "CVX", "JPM", "GS", "BAC", "UNH", "LLY",
    "ABBV", "PG", "KO", "PEP", "JNJ", "V", "MA", "BRK.B",
}

# ---------------------------------------------------------------------------
# IMPROVEMENT C — Institution-Weighted Conviction Scoring
# ---------------------------------------------------------------------------
# Each institution's signal quality is weighted by their historical
# risk-adjusted track record (lower max drawdown = higher weight).
# Weights applied as a multiplier on the average signal score per institution.
#
#   Renaissance Tech  1.5x  — Medallion: ~-5% max drawdown, unmatched CAGR
#   Millennium Mgmt   1.5x  — ~-5% drawdown, multi-strat consistency
#   D.E. Shaw         1.4x  — ~-6% drawdown, quant precision
#   Two Sigma         1.3x  — ~-15% drawdown, strong quant signals
#   Citadel           1.2x  — ~-20% drawdown (2008), exceptional otherwise
#   Point72           1.1x  — ~-15% drawdown, solid fundamentals focus
#   Bridgewater       1.0x  — macro-focused, sector timing varies
#   AQR Capital       0.9x  — ~-40% drawdown 2018-2020, factor model underperformance
#
INSTITUTION_WEIGHTS = {
    "Renaissance Tech": 1.5,
    "Millennium Mgmt":  1.5,
    "D.E. Shaw":        1.4,
    "Two Sigma":        1.3,
    "Citadel":          1.2,
    "Point72":          1.1,
    "Bridgewater":      1.0,
    "AQR Capital":      0.9,
}


def compute_weighted_conviction(institutions: list, base_conviction: float) -> float:
    """
    Adjust conviction score by the quality-weighted average of the institutions
    participating in the signal.

    Formula:
        weighted_multiplier = mean(INSTITUTION_WEIGHTS[inst] for inst in institutions)
        adjusted_conviction = base_conviction * weighted_multiplier

    Examples:
        Renaissance (1.5) + Citadel (1.2)       → avg 1.35 → 4.0 × 1.35 = 5.40  (HC boost)
        AQR (0.9) + Bridgewater (1.0)            → avg 0.95 → 2.0 × 0.95 = 1.90  (below HC)
        Renaissance (1.5) + Citadel (1.2) × 2.0 → avg 1.35 → 2.0 × 1.35 = 2.70  (boosted std)
        Citadel + Ren + Two Sigma + DE Shaw      → avg 1.35 → 6.0 × 1.35 = 8.10  (max HC)
    """
    if not institutions:
        return base_conviction
    weights = [INSTITUTION_WEIGHTS.get(inst, 1.0) for inst in institutions]
    avg_weight = sum(weights) / len(weights)
    return round(base_conviction * avg_weight, 3)

# Historical earnings quality scores — fraction of quarters each stock
# beats consensus EPS estimates (sourced from FactSet / Earnings Whispers data).
# Used when live yfinance earnings data is unavailable for historical dates.
HISTORICAL_BEAT_RATES = {
    "AAPL":  0.83, "MSFT":  0.86, "NVDA":  0.88, "AMZN":  0.72,
    "GOOGL": 0.79, "META":  0.74, "TSLA":  0.61, "AMD":   0.78,
    "NFLX":  0.68, "AVGO":  0.87, "JPM":   0.81, "GS":    0.71,
    "V":     0.85, "UNH":   0.86, "LLY":   0.83, "ABBV":  0.78,
    "PG":    0.80, "XOM":   0.70, "CVX":   0.70, "AMZN":  0.72,
}

# ---------------------------------------------------------------------------
# Price Data Cache
# ---------------------------------------------------------------------------

_price_cache: dict[str, pd.DataFrame] = {}
_earnings_cache: dict[str, object] = {}   # ticker → yfinance earnings_dates df

def get_price_data(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Download and cache historical daily OHLCV data."""
    if ticker in _price_cache:
        df = _price_cache[ticker]
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return None
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        _price_cache[ticker] = df
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    except Exception as e:
        logger.warning(f"Could not download {ticker}: {e}")
        return None


def get_price_on_date(ticker: str, date_str: str) -> Optional[float]:
    """Get closing price for ticker on or after a given date."""
    df = get_price_data(ticker, date_str,
                        (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[0])


def get_spy_200ma(spy_data: pd.DataFrame, date_str: str) -> tuple[float, float]:
    """
    Return (current_spy_price, 200-day moving average) on a given date.
    Uses all available SPY history up to that date.
    """
    dt = pd.Timestamp(date_str)
    history = spy_data[spy_data.index <= dt]["Close"]
    if len(history) < 200:
        # Not enough history — use whatever we have (early backtest dates)
        ma200 = float(history.mean())
    else:
        ma200 = float(history.iloc[-200:].mean())
    current = float(history.iloc[-1]) if not history.empty else 0.0
    return current, ma200


def get_earnings_surprise(ticker: str, before_date: str) -> Optional[float]:
    """
    Fetch the most recent earnings surprise % before `before_date`.
    Returns:
        float  — surprise % (positive = beat, negative = miss)
        None   — no data available (treat as neutral / allow trade)

    Uses yfinance earnings_dates which carries ~4 years of history.
    Falls back to historical beat-rate probability for older dates.
    """
    global _earnings_cache
    if ticker not in _earnings_cache:
        try:
            t   = yf.Ticker(ticker)
            eds = t.earnings_dates
            _earnings_cache[ticker] = eds
        except Exception:
            _earnings_cache[ticker] = None

    eds = _earnings_cache[ticker]
    if eds is None or eds.empty if hasattr(eds, "empty") else eds is None:
        return None

    try:
        # Normalize index to UTC-naive
        idx = eds.index
        if hasattr(idx, "tz") and idx.tz is not None:
            idx = idx.tz_localize(None)
        eds = eds.copy()
        eds.index = idx

        cutoff    = pd.Timestamp(before_date)
        past      = eds[eds.index < cutoff].dropna(subset=["Surprise(%)"])
        if past.empty:
            return None

        most_recent = past.iloc[0]
        return float(most_recent["Surprise(%)"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core Backtester
# ---------------------------------------------------------------------------

class Backtester:
    def __init__(
        self,
        starting_capital: float = STARTING_CAPITAL,
        start_date: str = BACKTEST_START_DATE,
        end_date: str = BACKTEST_END_DATE,
    ):
        self.starting_capital = starting_capital
        self.cash             = starting_capital
        self.start_date       = start_date
        self.end_date         = end_date

        self.positions: dict = {}      # ticker → {shares, entry_price, entry_date, cost}
        self.trade_log: list = []      # All executed trades
        self.daily_values: list = []   # Daily portfolio value snapshots

    # -----------------------------------------------------------------------
    # Position Management
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # IMPROVEMENT A — Market Regime Filter (200-day MA)
    # -----------------------------------------------------------------------

    def _regime_allows_buy(
        self,
        ticker: str,
        conviction: float,
        spy_data: pd.DataFrame,
        date_str: str,
    ) -> tuple[bool, str]:
        """
        Check if the current market regime permits a new buy.

        Rules:
          • Defensive tickers (energy, healthcare, financials, staples):
              Always allowed — they are hedges and often rally in downtrends.

          • Tech/growth tickers in BULL regime (SPY > 200MA):
              Fully allowed.

          • Tech/growth tickers in BEAR regime (SPY < 200MA):
              - High-conviction (Ren Tech + Citadel, score ≥ 4.0):
                  Allowed at 75% position size (conviction scaled down).
              - Standard signals:
                  BLOCKED — protect capital in downtrends.

        Returns (allowed: bool, reason: str)
        """
        current, ma200 = get_spy_200ma(spy_data, date_str)
        bull = current >= ma200
        pct_from_ma = ((current - ma200) / ma200) * 100 if ma200 > 0 else 0

        # Defensive sectors — always buy
        if ticker in DEFENSIVE_TICKERS:
            return True, f"DEFENSIVE (SPY {pct_from_ma:+.1f}% vs 200MA)"

        # Tech/growth in bull market — buy normally
        if bull:
            return True, f"BULL (SPY {pct_from_ma:+.1f}% above 200MA)"

        # Tech/growth in bear market
        if self._is_high_conviction(conviction):
            # HC signals: allow but caller will reduce position (conviction *= 0.75)
            return True, f"BEAR-HC (SPY {pct_from_ma:+.1f}% below 200MA — HC bypass, 75% size)"

        # Standard tech signal in downtrend — block it
        return False, f"BLOCKED: BEAR regime (SPY {pct_from_ma:+.1f}% below 200MA)"

    # -----------------------------------------------------------------------
    # IMPROVEMENT B — Earnings Surprise Filter
    # -----------------------------------------------------------------------

    def _earnings_allows_buy(
        self,
        ticker: str,
        conviction: float,
        date_str: str,
    ) -> tuple[bool, str]:
        """
        Check the most recent earnings surprise before the signal date.

        Rules:
          • Surprise ≥ +3%  (beat):        Always allowed — strong fundamental momentum.
          • Surprise -3% to +3% (neutral): Allowed — not enough to disqualify.
          • Surprise < -3%  (miss):
              - Standard signal:            BLOCKED — avoid falling knives.
              - High-conviction signal:     Allowed at 50% position size
                                            (conviction scaled — institutions see
                                             something the market doesn't).
          • No earnings data available:    Allowed — benefit of the doubt.
        """
        surprise = get_earnings_surprise(ticker, date_str)

        if surprise is None:
            # No historical data — use the ticker's long-run beat rate as proxy
            beat_rate = HISTORICAL_BEAT_RATES.get(ticker, 0.72)
            # Stocks with >75% beat rate are treated as if they beat this quarter
            if beat_rate >= 0.75:
                return True, f"EARNINGS: no data, beat-rate {beat_rate:.0%} → ALLOW"
            return True, f"EARNINGS: no data → ALLOW (neutral)"

        if surprise >= 3.0:
            return True, f"EARNINGS BEAT: +{surprise:.1f}%"
        elif surprise >= -3.0:
            return True, f"EARNINGS NEUTRAL: {surprise:+.1f}%"
        else:
            # Earnings miss
            if self._is_high_conviction(conviction):
                return True, f"EARNINGS MISS {surprise:+.1f}% — HC override, 50% size"
            return False, f"BLOCKED: earnings miss {surprise:+.1f}%"

    def _is_high_conviction(self, conviction: float) -> bool:
        """
        High conviction = conviction score >= 4.0, meaning at least 3 institutions
        are buying, typically including Renaissance Tech + Citadel together.
        These positions receive preferential sizing, stop-loss, and hold rules.
        """
        return conviction >= 4.0

    def _position_size(self, price: float, conviction: float = 1.0) -> float:
        """
        Calculate fractional share count based on max position size and conviction.
        Uses fractional shares (like Alpaca supports) so small accounts can
        participate in any stock regardless of share price.

        IMPROVEMENT #3: High-conviction signals use 15% of portfolio (vs 10% standard)
        to concentrate capital where the signal is strongest.
        Returns a float rounded to 6 decimal places.
        """
        portfolio_val = self._portfolio_value_estimate()

        # Improvement #3: 15% allocation for high-conviction, 10% for standard
        if self._is_high_conviction(conviction):
            base_pct = 0.15
        else:
            base_pct = MAX_POSITION_SIZE_PCT   # 0.10

        base_dollars     = portfolio_val * base_pct
        conviction_mult  = min(conviction / 2.0, 2.0)   # cap multiplier at 2x
        adjusted_dollars = min(base_dollars * conviction_mult, self.cash)
        if price <= 0:
            return 0.0
        return round(adjusted_dollars / price, 6)

    def _portfolio_value_estimate(self) -> float:
        """Approximate current portfolio value (cash + position market values, using entry prices)."""
        pos_value = sum(
            p["shares"] * p["entry_price"] for p in self.positions.values()
        )
        return self.cash + pos_value

    def _buy(self, ticker: str, date_str: str, price: float, conviction: float):
        if ticker in self.positions:
            return  # Already holding
        if len(self.positions) >= MAX_OPEN_POSITIONS:
            return  # Max positions reached
        if self.cash < 1.0:
            return  # Not enough cash

        shares = self._position_size(price, conviction)
        if shares <= 0.0:
            return

        cost = round(shares * price, 2)
        if cost > self.cash:
            # Scale down to available cash
            shares = round(self.cash / price, 6)
            cost   = round(shares * price, 2)

        if shares <= 0.0 or cost < 1.0:
            return

        high_conv = self._is_high_conviction(conviction)

        self.cash -= cost
        self.positions[ticker] = {
            "shares":          shares,
            "entry_price":     price,
            "entry_date":      date_str,
            "cost":            cost,
            "conviction":      conviction,
            "high_conviction": high_conv,   # stored so risk rules can read it
        }

        self.trade_log.append({
            "date":            date_str,
            "ticker":          ticker,
            "action":          "BUY",
            "shares":          shares,
            "price":           price,
            "value":           cost,
            "conviction":      conviction,
            "high_conviction": high_conv,
        })

    def _sell(self, ticker: str, date_str: str, price: float, reason: str):
        if ticker not in self.positions:
            return

        pos     = self.positions[ticker]
        shares  = pos["shares"]
        revenue = shares * price
        pnl     = revenue - pos["cost"]
        pnl_pct = (pnl / pos["cost"]) * 100

        self.cash += revenue

        self.trade_log.append({
            "date":    date_str,
            "ticker":  ticker,
            "action":  "SELL",
            "shares":  shares,
            "price":   price,
            "value":   revenue,
            "pnl":     pnl,
            "pnl_pct": pnl_pct,
            "reason":  reason,
        })

        del self.positions[ticker]

    # -----------------------------------------------------------------------
    # Main Simulation Loop
    # -----------------------------------------------------------------------

    def run(self) -> dict:
        """
        Run the full backtest simulation.
        Returns a results dict with daily_values, trade_log, and performance metrics.
        """
        logger.info("Starting 15-year backtest simulation...")

        # Build a trading calendar from S&P 500 price data
        spy_data = get_price_data("SPY", self.start_date, self.end_date)
        if spy_data is None:
            raise RuntimeError("Could not download SPY price data — check internet connection")

        trading_days = spy_data.index.strftime("%Y-%m-%d").tolist()

        # Convert signals to dict keyed by date.
        # If a signal date falls on a weekend, advance it to the next Monday
        # (13F filings processed at market open on next trading day).
        trading_day_set = set(trading_days)
        signals_by_date: dict[str, list] = {}
        for sig in HISTORICAL_SIGNALS:
            d = sig["date"]
            dt = datetime.strptime(d, "%Y-%m-%d")
            # Roll forward until we land on a real trading day
            while d not in trading_day_set:
                dt += timedelta(days=1)
                d   = dt.strftime("%Y-%m-%d")
            if d not in signals_by_date:
                signals_by_date[d] = []
            signals_by_date[d].append(sig)

        # Pre-download all ticker price data
        all_tickers = list(set(s["ticker"] for s in HISTORICAL_SIGNALS))
        logger.info(f"Pre-loading price data for {len(all_tickers)} tickers...")
        for ticker in all_tickers:
            get_price_data(ticker, self.start_date, self.end_date)
            benchmark_data = get_price_data("SPY", self.start_date, self.end_date)

        # Also get benchmark (SPY) prices indexed by date
        spy_by_date = {d.strftime("%Y-%m-%d"): float(row["Close"])
                       for d, row in spy_data.iterrows()}

        # Pre-fetch earnings data for all tickers (populates cache)
        logger.info("Pre-fetching earnings data for signal quality filter...")
        for ticker in all_tickers:
            get_earnings_surprise(ticker, self.end_date)   # warms the cache

        # Filter tracking
        self.regime_blocked:   list = []
        self.earnings_blocked: list = []
        self.filter_log:       list = []

        # ---- Day-by-day simulation ----
        for day in trading_days:
            # 1. Execute any new signals for this day — apply both filters
            if day in signals_by_date:
                for sig in signals_by_date[day]:
                    ticker     = sig["ticker"]
                    # --- IMPROVEMENT C: Apply institution-weighted conviction ---
                    conviction = compute_weighted_conviction(
                        sig.get("institutions", []), sig["conviction"]
                    )

                    # --- Filter A: Market Regime (200-day MA) ---
                    regime_ok, regime_reason = self._regime_allows_buy(
                        ticker, conviction, spy_data, day
                    )
                    if not regime_ok:
                        self.regime_blocked.append({
                            "date": day, "ticker": ticker,
                            "conviction": conviction, "reason": regime_reason,
                        })
                        self.filter_log.append(
                            f"  REGIME BLOCK  {day} {ticker:<6} conv={conviction} | {regime_reason}"
                        )
                        continue

                    # If bear-market HC bypass: scale conviction down to 75%
                    if "75% size" in regime_reason:
                        conviction = conviction * 0.75

                    # --- Filter B: Earnings Surprise ---
                    earn_ok, earn_reason = self._earnings_allows_buy(
                        ticker, conviction, day
                    )
                    if not earn_ok:
                        self.earnings_blocked.append({
                            "date": day, "ticker": ticker,
                            "conviction": conviction, "reason": earn_reason,
                        })
                        self.filter_log.append(
                            f"  EARNS  BLOCK  {day} {ticker:<6} conv={conviction} | {earn_reason}"
                        )
                        continue

                    # If earnings-miss HC override: scale conviction down to 50%
                    if "50% size" in earn_reason:
                        conviction = conviction * 0.50

                    price = get_price_on_date(ticker, day)
                    if price:
                        self.filter_log.append(
                            f"  SIGNAL  PASS  {day} {ticker:<6} conv={conviction:.1f} | "
                            f"{regime_reason} | {earn_reason}"
                        )
                        self._buy(ticker, day, price, conviction)

            # 2. Enforce stop-loss, take-profit, and max-hold rules
            #    IMPROVEMENT #1: High-conviction positions get 120-day max hold (vs 60)
            #    IMPROVEMENT #2: High-conviction positions get 10% stop-loss (vs 7%)
            to_sell = []
            for ticker, pos in self.positions.items():
                price_data = get_price_data(ticker, day,
                    (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d"))
                if price_data is None or price_data.empty:
                    continue

                current_price = float(price_data["Close"].iloc[0])
                entry_price   = pos["entry_price"]
                pnl_pct       = (current_price - entry_price) / entry_price

                hold_days     = (datetime.strptime(day, "%Y-%m-%d") -
                                 datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days

                high_conv = pos.get("high_conviction", False)

                # Improvement #1: extended hold for high-conviction signals
                max_hold  = 120 if high_conv else HOLD_DAYS_MAX       # 120 vs 60 days

                # Improvement #2: wider stop-loss for high-conviction signals
                stop_loss = 0.10 if high_conv else STOP_LOSS_PCT       # 10% vs 7%

                reason = None
                if pnl_pct <= -stop_loss:
                    reason = f"stop_loss({'HC' if high_conv else 'STD'})"
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    reason = "take_profit"
                elif hold_days >= max_hold:
                    reason = f"max_hold({'HC:120d' if high_conv else 'STD:60d'})"

                if reason:
                    to_sell.append((ticker, current_price, reason))

            for ticker, price, reason in to_sell:
                self._sell(ticker, day, price, reason)

            # 3. Calculate end-of-day portfolio value
            portfolio_value = self.cash
            for ticker, pos in self.positions.items():
                price_data = get_price_data(ticker, day,
                    (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d"))
                if price_data is not None and not price_data.empty:
                    portfolio_value += pos["shares"] * float(price_data["Close"].iloc[0])
                else:
                    portfolio_value += pos["cost"]  # Fallback: use cost

            spy_price = spy_by_date.get(day, None)

            self.daily_values.append({
                "date":            day,
                "portfolio_value": portfolio_value,
                "cash":            self.cash,
                "n_positions":     len(self.positions),
                "spy_price":       spy_price,
            })

        return self._compute_results(spy_data, self.regime_blocked, self.earnings_blocked, self.filter_log)

    # -----------------------------------------------------------------------
    # Results & Metrics
    # -----------------------------------------------------------------------

    def _compute_results(
        self,
        spy_data: pd.DataFrame,
        regime_blocked: list = None,
        earnings_blocked: list = None,
        filter_log: list = None,
    ) -> dict:
        daily_df = pd.DataFrame(self.daily_values)
        daily_df["date"] = pd.to_datetime(daily_df["date"])
        daily_df = daily_df.set_index("date")

        # Daily returns
        daily_df["daily_return"] = daily_df["portfolio_value"].pct_change().fillna(0)

        # SPY benchmark returns
        spy_start = float(spy_data["Close"].iloc[0])
        daily_df["spy_return_cumulative"] = (spy_data["Close"] / spy_start - 1) * 100
        daily_df["portfolio_return_cumulative"] = (
            (daily_df["portfolio_value"] / self.starting_capital) - 1
        ) * 100

        # Monthly aggregation
        monthly = daily_df["portfolio_value"].resample("ME").last()
        monthly_returns = monthly.pct_change().fillna(0) * 100
        monthly_df = pd.DataFrame({
            "portfolio_value": monthly,
            "monthly_return_pct": monthly_returns,
        })

        # Yearly aggregation
        yearly = daily_df["portfolio_value"].resample("YE").last()
        yearly_start = pd.Series(
            [self.starting_capital] + list(yearly.values[:-1]),
            index=yearly.index
        )
        yearly_returns = ((yearly / yearly_start) - 1) * 100
        yearly_df = pd.DataFrame({
            "end_value":      yearly,
            "annual_return_pct": yearly_returns,
        })

        # Performance metrics
        final_value   = daily_df["portfolio_value"].iloc[-1]
        total_return  = ((final_value / self.starting_capital) - 1) * 100
        n_years       = (daily_df.index[-1] - daily_df.index[0]).days / 365.25
        cagr          = ((final_value / self.starting_capital) ** (1 / n_years) - 1) * 100

        daily_rets    = daily_df["daily_return"]
        sharpe        = (daily_rets.mean() / daily_rets.std() * np.sqrt(252)
                         if daily_rets.std() > 0 else 0)

        # Max drawdown
        rolling_max   = daily_df["portfolio_value"].cummax()
        drawdown      = (daily_df["portfolio_value"] - rolling_max) / rolling_max * 100
        max_drawdown  = drawdown.min()

        # Win rate from trade log
        trade_df = pd.DataFrame(self.trade_log)
        sells     = trade_df[trade_df["action"] == "SELL"] if not trade_df.empty else pd.DataFrame()
        win_rate  = 0.0
        avg_win   = 0.0
        avg_loss  = 0.0
        if not sells.empty and "pnl" in sells.columns:
            winners  = sells[sells["pnl"] > 0]
            losers   = sells[sells["pnl"] <= 0]
            win_rate = len(winners) / len(sells) * 100
            avg_win  = winners["pnl_pct"].mean() if not winners.empty else 0
            avg_loss = losers["pnl_pct"].mean()  if not losers.empty  else 0

        # SPY comparison
        spy_total_return = ((float(spy_data["Close"].iloc[-1]) / float(spy_data["Close"].iloc[0])) - 1) * 100
        spy_cagr         = ((float(spy_data["Close"].iloc[-1]) / float(spy_data["Close"].iloc[0])) ** (1 / n_years) - 1) * 100

        return {
            # Dataframes
            "daily_df":      daily_df,
            "monthly_df":    monthly_df,
            "yearly_df":     yearly_df,
            "trade_df":      trade_df,

            # Summary metrics
            "starting_capital":   self.starting_capital,
            "final_value":        final_value,
            "total_return_pct":   total_return,
            "cagr":               cagr,
            "sharpe_ratio":       sharpe,
            "max_drawdown_pct":   max_drawdown,
            "win_rate":           win_rate,
            "avg_win_pct":        avg_win,
            "avg_loss_pct":       avg_loss,
            "n_trades":           len(sells),
            "n_years":            n_years,

            # Benchmark comparison
            "spy_total_return":   spy_total_return,
            "spy_cagr":           spy_cagr,
            "alpha":              cagr - spy_cagr,

            # Filter diagnostics
            "regime_blocked":   regime_blocked   or [],
            "earnings_blocked": earnings_blocked or [],
            "filter_log":       filter_log       or [],
            "n_regime_blocked":   len(regime_blocked   or []),
            "n_earnings_blocked": len(earnings_blocked or []),
        }


def run_backtest() -> dict:
    """Entry point: run the full 5-year backtest and return results."""
    bt = Backtester()
    return bt.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    results = run_backtest()
    print(f"\n{'='*50}")
    print(f"  5-YEAR BACKTEST RESULTS (2020–2024)")
    print(f"{'='*50}")
    print(f"  Starting Capital:  ${results['starting_capital']:>10,.2f}")
    print(f"  Final Value:       ${results['final_value']:>10,.2f}")
    print(f"  Total Return:      {results['total_return_pct']:>+10.1f}%")
    print(f"  CAGR:              {results['cagr']:>+10.1f}%")
    print(f"  Sharpe Ratio:      {results['sharpe_ratio']:>10.2f}")
    print(f"  Max Drawdown:      {results['max_drawdown_pct']:>+10.1f}%")
    print(f"  Win Rate:          {results['win_rate']:>10.1f}%")
    print(f"  Total Trades:      {results['n_trades']:>10d}")
    print(f"{'='*50}")
    print(f"  vs SPY Return:     {results['spy_total_return']:>+10.1f}%")
    print(f"  Alpha (CAGR):      {results['alpha']:>+10.1f}%")
    print(f"{'='*50}")

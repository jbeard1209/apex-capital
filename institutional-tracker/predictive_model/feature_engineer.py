#!/usr/bin/env python3
"""
APEX CAPITAL — FEATURE ENGINEER
=================================
Builds the feature matrix used for ML training and inference.

Features (28 total)
-------------------
  Filing / timing:
    days_since_filing         — how stale is the 13F? (lower = fresher signal)
    filing_delay_days         — days from quarter-end to filing date
    quarter_of_year           — Q1–Q4 seasonality
    amendment_flag            — 1 if 13F-HR/A (amended = higher conviction)

  Conviction:
    conviction_score          — raw score from signal_engine
    n_institutions            — how many tracked funds hold this ticker
    herding_flag              — 3+ institutions added in same quarter
    quant_consensus           — Renaissance + Two Sigma (or similar) both hold it
    filing_quarter_consensus  — fraction of all tracked funds that increased this ticker

  Position-change:
    pct_change_shares         — % change in shares vs prior filing
    new_position_flag         — 1 if this is a new position
    large_increase_flag       — 1 if shares increased > 50% QoQ
    position_size_pct_aum     — value_usd / total fund AUM (larger = higher conviction)

  Historical pattern:
    hold_streak_quarters      — consecutive quarters this institution held it
    avg_conviction_3q         — rolling 3-quarter average conviction for ticker
    institution_hit_rate      — fraction of past picks by this institution that beat S&P

  Price / market context:
    price_momentum_20d        — % price change in 20 days before filing
    price_vs_52w_high         — how far below 52-week high (0=at high, 1=far below)
    volatility_20d            — annualised 20-day volatility at filing time
    days_since_ipo            — age of the stock (newer = higher growth potential)
    short_interest_pct        — short interest as % of float (contrarian signal)

  Portfolio:
    portfolio_weight          — current allocation (0 if not held)

  R&D Bridge (live signals from ml_rd_bridge_agent.py):
    rd_signals_available      — 1 if bridge has data for this ticker today
    rd_ticker_rsi             — RSI from R&D research team (0-100)
    rd_ticker_pct_from_50ma   — % above/below 50-day MA (R&D computed)
    rd_ticker_trend_bullish   — 1 if R&D trend = bullish
    rd_ticker_conviction      — R&D conviction score (1-5)
    rd_ticker_action_buy      — 1 if R&D recommends BUY_NEW or ADD
    rd_sector_rsi             — RSI of the ticker's sector ETF
    rd_sector_bullish         — 1 if sector ETF trend = bullish
    rd_sector_ret_1m          — sector ETF 1-month return (%)
    rd_regime_bull            — 1 if market regime = bull
    rd_vix                    — current VIX level
    rd_spy_rsi                — SPY RSI (market overbought/oversold)
    rd_warning_count          — number of active R&D warning signals

  Commodity momentum (macro risk appetite signals — 100% backfillable historically):
    oil_momentum_20d          — USO 20-day return (oil demand/supply proxy)
    gold_momentum_20d         — GLD 20-day return (risk-off / inflation hedge proxy)
    dxy_momentum_20d          — UUP 20-day return (USD strength; negative for equities)
"""

import sys
import json
import sqlite3
import logging
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.edgar_historical_scraper import get_db
from predictive_model.data_collector import _ensure_collector_tables
from predictive_model.form4_collector import (
    get_insider_buy_flag, get_insider_buy_dollars, get_insider_is_ceo_cfo,
    _ensure_form4_table,
)

logger = logging.getLogger(__name__)

FEATURE_COLS = [
    # Filing / timing
    "days_since_filing",
    "filing_delay_days",
    "quarter_of_year",
    "amendment_flag",
    # Conviction
    "conviction_score",
    "n_institutions",
    "herding_flag",
    "quant_consensus",
    "filing_quarter_consensus",
    # Position-change
    "pct_change_shares",
    "new_position_flag",
    "large_increase_flag",
    "position_size_pct_aum",
    # Historical pattern
    "hold_streak_quarters",
    "avg_conviction_3q",
    "institution_hit_rate",
    # Price / market
    "price_momentum_20d",
    "price_momentum_5d",   # short-term 5-day momentum (mlsugg-2026-003)
    "price_vs_52w_high",
    "volatility_20d",
    # Portfolio
    "portfolio_weight",
    # Institution × market regime (how well does THIS institution pick stocks in THIS regime)
    "institution_regime_hit_rate",
    # Market context at filing date — 100% backfilled historically via label builder
    # (rd_regime_bull, rd_vix, rd_spy_rsi stored in pipeline_snapshots DB column)
    "rd_regime_bull",
    "rd_vix",
    "rd_spy_rsi",
    # Earnings surprise (price-reaction proxy at nearest earnings date before filing)
    "earnings_surprise_flag",
    # NOTE: days_since_ipo, short_interest_pct, insider_buy_dollars, insider_is_ceo_cfo,
    # and sector_momentum_vs_spy were REMOVED 2026-06-14. A coverage audit found all five
    # are 0% non-zero across the 10,555-row training set — their backfill (label builder /
    # Form 4 / sector-rotation enrichment) never actually populated the DB columns, so they
    # were zero-variance noise. Same rationale as the bridge/commodity exclusions below.
    # Removing them improved walk-forward AUC (see model_test_report.json). Re-add ONLY after
    # confirming the backfill writes non-zero values AND it lifts embargoed walk-forward AUC.
    # NOTE: Commodity momentum (oil_momentum_20d, gold_momentum_20d, dxy_momentum_20d) are
    # intentionally EXCLUDED from training features. 100% fill confirmed (532k rows), but AUC
    # regressed from 0.6057 → 0.5842 when included — these macro signals are collinear with the
    # existing rd_regime_bull / rd_vix / rd_spy_rsi features and add noise rather than signal for
    # individual stock outcome prediction. They ARE computed in get_inference_features() as context.
    # Re-evaluate if AUC consistently degrades after regime de-correlation analysis.
    #
    # NOTE: Bridge features (rd_signals_available, rd_ticker_trend_bullish, rd_ticker_conviction,
    # rd_ticker_action_buy, rd_sector_rsi, rd_sector_bullish, rd_sector_ret_1m, rd_warning_count,
    # rd_ticker_rsi, rd_ticker_pct_from_50ma) are intentionally EXCLUDED from training features.
    # They are all-zero for the 16,918 historical training rows (bridge only runs from 2026-05-14).
    # Zero-variance features in training = noise that hurts AUC. They ARE still computed in
    # get_inference_features() and sit in the DataFrame as context — the model just ignores them.
    # Re-add to FEATURE_COLS once ~6 months of labeled data accumulates with non-zero values.
]

_QUANT_FUNDS = {"Renaissance Tech", "Two Sigma", "D.E. Shaw", "AQR Capital"}

# yfinance cache — avoid redundant downloads during a single session
_yf_info_cache: dict = {}

# R&D bridge signals cache — loaded once per process from rd_ml_bridge_signals.json
_rd_bridge_cache: dict = {}
_rd_bridge_loaded: bool = False

_RD_BRIDGE_PATH = Path(__file__).parent.parent / "data" / "rd_ml_bridge_signals.json"


def _load_rd_bridge() -> None:
    """Load the R&D bridge signals file (once per process). Silently skips if missing."""
    global _rd_bridge_cache, _rd_bridge_loaded
    if _rd_bridge_loaded:
        return
    _rd_bridge_loaded = True
    try:
        with open(_RD_BRIDGE_PATH) as f:
            _rd_bridge_cache = json.load(f)
        n = len(_rd_bridge_cache.get("tickers", {}))
        logger.debug(f"  feature_engineer: loaded R&D bridge signals ({n} tickers)")
    except FileNotFoundError:
        logger.debug("  feature_engineer: rd_ml_bridge_signals.json not found; R&D features will be 0")
        _rd_bridge_cache = {}
    except Exception as e:
        logger.warning(f"  feature_engineer: could not load R&D bridge: {e}")
        _rd_bridge_cache = {}


def _get_rd_features(ticker: str) -> dict:
    """
    Return R&D bridge features for a ticker.
    Returns a dict with all rd_ feature keys.
    When the ticker is not in the bridge (historical data, unknown ticker),
    rd_signals_available=0 and all other rd_ features are set to neutral defaults.
    Tree models will learn the (0,neutral) encoding means "no R&D signal."
    """
    _load_rd_bridge()

    regime: dict = _rd_bridge_cache.get("regime", {})
    tickers: dict = _rd_bridge_cache.get("tickers", {})
    td: dict = tickers.get(ticker, {})
    available = 1 if td else 0

    return {
        "rd_signals_available":     available,
        "rd_ticker_rsi":            float(td.get("rd_ticker_rsi", 0)),
        "rd_ticker_pct_from_50ma":  float(td.get("rd_ticker_pct_from_50ma", 0)),
        "rd_ticker_trend_bullish":  int(td.get("rd_ticker_trend_bullish", 0)),
        "rd_ticker_conviction":     float(td.get("rd_ticker_conviction", 0)),
        "rd_ticker_action_buy":     int(td.get("rd_ticker_action_buy", 0)),
        "rd_sector_rsi":            float(td.get("rd_sector_rsi", 0)),
        "rd_sector_bullish":        int(td.get("rd_sector_bullish", 0)),
        "rd_sector_ret_1m":         float(td.get("rd_sector_ret_1m", 0)),
        # Market-wide regime (same for all tickers)
        "rd_regime_bull":           int(regime.get("rd_regime_bull", 0)),
        "rd_vix":                   float(regime.get("rd_vix", 0)),
        "rd_spy_rsi":               float(regime.get("rd_spy_rsi", 0)),
        "rd_warning_count":         int(regime.get("rd_warning_count", 0)),
    }


# =============================================================================
# COMMODITY MOMENTUM HELPERS
# =============================================================================

# USO = oil ETF, GLD = gold ETF, UUP = USD index ETF (proxy for DXY)
_COMMODITY_TICKERS = {"oil_momentum_20d": "USO", "gold_momentum_20d": "GLD", "dxy_momentum_20d": "UUP"}

# Cache: {ticker: pd.Series of Close} — shared with yfinance price fetches
_commodity_cache: dict = {}
_commodity_cache_date: Optional[date] = None


def _get_commodity_momentum() -> dict:
    """
    Return 20-day momentum for oil (USO), gold (GLD), and USD (UUP).
    Fetched live from yfinance, cached per process.
    Returns dict with oil_momentum_20d, gold_momentum_20d, dxy_momentum_20d (0.0 if unavailable).
    """
    global _commodity_cache, _commodity_cache_date
    today = date.today()

    if _commodity_cache_date != today or not _commodity_cache:
        try:
            import yfinance as yf
            import pandas as pd
            syms = list(_COMMODITY_TICKERS.values())
            df = yf.download(syms, period="45d", auto_adjust=True, progress=False)
            close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df
            _commodity_cache = {}
            for sym in syms:
                if sym in close.columns:
                    _commodity_cache[sym] = close[sym].dropna()
            _commodity_cache_date = today
        except Exception as e:
            logger.debug(f"  commodity momentum download failed: {e}")
            return {"oil_momentum_20d": 0.0, "gold_momentum_20d": 0.0, "dxy_momentum_20d": 0.0}

    result = {}
    for feat, sym in _COMMODITY_TICKERS.items():
        s = _commodity_cache.get(sym)
        if s is not None and len(s) >= 22:
            try:
                now  = float(s.iloc[-1])
                prev = float(s.iloc[-22])   # ~20 trading days back
                result[feat] = (now - prev) / prev if prev > 0 else 0.0
            except Exception:
                result[feat] = 0.0
        else:
            result[feat] = 0.0
    return result


# =============================================================================
# SECTOR MOMENTUM VS SPY HELPER
# =============================================================================

# GICS sector name → Select Sector SPDR ETF ticker
_SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Financials":             "XLF",
    "Health Care":            "XLV",
    "Consumer Discretionary": "XLY",
    "Industrials":            "XLI",
    "Communication Services": "XLC",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}

# Cache: {(etf_ticker, ref_date_iso): float}
_sector_momentum_cache: dict = {}
# Cache: {ticker: sector_etf_symbol}
_ticker_sector_cache: dict = {}


def _get_sector_etf(ticker: str) -> Optional[str]:
    """Map a stock ticker to its GICS sector ETF. Returns None if unknown."""
    if ticker in _ticker_sector_cache:
        return _ticker_sector_cache[ticker]
    info = _yf_info(ticker)
    sector_name = info.get("sector", "")
    etf = _SECTOR_ETF_MAP.get(sector_name)
    _ticker_sector_cache[ticker] = etf
    return etf


def _etf_momentum_20d(etf_ticker: str, ref_date_str: str) -> Optional[float]:
    """
    20-day return of an ETF up to ref_date (or most recent available if ref_date is
    in the past and data isn't available to the exact day).
    Returns None on failure.
    """
    cache_key = (etf_ticker, ref_date_str)
    if cache_key in _sector_momentum_cache:
        return _sector_momentum_cache[cache_key]

    try:
        import yfinance as yf
        import pandas as pd

        ref = date.fromisoformat(ref_date_str) if ref_date_str else date.today()
        start = (ref - timedelta(days=35)).isoformat()
        end   = (ref + timedelta(days=1)).isoformat()

        hist = yf.download(etf_ticker, start=start, end=end,
                           auto_adjust=True, progress=False)
        if hist.empty or len(hist) < 5:
            _sector_momentum_cache[cache_key] = None
            return None

        close = hist["Close"] if "Close" in hist.columns else hist.iloc[:, 0]
        close = close.dropna()
        if len(close) < 5:
            _sector_momentum_cache[cache_key] = None
            return None

        recent = float(close.iloc[-1])
        older  = float(close.iloc[max(0, len(close) - 21)])  # ~20 trading days
        mom    = (recent - older) / older if older > 0 else 0.0
        _sector_momentum_cache[cache_key] = mom
        return mom
    except Exception as e:
        logger.debug(f"  sector_momentum {etf_ticker}@{ref_date_str}: {e}")
        _sector_momentum_cache[cache_key] = None
        return None


def _get_sector_momentum_vs_spy(ticker: str, ref_date_str: str) -> float:
    """
    Returns sector ETF 20d return minus SPY 20d return at ref_date.
    Positive = sector outperforming (tailwind). Zero if data unavailable.
    """
    sector_etf = _get_sector_etf(ticker)
    if not sector_etf:
        return 0.0

    spy_mom    = _etf_momentum_20d("SPY", ref_date_str)
    sector_mom = _etf_momentum_20d(sector_etf, ref_date_str)

    if spy_mom is None or sector_mom is None:
        return 0.0
    return round(sector_mom - spy_mom, 6)


# =============================================================================
# EARNINGS SURPRISE HELPER
# =============================================================================

# Cache: {(ticker, filing_date_iso): int}  (-1 / 0 / +1)
_earnings_surprise_cache: dict = {}


def _get_earnings_surprise(ticker: str, filing_date_str: str) -> int:
    """
    Estimate earnings surprise proxy for a ticker relative to a filing date.
    Looks for the most recent large single-day price move (> ±3%) within 90 days
    before the filing date as a proxy for an earnings reaction day.

    Returns:
        +1  — BEAT (stock moved > +3% on that day)
        -1  — MISS (stock moved < -3%)
         0  — IN-LINE or no earnings data found
    """
    cache_key = (ticker, filing_date_str)
    if cache_key in _earnings_surprise_cache:
        return _earnings_surprise_cache[cache_key]

    result = 0
    try:
        import yfinance as yf
        import pandas as pd

        try:
            filing_dt = date.fromisoformat(filing_date_str)
        except Exception:
            _earnings_surprise_cache[cache_key] = 0
            return 0

        start = (filing_dt - timedelta(days=100)).isoformat()
        end   = (filing_dt + timedelta(days=1)).isoformat()

        tkr = yf.Ticker(ticker)
        hist = tkr.history(start=start, end=end, auto_adjust=True)

        if hist.empty or len(hist) < 5:
            _earnings_surprise_cache[cache_key] = 0
            return 0

        # Look for largest single-day move in window — proxy for earnings day
        daily_ret = hist["Close"].pct_change().dropna()
        # Only consider moves > ±3% as potential earnings reactions
        big_moves = daily_ret[daily_ret.abs() > 0.03]
        if big_moves.empty:
            _earnings_surprise_cache[cache_key] = 0
            return 0

        # Use the biggest absolute move in the window
        max_move_idx = big_moves.abs().idxmax()
        max_move = float(big_moves[max_move_idx])

        if max_move > 0.03:
            result = 1
        elif max_move < -0.03:
            result = -1
        else:
            result = 0

    except Exception as e:
        logger.debug(f"  earnings_surprise error {ticker}: {e}")
        result = 0

    _earnings_surprise_cache[cache_key] = result
    return result


# =============================================================================
# MARKET DATA HELPERS
# =============================================================================

def _yf_info(ticker: str) -> dict:
    if ticker in _yf_info_cache:
        return _yf_info_cache[ticker]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        _yf_info_cache[ticker] = info
        return info
    except Exception:
        _yf_info_cache[ticker] = {}
        return {}


def _get_short_interest(ticker: str) -> float:
    """Short interest as fraction of float. Returns 0.0 if unavailable."""
    info = _yf_info(ticker)
    si = info.get("shortPercentOfFloat") or info.get("shortRatio")
    if si and isinstance(si, (int, float)):
        return float(si)
    return 0.0


def _get_days_since_ipo(ticker: str) -> Optional[int]:
    """Days since first trade date. Proxy for company age."""
    info = _yf_info(ticker)
    founded = info.get("firstTradeDateEpochUtc") or info.get("firstTradeDateMilliseconds")
    if founded:
        try:
            import time
            epoch = founded / 1000 if founded > 1e10 else founded
            first = date.fromtimestamp(epoch)
            return (date.today() - first).days
        except Exception:
            pass
    return None


# =============================================================================
# HOLDINGS FEATURES
# =============================================================================

def _compute_holdings_features(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    From the holdings table, compute per-ticker per-filing features.
    """
    df = pd.read_sql(
        """SELECT h.ticker, h.institution, h.filing_date, h.shares, h.value_usd,
                  f.period_of_report, f.form, f.total_value_usd as fund_total_value
           FROM holdings h
           JOIN filings f ON h.filing_id = f.id
           WHERE h.ticker != ''
           ORDER BY h.filing_date""",
        conn,
    )
    if df.empty:
        return pd.DataFrame()

    df["filing_date"] = pd.to_datetime(df["filing_date"])

    # ── Vectorized rewrite ────────────────────────────────────────────────────
    # The original computed these features in a per-group Python loop over every
    # (ticker, filing_date) pair — ~278k groups, each re-scanning the full ~1.2M
    # row holdings frame three times (quarter consensus, prior-quarter, hold
    # streak). That is O(groups * rows) and never finished in practice, which is
    # why no model could train. Everything below is the same set of features
    # computed with vectorized groupby/merge operations (seconds, not minutes).
    df["is_amend"] = (df["form"] == "13F-HR/A").astype(int)

    g = df.groupby(["ticker", "filing_date"], sort=False)
    feats = g.agg(
        n_institutions=("institution", "nunique"),
        amendment_flag=("is_amend", "max"),
        max_value=("value_usd", "max"),
        max_aum=("fund_total_value", "max"),
        curr_shares=("shares", "sum"),
    ).reset_index()

    # Quant consensus — count DISTINCT quant funds per group (>=2 ⇒ consensus).
    dq = (df[df["institution"].isin(_QUANT_FUNDS)]
          .drop_duplicates(["ticker", "filing_date", "institution"])
          .groupby(["ticker", "filing_date"]).size().rename("quant_count").reset_index())
    feats = feats.merge(dq, on=["ticker", "filing_date"], how="left")
    feats["quant_count"] = feats["quant_count"].fillna(0)

    feats["herding_flag"]     = (feats["n_institutions"] >= 3).astype(int)
    feats["quant_consensus"]  = (feats["quant_count"] >= 2).astype(int)
    feats["position_size_pct_aum"] = (
        feats["max_value"] / feats["max_aum"].clip(lower=1)
    ).round(6)

    # Prior-quarter comparison — within each ticker, the immediately preceding
    # filing's summed shares (groupby shift over date-sorted groups).
    feats = feats.sort_values(["ticker", "filing_date"]).reset_index(drop=True)
    feats["prior_shares"] = feats.groupby("ticker")["curr_shares"].shift(1)
    _has_prior = feats["prior_shares"].notna() & (feats["prior_shares"] > 0)
    feats["pct_change_shares"] = 0.0
    feats.loc[_has_prior, "pct_change_shares"] = (
        (feats.loc[_has_prior, "curr_shares"] - feats.loc[_has_prior, "prior_shares"])
        / feats.loc[_has_prior, "prior_shares"]
    )
    feats["new_position_flag"]   = (~_has_prior).astype(int)
    feats["large_increase_flag"] = ((feats["pct_change_shares"] > 0.5) & _has_prior).astype(int)

    # Hold streak — count of the ticker's distinct filing dates up to and
    # including this one (dense rank). NOTE: the old loop was buggy here (it
    # iterated dates descending and broke on the first date > filing_date, so it
    # returned 0 for every filing except the latest); this is the intended
    # "quarters held" semantics the column name implies.
    feats["hold_streak_quarters"] = (
        feats.groupby("ticker")["filing_date"].rank(method="dense").astype(int)
    )

    # Filing-quarter consensus — fraction of all tracked funds holding *anything*
    # in the ~quarter window around each filing date. Only 155 distinct filing
    # dates exist, so compute per unique date (not per group).
    inst_dates = df[["filing_date", "institution"]].drop_duplicates()
    _fd_counts = {}
    for _fd in feats["filing_date"].unique():
        _m = inst_dates["filing_date"].between(
            _fd - pd.Timedelta(days=95), _fd + pd.Timedelta(days=5))
        _fd_counts[_fd] = inst_dates.loc[_m, "institution"].nunique() or 1
    _qfunds = feats["filing_date"].map(_fd_counts).clip(lower=1)
    feats["filing_quarter_consensus"] = (feats["n_institutions"] / _qfunds).round(4)

    return feats[[
        "ticker", "filing_date", "n_institutions", "herding_flag", "quant_consensus",
        "filing_quarter_consensus", "amendment_flag", "position_size_pct_aum",
        "pct_change_shares", "new_position_flag", "large_increase_flag",
        "hold_streak_quarters",
    ]]


def _compute_institution_hit_rates(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT institution, outcome_20d FROM pipeline_snapshots WHERE outcome_20d IS NOT NULL"
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["institution", "institution_hit_rate"])
    df = pd.DataFrame(rows, columns=["institution", "outcome_20d"])
    df["hit"] = (df["outcome_20d"] > 0).astype(int)
    rates = df.groupby("institution")["hit"].mean().reset_index()
    rates.columns = ["institution", "institution_hit_rate"]
    return rates


def _compute_institution_regime_hit_rates(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Per-institution hit rate broken down by market regime (bull/neutral/bear).
    Uses rd_regime_bull from the DB — values: 1=bull, 0=neutral, -1=bear.

    Returns DataFrame with columns: institution, regime, institution_regime_hit_rate
    Regime -1/0/1 maps to: bear=0.0 default, neutral=0.0 default, bull=0.0 default.
    Falls back to overall hit rate if regime data is unavailable.
    """
    rows = conn.execute(
        """SELECT institution, outcome_20d, rd_regime_bull
           FROM pipeline_snapshots
           WHERE outcome_20d IS NOT NULL"""
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["institution", "rd_regime_bull",
                                     "institution_regime_hit_rate"])
    df = pd.DataFrame(rows, columns=["institution", "outcome_20d", "rd_regime_bull"])
    df["hit"] = (df["outcome_20d"] > 0).astype(int)
    # Fill NULLs (rows before backfill) with 0 = neutral
    df["rd_regime_bull"] = df["rd_regime_bull"].fillna(0).astype(int)

    rates = (
        df.groupby(["institution", "rd_regime_bull"])["hit"]
          .mean()
          .reset_index()
          .rename(columns={"hit": "institution_regime_hit_rate"})
    )
    return rates


def _compute_rolling_avg_conviction(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT ticker, snapshot_date, conviction_score FROM pipeline_snapshots
           WHERE conviction_score IS NOT NULL ORDER BY ticker, snapshot_date"""
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["ticker", "snapshot_date", "avg_conviction_3q"])
    df = pd.DataFrame(rows, columns=["ticker", "snapshot_date", "conviction_score"])
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    # Collapse to ONE conviction per (ticker, snapshot_date) before the rolling
    # window. pipeline_snapshots has multiple rows per ticker/date (one per
    # institution); without this the result carries duplicate (ticker,
    # snapshot_date) keys, and the left-merge in build_feature_matrix multiplies
    # the training set ~3x with duplicated rows — which also leaks identical rows
    # across walk-forward CV folds. Dedup here keeps the matrix 1:1 with snapshots.
    per = (df.groupby(["ticker", "snapshot_date"], as_index=False)["conviction_score"]
             .mean()
             .sort_values(["ticker", "snapshot_date"]))
    per["avg_conviction_3q"] = (
        per.groupby("ticker")["conviction_score"]
           .transform(lambda x: x.rolling(3, min_periods=1).mean())
    )
    return per[["ticker", "snapshot_date", "avg_conviction_3q"]]


# =============================================================================
# MAIN FEATURE MATRIX (for training)
# =============================================================================

def build_feature_matrix(target: str = "outcome_20d") -> tuple:
    """
    Build X, y, meta from pipeline_snapshots.
    target: 'outcome_20d' | 'outcome_5d' | 'outcome_1d' | 'risk_adjusted_20d'

    For binary targets (outcome_*): y = (value > 0).astype(int)
    For continuous targets (risk_adjusted_*): y = raw float array
    """
    conn = get_db()
    _ensure_collector_tables(conn)

    # Pull snapshots — include all new columns if they exist
    available_cols = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_snapshots)")}

    extra_cols = []
    for c in ["price_momentum_20d", "price_momentum_5d", "amendment_flag",
              "position_size_pct_aum", "price_vs_52w_high", "volatility_20d",
              "risk_adjusted_20d", "risk_adjusted_5d", "is_historical",
              "filing_quarter_consensus", "insider_buy_flag",
              # Earnings surprise proxy (backfilled by label builder)
              "earnings_surprise_flag",
              # Form 4 enrichment (backfilled by label builder)
              "insider_buy_dollars", "insider_is_ceo_cfo",
              # Market-context features backfilled historically by label builder
              "rd_regime_bull", "rd_vix", "rd_spy_rsi",
              "rd_ticker_rsi", "rd_ticker_pct_from_50ma",
              # Sector rotation vs SPY (backfilled by label builder)
              "sector_momentum_vs_spy"]:
        if c in available_cols:
            extra_cols.append(c)

    extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""

    snaps = pd.read_sql(
        f"""SELECT id, ticker, snapshot_date, institution, conviction_score,
                   n_institutions, herding_flag, quant_consensus,
                   days_since_filing, portfolio_weight {extra_sql},
                   {target} as label
            FROM pipeline_snapshots
            WHERE {target} IS NOT NULL AND conviction_score IS NOT NULL""",
        conn,
    )

    if snaps.empty:
        logger.warning("  feature_engineer: no labeled snapshots yet")
        conn.close()
        return np.array([]), np.array([]), pd.DataFrame()

    snaps["snapshot_date"]  = pd.to_datetime(snaps["snapshot_date"])
    snaps["quarter_of_year"] = snaps["snapshot_date"].dt.quarter

    # Merge holdings features
    holdings_feats = _compute_holdings_features(conn)
    if not holdings_feats.empty:
        holdings_feats["filing_date"] = pd.to_datetime(holdings_feats["filing_date"])
        # As-of join: for each snapshot, attach the latest holdings_feat for the same
        # ticker with filing_date <= snapshot_date. This was a per-row Python loop
        # (O(n_snaps * n_holdings) — minutes on ~10k snapshots); pd.merge_asof does it
        # vectorized in one pass. Behaviour preserved: the old loop did
        # `latest.update(row)`, so snapshot columns win on any name collision — we
        # replicate that by only bringing holdings columns snaps doesn't already own.
        # These holdings-derived features are stored EMPTY (all-zero) in
        # pipeline_snapshots, so without intervention the empty snapshot copies
        # shadow the real values computed in _compute_holdings_features (verified
        # non-zero: herding 34%, quant_consensus 26%, filing_quarter_consensus
        # 100%). Drop the empty copies so the merge sources them from holdings.
        _resurrect = ["herding_flag", "quant_consensus", "filing_quarter_consensus"]
        snaps = snaps.drop(columns=[c for c in _resurrect if c in snaps.columns])
        hf_cols = [c for c in holdings_feats.columns
                   if c in ("ticker", "filing_date") or c not in snaps.columns]
        hf = holdings_feats[hf_cols].sort_values("filing_date")
        snaps = pd.merge_asof(
            snaps.sort_values("snapshot_date"),
            hf,
            left_on="snapshot_date",
            right_on="filing_date",
            by="ticker",
            direction="backward",
        )
        # Snapshots with no prior matching filing get the same defaults the old
        # else-branch applied (only fills NaNs, so real snapshot values are untouched).
        _no_match_defaults = {
            "pct_change_shares": 0.0, "new_position_flag": 1,
            "large_increase_flag": 0, "hold_streak_quarters": 1,
            "amendment_flag": 0, "position_size_pct_aum": 0.0,
            "filing_quarter_consensus": 0.0,
        }
        for _c, _d in _no_match_defaults.items():
            if _c in snaps.columns:
                snaps[_c] = pd.to_numeric(snaps[_c], errors="coerce").fillna(_d)
    else:
        for c in ["pct_change_shares", "new_position_flag", "large_increase_flag",
                  "hold_streak_quarters", "amendment_flag", "position_size_pct_aum",
                  "filing_quarter_consensus"]:
            if c not in snaps.columns:
                snaps[c] = 0.0

    # Rolling avg conviction
    avg_conv = _compute_rolling_avg_conviction(conn)
    if not avg_conv.empty:
        avg_conv["snapshot_date"] = pd.to_datetime(avg_conv["snapshot_date"])
        snaps = snaps.merge(avg_conv, on=["ticker", "snapshot_date"], how="left")
    if "avg_conviction_3q" not in snaps.columns:
        snaps["avg_conviction_3q"] = snaps["conviction_score"]

    # Institution hit rates (overall)
    hit_rates = _compute_institution_hit_rates(conn)
    if not hit_rates.empty:
        snaps = snaps.merge(hit_rates, on="institution", how="left")
    if "institution_hit_rate" not in snaps.columns:
        snaps["institution_hit_rate"] = 0.5

    # Institution × regime hit rates
    # Determine current regime for each snapshot from rd_regime_bull column (or 0 if missing)
    regime_rates = _compute_institution_regime_hit_rates(conn)
    if not regime_rates.empty:
        # Ensure rd_regime_bull exists in snaps (from extra_cols or default 0)
        if "rd_regime_bull" not in snaps.columns:
            snaps["rd_regime_bull"] = 0
        snaps["rd_regime_bull"] = snaps["rd_regime_bull"].fillna(0).astype(int)
        snaps = snaps.merge(
            regime_rates, on=["institution", "rd_regime_bull"], how="left"
        )
    if "institution_regime_hit_rate" not in snaps.columns:
        snaps["institution_regime_hit_rate"] = 0.5
    snaps["institution_regime_hit_rate"] = snaps["institution_regime_hit_rate"].fillna(0.5)

    # Filing delay
    delays = pd.read_sql(
        "SELECT institution, filing_date, period_of_report FROM filings WHERE period_of_report != ''",
        conn,
    )
    if not delays.empty:
        delays["filing_date"] = pd.to_datetime(delays["filing_date"])
        delays["period_of_report"] = pd.to_datetime(delays["period_of_report"], errors="coerce")
        delays["filing_delay_days"] = (delays["filing_date"] - delays["period_of_report"]).dt.days.clip(0, 90)
        delay_map = delays.groupby("institution")["filing_delay_days"].mean().to_dict()
        snaps["filing_delay_days"] = snaps["institution"].map(delay_map).fillna(45.0)
    else:
        snaps["filing_delay_days"] = 45.0

    conn.close()

    # Ensure all feature columns exist
    for col in FEATURE_COLS:
        if col not in snaps.columns:
            snaps[col] = 0.0
        snaps[col] = pd.to_numeric(snaps[col], errors="coerce").fillna(0.0)

    label_raw = snaps["label"].values

    # Binary vs continuous target
    if target.startswith("risk_adjusted"):
        y = label_raw.astype(float)
    else:
        y = (label_raw > 0).astype(int)

    X    = snaps[FEATURE_COLS].values.astype(float)
    meta = snaps[["ticker", "snapshot_date", "conviction_score"]].reset_index(drop=True)

    pos_rate = float((y > 0).mean()) if len(y) else 0.0
    logger.info(f"  feature_engineer: {len(X)} samples, {len(FEATURE_COLS)} features, "
                f"{pos_rate:.1%} positive, target={target}")
    return X, y, meta


# =============================================================================
# INFERENCE FEATURES (for live prediction)
# =============================================================================

def get_inference_features(signals: list) -> pd.DataFrame:
    """
    Build feature rows for live signals. No labels — for prediction only.
    Fetches market data (momentum, short interest, etc.) per ticker.
    """
    conn = get_db()
    _ensure_collector_tables(conn)
    _ensure_form4_table(conn)

    today = date.today()
    holdings_feats = _compute_holdings_features(conn)
    avg_conv       = _compute_rolling_avg_conviction(conn)
    hit_rates      = _compute_institution_hit_rates(conn)
    hit_map = dict(zip(hit_rates["institution"], hit_rates["institution_hit_rate"])) \
              if not hit_rates.empty else {}

    # Institution × regime hit rate (live: use current R&D regime from bridge)
    regime_rates   = _compute_institution_regime_hit_rates(conn)
    # Determine current regime from bridge signals (0=neutral if bridge unavailable)
    _load_rd_bridge()
    current_regime = int(_rd_bridge_cache.get("regime", {}).get("rd_regime_bull", 0))
    # Build lookup: institution → hit_rate in current regime
    inst_regime_map: dict = {}
    if not regime_rates.empty:
        regime_slice = regime_rates[regime_rates["rd_regime_bull"] == current_regime]
        inst_regime_map = dict(zip(
            regime_slice["institution"],
            regime_slice["institution_regime_hit_rate"],
        ))

    delays = pd.read_sql(
        "SELECT institution, filing_date, period_of_report FROM filings WHERE period_of_report != ''",
        conn,
    )
    delay_map: dict = {}
    if not delays.empty:
        delays["filing_date"] = pd.to_datetime(delays["filing_date"])
        delays["period_of_report"] = pd.to_datetime(delays["period_of_report"], errors="coerce")
        delays["filing_delay_days"] = (delays["filing_date"] - delays["period_of_report"]).dt.days.clip(0, 90)
        delay_map = delays.groupby("institution")["filing_delay_days"].mean().to_dict()

    conn.close()

    # Commodity momentum — fetch once for all rows (market-wide, same for every ticker)
    commodity_mom = _get_commodity_momentum()

    rows = []
    for sig in signals:
        ticker = sig.get("ticker", "")
        if not ticker:
            continue

        inst = sig.get("institution", "")

        filing_date_str = sig.get("filing_date", "")
        days_since = 45
        if filing_date_str:
            try:
                fd = date.fromisoformat(filing_date_str)
                days_since = (today - fd).days
            except Exception:
                pass

        # Holdings features (latest available for this ticker)
        hf_row: dict = {}
        if not holdings_feats.empty:
            hf = holdings_feats[holdings_feats["ticker"] == ticker].sort_values("filing_date")
            if not hf.empty:
                hf_row = hf.iloc[-1].to_dict()

        # Rolling avg conviction
        avg_c = sig.get("conviction_score", sig.get("score", 0.0))
        if not avg_conv.empty:
            ac = avg_conv[avg_conv["ticker"] == ticker]
            if not ac.empty:
                avg_c = float(ac.iloc[-1]["avg_conviction_3q"])

        # Market data (fetched live)
        short_int          = _get_short_interest(ticker)
        days_ipo           = _get_days_since_ipo(ticker)
        earnings_surprise  = _get_earnings_surprise(ticker, filing_date_str)
        insider_dollars    = get_insider_buy_dollars(ticker, lookback_days=30)
        insider_ceo_cfo    = get_insider_is_ceo_cfo(ticker, lookback_days=30)
        sector_vs_spy      = _get_sector_momentum_vs_spy(ticker, str(today))

        # R&D bridge features (live, from ml_rd_bridge_agent.py output)
        rd_feats = _get_rd_features(ticker)

        rows.append({
            "ticker":                   ticker,
            # Filing / timing
            "days_since_filing":        days_since,
            "filing_delay_days":        delay_map.get(inst, 45.0),
            "quarter_of_year":          (today.month - 1) // 3 + 1,
            "amendment_flag":           hf_row.get("amendment_flag", 0),
            # Conviction
            "conviction_score":         sig.get("conviction_score", sig.get("score", 0.0)),
            "n_institutions":           sig.get("n_institutions", 1),
            "herding_flag":             hf_row.get("herding_flag", 0),
            "quant_consensus":          hf_row.get("quant_consensus", 0),
            "filing_quarter_consensus": hf_row.get("filing_quarter_consensus", 0.0),
            # Position-change
            "pct_change_shares":        hf_row.get("pct_change_shares", 0.0),
            "new_position_flag":        hf_row.get("new_position_flag", 1),
            "large_increase_flag":      hf_row.get("large_increase_flag", 0),
            "position_size_pct_aum":    hf_row.get("position_size_pct_aum", 0.0),
            # Historical pattern
            "hold_streak_quarters":     hf_row.get("hold_streak_quarters", 1),
            "avg_conviction_3q":        avg_c,
            "institution_hit_rate":     hit_map.get(inst, 0.5),
            "institution_regime_hit_rate": inst_regime_map.get(inst, hit_map.get(inst, 0.5)),
            # Price / market
            "price_momentum_20d":       sig.get("price_momentum_20d", 0.0),
            "price_momentum_5d":        sig.get("price_momentum_5d", 0.0),
            "price_vs_52w_high":        sig.get("price_vs_52w_high", 0.0),
            "volatility_20d":           sig.get("volatility_20d", 0.2),
            "days_since_ipo":           days_ipo or 3650,
            "short_interest_pct":       short_int,
            # Portfolio
            "portfolio_weight":         sig.get("portfolio_weight", 0.0),
            # Earnings surprise proxy
            "earnings_surprise_flag":    earnings_surprise,
            # Form 4 enrichment (replaces binary insider_buy_flag)
            "insider_buy_dollars":       insider_dollars,
            "insider_is_ceo_cfo":        insider_ceo_cfo,
            # Sector rotation signal (ticker's sector ETF return minus SPY return, live)
            "sector_momentum_vs_spy":    sector_vs_spy,
            # Commodity momentum (market-wide — same for all tickers)
            **commodity_mom,
            # R&D Bridge
            **rd_feats,
        })

    if not rows:
        return pd.DataFrame(columns=["ticker"] + FEATURE_COLS)

    df = pd.DataFrame(rows)
    for col in FEATURE_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    X, y, meta = build_feature_matrix()
    if len(X):
        print(f"Feature matrix: {X.shape}, positive rate: {y.mean():.1%}")
        print(f"Features ({len(FEATURE_COLS)}): {FEATURE_COLS}")
    else:
        print("No labeled data yet. Run historical_label_builder.py first.")

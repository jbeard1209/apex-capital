#!/usr/bin/env python3
"""
APEX CAPITAL — MACRO FEATURES
================================
Fetches macroeconomic context features for any historical date.

Why this matters
----------------
Institutional investors don't operate in a vacuum.  A hedge fund buying NVDA
when VIX is 35 and the yield curve is inverted is making a very different bet
than the same buy when VIX is 12 and the curve is steep.  The macro regime at
filing time is a real predictor of whether that pick will succeed in the next
20 days.  Research shows macro features lift AUC by 2–4% on institutional
signal models (Koijen et al., 2021; Jha et al., 2024).

Features produced (all measured AT the filing date)
----------------------------------------------------
  vix_level            — VIX index closing value
  vix_regime           — 0=low(<15), 1=normal(15–25), 2=elevated(25–35), 3=crisis(>35)
  yield_spread_10y3m   — 10-year minus 3-month Treasury yield (negative = inverted)
  yield_inverted       — 1 if yield curve inverted (10Y-3M < 0)
  spy_momentum_60d     — SPY % return over prior 60 trading days
  spy_momentum_20d     — SPY % return over prior 20 trading days (short-term regime)
  market_regime        — 0=bear(<-10% 60d), 1=neutral, 2=bull(>10% 60d)
  qqq_spy_ratio_20d    — QQQ / SPY 20d momentum ratio (growth vs value)
  xlu_spy_ratio_20d    — XLU / SPY 20d momentum ratio (defensive rotation signal)

Cache
-----
  All underlying price data cached at data/macro_cache.parquet.
  First call downloads ~5 years, subsequent calls are instant.
"""

import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR    = Path(__file__).parent.parent / "data"
CACHE_PATH  = DATA_DIR / "macro_cache.parquet"

# Tickers to pull from yfinance
_MACRO_TICKERS = ["^VIX", "^TNX", "^IRX", "SPY", "QQQ", "XLU"]

# How many years of history to pre-download
_HISTORY_YEARS = 6

MACRO_FEATURE_COLS = [
    "vix_level",
    "vix_regime",
    "yield_spread_10y3m",
    "yield_inverted",
    "spy_momentum_60d",
    "spy_momentum_20d",
    "market_regime",
    "qqq_spy_ratio_20d",
    "xlu_spy_ratio_20d",
]

# ── Defaults used when data is unavailable ───────────────────────────────────
_DEFAULTS = {
    "vix_level":          20.0,   # long-run average
    "vix_regime":          1,     # normal
    "yield_spread_10y3m":  1.5,   # historical average (positive/steep)
    "yield_inverted":      0,
    "spy_momentum_60d":    0.04,  # gentle uptrend
    "spy_momentum_20d":    0.01,
    "market_regime":       1,     # neutral
    "qqq_spy_ratio_20d":   0.0,
    "xlu_spy_ratio_20d":   0.0,
}


# =============================================================================
# CACHE MANAGEMENT
# =============================================================================

_df_cache: Optional[pd.DataFrame] = None   # in-memory after first load


def _load_or_download() -> pd.DataFrame:
    global _df_cache
    if _df_cache is not None:
        return _df_cache

    if CACHE_PATH.exists():
        try:
            df = pd.read_parquet(CACHE_PATH)
            _df_cache = df
            logger.info(f"  macro_features: loaded cache ({len(df)} rows, "
                        f"{df.index.min().date()} → {df.index.max().date()})")
            return df
        except Exception as e:
            logger.warning(f"  macro_features: cache read failed ({e}), re-downloading")

    return _download_and_cache()


def _download_and_cache() -> pd.DataFrame:
    global _df_cache
    import yfinance as yf
    import logging as _lg
    _lg.getLogger("yfinance").setLevel(_lg.CRITICAL)

    start = (date.today() - timedelta(days=365 * _HISTORY_YEARS)).isoformat()
    logger.info(f"  macro_features: downloading macro data from {start}…")

    frames = {}
    for ticker in _MACRO_TICKERS:
        try:
            raw = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if raw.empty:
                continue
            col = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
            frames[ticker] = col.squeeze()
        except Exception as e:
            logger.debug(f"  macro_features: {ticker} download failed: {e}")

    if not frames:
        logger.warning("  macro_features: no data downloaded — using defaults")
        _df_cache = pd.DataFrame()
        return _df_cache

    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)
    df.ffill(inplace=True)    # fill weekends / holidays

    DATA_DIR.mkdir(exist_ok=True)
    df.to_parquet(CACHE_PATH)
    _df_cache = df
    logger.info(f"  macro_features: cached {len(df)} rows → {CACHE_PATH}")
    return df


def refresh_cache():
    """Re-download and overwrite cache.  Call once a day for live inference."""
    global _df_cache
    _df_cache = None
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
    _download_and_cache()


# =============================================================================
# FEATURE COMPUTATION
# =============================================================================

def get_macro_at_date(filing_date) -> dict:
    """
    Return macro feature dict for a single filing_date.
    filing_date: datetime, date, or ISO string.
    Returns _DEFAULTS if data is unavailable.
    """
    if isinstance(filing_date, str):
        filing_date = pd.Timestamp(filing_date)
    elif isinstance(filing_date, date):
        filing_date = pd.Timestamp(filing_date)

    df = _load_or_download()
    if df.empty:
        return dict(_DEFAULTS)

    # Find closest available date ≤ filing_date
    avail = df.index[df.index <= filing_date]
    if len(avail) == 0:
        return dict(_DEFAULTS)
    t = avail[-1]

    def _get(col, default=np.nan):
        if col not in df.columns:
            return default
        v = df.loc[t, col]
        return float(v) if pd.notna(v) else default

    def _pct_change(col, days, default=0.0):
        if col not in df.columns:
            return default
        sub = df[col].loc[:t]
        if len(sub) < days + 1:
            return default
        past = sub.iloc[-(days + 1)]
        curr = sub.iloc[-1]
        if past == 0 or pd.isna(past) or pd.isna(curr):
            return default
        return float((curr - past) / past)

    # ── VIX ──────────────────────────────────────────────────────────────────
    vix = _get("^VIX", _DEFAULTS["vix_level"])
    if vix < 15:
        vix_regime = 0
    elif vix < 25:
        vix_regime = 1
    elif vix < 35:
        vix_regime = 2
    else:
        vix_regime = 3

    # ── Yield curve (10Y − 3M) ────────────────────────────────────────────────
    # ^TNX = 10-year yield (in %), ^IRX = 13-week yield (in %)
    tnx = _get("^TNX", np.nan)
    irx = _get("^IRX", np.nan)
    if pd.notna(tnx) and pd.notna(irx):
        spread = float(tnx - irx)
    else:
        spread = _DEFAULTS["yield_spread_10y3m"]
    inverted = 1 if spread < 0 else 0

    # ── SPY momentum ──────────────────────────────────────────────────────────
    spy_60  = _pct_change("SPY", 60,  _DEFAULTS["spy_momentum_60d"])
    spy_20  = _pct_change("SPY", 20,  _DEFAULTS["spy_momentum_20d"])

    if spy_60 < -0.10:
        market_regime = 0   # bear
    elif spy_60 > 0.10:
        market_regime = 2   # bull
    else:
        market_regime = 1   # neutral

    # ── Sector rotation ───────────────────────────────────────────────────────
    qqq_20  = _pct_change("QQQ", 20, 0.0)
    xlu_20  = _pct_change("XLU", 20, 0.0)
    spy_20_ = spy_20 if spy_20 != 0 else 0.001
    qqq_spy = round(qqq_20 - spy_20_, 4)   # growth outperformance
    xlu_spy = round(xlu_20 - spy_20_, 4)   # defensive outperformance

    return {
        "vix_level":           round(vix,     2),
        "vix_regime":          vix_regime,
        "yield_spread_10y3m":  round(spread,  3),
        "yield_inverted":      inverted,
        "spy_momentum_60d":    round(spy_60,  4),
        "spy_momentum_20d":    round(spy_20,  4),
        "market_regime":       market_regime,
        "qqq_spy_ratio_20d":   qqq_spy,
        "xlu_spy_ratio_20d":   xlu_spy,
    }


def add_macro_features(df: pd.DataFrame, date_col: str = "snapshot_date") -> pd.DataFrame:
    """
    Vectorised version: adds all MACRO_FEATURE_COLS to a DataFrame that
    has a date column.  Batches lookups by unique date for speed.
    """
    df = df.copy()
    unique_dates = df[date_col].unique()
    macro_rows = {}
    for d in unique_dates:
        macro_rows[d] = get_macro_at_date(d)

    for col in MACRO_FEATURE_COLS:
        df[col] = df[date_col].map(lambda d, c=col: macro_rows.get(d, _DEFAULTS).get(c, _DEFAULTS[c]))

    return df


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    test_dates = ["2022-01-15", "2022-10-01", "2023-03-15", "2024-06-01", "2025-01-10"]
    for d in test_dates:
        m = get_macro_at_date(d)
        print(f"\n{d}:")
        for k, v in m.items():
            print(f"  {k:30s} {v}")

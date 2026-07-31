#!/usr/bin/env python3
"""
APEX CAPITAL — FEATURE ENGINEER
=================================
Builds the feature matrix used for ML training and inference.

Features (22 total)
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
from predictive_model.form4_collector import get_insider_buy_flag, _ensure_form4_table

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
    "conviction_velocity",
    "institution_hit_rate",
    # Price / market
    "price_momentum_20d",
    "price_momentum_5d",   # short-term 5-day momentum (mlsugg-2026-003)
    "price_vs_52w_high",
    "volatility_20d",
    "days_since_ipo",
    "short_interest_pct",
    # Portfolio
    "portfolio_weight",
    # Insider
    "insider_buy_flag",
    # R&D Bridge (ml_rd_bridge_agent.py writes data/rd_ml_bridge_signals.json)
    "rd_signals_available",
    "rd_ticker_rsi",
    "rd_ticker_pct_from_50ma",
    "rd_ticker_trend_bullish",
    "rd_ticker_conviction",
    "rd_ticker_action_buy",
    "rd_sector_rsi",
    "rd_sector_bullish",
    "rd_sector_ret_1m",
    "rd_regime_bull",
    "macro_regime_bull",
    "macro_regime_late_cycle",
    "macro_regime_contraction",
    "macro_regime_recovery",
    "crypto_fear_greed",
    "crypto_risk_off_flag",
    "rd_vix",
    "rd_spy_rsi",
    "rd_warning_count",
    # Quiver Quantitative (#32)
    "quiver_congress_net_buys",
    "quiver_congress_buy_flag",
    "quiver_govcontract_flag",
]

# Top-8 SHAP features for LT (365-day) model.
# Promoted 2026-05-25: walk-forward AUC 0.5343 vs threshold 0.53 (baseline 0.5214).
LT_FEATURE_COLS = [
    "price_vs_52w_high", "volatility_20d", "avg_conviction_3q",
    "conviction_velocity",                  # QoQ change in institutional conviction
    "price_momentum_20d", "rd_vix", "rd_spy_rsi",
    "price_momentum_5d", "institution_hit_rate",
]

#: The long-term label column. Import this instead of hardcoding the string.
#
# "outcome_365d" was the original calendar-day design. build_365d_labels.py was
# written to ALTER TABLE it into existence and never ran once (no
# logs/build_365d_labels.log, in no crontab, called by nothing) -- so the column
# has never existed. Nine modules queried it anyway for months. Four of them
# guard with `if "outcome_365d" not in cols: skip`, so they ran clean and
# reported nothing, forever; the rest raised. The live label is outcome_252d
# (252 TRADING days ~= 1 calendar year), built by
# predictive_model/historical_label_builder_fast.py -- 17,652 labeled rows.
# Guarded by test_pipeline_smoke.py::test_every_label_column_referenced_exists.
LT_TARGET = "outcome_252d"

# Targets that select LT_FEATURE_COLS above. Anything training on a long
# horizon must be listed, or it silently gets the short-term feature set.
LT_TARGETS = frozenset({LT_TARGET, "risk_adjusted_252d"})


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



_MACRO_REGIME_PATH = Path(__file__).parent.parent / "data" / "macro_regime_state.json"
_macro_regime_cache: dict = {}
_macro_regime_loaded: bool = False

def _load_macro_regime() -> None:
    global _macro_regime_cache, _macro_regime_loaded
    if _macro_regime_loaded:
        return
    _macro_regime_loaded = True
    try:
        with open(_MACRO_REGIME_PATH) as f:
            _macro_regime_cache = json.load(f)
    except Exception:
        _macro_regime_cache = {}



_CRYPTO_PATH = Path(__file__).parent.parent / "data" / "crypto_signals.json"
_crypto_cache: dict = {}
_crypto_loaded: bool = False

_QUIVER_PATH = Path(__file__).parent.parent / "data" / "quiver_signals.json"
_quiver_cache: dict = {}
_quiver_loaded: bool = False

def _load_crypto() -> None:
    global _crypto_cache, _crypto_loaded
    if _crypto_loaded: return
    _crypto_loaded = True
    try:
        with open(_CRYPTO_PATH) as f: _crypto_cache = json.load(f)
    except Exception: _crypto_cache = {}

def _load_quiver() -> None:
    global _quiver_cache, _quiver_loaded
    if _quiver_loaded: return
    _quiver_loaded = True
    try:
        with open(_QUIVER_PATH) as f: _quiver_cache = json.load(f)
    except Exception: _quiver_cache = {}

def _get_rd_features(ticker: str) -> dict:
    """
    Return R&D bridge features for a ticker.
    Returns a dict with all rd_ feature keys.
    When the ticker is not in the bridge (historical data, unknown ticker),
    rd_signals_available=0 and all other rd_ features are set to neutral defaults.
    Tree models will learn the (0,neutral) encoding means "no R&D signal."
    """
    _load_rd_bridge()
    _load_macro_regime()
    _load_crypto()
    _load_quiver()

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
        # Prefer macro_regime_state.json (#18) if available; fall back to rd bridge
        "rd_regime_bull":           int(regime.get("rd_regime_bull", 0)),
        "macro_regime_bull":        int(_macro_regime_cache.get("is_bull", 0)),
        "macro_regime_late_cycle": int(_macro_regime_cache.get("is_late_cycle", 0)),
        "macro_regime_contraction": int(_macro_regime_cache.get("is_contraction", 0)),
        "macro_regime_recovery":    int(_macro_regime_cache.get("is_recovery", 0)),
        # Crypto macro signals (#30 Phase 1)
        "crypto_fear_greed":        float(_crypto_cache.get("crypto_fg_normalized", 0.5)),
        "crypto_risk_off_flag":      int(_crypto_cache.get("crypto_risk_off_flag", 0)),
        "rd_vix":                   float(regime.get("rd_vix", 0)),
        "rd_spy_rsi":               float(regime.get("rd_spy_rsi", 0)),
        "rd_warning_count":         int(regime.get("rd_warning_count", 0)),
        # Quiver Quantitative signals (#32)
        "quiver_congress_net_buys":  float(_quiver_cache.get("congress_net_buys_90d", 0.0)),
        "quiver_congress_buy_flag":  int(_quiver_cache.get("congress_buy_flag", 0)),
        "quiver_govcontract_flag":   int(_quiver_cache.get("govcontract_flag", 0)),
    }


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

    records = []
    for (ticker, filing_date), grp in df.groupby(["ticker", "filing_date"]):
        n_inst      = len(grp["institution"].unique())
        herding     = 1 if n_inst >= 3 else 0
        quant_count = len(set(grp["institution"]) & _QUANT_FUNDS)
        quant_cons  = 1 if quant_count >= 2 else 0

        # Filing quarter consensus — fraction of all tracked funds that hold it this quarter
        all_funds_this_quarter = df[
            (df["filing_date"].between(filing_date - pd.Timedelta(days=95), filing_date + pd.Timedelta(days=5)))
        ]["institution"].nunique()
        q_consensus = n_inst / max(all_funds_this_quarter, 1)

        # Amendment flag (any fund filed amendment for this ticker)
        amend = 1 if (grp["form"] == "13F-HR/A").any() else 0

        # Position size as % of AUM (largest single-fund position)
        total_aum = grp["fund_total_value"].max() or 1
        pos_pct   = float(grp["value_usd"].max()) / max(total_aum, 1)

        # Prior quarter comparison
        prior = df[
            (df["ticker"] == ticker) &
            (df["filing_date"] < filing_date)
        ].sort_values("filing_date")

        pct_change   = 0.0
        new_flag     = 1
        large_flag   = 0

        if not prior.empty:
            last_filing   = prior["filing_date"].max()
            prior_shares  = prior[prior["filing_date"] == last_filing]["shares"].sum()
            curr_shares   = grp["shares"].sum()
            if prior_shares > 0:
                pct_change = (curr_shares - prior_shares) / prior_shares
                new_flag   = 0
                large_flag = 1 if pct_change > 0.5 else 0

        # Hold streak
        all_dates = sorted(df[df["ticker"] == ticker]["filing_date"].unique())
        streak = 0
        for d in reversed(all_dates):
            if d <= filing_date:
                streak += 1
            else:
                break

        records.append({
            "ticker":                  ticker,
            "filing_date":             filing_date,
            "n_institutions":          n_inst,
            "herding_flag":            herding,
            "quant_consensus":         quant_cons,
            "filing_quarter_consensus": round(q_consensus, 4),
            "amendment_flag":          amend,
            "position_size_pct_aum":   round(pos_pct, 6),
            "pct_change_shares":       pct_change,
            "new_position_flag":       new_flag,
            "large_increase_flag":     large_flag,
            "hold_streak_quarters":    streak,
        })

    return pd.DataFrame(records)


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


def _compute_rolling_avg_conviction(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT ticker, snapshot_date, conviction_score FROM pipeline_snapshots
           WHERE conviction_score IS NOT NULL ORDER BY ticker, snapshot_date"""
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["ticker", "snapshot_date", "avg_conviction_3q"])
    df = pd.DataFrame(rows, columns=["ticker", "snapshot_date", "conviction_score"])
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df["avg_conviction_3q"] = (
        df.groupby("ticker")["conviction_score"]
          .transform(lambda x: x.rolling(3, min_periods=1).mean())
    )
    return df[["ticker", "snapshot_date", "avg_conviction_3q"]]


def _compute_conviction_velocity(conn, max_rows: int = 100_000) -> "pd.DataFrame":
    """
    Compute conviction velocity: QoQ change in avg_conviction_3q.
    conviction_velocity > 0  = rising institutional conviction
    conviction_velocity < 0  = falling conviction (warning sign)
    conviction_velocity = 0  = static (most common)
    max_rows: limit to most recent rows to control memory usage on 4GB VM.
    """
    try:
        import pandas as pd
        rows = conn.execute(
            f"""SELECT ticker, snapshot_date, conviction_score FROM pipeline_snapshots
               WHERE conviction_score IS NOT NULL ORDER BY ticker, snapshot_date
               LIMIT {max_rows}"""
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["ticker", "snapshot_date", "conviction_velocity"])
        df = pd.DataFrame(rows, columns=["ticker", "snapshot_date", "conviction_score"])
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
        df["avg_3q"] = (
            df.groupby("ticker")["conviction_score"]
              .transform(lambda x: x.rolling(3, min_periods=1).mean())
        )
        # Velocity = current avg - prior quarter avg (shift by 1 within ticker)
        df["conviction_velocity"] = df.groupby("ticker")["avg_3q"].diff(1).fillna(0)
        return df[["ticker", "snapshot_date", "conviction_velocity"]]
    except Exception:
        import pandas as pd
        return pd.DataFrame(columns=["ticker", "snapshot_date", "conviction_velocity"])



# =============================================================================
# MAIN FEATURE MATRIX (for training)
# =============================================================================

_MATRIX_CACHE: dict = {}

def build_feature_matrix(target: str = "outcome_20d", use_cache: bool = True) -> tuple:
    """
    Build X, y, meta from pipeline_snapshots. Caches result to disk for speed.
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
              "filing_quarter_consensus", "insider_buy_flag"]:
        if c in available_cols:
            extra_cols.append(c)

    extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""

    # risk_adjusted_20d_binary is derived (not a DB column) — fetch risk_adjusted_20d instead
    _sql_target = "risk_adjusted_20d" if target == "risk_adjusted_20d_binary" else target
    snaps = pd.read_sql(
        f"""SELECT id, ticker, snapshot_date, institution, conviction_score,
                   n_institutions, herding_flag, quant_consensus,
                   days_since_filing, portfolio_weight {extra_sql},
                   {_sql_target} as label
            FROM pipeline_snapshots
            WHERE {_sql_target} IS NOT NULL AND conviction_score IS NOT NULL""",
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
        merged = []
        for _, row in snaps.sort_values("snapshot_date").iterrows():
            hf = holdings_feats[
                (holdings_feats["ticker"] == row["ticker"]) &
                (holdings_feats["filing_date"] <= row["snapshot_date"])
            ].sort_values("filing_date")
            if not hf.empty:
                latest = hf.iloc[-1].to_dict()
                latest.update(row.to_dict())
                merged.append(latest)
            else:
                r = row.to_dict()
                r.update({
                    "pct_change_shares": 0.0, "new_position_flag": 1,
                    "large_increase_flag": 0, "hold_streak_quarters": 1,
                    "amendment_flag": 0, "position_size_pct_aum": 0.0,
                    "filing_quarter_consensus": 0.0,
                })
                merged.append(r)
        snaps = pd.DataFrame(merged)
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

    # Conviction velocity (QoQ change in rolling average conviction)
    conv_vel = _compute_conviction_velocity(conn)
    if not conv_vel.empty:
        conv_vel["snapshot_date"] = __import__("pandas").to_datetime(conv_vel["snapshot_date"])
        snaps = snaps.merge(conv_vel, on=["ticker", "snapshot_date"], how="left")
    if "conviction_velocity" not in snaps.columns:
        snaps["conviction_velocity"] = 0.0
    snaps["conviction_velocity"] = snaps["conviction_velocity"].fillna(0.0)

    # Institution hit rates
    hit_rates = _compute_institution_hit_rates(conn)
    if not hit_rates.empty:
        snaps = snaps.merge(hit_rates, on="institution", how="left")
    if "institution_hit_rate" not in snaps.columns:
        snaps["institution_hit_rate"] = 0.5

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
    # ── Cache check ─────────────────────────────────────────────────────────
    import hashlib, pickle as _pkl
    from pathlib import Path as _Path
    _fe_dir   = _Path(__file__).parent
    _data_dir = _fe_dir / "data"
    _db_path  = _data_dir / "historical_13f.db"
    _cache_dir = _data_dir / "matrix_cache"
    _cache_dir.mkdir(exist_ok=True)
    _db_mtime = int(_db_path.stat().st_mtime) if _db_path.exists() else 0
    # Resolve the feature set BEFORE the cache lookup, and key the cache on it.
    #
    # 2026-07-31: the key was md5(target + db_mtime) only. When the LT feature
    # selection below was fixed, every cached matrix still had the OLD column
    # set — same target, same db_mtime, so a stale 47-column matrix would be
    # handed back to code now asking for 9 columns, with no error anywhere. A
    # cache whose key omits an input it depends on doesn't just go stale, it
    # silently defeats the fix that invalidated it.
    _feat_cols = LT_FEATURE_COLS if target in LT_TARGETS else FEATURE_COLS
    _cache_key = hashlib.md5(
        f"{target}_{_db_mtime}_{','.join(_feat_cols)}".encode()).hexdigest()[:12]
    _cache_file = _cache_dir / f"matrix_{target}_{_cache_key}.pkl"
    if use_cache and _cache_file.exists():
        try:
            with open(_cache_file, "rb") as _cf:
                _cached = _pkl.load(_cf)
            logger.info(f"  feature_engineer: loaded from cache ({_cache_file.name})")
            return _cached["X"], _cached["y"], _cached["meta"]
        except Exception as _ce:
            logger.warning(f"  Cache load failed: {_ce} — rebuilding")
    # ─────────────────────────────────────────────────────────────────────────
    if target == "risk_adjusted_20d_binary":
        # Binarize Sharpe target at 60th percentile (top-40% by risk-adj return)
        ra = label_raw.astype(float)
        thresh = float(np.nanpercentile(ra, 60))
        y = (ra > thresh).astype(int)
        logger.info(f"  risk_adjusted_20d_binary: binarized at p60 = {thresh:.3f} (pos_rate={y.mean():.1%})")
    elif target.startswith("risk_adjusted"):
        y = label_raw.astype(float)
    else:
        y = (label_raw > 0).astype(int)

    # _feat_cols was resolved above the cache check (it is part of the cache
    # key). It used to be decided here by `target == "outcome_365d"` — a column
    # that was never built. The long-term label shipped as outcome_252d (252
    # trading days), so that test was always False and the LT model silently
    # trained on all 47 short-term features instead of its curated 9, on only
    # ~17.6k labeled rows. No error, no warning: the `else` branch just handed
    # back the wrong matrix.
    X    = snaps[_feat_cols].values.astype(float)
    meta = snaps[["ticker", "snapshot_date", "conviction_score"]].reset_index(drop=True)

    pos_rate = float((y > 0).mean()) if len(y) else 0.0
    logger.info(f"  feature_engineer: {len(X)} samples, {len(_feat_cols)} features, "
                f"{pos_rate:.1%} positive, target={target}")
    # ── Save to cache ────────────────────────────────────────────────────────
    if use_cache:
        try:
            import pickle as _pkl  # noqa: F811
            with open(_cache_file, "wb") as _cf:
                _pkl.dump({"X": X, "y": y, "meta": meta}, _cf, protocol=4)
            logger.info(f"  feature_engineer: cached to {_cache_file.name}")
        except Exception as _ce:
            logger.warning(f"  Cache save failed: {_ce}")
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
        short_int     = _get_short_interest(ticker)
        days_ipo      = _get_days_since_ipo(ticker)
        insider_buy   = get_insider_buy_flag(ticker, lookback_days=30)

        # R&D bridge features (live, from ml_rd_bridge_agent.py output)
        rd_feats = _get_rd_features(ticker)

        rows.append({
            "ticker":                   ticker,
            # Filing / timing
            "days_since_filing":        days_since,
            "filing_delay_days":        delay_map.get(inst, 45.0),
            "quarter_of_year":          today.month // 4 + 1,
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
            # Price / market
            "price_momentum_20d":       sig.get("price_momentum_20d", 0.0),
            "price_momentum_5d":        sig.get("price_momentum_5d", 0.0),
            "price_vs_52w_high":        sig.get("price_vs_52w_high", 0.0),
            "volatility_20d":           sig.get("volatility_20d", 0.2),
            "days_since_ipo":           days_ipo or 3650,
            "short_interest_pct":       short_int,
            # Portfolio
            "portfolio_weight":         sig.get("portfolio_weight", 0.0),
            # Insider
            "insider_buy_flag":         insider_buy,
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

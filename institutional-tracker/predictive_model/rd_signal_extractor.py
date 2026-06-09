#!/usr/bin/env python3
"""
APEX CAPITAL — R&D SIGNAL EXTRACTOR
=====================================
Converts the R&D team's structured JSON reports into precise ML features.

Why this is better than the old keyword-scan approach
------------------------------------------------------
The old approach scanned rd_*.log files for words like "recession" and
"stimulus" — imprecise and language-dependent.  This module reads the actual
structured data that the R&D agents already produce every hour:

  data/sector_assignments.json   — per-sector momentum scores (0–12), RSI,
                                   1M/3M returns, trend (BULL/BEAR/NEUTRAL)
  data/research_team_report.json — 71+ recommendations with conviction (1–5★),
                                   momentum, action, source agent
  data/international_report.json — global sentiment score, risk_on/off,
                                   watch tickers, US implications

Features produced
-----------------
  rd_macro_regime       — −1 bearish · 0 neutral · 1 bullish  (sector breadth)
  rd_sector_breadth     — fraction of sectors with score ≥ 6/12 (0–1)
  rd_cro_risk_level     — 0=LOW · 1=MEDIUM · 2=HIGH · 3=CRITICAL (risk gauge)
  rd_intl_sentiment     — −1 to +1 from IMI desk (-1=RISK_OFF, +1=RISK_ON)
  rd_intl_score_raw     — raw integer from international_report
  rd_n_high_conviction  — count of 4–5★ R&D picks  (market opportunity signal)
  rd_avg_conviction     — mean conviction across all picks (0–1 normalised)
  rd_ticker_conviction  — conviction for a specific ticker, 0 if not in report
  rd_ticker_in_report   — 1 if this ticker appears in latest R&D recommendations
  rd_ticker_sector_score— ETF momentum score (0–12) for this ticker's sector

Usage
-----
  # For live inference (adds current R&D context to a signals DataFrame)
  from predictive_model.rd_signal_extractor import get_rd_signals, add_rd_features

  live_features = get_rd_signals()              # dict of all market-level features
  df = add_rd_features(df, ticker_col="ticker") # adds rd_* columns to DataFrame

  # For training — returns only non-ticker-specific features (market context)
  signals = get_rd_signals()
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"

# Feature names exposed to ML models
RD_MARKET_COLS = [
    "rd_macro_regime",
    "rd_sector_breadth",
    "rd_cro_risk_level",
    "rd_intl_sentiment",
    "rd_intl_score_raw",
    "rd_n_high_conviction",
    "rd_avg_conviction",
]

RD_TICKER_COLS = [
    "rd_ticker_conviction",
    "rd_ticker_in_report",
    "rd_ticker_sector_score",
]

RD_FEATURE_COLS = RD_MARKET_COLS + RD_TICKER_COLS

# Defaults (used when reports are missing or stale)
_DEFAULTS = {
    "rd_macro_regime":        0,
    "rd_sector_breadth":      0.5,
    "rd_cro_risk_level":      1,
    "rd_intl_sentiment":      0.0,
    "rd_intl_score_raw":      0,
    "rd_n_high_conviction":   0,
    "rd_avg_conviction":      0.5,
    "rd_ticker_conviction":   0.0,
    "rd_ticker_in_report":    0,
    "rd_ticker_sector_score": 6.0,
}

# Reports are considered stale after this many hours
_STALE_HOURS = 4


def _load_json(path: Path) -> Optional[dict]:
    """Load JSON; return None if missing, empty, or too old."""
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime > timedelta(hours=_STALE_HOURS):
            logger.debug(f"  rd_signal: {path.name} is stale ({datetime.now()-mtime})")
            return None
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"  rd_signal: could not load {path.name}: {e}")
        return None


def get_rd_signals_at_date(target_date) -> dict:
    """
    Look up R&D features from the historical snapshot log for a specific date.
    Used for historical training — returns the R&D state closest to target_date.
    Falls back to _DEFAULTS if no history available.
    """
    history_path = DATA_DIR / "rd_ml_history.json"
    if not history_path.exists():
        return dict(_DEFAULTS)
    try:
        import pandas as pd
        with open(history_path) as f:
            history = json.load(f)
        if not history:
            return dict(_DEFAULTS)
        target_ts = pd.Timestamp(target_date)
        # Find closest snapshot by date
        best = min(history, key=lambda r: abs(
            (pd.Timestamp(r["date"]) - target_ts).days
        ))
        # Extract market-level features
        return {k: best.get(k, _DEFAULTS[k]) for k in RD_MARKET_COLS}
    except Exception as e:
        logger.debug(f"  rd_signal: history lookup failed: {e}")
        return dict(_DEFAULTS)


def get_rd_signals(ticker: Optional[str] = None) -> dict:
    """
    Extract all R&D signals as a flat dict of ML-ready features.

    Args:
        ticker: optional — if provided, adds ticker-specific features
                (rd_ticker_conviction, rd_ticker_in_report, etc.)
    """
    out = dict(_DEFAULTS)

    # ── 1. Sector assignments (macro regime + breadth) ────────────────────────
    asgn = _load_json(DATA_DIR / "sector_assignments.json")
    if asgn:
        scored = asgn.get("all_scored", [])
        if scored:
            scores = [s.get("score_data", {}).get("score", 6) for s in scored]
            n_strong = sum(1 for s in scores if s >= 6)
            breadth  = n_strong / max(len(scores), 1)
            avg_score = sum(scores) / max(len(scores), 1)

            out["rd_sector_breadth"] = round(breadth, 3)
            # Macro regime: bull if >60% sectors strong, bear if <30%
            if breadth > 0.6:
                out["rd_macro_regime"] = 1
            elif breadth < 0.3:
                out["rd_macro_regime"] = -1
            else:
                out["rd_macro_regime"] = 0

            # Ticker's sector score
            if ticker:
                # Find which sector this ticker belongs to (via watchlists)
                for sec in scored:
                    watchlist = sec.get("watchlist", [])
                    if ticker in watchlist:
                        out["rd_ticker_sector_score"] = float(
                            sec.get("score_data", {}).get("score", 6)
                        )
                        break

        # CRO risk level
        risk_str = asgn.get("cro_risk_level", "MEDIUM").upper()
        risk_map = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "CRISIS": 3}
        out["rd_cro_risk_level"] = risk_map.get(risk_str, 1)

    # ── 2. Research recommendations (conviction signals) ──────────────────────
    report = _load_json(DATA_DIR / "research_team_report.json")
    if report:
        recs = report.get("recommendations", [])
        if recs:
            convictions = [r.get("conviction", 3) for r in recs]
            high_conv   = [c for c in convictions if c >= 4]
            avg_conv    = sum(convictions) / max(len(convictions), 1) / 5.0  # normalise 0-1

            out["rd_n_high_conviction"] = len(high_conv)
            out["rd_avg_conviction"]    = round(avg_conv, 4)

            if ticker:
                # Find this specific ticker in recommendations
                ticker_recs = [r for r in recs if r.get("ticker") == ticker]
                if ticker_recs:
                    best = max(ticker_recs, key=lambda r: r.get("conviction", 0))
                    out["rd_ticker_conviction"] = round(
                        float(best.get("conviction", 0)) / 5.0, 4
                    )
                    out["rd_ticker_in_report"]  = 1

    # ── 3. International sentiment ────────────────────────────────────────────
    intl = _load_json(DATA_DIR / "international_report.json")
    if intl:
        sentiment = intl.get("sentiment", "NEUTRAL").upper()
        score_raw = intl.get("sentiment_score", 0)
        sent_map  = {"RISK_ON": 1.0, "NEUTRAL": 0.0, "RISK_OFF": -1.0}
        out["rd_intl_sentiment"]  = sent_map.get(sentiment, 0.0)
        out["rd_intl_score_raw"]  = int(score_raw)

    return out


def add_rd_features(df, ticker_col: str = "ticker"):
    """
    Adds all RD_FEATURE_COLS to a DataFrame using current R&D signals.
    Market-level features are the same for all rows (current conditions).
    Ticker-specific features vary by ticker.
    """
    import pandas as pd

    df = df.copy()
    # Market-level: compute once
    market_signals = get_rd_signals()

    for col in RD_MARKET_COLS:
        df[col] = market_signals.get(col, _DEFAULTS[col])

    # Ticker-level: compute per ticker
    if ticker_col in df.columns:
        unique_tickers = df[ticker_col].unique()
        ticker_signals = {}
        for t in unique_tickers:
            sig = get_rd_signals(ticker=t)
            ticker_signals[t] = {c: sig[c] for c in RD_TICKER_COLS}

        for col in RD_TICKER_COLS:
            df[col] = df[ticker_col].map(
                lambda t, c=col: ticker_signals.get(t, _DEFAULTS)[c]
            )
    else:
        for col in RD_TICKER_COLS:
            df[col] = _DEFAULTS[col]

    return df


def get_rd_regime_summary() -> str:
    """Human-readable one-liner for logging/email."""
    s = get_rd_signals()
    regime = {1: "BULLISH", 0: "NEUTRAL", -1: "BEARISH"}[s["rd_macro_regime"]]
    risk   = ["LOW", "MEDIUM", "HIGH", "CRITICAL"][s["rd_cro_risk_level"]]
    intl   = {1.0: "RISK_ON", 0.0: "NEUTRAL", -1.0: "RISK_OFF"}.get(
        s["rd_intl_sentiment"], "NEUTRAL"
    )
    return (f"R&D regime: {regime} | breadth {s['rd_sector_breadth']:.0%} | "
            f"CRO risk: {risk} | IMI: {intl} | "
            f"{s['rd_n_high_conviction']} high-conviction picks")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    import json as _j
    print(get_rd_regime_summary())
    print(_j.dumps(get_rd_signals(), indent=2))

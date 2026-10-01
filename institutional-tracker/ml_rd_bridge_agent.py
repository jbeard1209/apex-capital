#!/usr/bin/env python3
"""
APEX CAPITAL — R&D ↔ ML BRIDGE AGENT
======================================
Runs 30 min after each R&D cycle (every 2 hours on Mac).
Reads the full R&D output stack, extracts richer per-ticker features than
the 7 simplified aggregate signals currently in rd_ml_history.json, and
writes data/rd_ml_bridge_signals.json for the inference pipeline to use.

Also appends a richer snapshot to rd_ml_history.json and queues any newly
detected feature opportunities as IMPLEMENT suggestions in ml_suggestions.json.

Schedule (Mac crontab):
  30 10,12,14,16,18,20,22,0,2,4 * * * python3 ml_rd_bridge_agent.py

Key outputs
-----------
  data/rd_ml_bridge_signals.json  — per-ticker R&D features + regime context
  data/rd_ml_history.json         — appended with richer snapshot
  data/ml_suggestions.json        — new feature IDEAS (REVIEW) if found
"""

import json
import fcntl
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [RD-Bridge]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ml_rd_bridge")

BASE = Path(__file__).parent
DATA = BASE / "data"

# ── Input files ────────────────────────────────────────────────────────────
_RESEARCH_REPORT   = DATA / "research_team_report.json"
_SECTOR_ASSIGN     = DATA / "sector_assignments.json"
_HISTORICAL_CTX    = DATA / "rd_historical_context.json"
_GEO_CTX           = DATA / "rd_geopolitical_context.json"
_EARNINGS_PREDS    = DATA / "earnings_predictions.json"

# ── Output files ───────────────────────────────────────────────────────────
_BRIDGE_OUT        = DATA / "rd_ml_bridge_signals.json"
_ML_HISTORY        = DATA / "rd_ml_history.json"
_ML_SUGGESTIONS    = DATA / "ml_suggestions.json"

#: Latest-state features. Each run computes current per-ticker R&D features
#: and regime context from scratch; board_meeting reads whatever is most
#: recent. There is no history for a cadence-slow reader to miss here.
_WIRING_SNAPSHOT = {"rd_ml_bridge_signals.json"}

_LOCK_PATH         = "/tmp/ml_rd_bridge.lock"


# =============================================================================
# SAFE LOADERS
# =============================================================================

def _load(path: Path) -> Union[dict, list]:
    """Load JSON; return {} or [] on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load {path.name}: {e}")
        return {}


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


# =============================================================================
# FEATURE EXTRACTORS
# =============================================================================

def _build_ticker_features(
    research: dict,
    sector_assign: dict,
    geo: dict,
    earnings: dict,
) -> dict[str, dict]:
    """
    Returns {ticker: {feature_dict}} with rich per-ticker R&D signals.

    Features extracted:
      rd_ticker_rsi           — RSI from R&D (0-100); 50 if unknown
      rd_ticker_pct_from_50ma — % above/below 50-day MA
      rd_ticker_pct_from_200ma— % above/below 200-day MA
      rd_ticker_trend_bullish — 1 if R&D trend = "bullish"
      rd_ticker_trend_bearish — 1 if R&D trend = "bearish"
      rd_ticker_conviction    — R&D conviction (1-5)
      rd_ticker_action_buy    — 1 if R&D recommends BUY_NEW or ADD
      rd_ticker_fwd_pe        — forward P/E ratio (0 if unavailable)
      rd_ticker_rev_growth    — YoY revenue growth %
      rd_sector_rsi           — RSI of the ticker's sector ETF
      rd_sector_bullish       — 1 if sector trend = "bullish"
      rd_sector_ret_1m        — 1-month return of sector ETF (%)
      rd_sector_volatility    — 20-day volatility of sector ETF (%)
      rd_geo_risk             — 1 if ticker's sector has active geo risk flag
      rd_earnings_proximity   — days until next earnings (90 if unknown)
    """
    # ── sector map: ETF → score_data ──────────────────────────────────────
    sector_data: dict[str, dict] = {}  # sector_name → score_data
    sector_etf_map: dict[str, str] = {}  # sector_name → etf
    for s in sector_assign.get("all_scored", []):
        sname = s.get("sector_name", "")
        sd = s.get("score_data", {})
        sector_data[sname] = sd
        sector_etf_map[sname] = s.get("etf", "")

    # ── ticker → sector mapping from agent_reports ─────────────────────────
    ticker_sector: dict[str, str] = {}
    for agent_report in research.get("agent_reports", []):
        sname = agent_report.get("sector_name", "")
        for pick in agent_report.get("top_picks", []):
            t = pick.get("ticker", "")
            if t:
                ticker_sector[t] = sname

    # Also from recommendations (has source specialty)
    for rec in research.get("recommendations", []):
        t = rec.get("ticker", "")
        specialty = rec.get("specialty", "")
        if t and specialty and t not in ticker_sector:
            ticker_sector[t] = specialty

    # ── geo risk: which sectors have active flags ─────────────────────────
    geo_analysis = geo.get("geopolitical_analysis", {}) if isinstance(geo, dict) else {}
    high_geo_sectors = set(geo_analysis.get("high_sensitivity_sectors", []))
    moderate_geo_sectors = set(geo_analysis.get("moderate_sensitivity_sectors", []))
    double_flagged = set(geo.get("double_flagged_sectors", []) if isinstance(geo, dict) else [])
    geo_risk_sectors = high_geo_sectors | double_flagged  # moderate is not a block

    # ── earnings proximity: ticker → days ─────────────────────────────────
    earnings_days: dict[str, int] = {}
    for pred in (earnings.get("predictions", []) if isinstance(earnings, dict) else []):
        t = pred.get("ticker", "")
        # earnings_predictions stores expected dates or days_to_earnings
        days = pred.get("days_to_earnings") or pred.get("days_until_earnings")
        if t and days is not None:
            try:
                earnings_days[t] = int(days)
            except (TypeError, ValueError):
                pass

    # ── build per-ticker signals ───────────────────────────────────────────
    out: dict[str, dict] = {}

    for rec in research.get("recommendations", []):
        ticker = rec.get("ticker", "")
        if not ticker:
            continue

        mom = rec.get("momentum", {}) or {}
        rsi             = float(mom.get("rsi_14", 50) or 50)
        pct_50ma        = float(mom.get("pct_from_50ma", 0) or 0)
        pct_200ma       = float(mom.get("pct_from_200ma", 0) or 0)
        trend           = (mom.get("trend") or "neutral").lower()
        conviction      = float(rec.get("conviction", 0) or 0)
        action          = (rec.get("action") or "").upper()
        fwd_pe          = float((rec.get("fundamentals") or {}).get("forward_pe", 0) or 0)
        rev_growth      = float((rec.get("fundamentals") or {}).get("revenue_growth_pct", 0) or 0)

        sector_name     = ticker_sector.get(ticker, "")
        sd              = sector_data.get(sector_name, {})
        sector_rsi      = float(sd.get("rsi_14", 50) or 50)
        sector_trend    = (sd.get("trend") or "neutral").lower()
        sector_ret_1m   = float(sd.get("ret_1m_pct", 0) or 0)
        sector_vol      = float(sd.get("volatility_20d", 20) or 20)

        # Map sector ETF name → geo risk
        geo_flag = 1 if (sector_name in geo_risk_sectors or ticker in geo_risk_sectors) else 0

        earnings_prox   = earnings_days.get(ticker, 90)

        out[ticker] = {
            "sector":                    sector_name,
            "rd_ticker_rsi":             round(rsi, 2),
            "rd_ticker_pct_from_50ma":   round(pct_50ma, 4),
            "rd_ticker_pct_from_200ma":  round(pct_200ma, 4),
            "rd_ticker_trend_bullish":   1 if trend == "bullish" else 0,
            "rd_ticker_trend_bearish":   1 if trend == "bearish" else 0,
            "rd_ticker_conviction":      round(conviction, 2),
            "rd_ticker_action_buy":      1 if action in ("BUY_NEW", "ADD", "STRONG_BUY") else 0,
            "rd_ticker_fwd_pe":          round(min(fwd_pe, 200), 4),   # cap outliers
            "rd_ticker_rev_growth":      round(rev_growth, 4),
            "rd_sector_rsi":             round(sector_rsi, 2),
            "rd_sector_bullish":         1 if sector_trend == "bullish" else 0,
            "rd_sector_ret_1m":          round(sector_ret_1m, 4),
            "rd_sector_volatility":      round(sector_vol, 4),
            "rd_geo_risk":               geo_flag,
            "rd_earnings_proximity":     int(earnings_prox),
        }

    log.info(f"Built per-ticker features for {len(out)} tickers")
    return out


def _build_regime_context(historical: dict) -> dict:
    """
    Extract clean regime signals from rd_historical_context.json.

    Returns:
      rd_regime_bull          — 1 if spy_trend == "bull"
      rd_regime_bear          — 1 if spy_trend == "bear"
      rd_vix                  — current VIX level
      rd_vix_high             — 1 if VIX category = "high" (>25)
      rd_spy_rsi              — SPY RSI (0-100)
      rd_spy_overbought       — 1 if SPY RSI > 70
      rd_rising_rates         — 1 if TLT trend = "rising_rates"
      rd_momentum_late        — 1 if momentum_phase contains "late" or "overbought"
      rd_warning_count        — number of active warning signals
      rd_spy_pct_vs_200ma     — SPY % above/below 200-day MA
    """
    if not isinstance(historical, dict):
        return _default_regime()

    regime = historical.get("current_regime", {}) or {}
    warnings = historical.get("warning_signals", []) or []

    spy_trend   = (regime.get("spy_trend") or "neutral").lower()
    vix         = float(regime.get("vix", 20) or 20)
    vix_cat     = (regime.get("vix_category") or "medium").lower()
    spy_rsi     = float(regime.get("spy_rsi", 50) or 50)
    tlt_trend   = (regime.get("tlt_trend") or "").lower()
    mom_phase   = (regime.get("momentum_phase") or "").lower()
    spy_vs_200  = float(regime.get("spy_pct_vs_200ma", 0) or 0)

    return {
        "rd_regime_bull":        1 if spy_trend == "bull" else 0,
        "rd_regime_bear":        1 if spy_trend == "bear" else 0,
        "rd_vix":                round(vix, 2),
        "rd_vix_high":           1 if vix_cat == "high" or vix > 25 else 0,
        "rd_spy_rsi":            round(spy_rsi, 2),
        "rd_spy_overbought":     1 if spy_rsi > 70 else 0,
        "rd_rising_rates":       1 if "rising_rate" in tlt_trend or "rising" in tlt_trend else 0,
        "rd_momentum_late":      1 if "late" in mom_phase or "overbought" in mom_phase else 0,
        "rd_warning_count":      len(warnings),
        "rd_spy_pct_vs_200ma":   round(spy_vs_200, 4),
    }


def _default_regime() -> dict:
    return {
        "rd_regime_bull": 0, "rd_regime_bear": 0,
        "rd_vix": 20.0, "rd_vix_high": 0,
        "rd_spy_rsi": 50.0, "rd_spy_overbought": 0,
        "rd_rising_rates": 0, "rd_momentum_late": 0,
        "rd_warning_count": 0, "rd_spy_pct_vs_200ma": 0.0,
    }


# =============================================================================
# HISTORY UPDATE
# =============================================================================

def _update_history(ticker_features: dict, regime: dict, research: dict, sector_assign: dict) -> None:
    """Append a richer snapshot to rd_ml_history.json."""
    history = _load(_ML_HISTORY)
    if not isinstance(history, list):
        history = []

    now = datetime.now(timezone.utc)

    # Aggregate sector signals (mean across all scored sectors)
    all_scored = sector_assign.get("all_scored", [])
    sector_snapshots = []
    for s in all_scored:
        sd = s.get("score_data", {})
        sector_snapshots.append({
            "sector":   s.get("sector_name", ""),
            "etf":      s.get("etf", ""),
            "rsi":      float(sd.get("rsi_14", 50) or 50),
            "trend":    (sd.get("trend") or "neutral").upper(),
            "ret_1m":   float(sd.get("ret_1m_pct", 0) or 0),
            "ret_3m":   float(sd.get("ret_3m_pct", 0) or 0),
            "vol_20d":  float(sd.get("volatility_20d", 20) or 20),
            "pct_50ma": float(sd.get("pct_from_50ma", 0) or 0),
        })

    # Summary stats from research report
    summary = research.get("summary", {}) or {}
    recs    = research.get("recommendations", []) or []
    convictions = [float(r.get("conviction", 0) or 0) for r in recs if r.get("conviction")]

    # Derive simplified aggregate signals (keep backwards compat)
    spy_trend = "bull" if regime.get("rd_regime_bull") else ("bear" if regime.get("rd_regime_bear") else "neutral")
    macro_map = {"bull": 1, "neutral": 0, "bear": -1}

    bullish_sectors = sum(1 for ss in sector_snapshots if ss["trend"] == "BULLISH")
    sector_breadth  = bullish_sectors / max(len(sector_snapshots), 1)

    cro_risk = sector_assign.get("cro_risk_level", "LOW")
    cro_map  = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    snapshot = {
        "ts":                    now.isoformat(),
        "date":                  now.strftime("%Y-%m-%d"),
        "source":                "rd_ml_bridge_agent",
        # Backwards-compatible aggregate fields
        "rd_macro_regime":       macro_map.get(spy_trend, 0),
        "rd_sector_breadth":     round(sector_breadth, 4),
        "rd_cro_risk_level":     cro_map.get(cro_risk, 0),
        "rd_intl_sentiment":     0.0,   # international data not yet integrated
        "rd_intl_score_raw":     0,
        "rd_n_high_conviction":  int(summary.get("high_conviction", 0)),
        "rd_avg_conviction":     round(sum(convictions) / max(len(convictions), 1), 4),
        # New richer regime fields
        "regime":                regime,
        # Per-sector detail
        "sector_scores":         sector_snapshots,
        # Ticker coverage stats
        "n_tickers_tracked":     len(ticker_features),
        "pct_tickers_bullish":   round(
            sum(1 for v in ticker_features.values() if v.get("rd_ticker_trend_bullish")) /
            max(len(ticker_features), 1), 4
        ),
        "pct_action_buy":        round(
            sum(1 for v in ticker_features.values() if v.get("rd_ticker_action_buy")) /
            max(len(ticker_features), 1), 4
        ),
    }

    history.append(snapshot)
    # Keep last 365 snapshots (2 per day × 365 = ~730 max; trim to 500)
    if len(history) > 500:
        history = history[-500:]

    _save(_ML_HISTORY, history)
    log.info(f"rd_ml_history.json updated ({len(history)} snapshots total)")


# =============================================================================
# SUGGESTION GENERATOR
# =============================================================================

def _check_and_suggest(ticker_features: dict, regime: dict) -> None:
    """
    Compare available R&D signals vs what's currently in the ML model.
    Queue REVIEW suggestions if we spot exploitable gaps.
    Does NOT run if feature is already suggested/implemented.
    """
    sugg_path = _ML_SUGGESTIONS
    existing: list = []
    try:
        with open(sugg_path) as f:
            existing = json.load(f)
    except Exception:
        existing = []

    already_ids = {s.get("id", "") for s in existing}

    new_suggestions = []

    # Suggestion: earnings proximity feature
    _id = "rd_bridge_001"
    if _id not in already_ids:
        has_earnings = sum(1 for v in ticker_features.values() if v.get("rd_earnings_proximity", 90) < 30)
        if has_earnings > 3:
            new_suggestions.append({
                "id":         _id,
                "source":     "ml_rd_bridge_agent",
                "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "category":   "FEATURE",
                "priority":   "REVIEW",
                "title":      "Add rd_earnings_proximity as ML feature",
                "description": (
                    f"R&D tracks {has_earnings} tickers with earnings within 30 days. "
                    "Earnings-window volatility is a known alpha source. "
                    "Encode as days_to_earnings (capped at 90) from rd_ml_bridge_signals.json."
                ),
                "proposed_fix": (
                    "In feature_engineer.py FEATURE_COLS, add 'rd_earnings_proximity'. "
                    "In _get_rd_features(ticker), read from rd_ml_bridge_signals.json. "
                    "Default 90 (far from earnings). "
                    "This feature is already written by ml_rd_bridge_agent.py."
                ),
                "status":     "PENDING",
            })

    # Suggestion: regime overbought warning
    _id = "rd_bridge_002"
    if _id not in already_ids and regime.get("rd_spy_overbought") and regime.get("rd_warning_count", 0) >= 2:
        new_suggestions.append({
            "id":         _id,
            "source":     "ml_rd_bridge_agent",
            "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "category":   "FEATURE",
            "priority":   "REVIEW",
            "title":      "Add rd_spy_overbought + rd_warning_count regime features",
            "description": (
                f"Current regime: SPY RSI={regime.get('rd_spy_rsi')}, {regime.get('rd_warning_count')} warnings. "
                "Historical analyst is flagging overbought conditions. "
                "These regime signals are in rd_ml_bridge_signals.json but not fed to the model."
            ),
            "proposed_fix": (
                "Add 'rd_spy_overbought' and 'rd_warning_count' to FEATURE_COLS in feature_engineer.py. "
                "Read from rd_ml_bridge_signals.json['regime'] in _get_rd_features(ticker). "
                "These are ticker-independent (market-wide) features."
            ),
            "status":     "PENDING",
        })

    # Suggestion: sector RSI breadth
    _id = "rd_bridge_003"
    if _id not in already_ids:
        sector_rsis = [v.get("rd_sector_rsi", 50) for v in ticker_features.values()]
        if sector_rsis:
            avg_sector_rsi = sum(sector_rsis) / len(sector_rsis)
            if avg_sector_rsi > 65 or avg_sector_rsi < 40:
                new_suggestions.append({
                    "id":         _id,
                    "source":     "ml_rd_bridge_agent",
                    "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "category":   "FEATURE",
                    "priority":   "REVIEW",
                    "title":      "Add sector RSI as per-ticker ML feature",
                    "description": (
                        f"Average sector RSI across tracked tickers: {avg_sector_rsi:.1f}. "
                        "Sector-level RSI captures sector rotation momentum not in individual ticker RSI. "
                        "R&D already computes this — just needs routing to feature_engineer."
                    ),
                    "proposed_fix": (
                        "Add 'rd_sector_rsi' and 'rd_sector_bullish' to FEATURE_COLS. "
                        "Read from rd_ml_bridge_signals.json['tickers'][ticker]['rd_sector_rsi']. "
                        "Default 50 (neutral) when ticker not in bridge."
                    ),
                    "status":     "PENDING",
                })

    if new_suggestions:
        existing.extend(new_suggestions)
        with open(sugg_path, "w") as f:
            json.dump(existing, f, indent=2, default=str)
        log.info(f"Added {len(new_suggestions)} new suggestion(s) to ml_suggestions.json")
    else:
        log.info("No new suggestions to add")


# =============================================================================
# MAIN
# =============================================================================

def run():
    log.info("=== R&D → ML Bridge Agent starting ===")

    # ── load all R&D outputs ───────────────────────────────────────────────
    research    = _load(_RESEARCH_REPORT)
    sector_assign = _load(_SECTOR_ASSIGN)
    historical  = _load(_HISTORICAL_CTX)
    geo         = _load(_GEO_CTX)
    earnings    = _load(_EARNINGS_PREDS)

    if not research or not research.get("recommendations"):
        log.warning("research_team_report.json is empty or missing. Skipping.")
        return

    # ── extract features ───────────────────────────────────────────────────
    ticker_features = _build_ticker_features(research, sector_assign, geo, earnings)
    regime_context  = _build_regime_context(historical)

    # ── write bridge signals file ──────────────────────────────────────────
    bridge_out = {
        "updated_at":  datetime.now(timezone.utc).isoformat(),
        "rd_run_at":   research.get("generated_at", "unknown"),
        "n_tickers":   len(ticker_features),
        "regime":      regime_context,
        "tickers":     ticker_features,
    }
    _save(_BRIDGE_OUT, bridge_out)
    log.info(f"Wrote rd_ml_bridge_signals.json — {len(ticker_features)} tickers, "
             f"regime: {'BULL' if regime_context.get('rd_regime_bull') else 'BEAR' if regime_context.get('rd_regime_bear') else 'NEUTRAL'}")

    # ── update rd_ml_history.json with richer snapshot ─────────────────────
    _update_history(ticker_features, regime_context, research, sector_assign)

    # ── check for new feature opportunities ───────────────────────────────
    _check_and_suggest(ticker_features, regime_context)

    # ── brief summary log ─────────────────────────────────────────────────
    bullish = sum(1 for v in ticker_features.values() if v.get("rd_ticker_trend_bullish"))
    buy_calls = sum(1 for v in ticker_features.values() if v.get("rd_ticker_action_buy"))
    geo_flagged = sum(1 for v in ticker_features.values() if v.get("rd_geo_risk"))
    log.info(
        f"Summary: {len(ticker_features)} tickers | "
        f"{bullish} bullish ({100*bullish//max(len(ticker_features),1)}%) | "
        f"{buy_calls} BUY calls | "
        f"{geo_flagged} geo-risk flagged | "
        f"VIX={regime_context.get('rd_vix')} | "
        f"SPY RSI={regime_context.get('rd_spy_rsi')} | "
        f"{regime_context.get('rd_warning_count')} warnings"
    )
    log.info("=== R&D → ML Bridge Agent done ===")


if __name__ == "__main__":
    # Single-instance lock
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('MLRDBridgeAgent'):
        raise SystemExit(0)
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Bridge agent already running — skipping this tick.")
        sys.exit(0)

    run()

#!/usr/bin/env python3
"""
macro_regime_model.py — Macro Regime Model (#18)
=================================================
Produces a multi-class market regime label:
  BULL        — expanding economy, low vol, positive momentum
  LATE_CYCLE  — above-trend growth but rising rates/vol, flattening yield curve
  CONTRACTION — recessionary signals, widening spreads, vol spike
  RECOVERY    — post-trough, improving breadth, still below trend

Inputs (all from public data via yfinance + FRED proxies):
  - SPY 200d MA distance (trend)
  - VIX level + 20d Z-score
  - 10Y-2Y yield spread (proxy from ^TNX - ^IRX)
  - SPY 12-1 momentum (MOM12M)
  - Breadth: % of S&P 500 above 200d MA (proxy via equal-weight vs cap-weight)
  - Credit spread proxy: LQD/IEF ratio

Output saved to data/macro_regime_state.json, read by feature_engineer and all models.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import numpy as np

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
# Silence yfinance download error messages — our code handles them gracefully
for _noisy_logger in ["yfinance", "yfinance.base", "yfinance.utils",
                       "yfinance.download", "curl_cffi", "peewee",
                       "multitasking"]:
    logging.getLogger(_noisy_logger).setLevel(logging.CRITICAL)
logger = logging.getLogger("macro_regime")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
OUTPUT_PATH= os.path.join(DATA_DIR, "macro_regime_state.json")

REGIME_LABELS = ["BULL", "LATE_CYCLE", "CONTRACTION", "RECOVERY"]


def _safe_float(x) -> float:
    try:
        v = float(x)
        return v if np.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _download(ticker: str, period: str = "1y"):
    import yfinance as yf
    df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    return df


def compute_regime_signals() -> dict:
    """
    Download market data and compute regime indicator signals.
    Returns a dict of signals (floats) and the derived regime label.
    """
    signals = {}

    # ── SPY ─────────────────────────────────────────────────────────────────
    try:
        spy = _download("SPY", period="2y")["Close"].dropna()
        if len(spy) > 200:
            spy_last   = float(spy.iloc[-1])
            spy_200ma  = float(spy.rolling(200).mean().iloc[-1])
            spy_50ma   = float(spy.rolling(50).mean().iloc[-1])
            spy_dist_200 = (spy_last - spy_200ma) / spy_200ma * 100 if spy_200ma > 0 else 0.0
            # 12-1 momentum
            spy_12m    = float((spy_last / spy.iloc[-252] - 1) * 100) if len(spy) >= 252 else 0.0
            spy_1m     = float((spy_last / spy.iloc[-21] - 1) * 100)  if len(spy) >= 21  else 0.0
            spy_mom    = spy_12m - spy_1m
            signals["spy_dist_200ma"] = round(spy_dist_200, 2)
            signals["spy_mom12_1"]    = round(spy_mom, 2)
            signals["spy_above_200ma"]= int(spy_last > spy_200ma)
            signals["spy_above_50ma"] = int(spy_last > spy_50ma)
            logger.debug(f"  SPY: dist_200={spy_dist_200:.1f}% mom={spy_mom:.1f}%")
    except Exception as e:
        logger.debug(f"  SPY signal error: {e}")
        signals.setdefault("spy_dist_200ma", 0.0)
        signals.setdefault("spy_mom12_1", 0.0)

    # ── VIX ─────────────────────────────────────────────────────────────────
    try:
        vix = _download("^VIX", period="1y")["Close"].dropna()
        vix_last = float(vix.iloc[-1])
        vix_20d_zscore = (vix_last - float(vix.rolling(20).mean().iloc[-1])) / max(float(vix.rolling(20).std().iloc[-1]), 0.01)
        signals["vix_level"]    = round(vix_last, 2)
        signals["vix_zscore_20d"]= round(vix_20d_zscore, 2)
        logger.debug(f"  VIX: {vix_last:.1f} z={vix_20d_zscore:.2f}")
    except Exception as e:
        logger.debug(f"  VIX signal error: {e}")
        signals.setdefault("vix_level", 20.0)
        signals.setdefault("vix_zscore_20d", 0.0)

    # ── Yield Curve Proxy (10Y - 2Y) ─────────────────────────────────────────
    try:
        t10 = _download("^TNX", period="1y")["Close"].dropna()
        t2  = _download("^IRX", period="1y")["Close"].dropna()
        if len(t10) > 0 and len(t2) > 0:
            spread = float(t10.iloc[-1]) - float(t2.iloc[-1])
            # 20d change in spread
            spread_20d_ago = float(t10.iloc[-21]) - float(t2.iloc[-21]) if len(t10) > 21 else spread
            spread_trend   = spread - spread_20d_ago
            signals["yield_spread_10_2"]      = round(spread, 3)
            signals["yield_spread_trend_20d"] = round(spread_trend, 3)
            signals["yield_curve_inverted"]   = int(spread < 0)
            logger.debug(f"  Yield spread 10-2: {spread:.2f}% trend={spread_trend:.3f}")
    except Exception as e:
        logger.debug(f"  Yield curve signal error: {e}")
        signals.setdefault("yield_spread_10_2", 0.5)
        signals.setdefault("yield_curve_inverted", 0)

    # ── Credit Spread Proxy (LQD/IEF ratio) ────────────────────────────────
    try:
        lqd = _download("LQD", period="1y")["Close"].dropna()
        ief = _download("IEF", period="1y")["Close"].dropna()
        if len(lqd) > 20 and len(ief) > 20:
            ratio     = float(lqd.iloc[-1] / ief.iloc[-1])
            ratio_20d = float(lqd.iloc[-21] / ief.iloc[-21]) if len(lqd) > 21 else ratio
            ratio_pct_chg = (ratio - ratio_20d) / ratio_20d * 100
            signals["credit_spread_proxy"]       = round(ratio, 4)
            signals["credit_spread_trend_20d"]   = round(ratio_pct_chg, 3)
            signals["credit_spreads_widening"]   = int(ratio_pct_chg < -0.5)  # LQD falling vs IEF = spreads widening
            logger.debug(f"  Credit proxy LQD/IEF: {ratio:.4f} pct_chg={ratio_pct_chg:.2f}%")
    except Exception as e:
        logger.debug(f"  Credit spread signal error: {e}")
        signals.setdefault("credit_spreads_widening", 0)

    # ── Market Breadth Proxy (RSP/SPY — equal weight vs cap weight) ─────────
    try:
        rsp = _download("RSP", period="1y")["Close"].dropna()
        spy2= _download("SPY", period="1y")["Close"].dropna()
        if len(rsp) > 20 and len(spy2) > 20:
            ratio_now  = float(rsp.iloc[-1]  / spy2.iloc[-1])
            ratio_20d  = float(rsp.iloc[-21] / spy2.iloc[-21]) if len(rsp) > 21 else ratio_now
            breadth_trend = (ratio_now - ratio_20d) / ratio_20d * 100
            signals["breadth_ew_vs_cw"] = round(breadth_trend, 3)
            signals["breadth_improving"] = int(breadth_trend > 0)
    except Exception as e:
        logger.debug(f"  Breadth signal error: {e}")
        signals.setdefault("breadth_improving", 1)

    return signals


def classify_regime(signals: dict) -> dict:
    """
    Rule-based regime classification from signals.
    Returns {"regime": str, "confidence": float, "probabilities": dict, "rules_fired": list}.
    """
    scores = {label: 0.0 for label in REGIME_LABELS}
    rules  = []

    spy_dist   = _safe_float(signals.get("spy_dist_200ma", 0))
    spy_mom    = _safe_float(signals.get("spy_mom12_1", 0))
    spy_above  = signals.get("spy_above_200ma", 0)
    vix        = _safe_float(signals.get("vix_level", 20))
    vix_z      = _safe_float(signals.get("vix_zscore_20d", 0))
    yld_spread = _safe_float(signals.get("yield_spread_10_2", 0.5))
    inverted   = signals.get("yield_curve_inverted", 0)
    credit_widen= signals.get("credit_spreads_widening", 0)
    breadth_ok = signals.get("breadth_improving", 1)

    # ── BULL signals ──────────────────────────────────────────────────────
    if spy_above and spy_dist > 3:
        scores["BULL"] += 2.0; rules.append("SPY well above 200MA")
    if spy_mom > 5:
        scores["BULL"] += 1.5; rules.append("Strong 12-1 momentum")
    if vix < 18:
        scores["BULL"] += 1.5; rules.append("Low VIX")
    if not inverted and yld_spread > 0.5:
        scores["BULL"] += 1.0; rules.append("Positive yield curve")
    if breadth_ok:
        scores["BULL"] += 1.0; rules.append("Improving breadth")

    # ── LATE_CYCLE signals ───────────────────────────────────────────────
    if spy_above and spy_dist < 5 and spy_mom < 3:
        scores["LATE_CYCLE"] += 2.0; rules.append("SPY above 200MA but momentum fading")
    if vix > 18 and vix < 28:
        scores["LATE_CYCLE"] += 1.0; rules.append("Elevated VIX")
    if inverted:
        scores["LATE_CYCLE"] += 2.0; rules.append("Inverted yield curve")
    if yld_spread < 0.3 and not inverted:
        scores["LATE_CYCLE"] += 1.0; rules.append("Flattening yield curve")
    if credit_widen:
        scores["LATE_CYCLE"] += 1.0; rules.append("Credit spreads widening")

    # ── CONTRACTION signals ──────────────────────────────────────────────
    if not spy_above:
        scores["CONTRACTION"] += 2.0; rules.append("SPY below 200MA")
    if spy_dist < -5:
        scores["CONTRACTION"] += 1.5; rules.append("SPY significantly below 200MA")
    if vix > 28:
        scores["CONTRACTION"] += 2.0; rules.append("High VIX (>28)")
    if vix_z > 1.5:
        scores["CONTRACTION"] += 1.0; rules.append("VIX spike (+1.5 sigma)")
    if credit_widen and inverted:
        scores["CONTRACTION"] += 2.0; rules.append("Inverted curve + credit stress")
    if spy_mom < -5:
        scores["CONTRACTION"] += 2.0; rules.append("Negative momentum")

    # ── RECOVERY signals ─────────────────────────────────────────────────
    if not spy_above and spy_dist > -8 and spy_mom > -2:
        scores["RECOVERY"] += 2.0; rules.append("Below 200MA but recovering (mom improving)")
    if vix > 22 and vix_z < 0:
        scores["RECOVERY"] += 1.5; rules.append("High VIX but falling")
    if breadth_ok and not spy_above:
        scores["RECOVERY"] += 1.5; rules.append("Breadth improving while below 200MA")
    if credit_widen == 0 and not spy_above:
        scores["RECOVERY"] += 1.0; rules.append("Credit stable during drawdown = recovery")

    # Normalize to probabilities
    total = sum(scores.values()) or 1.0
    probs = {k: round(v / total, 3) for k, v in scores.items()}
    best  = max(probs, key=probs.get)
    conf  = probs[best]

    return {
        "regime":        best,
        "confidence":    conf,
        "probabilities": probs,
        "rules_fired":   list(set(rules)),
    }


def run_macro_regime() -> dict:
    """Compute signals, classify regime, save to file."""
    logger.info("Running macro regime model...")
    try:
        signals = compute_regime_signals()
    except Exception as e:
        logger.error(f"  Signal computation failed: {e}", exc_info=True)
        signals = {}

    classification = classify_regime(signals)
    regime = classification["regime"]
    conf   = classification["confidence"]
    logger.info(f"  Regime: {regime} (confidence={conf:.1%})")
    logger.info(f"  Probabilities: {classification['probabilities']}")

    output = {
        "computed_at":   datetime.now(timezone.utc).isoformat(),
        "regime":        regime,
        "confidence":    conf,
        "probabilities": classification["probabilities"],
        "rules_fired":   classification["rules_fired"],
        "signals":       signals,
        # Legacy compat: rd_regime_bull can read from here
        "rd_regime_bull":int(regime in ("BULL",)),
        "is_bull":       int(regime == "BULL"),
        "is_late_cycle": int(regime == "LATE_CYCLE"),
        "is_contraction":int(regime == "CONTRACTION"),
        "is_recovery":   int(regime == "RECOVERY"),
    }

    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(output, f, indent=2, default=str)
        os.replace(tmp, OUTPUT_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    logger.info(f"  Saved to {OUTPUT_PATH}")
    return output


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
        from agent_inbox import main_inbox_only
        main_inbox_only("MacroRegimeModel")
        sys.exit(0)

    result = run_macro_regime()
    print(f"\nRegime: {result['regime']} ({result['confidence']:.1%} confidence)")
    print(f"Signals: {json.dumps({k: v for k,v in result['signals'].items() if isinstance(v, (int, float))}, indent=2)}")

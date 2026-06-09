#!/usr/bin/env python3
"""
APEX CAPITAL — RUNTIME PREDICTOR
==================================
Called during each main.py run after signal_engine generates raw signals.
Applies the ML model to score each signal, then writes an enhanced signal
list to data/predictive_signals.json.

Key concept: predictive_multiplier
------------------------------------
The multiplier is a non-breaking enhancement to the existing conviction_score:

  final_score = raw_conviction_score × predictive_multiplier

  predictive_multiplier range: 1.0 – 1.5
    1.0 = model is neutral / model not trained yet
    1.5 = model assigns maximum confidence to this signal

This means:
  - The pipeline runs identically until the model has enough data
  - High-confidence model picks get up to 50% conviction boost
  - Existing MIN_INSTITUTIONS_SIGNAL threshold and other guards unchanged

Integration point (signal_engine.py):
  After build_confluence_signals() returns raw_signals, main.py calls:
    from predictive_model.predictor import enhance_signals
    signals = enhance_signals(raw_signals)

Output
------
  data/predictive_signals.json  — enhanced signals with ml_score + multiplier

Usage
-----
  from predictive_model.predictor import enhance_signals
  enhanced = enhance_signals(raw_signals)

  python3 predictive_model/predictor.py  (runs on data/signals.json if present)
"""

import sys
import json
import logging
import numpy as np
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from predictive_model.feature_engineer import get_inference_features
from predictive_model.ml_model import load_model, get_model_status

logger = logging.getLogger(__name__)

DATA_DIR      = Path(__file__).parent.parent / "data"


def _dispatch_predict(bundle, feature_df) -> np.ndarray:
    """
    Route prediction to the correct inference function based on bundle version.
    v1 bundles use rf/lr/lgb keys; v2/v3/v4 bundles use fitted_bases + meta_model.
    Falls back to v1 predict_proba if version is not recognised.
    """
    version = bundle.get("version", "v1") if bundle else "v1"
    try:
        if version == "v5":
            from predictive_model.ml_model_v5 import predict_proba_v5
            return predict_proba_v5(feature_df)
        elif version == "v4":
            from predictive_model.ml_model_v4 import predict_proba_v4
            return predict_proba_v4(feature_df)
        elif version == "v3":
            from predictive_model.ml_model_v3 import predict_proba_v3
            return predict_proba_v3(feature_df)
        elif version == "v2":
            from predictive_model.ml_model_v2 import predict_proba_v2
            return predict_proba_v2(feature_df)
        else:
            from predictive_model.ml_model import predict_proba as predict_proba_v1
            return predict_proba_v1(feature_df)
    except Exception as e:
        logger.warning(f"  predictor: {version} inference failed ({e}), trying v1 fallback")
        try:
            from predictive_model.ml_model import predict_proba as predict_proba_v1
            return predict_proba_v1(feature_df)
        except Exception:
            return np.full(len(feature_df), 0.5)
OUTPUT_PATH   = DATA_DIR / "predictive_signals.json"
SIGNALS_PATH  = DATA_DIR / "signals.json"

# Multiplier bounds
MULTIPLIER_MIN = 1.0
MULTIPLIER_MAX = 1.5

# Only boost signals with ML score above this threshold
BOOST_THRESHOLD = 0.55


def _ml_score_to_multiplier(score: float) -> float:
    """
    Map ML probability score (0–1) to a conviction multiplier (1.0–1.5).
    Scores below BOOST_THRESHOLD return 1.0 (no boost).
    Scores above 0.9 return 1.5 (maximum boost).
    """
    if score < BOOST_THRESHOLD:
        return 1.0
    # Linear interpolation between BOOST_THRESHOLD → 1.0 and 1.0 → MULTIPLIER_MAX
    t = (score - BOOST_THRESHOLD) / (1.0 - BOOST_THRESHOLD)
    return round(MULTIPLIER_MIN + t * (MULTIPLIER_MAX - MULTIPLIER_MIN), 4)


def enhance_signals(signals: list[dict]) -> list[dict]:
    """
    Apply ML model scores to raw signals from signal_engine.
    Adds 'ml_score', 'predictive_multiplier', and 'ml_enhanced_conviction'
    to each signal dict.

    If no model is available, returns signals unchanged (multiplier=1.0).
    """
    if not signals:
        return signals

    model = load_model()
    model_active = model is not None

    feature_df = get_inference_features(signals)

    if feature_df.empty or not model_active:
        for sig in signals:
            sig.setdefault("ml_score", 0.5)
            sig.setdefault("predictive_multiplier", 1.0)
            sig.setdefault("ml_enhanced_conviction",
                           sig.get("conviction_score", sig.get("score", 0.0)))
            sig["model_active"] = False
        return signals

    try:
        scores = _dispatch_predict(model, feature_df)
    except Exception as e:
        logger.warning(f"  predictor: model prediction failed: {e} — falling back to 1.0 multiplier")
        for sig in signals:
            sig.setdefault("ml_score", 0.5)
            sig.setdefault("predictive_multiplier", 1.0)
            sig.setdefault("ml_enhanced_conviction",
                           sig.get("conviction_score", sig.get("score", 0.0)))
            sig["model_active"] = False
        return signals

    # Build ticker → score map (take max if ticker appears multiple times)
    ticker_scores = {}
    for row, score in zip(feature_df.itertuples(index=False), scores):
        ticker = row.ticker if hasattr(row, "ticker") else ""
        ticker_scores[ticker] = max(ticker_scores.get(ticker, 0.0), float(score))

    enhanced = []
    for sig in signals:
        ticker = sig.get("ticker", "")
        ml_score = ticker_scores.get(ticker, 0.5)
        multiplier = _ml_score_to_multiplier(ml_score)

        raw_conviction = sig.get("conviction_score", sig.get("score", 0.0))
        enhanced_conviction = round(raw_conviction * multiplier, 2)

        enhanced.append({
            **sig,
            "ml_score":               round(ml_score, 4),
            "predictive_multiplier":  multiplier,
            "ml_enhanced_conviction": enhanced_conviction,
            "model_active":           True,
        })

    # Sort by enhanced conviction descending
    enhanced.sort(key=lambda x: x.get("ml_enhanced_conviction", 0), reverse=True)

    # Write output
    output = {
        "generated_at":  datetime.now().isoformat(),
        "model_active":  model_active,
        "n_signals":     len(enhanced),
        "n_boosted":     sum(1 for s in enhanced if s.get("predictive_multiplier", 1.0) > 1.0),
        "signals":       enhanced,
    }
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    n_boosted = output["n_boosted"]
    avg_mult  = np.mean([s.get("predictive_multiplier", 1.0) for s in enhanced])
    logger.info(f"  predictor: {len(enhanced)} signals scored, {n_boosted} boosted, "
                f"avg multiplier={avg_mult:.3f}")

    return enhanced


def get_predictive_summary() -> dict:
    """Return a summary for morning_briefing.py."""
    status = get_model_status()
    if not OUTPUT_PATH.exists():
        return {"status": "NO_OUTPUT", "model": status}

    try:
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
        return {
            "status":       "OK",
            "model":        status,
            "n_signals":    data.get("n_signals", 0),
            "n_boosted":    data.get("n_boosted", 0),
            "generated_at": data.get("generated_at", ""),
        }
    except Exception:
        return {"status": "ERROR", "model": status}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    # Load raw signals if available and run enhancement
    raw = []
    if SIGNALS_PATH.exists():
        with open(SIGNALS_PATH) as f:
            data = json.load(f)
            raw = data if isinstance(data, list) else data.get("signals", [])

    if not raw:
        print("No signals found. Run main.py first to generate signals.")
    else:
        enhanced = enhance_signals(raw)
        print(f"Enhanced {len(enhanced)} signals.")
        for s in enhanced[:5]:
            print(f"  {s.get('ticker','?'):6s}  conviction={s.get('conviction_score',0):.1f}"
                  f"  ml_score={s.get('ml_score',0):.3f}"
                  f"  multiplier={s.get('predictive_multiplier',1.0):.3f}"
                  f"  final={s.get('ml_enhanced_conviction',0):.1f}")

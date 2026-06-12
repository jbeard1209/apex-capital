# =============================================================================
# APEX CAPITAL — EARNINGS PREDICTION AGENT
# =============================================================================
# Runs as part of the R&D pipeline. For every stock in the universe that has
# an earnings date within the next 90 days it:
#
#   1. Pulls quarterly EPS history (yfinance)
#   2. Calculates trailing beat rate and EPS trend (accelerating / decelerating)
#   3. Compares projected EPS to analyst consensus estimate
#   4. Emits a prediction: BEAT | MISS | INLINE, with a confidence score 1-5
#
# Output → data/earnings_predictions.json
#
# Usage:
#   python3 earnings_predictor.py
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import yfinance as yf

from config import STOCK_UNIVERSE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("earnings_predictor")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)

OUTPUT_PATH = "data/earnings_predictions.json"
LOOKAHEAD_DAYS = 90     # predict earnings within the next 90 days


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _earnings_date(ticker_obj) -> Optional[datetime]:
    """Return the next upcoming earnings date (tz-naive), or None."""
    import datetime as _dt
    try:
        cal = ticker_obj.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("earningsDate") or []
            if hasattr(dates, "__iter__"):
                future = []
                today = _dt.date.today()
                for d in dates:
                    try:
                        # normalise to date
                        if hasattr(d, "to_pydatetime"):
                            d = d.to_pydatetime().date()
                        elif hasattr(d, "date") and callable(d.date):
                            d = d.date()
                        elif isinstance(d, _dt.datetime):
                            d = d.date()
                        # now d should be datetime.date
                        if d > today:
                            future.append(d)
                    except Exception:
                        pass
                if future:
                    earliest = min(future)
                    return _dt.datetime(earliest.year, earliest.month, earliest.day)
    except Exception:
        pass
    return None


def _quarterly_eps_history(ticker_obj) -> list[float]:
    """Return list of reported Diluted EPS per quarter, oldest → newest."""
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            df = getattr(ticker_obj, attr, None)
            if df is None or df.empty:
                continue
            for row_key in ("Diluted EPS", "Basic EPS", "EPS"):
                if row_key in df.index:
                    vals = df.loc[row_key].dropna().tolist()
                    # yfinance returns columns newest-first
                    return list(reversed(vals))
        except Exception:
            pass
    return []


def _analyst_eps_estimate(info: dict) -> Optional[float]:
    """Best available forward EPS estimate from yfinance info dict."""
    for key in ("epsCurrentYear", "epsForwardQuarter", "earningsEstimate",
                "forwardEps", "epsNextQuarter"):
        v = _safe_float(info.get(key))
        if v is not None:
            return v
    return None


def _eps_trend(history: list[float]) -> str:
    """Characterise EPS trajectory over the last 4 quarters."""
    if len(history) < 3:
        return "INSUFFICIENT_DATA"
    recent = history[-4:]
    diffs  = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    pos    = sum(1 for d in diffs if d > 0)
    neg    = sum(1 for d in diffs if d < 0)
    if pos >= 2 and pos > neg:
        return "ACCELERATING"
    if neg >= 2 and neg > pos:
        return "DECELERATING"
    return "MIXED"


def _beat_rate(ticker_obj) -> Optional[float]:
    """Fraction of recent quarters where actual EPS beat estimate (0.0–1.0)."""
    try:
        df = ticker_obj.earnings_dates
        if df is None or df.empty:
            return None
        needed = {"Reported EPS", "EPS Estimate"}
        if not needed.issubset(set(df.columns)):
            return None
        valid = df.dropna(subset=["Reported EPS", "EPS Estimate"]).head(8)
        if valid.empty:
            return None
        beats = (valid["Reported EPS"] > valid["EPS Estimate"]).sum()
        return float(beats) / len(valid)
    except Exception:
        return None


def _predict(history: list[float],
             estimate: Optional[float],
             beat_rate_val: Optional[float],
             trend: str) -> tuple[str, int, str]:
    """
    Return (prediction, confidence_1_5, reasoning).
    prediction: 'BEAT' | 'MISS' | 'INLINE'
    """
    signals_beat = 0
    signals_miss = 0
    reasons = []

    if trend == "ACCELERATING":
        signals_beat += 2
        reasons.append("EPS accelerating for 3+ quarters")
    elif trend == "DECELERATING":
        signals_miss += 2
        reasons.append("EPS decelerating for 3+ quarters")
    else:
        reasons.append("EPS trend mixed")

    if beat_rate_val is not None:
        if beat_rate_val >= 0.75:
            signals_beat += 2
            reasons.append(f"historical beat rate {beat_rate_val:.0%}")
        elif beat_rate_val <= 0.40:
            signals_miss += 2
            reasons.append(f"historical beat rate only {beat_rate_val:.0%}")
        else:
            signals_beat += 1
            reasons.append(f"historical beat rate {beat_rate_val:.0%}")

    # Momentum: last reported vs estimate gap
    if estimate is not None and len(history) >= 1:
        last_actual = history[-1]
        if last_actual > estimate * 1.05:
            signals_beat += 1
            reasons.append(f"last Q beat estimate by >{5:.0f}%")
        elif last_actual < estimate * 0.95:
            signals_miss += 1
            reasons.append(f"last Q missed estimate by >{5:.0f}%")

    if signals_beat > signals_miss + 1:
        pred = "BEAT"
        confidence = min(5, 2 + signals_beat)
    elif signals_miss > signals_beat + 1:
        pred = "MISS"
        confidence = min(5, 2 + signals_miss)
    else:
        pred = "INLINE"
        confidence = 2

    return pred, confidence, "; ".join(reasons)


def run(universe: list[str] = None) -> list[dict]:
    tickers = universe or STOCK_UNIVERSE
    now     = datetime.now()
    cutoff  = now + timedelta(days=LOOKAHEAD_DAYS)

    predictions = []
    logger.info(f"[EarningsPredictor] Scanning {len(tickers)} tickers (next {LOOKAHEAD_DAYS}d)")

    for sym in tickers:
        try:
            t    = yf.Ticker(sym)
            info = t.info or {}

            earn_date = _earnings_date(t)
            if earn_date is None or earn_date > cutoff or earn_date < now:
                del t
                continue

            days_out    = (earn_date - now).days
            history     = _quarterly_eps_history(t)
            estimate    = _analyst_eps_estimate(info)
            br          = _beat_rate(t)
            trend       = _eps_trend(history)
            pred, conf, reasoning = _predict(history, estimate, br, trend)

            forward_pe  = _safe_float(info.get("forwardPE"))
            trailing_pe = _safe_float(info.get("trailingPE"))
            sector      = info.get("sector", "Unknown")
            name        = info.get("shortName") or sym

            predictions.append({
                "ticker":           sym,
                "name":             name,
                "sector":           sector,
                "earnings_date":    earn_date.strftime("%Y-%m-%d"),
                "days_until":       days_out,
                "prediction":       pred,
                "confidence":       conf,
                "reasoning":        reasoning,
                "eps_trend":        trend,
                "beat_rate":        round(br, 2) if br is not None else None,
                "analyst_estimate": estimate,
                "eps_history_qtrs": len(history),
                "forward_pe":       forward_pe,
                "trailing_pe":      trailing_pe,
            })

            logger.info(
                f"  {sym:<6} → {pred} ({conf}★)  earnings in {days_out}d  "
                f"trend={trend}  beat_rate={f'{br:.0%}' if br is not None else 'n/a'}"
            )
            del t

        except Exception as e:
            logger.warning(f"  [EarningsPredictor] {sym}: {e}")

    # ── Fable 5 sanity check — gated to high-confidence MISS calls on
    #    positions the firm actually holds (≤ a few calls per week) ─────────
    try:
        from apex_llm import reason as _r, is_available
        if is_available("earnings_predictor"):
            import json as _j
            from pathlib import Path as _P
            _state = {}
            _sp = _P(__file__).parent / "data" / "state.json"
            if _sp.exists():
                _state = _j.loads(_sp.read_text())
            held = set(_state.get("entry_dates", {}).keys())
            for p in predictions:
                if p["ticker"] in held and p["prediction"] == "MISS" and p["confidence"] >= 4:
                    raw = _r(
                        "You are an earnings analyst. Sanity-check a mechanical MISS prediction.",
                        f"{p['ticker']} ({p['sector']}) reports in {p['days_until']}d. "
                        f"Mechanical model: MISS {p['confidence']}★ — {p['reasoning'][:150]}. "
                        f"EPS trend {p['eps_trend']}, beat rate {p['beat_rate']}.\n"
                        "We HOLD this stock. 2 sentences: agree with MISS, and trim before earnings?",
                        agent_name="earnings_predictor", max_tokens=110,
                    )
                    if raw:
                        p["llm_check"] = raw.strip()
    except Exception:
        pass

    # Sort by earnings date
    predictions.sort(key=lambda x: x["days_until"])

    payload = {
        "generated_at":    datetime.now().isoformat(),
        "lookahead_days":  LOOKAHEAD_DAYS,
        "tickers_scanned": len(tickers),
        "predictions":     predictions,
        "summary": {
            "total":         len(predictions),
            "beat_calls":    sum(1 for p in predictions if p["prediction"] == "BEAT"),
            "miss_calls":    sum(1 for p in predictions if p["prediction"] == "MISS"),
            "inline_calls":  sum(1 for p in predictions if p["prediction"] == "INLINE"),
            "high_conf":     sum(1 for p in predictions if p["confidence"] >= 4),
        },
    }

    os.makedirs("data", exist_ok=True)
    # Write to a temp file first then rename so a crash mid-write never leaves a corrupt OUTPUT_PATH.
    _tmp_path = OUTPUT_PATH + ".tmp"
    try:
        with open(_tmp_path, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(_tmp_path, OUTPUT_PATH)
    except Exception as e:
        logger.warning(f"[EarningsPredictor] Failed to write output: {e}")
        try:
            os.remove(_tmp_path)
        except OSError:
            pass

    logger.info(
        f"[EarningsPredictor] Done — {len(predictions)} upcoming earnings: "
        f"{payload['summary']['beat_calls']} BEAT / "
        f"{payload['summary']['miss_calls']} MISS / "
        f"{payload['summary']['inline_calls']} INLINE"
    )
    return predictions


if __name__ == "__main__":
    run()

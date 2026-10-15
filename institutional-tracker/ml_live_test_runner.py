# =============================================================================
# APEX CAPITAL — ML LIVE PICKS TEST RUNNER
# =============================================================================
#
# Tests the ST model's real-world performance by:
#   1. Running the predictor on the current universe → recording picks
#   2. After 20 calendar days, fetching actual price outcomes
#   3. Computing hit rate, directional accuracy, and return vs. benchmark
#   4. Writing a human-readable report and logging to ml_improvement_log.json
#
# The LT model enters live testing automatically when its AUC crosses 0.57.
#
# Schedule: run nightly (added to GCP crontab alongside overnight_ml_runner.py)
#
# Usage:
#   python3 ml_live_test_runner.py            # enter new picks + evaluate expired picks
#   python3 ml_live_test_runner.py --enter    # only enter new picks
#   python3 ml_live_test_runner.py --evaluate # only evaluate expired picks
#   python3 ml_live_test_runner.py --report   # print full report
# =============================================================================

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ml_live_test_runner")

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"

LIVE_PICKS_FILE   = DATA_DIR / "ml_live_picks_log.json"
IMPROVEMENT_LOG   = DATA_DIR / "ml_improvement_log.json"
# (Removed unused ST_QUEUE_FILE / LT_QUEUE_FILE — an "experiment queue" feature
#  that was never implemented; the constants were declared but read/written
#  nowhere, leaving two permanently-empty orphan files. See wiring_audit.py.)
ST_LIVE_AUC_GATE  = 0.57   # ST model must clear this before live picks begin
LT_LIVE_AUC_GATE  = 0.57   # LT model must clear this before live picks begin

HORIZON_DAYS_ST   = 20
HORIZON_DAYS_LT   = 365


def _model_clears_gate(model: str):
    """Honest gate: a model only enters live testing once its walk-forward AUC
    clears the gate. Reads the single source of truth (model_metrics), so it tracks
    whatever was actually trained — no hardcoded numbers. Returns (ok, auc, gate)."""
    try:
        from model_metrics import get_model_metrics
        m = get_model_metrics()
    except Exception:
        return False, None, (LT_LIVE_AUC_GATE if model == "longterm" else ST_LIVE_AUC_GATE)
    if model == "longterm":
        auc = m.get("lt_auc")
        return bool(auc is not None and auc >= LT_LIVE_AUC_GATE), auc, LT_LIVE_AUC_GATE
    auc = m.get("auc")   # ST walk-forward headline
    return bool(auc is not None and auc >= ST_LIVE_AUC_GATE), auc, ST_LIVE_AUC_GATE


def _load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else {}


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def _fetch_close(ticker: str) -> Tuple[Optional[float], Optional[str]]:
    """Latest close AND the market date it belongs to.

    The date is what makes an entry an observation. The cron runs at 06:00 UTC
    every day, so Saturday, Sunday and Monday (pre-open) all read Friday's
    close: three "picks" that are one bet. That is how 2026-06-14 (a Sunday)
    and 2026-06-15 each logged the same 10 tickers at identical entry prices,
    were evaluated in the same pass at identical exit prices, and the report
    showed n=20 for 10 outcomes.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1]), hist.index[-1].date().isoformat()
    except Exception as e:
        logger.warning(f"Could not fetch price for {ticker}: {e}")
    return None, None


def _fetch_current_price(ticker: str) -> Optional[float]:
    return _fetch_close(ticker)[0]


def _independent_observations(picks: list) -> list:
    """Collapse evaluated picks that are the same bet logged twice.

    Keyed on (model, ticker, the close the entry used, exit price). New rows
    carry `entry_price_date`; rows from before it existed fall back to the
    entry price itself — two entries at the identical float price for the
    same ticker came from the same close. First occurrence wins.
    """
    seen, out = set(), []
    for p in picks:
        entry_key = p.get("entry_price_date") or round(float(p.get("entry_price") or 0), 4)
        key = (p.get("model"), p.get("ticker"), entry_key,
               round(float(p.get("exit_price") or 0), 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def enter_new_picks(model: str = "shortterm", top_n: int = 10) -> list:
    """
    Run the predictor on the current universe and record the top-N picks.
    """
    logger.info(f"Entering new live picks for {model} model (top {top_n})...")

    picks_log = _load_json(LIVE_PICKS_FILE, {"picks": []})
    today = date.today().isoformat()
    horizon = HORIZON_DAYS_LT if model == "longterm" else HORIZON_DAYS_ST

    # Check we haven't already entered picks today for this model
    todays_picks = [p for p in picks_log["picks"] if p.get("pick_date") == today and p.get("model") == model]
    if todays_picks:
        logger.info(f"  Picks already entered today for {model} model ({len(todays_picks)} picks). Skipping.")
        return todays_picks

    # AUC gate — a model only enters live testing once it clears its gate. This is
    # the enforcement the module always documented but never actually applied.
    clears, auc, gate = _model_clears_gate(model)
    if not clears:
        logger.info(f"  {model} model AUC={auc} < gate {gate} — NOT entering live picks "
                    f"(model held in validation until it clears the gate).")
        picks_log["picks"].append({
            "model": model, "pick_date": today, "status": "model_below_gate",
            "auc": auc, "gate": gate, "horizon_days": horizon, "picks": [],
        })
        _save_json(LIVE_PICKS_FILE, picks_log)
        return []

    try:
        sys.path.insert(0, str(BASE_DIR))
        from predictive_model.predictor import enhance_signals

        # Build a placeholder signal list for the full tracked universe
        # The predictor will score them using the latest trained model
        import yfinance as yf
        universe = [
            "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V",
            "MA", "UNH", "LLY", "JNJ", "WMT", "HD", "PG", "MRK", "XOM", "CVX",
            "BAC", "KO", "PEP", "ABBV", "TMO", "COST", "ACN", "CRM", "AMD", "NFLX",
            "ADBE", "TXN", "QCOM", "INTC", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "ON",
            "SPY", "QQQ", "IWM", "GLD", "TLT",
        ]
        stub_signals = [{"ticker": t, "action": "BUY", "conviction": 0.5} for t in universe]
        enhanced = enhance_signals(stub_signals)
    except Exception as e:
        logger.warning(f"  Predictor unavailable: {e}. Entering placeholder picks.")
        enhanced = []

    # Sort by ML score descending
    enhanced_buy = [s for s in enhanced if s.get("ml_score") is not None]
    enhanced_buy.sort(key=lambda x: x.get("ml_score", 0), reverse=True)
    top_picks = enhanced_buy[:top_n] if enhanced_buy else []

    # Fallback: if predictor returned nothing, record a note
    if not top_picks:
        logger.warning("  No ML scores returned — recording empty pick set for today.")
        entry = {
            "model": model,
            "pick_date": today,
            "evaluate_after": (date.today() + timedelta(days=horizon)).isoformat(),
            "status": "no_picks_today",
            "note": "Predictor unavailable or returned no scores.",
            "picks": [],
        }
        picks_log["picks"].append(entry)
        _save_json(LIVE_PICKS_FILE, picks_log)
        return []

    # A (ticker, close-date) already on the log is not a new observation —
    # see _fetch_close. Skip it rather than log the same bet twice.
    already = {(p.get("model"), p.get("ticker"), p.get("entry_price_date"))
               for p in picks_log["picks"] if p.get("entry_price_date")}
    new_picks, n_same_close = [], 0
    for pick in top_picks:
        ticker = pick.get("ticker", "?")
        entry_price, entry_price_date = _fetch_close(ticker)
        if entry_price_date and (model, ticker, entry_price_date) in already:
            n_same_close += 1
            continue
        new_picks.append({
            "model": model,
            "pick_date": today,
            "evaluate_after": (date.today() + timedelta(days=horizon)).isoformat(),
            "status": "pending",
            "ticker": ticker,
            "ml_score": pick.get("ml_score"),
            "ml_label": pick.get("ml_label"),
            "conviction": pick.get("conviction"),
            "entry_price": entry_price,
            "entry_price_date": entry_price_date,
            "exit_price": None,
            "return_pct": None,
            "correct_direction": None,
        })

    if not new_picks and n_same_close:
        # A visible row, not silence: the run happened, there was nothing new.
        picks_log["picks"].append({
            "model": model, "pick_date": today, "status": "no_new_session",
            "note": f"All {n_same_close} picks priced off a close already logged.",
            "horizon_days": horizon, "picks": [],
        })
    picks_log["picks"].extend(new_picks)
    _save_json(LIVE_PICKS_FILE, picks_log)
    logger.info(f"  Recorded {len(new_picks)} live picks for {today}"
                f" ({n_same_close} skipped: same close already logged).")
    return new_picks


def evaluate_expired_picks() -> dict:
    """
    Find picks whose evaluate_after date has passed, fetch exit prices,
    compute returns, and write a summary to the improvement log.
    """
    picks_log = _load_json(LIVE_PICKS_FILE, {"picks": []})
    today = date.today().isoformat()
    results = {"evaluated": 0, "correct": 0, "incorrect": 0, "picks": []}

    for pick in picks_log["picks"]:
        if pick.get("status") != "pending":
            continue
        if pick.get("evaluate_after", "9999-99-99") > today:
            continue

        ticker = pick.get("ticker")
        if not ticker:
            pick["status"] = "skipped"
            continue

        exit_price = _fetch_current_price(ticker)
        entry_price = pick.get("entry_price")

        if exit_price and entry_price:
            ret = (exit_price - entry_price) / entry_price
            pick["exit_price"]        = round(exit_price, 4)
            pick["return_pct"]        = round(ret * 100, 2)
            pick["correct_direction"] = ret > 0  # model predicted UP (BUY signal)
            pick["status"]            = "evaluated"
            pick["evaluated_on"]      = today

            results["evaluated"] += 1
            if pick["correct_direction"]:
                results["correct"] += 1
            else:
                results["incorrect"] += 1
            results["picks"].append(pick)
        else:
            pick["status"] = "price_unavailable"

    _save_json(LIVE_PICKS_FILE, picks_log)

    # Score independent observations only — the same bet logged twice is one
    # outcome, and counting it twice halves the apparent sampling error.
    results["picks"] = _independent_observations(results["picks"])
    results["evaluated"] = len(results["picks"])
    results["correct"] = sum(1 for p in results["picks"] if p.get("correct_direction"))
    results["incorrect"] = results["evaluated"] - results["correct"]

    if results["evaluated"] > 0:
        hit_rate = results["correct"] / results["evaluated"]
        avg_ret  = sum(p.get("return_pct", 0) for p in results["picks"]) / results["evaluated"]
        results["hit_rate_pct"] = round(hit_rate * 100, 1)
        results["avg_return_pct"] = round(avg_ret, 2)
        _log_evaluation_to_improvement_log(results)
        logger.info(
            f"  Evaluated {results['evaluated']} picks — "
            f"hit rate: {results['hit_rate_pct']}% | avg return: {avg_ret:.2f}%"
        )

    return results


def _log_evaluation_to_improvement_log(results: dict):
    log = _load_json(IMPROVEMENT_LOG, {"entries": []})
    log["entries"].append({
        "logged_at":     datetime.now().isoformat(),
        "type":          "live_pick_evaluation",
        "model":         "shortterm",
        "horizon_days":  HORIZON_DAYS_ST,
        "n_evaluated":   results["evaluated"],
        "n_correct":     results["correct"],
        "n_incorrect":   results["incorrect"],
        "hit_rate_pct":  results.get("hit_rate_pct"),
        "avg_return_pct": results.get("avg_return_pct"),
        "detail":        results["picks"],
    })
    _save_json(IMPROVEMENT_LOG, log)


def print_full_report():
    picks_log  = _load_json(LIVE_PICKS_FILE, {"picks": []})
    all_picks  = picks_log.get("picks", [])
    raw_evaluated = [p for p in all_picks if p.get("status") == "evaluated"]
    evaluated  = _independent_observations(raw_evaluated)
    pending    = [p for p in all_picks if p.get("status") == "pending"]
    # Gate-held / no-pick rows are run records, not picks. Counting them made
    # "Total picks recorded: 224" out of 20 real picks.
    real_picks = [p for p in all_picks if p.get("ticker")]
    gate_held  = sum(1 for p in all_picks if p.get("status") == "model_below_gate")

    print("\n" + "="*65)
    print("APEX CAPITAL — ML LIVE PICKS REPORT (ST Model)")
    print("="*65)
    print(f"Total picks recorded : {len(real_picks)}  ({gate_held} runs held below the AUC gate)")
    print(f"Evaluated            : {len(evaluated)} independent"
          + (f"  ({len(raw_evaluated) - len(evaluated)} duplicate entries of the same close collapsed)"
             if len(raw_evaluated) != len(evaluated) else ""))
    print(f"Pending (< 20 days)  : {len(pending)}")
    if evaluated:
        hit_rate = sum(1 for p in evaluated if p.get("correct_direction")) / len(evaluated)
        avg_ret  = sum(p.get("return_pct", 0) for p in evaluated) / len(evaluated)
        print(f"\nHit Rate             : {hit_rate*100:.1f}%  (need > 55% to confirm model edge)")
        print(f"Average Return       : {avg_ret:.2f}%  per 20-day holding period")
        print(f"\nTop 5 winners:")
        for p in sorted(evaluated, key=lambda x: x.get("return_pct", 0), reverse=True)[:5]:
            print(f"  {p['ticker']:6s}  {p['return_pct']:+.1f}%  (entered {p['pick_date']})")
        print(f"\nTop 5 losers:")
        for p in sorted(evaluated, key=lambda x: x.get("return_pct", 0))[:5]:
            print(f"  {p['ticker']:6s}  {p['return_pct']:+.1f}%  (entered {p['pick_date']})")
    print("="*65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enter",    action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--report",   action="store_true")
    args = parser.parse_args()

    os.chdir(BASE_DIR)  # ensure relative paths work

    if args.report:
        print_full_report()
    elif args.enter:
        enter_new_picks("shortterm")
        enter_new_picks("longterm")   # gated off until LT clears 0.57
    elif args.evaluate:
        evaluate_expired_picks()
    else:
        enter_new_picks("shortterm")
        enter_new_picks("longterm")   # gated off until LT clears 0.57
        evaluate_expired_picks()
        print_full_report()

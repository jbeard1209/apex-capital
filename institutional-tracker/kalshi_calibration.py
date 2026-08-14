#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI CALIBRATION (the gate the whole sector hangs on)
======================================================================
This is the file that decides whether the Kalshi sector is ever allowed to risk
money on an opinion, and it is the reason the Chairman's three-week training
period is pointed at the right target.

THE WRONG TARGET IS PROFIT. Over three weeks at 96c you might take twenty-four
positions and win all of them, and still have no evidence at all: at a true 96%
the expected number of losses in 24 tries is one, and observing zero is
completely ordinary. A run of wins at 24:1 payoff is the single most misleading
signal in this strategy, because it looks like proof exactly when it is noise.

THE RIGHT TARGET IS CALIBRATION. When we say 99%, does it happen 99% of the
time? That is measurable, it is falsifiable, and — critically — it can be
measured on predictions we never funded. Every scan writes what it BELIEVED;
settlement writes what HAPPENED; this file compares them. Three weeks of
shadow predictions is a real experiment. Three weeks of P&L is an anecdote.

THE BASELINE IS THE MARKET, NOT ZERO
------------------------------------
An estimator that is 97% accurate sounds excellent and is worthless here: the
market was already at 96c, breakeven is ~96.3%, and being right 97% of the time
about things the market prices at 96% is not an edge, it is agreement plus
fees. So `is_edge_proven()` requires our Brier score to beat the Brier score of
simply believing the market price. That mirrors `model_test_harness.py`, which
makes the equity model beat a naive momentum baseline before it counts.

SMALL SAMPLES LIE, LOUDLY
-------------------------
In the 90-100% bucket the whole question is a difference of ~3 percentage
points. Distinguishing 96% from 99% at any confidence needs hundreds of
settled observations, not dozens. `is_edge_proven()` therefore refuses on
sample size FIRST and says so, rather than returning a verdict it has not
earned (memory `verdict-with-no-consequence`).

Usage:
    python3 kalshi_calibration.py --record     # log today's scan predictions
    python3 kalshi_calibration.py --resolve    # fetch settlements, fill outcomes
    python3 kalshi_calibration.py --report     # reliability curve + verdict
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client as kc

DATA_DIR = Path(__file__).resolve().parent / "data"
LEDGER = DATA_DIR / "kalshi_predictions.json"
SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"

# Below this many SETTLED observations the answer is "not yet", never "no edge"
# and never "proven". Chosen because separating 96% from 99% is a 3-point
# question: at n=200 the standard error on a 97% rate is ~1.2 points, which is
# only just inside the gap we are trying to resolve.
MIN_SETTLED_FOR_VERDICT = 200

# Buckets for the reliability curve. Deliberately fine at the top, because the
# entire strategy lives in the last two.
BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9),
           (0.9, 0.95), (0.95, 0.98), (0.98, 1.0)]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_ledger():
    try:
        d = json.loads(LEDGER.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_ledger(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, indent=2))


# ----------------------------------------------------------------- record --

def record_from_scan(scan=None):
    """Append today's near-certainty observations to the append-only ledger.

    Idempotent on (ticker, scan date): re-running the scanner four times a day
    must not quadruple-count one market into the calibration sample, which
    would make a single lucky settlement look like four confirmations.
    """
    if scan is None:
        try:
            scan = json.loads(SCAN_LOG.read_text())
        except Exception:
            return 0, "no scan log to record from — run kalshi_scanner.py first"

    rows = _load_ledger()
    have = {(r.get("ticker"), r.get("recorded_date")) for r in rows}
    day = _now()[:10]
    added = 0

    for c in scan.get("disagreement", []):
        key = (c.get("ticker"), day)
        if key in have or not c.get("ticker"):
            continue
        rows.append({
            "id": f"KAL-{day}-{c['ticker']}",
            "recorded_at": _now(), "recorded_date": day,
            "ticker": c["ticker"], "event_ticker": c.get("event_ticker"),
            "title": c.get("title"),
            # what the market said
            "market_price": c.get("market_price"),
            "breakeven_probability": c.get("breakeven_probability"),
            # what WE said. None is honest: no calibrated estimator exists yet,
            # and a null here is what keeps an unmeasured guess out of the
            # sample rather than quietly defaulting it to the market price.
            "p_hat": c.get("p_hat"),
            "rationale": c.get("rationale"),
            "close_time": c.get("close_time"),
            # filled in by --resolve
            "settled": False, "outcome": None, "settled_at": None,
        })
        have.add(key)
        added += 1

    _save_ledger(rows)
    return added, f"{added} prediction(s) recorded, ledger now {len(rows)}"


# ---------------------------------------------------------------- resolve --

def resolve_settlements(max_lookups=400):
    """Fill in what actually happened. Only touches rows still unsettled.

    A market that cannot be looked up stays unsettled forever rather than being
    dropped or guessed. Dropping it would bias the sample toward outcomes we
    could see, which is exactly how a calibration study reports the number its
    author hoped for.
    """
    rows = _load_ledger()
    pending = [r for r in rows if not r.get("settled")][:max_lookups]
    resolved = failed = 0

    for r in pending:
        try:
            d = kc._get(f"/markets/{r['ticker']}")
            m = d.get("market", d)
            status = str(m.get("status") or "").lower()
            result = str(m.get("result") or "").lower()
            if status in ("settled", "finalized") and result in ("yes", "no"):
                r["settled"] = True
                r["outcome"] = 1 if result == "yes" else 0
                r["settled_at"] = _now()
                resolved += 1
        except Exception:
            failed += 1

    _save_ledger(rows)
    return resolved, (f"{resolved} settled, {failed} lookup failure(s), "
                      f"{sum(1 for r in rows if not r.get('settled'))} still open")


# ------------------------------------------------------------ measurement --

def _brier(pairs):
    """Mean squared error between forecast probability and 0/1 outcome."""
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def reliability_curve(rows=None):
    """Observed settlement frequency per forecast bucket, plus a Wilson interval.

    The Wilson interval is used rather than the textbook normal approximation
    because at p near 1.0 the normal interval runs above 100% and can make an
    obviously-broken estimator look statistically fine.
    """
    rows = rows if rows is not None else _load_ledger()
    settled = [r for r in rows
               if r.get("settled") and r.get("outcome") in (0, 1)]

    curve = []
    for lo, hi in BUCKETS:
        # Fall back to the market price when we have no view of our own, so the
        # market's own calibration is measured from day one even before an
        # estimator exists. Which forecast a row used is recorded explicitly.
        pts = []
        for r in settled:
            p = r.get("p_hat")
            if p is None:
                p = r.get("market_price")
            if p is None:
                continue
            if lo <= p < hi or (hi == 1.0 and p == 1.0):
                pts.append((float(p), int(r["outcome"])))
        if not pts:
            curve.append({"bucket": f"{lo:.2f}-{hi:.2f}", "n": 0,
                          "mean_forecast": None, "observed": None,
                          "ci_low": None, "ci_high": None})
            continue

        n = len(pts)
        obs = sum(o for _, o in pts) / n
        z = 1.96
        denom = 1 + z * z / n
        centre = (obs + z * z / (2 * n)) / denom
        half = z * math.sqrt(obs * (1 - obs) / n + z * z / (4 * n * n)) / denom
        curve.append({
            "bucket": f"{lo:.2f}-{hi:.2f}", "n": n,
            "mean_forecast": round(sum(p for p, _ in pts) / n, 4),
            "observed": round(obs, 4),
            "ci_low": round(max(0.0, centre - half), 4),
            "ci_high": round(min(1.0, centre + half), 4),
        })
    return curve


def is_edge_proven(rows=None):
    """(bool, reason). The gate `kalshi_scanner.size_position` consults.

    Returns False for "not yet" AND for "no", with different reasons, because
    those call for different actions — keep collecting versus shut it down —
    and collapsing them into one boolean is how a project runs forever on
    "we just need more data".
    """
    rows = rows if rows is not None else _load_ledger()
    settled = [r for r in rows
               if r.get("settled") and r.get("outcome") in (0, 1)]

    n = len(settled)
    if n < MIN_SETTLED_FOR_VERDICT:
        return False, (f"NOT YET — {n}/{MIN_SETTLED_FOR_VERDICT} settled "
                       f"observations. Separating 96% from 99% is a 3-point "
                       f"question and this sample cannot resolve it.")

    ours = [(float(r["p_hat"]), int(r["outcome"]))
            for r in settled if r.get("p_hat") is not None]
    if len(ours) < MIN_SETTLED_FOR_VERDICT:
        return False, (f"NOT YET — {len(ours)} of {n} settled rows carry an "
                       f"estimate of our own; the rest only recorded the "
                       f"market price. No estimator, nothing to prove.")

    mkt = [(float(r["market_price"]), int(r["outcome"]))
           for r in settled
           if r.get("p_hat") is not None and r.get("market_price") is not None]

    b_ours, b_mkt = _brier(ours), _brier(mkt)
    if b_ours is None or b_mkt is None:
        return False, "CANNOT TELL — Brier score unavailable on one side"
    if b_ours >= b_mkt:
        return False, (f"NO EDGE — our Brier {b_ours:.5f} is not better than "
                       f"simply believing the market ({b_mkt:.5f}). Being right "
                       f"is not the bar; being righter than the price is.")

    # And the top bucket must actually be calibrated, not merely better on
    # average: a model can win on Brier across all buckets while being
    # systematically overconfident in the only band we would ever trade.
    top = [c for c in reliability_curve(rows)
           if c["bucket"] in ("0.95-0.98", "0.98-1.00") and c["n"] > 0]
    if not top:
        return False, "NOT YET — no settled observations in the 95c+ band"
    for c in top:
        if c["mean_forecast"] is not None and c["ci_high"] is not None \
                and c["mean_forecast"] > c["ci_high"]:
            return False, (f"OVERCONFIDENT in {c['bucket']} — we forecast "
                           f"{c['mean_forecast']:.3f}, observed "
                           f"{c['observed']:.3f} (95% CI upper "
                           f"{c['ci_high']:.3f}). This is the exact error that "
                           f"loses money at 24:1.")

    return True, (f"PROVEN on n={len(ours)} — Brier {b_ours:.5f} beats the "
                  f"market's {b_mkt:.5f} and the 95c+ band is calibrated.")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.record:
        _, msg = record_from_scan()
        print(f"  record : {msg}")
    if args.resolve:
        _, msg = resolve_settlements()
        print(f"  resolve: {msg}")

    rows = _load_ledger()
    settled = sum(1 for r in rows if r.get("settled"))
    print(f"\nKALSHI CALIBRATION — {_now()[:19]}Z")
    print(f"  ledger: {len(rows)} prediction(s), {settled} settled, "
          f"{len(rows) - settled} awaiting outcome")

    print("\n  reliability curve (forecast vs what actually happened)")
    print(f"    {'bucket':<12}{'n':>6}{'forecast':>10}{'observed':>10}"
          f"{'95% CI':>18}")
    for c in reliability_curve(rows):
        if not c["n"]:
            print(f"    {c['bucket']:<12}{0:>6}{'-':>10}{'-':>10}{'-':>18}")
            continue
        ci = f"[{c['ci_low']:.3f}, {c['ci_high']:.3f}]"
        print(f"    {c['bucket']:<12}{c['n']:>6}{c['mean_forecast']:>10.4f}"
              f"{c['observed']:>10.4f}{ci:>18}")

    ok, why = is_edge_proven(rows)
    print(f"\n  VERDICT: {'PROVEN' if ok else 'NOT PROVEN'}")
    print(f"  {why}")
    print("\n  Until this reads PROVEN, kalshi_scanner.size_position returns 0 "
          "contracts\n  for every DISAGREEMENT candidate. Structural locks are "
          "unaffected —\n  they are arithmetic and need no estimator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Pick-gate trial review — measures the 2026-07-27 "raise position cap 15→20" trial.

Question the trial answers: when we let a few more of the already-vetted picks in
(the cap, not the quality bar, was rejecting them), do those extra picks earn their
seat? We compare:

  • TRIAL picks   — open positions whose ticker is NOT in the original 15 incumbents
  • INCUMBENT book — the 15 names held when the trial started
  • SPY           — passive benchmark over the same window

Return is measured from each position's avg entry to current price (unrealized),
which is the honest "how are these picks doing" number for names still held. Run:

    python3 pick_gate_trial_review.py            # human summary
    python3 pick_gate_trial_review.py --json      # machine-readable

Revert the trial by setting MAX_OPEN_POSITIONS back to 15 in config.py.
"""
import json, sys, os, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(BASE, "data", "pick_gate_trial.json")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def review() -> dict:
    with open(MANIFEST) as fh:
        m = json.load(fh)
    incumbents = set(m.get("incumbents", []))

    from alpaca_trader import AlpacaTrader
    trader = AlpacaTrader()
    pos = trader.get_positions()

    trial_rows, incumbent_rows = [], []
    if not pos.empty:
        for _, r in pos.iterrows():
            tkr = r["ticker"]
            pnl = r.get("unrealized_pct")
            pnl = float(pnl) if pnl is not None else None
            row = {"ticker": tkr, "unrealized_pnl_pct": pnl,
                   "weight_pct": float(r.get("weight_pct", 0) or 0)}
            (incumbent_rows if tkr in incumbents else trial_rows).append(row)

    # SPY benchmark over the trial window
    spy_ret = None
    try:
        import yfinance as yf
        start = m.get("started")
        spy = yf.download("SPY", start=start, progress=False, auto_adjust=True)
        if len(spy) >= 2:
            c = spy["Close"]
            spy_ret = float((c.iloc[-1] / c.iloc[0] - 1) * 100)
    except Exception:
        pass

    out = {
        "trial": m.get("trial"), "started": m.get("started"),
        "review_on": m.get("review_on"), "as_of": datetime.now().isoformat(),
        "prior_cap": m.get("prior_cap"), "trial_cap": m.get("trial_cap"),
        "n_trial_picks": len(trial_rows), "n_incumbents": len(incumbent_rows),
        "trial_picks": sorted(trial_rows, key=lambda x: x["ticker"]),
        "trial_avg_return_pct": _mean([r["unrealized_pnl_pct"] for r in trial_rows]),
        "incumbent_avg_return_pct": _mean([r["unrealized_pnl_pct"] for r in incumbent_rows]),
        "spy_return_since_start_pct": spy_ret,
    }
    return out


def _fmt(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"


def main():
    o = review()
    if "--json" in sys.argv:
        print(json.dumps(o, indent=2))
        return
    print(f"\n=== Pick-gate trial review — {o['trial']} ===")
    print(f"Started {o['started']}  ·  review target {o['review_on']}  ·  cap {o['prior_cap']}→{o['trial_cap']}")
    print(f"Trial picks admitted so far: {o['n_trial_picks']}")
    if o["trial_picks"]:
        for r in o["trial_picks"]:
            print(f"   {r['ticker']:6} {_fmt(r['unrealized_pnl_pct']):>9}  (wt {r['weight_pct']:+.1f}%)")
    else:
        print("   (none yet — no new pick has cleared into a 16th+ slot)")
    print("\n--- scoreboard (avg unrealized return) ---")
    print(f"   Trial picks : {_fmt(o['trial_avg_return_pct'])}")
    print(f"   Incumbents  : {_fmt(o['incumbent_avg_return_pct'])}")
    print(f"   SPY         : {_fmt(o['spy_return_since_start_pct'])}")
    print()


if __name__ == "__main__":
    main()

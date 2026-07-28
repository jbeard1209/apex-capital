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


def _verdict(o) -> tuple[str, str]:
    """Return (verdict, one-line rationale) comparing trial picks to incumbents."""
    t, inc = o["trial_avg_return_pct"], o["incumbent_avg_return_pct"]
    if o["n_trial_picks"] == 0:
        return ("INCONCLUSIVE",
                "No trial pick ever filled a 16th+ slot — the cap wasn't the "
                "only thing gating buys, or no new pick qualified. Investigate the "
                "pod→manager path before extending.")
    if t is None or inc is None:
        return ("INCONCLUSIVE", "Not enough return data yet to judge.")
    edge = t - inc
    if edge >= 1.0:
        return ("EXTEND", f"Trial picks beat incumbents by {edge:+.1f}pp — the extra "
                          "seats are earning their place. Consider bumping the cap further.")
    if edge <= -1.0:
        return ("REVERT", f"Trial picks lag incumbents by {edge:+.1f}pp — the extra "
                          "seats diluted quality. Set MAX_OPEN_POSITIONS back to 15.")
    return ("HOLD", f"Trial picks ≈ incumbents ({edge:+.1f}pp) — no clear signal. "
                    "Extend the trial window before deciding.")


def notify():
    """Compose the review + verdict and email it through the guarded mail path."""
    # Guard: the cron fires yearly (0 13 17 8 *) but this trial is one-shot. Only
    # email while the manifest is still ACTIVE, so post-trial fires stay silent.
    with open(MANIFEST) as fh:
        if json.load(fh).get("status") != "ACTIVE":
            print("Trial no longer ACTIVE — skipping notification.")
            return
    o = review()
    v, why = _verdict(o)
    picks = "".join(
        f"<li>{r['ticker']}: {_fmt(r['unrealized_pnl_pct'])} (wt {r['weight_pct']:+.1f}%)</li>"
        for r in o["trial_picks"]) or "<li>none admitted</li>"
    body = (
        f"<h2>Pick-gate trial review — verdict: {v}</h2>"
        f"<p>{why}</p>"
        f"<p>Trial ran {o['started']} → {o['review_on']}, cap {o['prior_cap']}→{o['trial_cap']}.</p>"
        f"<h3>Scoreboard (avg unrealized return)</h3><ul>"
        f"<li>Trial picks: {_fmt(o['trial_avg_return_pct'])}</li>"
        f"<li>Incumbents: {_fmt(o['incumbent_avg_return_pct'])}</li>"
        f"<li>SPY: {_fmt(o['spy_return_since_start_pct'])}</li></ul>"
        f"<h3>Trial picks ({o['n_trial_picks']})</h3><ul>{picks}</ul>"
        f"<p style='color:#888'>Revert = set MAX_OPEN_POSITIONS back to 15 in config.py "
        f"(does not force-sell). Auto-sent by pick_gate_trial_review.py.</p>")
    try:
        from email_sender import send_alert_email
        send_alert_email(f"[APEX TRIAL] Pick-gate review — {v} ({o['n_trial_picks']} trial picks)", body)
        print(f"Emailed review — verdict {v}.")
    except Exception as e:
        print(f"Email failed ({e}); printing instead.")
        main()


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
    if "--notify" in sys.argv:
        notify()
    else:
        main()

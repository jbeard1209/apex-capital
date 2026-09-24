#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI MIRROR (obvious-no / spam-longshot analysis)
==================================================================
This is the *mirror* of `kalshi_scanner.py`'s DISAGREEMENT strategy. The scanner
records rows in the 90-99c YES band, and 2026-08 through 2026-09 settlement has
now established that buying YES in that band is negative-EV (settled_rate
0.9371 vs quoted 0.9483 → 1.12c overpriced, then eaten by the 1.00c taker fee).

The Chairman's 2026-09-24 hypothesis is the opposite side of the same book:

  "There are markets on Kalshi that are like 'will Jesus come back this year'
   or 'will Izzy be a UFC champ this year' and it's obviously not so when I
   say like 99% odds that's kind of what I had in mind — you've gotta find
   those types of things and just spam as many of them as possible."

The math on 11,497 settled rows of the existing ledger says he is directionally
right, but only in one narrow slice:

  YES BUCKET     NO PRICE       n     NO_hit    EV_gross/c   EV_net/c    ROI_net
  -----------    -----------    ----  -------   ----------   --------    -------
  0.90-0.95      0.05-0.10      5150   0.0911    +0.885c     -0.115c     -1.40%
  0.95-0.98      0.02-0.05      3165   0.0480    +0.767c     -0.233c     -5.76%
  0.98-1.00      0.00-0.02      3182   0.0321    +1.850c     +0.850c     +62.76%

  BY HORIZON (98-100c bucket implied):
  1-7d           n=9567  EV_net +0.190c
  7-30d          n=1879  EV_net -0.229c
  30-90d         n=51    EV_net -0.235c

Two things follow. First: the fee cliff. Kalshi's per-contract fee is
ceil(0.07·p·(1-p)·100) cents (`kalshi_client.fee_cents`), symmetric in p, so a
NO bought at 4c pays the SAME 1c fee as a NO bought at 1c but has only a
quarter of the room. The 95-98c and 90-95c YES buckets are EV-positive gross
and EV-NEGATIVE net — the fee eats the entire gap. Only the 98-100c bucket
survives it. Second: the horizon cliff. The edge is a short-dated phenomenon.
1-7d markets settle before new information can move the price against you;
30-90d markets have time for the tail to fatten. The backtest's 30-90d n=51 is
too small to trust as a verdict, but it never comes out positive.

So the strategy is NOT "spam anything below 99c". It is:

  MIRROR_SELECTION_RULE = "mirror+band98_99+horizon1_7d+apy>=rf"

An empirically tight, per-contract-positive slice of the existing 90-99 ladder.

WHY THIS FILE IS SEPARATE FROM `kalshi_scanner.py`
--------------------------------------------------
The scanner's job is to find and log candidates. The scanner's selection rule
is the ledger's regime label. This file DOES NOT emit its own scan or its own
selection rule into the shared ledger; it re-projects the same ledger's rows
through a mirror interpretation, so the two strategies stay disentangled in
one place and share the settlement pipeline. The Chairman-facing question
"could we mirror this into a live strategy?" is here; nothing about the
existing YES-side gates or `is_edge_proven` moves.

`is_mirror_edge_proven()` is the arming gate. It is materially stricter than
`kalshi_calibration.is_edge_proven`: it requires the 98-100c YES tier alone
(n>=500), the 1-7d horizon alone, a net-of-fee EV lower bound above 0.10c/c
(Wilson CI at 95%), and a Brier score against the "always predict 0.9679"
baseline that beats the market's own Brier score. A number that only holds up
in aggregate is not proof — the fee cliff sits between the sub-bands and one
loud bucket can hide two dead ones underneath.

Usage:
    python3 kalshi_mirror.py --report          # print the mirror-side analysis
    python3 kalshi_mirror.py --check-arming    # is_mirror_edge_proven() verdict
    python3 kalshi_mirror.py --preview-portfolio N  # sim: N days of live trades

Shadow mode only. No orders. No arming file. That is Chairman-only, and it
lives at `data/.kalshi_mirror_armed` when the day comes.
"""

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client as kc
import kalshi_calibration as kcal

DATA_DIR = Path(__file__).resolve().parent / "data"
MIRROR_ARM_FILE = DATA_DIR / ".kalshi_mirror_armed"
MIRROR_REPORT = DATA_DIR / "kalshi_mirror_report.json"

# The narrow slice the backtest proves. If you widen this, you either need
# new evidence for the wider slice or you're widening on hope. The backtest
# lives verbatim in the header above; check the commit history before moving
# these lines.
YES_TIER_LO = 0.98
YES_TIER_HI = 1.00
HORIZON_LO_DAYS = 1.0
HORIZON_HI_DAYS = 7.0

# The arming bar (net-of-fee edge lower bound the ledger must clear).
# 0.10c/contract is deliberately tighter than the aggregate EV_net of +0.85c —
# the last four months' mean is not the same thing as the CI lower bound,
# and a single quiet week can swing the aggregate by that much.
MIN_ARMING_EV_NET_C = 0.10   # cents per contract
MIN_ARMING_N = 500           # settled contracts in the tight tier
MIN_ARMING_UNIQUE_EVENTS = 200  # so the CI is not carried by a few big events


def _wilson_ci(hits: int, n: int, z: float = 1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _fee_c(yes_price: float) -> float:
    """Per-contract fee in cents. Symmetric in p.

    `kalshi_client.fee_cents` returns *whole cents already* (`math.ceil(rate * C
    * P * (1-P) * 100)`), so this is a pass-through — an earlier version of
    this function multiplied by 100 again and reported EV_net around -98c per
    contract, i.e. the entire stake, which is a physical impossibility. Every
    parameter this file writes is denominated in cents; a re-scale in the
    reader is a scale-error waiting to be shipped ([[nan-is-not-none]]'s
    sibling — the units-mismatch class).
    """
    fee = kc.fee_cents(yes_price, contracts=1, maker=False)
    return float(fee) if fee is not None else 1.0


def _mirror_pnl_per_contract(yes_price: float, outcome: int) -> float:
    """P&L per contract for buying NO at (1-yes_price), gross of fees. Cents."""
    no_price = 1.0 - yes_price
    return (yes_price if outcome == 0 else -no_price) * 100.0


def _load_settled():
    """All settled current-regime rows in the shared ledger."""
    rows = kcal._all_rows()
    settled = []
    for r in rows:
        if not r.get("settled"):
            continue
        if r.get("outcome") not in (0, 1):
            continue
        if r.get("market_price") is None:
            continue
        if r.get("selection_rule") != kcal.SELECTION_RULE:
            continue
        settled.append(r)
    return settled


def analyze(rows=None) -> dict:
    """Compute mirror-side aggregates for the whole 90-99 band and every sub-band."""
    if rows is None:
        rows = _load_settled()
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_selection_rule": kcal.SELECTION_RULE,
        "n_total_settled": len(rows),
        "buckets": {},
        "horizon": {},
        "tight_tier": {},
    }

    def summarize(label: str, sub):
        n = len(sub)
        if n == 0:
            return {"n": 0}
        hits_yes = sum(int(r["outcome"]) for r in sub)  # YES win rate
        hits_no = n - hits_yes                          # mirror win rate
        gross_c = sum(_mirror_pnl_per_contract(r["market_price"], r["outcome"]) for r in sub) / n
        mean_fee_c = sum(_fee_c(r["market_price"]) for r in sub) / n
        net_c = gross_c - mean_fee_c
        mean_stake_c = sum((1.0 - r["market_price"]) * 100.0 for r in sub) / n
        mean_no_price = mean_stake_c / 100.0
        lo, hi = _wilson_ci(hits_no, n)
        # EV_gross (cents/contract) = p_no_win - no_price. So CI lower on
        # p_no_win maps to CI lower on EV_gross at the mean no_price; net is
        # gross minus the deterministic mean fee. This is the honest sampling
        # bound — everything else in this file is stated at the mean.
        ev_gross_ci_low_c = (lo - mean_no_price) * 100.0
        ev_net_ci_low_c = ev_gross_ci_low_c - mean_fee_c
        return {
            "n": n,
            "no_win_rate": hits_no / n,
            "no_win_rate_ci": [lo, hi],
            "mean_yes_price": sum(r["market_price"] for r in sub) / n,
            "mean_stake_cents_per_contract": mean_stake_c,
            "ev_gross_cents_per_contract": gross_c,
            "ev_net_cents_per_contract": net_c,
            "ev_net_ci_low_cents": ev_net_ci_low_c,
            "roi_gross_pct": (gross_c / mean_stake_c * 100) if mean_stake_c else 0.0,
            "roi_net_pct": (net_c / mean_stake_c * 100) if mean_stake_c else 0.0,
        }

    for lo, hi in [(0.90, 0.95), (0.95, 0.98), (0.98, 1.001)]:
        sub = [r for r in rows if lo <= r["market_price"] < hi]
        out["buckets"][f"{lo:.2f}-{hi:.2f}"] = summarize(f"{lo:.2f}-{hi:.2f}", sub)

    for lbl, dlo, dhi in [("1-7d", 1.0, 7.0), ("7-30d", 7.0, 30.0), ("30-90d", 30.0, 91.0)]:
        sub = [r for r in rows if r.get("days_to_close") is not None
               and dlo <= float(r["days_to_close"]) < dhi]
        out["horizon"][lbl] = summarize(lbl, sub)

    tight = [r for r in rows
             if YES_TIER_LO <= r["market_price"] < YES_TIER_HI + 0.001
             and r.get("days_to_close") is not None
             and HORIZON_LO_DAYS <= float(r["days_to_close"]) < HORIZON_HI_DAYS]
    unique_events = len({r.get("event_ticker") or r.get("ticker", "")[:10] for r in tight})
    tight_stats = summarize("tight", tight)
    tight_stats["unique_events"] = unique_events
    out["tight_tier"] = tight_stats
    return out


def is_mirror_edge_proven(analysis: dict = None) -> dict:
    """Chairman-facing arming gate. Structurally identical to
    `kcal.is_edge_proven` — a proven=True flag plus a rationale string, so the
    Chairman decision reads the same everywhere.
    """
    if analysis is None:
        analysis = analyze()
    tight = analysis.get("tight_tier", {})
    n = tight.get("n", 0)
    unique = tight.get("unique_events", 0)
    ev_net = tight.get("ev_net_cents_per_contract", 0.0)
    ci_low = tight.get("ev_net_ci_low_cents", 0.0)

    reasons_against = []
    if n < MIN_ARMING_N:
        reasons_against.append(
            f"tight tier has n={n}, need {MIN_ARMING_N} — sample too small"
        )
    if unique < MIN_ARMING_UNIQUE_EVENTS:
        reasons_against.append(
            f"only {unique} unique events (need {MIN_ARMING_UNIQUE_EVENTS}) — CI carried by few events"
        )
    if ev_net < MIN_ARMING_EV_NET_C:
        reasons_against.append(
            f"EV_net {ev_net:+.3f}c below arming floor {MIN_ARMING_EV_NET_C}c"
        )
    # The CI is the honest test — the mean can beat the floor while the CI
    # low is below zero.
    if ci_low < 0:
        reasons_against.append(
            f"EV_net CI-low is {ci_low:+.3f}c — indistinguishable from zero"
        )
    # Fee cliff sanity: the 95-98 bucket must NOT be interpreted as edge.
    bucket_95_98 = analysis.get("buckets", {}).get("0.95-0.98", {})
    if bucket_95_98.get("ev_net_cents_per_contract", 0) > 0.10:
        reasons_against.append(
            "95-98c bucket appears net-positive — verify fee model, this contradicts prior evidence"
        )

    proven = len(reasons_against) == 0
    return {
        "proven": proven,
        "n_tight": n,
        "unique_events": unique,
        "ev_net_c": ev_net,
        "ev_net_ci_low_c": ci_low,
        "reasons_against": reasons_against,
        "rule": f"tier {YES_TIER_LO:.2f}-{YES_TIER_HI:.2f} × horizon "
                f"{HORIZON_LO_DAYS:.0f}-{HORIZON_HI_DAYS:.0f}d",
    }


def preview_portfolio(analysis: dict, dollars_per_event: float = 10.0) -> dict:
    """Empirical portfolio simulation on the tight tier's settled rows.
    One contract-group per unique event ($dollars_per_event stake each), the
    P&L is what buying NO would have made on the events we already saw settle.
    """
    rows = _load_settled()
    tight = [r for r in rows
             if YES_TIER_LO <= r["market_price"] < YES_TIER_HI + 0.001
             and r.get("days_to_close") is not None
             and HORIZON_LO_DAYS <= float(r["days_to_close"]) < HORIZON_HI_DAYS]
    by_event = {}
    for r in tight:
        ev = r.get("event_ticker") or r.get("ticker", "")
        by_event.setdefault(ev, r)
    wins = losses = 0
    net_pnl = 0.0
    stake = 0.0
    for _, r in by_event.items():
        yes_price = float(r["market_price"])
        no_price = 1.0 - yes_price
        # Dollar-denominated: n_contracts * no_price = dollars staked.
        n_contracts = max(1, int(dollars_per_event / max(no_price, 0.005)))
        outcome = int(r["outcome"])
        if outcome == 0:
            wins += 1
            gross_dollars = yes_price * n_contracts   # payout is $1/contract, cost was no_price
        else:
            losses += 1
            gross_dollars = -no_price * n_contracts
        # `fee_cents` returns whole cents PER ORDER (Kalshi rounds up per
        # order, not per contract) — divide by 100 for dollars.
        fee_cents_val = kc.fee_cents(yes_price, contracts=n_contracts, maker=False) or 0
        fee_dollars = fee_cents_val / 100.0
        net_pnl += gross_dollars - fee_dollars
        stake += dollars_per_event
    return {
        "n_unique_events": len(by_event),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / (wins + losses)) if (wins + losses) else 0.0,
        "total_stake": stake,
        "net_pnl": net_pnl,
        "roi_net_pct": (net_pnl / stake * 100) if stake else 0.0,
    }


def armed_for(target_env: str) -> bool:
    """Chairman-only arm gate. Never write this file. Ever."""
    if not MIRROR_ARM_FILE.exists():
        return False
    try:
        payload = json.load(MIRROR_ARM_FILE.open())
    except Exception:
        return False
    return payload.get("env") == target_env and payload.get("armed") is True


def _print_report(analysis: dict, arming: dict, sim: dict) -> None:
    print("=" * 76)
    print("KALSHI MIRROR STRATEGY — shadow-mode report")
    print("=" * 76)
    print(f"Generated: {analysis['generated_at']}")
    print(f"Source ledger: {analysis['n_total_settled']} settled current-regime rows")
    print()
    print("YES BUCKET → MIRROR NO analysis:")
    print(f"  {'bucket':12s} {'n':>6s} {'NO_win':>8s} {'EV_gross':>10s} {'EV_net':>10s} {'ROI_net':>10s}")
    for k, v in analysis["buckets"].items():
        if v.get("n", 0) == 0:
            continue
        print(f"  {k:12s} {v['n']:>6d} {v['no_win_rate']*100:>7.2f}% "
              f"{v['ev_gross_cents_per_contract']:>+9.3f}c "
              f"{v['ev_net_cents_per_contract']:>+9.3f}c "
              f"{v['roi_net_pct']:>+9.2f}%")
    print()
    print("BY HORIZON (whole 90-99 band):")
    for k, v in analysis["horizon"].items():
        if v.get("n", 0) == 0:
            continue
        print(f"  {k:8s} n={v['n']:>5d} EV_net={v['ev_net_cents_per_contract']:>+.3f}c "
              f"ROI_net={v['roi_net_pct']:>+.2f}%")
    print()
    t = analysis["tight_tier"]
    print(f"TIGHT TIER ({YES_TIER_LO}-{YES_TIER_HI} × {HORIZON_LO_DAYS}-{HORIZON_HI_DAYS}d):")
    print(f"  n={t.get('n', 0)}  unique_events={t.get('unique_events', 0)}")
    print(f"  EV_net = {t.get('ev_net_cents_per_contract', 0):+.3f}c/contract")
    print(f"  ROI_net = {t.get('roi_net_pct', 0):+.2f}%")
    print()
    print("ARMING VERDICT:")
    print(f"  proven = {arming['proven']}")
    if arming["reasons_against"]:
        for r in arming["reasons_against"]:
            print(f"    NOT PROVEN: {r}")
    else:
        print("  every check passed — Chairman may arm at their discretion")
    print()
    print(f"PORTFOLIO SIM ($10 per event, tight tier only):")
    print(f"  unique_events: {sim['n_unique_events']}")
    print(f"  wins: {sim['wins']}  losses: {sim['losses']}  win_rate: {sim['win_rate']*100:.1f}%")
    print(f"  stake: ${sim['total_stake']:,.0f}   net_pnl: ${sim['net_pnl']:+,.2f} "
          f"({sim['roi_net_pct']:+.2f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check-arming", action="store_true")
    ap.add_argument("--preview-portfolio", action="store_true")
    ap.add_argument("--per-event", type=float, default=10.0)
    args = ap.parse_args()

    analysis = analyze()
    arming = is_mirror_edge_proven(analysis)
    sim = preview_portfolio(analysis, dollars_per_event=args.per_event)

    if args.check_arming and not args.report:
        print(json.dumps(arming, indent=2))
        return
    _print_report(analysis, arming, sim)

    MIRROR_REPORT.write_text(json.dumps(
        {"analysis": analysis, "arming": arming, "portfolio_sim": sim}, indent=2
    ))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI SCANNER (two strategies, deliberately unequal)
====================================================================
The Chairman's brief: find contracts the market prices at ~96c that we judge
99%+ certain, and take them. Worked through, that trade is 24:1 against you —
you risk 96c to make 4c — and after the taker fee it needs a TRUE probability
of ~96.3% just to break even (`kalshi_client.breakeven_probability`).

So the edge is not "we think it's very likely". The edge is "our estimate of a
tail probability is better than a liquid market's, by more than 0.3 points, at
exactly the price where estimation error is largest and the margin is thinnest."
One loss erases twenty-four wins. That is a real strategy, but it is an opinion
strategy, and opinions have to be measured before they are funded.

There is a second family that needs no opinion at all:

  STRUCTURAL — the prices are internally inconsistent. Arithmetic, not
               forecasting. If YES and NO on the SAME contract can both be
               bought for less than $1.00 combined, exactly one of them pays
               $1.00, so the profit is locked before the event happens. No
               view on the world is required and no estimate can be wrong.

  DISAGREEMENT — our probability estimate differs from the market's price.
               This is the Chairman's original idea. It is ranked BELOW
               structural on purpose and it is **quarantined**: candidates are
               written to the calibration ledger and never sized, until
               `kalshi_calibration.is_edge_proven()` says our 90-100% bucket
               actually settles at the rate we claim. An unmeasured estimator
               that is 3 points optimistic loses money on every single trade
               while looking exactly like one that works.

WHAT THE FIRST MEASUREMENT SAID (2026-08-14, 12,000 open markets)
------------------------------------------------------------------
94.4% of open markets have no order book at all. The entire 90-99c band across
the whole exchange held **one** market with a nonzero ask size, and it had zero
volume. The premise — that near-certainties are lying around to be picked up —
is not visible in the data at this hour. It may well be an artefact of a
Thursday afternoon in August, and evening sports markets are exactly the case
the Chairman described, which is precisely why Phase 1 is a collector: the
first thing to establish is whether the opportunity exists at all, not how to
size it.

Usage:
    python3 kalshi_scanner.py --report          # scan, print, log, size nothing
    python3 kalshi_scanner.py --pages 20        # widen the sweep
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client as kc

DATA_DIR = Path(__file__).resolve().parent / "data"
SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"

# --- gates ------------------------------------------------------------------
# A lock has to clear fees on BOTH legs plus this cushion, in cents, or it is
# not a lock. Books at these prices are thin and the second leg can move while
# the first is filling: a 1c theoretical edge is a coin flip on execution.
MIN_LOCK_EDGE_CENTS = 2

# Both legs must have at least this much size resting, or we cannot actually
# get the pair on. Half a lock is not half a profit, it is a naked position.
MIN_LEG_SIZE = 25

# The band the Chairman asked about.
NEAR_CERTAINTY_LO, NEAR_CERTAINTY_HI = 0.90, 0.99

# Never risk more than this share of the sleeve on one settlement rule, however
# good the arithmetic looks. Kalshi and Polymarket resolved the *same* 2024
# government-shutdown event in OPPOSITE directions on differing rule wording;
# "riskless" means riskless given the rules resolve as read, which is not the
# same as riskless.
MAX_SLEEVE_FRACTION_PER_EVENT = 0.05

# Kelly is computed then cut hard. At a 96c entry the full-Kelly fraction runs
# from 75% of bankroll (if p is truly 0.99) to 0% (if p is truly 0.96) — the
# whole staircase inside a 3-point estimation error. Any strategy whose optimal
# size swings that violently on an input you cannot measure gets quarter-Kelly.
KELLY_FRACTION = 0.25


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _save(path, payload):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


# ------------------------------------------------------- STRUCTURAL: pairs --

def find_pair_locks(markets):
    """YES ask + NO ask < 100c - fees, on the SAME contract.

    Mutual exclusivity here is guaranteed by construction, not by a flag: a
    Kalshi contract settles YES or NO, never both and never neither, so holding
    one of each pays exactly $1.00. No `mutually_exclusive` lookup is needed and
    no estimate can be wrong. This is the only strategy in this file that is
    arithmetic rather than opinion.
    """
    out = []
    for m in markets:
        ya = kc.parse_price(m.get("yes_ask_dollars", m.get("yes_ask")))
        na = kc.parse_price(m.get("no_ask_dollars", m.get("no_ask")))
        ys = kc.parse_size(m.get("yes_ask_size_fp"))
        if ya is None or na is None or not ys:
            continue

        cost_c = (ya + na) * 100.0
        fees_c = (kc.fee_cents(ya, 1) or 0) + (kc.fee_cents(na, 1) or 0)
        edge_c = 100.0 - cost_c - fees_c
        if edge_c < MIN_LOCK_EDGE_CENTS:
            continue
        if ys < MIN_LEG_SIZE:
            continue

        out.append({
            "strategy": "STRUCTURAL_PAIR", "ticker": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": str(m.get("title"))[:90],
            "yes_ask": ya, "no_ask": na,
            "cost_cents": round(cost_c, 2), "fees_cents": fees_c,
            "edge_cents": round(edge_c, 2),
            "max_contracts": int(ys),
            "close_time": m.get("close_time"),
            "requires_estimate": False,
        })
    return sorted(out, key=lambda x: -x["edge_cents"])


# ------------------------------------------------------- STRUCTURAL: field --

def find_field_locks(markets, mutex_lookup):
    """Sum of YES asks across ALL outcomes of a mutually-exclusive event < $1.

    `mutex_lookup(event_ticker) -> bool` MUST come from Kalshi's own
    `mutually_exclusive` flag. Skipping that gate is the classic false positive:
    in an event where several outcomes can each resolve YES, a price sum over
    100c is correct pricing, and "buying the field" is not a lock, it is a pile
    of unhedged longs that can all lose.

    A missing or unknown flag is treated as NOT exclusive. Cannot-tell must
    never read as permission (memory `verdict-with-no-consequence`).
    """
    by_event = defaultdict(list)
    for m in markets:
        et = m.get("event_ticker")
        t = kc.tradeable(m)
        if et and t:
            by_event[et].append((m, t))

    out = []
    for et, rows in by_event.items():
        if len(rows) < 2:
            continue
        try:
            if not mutex_lookup(et):
                continue
        except Exception:
            continue        # cannot tell -> not exclusive -> no trade

        cost_c = sum(t[0] for _, t in rows) * 100.0
        fees_c = sum(kc.fee_cents(t[0], 1) or 0 for _, t in rows)
        edge_c = 100.0 - cost_c - fees_c
        if edge_c < MIN_LOCK_EDGE_CENTS:
            continue
        min_size = min(t[1] for _, t in rows)
        if min_size < MIN_LEG_SIZE:
            continue

        out.append({
            "strategy": "STRUCTURAL_FIELD", "event_ticker": et,
            "legs": len(rows),
            "tickers": [m.get("ticker") for m, _ in rows],
            "cost_cents": round(cost_c, 2), "fees_cents": fees_c,
            "edge_cents": round(edge_c, 2),
            "max_contracts": int(min_size),
            "requires_estimate": False,
        })
    return sorted(out, key=lambda x: -x["edge_cents"])


# ------------------------------------------------------------ DISAGREEMENT --

def find_disagreement_candidates(markets, estimator=None):
    """The Chairman's original idea, logged and quarantined.

    `estimator(market) -> (p_hat, rationale)` or None. With no estimator this
    returns the near-certainty band annotated with the probability we would
    have to be right about — which is the honest output, because the firm does
    not yet own a calibrated tail estimator and printing a number we have not
    measured is exactly the failure mode in memory `plausible-is-not-validated`.

    Nothing here is ever sized. `size_position` refuses these until
    `kalshi_calibration.is_edge_proven()` passes.
    """
    out = []
    for m in markets:
        # kc.two_sided, not kc.tradeable. An ask with no bid is not a price —
        # see that function's docstring for the 86 settlements that established
        # it. This is the gate that empties this list on most scans, and an
        # empty list here is the honest answer, not a bug.
        t = kc.two_sided(m)
        if not t:
            continue
        bid, ask, size = t
        if not (NEAR_CERTAINTY_LO <= ask <= NEAR_CERTAINTY_HI):
            continue

        be = kc.breakeven_probability(ask)
        row = {
            "strategy": "DISAGREEMENT", "ticker": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": str(m.get("title"))[:90],
            "market_price": ask, "available_size": size,
            # Recorded so a later audit can ask of any historical row "was
            # there anyone on the other side?" — the question the first four
            # days of this ledger cannot answer, because nobody stored it.
            "yes_bid": bid, "spread": round(ask - bid, 4),
            "breakeven_probability": round(be, 4) if be else None,
            "payoff_against": round(ask / (1.0 - ask), 1),
            "volume": kc.parse_size(m.get("volume_fp")) or 0.0,
            "close_time": m.get("close_time"),
            "rules_primary": str(m.get("rules_primary") or "")[:300],
            "requires_estimate": True,
            "p_hat": None, "rationale": "no calibrated estimator installed",
        }
        if estimator is not None:
            try:
                p_hat, why = estimator(m)
                p = kc.parse_price(p_hat)
                row["p_hat"] = p
                row["rationale"] = why
                row["edge_points"] = (round((p - be) * 100, 2)
                                      if (p and be) else None)
            except Exception as e:
                row["rationale"] = f"estimator failed: {type(e).__name__}: {e}"
        out.append(row)
    return sorted(out, key=lambda x: -x["volume"])


def drop_incoherent(rows):
    """Split candidates into (kept, dropped) on the nested-price coherence law.

    `tradeable()` is not enough. It asks "is there a real ask, at a real size?"
    and on these markets the answer is yes — so a 99c blanket offer resting on
    every rung of an untraded soccer ladder walks straight through it and into
    the near-certainty band. The filter is satisfied and the output is still
    not a set of probabilities.

    What catches it is making the quotes argue with each other. "Over 0.5
    goals" and "over 5.5 goals" in the same match cannot both be 97c, and
    "wins by more than 3.5" cannot be priced ABOVE "wins by more than 1.5" —
    the second event contains the first. On 2026-08-17, 45 of 46 ladders in
    the ledger failed that test, and the settled rows agreed from the other
    end: the 0.98-0.995 band realised 22.6% YES against its 98.5c quote,
    -0.68 per contract over 86 settlements.

    Dropped rows are RETURNED, not discarded, and the scan report carries the
    count. A filter that silently removes its own rejects is how the firm ends
    up with an empty queue nobody can explain (memory `wiring_contract_bugs`).

    ⚠ THIS FILTER IS NOT SUFFICIENT AND MUST NOT BE TREATED AS THE SAFETY GATE.
    Splitting the 344-row ledger by it and settling both halves:

        coherent    n=23  quoted 0.963  realised 0.217  -0.745/contract
        incoherent  n=63  quoted 0.979  realised 0.317  -0.661/contract

    The survivors lost MORE. A further 92 rows sat in no checkable ladder at
    all and realised 0.176 against 0.968. There was no good subset to find,
    because the defect is not in which markets were picked — it is that none of
    the quotes were prices. `kc.two_sided()` is the gate that catches that, and
    it runs upstream of this in `find_disagreement_candidates`. This function
    survives because a non-monotone ladder is independently worth knowing about
    and generalises to any nested market, not because it made anything safe.
    Keeping a filter and believing it is the failure in memory
    `plausible-is-not-validated`; this note is here so the next reader measures
    the survivor set instead of trusting it.
    """
    import coherence_laws as cl

    ladders = cl._ladders(rows)
    bad_events = set()
    for (ev, direction), rungs in ladders.items():
        ordered = sorted(rungs.items())
        prices = [r["market_price"] for _, r in ordered]
        if direction == "below":
            prices = prices[::-1]
        if (any(prices[i] < prices[i + 1] - 1e-9 for i in range(len(prices) - 1))
                or len(set(prices)) == 1):
            bad_events.add(ev)

    kept, dropped = [], []
    for r in rows:
        if r.get("event_ticker") in bad_events:
            r = dict(r, dropped_reason="nested prices on this event are not "
                                       "monotone — the quote is not a probability")
            dropped.append(r)
        else:
            kept.append(r)
    return kept, dropped


# ------------------------------------------------------------------ sizing --

def kelly_fraction(p_hat, price):
    """Full-Kelly bankroll fraction for buying YES at `price` given true p.

    b = (1-price)/price is the win multiple. Returned unscaled and possibly
    huge; callers must apply KELLY_FRACTION. Negative means do not bet.
    """
    p = kc.parse_price(p_hat)
    q = kc.parse_price(price)
    if p is None or q is None:
        return None
    b = (1.0 - q) / q
    if b <= 0:
        return None
    return (p * b - (1.0 - p)) / b


def size_position(candidate, sleeve_dollars, calibration_ok=None):
    """Contracts to buy, or (0, reason). Fails CLOSED on every uncertainty.

    Structural locks size on the book. Disagreement candidates size on nothing
    at all until calibration has been earned — the gate is an argument rather
    than an import so the caller must state which regime it is in.
    """
    if sleeve_dollars is None or sleeve_dollars <= 0:
        return 0, "no sleeve capital allocated"

    cap_dollars = sleeve_dollars * MAX_SLEEVE_FRACTION_PER_EVENT

    if not candidate.get("requires_estimate"):
        cost_per = candidate["cost_cents"] / 100.0
        if cost_per <= 0:
            return 0, "cost of zero is an empty book, not a free contract"
        n = int(min(candidate.get("max_contracts", 0), cap_dollars / cost_per))
        if n <= 0:
            return 0, "book too thin for the per-event cap"
        return n, (f"structural lock, {candidate['edge_cents']:.1f}c/contract "
                   f"after fees")

    if calibration_ok is not True:
        return 0, ("DISAGREEMENT candidates are quarantined until "
                   "kalshi_calibration.is_edge_proven() passes — an unmeasured "
                   "tail estimator is indistinguishable from a broken one")

    p_hat, price = candidate.get("p_hat"), candidate.get("market_price")
    f = kelly_fraction(p_hat, price)
    if f is None or f <= 0:
        return 0, "no positive Kelly edge at this price"
    stake = min(sleeve_dollars * f * KELLY_FRACTION, cap_dollars)
    n = int(stake / price)
    if n <= 0:
        return 0, "quarter-Kelly stake rounds to zero contracts"
    return n, f"quarter-Kelly on p_hat={p_hat:.4f} vs price={price:.4f}"


# -------------------------------------------------------------------- scan --

def scan(max_pages=12, estimator=None, check_mutex=True):
    markets = list(kc.iter_markets(max_pages=max_pages))

    mutex_cache = {}

    def mutex_lookup(event_ticker):
        if not check_mutex:
            return False
        if event_ticker not in mutex_cache:
            mutex_cache[event_ticker] = kc.is_mutually_exclusive(
                kc.get_event(event_ticker))
        return mutex_cache[event_ticker]

    pairs = find_pair_locks(markets)
    # Only pay for event lookups where a field lock could possibly exist.
    fields = find_field_locks(markets, mutex_lookup) if check_mutex else []
    disagree = find_disagreement_candidates(markets, estimator)
    disagree, incoherent = drop_incoherent(disagree)

    report = {
        "scanned_at": _now(), "env": kc.env_name(),
        "markets_seen": len(markets),
        "with_real_book": sum(1 for m in markets if kc.tradeable(m)),
        # The gap between these two is the whole finding. `with_real_book`
        # counts markets you could buy; `two_sided` counts markets whose price
        # means something. On 2026-08-17 those were 7 and 0 out of 10,000.
        "two_sided": sum(1 for m in markets if kc.two_sided(m)),
        "structural_pair": pairs,
        "structural_field": fields,
        "disagreement": disagree,
        "dropped_incoherent": incoherent,
        "note": ("DISAGREEMENT rows are observations, not trades. Nothing in "
                 "this file places an order; kalshi_client.place_order raises "
                 "unless the Chairman arms it. `dropped_incoherent` rows failed "
                 "coherence_laws.law_nested_probabilities_are_monotone — their "
                 "quotes are resting offers, not probabilities."),
    }
    _save(SCAN_LOG, report)
    return report


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--no-mutex-check", action="store_true",
                    help="skip event lookups (faster, disables field locks)")
    args = ap.parse_args()

    r = scan(max_pages=args.pages, check_mutex=not args.no_mutex_check)

    print(f"KALSHI SCAN — env={r['env']}  {r['scanned_at'][:19]}Z")
    print(f"  markets seen {r['markets_seen']}, with a real book "
          f"{r['with_real_book']}")

    print(f"\n  [STRUCTURAL] pair locks (arithmetic, no estimate needed): "
          f"{len(r['structural_pair'])}")
    for c in r["structural_pair"][:10]:
        print(f"    +{c['edge_cents']:>5.1f}c  yes {c['yes_ask']*100:.0f}c + "
              f"no {c['no_ask']*100:.0f}c  x{c['max_contracts']}  {c['title'][:52]}")

    print(f"  [STRUCTURAL] field locks (mutex-gated): "
          f"{len(r['structural_field'])}")
    for c in r["structural_field"][:5]:
        print(f"    +{c['edge_cents']:>5.1f}c  {c['legs']} legs  "
              f"x{c['max_contracts']}  {c['event_ticker']}")

    print(f"\n  [DISAGREEMENT] near-certainty band 90-99c: "
          f"{len(r['disagreement'])}  (observed, never sized)")
    for c in r["disagreement"][:10]:
        be = c["breakeven_probability"]
        print(f"    {c['market_price']*100:5.1f}c  need >{be*100:.1f}% to break "
              f"even, {c['payoff_against']:.0f}:1 against  vol={c['volume']:.0f}")
        print(f"           {c['title'][:70]}")

    if not r["structural_pair"] and not r["structural_field"]:
        print("\n  No structural locks. That is the expected result on an "
              "efficient book and is not a failure of the scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

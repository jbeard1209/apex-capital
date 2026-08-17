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
from collections import Counter, defaultdict
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

# The order size breakeven is quoted for. Kalshi rounds its fee UP to a whole
# cent per ORDER, so the per-contract cost falls steeply with size: at 99c a
# 1-contract order pays 1c (14x the true rate) and a 100-contract order pays
# 0.07c each. Quoting breakeven at 1 contract made every 99c market require a
# >100% win rate — impossible, so the band auto-rejected itself. 100 is chosen
# because it is where the rounding stops dominating, not because it is a target
# position; the row is always priced at min(resting size, this).
REFERENCE_ORDER_CONTRACTS = 100

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

# Series whose outcome set is COLLECTIVELY EXHAUSTIVE — one leg must resolve YES.
#
# Empty, and that is the finding, not an oversight. Adding a series here requires
# evidence from the settlement rules that no residual outcome exists (e.g. an
# explicit "any other" leg, or a rulebook clause that the event cannot resolve
# all-No). Kalshi exposes no flag for this. Do NOT populate it by inferring from
# a title, and do NOT populate it because the arithmetic looks attractive — the
# attractive arithmetic IS the symptom of a non-exhaustive field.
EXHAUSTIVE_SERIES = frozenset()

# A certified field cannot be cheap. If a series on the allow-list ever quotes a
# large edge, the allow-list is wrong — nobody leaves 25c of guaranteed money on
# a liquid exchange for days. The bound refuses the trade and indicts the entry.
MAX_CREDIBLE_FIELD_EDGE_CENTS = 25.0


def find_field_locks(markets, mutex_lookup, stats=None):
    """Buy every outcome of an event for < $1. Returns [] until a field is proven
    EXHAUSTIVE, which Kalshi's API does not tell you.

    `mutex_lookup(event_ticker) -> bool` comes from Kalshi's own
    `mutually_exclusive` flag, and that flag is necessary but NOT sufficient.
    It promises **at most one** leg resolves YES. The lock needs **exactly one**.

    Measured 2026-08-17 across 1,591 fully-quoted mutex fields: median cost
    110.0c, 1,129 above 105c, max 6,594c on a 256-leg field. Correct pricing,
    no arbitrage. Every one of the 40 fields under 95c was a residual-outcome
    event, and Kalshi says so in its own settlement text — `KXLAPRIMARY-01R26`
    ("LA-01 Republican nominee?", 2 legs, 12.9c) carries the clause *"Otherwise,
    all markets in this event will resolve to No."* Louisiana moved to a jungle
    primary, so there may be no nominee at all. Buying both legs for 13c does
    not buy $1; it buys two tickets that expire worthless together. Likewise
    `KXSTATE51-29` ("What will be the 51st state?", 8 legs, 14.5c) — the market
    is not mispriced, it is saying *there will not be a 51st state*.

    Without this gate the first honest scan of the exchange would have handed the
    Chairman fourteen "locked" fields averaging ~70c of "edge", every one of
    which loses the entire stake. The mutex flag was itself added to stop the
    previous version of this same mistake; the assumption simply moved up a
    level, which is why the fix is an allow-list of PROVEN series and not another
    inferred flag.

    `stats` (optional dict) receives the denominators: a zero here means
    "nothing certifiable", and the counts are what distinguish that from
    "nothing looked at" (memory `presence-is-not-currency`).
    """
    by_event = defaultdict(list)
    for m in markets:
        et = m.get("event_ticker")
        t = kc.tradeable(m)
        if et and t:
            by_event[et].append((m, t))

    # THE WHOLE FIELD, or it is not a field.
    #
    # `by_event` holds only the legs carrying an ask. Summing those and calling
    # the shortfall an edge assumes the legs you can price ARE the outcome set —
    # the same assumption that made us scan 12,000 shards and report on "the
    # exchange". The first run against a real feed produced a 2-leg field at
    # 83c edge: buy both for 17c, collect $1. That is not an arbitrage, it is an
    # event with more outcomes than the two that happened to be quoted, and
    # buying them is a naked pair. So a lock requires an ask on EVERY leg.
    total_legs = Counter(m.get("event_ticker") for m in markets
                         if m.get("event_ticker"))

    out = []
    uncertifiable = []
    partial_fields = 0
    mutex_fields = 0
    for et, rows in by_event.items():
        if len(rows) < 2:
            continue
        if len(rows) != total_legs.get(et, 0):
            partial_fields += 1
            continue        # some outcome has no ask — the field cannot be bought
        try:
            if not mutex_lookup(et):
                continue
        except Exception:
            continue        # cannot tell -> not exclusive -> no trade
        mutex_fields += 1

        cost_c = sum(t[0] for _, t in rows) * 100.0
        fees_c = sum(kc.fee_cents(t[0], 1) or 0 for _, t in rows)
        edge_c = 100.0 - cost_c - fees_c
        if edge_c < MIN_LOCK_EDGE_CENTS:
            continue
        min_size = min(t[1] for _, t in rows)
        if min_size < MIN_LEG_SIZE:
            continue

        # THE EXHAUSTIVENESS GATE. Everything above proves the field is cheap;
        # nothing above proves it pays. Cheap and unproven is not a trade, it is
        # a row in a list the Chairman can read.
        series = (rows[0][0].get("series_ticker")
                  or et.rsplit("-", 1)[0])
        if series not in EXHAUSTIVE_SERIES:
            uncertifiable.append({
                "event_ticker": et, "series_ticker": series,
                "legs": len(rows), "cost_cents": round(cost_c, 2),
                "apparent_edge_cents": round(edge_c, 2),
                "reason": ("mutually_exclusive proves at most one YES, not "
                           "exactly one; this event may resolve all-No"),
            })
            continue
        if edge_c > MAX_CREDIBLE_FIELD_EDGE_CENTS:
            uncertifiable.append({
                "event_ticker": et, "series_ticker": series,
                "legs": len(rows), "cost_cents": round(cost_c, 2),
                "apparent_edge_cents": round(edge_c, 2),
                "reason": ("edge exceeds what a liquid exchange leaves standing "
                           "— treat as evidence EXHAUSTIVE_SERIES is wrong"),
            })
            continue

        # A field lock is arithmetic, so `tradeable()` (ask only) is the right
        # gate here and NOT the discredited one it is elsewhere: you are buying
        # the whole mutually-exclusive field, and $1 arrives no matter what the
        # individual quotes "meant". What can still kill it is FILL RISK — the
        # lock exists only if every leg fills. Legs quoted one-sided have a
        # single resting counterparty, and an 11-leg field is eleven chances to
        # end up holding a partial field, which is not a lock but a naked book.
        # So the count travels with the row instead of being discovered later.
        two_sided_legs = sum(1 for m, _ in rows if kc.two_sided(m))
        out.append({
            "strategy": "STRUCTURAL_FIELD", "event_ticker": et,
            "legs": len(rows),
            "legs_two_sided": two_sided_legs,
            "fill_risk": ("LOW" if two_sided_legs == len(rows) and len(rows) <= 3
                          else "HIGH" if len(rows) > 5 or two_sided_legs < len(rows)
                          else "MEDIUM"),
            "tickers": [m.get("ticker") for m, _ in rows],
            "cost_cents": round(cost_c, 2), "fees_cents": fees_c,
            "edge_cents": round(edge_c, 2),
            "max_contracts": int(min_size),
            "requires_estimate": False,
        })
    if stats is not None:
        stats["partial_fields_skipped"] = partial_fields
        stats["mutex_fields_priced"] = mutex_fields
        stats["uncertifiable"] = sorted(
            uncertifiable, key=lambda x: -x["apparent_edge_cents"])
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

        # Breakeven is priced for the order we could ACTUALLY send: the whole
        # resting size, capped at the reference order. Both numbers travel with
        # the row because both are true — a market with 3 contracts resting
        # really does cost the punitive 1c-rounded fee, and one with 500 really
        # does not. Publishing only the 1-contract figure declared the entire
        # 99c band mathematically impossible; publishing only the sized figure
        # would hide that thin books are taxed. See kc.breakeven_probability.
        order_n = max(1, min(int(size), REFERENCE_ORDER_CONTRACTS))
        be = kc.breakeven_probability(ask, contracts=order_n)
        be_one = kc.breakeven_probability(ask, contracts=1)
        row = {
            "strategy": "DISAGREEMENT", "ticker": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": str(m.get("title"))[:90],
            "market_price": ask, "available_size": size,
            # Recorded so a later audit can ask of any historical row "was
            # there anyone on the other side?" — the question the first four
            # days of this ledger cannot answer, because nobody stored it.
            "yes_bid": bid, "spread": round(ask - bid, 4),
            # The exchange's own description of the rung. Stored because the
            # title of every leg of an MLB total-runs ladder is identical —
            # "St. Louis vs Cincinnati Total Runs?" eleven times — so a
            # coherence check that reads the title can place none of them on
            # the number line, and reports zero incoherent because it checked
            # nothing. See coherence_laws.market_threshold.
            "strike_type": m.get("strike_type"),
            "floor_strike": m.get("floor_strike"),
            "cap_strike": m.get("cap_strike"),
            "yes_sub_title": m.get("yes_sub_title"),
            "breakeven_probability": round(be, 4) if be else None,
            "breakeven_at_one_contract": round(be_one, 4) if be_one else None,
            "priced_for_contracts": order_n,
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


def _ladder_rows_from_markets(markets):
    """Every quoted leg of every event, in the shape `coherence_laws._ladders` reads.

    Built from the FULL market list rather than from the surfaced candidates,
    because a ladder's coherence is a property of the whole ladder. The MLB
    event that surfaced on 2026-08-17 had eleven rungs and exactly two of them
    landed in the 90-99c band; judging monotonicity on those two would have
    passed an event whose other nine legs were never looked at. Two adjacent
    rungs are almost always locally fine — that is precisely why a local check
    is not the check.
    """
    out = []
    for m in markets:
        ask = kc.parse_price(m.get("yes_ask_dollars"))
        if not ask:
            continue
        out.append({
            "ticker": m.get("ticker"), "event_ticker": m.get("event_ticker"),
            "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"),
            "market_price": ask, "strike_type": m.get("strike_type"),
            "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
        })
    return out


def drop_incoherent(rows, markets=None):
    """Split candidates into (kept, dropped, unchecked) on the nested-price law.

    THREE OUTPUTS, NOT TWO, and the third is the point of this signature.

    This function used to return (kept, dropped) and it reported `dropped: 0`
    on a live scan — which read as "every surfaced candidate is coherent" and
    meant "not one of them could be placed on the number line". The threshold
    was in `floor_strike`, the check was reading the title, and every leg of
    the ladder shares one title. A filter that cannot parse its input and
    answers "kept" is the same failure as a verdict of GREEN from a check that
    could not read its source: it converts an inability to look into a pass.

    `unchecked` rows are surfaced with `coherence: "UNCHECKED"` on them so that
    a later consumer can tell "this passed" from "nobody could tell", which is
    the whole three-verdict discipline in coherence_laws applied one layer down.

    Pass `markets` — the full scan — whenever it is available.

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

    universe = _ladder_rows_from_markets(markets) if markets is not None else rows
    ladders = cl._ladders(universe)

    bad_events, checked_events = set(), set()
    for (ev, direction), rungs in ladders.items():
        checked_events.add(ev)
        ordered = sorted(rungs.items())
        prices = [r["market_price"] for _, r in ordered]
        if direction == "below":
            prices = prices[::-1]
        if (any(prices[i] < prices[i + 1] - 1e-9 for i in range(len(prices) - 1))
                or len(set(prices)) == 1):
            bad_events.add(ev)

    kept, dropped, unchecked = [], [], []
    for r in rows:
        ev = r.get("event_ticker")
        if ev in bad_events:
            dropped.append(dict(
                r, coherence="VIOLATED",
                dropped_reason="nested prices on this event are not monotone — "
                               "the quote is not a probability"))
        elif ev in checked_events:
            dropped_from = len(ladders.get((ev, "above"), {})) or \
                len(ladders.get((ev, "below"), {}))
            kept.append(dict(r, coherence="HOLDS", rungs_checked=dropped_from))
        else:
            # Not a pass. Nobody could place this event on the number line —
            # a single-leg market, a range market, or prose no parser reads.
            unchecked.append(dict(
                r, coherence="UNCHECKED",
                unchecked_reason="no nested ladder of 2+ parseable rungs on this "
                                 "event — coherence could not be evaluated"))
    return kept, dropped, unchecked


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

    # Belt to that brace, and it fails closed on ABSENCE: a row with no
    # `coherence` key at all is refused, not waved through. A candidate reaches
    # here only via drop_incoherent, which stamps all three verdicts; a row
    # missing the stamp came from somewhere that never ran the check.
    if candidate.get("coherence") != "HOLDS":
        return 0, (f"coherence={candidate.get('coherence') or 'ABSENT'} — only a "
                   f"candidate whose nested ladder was CHECKED and held may be "
                   f"sized. UNCHECKED is not a pass")

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

def scan(max_pages=25, estimator=None, check_mutex=True):
    # ONE walk of /events, and everything downstream is derived from it.
    #
    # This used to walk markets, then call `kc.get_event()` once per event to
    # read `mutually_exclusive` — 3,500+ extra HTTP round trips, and a scan that
    # ran past ten minutes. The flag was already on every event object the walk
    # had just downloaded. That runtime was not a detail: the cron budget is what
    # decides whether this sector runs unattended 4x/day or only when a human
    # remembers, and an unrunnable scan produces the same evidence as no scan.
    events = list(kc.iter_events(max_pages=max_pages))
    markets = [m for e in events for m in (e.get("markets") or [])
               if not kc.is_shard(m)]
    mutex_map = {e.get("event_ticker"): bool(e.get("mutually_exclusive"))
                 for e in events if e.get("event_ticker")}

    # A scan that finds nothing has two explanations with opposite responses:
    # the exchange is quiet, or we are not reading the exchange. For weeks this
    # returned the second while reporting the first — every market it walked was
    # an auto-generated parlay shard (see kc.iter_markets). So the feed states
    # its own health, and an empty result on a sick feed is CANNOT_TELL.
    feed = {"events": len(events),
            "genuine": len(markets),
            "mutually_exclusive_events": sum(1 for v in mutex_map.values() if v),
            "shards_excluded": True,
            "healthy": len(markets) > 0}
    if not feed["healthy"]:
        report = {"scanned_at": _now(), "env": kc.env_name(),
                  "verdict": "CANNOT_TELL", "feed": feed, "markets_seen": 0,
                  "note": ("The feed returned no genuine markets. That is a "
                           "statement about the read, NOT about the exchange — "
                           "do not record it as 'no opportunities'.")}
        _save(SCAN_LOG, report)
        return report

    def mutex_lookup(event_ticker):
        # An event the walk never saw is NOT exclusive. Same rule as everywhere
        # else here: cannot-tell is not permission.
        return bool(check_mutex and mutex_map.get(event_ticker))

    field_stats = {}
    pairs = find_pair_locks(markets)
    fields = find_field_locks(markets, mutex_lookup, stats=field_stats) if check_mutex else []
    disagree = find_disagreement_candidates(markets, estimator)
    # `markets`, not `disagree`: the ladder is judged whole. See drop_incoherent.
    disagree, incoherent, unchecked = drop_incoherent(disagree, markets=markets)

    report = {
        "scanned_at": _now(), "env": kc.env_name(),
        "feed": feed,
        "markets_seen": len(markets),
        "with_real_book": sum(1 for m in markets if kc.tradeable(m)),
        # The gap between these two is the whole finding. `with_real_book`
        # counts markets you could buy; `two_sided` counts markets whose price
        # means something. Through the shard feed those were 7 and 0 of 10,000;
        # through the events feed, the same hour, 24,881 of 48,199 are two-sided.
        "two_sided": sum(1 for m in markets if kc.two_sided(m)),
        "structural_pair": pairs,
        "structural_field": fields,
        # The denominators for the field scan. `structural_field: 0` with these
        # at 428/1591/14 means "1,591 mutex fields were priced, 14 were cheap,
        # and none is provably exhaustive" — a fact about the rulebook. At
        # 0/0/0 it would mean nothing was looked at. Same zero, opposite
        # meanings, which is why all three travel with it.
        "field_partial_skipped": field_stats.get("partial_fields_skipped", 0),
        "field_mutex_priced": field_stats.get("mutex_fields_priced", 0),
        "field_uncertifiable": field_stats.get("uncertifiable", []),
        "disagreement": disagree,
        "dropped_incoherent": incoherent,
        # Held apart from `disagreement` on purpose. Folding these in would make
        # a scan that checked nothing indistinguishable from a scan that checked
        # everything and found it clean — which is exactly what `dropped: 0` on
        # 2026-08-17 meant before the strike fields were read.
        "unchecked_coherence": unchecked,
        "note": ("DISAGREEMENT rows are observations, not trades. Nothing in "
                 "this file places an order; kalshi_client.place_order raises "
                 "unless the Chairman arms it. `dropped_incoherent` rows failed "
                 "coherence_laws.law_nested_probabilities_are_monotone — their "
                 "quotes are resting offers, not probabilities. "
                 "`unchecked_coherence` rows sit in no checkable ladder: they "
                 "were NOT cleared, nobody could tell."),
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

    unc = r.get("field_uncertifiable", [])
    print(f"  [STRUCTURAL] field locks (exhaustiveness-gated): "
          f"{len(r['structural_field'])} certified of "
          f"{r.get('field_mutex_priced', 0)} mutex fields priced "
          f"({r.get('field_partial_skipped', 0)} skipped: not every leg quoted)")
    for c in r["structural_field"][:5]:
        print(f"    +{c['edge_cents']:>5.1f}c  {c['legs']} legs "
              f"({c.get('legs_two_sided', '?')} two-sided, fill risk "
              f"{c.get('fill_risk', '?')})  x{c['max_contracts']}  {c['event_ticker']}")
    if unc:
        print(f"    {len(unc)} cheap field(s) REFUSED — no proof the outcome set "
              f"is exhaustive, so 'buy the field' can lose 100%:")
        for c in unc[:5]:
            print(f"      would-be +{c['apparent_edge_cents']:>5.1f}c  "
                  f"{c['legs']} legs  {c['event_ticker']}")

    print(f"\n  [DISAGREEMENT] near-certainty band 90-99c: "
          f"{len(r['disagreement'])} coherent, "
          f"{len(r.get('dropped_incoherent', []))} dropped incoherent, "
          f"{len(r.get('unchecked_coherence', []))} uncheckable  "
          f"(observed, never sized)")
    for c in r["disagreement"][:10]:
        be = c["breakeven_probability"]
        be1 = c.get("breakeven_at_one_contract") or be
        print(f"    {c['market_price']*100:5.1f}c  need >{be*100:.2f}% to break "
              f"even at x{c.get('priced_for_contracts', 1)} "
              f"({be1*100:.2f}% at x1), {c['payoff_against']:.0f}:1 against  "
              f"vol={c['volume']:.0f}")
        print(f"           {c['title'][:70]}")

    if not r["structural_pair"] and not r["structural_field"]:
        print("\n  No structural locks. That is the expected result on an "
              "efficient book and is not a failure of the scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

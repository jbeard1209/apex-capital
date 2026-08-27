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

WHAT THE FIRST MEASUREMENT SAID, AND WHY IT WAS WRONG ABOUT THE EXCHANGE
------------------------------------------------------------------------
2026-08-14, 12,000 markets off the flat `/markets` feed: 94.4% had no order
book at all, and the entire 90-99c band across "the whole exchange" held **one**
market with a nonzero ask size, at zero volume. Read as a fact about Kalshi,
that says the premise is dead.

It was a fact about the FEED. `/markets` serves multivariate parlay shards
first and there are enough of them to swallow any bounded walk — 59,989 of
60,000 on 2026-08-17 — and shards have no book by construction. The scan was
accurate about the population it measured, and that population was not the
exchange (`kc.iter_markets`).

Through `/events?with_nested_markets=true`, shards excluded, live 2026-08-24:
**95,973 genuine markets, 91,049 with a real ask (94.9%), 44,676 two-sided
(46.6%), 4,999 priced mutex fields, 8 through the band gates.** Not one gate
was loosened to get there.

Phase 1 stays a collector regardless. What changed is that "does the
opportunity exist at all?" now has a population it can honestly be asked of;
before, the answer was an artefact of which endpoint we walked.

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

# The COMPLETE ladders behind everything that surfaced, kept in their own file so
# `coherence_laws.law_nested_probabilities_are_monotone` can re-derive its verdict
# from raw quotes instead of from the rows this scanner chose to keep.
#
# A ladder's coherence is a property of the WHOLE ladder, and the near-certainty
# band is a slice through it. Judge the slice and you get it wrong in both
# directions: the MLB event with eleven rungs put exactly two in the band, and
# two adjacent rungs are almost always locally fine (false negative); a ladder
# whose in-band rungs both clip to the 99c ceiling looks "all identically
# priced" (false positive — this cost a real investigation on 2026-08-17, when
# the law reported 26 leaks that were not leaks). Only complete ladders are
# checkable, so only complete ladders are stored.
LADDER_UNIVERSE = DATA_DIR / "kalshi_ladders.json"

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

# ── The two gates that turn a price band into a business ─────────────────────
#
# Measured 2026-08-18 over 48,630 genuine markets: the 90-99c band selected on
# PRICE ALONE has a median 442 days to close (max 3,816 — a decade). Its median
# annualized return is 3.44%, BELOW the 4.25% T-bill, and that figure already
# assumes a 100% hit rate before fees. So the band as originally defined is not
# a mispricing to harvest; it is mostly the time value of money, correctly
# priced. Buying it would lock capital for fourteen months to underperform cash
# while carrying settlement risk on top. No amount of calibration work fixes
# that — the trade is bad even when the probability estimate is perfect.
#
# `days_to_close` is the load-bearing filter, and it buys two separate things:
#
#   1. RETURN. APY = gross / days, so the horizon IS the return. Every one of
#      the top 15 markets by APY closes within 82 days.
#   2. MEASURABILITY. A calibration study whose observations settle in 442 days
#      cannot report for over a year, so `is_edge_proven` would answer "NOT YET"
#      until 2027 and the sector would run all that time on the strength of a
#      number nobody had checked. Memory `sample-outlived-its-rule`.
#
# 90 days is where the exchange actually is, not a round number: of 2,946
# two-sided band markets, ZERO close within 30 days and 252 close in 31-90.
# Under 31 days the band is empty — the near-certainty that settles next week
# gets arbitraged away, which is the market working correctly.
MAX_DAYS_TO_CLOSE = 90

# And a FLOOR, which is the less obvious half.
#
# Measured on the first gated scan: 29 candidates closed within six hours and
# 108 within a day — "Bitcoin price on Aug 18, 2026?" quoted at 0.98 an hour
# before it settles. That is not a forecast, it is a reading of an event that
# has essentially already happened, and it breaks the sector in two ways:
#
#   1. The return is fictional. APY = gross x 365/days explodes as days -> 0;
#      those rows scored up to 96,894% annualized. Nothing can be ranked against
#      a number that is mostly a division by a small denominator.
#   2. It poisons the CALIBRATION, and this is the one that would have cost
#      money. Near-expiry quotes are nearly tautological, so they settle YES at
#      a very high rate — AND they settle first. The first 200 settled rows
#      would therefore be almost entirely sub-3-day markets, `is_edge_proven`
#      would flip to PROVEN on them, and that verdict would then license sizing
#      on the 30-90 day positions the sample never contained. A hit rate is a
#      statement about a horizon; borrowing one horizon's rate to underwrite
#      another's is the same error as `sample-outlived-its-rule`.
#
# One day is the floor because a scanner on a 4x-daily cron cannot reliably act
# inside one, so anything below it is unactionable regardless of its arithmetic.
MIN_DAYS_TO_CLOSE = 1.0

# The return floor. A 97c contract settling in 442 days pays 3.1% gross; the
# same 97c settling in 45 days pays 25% annualized. Only the second is a trade.
# Set at the 3-month T-bill because that is the literal alternative use of the
# same locked dollar — anything below this is a worse Treasury with counterparty
# and rules risk stapled on. Re-measure when rates move; this is not a constant
# of nature. Gross of fees and assuming settlement goes our way, so it is an
# upper bound on the return, and a market failing it fails generously.
RISK_FREE_RATE = 0.0425
MIN_ANNUALIZED_RETURN = RISK_FREE_RATE

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

    THAT ARITHMETIC IS ONLY TRUE IF BOTH LEGS FILL, and this sized the whole
    two-leg position on `yes_ask_size_fp` — the depth of the YES leg — without
    ever reading the NO leg's. Kalshi publishes no `no_ask_size`, which is why
    it looked absent. It is not: the NO ask book IS the YES bid book. Measured
    2026-08-26 across 32,458 live markets quoting both sides, `no_ask ==
    1 - yes_bid` held to the last digit on every one of them (max deviation
    0.0). The NO leg's depth is `yes_bid_size_fp`, and it was sitting on the
    same market object this function already held.

    Not a rare disagreement: of 31,651 live markets quoting depth on both sides
    the two differ on 72.2%, and the unread leg is the SHALLOWER one on 39.7%
    (12,560) — median ratio 0.113, so the typical affected market has nine times
    less room on the side nobody checked, and 5,884 are over-sized by more than
    10x. Filling 500 YES against 3 NO is not a 2c lock, it is 497 naked long
    contracts at 95c inside the one strategy this file calls arithmetic.

    What made it worse than a sizing slip: `kalshi_board._liquidity_officer`
    reads `max_contracts` and reports it back as "N contracts resting on the
    thinnest leg". The seat whose entire job is refusing thin books was handed
    a number with the thin leg already removed, and quoted it as the thin leg
    (memory `guardrail-on-one-path`).

    `find_field_locks` never had this bug: it buys YES on every leg, so every
    leg's depth IS a `yes_ask_size_fp` and its `min(...)` was already right. The
    pair is the only lock with a leg bought from the other side of the book —
    the only one where "this leg's depth" and "the YES ask depth" differ.

    NOT AN ESCAPE. This path has surfaced nothing: a full walk on 2026-08-26 —
    102,652 genuine markets — returned 0 pair locks, so nothing was ever
    mis-sized in production, and "it has never lost money" is not evidence about
    it either. It is fixed now because it is the ONE strategy that sizes without
    waiting for calibration (`requires_estimate` is False), which makes it the
    first thing to run on the day the sector is armed.
    """
    out = []
    for m in markets:
        ya = kc.parse_price(m.get("yes_ask_dollars", m.get("yes_ask")))
        na = kc.parse_price(m.get("no_ask_dollars", m.get("no_ask")))
        # One book read from two sides: `yes_ask_size_fp` backs the YES buy,
        # `yes_bid_size_fp` backs the NO buy.
        ys = kc.parse_size(m.get("yes_ask_size_fp"))
        ns = kc.parse_size(m.get("yes_bid_size_fp"))
        if ya is None or na is None or not ys or not ns:
            continue

        cost_c = (ya + na) * 100.0
        fees_c = (kc.fee_cents(ya, 1) or 0) + (kc.fee_cents(na, 1) or 0)
        edge_c = 100.0 - cost_c - fees_c
        if edge_c < MIN_LOCK_EDGE_CENTS:
            continue
        # The lock is only as large as its SHALLOWER leg, and the size floor
        # belongs on that leg rather than on whichever one is quoted first.
        depth = min(ys, ns)
        if depth < MIN_LEG_SIZE:
            continue

        out.append({
            "strategy": "STRUCTURAL_PAIR", "ticker": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": str(m.get("title"))[:90],
            "yes_ask": ya, "no_ask": na,
            "cost_cents": round(cost_c, 2), "fees_cents": fees_c,
            "edge_cents": round(edge_c, 2),
            # Both depths travel with the row. A lone `max_contracts` is a number
            # no reader can check; the pair is what shows WHICH leg binds, and
            # what makes a regression to one-leg sizing visible in the artifact
            # instead of only in the diff.
            "yes_leg_size": int(ys), "no_leg_size": int(ns),
            "max_contracts": int(depth),
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

def days_to_close(market, now=None):
    """Days until the market closes, or None if it does not say.

    None is NOT zero and is not "soon". A market with no close_time cannot be
    horizon-filtered, so the caller must reject it rather than default it —
    memory `fallback-that-means-act`: the constant a degraded read substitutes
    is itself a decision, and every plausible default here (0, 999) reads as
    either "buy immediately" or "never buy".
    """
    c = market.get("close_time")
    if not c:
        return None
    try:
        d = datetime.fromisoformat(str(c).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (d - now).total_seconds() / 86400.0


def annualized_return(ask, days):
    """Simple annualized return if the contract settles YES, or None.

    Deliberately NOT compounded: these are single-settlement instruments held to
    expiry, so there is nothing to reinvest and a compounded figure would flatter
    the short-dated end of exactly the filter that selects on it.
    """
    if not ask or days is None or days <= 0 or ask <= 0 or ask >= 1:
        return None
    return ((1.0 - ask) / ask) * 365.0 / days


def find_disagreement_candidates(markets, estimator=None, stats=None):
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
    # Denominators. A gate that reports only what it passed cannot be audited:
    # "4 candidates" is the same output whether it rejected 6 or 60,000, and the
    # internal auditor refuses a HOLDS with no n_examined for exactly this
    # reason. Every `continue` below increments something.
    s = stats if stats is not None else {}
    for k in ("seen", "no_two_sided_book", "outside_band", "no_close_time",
              "too_far_out", "settles_too_soon", "return_below_riskfree",
              "passed"):
        s.setdefault(k, 0)

    for m in markets:
        s["seen"] += 1
        # kc.two_sided, not kc.tradeable. An ask with no bid is not a price —
        # see that function's docstring for the 86 settlements that established
        # it. This is the gate that empties this list on most scans, and an
        # empty list here is the honest answer, not a bug.
        t = kc.two_sided(m)
        if not t:
            s["no_two_sided_book"] += 1
            continue
        bid, ask, size = t
        if not (NEAR_CERTAINTY_LO <= ask <= NEAR_CERTAINTY_HI):
            s["outside_band"] += 1
            continue

        # Horizon, then return. See MAX_DAYS_TO_CLOSE for the measurement that
        # put these here: on price alone this band is a worse T-bill.
        dtc = days_to_close(m)
        if dtc is None:
            s["no_close_time"] += 1
            continue
        if dtc > MAX_DAYS_TO_CLOSE:
            s["too_far_out"] += 1
            continue
        if dtc < MIN_DAYS_TO_CLOSE:
            s["settles_too_soon"] += 1
            continue
        apy = annualized_return(ask, dtc)
        if apy is None or apy < MIN_ANNUALIZED_RETURN:
            s["return_below_riskfree"] += 1
            continue
        s["passed"] += 1

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
            # Both gates travel with the row, not just their verdict. A ledger
            # that stores only survivors cannot later answer "what rule was this
            # picked under?", and that question is the whole of
            # `sample-outlived-its-rule`.
            "days_to_close": round(dtc, 2),
            "annualized_return": round(apy, 4),
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
            "market_price": ask, "yes_bid": kc.parse_price(m.get("yes_bid_dollars")),
            "strike_type": m.get("strike_type"),
            "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
        })
    return out


def _persistable_ladder_universe(universe, band_events):
    """Every quoted rung of every event that put a leg in the band.

    Whole ladders for the events that matter, rather than every rung on the
    exchange (46,726 rungs / 13.7MB per scan) or only the in-band rungs (which
    are not ladders at all). Measured: 21,048 rungs, ~4MB.

    The scanner's only contribution is WHICH EVENTS to store — a structural
    choice, not the judgment under test. Thresholds are deliberately NOT parsed
    here: the law runs its own `market_threshold` over the raw exchange fields,
    so the two sides of the comparison stay independent. Storing a verdict, or
    even a parse, would make the law audit this function's opinion instead of
    the exchange's numbers.
    """
    # `yes_bid` is not optional decoration: without the other side of the book
    # the monotonicity law can only compare ask to ask, which is wrong 98.6% of
    # the time (see `coherence_laws._bid_of`). `title` is dropped — measured
    # 2026-08-17, keeping it cost 3.2MB per scan and bought zero extra ladders,
    # because `yes_sub_title` plus the strike fields already parse every rung.
    keep = ("ticker", "event_ticker", "market_price", "yes_bid", "strike_type",
            "floor_strike", "cap_strike", "yes_sub_title")
    return [{k: r.get(k) for k in keep}
            for r in universe if r.get("event_ticker") in band_events]


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

    THE TEST IS `cl._ladder_fault`, NOT A COPY OF IT. Until 2026-08-17 this
    function held its own ask-vs-ask comparison, dropped 441 rows a day, and
    told the ledger they were dropped because "the quote is not a probability".
    Measured against the arbitrage condition, 439 of those 441 were nothing of
    the kind — they were the bid-ask spread, on candidates that had already
    cleared `kc.two_sided`. A filter is allowed to be conservative; it is not
    allowed to write a false reason into the record, and 23% of the intake was
    being discarded on one. Both consumers now call the same rule so they
    cannot drift apart again.

    A one-sided ladder no longer counts as checked. It used to land in `kept`
    with `coherence="HOLDS"` — a pass awarded to a ladder that could not fail.

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
        _o, _a, _b, hit, pairs = cl._ladder_fault(rungs, direction)
        if not pairs:
            continue          # one-sided: not checked, so not a pass either
        checked_events.add(ev)
        if hit is not None:
            bad_events.add(ev)

    kept, dropped, unchecked = [], [], []
    for r in rows:
        ev = r.get("event_ticker")
        if ev in bad_events:
            dropped.append(dict(
                r, coherence="VIOLATED",
                dropped_reason="a nested pair on this event quotes ask(wide) below "
                               "bid(narrow) — no set of probabilities produces it"))
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

def scan(max_pages=60, estimator=None, check_mutex=True):
    # ONE walk of /events, and everything downstream is derived from it.
    #
    # This used to walk markets, then call `kc.get_event()` once per event to
    # read `mutually_exclusive` — 3,500+ extra HTTP round trips, and a scan that
    # ran past ten minutes. The flag was already on every event object the walk
    # had just downloaded. That runtime was not a detail: the cron budget is what
    # decides whether this sector runs unattended 4x/day or only when a human
    # remembers, and an unrunnable scan produces the same evidence as no scan.
    walk = {}
    events = list(kc.iter_events(max_pages=max_pages, stats=walk))
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
            # Did we reach the end of the feed, or stop at the page cap? These
            # are separate from `healthy` because they answer a different
            # question: `healthy` asks "did we read anything?", `walk_complete`
            # asks "did we read it ALL?". Only the second licenses a conclusion
            # from an empty result — and the cap silently truncated the walk at
            # 5,000 of 10,184 events for as long as this feed has been used.
            "pages_walked": walk.get("pages", 0),
            "walk_complete": bool(walk.get("complete")),
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
    band_stats = {}
    disagree = find_disagreement_candidates(markets, estimator, stats=band_stats)
    # `markets`, not `disagree`: the ladder is judged whole. See drop_incoherent.
    disagree, incoherent, unchecked = drop_incoherent(disagree, markets=markets)

    # Publish the complete ladders behind everything that surfaced, so the
    # coherence law can check this scanner's work against the raw quotes rather
    # than against the rows this scanner decided to keep.
    universe = _ladder_rows_from_markets(markets)
    band_events = {r["event_ticker"] for r in universe
                   if r.get("event_ticker")
                   and NEAR_CERTAINTY_LO <= r["market_price"] <= NEAR_CERTAINTY_HI}
    ladder_rows = _persistable_ladder_universe(universe, band_events)
    _save(LADDER_UNIVERSE, {"scanned_at": _now(), "env": kc.env_name(),
                            "events": len(band_events), "rungs": len(ladder_rows),
                            "rows": ladder_rows})

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
        # Why the band emptied, in order of gate. Without this a scan reporting
        # 4 candidates cannot be distinguished from a broken feed, and the
        # horizon gate in particular rejects ~90% of the price band — a fact
        # about the exchange that has to stay visible, or a later reader will
        # "restore" the candidates by deleting the filter.
        "band_gates": band_stats,
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
    # None, not a number. This flag carried its own default of 12 — so every
    # command-line run, which is every CRON run, walked 2,400 of the feed's
    # 10,184 events while `scan()`'s own default said otherwise. Two defaults
    # for one quantity means the one you read is not the one that runs.
    ap.add_argument("--pages", type=int, default=None,
                    help="page cap for the events walk (default: scan()'s own)")
    ap.add_argument("--no-mutex-check", action="store_true",
                    help="skip event lookups (faster, disables field locks)")
    args = ap.parse_args()

    kw = {"check_mutex": not args.no_mutex_check}
    if args.pages is not None:
        kw["max_pages"] = args.pages
    r = scan(**kw)

    print(f"KALSHI SCAN — env={r['env']}  {r['scanned_at'][:19]}Z")
    _f = r.get("feed", {})
    _walk = ("whole feed" if _f.get("walk_complete")
             else f"⚠ TRUNCATED at the {_f.get('pages_walked', '?')}-page cap")
    print(f"  markets seen {r['markets_seen']}, with a real book "
          f"{r['with_real_book']}  [{_f.get('pages_walked','?')} pages, {_walk}]")

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

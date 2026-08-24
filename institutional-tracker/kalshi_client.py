#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI CLIENT (the read layer, and the armed-only write layer)
=============================================================================
Kalshi is a CFTC-regulated event exchange: contracts settle at $1.00 or $0.00.
A price is therefore a probability, and a 96c ask is the market saying "96%".

WHY THIS FILE IS MOSTLY DEFENSIVE PARSING
-----------------------------------------
Every published Kalshi tutorial reads `market["yes_ask"]` and treats it as an
integer number of cents. Measured against the live API on 2026-08-14, that
field returns **None** on every market. The live schema is `yes_ask_dollars`,
and its value is a **string**: `'0.9600'`, not `0.96` and not `96`.

Three ways that kills you, all of which we have shipped bugs shaped like before:

  1. `m.get("yes_ask", 0)`      -> 0 cents -> "this contract is FREE, buy it"
  2. `float(m["yes_ask"])`      -> TypeError on None, mid-scan, silently caught
  3. `m["yes_ask_dollars"] < 1` -> TypeError: '<' not supported str vs int

THE ZERO TRAP (this one is the expensive one)
---------------------------------------------
`yes_ask_dollars` is `'0.0000'` on an empty book, with `yes_ask_size_fp` of
`'0.00'`. Zero is a perfectly valid float. It passes `is not None`, it passes
`isinstance(x, float)`, and it reads as "you can buy YES for nothing" — an
infinite-return arbitrage on every illiquid market on the exchange.

**A price with no size behind it is not a price, it is the absence of one.**
Measured 2026-08-14 across 12,000 markets from the flat `/markets` feed:
**94.4% (11,331) had no real book** — but that feed serves parlay shards first,
so the finding was about shards, not the exchange (see `iter_markets`, and the
corrected rates in `tradeable`). On the genuine population the filter passes
94.9%. The filter is no less necessary: the markets it rejects still quote
`0.0000`, and there are still 4,924 of them.
This is the same bug class as memory `nan-is-not-none`, wearing a new costume:
the sentinel is `0.0` instead of `NaN`, and it is *more* dangerous because it
survives arithmetic instead of poisoning it.

So: `parse_price()` returns None for anything it cannot vouch for, and
`tradeable()` requires a price AND a size AND a sane 0<p<1 range. Callers must
branch on None. There is no default.

AUTH AND THE ARM GATE
---------------------
Public market data needs no credentials at all (verified: HTTP 200 unauthed),
so the entire research and calibration phase runs with zero secrets on disk.

Order placement needs RSA-PSS request signing. Claude does not and will not
handle those keys: `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` are read
from the environment, which the Chairman populates himself. On top of that,
`place_order()` refuses to do anything unless `data/.kalshi_armed` exists and
names the same environment — the same shadow -> ENFORCE shape as
`signoff_ledger`. Default is `shadow`, which cannot place an order at all.

Usage:
    python3 kalshi_client.py --report        # exchange snapshot + book health
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
ARM_FILE = DATA_DIR / ".kalshi_armed"

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

USER_AGENT = "apex-capital/1.0 (research)"

# Kalshi's published fee schedule. The fee is a DOME peaking at p=0.50 and
# decaying toward both extremes, which is the one structural thing working in
# favour of trading the tails: at 96c the taker fee is ceil(0.07*0.96*0.04*100)
# = 1c per contract, against 2c at the midpoint.
TAKER_FEE_RATE = 0.07
MAKER_FEE_RATE = 0.0175


class KalshiNotArmed(RuntimeError):
    """Raised instead of returning None, so a caller cannot mistake a refused
    order for a placed one. Same reasoning as SingleWriterBlocked."""


def env_name() -> str:
    """shadow | demo | live. Default is the one that cannot spend money."""
    v = (os.environ.get("KALSHI_ENV") or "shadow").strip().lower()
    return v if v in ("shadow", "demo", "live") else "shadow"


def base_url() -> str:
    return DEMO_BASE if env_name() == "demo" else PROD_BASE


# ----------------------------------------------------------------- parsing --

def parse_price(raw):
    """A price in DOLLARS as a float in (0, 1), or None if not a real price.

    Returns None — never 0.0 — for missing, unparseable, or out-of-range input.
    0.0 and 1.0 are both rejected: they are what an empty book reports, and a
    contract that truly cannot move is one nobody will fill you on anyway.
    """
    if raw is None:
        return None
    try:
        v = float(raw)          # str '0.9600' and float 0.96 both land here
    except (TypeError, ValueError):
        return None
    if v != v:                  # NaN — see memory `nan-is-not-none`
        return None
    if not (0.0 < v < 1.0):
        return None
    return v


def parse_size(raw):
    """Contracts resting at that price, or None. 0 means no book, not no data."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:
        return None
    return v


def fee_cents(price, contracts=1, maker=False):
    """Kalshi trading fee in whole cents: ceil(rate * C * P * (1-P) * 100).

    Charged on the trade, not on settlement. Rounded UP per Kalshi's schedule,
    so a 1-contract trade at any price costs at least 1c — which is why tiny
    position sizes are disproportionately taxed and why size discipline here is
    not the same as size discipline in equities.
    """
    import math
    p = parse_price(price)
    if p is None or contracts <= 0:
        return None
    rate = MAKER_FEE_RATE if maker else TAKER_FEE_RATE
    return math.ceil(rate * contracts * p * (1.0 - p) * 100.0)


def breakeven_probability(price, maker=False, contracts=1):
    """The true probability at which buying YES at `price` is EV-neutral,
    FOR AN ORDER OF `contracts`. Order size is not a detail here — it moves the
    answer by more than the entire margin the strategy trades in.

    Buying N contracts at p costs N*p dollars plus ONE rounded-up fee, and
    returns N*$1.00 if it settles YES. So we need q - p - fee/(100*N) = 0.

    Kalshi rounds the fee up to a whole cent PER ORDER. At 99c the true rate is
    0.07 * 0.99 * 0.01 = 0.069c per contract, so a 1-contract order pays 1c —
    fourteen times the rate — while a 100-contract order pays 7c, or 0.07c each.
    Breakeven at 99c is therefore 100.00% for one contract and 99.07% for a
    hundred. This default used to be hardcoded at 1, and the scanner printed
    "need >100.0% to break even" against every 99c market on the exchange: not
    a fact about Kalshi, an artifact of rounding a fee we were not going to pay,
    and a mathematical impossibility that auto-rejected the band the sector
    exists to trade. Fee models that overstate cost look prudent and are not —
    they retire strategies without ever stating a reason ([[nan-is-not-none]]
    for the same shape in the other direction).

    The default stays 1 so no existing caller silently gets a rosier number;
    callers that are sizing an order MUST pass the size they would actually
    send, bounded by the resting size they could actually fill.
    """
    p = parse_price(price)
    if p is None:
        return None
    try:
        n = int(contracts)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    f = fee_cents(p, n, maker)
    if f is None:
        return None
    return p + f / (100.0 * n)


def tradeable(market):
    """(yes_ask, yes_size) if this market has a real, buyable YES book, else None.

    THE filter. Callers that skip it are not scanning an exchange, they are
    scanning a list of empty order books.

    THE PASS RATE INVERTED WHEN THE FEED CHANGED, AND BOTH NUMBERS ARE TRUE.
    This docstring said "94.4% of open markets fail it" from 2026-08-14 until
    2026-08-24. That was measured through the flat `/markets` walk, which
    returns parlay shards first (see `iter_markets`): the population was ~94%
    inventory, and inventory has no book, so the filter was mostly measuring the
    shard fraction. Through `/events` with shards excluded, live 2026-08-24,
    **91,049 of 95,973 genuine markets PASS** — 94.9%.

    Leaving the old figure here was the live hazard, not the untidiness: the
    next reader to check the docstring against a live scan sees 94.9% pass where
    the comment promises 94.4% fail, concludes the filter is inverted, and
    loosens the one gate that must not be loosened. A rate is a fact about a
    POPULATION. Quote the population or do not quote the rate.

    The rate that still bites is `two_sided()`'s: 44,676 of 95,973 (46.6%).
    """
    ask = parse_price(market.get("yes_ask_dollars", market.get("yes_ask")))
    size = parse_size(market.get("yes_ask_size_fp"))
    if ask is None or not size:
        return None
    return ask, size


def two_sided(market, max_spread=0.10):
    """(bid, ask, ask_size) if BOTH sides are quoted and close, else None.

    THE STRICTER FILTER, and the one the near-certainty band actually needed.

    `tradeable()` asks "is there a real ask, at a real size?" That is enough to
    know you can BUY. It is not enough to know the ask means anything. A market
    with an ask and no bid has exactly one participant — a seller — and their
    asking price is not a probability, it is an opinion nobody has met. You can
    fill on it, and that is the trap: the fill is real and the price is fiction.

    Measured 2026-08-17, 10,000 open markets: 7 passed `tradeable()`, and **all
    7 had no yes_bid at all**. Zero markets on the entire exchange had a
    two-sided book at that hour. The settled ledger says what buying the
    one-sided quotes was worth: 86 settlements at an average quote of 0.97
    resolved YES 27% of the time, -0.68 per contract. Splitting them by whether
    their event's nested legs were coherently priced changed nothing — the
    "coherent" survivors lost 0.745 per contract, slightly WORSE than the
    incoherent ones. There was no good subset to find, because the defect is
    not in which markets were picked, it is in what the number meant.

    So: no bid, no price. This is the mirror of the rule that opened the sector
    ("cost of zero is an empty book, not a free contract") — an empty book
    reports 0 on one side and, when someone rests a token offer, the top of the
    range on the other. Both readings are the same absence.

    `max_spread` exists because a bid of 0.05 against an ask of 0.98 is a
    two-sided book only in the sense that two people are shouting past each
    other. Neither number constrains the other and the mid is meaningless.
    """
    ask = parse_price(market.get("yes_ask_dollars", market.get("yes_ask")))
    bid = parse_price(market.get("yes_bid_dollars", market.get("yes_bid")))
    size = parse_size(market.get("yes_ask_size_fp"))
    if ask is None or bid is None or not size:
        return None
    if ask <= bid:              # crossed or locked: a stale snapshot, not a book
        return None
    if (ask - bid) > max_spread:
        return None
    return bid, ask, size


# -------------------------------------------------------------- transport --

def _get(path, params=None, timeout=30, retries=2):
    """GET against the public API. Raises on failure — never returns {}.

    An empty dict would read downstream as "the exchange listed no markets",
    which is indistinguishable from "we could not reach the exchange". Those
    have opposite correct responses, so this must raise.
    """
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items()
                                             if v is not None})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt)
                last = e
                continue
            raise
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise
    raise last


def is_shard(market) -> bool:
    """True for an auto-generated multivariate parlay leg, not a real market.

    Kalshi mints `KXMVECROSSCATEGORY-SHARD*` combination markets in bulk — every
    permutation of a handful of underlying legs. They are `is_provisional`, they
    carry an `mve_collection_ticker`, and essentially none of them ever get a
    quote. They are inventory, not an order book.
    """
    return bool(market.get("mve_collection_ticker") or market.get("is_provisional"))


def iter_events(status="open", max_pages=60, limit=200, stats=None):
    """Yield open events WITH their markets nested. Bounded, like iter_markets.

    This is the door onto the exchange. `/markets` is not — see iter_markets.

    `max_pages` was 25, which at limit=200 is 5,000 events. Measured 2026-08-18,
    twice, identically: the open-events feed exhausts at **51 pages — 10,184
    events, 85,949 genuine markets**. So the cap was cutting the exchange
    slightly past half, every scan, since the feed was adopted. Nothing reported
    it, because a truncated walk and a small exchange produce the same list. The
    full walk costs 18s against the capped walk's 10s, which is not a budget
    question. 60 leaves headroom; if the exchange outgrows it, `stats` says so
    rather than quietly returning less.

    `stats` (optional dict) receives `pages`, `events`, and **`complete`** —
    True only when the cursor ran out, i.e. we reached the end of the feed.
    False means we stopped because we hit `max_pages` and there was more.
    Callers that intend to conclude anything from an ABSENCE must check it: on
    an incomplete walk "no candidates found" is a statement about the walk, not
    about the exchange, and this sector has already published that mistake once
    (see iter_markets, and memory `fallback-that-means-act`).
    """
    # Initialised HERE, in a plain function, and the walk itself is the
    # generator below. Written as one generator the whole body — including this
    # init — would not run until the first `next()`, so a caller that built the
    # dict and checked `complete` before iterating would read a missing key and
    # a `.get()` would hand it None. "Not started" must not be reachable as
    # anything other than incomplete.
    s = stats if stats is not None else {}
    s.update({"pages": 0, "events": 0, "complete": False})
    return _walk_events(status, max_pages, limit, s)


def _walk_events(status, max_pages, limit, s):
    cursor = None
    for _ in range(max_pages):
        d = _get("/events", {"limit": limit, "status": status, "cursor": cursor,
                             "with_nested_markets": "true"})
        batch = d.get("events", [])
        s["pages"] += 1
        s["events"] += len(batch)
        for e in batch:
            yield e
        cursor = d.get("cursor")
        if not cursor:
            s["complete"] = True
            return


def iter_markets(status="open", max_pages=25, limit=200):
    """Yield every genuine market, walked through /events.

    THIS FUNCTION USED TO WALK `/markets` DIRECTLY, AND THAT IS WHY THIS SECTOR
    SPENT WEEKS CONCLUDING THE EXCHANGE HAD NO LIQUIDITY.

    Measured 2026-08-17. Walking `/markets` with a cursor returns the multivariate
    parlay shards first, and there are enough of them to swallow any bounded walk:
    60 pages, 60,000 markets, **59,989 of them KXMVECROSSCATEGORY shards** and 11
    real ones. Every scan we have ever run saw only shards. Shards have no book,
    so the sector reported "12,000 markets, 0 with a two-sided book, no business
    here" — and the number was accurate about the population it measured, which
    was not the exchange.

    Through `/events?with_nested_markets=true`, the same hour: 5,000 events,
    48,199 genuine markets, **24,881 passing `two_sided()` unchanged**, 2,977 of
    them in the 0.90-0.99 band. Not one gate was loosened to get there. The
    filters were never too strict; they were being handed inventory.

    The lesson is the one in `fallback-that-means-act`: we read an absence as a
    fact about the world. "No two-sided books exist" and "we are not looking at
    the exchange" produce the same zero, and only one of them is a finding.
    """
    for e in iter_events(status=status, max_pages=max_pages, limit=limit):
        for m in (e.get("markets") or []):
            if not is_shard(m):
                yield m


def iter_markets_flat(status="open", max_pages=12, limit=1000):
    """The raw `/markets` walk. NOT the way to enumerate the exchange.

    Kept because it is the only way to ask "what does the flat endpoint return?",
    which is exactly the question `feed_health()` needs to answer. Do not reach
    for it to find something to trade — see iter_markets for what it returns.
    """
    cursor = None
    for _ in range(max_pages):
        d = _get("/markets", {"limit": limit, "status": status, "cursor": cursor})
        for m in d.get("markets", []):
            yield m
        cursor = d.get("cursor")
        if not cursor:
            return


def feed_health(sample_pages=2):
    """Is the market feed showing us the exchange, or a pile of shards?

    A scan that finds nothing has two very different explanations — the exchange
    is quiet, or we are reading the wrong population — and they call for opposite
    responses. This measures which one is true instead of letting a zero speak
    for itself, and it is why `scan()` can say CANNOT_TELL rather than "no
    opportunities".
    """
    genuine = shards = 0
    for m in iter_markets_flat(max_pages=sample_pages):
        if is_shard(m):
            shards += 1
        else:
            genuine += 1
    seen = genuine + shards
    ev_markets = ev_genuine = 0
    for e in iter_events(max_pages=2):
        for m in (e.get("markets") or []):
            ev_markets += 1
            if not is_shard(m):
                ev_genuine += 1
    return {
        "flat_seen": seen, "flat_genuine": genuine, "flat_shards": shards,
        "flat_shard_pct": round(100.0 * shards / seen, 1) if seen else None,
        "events_markets": ev_markets, "events_genuine": ev_genuine,
        "healthy": ev_genuine > 0,
    }


def get_event(event_ticker):
    return _get(f"/events/{urllib.parse.quote(event_ticker)}")


def is_mutually_exclusive(event):
    """Kalshi's own flag. The whole structural strategy is gated on this.

    Without it, a set of outcomes that can EACH resolve YES will show a price
    sum well over 100c, which is correct pricing and not an arbitrage. Buying
    'the whole field' there is not a locked profit, it is a pile of longs.
    """
    ev = event.get("event", event) if isinstance(event, dict) else {}
    return bool(ev.get("mutually_exclusive"))


# ------------------------------------------------------------- write path --

def armed_for(target_env):
    """True only if data/.kalshi_armed exists AND names this environment.

    Deliberately a file the Chairman creates, not a flag Claude can flip, and
    deliberately environment-specific so arming demo never silently arms live.
    """
    try:
        d = json.loads(ARM_FILE.read_text())
    except Exception:
        return False
    return d.get("env") == target_env and bool(d.get("armed"))


def place_order(**kwargs):
    """Refuses unless explicitly armed. Not wired to a signer yet, on purpose.

    The RSA-PSS signing path needs KALSHI_API_KEY_ID and a private key file that
    only the Chairman installs. Until this firm has measured calibration on the
    read-only path, an order function that works is a liability, not a feature.
    """
    env = env_name()
    if env == "shadow":
        raise KalshiNotArmed(
            "KALSHI_ENV=shadow: this build observes and measures only. "
            "Set KALSHI_ENV=demo and arm data/.kalshi_armed to place demo orders.")
    if not armed_for(env):
        raise KalshiNotArmed(
            f"KALSHI_ENV={env} but data/.kalshi_armed does not authorise '{env}'. "
            f"Arming is a Chairman action.")
    raise KalshiNotArmed(
        f"Armed for {env}, but no request signer is installed. Install "
        f"KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH and implement RSA-PSS "
        f"signing before this can transmit.")


# ------------------------------------------------------------------ report --

def exchange_snapshot(max_pages=25):
    """How much of this exchange is actually a market? Answers with numbers.

    Counts against `two_sided()`, not `tradeable()`. `tradeable()` asks only
    whether an ask exists, and the settled ledger already priced that question:
    86 settlements at an average quote of 0.97 resolved YES 27% of the time.
    A snapshot headline computed on the discredited gate would put a reassuring
    number next to a strategy we know loses money.
    """
    total = booked = 0
    band = []
    for m in iter_markets(max_pages=max_pages):
        total += 1
        t = two_sided(m)
        if not t:
            continue
        booked += 1
        bid, ask, size = t
        if 0.90 <= ask <= 0.99:
            band.append({"ticker": m.get("ticker"), "bid": bid, "ask": ask,
                         "size": size, "breakeven": breakeven_probability(ask),
                         "volume": parse_size(m.get("volume_fp")) or 0.0,
                         "title": str(m.get("title"))[:70],
                         "close_time": m.get("close_time")})
    band.sort(key=lambda x: -x["volume"])
    return {"checked_at": datetime.now(timezone.utc).isoformat(),
            "env": env_name(), "markets_seen": total, "with_real_book": booked,
            "empty_book_pct": round(100.0 * (total - booked) / total, 1) if total else None,
            "near_certainty_band": band}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--pages", type=int, default=12)
    args = ap.parse_args()

    s = exchange_snapshot(max_pages=args.pages)
    print(f"KALSHI EXCHANGE SNAPSHOT — env={s['env']}  {s['checked_at'][:19]}Z")
    print(f"  markets seen      : {s['markets_seen']}")
    print(f"  with a real book  : {s['with_real_book']}")
    print(f"  empty book        : {s['empty_book_pct']}%  <- the filter that matters")
    print(f"  90-99c band       : {len(s['near_certainty_band'])} tradeable")
    for b in s["near_certainty_band"][:10]:
        print(f"    {b['ask']*100:5.1f}c  size={b['size']:>8.0f} "
              f"vol={b['volume']:>9.0f}  {b['title']}")
    if not s["near_certainty_band"]:
        print("    (none — the premise of the strategy is not visible right now)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

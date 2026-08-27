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
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client as kc

DATA_DIR = Path(__file__).resolve().parent / "data"
logger = logging.getLogger("kalshi_calibration")

LEDGER = DATA_DIR / "kalshi_predictions.json"
SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"

# Below this many SETTLED observations the answer is "not yet", never "no edge"
# and never "proven". Chosen because separating 96% from 99% is a 3-point
# question: at n=200 the standard error on a 97% rate is ~1.2 points, which is
# only just inside the gap we are trying to resolve.
MIN_SETTLED_FOR_VERDICT = 200

# The selection rule this sample is a sample OF, written into every row.
#
# Bump this string whenever the scanner's selection changes, and the old rows
# stop counting toward the verdict automatically. That is the point: a hit rate
# is a statement about a rule, so a sample gathered under a rule that no longer
# exists cannot confirm or refute the one that does — memory
# `sample-outlived-its-rule`. The 893 rows recorded on 2026-08-18 under
# `two_sided` alone are honest observations of a band with a median 442 days to
# close, which is not the band the scanner now surfaces.
#
# It is an explicit stamp rather than an inferred one on purpose. The previous
# regime was detected by `yes_bid is not None` — a field that happened to
# correlate with the rule change — and a proxy like that silently stops
# discriminating the moment a third regime arrives, which is exactly what
# happened here.
SELECTION_RULE = "two_sided+band90_99+horizon1_90d+apy>=rf"

# When the rule above went live. Everything before it is a different experiment.
REGIME_START = "2026-08-18"

# How long the current rule gets to produce a measurable sample before "not yet"
# stops being an acceptable answer. This exists because of memory
# `verdict-with-no-consequence`: a gate that can only ever say "keep waiting" is
# a project that runs forever on "we just need more data", and the honest gate
# here is strict enough that the sample may never arrive at all — the scan of
# 2026-08-17 12:23 found ZERO two-sided books in 12,000 open markets. If the
# rate says the sample is decades away, that is an answer about the business,
# not a reason to keep the tab open. At that point it becomes a Chairman
# decision rather than a wait.
SAMPLE_PATIENCE_DAYS = 45
MIN_RATE_TO_BE_WORTH_WAITING = 1.0    # settled current-regime rows per day

# Buckets for the reliability curve. Deliberately fine at the top, because the
# entire strategy lives in the last two.
BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9),
           (0.9, 0.95), (0.95, 0.98), (0.98, 1.0)]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_ledger():
    """Every row that is still evidence.

    Voided rows are filtered here, at the single door into the ledger, rather
    than at each of the seven callers — one of which would eventually be added
    without the filter and would quietly put a known-bad observation back into a
    calibration statistic.

    They are VOIDED and not DELETED on purpose. Deleting would leave a ledger
    that looks like it was always clean, and the fact that this sector once
    scanned parlay shards and recorded one as a near-certainty is exactly the
    kind of thing a future session needs to be able to find. `voided_rows()`
    reads them back for the audit trail.
    """
    return [r for r in _all_rows() if not r.get("voided")]


def _all_rows():
    try:
        d = json.loads(LEDGER.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def voided_rows():
    return [r for r in _all_rows() if r.get("voided")]


def void_row(row_id, reason, actor="system"):
    """Mark one row as no-longer-evidence, with a reason. Idempotent.

    Returns True if this call did the voiding, False if it was already void or
    no such row exists.
    """
    rows = _all_rows()
    for r in rows:
        if r.get("id") != row_id:
            continue
        if r.get("voided"):
            return False
        r["voided"] = True
        r["void_reason"] = reason
        r["voided_at"] = _now()
        r["voided_by"] = actor
        _save_ledger(rows)
        return True
    return False


def _save_ledger(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, indent=2))


# ----------------------------------------------------------------- record --

def record_from_scan(scan=None):
    """Append newly-seen near-certainty observations to the append-only ledger.

    **Idempotent on ticker alone — one row per market, ever.** The dedup key was
    `(ticker, scan date)`, which stops a market being counted four times in one
    day but not four hundred times over its life: a contract open for 400 days
    is re-surfaced by every daily scan, and each row settles to the SAME
    outcome. That is one observation recorded four hundred times, not four
    hundred observations. `MIN_SETTLED_FOR_VERDICT` would then be cleared by a
    handful of genuinely independent markets wearing a large n, and
    `is_edge_proven` would return a confident verdict with a real standard error
    several times the one it printed.

    The first sighting is the prediction, because that is the trade: we would
    have bought it then. Re-recording the same market later at a moved price is
    a different, path-dependent experiment whose rows are not independent of the
    first — so it is not merely double-counting, it is double-counting weighted
    toward whatever stayed open longest, i.e. toward the hardest markets.
    """
    if scan is None:
        try:
            scan = json.loads(SCAN_LOG.read_text())
        except Exception:
            return 0, "no scan log to record from — run kalshi_scanner.py first"

    # `_all_rows`, NOT `_load_ledger`: this function ends in `_save_ledger(rows)`,
    # so it must round-trip the WHOLE file. Handing it the voided-filtered view
    # would write that view back and silently delete every voided row — a
    # remediation that erases its own evidence (memory `fix_that_erased_its_
    # detector`). The dedup set below also has to see voided tickers, or a
    # voided market would be re-recorded on the next scan of the same day.
    rows = _all_rows()
    # (ticker, rule), not ticker, and not (ticker, day) — see the docstring.
    #
    # Ticker alone stops the daily re-count, but it would also mean a market
    # first seen under an OLD rule can never enter the sample of a new one. That
    # is not hypothetical: most of the 893 rows recorded 2026-08-18 sit at 136+
    # days to close, and each will cross into the current rule's 90-day window
    # as time passes. Keyed on ticker alone they would be skipped forever —
    # invisible to the old verdict (wrong rule) and to the new one (already
    # seen). One observation per market PER EXPERIMENT is the honest unit.
    #
    # Voided tickers are in this set too: a row thrown out as bad evidence must
    # not be quietly re-recorded by the next scan under the same rule, which
    # would undo the void.
    have = {(r.get("ticker"), r.get("selection_rule")) for r in rows}
    day = _now()[:10]
    added = 0

    for c in scan.get("disagreement", []):
        if not c.get("ticker"):
            continue
        key = (c["ticker"], SELECTION_RULE)
        if key in have:
            continue
        rows.append({
            "id": f"KAL-{day}-{c['ticker']}",
            "recorded_at": _now(), "recorded_date": day,
            # Which experiment this row belongs to. Read by `_is_current_regime`;
            # without it the row is legacy and never reaches a verdict.
            "selection_rule": SELECTION_RULE,
            "ticker": c["ticker"], "event_ticker": c.get("event_ticker"),
            "title": c.get("title"),
            # The two gate values, so a later audit can re-derive the selection
            # from the row instead of trusting that the scanner applied it.
            "days_to_close": c.get("days_to_close"),
            "annualized_return": c.get("annualized_return"),
            # what the market said
            "market_price": c.get("market_price"),
            "breakeven_probability": c.get("breakeven_probability"),
            # The bid, and the ladder verdict, travel INTO the ledger. They did
            # not before, and the consequence was a law reading its own blind
            # spot back as a finding: law_quoted_prices_have_two_sides reported
            # "0 of 344 rows carry a yes_bid" while the scanner had been
            # recording one on every candidate. The producer stored it, the
            # ledger writer dropped it, and the consumer concluded the market
            # was one-sided. Copy every field a law is going to ask about.
            "yes_bid": c.get("yes_bid"), "spread": c.get("spread"),
            "available_size": c.get("available_size"),
            "coherence": c.get("coherence"),
            "strike_type": c.get("strike_type"),
            "floor_strike": c.get("floor_strike"),
            "cap_strike": c.get("cap_strike"),
            "yes_sub_title": c.get("yes_sub_title"),
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

# How many market lookups one --resolve run may spend. Exported as a constant so
# `kalshi_watchdog` can IMPORT the budget rather than restate it: a supervisor
# carrying its own copy of the number it supervises is `sample-outlived-its-rule`
# with extra steps.
RESOLVE_MAX_LOOKUPS = 400

# How many times the API may answer "this closed market is still not resolved"
# before the row stops competing for a place at the front of the queue.
#
# This exists to stop the close_time ordering below from re-creating, in a new
# shape, the very starvation it fixes. A ticker that is voided or delisted
# upstream never settles, so with a pure close_time sort it holds position 0
# FOREVER and burns one lookup every run. Stuck rows never leave, so the count
# only grows; at even one a day it reaches the 400 budget inside a year and the
# queue starves again — silently, and with every individual number still true.
#
# Ten is chosen against the observed data, not by feel: --resolve runs once a
# day and the settle lag on rows that did settle is p75 +14.8h, max +34.8h. A
# closed market still unresolved after ten daily looks is outside anything this
# sample has ever seen.
STUCK_AFTER_LOOKS = 10


def _resolve_priority(row, now=None):
    """Sort key for the lookup queue: soonest close first, unknown close last.

    A row can only settle after its market resolves, so `close_time` is the only
    field that carries information about whether an answer exists YET. Rows with
    no readable close_time sort last rather than being dropped — unorderable is
    not the same as ineligible.

    Rows that have been asked `STUCK_AFTER_LOOKS` times since closing and still
    have no result sort into a second bucket BEHIND everything else. They are
    demoted, never removed: dropping them would bias the sample toward the
    outcomes we happened to be able to see, which is exactly what
    `resolve_settlements`' own docstring forbids. They still get looked up on any
    run with budget to spare, which is nearly every run once the queue is healthy.
    """
    stuck = 1 if int(row.get("unresolved_looks") or 0) >= STUCK_AFTER_LOOKS else 0
    raw = row.get("close_time")
    if isinstance(raw, str):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (stuck, 0, d)
        except ValueError:
            pass
    return (stuck, 1, datetime.max.replace(tzinfo=timezone.utc))


def _has_closed(row, now):
    """Has this contract's close_time passed? Unknown close_time reads False.

    False is the safe answer for an unreadable date here: it means the row is
    never counted toward STUCK_AFTER_LOOKS and so can never be demoted on the
    strength of a date nobody could parse.
    """
    raw = row.get("close_time")
    if not isinstance(raw, str):
        return False
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d < now


def resolve_candidates(rows, max_lookups=RESOLVE_MAX_LOOKUPS):
    """The unsettled rows worth spending this run's lookups on, best first.

    Pure and network-free so the ORDERING can be tested without an API. The
    ordering is the whole point of this function, and it used to be absent.

    This was `[r for r in rows if unsettled][:max_lookups]` — the first 400 in
    LEDGER order, i.e. ordered by when a row happened to be recorded, a key with
    no bearing on whether Kalshi has published an outcome. With 2,517 pending
    rows against a 400 budget that examined 16% of the ledger per run, and on
    2026-08-22 it had stranded 465 rows that had closed, were unsettled, and had
    never once been looked up. Six of them, probed against the live API that same
    hour, were ALL `status=finalized` with a result sitting there free to read.
    The verdict gate the whole Kalshi campaign waits on (200 settled, 40 per
    horizon) was being starved of observations that already existed.

    SORTED, NOT FILTERED. The obvious-looking version of this fix is to keep only
    rows whose close_time has passed, and it would have been wrong: 209 of the
    341 rows settled so far settled BEFORE their close_time (median -32.2h),
    because Kalshi finalises most of these markets early. A `close_time < now`
    filter would therefore have discarded the majority of real settlements while
    looking like an optimisation. Sorting keeps every row eligible and only
    guarantees that nothing already answerable waits behind something that
    cannot possibly have an answer.
    """
    pending = [r for r in rows if not r.get("settled") and not r.get("voided")]
    return sorted(pending, key=_resolve_priority)[:max_lookups], len(pending)


def resolve_settlements(max_lookups=RESOLVE_MAX_LOOKUPS):
    """Fill in what actually happened. Only touches rows still unsettled.

    A market that cannot be looked up stays unsettled forever rather than being
    dropped or guessed. Dropping it would bias the sample toward outcomes we
    could see, which is exactly how a calibration study reports the number its
    author hoped for.
    """
    # `_all_rows` for the same reason as `record_from_scan`: this ends in
    # `_save_ledger(rows)`. Voided rows are skipped for lookup — a bad
    # observation is not worth an API call — but they must stay in the list
    # that gets written back.
    rows = _all_rows()
    pending, n_pending = resolve_candidates(rows, max_lookups)
    now = datetime.now(timezone.utc)
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
            elif _has_closed(r, now):
                # Counted ONLY after the contract has closed. Before close,
                # "not resolved" is the expected answer and carries no
                # information, so counting it would penalise every long-dated
                # contract for being long-dated: a row closing in 80 days would
                # trip STUCK_AFTER_LOOKS on day 10 while behaving perfectly
                # normally, and the demotion would land on exactly the 30-90d
                # horizon that the verdict gate is currently waiting on.
                r["unresolved_looks"] = int(r.get("unresolved_looks") or 0) + 1
                r["last_lookup_at"] = _now()
        except Exception:
            # A network failure says nothing about the market, so it must not
            # count toward the stuck threshold — that would demote healthy rows
            # for the API's mistakes.
            failed += 1

    _save_ledger(rows)
    # The backlog is reported, not just the successes. When `n_pending` exceeds
    # the budget this run could not have seen every answerable row, and that is
    # the fact a reader needs in order to tell "nothing settled because nothing
    # has resolved" from "nothing settled because we never looked" — the two
    # states this function was previously unable to distinguish in its own log.
    unexamined = max(0, n_pending - len(pending))
    return resolved, (f"{resolved} settled, {failed} lookup failure(s), "
                      f"{sum(1 for r in rows if not r.get('settled') and not r.get('voided'))} "
                      f"still open"
                      + (f" ({unexamined} beyond this run's {max_lookups}-lookup "
                         f"budget, unexamined)" if unexamined else ""))


# ------------------------------------------------------------ measurement --

def wilson(hits, n, z=1.96):
    """(low, high) — 95% Wilson interval on hits/n. ONE definition, three callers.

    `is_premise_alive` computed this inline and `edge_versus_cost` needs the
    identical interval on the identical sample. Two arithmetically-equal copies
    of a formula are the setup for [[both-sides-of-a-comparison]]: they agree
    until someone tunes one of them, and then two functions publish different
    confidence intervals about the same 1,920 rows and neither is obviously
    wrong. Pinned by `test_the_two_intervals_are_the_same_interval`.

    Wilson rather than the normal approximation for the reason `reliability_curve`
    gives: at p near 1.0 the normal interval runs above 100% and makes an
    obviously-broken estimator look statistically fine. This whole sector lives
    at p near 1.0.

    n == 0 returns the whole unit interval — the honest statement that the
    sample says nothing, and the one value that cannot be mistaken for evidence.
    """
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


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


# The horizon strata a verdict must cover, and the minimum settled count in
# each. The bands are the ones the scanner's 1-90 day window spans; the floor of
# 40 is the point where a 97% rate has a 95% CI of roughly +/-5 points — wide,
# but narrow enough to notice a stratum settling at 85% while the others hold.
HORIZON_STRATA = (("1-7d", 1.0, 7.0), ("7-30d", 7.0, 30.0), ("30-90d", 30.0, 91.0))
MIN_SETTLED_PER_HORIZON = 40


def horizon_strata(rows):
    """{stratum: {n, hits, rate}} over settled rows, by days held to close.

    Rows with no `days_to_close` are counted in no stratum rather than dumped
    into the nearest one. They are pre-stratification history, and quietly
    filing them under "1-7d" would be inventing the very fact the strata exist
    to check.
    """
    out = {k: {"n": 0, "hits": 0, "rate": None} for k, _, _ in HORIZON_STRATA}
    for r in rows:
        d = r.get("days_to_close")
        if d is None or r.get("outcome") not in (0, 1):
            continue
        for k, lo, hi in HORIZON_STRATA:
            if lo <= float(d) < hi:
                out[k]["n"] += 1
                out[k]["hits"] += int(r["outcome"])
                break
    for v in out.values():
        v["rate"] = round(v["hits"] / v["n"], 4) if v["n"] else None
    return out


def is_edge_proven(rows=None):
    """(bool, reason). The gate `kalshi_scanner.size_position` consults.

    Returns False for "not yet", for "no", AND for "blocked", with different
    reasons, because those call for three different actions — keep collecting,
    shut it down, or go build the missing piece — and collapsing them into one
    boolean is how a project runs forever on "we just need more data".

    The third one was added 2026-08-24 after the first two proved insufficient
    in exactly the way this docstring warned about: both evidentiary blockers
    discharge with time, so whichever one fires reads as "wait", and the
    permanent blocker was checked last and never reached. Order the refusals by
    what the answer tells you to DO, not by what is cheapest to test.

    Scoped to the CURRENT selection rule, same as `is_premise_alive`. A gate
    that counted the retired `tradeable()` rows toward its sample would clear
    its own size threshold on 216 observations of a rule the scanner no longer
    applies — reaching a verdict sooner by measuring the wrong thing.
    """
    rows = rows if rows is not None else _load_ledger()
    regime = [r for r in rows if _is_current_regime(r)]
    settled = [r for r in regime
               if r.get("settled") and r.get("outcome") in (0, 1)]

    n = len(settled)

    # A STRUCTURAL BLOCKER OUTRANKS AN EVIDENTIARY ONE, however cheap the
    # evidentiary one is to check.
    #
    # This test used to sit at the BOTTOM of the function, behind the sample-size
    # and stratification gates, and was therefore unreachable: those two return
    # first and will keep returning first until 2026-09-21. So for weeks the
    # sector answered "NOT YET, keep collecting" when the true answer — already
    # knowable, from row one — was that **no estimator exists**, and not one of
    # 3,157 current-regime rows carries a `p_hat` (measured 2026-08-24, on the
    # VM, where the ledger lives). Every row records its own reason:
    # `"rationale": "no calibrated estimator installed"`.
    #
    # The difference is not cosmetic, it is the difference between two actions.
    # "NOT YET" says wait — and waiting is free and eventually works. "No
    # estimator" says build something, and no quantity of settlements will ever
    # discharge it. Ordering the cheap-but-temporary blocker ahead of the
    # permanent one is precisely how, in this module's own words, "a project
    # runs forever on 'we just need more data'". The gate distinguished NOT YET
    # from NO and still could not distinguish *waiting* from *stuck*.
    #
    # Fires only on a real denominator, and only when the whole regime sample is
    # empty of estimates — one `p_hat` anywhere means an estimator exists and
    # the question is genuinely evidentiary again, so the checks below resume.
    with_est = sum(1 for r in regime if r.get("p_hat") is not None)
    if len(regime) >= 50 and with_est == 0:
        # "Go build an estimator" is an instruction with a price tag, and this
        # is the only place the firm ever hears it. Sending it without the bar
        # the estimator would have to clear is how a large build gets authorised
        # against a target nobody measured — so the toll rides along on the same
        # sentence. It is measurable today and needs no estimator, which is the
        # entire reason it can be attached to a verdict that says one is missing.
        try:
            _cv, _cwhy, _cd = edge_versus_cost(rows)
            _agg = _cd.get("aggregate")
            bar = ("" if not _agg else
                   f" THE BAR IT WOULD HAVE TO CLEAR: on {_agg['n']} settled "
                   f"rows the market misprices by {_agg['edge']*100:.2f}c/"
                   f"contract and it costs {_agg['cost']['total']*100:.2f}c to "
                   f"take at the CHEAPEST execution that exists "
                   f"({_agg['cost']['spread']*100:.2f}c half-spread + "
                   f"{_agg['cost']['fee']*100:.2f}c maker fee; crossing costs "
                   f"{_agg['cost']['taker']['total']*100:.2f}c), so a PERFECT "
                   f"forecaster nets {_agg['net_point']*100:+.2f}c/contract at "
                   f"the point estimate and {_agg['net_best_case']*100:+.2f}c at "
                   f"the optimistic end of the interval. The build decision is "
                   f"whether an estimator can find more than "
                   f"{_agg['cost']['total']*100:.2f}c — not whether it can find "
                   f"any edge at all. [{_cv}]")
        except Exception:                        # noqa: BLE001
            # A failure to price the toll must not suppress the blocker itself;
            # the blocker is the finding, the bar is the context.
            bar = ""
        return False, (
            f"BLOCKED, NOT WAITING — 0 of {len(regime)} current-regime rows "
            f"carry an estimate of our own ({n} of them settled). The sector "
            f"records the market's price and the probability we would have to "
            f"be right about; it has never recorded a probability WE produced, "
            f"because no calibrated estimator is installed "
            f"(kalshi_scanner.find_disagreement_candidates takes one and is "
            f"always called without it). Collecting more settlements cannot "
            f"change this verdict — the missing input is a component, not an "
            f"observation. Owner: the Chairman, as a build-or-drop decision. "
            f"Note that the arithmetic locks are unaffected: they need no "
            f"estimator, and this gate does not hold them.{bar}")

    if n < MIN_SETTLED_FOR_VERDICT:
        return False, (f"NOT YET — {n}/{MIN_SETTLED_FOR_VERDICT} settled "
                       f"observations. Separating 96% from 99% is a 3-point "
                       f"question and this sample cannot resolve it.")

    # n alone is not enough, because settlements do not arrive in a random
    # order — they arrive SHORTEST-HORIZON FIRST, always. The rule admits
    # markets from 1 to 90 days out, so the first 200 settled rows are drawn
    # almost entirely from the short end, and short-dated near-certainty is the
    # easy case: the event has nearly happened. Declaring PROVEN there and then
    # sizing 60-day positions would be underwriting one horizon with another
    # horizon's evidence — n=200 would be perfectly real and completely
    # inapplicable. So every stratum we would trade must carry its own weight.
    strata = horizon_strata(settled)
    thin = [k for k, v in strata.items() if v["n"] < MIN_SETTLED_PER_HORIZON]
    if thin:
        detail = ", ".join(f"{k}: {strata[k]['n']}" for k in sorted(thin))
        return False, (f"NOT YET — {n} settled, but the sample is not spread "
                       f"across the horizons this rule trades. Under "
                       f"{MIN_SETTLED_PER_HORIZON} settled in {detail}. Short-"
                       f"dated markets settle first and are the easy case; "
                       f"their hit rate does not underwrite longer holds.")

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
    top = [c for c in reliability_curve(settled)
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


ALIVE, DEAD, UNDECIDED = "ALIVE", "DEAD", "CANNOT_TELL"


def _is_current_regime(row) -> bool:
    """Was this row selected by the rule the sector runs today?

    An exact match on the stamp the writer put there, never a guess from the
    row's contents. A row with no stamp is legacy — the conservative direction,
    since a row wrongly called legacy only delays a verdict while a row wrongly
    called current corrupts one.
    """
    return row.get("selection_rule") == SELECTION_RULE


def _regime_age_and_rate(settled_now: int):
    """(days the current rule has been live, settled rows per day).

    Anchored on REGIME_START rather than on the ledger's own earliest row: an
    empty cohort has no earliest row, and "no data yet" is exactly the state
    this needs to be able to time.
    """
    try:
        start = datetime.fromisoformat(REGIME_START).replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - start).total_seconds() / 86400.0)
    except ValueError:
        return 0.0, 0.0
    return round(days, 1), (round(settled_now / days, 3) if days >= 1 else 0.0)


def split_by_regime(rows=None):
    """(current, legacy). Two cohorts that must never share a statistic.

    Every renderer takes the current one. The legacy cohort is shown as a
    labelled footnote, never as a row in the same table — a reader who sees one
    reliability curve will read it as the sector's calibration, whichever rows
    happen to be underneath it.
    """
    rows = rows if rows is not None else _load_ledger()
    return ([r for r in rows if _is_current_regime(r)],
            [r for r in rows if not _is_current_regime(r)])


def _horizon_scope(settled, current):
    """(coverage, caveat) — which horizons a premise verdict actually speaks for.

    `is_edge_proven` already refuses a verdict when the settled sample is not
    spread across the bands the rule trades, and states why twenty lines above:
    settlements arrive shortest-horizon-first, so underwriting a 60-day position
    with 3-day evidence is using one horizon's numbers for another's. That
    argument is about the SAMPLE, not about which function is reading it — but it
    had only ever been applied to one of the two gates.

    `is_premise_alive` was the other one. On 2026-08-22, once the resolver
    backlog drained, it returned DEAD on n=929 rows of which 923 sat in the 1-7d
    band, and phrased it as a verdict on a rule that trades 1-90d. Both numbers
    were true and the sentence around them was about something wider than the
    evidence (`sample-outlived-its-rule`, from the reporting side).

    The verdict is NOT softened here, and deliberately so. The short-dated
    refutation is real, and turning a genuine DEAD into CANNOT_TELL because the
    sample is uneven would be loosening a refusal to make a nicer-looking
    answer. What the caveat does is stop the reader acting on it further than it
    reaches: ALIVE authorises nothing, but DEAD's documented consequence is
    closing the sector, and closing the sector ends the collection that fills
    the untested bands. So the scope is stated where the verdict is read.
    """
    strata = horizon_strata(settled)
    covered = {k: v["n"] for k, v in strata.items()}
    thin = [k for k, _, _ in HORIZON_STRATA
            if covered.get(k, 0) < MIN_SETTLED_PER_HORIZON]
    if not thin:
        return covered, ""

    # Open rows are counted from the whole current cohort, not the settled
    # sample, because the question a reader has here is "is more evidence
    # coming or is this all there will ever be?" — and only the unsettled rows
    # can answer it.
    pending = {k: 0 for k, _, _ in HORIZON_STRATA}
    for r in current:
        if r.get("settled") or r.get("voided"):
            continue
        d = r.get("days_to_close")
        if d is None:
            continue
        for k, lo, hi in HORIZON_STRATA:
            if lo <= float(d) < hi:
                pending[k] += 1
                break

    carried = max(covered, key=lambda k: covered.get(k, 0)) if covered else "?"
    share = (100.0 * covered.get(carried, 0) / len(settled)) if settled else 0.0
    parts = ", ".join(f"{k}: {covered.get(k, 0)} settled / {pending[k]} still open"
                      for k in thin)
    return covered, (
        f" SCOPE: {share:.1f}% of this sample ({covered.get(carried, 0)} of "
        f"{len(settled)}) sits in the {carried} band, and the rule this judges "
        f"trades 1-90d. The bands it does not test — {parts} — are not covered "
        f"by the number above, and settlements arrive shortest-horizon-first, so "
        f"that gap closes on its own. Read this as a verdict on {carried}, not "
        f"on the rule.")


def is_premise_alive(rows=None):
    """(verdict, reason, detail). The question that comes BEFORE is_edge_proven.

    is_edge_proven asks "is our estimate better than the price?" and correctly
    refuses to answer with 0 estimates on file. But that refusal is permanent
    unless someone builds an estimator, and building one is only worth doing if
    the premise underneath the whole sector holds: **that the quoted number is a
    probability at all.** This function answers that, and the data we already
    have answers it definitively — no estimator required.

    Two INDEPENDENT measurements, same discipline as coherence_laws:

      A. SETTLEMENT. What the quote said versus what happened. If the mean quote
         sits above the upper bound of the Wilson interval on the realised rate,
         the quote is not a forecast. This does not need a view of our own — it
         only needs the market's own claim and the outcome.
      B. BOOK. Whether anyone was ever on the other side. A price is a meeting
         of a bid and an ask; a lone ask at 99c with no bid is a number on a
         screen that nobody will trade against, and marking a position to it is
         marking to a wish.

    Three verdicts, not a bool, and deliberately not the same shape as
    is_edge_proven: DEAD is an answer, and folding it into `False` next to
    "not yet" is exactly the collapse is_edge_proven's own docstring warns about.

    THE SAMPLE MUST MATCH THE RULE THAT PRODUCED IT
    -----------------------------------------------
    This function got its own first answer wrong, and the mistake is worth
    keeping written down because it is the house failure mode wearing a new hat.

    It read the whole ledger and returned DEAD on n=216: quoted 97.7%, settled
    YES 39.4%. Both numbers are real. But every one of those 216 rows was
    selected by `kalshi_client.tradeable()`, which asked only "is there an ask?"
    — and `two_sided()` replaced it on 2026-08-17 precisely because an ask with
    no bid is not a price. Not one row in that sample could pass today's gate.
    So the verdict was about a producer that no longer exists, delivered as a
    verdict about the sector as it stands (memory `reader-outlived-its-producer`,
    inverted: here the READER outlived nothing — the SAMPLE did).

    A calibration study that pools two selection rules measures neither. So the
    ledger is partitioned by the rule that produced each row, `is_premise_alive`
    judges only the CURRENT one, and the legacy cohort is reported as what it
    is: a closed finding about a rule already retired.

    The marker is the presence of `yes_bid`. Only the post-fix writer stores it,
    and `two_sided()` cannot admit a row without one, so its presence is proof
    of the current rule and its absence is treated as legacy. That direction
    fails safe: a mislabelled row can only DELAY a verdict, never manufacture
    one.

    This function NEVER authorises a trade. DEAD closes the sector; ALIVE only
    means the prior question stopped blocking, and is_edge_proven still gates
    every dollar.
    """
    rows = rows if rows is not None else _load_ledger()
    current = [r for r in rows if _is_current_regime(r)]
    legacy = [r for r in rows if not _is_current_regime(r)]

    settled = [r for r in current
               if r.get("settled") and r.get("outcome") in (0, 1)
               and r.get("market_price") is not None]

    detail = {"settled": len(settled), "regime": "two_sided",
              "legacy_rows": len(legacy), "current_rows": len(current)}

    # ---- the retired cohort, reported but never mixed in -------------------
    # This is history, not evidence about the live rule. It is carried in the
    # detail so the email can say WHY the gate that produced the current cohort
    # exists, without the two samples ever being averaged together.
    legacy_settled = [r for r in legacy
                      if r.get("settled") and r.get("outcome") in (0, 1)
                      and r.get("market_price") is not None]
    if legacy_settled:
        detail["legacy"] = {
            "settled": len(legacy_settled),
            "mean_quote": round(sum(float(r["market_price"])
                                    for r in legacy_settled) / len(legacy_settled), 4),
            "realised": round(sum(int(r["outcome"])
                                  for r in legacy_settled) / len(legacy_settled), 4),
        }

    if len(settled) < MIN_SETTLED_FOR_VERDICT:
        # The rule is RENDERED, never described. This line read "(two-sided
        # book, spread <= 10c)" — a gate retired two revisions ago — while the
        # rows it counted were selected by horizon and annualized return. A
        # verdict that names the wrong selection rule is the failure in
        # `sample-outlived-its-rule` told from the reporting side: the number is
        # right and the sentence around it is about something else.
        why = (f"CANNOT TELL — {len(settled)}/{MIN_SETTLED_FOR_VERDICT} settled "
               f"quotes selected by the CURRENT rule ({SELECTION_RULE}). The "
               f"premise is judged on outcomes, and this rule has not produced "
               f"enough yet.")
        if "legacy" in detail:
            lg = detail["legacy"]
            why += (f" The {lg['settled']} settled rows from the retired "
                    f"`tradeable()` rule (quoted {lg['mean_quote']:.1%}, settled "
                    f"YES {lg['realised']:.1%}) are NOT evidence here — none of "
                    f"them could pass today's gate, which is why the gate "
                    f"changed. Pooling them would measure neither rule.")

        # "Not yet" has to expire. Otherwise this reads CANNOT_TELL every week
        # forever and the heartbeat email becomes the sound of a decision nobody
        # is making.
        days, rate = _regime_age_and_rate(len(settled))
        detail.update({"regime_days": days, "settled_per_day": rate})
        if days >= SAMPLE_PATIENCE_DAYS and rate < MIN_RATE_TO_BE_WORTH_WAITING:
            eta = ("never at this rate" if rate <= 0 else
                   f"~{(MIN_SETTLED_FOR_VERDICT - len(settled)) / rate:.0f} more days")
            why += (f" DECISION DUE: the current rule has been live {days} days "
                    f"and has produced {len(settled)} settled observation(s) "
                    f"({rate:.2f}/day, {eta}). The sample is not late, it is not "
                    f"coming — a gate honest enough to reject a one-sided book "
                    f"rejects essentially this entire exchange. Whether to keep "
                    f"the sector open is a Chairman decision, not a wait.")
        return UNDECIDED, why, detail

    # ---- A. settlement ----------------------------------------------------
    n = len(settled)
    hits = sum(int(r["outcome"]) for r in settled)
    realised = hits / n
    mean_quote = sum(float(r["market_price"]) for r in settled) / n
    # Was computed inline here. `edge_versus_cost` needs the SAME interval on
    # the SAME rows, and a second transcription of the algebra is a divergence
    # waiting for someone to tune one copy — see `wilson`.
    ci_low, ci_high = wilson(hits, n)

    # ---- B. the book -------------------------------------------------------
    # Independent of settlement, and it must be measured on the SAME cohort.
    # Note what this can and cannot say now: every current-regime row carries a
    # bid by construction, so "did anyone quote the other side" is answered YES
    # trivially and is NOT corroboration. What is still informative is how far
    # the bid sits from the ask — a 97c ask against a 90c bid brackets a range
    # the quote does not pin down.
    spreads = [float(r["spread"]) for r in settled if r.get("spread") is not None]
    mean_spread = sum(spreads) / len(spreads) if spreads else None

    # Which horizons this sample actually speaks for. Attached to every terminal
    # verdict below, including ALIVE: an unscoped ALIVE invites someone to build
    # an estimator on evidence that covers a fraction of the rule's range.
    coverage, scope = _horizon_scope(settled, current)

    # ---- C. where in the band the damage is --------------------------------
    # One mean quote against one interval is a true sentence that hides which
    # prices are broken, and the three buckets do not fail alike. Measured
    # 2026-08-24 on n=1258: 0.90-0.95 quoted 92.0% / settled 89.0% (CI high
    # 91.4%) — outside; 0.95-0.98 quoted 96.0% / settled 94.9% (CI 92.4-96.7%)
    # — INSIDE, i.e. fairly priced; 0.98-1.00 quoted 98.6% / settled 95.0%
    # (CI high 96.9%) — outside by 1.7 points, the worst bucket on the board.
    #
    # The aggregate ("2.5 points rich") invites precisely the wrong response:
    # move up the band to be safer. Up the band is where it is worst, because
    # above 95c the quote stops carrying information (0.95-0.98 and 0.98-1.00
    # both settle ~95%) while the price keeps climbing. That is a fact about
    # the marginal cent, and no mean can express it — [[an-aggregate-is-not-a-
    # finding]]. Attached to ALIVE as well as to both DEADs: an ALIVE aggregate
    # sitting on top of one broken bucket is the case where somebody goes and
    # builds an estimator for the bucket that cannot be beaten.
    bad, fair = [], []
    for c in reliability_curve(settled):
        if not c["n"] or c["mean_forecast"] is None or c["ci_high"] is None:
            continue
        (bad if c["mean_forecast"] > c["ci_high"] or
             (c["ci_low"] is not None and c["mean_forecast"] < c["ci_low"])
         else fair).append(
            f"{c['bucket']} (n={c['n']}, quoted {c['mean_forecast']:.1%}, "
            f"settled {c['observed']:.1%})")
    if bad:
        buckets = (f" BY BUCKET, the mean hides which prices are broken — "
                   f"outside its own interval: {'; '.join(bad)}."
                   + (f" Inside it: {'; '.join(fair)}." if fair else
                      " No bucket in this sample is fairly priced."))
    elif fair:
        buckets = (f" BY BUCKET, every bucket sits inside its own interval: "
                   f"{'; '.join(fair)} — so the aggregate is not concealing a "
                   f"broken price at one end of the band.")
    else:
        buckets = (" BY BUCKET: no bucket carried enough settled rows to test, "
                   "so the aggregate above is unaudited at the price level.")

    detail.update({"mean_quote": round(mean_quote, 4),
                   "realised": round(realised, 4),
                   "ci_low": round(ci_low, 4), "ci_high": round(ci_high, 4),
                   "horizon_coverage": coverage,
                   "horizon_scoped": bool(scope),
                   "buckets_outside_ci": len(bad), "buckets_inside_ci": len(fair),
                   "mean_spread": round(mean_spread, 4) if mean_spread else None})

    # ---- D. the toll -------------------------------------------------------
    # A premise verdict decides whether the quote is a PROBABILITY. It does not
    # decide whether the gap is worth collecting — and both of its terminal
    # answers get read as instructions about an estimator: DEAD says do not
    # build one, ALIVE says one is worth building. Neither is safe to act on
    # without the number below, because a mispricing smaller than the spread you
    # must cross is not an opportunity at any level of forecasting skill. This
    # is attached to ALL THREE verdicts on purpose: the expensive mistake is an
    # ALIVE that sends someone to build a forecaster for a 1.7c edge behind a
    # 2.2c toll, and that reading is exactly the one that would never see it.
    cost_verdict, cost_why, cost_detail = edge_versus_cost(rows)
    detail["cost_verdict"] = cost_verdict
    detail["cost"] = cost_detail.get("aggregate")
    detail["cost_settled"] = cost_detail.get("settled")
    toll = f" COST: {cost_why}"

    if mean_quote > ci_high:
        book = (f" Mean bid-ask spread {mean_spread:.3f} — the book bracketed a "
                f"range the quote did not pin down." if mean_spread else "")
        return DEAD, (
            f"DEAD on n={n} rows selected by the CURRENT two-sided rule — the "
            f"market quoted a mean of {mean_quote:.1%} and settled YES "
            f"{realised:.1%} of the time (95% CI {ci_low:.1%}-{ci_high:.1%}). "
            f"The quote is not above the interval by a little; it is outside it "
            f"entirely, so these numbers are not probabilities and there is "
            f"nothing for an estimator to beat.{book}{buckets}{scope}{toll}"), detail

    if mean_quote < ci_low:
        return DEAD, (
            f"DEAD on n={n} — inverted: quoted {mean_quote:.1%}, settled "
            f"{realised:.1%} (95% CI {ci_low:.1%}-{ci_high:.1%}). Systematically "
            f"cheap in this band is still a broken price, not free money; the "
            f"reason a 97c contract settles 100% of the time is that nobody "
            f"will sell it to you.{buckets}{scope}{toll}"), detail

    return ALIVE, (f"ALIVE on n={n} — quoted {mean_quote:.1%}, settled "
                   f"{realised:.1%} (95% CI {ci_low:.1%}-{ci_high:.1%}); the "
                   f"quote is inside its own interval. This clears the PRIOR "
                   f"question only. is_edge_proven still gates every "
                   f"dollar.{buckets}{scope}{toll}"), detail


# ---------------------------------------------------------------------------
# THE EDGE VERSUS THE COST OF TAKING IT
# ---------------------------------------------------------------------------
HARVESTABLE, MARGINAL, UNHARVESTABLE = "HARVESTABLE", "MARGINAL", "UNHARVESTABLE"


def _cost_per_contract(quote, mean_spread):
    """What one contract costs to put on, priced in BOTH execution regimes.

    RESTING AND CROSSING ARE ONE CHOICE, NOT TWO INDEPENDENT DIALS. A resting
    order that fills pays half the spread AND the maker rate; an order that
    crosses pays the whole spread AND the taker rate. The first draft of this
    function charged the maker's half-spread and the taker's fee — 4× the fee
    that goes with the spread assumption beside it — and called the result
    "optimistic by construction". It was not optimistic, it was mixed, and a
    mixed cost is the [[both-sides-of-a-comparison]] failure priced in dollars:
    each half correct, the comparison meaningless. Neither row below is a mix.

    The VERDICT is computed against `maker` — the cheapest execution that
    exists on this exchange. That is deliberate. This function exists to bound a
    NEGATIVE claim ("the edge does not clear the cost"), and a negative claim
    proved against the cheapest possible cost is as strong as it gets. Proving
    it against a pessimistic cost would prove nothing.

    But `maker` is not free money either, and the reason is adverse selection,
    not fees: a bid resting at the mid on a 95c contract fills when someone is
    willing to hit it, which is disproportionately when the price is about to
    fall. `two_sided()` exists because 94.4% of this exchange has no book at
    all, so "we rested and got filled at the mid" is a strong assumption about
    a thin book, not a neutral one. `taker` is carried alongside so the gap
    between the two is visible rather than assumed away.

    Both fee terms are UN-ROUNDED, unlike `kalshi_client.fee_cents`, which
    rounds up to a whole cent PER ORDER: a 1-contract order at 95c pays 1c
    where the rate implies 0.33c. The un-rounded rate is the large-order limit
    — it assumes we always trade in enough size to amortise the round-up away,
    which is again the generous reading.

    Returns components, not just a total: a single number would be an aggregate
    nobody could argue with ([[an-aggregate-is-not-a-finding]]), and which of
    the two terms binds decides what you would even do about it — a spread-bound
    sector is a liquidity problem, a fee-bound one is a size problem.
    """
    def _leg(spread_cost, rate):
        fee = rate * quote * (1.0 - quote)
        return {"spread": round(spread_cost, 5), "fee": round(fee, 5),
                "total": round(spread_cost + fee, 5)}

    maker = _leg(mean_spread / 2.0, kc.MAKER_FEE_RATE)
    taker = _leg(mean_spread, kc.TAKER_FEE_RATE)
    return {"maker": maker, "taker": taker,
            # `total` is the maker leg. Every verdict below reads this key, so
            # the bound is the cheap one by default and nothing has to remember
            # to pick it.
            "spread": maker["spread"], "fee": maker["fee"],
            "total": maker["total"]}


def edge_versus_cost(rows=None):
    """(verdict, why, detail) — the question both other gates leave open.

    `is_premise_alive` asks whether the quote is a probability. `is_edge_proven`
    asks whether OUR estimate beats it. Neither asks the one that decides
    whether the sector can pay for itself: **is the mispricing bigger than the
    bid-ask spread you must cross to collect it?**

    That gap is not academic. `is_edge_proven` returns BLOCKED with the
    instruction to go build a calibrated estimator, and `is_premise_alive`'s
    ALIVE branch is the same instruction by another route. Both send someone to
    build a forecaster. Neither of them has ever compared what a PERFECT
    forecaster could win against what the trade costs — and measured on the live
    ledger 2026-08-26 (n=1,920 current-regime settled rows) those two numbers
    are 1.73c and 1.92c, the second being the CHEAPEST execution that exists
    (rest at the mid, maker rate, size enough to amortise the fee round-up;
    crossing costs 4.01c). An estimator is a large build aimed at a target the
    best available evidence puts BELOW the toll.

    Needs no estimator of our own, which is the point: it is answerable today,
    from rows already on file, and it stays answerable however long
    `is_edge_proven` remains blocked.

    FOUR VERDICTS, and the middle one is the honest place to be:
      UNHARVESTABLE  the whole confidence interval on the edge sits below cost.
                     No estimator can clear it — a close-the-sector finding.
      MARGINAL       the POINT estimate is below cost but the interval reaches
                     above it. Not currently paying; would need the true edge
                     near the top of its interval AND perfect execution.
      HARVESTABLE    the whole interval clears cost.
      CANNOT_TELL    too few settled rows to bound the edge at all.

    Reporting MARGINAL as UNHARVESTABLE would be the overclaim this module has
    made before ([[verdict-wider-than-its-sample]]): on 2026-08-26 the aggregate
    point estimate is -0.19c/contract and the upper end of the interval is
    +1.02c, so "no estimator can ever pay" is NOT what the data says, however
    much the point estimate looks like it. Note which term carries it — 1.84c of
    the 1.92c is SPREAD, 0.08c is fee. This is a liquidity problem wearing a
    cost problem's clothes, and the only lever that moves it is a deeper book.

    Scoped to the CURRENT selection rule and partitioned by horizon, same
    discipline as the two gates beside it — the retired `tradeable()` cohort
    quoted 97.7% and settled 45.8%, and pooling it here would manufacture an
    enormous edge out of a rule that no longer exists.
    """
    rows = rows if rows is not None else _load_ledger()
    current = [r for r in rows if _is_current_regime(r)]
    settled = [r for r in current
               if r.get("settled") and r.get("outcome") in (0, 1)
               and r.get("market_price") is not None
               and r.get("spread") is not None]

    detail = {"settled": len(settled), "regime": "two_sided",
              "selection_rule": SELECTION_RULE, "strata": {}}

    def _measure(sample):
        n = len(sample)
        if not n:
            return None
        hits = sum(int(r["outcome"]) for r in sample)
        realised = hits / n
        quote = sum(float(r["market_price"]) for r in sample) / n
        spread = sum(float(r["spread"]) for r in sample) / n
        lo, hi = wilson(hits, n)
        # The mispricing is |quote - realised|; its interval is the quote (a
        # constant, measured exactly) against each end of the interval on the
        # realised rate. When that interval STRADDLES the quote the mispricing
        # could genuinely be zero, and the low end must say zero rather than
        # report a positive distance to the nearer bound — that distance is the
        # smaller of two errors, not a floor on the edge.
        ends = (abs(quote - lo), abs(quote - hi))
        straddles = (quote - lo) * (quote - hi) <= 0
        edge_lo = 0.0 if straddles else min(ends)
        cost = _cost_per_contract(quote, spread)
        return {"n": n, "quote": round(quote, 4), "realised": round(realised, 4),
                "ci_low": round(lo, 4), "ci_high": round(hi, 4),
                "mean_spread": round(spread, 4),
                "edge": round(abs(quote - realised), 5),
                "edge_low": round(edge_lo, 5), "edge_high": round(max(ends), 5),
                "cost": cost,
                "net_point": round(abs(quote - realised) - cost["total"], 5),
                "net_best_case": round(max(ends) - cost["total"], 5),
                "direction": ("market OVERPRICES yes" if quote > realised
                              else "market UNDERPRICES yes")}

    for key, lo_d, hi_d in HORIZON_STRATA:
        band = [r for r in settled
                if r.get("days_to_close") is not None
                and lo_d <= float(r["days_to_close"]) < hi_d]
        m = _measure(band)
        if m:
            detail["strata"][key] = m

    agg = _measure(settled)
    if agg is None or len(settled) < MIN_SETTLED_FOR_VERDICT:
        return UNDECIDED, (
            f"CANNOT TELL — {len(settled)}/{MIN_SETTLED_FOR_VERDICT} settled "
            f"rows carrying BOTH an outcome and a recorded spread under the "
            f"current rule ({SELECTION_RULE}). The edge cannot be bounded "
            f"tightly enough to compare against a ~2c cost."), detail

    detail["aggregate"] = agg
    cost, edge = agg["cost"], agg["edge"]
    if agg["edge_high"] <= cost["total"]:
        verdict = UNHARVESTABLE
        head = (f"UNHARVESTABLE — even the most favourable reading of "
                f"{agg['n']} settled rows puts the mispricing at "
                f"{agg['edge_high']*100:.2f}c, below the "
                f"{cost['total']*100:.2f}c it costs to take it. No estimator "
                f"can clear this; the missing piece is not a forecast.")
    elif agg["edge_low"] > cost["total"]:
        verdict = HARVESTABLE
        head = (f"HARVESTABLE — the mispricing is at least "
                f"{agg['edge_low']*100:.2f}c against a {cost['total']*100:.2f}c "
                f"cost, across the whole interval on {agg['n']} settled rows.")
    else:
        verdict = MARGINAL
        head = (f"MARGINAL — the market misprices by {edge*100:.2f}c/contract "
                f"(95% CI {agg['edge_low']*100:.2f}-{agg['edge_high']*100:.2f}c) "
                f"and it costs {cost['total']*100:.2f}c to collect by "
                f"RESTING: {cost['maker']['spread']*100:.2f}c of half-spread "
                f"plus {cost['maker']['fee']*100:.2f}c of maker fee. At the point "
                f"estimate that is {agg['net_point']*100:+.2f}c/contract — a "
                f"loss. It only pays if the true edge sits near the top of its "
                f"interval ({agg['net_best_case']*100:+.2f}c there) AND every "
                f"order rests instead of crossing.")

    # Which of the two costs binds is the actionable half. A spread-bound sector
    # is a liquidity problem (rest orders, or trade a deeper book); a fee-bound
    # one is a size problem (trade bigger, or not at all).
    binds = "the spread" if cost["spread"] > cost["fee"] else "the fee"
    head += (f" {binds.capitalize()} is the binding cost "
             f"({cost['spread']*100:.2f}c vs {cost['fee']*100:.2f}c).")

    # A MAGNITUDE WITH NO SIGN IS NOT ACTIONABLE. The first version of this said
    # "the market misprices by 1.73c" and never said which way — `direction` was
    # computed, stored in the detail dict, and left out of the only sentence a
    # human reads. That is not cosmetic here, because every candidate this firm
    # can produce is a BUY: `find_disagreement_candidates` prices the ask,
    # takes its size from the ask book, and emits no side; all 4,239 ledger rows
    # carry no `side` key at all. So a reader handed an unsigned edge and this
    # pipeline takes the only side there is — and on the aggregate that is the
    # losing one. [[both-sides-of-a-comparison]]: the number was right and the
    # sentence it lived in could not be acted on.
    overpriced = agg["quote"] > agg["realised"]
    harvest = "SELL yes (equivalently, buy no)" if overpriced else "BUY yes"
    head += (f" DIRECTION: {agg['direction']} — quoted "
             f"{agg['quote']*100:.2f}c against {agg['realised']*100:.2f}c "
             f"settled, so collecting it means {harvest}.")
    if overpriced:
        head += (" The scanner has no such path: every candidate it emits is a"
                 " buy of yes at the ask. The gap above is on the side the firm"
                 " cannot currently take, which is a build decision separate"
                 " from — and cheaper than — the estimator one.")

    # A sign is a property of a horizon, not of the sector. Only bands with a
    # real n AND an interval that clears the quote get a vote: a straddling
    # band's "direction" is the sign of noise, and counting it would let the
    # aggregate borrow confidence from a stratum that has none.
    signed = {k: v for k, v in detail["strata"].items()
              if v["n"] >= MIN_SETTLED_PER_HORIZON and v["edge_low"] > 0}
    over = sorted(k for k, v in signed.items() if v["quote"] > v["realised"])
    under = sorted(k for k, v in signed.items() if v["quote"] <= v["realised"])
    if over and under:
        head += (f" AND THE SIGN FLIPS ACROSS HORIZONS: yes is overpriced at "
                 f"{', '.join(over)} and underpriced at {', '.join(under)}, so "
                 f"no one direction applies to the rule as a whole.")
    else:
        head += (f" That sign is separately established on {len(signed)} of "
                 f"{len(detail['strata'])} horizon strata "
                 f"({', '.join(sorted(signed)) or 'none'}) — elsewhere the "
                 f"interval straddles the quote or the band is too thin, so "
                 f"it is that band's sign, not the rule's.")

    # Both cost terms are the BEST case. Say so on every verdict, including
    # HARVESTABLE — especially HARVESTABLE, where an optimistic cost is what
    # would send real money after a margin that is not there.
    head += (f" This is priced at the CHEAPEST execution that exists: resting "
             f"and filling at the mid, at the maker rate, in size large enough "
             f"to amortise Kalshi's per-order fee round-up (a 1-contract order "
             f"at 95c pays 1c, not {cost['fee']*100:.2f}c). CROSSING instead "
             f"costs {cost['taker']['total']*100:.2f}c "
             f"({cost['taker']['spread']*100:.2f}c full spread + "
             f"{cost['taker']['fee']*100:.2f}c taker fee), "
             f"{cost['taker']['total']/cost['total']:.1f}x the bound above — and "
             f"no verdict here is computed against that. Resting is also not "
             f"free: a bid at the mid on a near-certainty contract fills when "
             f"someone wants to hit it, which is when the price is about to "
             f"fall.")

    thin = sorted(k for k, v in detail["strata"].items()
                  if v["n"] < MIN_SETTLED_PER_HORIZON)
    if thin:
        named = ", ".join("%s (n=%d)" % (k, detail["strata"][k]["n"])
                          for k in thin)
        head += (f" Horizon strata too thin to speak for themselves: {named} "
                 f"— the aggregate is carried by the band that settles fastest, "
                 f"and this verdict does not extend to the others.")

    # An aggregate that says "does not pay" can conceal a band that does, and
    # the strata table this function feeds prints a NET column with nothing
    # beside it. Measured 2026-08-26: 7-30d nets +1.28c/contract on n=68 (above
    # the per-horizon bar) while the aggregate reads -0.44c, and 30-90d nets
    # +31.54c on n=5. Both look like the answer and neither is: the first has an
    # interval that straddles the quote, so its edge is not distinguishable from
    # zero; the second is five rows.
    #
    # [[an-aggregate-is-not-a-finding]] cuts in this direction too — the mean is
    # not automatically the conservative reading. Name the exception, and say in
    # the same breath which of the three things it is, because a reader who
    # sizes off that column is acting on a point estimate with no lower bound.
    payers = sorted(k for k, v in detail["strata"].items() if v["net_point"] > 0)
    if payers:
        parts = []
        for k in payers:
            v = detail["strata"][k]
            if v["n"] < MIN_SETTLED_PER_HORIZON:
                note = (f"n={v['n']} is below the {MIN_SETTLED_PER_HORIZON}-row "
                        f"bar — noise, not a band that pays")
            elif v["edge_low"] <= 0:
                note = ("its interval STRADDLES the quote, so that edge is not "
                        "distinguishable from zero")
            elif v["edge_low"] > v["cost"]["total"]:
                note = "its whole interval clears the cost — the one to look at"
            else:
                note = "but only at the point estimate; the interval reaches below cost"
            parts.append(f"{k} nets {v['net_point']*100:+.2f}c/contract "
                         f"(n={v['n']}) — {note}")
        head += f" NOT UNIFORM ACROSS HORIZONS: {'; '.join(parts)}."
    return verdict, head, detail


VERDICT_STATE = DATA_DIR / "kalshi_verdict_notified.json"
VERDICT_HEARTBEAT_DAYS = 7


def email_verdict(rows=None, force=False):
    """Buffer the sector's verdict to Jack. Returns (sent, why_or_why_not).

    WHEN IT SENDS: on a CHANGE of verdict, and otherwise once every
    VERDICT_HEARTBEAT_DAYS. Those are two different jobs and both are needed.

    The change trigger is the news. The heartbeat exists because the verdict has
    read DEAD for days and will keep reading DEAD, and a sector that mails only
    on change is indistinguishable — from Jack's inbox — from a sector whose
    cron entry was deleted. Every silent-failure postmortem in this repo has the
    same shape: the absence of output was read as the absence of a problem.

    Sent through email_buffer, never smtplib: it joins the one daily digest that
    mail_guard permits. ~20 scripts opening their own connection is what buried
    the real alerts in July.
    """
    rows = rows if rows is not None else _load_ledger()
    premise, why_premise, detail = is_premise_alive(rows)
    proven, why_edge = is_edge_proven(rows)
    # The toll is a THIRD verdict and it moves independently of the other two:
    # the premise can stay DEAD while the spread narrows enough to turn
    # UNHARVESTABLE into MARGINAL, or MARGINAL into HARVESTABLE. Leaving it out
    # of the signature would make "this sector might now pay for itself" wait up
    # to VERDICT_HEARTBEAT_DAYS and then arrive as part of a routine heartbeat,
    # which is the shape of every silent-failure postmortem in this repo: the
    # news existed, nothing was watching the field it lived in.
    cost_verdict, why_cost, cost_detail = edge_versus_cost(rows)
    signature = (f"{premise}|{'PROVEN' if proven else 'NOT_PROVEN'}"
                 f"|{cost_verdict}")

    try:
        prior_text = VERDICT_STATE.read_text()
        st = json.loads(prior_text)
        prior, last_at = st.get("signature"), st.get("at")
        prior_subject = st.get("subject", "")
        prior_buffered = st.get("buffered_at")
    except (OSError, ValueError, AttributeError):
        prior_text, prior, last_at = None, None, None
        prior_subject, prior_buffered = "", None

    stale = True
    if last_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
            stale = age.days >= VERDICT_HEARTBEAT_DAYS
        except (TypeError, ValueError):
            stale = True

    subject = (f"Kalshi verdict: premise {premise}, edge "
               f"{'PROVEN' if proven else 'not proven'}, cost {cost_verdict} "
               f"(n={detail.get('settled', 0)} settled)")

    if not force and signature == prior and not stale:
        # "I buffered it" is not "Jack read it". The verdict is enqueued at
        # 11:45 UTC and carried by the 22:00 digest ten hours later; stamping
        # this file at enqueue time asserted a delivery nothing had confirmed,
        # so one failed digest would have silenced the sector for a week while
        # the state file said it had reported. Ask the receipt instead.
        try:
            from email_buffer import delivery_state
            state, why_delivery = delivery_state(prior_buffered, prior_subject)
        except Exception:                        # noqa: BLE001
            state, why_delivery = "unknown", "delivery state unavailable"
        if state != "lost":
            return False, (f"unchanged ({signature}), last queued {last_at} "
                           f"[{state}: {why_delivery}]")
        logger.warning(f"kalshi verdict was never delivered ({why_delivery}) "
                       f"— re-queueing rather than staying silent")

    try:
        VERDICT_STATE.write_text(json.dumps({
            "signature": signature, "at": _now(),
            # What was enqueued, and when — the two facts delivery_state needs
            # to tell "still in tonight's digest" from "dropped on the floor".
            "subject": subject, "buffered_at": datetime.now().isoformat(),
            "delivery": "buffered",
        }))
    except OSError:
        pass

    curve = "".join(
        f"<tr><td style='padding:4px 10px'>{c['bucket']}</td>"
        f"<td align='right' style='padding:4px 10px'>{c['n']}</td>"
        f"<td align='right' style='padding:4px 10px'>{c['mean_forecast']:.3f}</td>"
        f"<td align='right' style='padding:4px 10px'><b>{c['observed']:.3f}</b></td>"
        f"<td align='right' style='padding:4px 10px'>"
        f"[{c['ci_low']:.3f}, {c['ci_high']:.3f}]</td></tr>"
        for c in reliability_curve(split_by_regime(rows)[0]) if c["n"])

    # Read the last scan for the coherence split. Absent is reported as absent —
    # a missing scan log is a stopped scanner, which Jack wants to hear about.
    try:
        scan = json.loads(SCAN_LOG.read_text())
        coh = (f"Last scan {scan.get('scanned_at', '?')[:16]}: "
               f"{scan.get('markets_seen', 0)} markets, "
               f"{scan.get('two_sided', 0)} with a two-sided book, "
               f"{len(scan.get('disagreement', []))} coherent candidate(s), "
               f"{len(scan.get('dropped_incoherent', []))} dropped incoherent, "
               f"{len(scan.get('unchecked_coherence', []))} uncheckable.")
    except (OSError, ValueError):
        coh = ("No readable scan log — the scanner may have stopped. That is "
               "itself the finding.")

    # The toll gets its own paragraph rather than a line inside the premise
    # paragraph. It answers a different question from either gate above — not
    # "is the quote a probability" and not "is our estimate better", but "is the
    # gap bigger than the fare" — and it is the only one of the three that is
    # answerable today with no estimator installed.
    body = (
        f"<p><b>PREMISE: {premise}</b><br>{why_premise}</p>"
        f"<p><b>EDGE: {'PROVEN' if proven else 'NOT PROVEN'}</b><br>{why_edge}</p>"
        f"<p><b>COST: {cost_verdict}</b><br>{why_cost}</p>"
        f"<p>{coh}</p>"
        f"<table style='border-collapse:collapse;font-family:system-ui,sans-serif;"
        f"font-size:13px'>"
        f"<tr><th align='left' style='padding:4px 10px'>quoted band</th>"
        f"<th align='right' style='padding:4px 10px'>n</th>"
        f"<th align='right' style='padding:4px 10px'>quoted</th>"
        f"<th align='right' style='padding:4px 10px'>settled YES</th>"
        f"<th align='right' style='padding:4px 10px'>95% CI</th></tr>{curve}</table>"
        f"<p style='color:#666;font-size:12px'>Nothing in the Kalshi sector can "
        f"place an order: kalshi_client.place_order() raises unless "
        f"data/.kalshi_armed exists, and only the Chairman creates that file. "
        f"This email is a measurement, not a recommendation.</p>")

    try:
        from email_buffer import buffer_email
        # False means "urgent — I did not buffer it, you send it". This caller
        # has no direct sender, so that return is a MISS, not a success.
        buffered = buffer_email(subject=subject, html_body=body,
                                sender_name="Kalshi Sector",
                                key_metrics={"premise": premise,
                                             "edge_proven": proven,
                                             "cost": cost_verdict,
                                             "settled": detail.get("settled", 0),
                                             "realised": detail.get("realised"),
                                             "mean_quote": detail.get("mean_quote"),
                                             "net_per_contract": (
                                                 (cost_detail.get("aggregate") or {})
                                                 .get("net_point"))})
        if buffered is False:
            # Nothing was transmitted, so put the dedupe record back — otherwise
            # the state file asserts a delivery that never happened and the
            # verdict stays silent until the signature changes or the heartbeat
            # expires. There is no flood risk in retrying a send that sends
            # nothing.
            try:
                if prior_text is None:
                    VERDICT_STATE.unlink(missing_ok=True)
                else:
                    VERDICT_STATE.write_text(prior_text)
            except OSError:
                pass
            logger.error("kalshi verdict classified URGENT — not buffered and "
                         "this caller cannot transmit. NOT SENT.")
            return False, "classified urgent; no direct sender here"
        # "queued", not "sent": buffer_email enqueues. Saying sent here is how
        # a log line comes to assert a delivery nobody witnessed.
        return True, f"queued for the daily digest ({signature})"
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"kalshi verdict email failed: {e}")
        return False, f"buffer failed: {e}"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--email", action="store_true",
                    help="buffer the verdict to the daily digest on change, "
                         "or on the weekly heartbeat")
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

    cur_rows, legacy_rows = split_by_regime(rows)
    print(f"\n  reliability curve — CURRENT rule only "
          f"({SELECTION_RULE}, {len(cur_rows)} row(s))")
    print(f"    {'bucket':<12}{'n':>6}{'forecast':>10}{'observed':>10}"
          f"{'95% CI':>18}")
    for c in reliability_curve(cur_rows):
        if not c["n"]:
            print(f"    {c['bucket']:<12}{0:>6}{'-':>10}{'-':>10}{'-':>18}")
            continue
        ci = f"[{c['ci_low']:.3f}, {c['ci_high']:.3f}]"
        print(f"    {c['bucket']:<12}{c['n']:>6}{c['mean_forecast']:>10.4f}"
              f"{c['observed']:>10.4f}{ci:>18}")

    # The retired cohort, named and kept out of the table above. Every row of it
    # was picked by `tradeable()`, which required only an ask; `two_sided()`
    # replaced it because an ask with no bid is not a price. Its numbers are a
    # finding about that rule, not about this one.
    lg_settled = [r for r in legacy_rows
                  if r.get("settled") and r.get("outcome") in (0, 1)]
    if lg_settled:
        n = len(lg_settled)
        print(f"\n  retired cohort (`tradeable()`, ask-only — NOT counted above): "
              f"{n} settled, "
              f"quoted {sum(float(r['market_price']) for r in lg_settled)/n:.1%}, "
              f"settled YES {sum(int(r['outcome']) for r in lg_settled)/n:.1%}")

    # The prior question first. is_edge_proven cannot resolve without an
    # estimator, and there is no point building one if this reads DEAD.
    premise, why_premise, pd = is_premise_alive(rows)
    print(f"\n  PREMISE: {premise}")
    print(f"  {why_premise}")

    # The toll, before the edge gate — because the edge gate's answer is
    # "go build an estimator" and this is the number that decides whether that
    # build is worth starting.
    cost_verdict, why_cost, cd = edge_versus_cost(rows)
    print(f"\n  COST: {cost_verdict}")
    print(f"  {why_cost}")
    if cd.get("strata"):
        print(f"\n    {'horizon':<10}{'n':>6}{'quote':>9}{'settled':>9}"
              f"{'edge':>9}{'cost':>9}{'net':>9}")
        for key, _lo, _hi in HORIZON_STRATA:
            m = cd["strata"].get(key)
            if not m:
                continue
            print(f"    {key:<10}{m['n']:>6}{m['quote']:>9.4f}"
                  f"{m['realised']:>9.4f}{m['edge']:>9.4f}"
                  f"{m['cost']['total']:>9.4f}{m['net_point']:>+9.4f}")

    ok, why = is_edge_proven(rows)
    print(f"\n  VERDICT: {'PROVEN' if ok else 'NOT PROVEN'}")
    print(f"  {why}")
    print("\n  Until this reads PROVEN, kalshi_scanner.size_position returns 0 "
          "contracts\n  for every DISAGREEMENT candidate. Structural locks are "
          "unaffected —\n  they are arithmetic and need no estimator.")

    if args.email:
        sent, why = email_verdict(rows)
        print(f"\n  email  : {'buffered' if sent else 'held'} — {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

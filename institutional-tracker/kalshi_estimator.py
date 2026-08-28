#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI ESTIMATOR
================================
The missing component. `kalshi_scanner.py` has produced complete, tradeable
candidates for months — 964 in the last live scan — each carrying
`requires_estimate: true`, `p_hat: null`, `rationale: "no calibrated estimator
installed"`. Every downstream gate (`kalshi_calibration.is_edge_proven`, the
board memo, the arm file) then reported BLOCKED, and the block was read as
"waiting for more settlements". It was not. 2,352 rows have settled and not one
carries an estimate of our own, because nothing in the firm produced one. More
settlements could never have changed that.

This module produces one. It is deliberately the smallest estimator that can be
honest, and it refuses rather than guesses.


WHAT IT ESTIMATES, AND WHY THAT IS THE RIGHT TARGET
---------------------------------------------------
Not "will SpaceX launch 12 rockets". We have no edge on that and never will.
What we can estimate is the firm's OWN SELECTION BIAS, which is a property of
our scanner, not of the world — and which is therefore both knowable and ours.

The scanner admits a market when its quoted price sits in [0.90, 0.99]. Where
the book is wide, that "price" is a noisy midpoint of a thin or stale book.
Selecting on `price >= 0.90` therefore preferentially admits markets whose
measurement error points UP. The wider the book, the larger the noise, the
larger the selection bias — a winner's curse on our own filter. The sign and
the monotonicity are predicted by the mechanism BEFORE looking at outcomes,
which is what separates this from a slice found by fishing.

Measured on 2,007 settled current-regime rows (2026-08-28):

    spread      n    realized - price
    1-2c      419         -0.22c
    2-3c      416         +0.10c
    3-5c      610         -1.37c
    5-10c     494         -3.64c
    >=10c      68         -7.88c

so the correction is `p_hat = p_market + (a + b * spread)`, with b < 0.


THE PARAMETER THE WHOLE SECTOR REDUCES TO
------------------------------------------
Wider spread means more bias, but spread IS the cost of crossing it — you pay
half of it. So the bias only pays if it grows FASTER than half the spread. The
entire build-or-drop question is one slope against one threshold:

    bias(cents) = +0.603 - 0.6033 * spread(cents)
    break-even slope = -0.50
    cluster-robust 95% CI (1,186 events) = [-1.167, -0.040]

The point estimate clears the bar. The interval does not: -0.50 sits inside it,
t = -0.36. So the correct reading is NOT "there is edge" and NOT "there is no
edge" — it is that the sector is worth running in shadow at a measured cost,
and is not yet worth arming.


THE TWO RESULTS THAT ACTUALLY DECIDE WHAT TO BUILD
---------------------------------------------------
Pricing the slope against the firm's real fee schedule (`kalshi_client`, maker
0.0175 / taker 0.07, un-rounded) turns one ambiguous slope into two findings
that are not ambiguous at all. Mean net cents per contract, 2,007 settled rows:

    spread      n     BUY/rest  SELL/rest   BUY/cross  SELL/cross
    1-2c      419       -0.80c     -0.36c      -1.53c      -1.10c
    2-3c      416       -0.98c     -1.19c      -2.23c      -2.44c
    3-5c      610       -3.17c     -0.44c      -5.14c      -2.40c
    5-10c     494       -7.07c     +0.21c     -10.67c      -3.39c
    >=10c      68      -12.97c     +2.80c     -18.23c      -2.46c

1. THE SCANNER'S OWN ACTION LOSES, EVERYWHERE. Buying these near-certainties is
   negative in all five buckets and gets monotonically worse with spread. The
   sector was built to buy; buying is the losing side of it, and no amount of
   further settlement data will rescue a column that is negative at n=2,007.

2. CROSSING CANNOT WORK, ON EITHER SIDE. Every cell of both crossing columns is
   negative. This is structural, not statistical: the spread we would pay IS
   the mispricing we would be harvesting, so an order that crosses hands back
   the entire edge and then some. Any fill obtained by crossing is a LOSS, not
   a smaller gain — which is why `signal()` emits `execution: "REST_ONLY"` as
   data rather than as advice.

What survives is one cell: RESTING A SELL on wide-spread contracts. Over the
562 legs with spread >= 5c (414 events) it is +0.52c per contract, cluster
bootstrap 95% CI [-2.25c, +3.65c], P(>0) = 0.62. That is a lean, not an edge,
and it is measured under the generous assumption that a resting order fills at
the mid without adverse selection — on a book where 94.4% of markets have no
book at all. So this module does not stop here: it books every signal into a
shadow ledger, records requested-vs-filled, and lets settlements decide.


WHAT THIS MODULE WILL NOT DO
-----------------------------
* It never arms anything and never places an order. `data/.kalshi_armed` is
  Jack's file and is created by Jack. This module writes a shadow book.
* `estimate()` returns None — never a number — when it cannot honestly produce
  one: no fit, stale fit, a spread outside the fitted support, or a regime
  string that no longer matches the one the fit was trained on. A degraded read
  that substitutes a constant is a decision disguised as a default; see memory
  `fallback-that-means-act`, where 999 and 350 both silently read as "go".
* It refuses to serve estimates at all unless the fit BEAT THE MARKET
  out-of-sample under walk-forward validation with an embargo. An estimator
  that cannot beat the price it is correcting has nothing to add, and saying so
  is the useful output.

Run:  python3 kalshi_estimator.py --fit       (refit from settled history)
      python3 kalshi_estimator.py --validate  (walk-forward, embargoed)
      python3 kalshi_estimator.py --report    (what it would trade today)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict

DATA = Path(__file__).resolve().parent / "data"
PREDICTIONS = DATA / "kalshi_predictions.json"
FIT_FILE = DATA / "kalshi_estimator_fit.json"
SHADOW_BOOK = DATA / "kalshi_shadow_book.json"

# The selection rule the fit is valid for. A fit is a statement about the
# population a rule admits; change the rule and the sample is describing a
# population that no longer exists. `sample-outlived-its-rule` is the memory —
# a real n, a real CI, and a verdict about a selection rule that had been
# replaced. Stored in the fit file and checked on every estimate.
REGIME = "two_sided+band90_99+horizon1_90d+apy>=rf"

# Fee rates are NOT redefined here. The first cut of this file carried its own
# `FEE_COEF = 0.07` and a `MAKER_FEE_FLOOR`, which is a second source for a
# number the firm already owns — and it was wrong twice over: 0.07 is the TAKER
# rate (maker is 0.0175, 4x cheaper), and the "floor" hard-coded the 1c
# per-ORDER round-up as if it were per-contract, which overstates the fee by 3x
# at 95c and by 12x against the maker rate. Charging a taker fee beside a maker
# half-spread is precisely the mixed cost `_cost_per_contract` postmortems.
# One source: kalshi_client.
from kalshi_client import MAKER_FEE_RATE, TAKER_FEE_RATE  # noqa: E402

# Below this many settled rows the slope is not worth reporting, let alone
# trading. Deliberately independent of kalshi_calibration's thresholds: this
# gates whether an ESTIMATE may be produced, that one gates whether an EDGE is
# proven, and collapsing them would let a thin fit satisfy a verdict.
MIN_FIT_ROWS = 400
MIN_VALIDATION_FOLDS = 3

# --------------------------------------------------------------------------
# HOW LONG THIS TAKES, DERIVED RATHER THAN HOPED
# --------------------------------------------------------------------------
# The single most important number in this file, and the one most likely to be
# wished away. A leg is a binary that pays 0 or 100c, so at a 95c price its
# payoff SD is sqrt(0.95*0.05)*100 = 21.8c before costs; measured on the 562
# settled wide-spread legs it is 29.1c per leg and 30.4c per EVENT (events are
# the independent unit — 1.36 legs each).
#
# The mean we are hunting is +0.52c. So the per-trade signal-to-noise ratio is
# 0.52/30.4 = 0.017, and the sample needed to resolve it at 95% is
#
#     n_events = (1.96 * 30.4 / effect)^2
#              = 14,229 events for a 0.5c effect     (~279 days at ~51/day)
#              =  3,558 events for a 1.0c effect     (~70 days)
#              =    890 events for a 2.0c effect     (~137 days at the ~6.5
#                                                     wide-spread events/day
#                                                     that actually produce it)
#
# This is why the sector cannot be declared profitable "shortly", and the
# reason is arithmetic rather than effort: we are looking for a sub-cent mean
# under 30c of per-trade noise. Nothing about running the scanner harder
# changes that ratio. What WOULD change it is a larger effect — which is the
# case for booking only the wide-spread subset and for reviewing the threshold
# against the shadow book, not for lowering the bar to reach a verdict sooner.
#
# MIN_SHADOW_EVENTS is therefore a FLOOR on computing a verdict at all, not the
# point at which one becomes likely. `shadow_verdict` returns UNDECIDED for a
# long time after this floor, and that is the correct output.
MIN_SHADOW_EVENTS = 200
SHADOW_BOOTSTRAP = 20000

# Walk-forward embargo. Rows are recorded days before they settle, so a naive
# time split lets a training row's OUTCOME land after a test row was recorded —
# the label leaks backwards.
#
# The first cut of this file used a flat EMBARGO_DAYS = 21. That is a round
# number picked to look conservative, and it was wrong in both directions: too
# long for a contract that settles in 6 hours, too short for one that settles in
# 80 days, and — decisively — unsatisfiable on a regime only 8 days old, which
# is why `--validate` reported "0 usable folds" rather than a verdict. A gap
# nobody can meet does not make an estimator careful, it makes it silent.
#
# The constraint is not calendar distance, it is INFORMATION AVAILABILITY, and
# the data already carries it: `settled_at` is the moment a training row's label
# became public. So a row may train a fold only if it had settled before the
# earliest row in that fold's test window was recorded. That is exact rather
# than approximate, it derives from the process instead of being chosen round,
# and it tightens automatically for fast contracts and loosens for slow ones.
# The 21-day constant is kept only as the FLOOR on total span (below), because
# an 8-day sample cannot say anything about a 90-day horizon no matter how the
# folds are cut.
MIN_REGIME_SPAN_DAYS = 21

# A fit older than this is not applied. The scanner's universe turns over and a
# selection bias is a fact about a book, not a law of nature.
FIT_MAX_AGE_DAYS = 30


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _outcome(row: dict) -> Optional[int]:
    """1/0, or None when the row cannot vote. Never defaults to 0.

    A missing outcome read as NO is how a resolver bug becomes a 19-cent
    'edge': on 2026-08-28 the 345 pre-regime rows realised 45.8% at a 97.7c
    mean price purely because they had never been resolved properly.
    """
    o = row.get("outcome")
    if o is None:
        return None
    if isinstance(o, bool):
        return 1 if o else 0
    t = str(o).strip().lower()
    if t in ("yes", "y", "true", "1"):
        return 1
    if t in ("no", "n", "false", "0"):
        return 0
    return None


def _is_number(v) -> bool:
    """True only for a real, finite number.

    `isinstance(x, float)` is True for NaN, and NaN passes every `is not None`
    guard while failing every threshold, so bad data becomes silence rather
    than an alarm. See memory `nan-is-not-none`.
    """
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _parse_ts(s) -> Optional[dt.datetime]:
    """Timezone-aware UTC, or None. Never naive.

    `.replace("Z", "+00:00")` is the correct py3.9 spelling of a trailing Z and
    is used deliberately here; the bug is dropping the offset, not keeping it.
    """
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fee_per_contract(price: float, cross: bool = True) -> Optional[float]:
    """Kalshi's per-contract fee at a given price, in dollars. None if unpriced.

    Un-rounded, i.e. the large-order limit. Kalshi rounds up to a whole cent
    per ORDER, so a 1-contract trade at 95c pays 1c against a maker rate that
    implies 0.083c; that round-up is amortised away in size and is a position-
    sizing constraint, not a rate. Returning the rate here and handling size
    where size is decided keeps one fact in one place.
    """
    if not _is_number(price):
        return None
    return (TAKER_FEE_RATE if cross else MAKER_FEE_RATE) * price * (1.0 - price)


def round_trip_cost(row: dict, cross: bool = True) -> Optional[float]:
    """Cost in DOLLARS per contract to take this row's position, or None.

    RESTING AND CROSSING ARE ONE CHOICE. Crossing pays the WHOLE spread and the
    taker rate; resting pays half and the maker rate. Mixing the two halves is
    the cost-model bug `kalshi_calibration._cost_per_contract` already
    postmortems, so this function takes one flag and moves both terms together.

    Measured 2026-08-28 on 2,007 settled rows, mean net per contract by spread
    bucket — the reason `signal()` is REST-ONLY:

        spread      n     BUY/rest  SELL/rest   BUY/cross  SELL/cross
        1-2c      419       -0.80c     -0.36c      -1.53c      -1.10c
        2-3c      416       -0.98c     -1.19c      -2.23c      -2.44c
        3-5c      610       -3.17c     -0.44c      -5.14c      -2.40c
        5-10c     494       -7.07c     +0.21c     -10.67c      -3.39c
        >=10c      68      -12.97c     +2.80c     -18.23c      -2.46c

    Every CROSSING cell is negative, on both sides, in every bucket. That is
    not a close call and it is not a sample-size question: crossing this book
    cannot be profitable, because the spread we would pay IS the mispricing we
    would be harvesting. Only two cells are positive and both are resting sells
    of wide-spread contracts.

    Resting is not free either, and the part that is missing is adverse
    selection: a bid resting on a 95c contract fills disproportionately when
    the price is about to fall. This module has no data to price that, so the
    maker row is an UPPER BOUND on what is achievable, not an estimate of it —
    which is why the shadow book records requested-vs-filled rather than
    assuming the fill.
    """
    px, spread = row.get("market_price"), row.get("spread")
    fee = fee_per_contract(px, cross=cross)
    if not (_is_number(px) and _is_number(spread) and fee is not None):
        return None
    return (spread if cross else spread / 2.0) + fee


# --------------------------------------------------------------------------
# execution — the assumption that decides the verdict
# --------------------------------------------------------------------------
# `round_trip_cost(cross=False)` charges `spread/2 + fee` unconditionally. Half
# a spread is not a fee. It is the price of one particular FILL MODEL — "our
# offer only gets hit if we improve all the way to the mid" — and on the 2,113
# settled legs measured 2026-08-28 that single unmeasured assumption is the
# whole verdict:
#
#     resting at the ask   net +2.06c/contract at a 5c spread   (t ~ +3.0)
#     resting at the mid   net -0.44c/contract at a 5c spread   (t ~ +0.5)
#
# Same edge, same rule, same sample; opposite conclusions. So the cost of an
# execution is parameterised here rather than assumed, and the missing number
# — how often an offer at a given price actually gets lifted — is MEASURED by
# the quote track below rather than substituted for. A constant substituted for
# a quantity nobody measured is a decision, not a default (memory
# `fallback-that-means-act`).
#
# `round_trip_cost` itself is left alone: it is the pessimistic bound, several
# consumers and tests depend on it, and a bound that silently got cheaper would
# be exactly the wrong kind of improvement.


def execution_cost_cents(row: dict,
                         improvement_cents: Optional[float] = None) -> Optional[float]:
    """Cents/contract to REST a sell that gives up `improvement_cents` of price.

    `market_price` is the ASK (`kalshi_scanner.py`: `"market_price": ask`), so
    an offer joining at the ask gives up nothing and pays only the maker fee.
    `improvement_cents=None` means "improve to the mid" — the spread/2 that
    `round_trip_cost(cross=False)` charges unconditionally.

    Returns CENTS. `round_trip_cost` returns dollars; this file has already had
    one 1.83c-vs-0.0183 unit collision and the name is doing the work of
    preventing the second.
    """
    px, spread = row.get("market_price"), row.get("spread")
    fee = fee_per_contract(px, cross=False)
    if not (_is_number(px) and _is_number(spread) and fee is not None):
        return None
    s = spread * 100.0
    d = s / 2.0 if improvement_cents is None else float(improvement_cents)
    # Below the bid is not an improvement, it is a cross, and crossing is priced
    # by `round_trip_cost(cross=True)` at the taker rate. Refuse rather than
    # quietly return a resting cost for an order that could not rest.
    if not (0.0 <= d <= s):
        return None
    return d + fee * 100.0


def break_even_spread(f: dict, price: float,
                      improvement_cents: Optional[float] = None) -> dict:
    """Spread (cents) above which a resting SELL pays, derived from the fit.

    SIGN DISCIPLINE, which is where this went wrong before. `fit()` regresses
    `(outcome - market_price)`: that is the BUY side. The resting sell earns its
    negative. The comment that justified `SHADOW_MIN_SPREAD_C` set the BUY-side
    expression equal to the SELL-side cost and got 0.547c; done correctly on the
    same fit shape the answer is 10.95c at the mid — an order of magnitude, and
    on the other side of the live threshold.

        gross_sell(s) = -(a + b*s)
        cost(s)       = d + fee          (fixed improvement d)
                      = s/2 + fee        (improve to the mid)
        net(s) = 0  ->  s = (a + fee + d) / (-b - m),  m = 0.5 at the mid else 0

    A non-positive denominator is not a large break-even, it is NO break-even:
    cost rises with spread at least as fast as the mispricing does, so the arm
    loses at every spread. Say that instead of returning a big number nobody
    reads the sign of.
    """
    if not isinstance(f, dict):
        return {"ok": False, "reason_code": "NO_FIT", "why": "no fit supplied"}
    a = f.get("intercept_cents")
    b = f.get("slope_cents_per_cent_spread")
    fee = fee_per_contract(price, cross=False)
    if not (_is_number(a) and _is_number(b) and fee is not None):
        return {"ok": False, "reason_code": "NO_FIT",
                "why": "fit is missing intercept/slope, or price is unusable"}
    at_mid = improvement_cents is None
    d = 0.0 if at_mid else float(improvement_cents)
    denom = -b - (0.5 if at_mid else 0.0)
    if denom <= 0:
        return {"ok": False, "reason_code": "NEVER_PROFITABLE",
                "why": ("cost rises with spread at least as fast as the bias "
                        "(slope %.4f, cost slope %.2f) — no break-even exists"
                        % (b, 0.5 if at_mid else 0.0)),
                "execution": "mid" if at_mid else "improve_%gc" % d}
    s = (a + fee * 100.0 + d) / denom
    lo, hi = (f.get("spread_support_cents") or [None, None])[:2]
    in_support = bool(_is_number(lo) and _is_number(hi) and lo <= s <= hi)
    out = {"ok": True, "spread_cents": s, "in_support": in_support,
           "execution": "mid" if at_mid else "improve_%gc" % d,
           "support_cents": [lo, hi], "fee_cents": fee * 100.0}
    if not in_support:
        # Not a footnote. A break-even above the widest spread ever observed
        # means the arm cannot be profitable anywhere it will actually trade,
        # and that is the finding, not a caveat to one.
        out["reason_code"] = "OUTSIDE_SUPPORT"
        out["why"] = ("break-even %.2fc lies outside the observed spread "
                      "support %s — extrapolation, and unreachable in practice"
                      % (s, [lo, hi]))
    return out


def execution_curve(f: dict, price: float,
                    spreads: Optional[List[float]] = None) -> dict:
    """Net cents/contract by execution mode — the table, never one cell of it.

    The edge is reported in every mode because the mode is the open question.
    Quoting the at-ask number alone would be the mirror of the mistake
    `round_trip_cost` makes by quoting the at-mid one.
    """
    modes = [("at_ask", 0.0), ("improve_1c", 1.0), ("improve_2c", 2.0),
             ("at_mid", None)]
    if not isinstance(f, dict) or not _is_number(f.get("slope_cents_per_cent_spread")):
        return {"ok": False, "reason_code": "NO_FIT"}
    a = f["intercept_cents"]
    b = f["slope_cents_per_cent_spread"]
    fee_c = (fee_per_contract(price, cross=False) or 0.0) * 100.0
    spreads = spreads if spreads is not None else [2.0, 5.0, 8.0]
    rows = []
    for name, d in modes:
        be = break_even_spread(f, price, improvement_cents=d)
        cells = {}
        for s in spreads:
            give = s / 2.0 if d is None else d
            cells["%gc" % s] = (-(a + b * s)) - give - fee_c if give <= s else None
        rows.append({"execution": name, "break_even": be, "net_cents": cells})
    return {"ok": True, "price": price, "fee_cents": fee_c,
            "intercept_cents": a, "slope_cents_per_cent_spread": b,
            "spread_support_cents": f.get("spread_support_cents"),
            "modes": rows,
            # The number the system should be built around: at a given spread,
            # give up more than this and the mispricing is gone. Reported PER
            # SPREAD, not as a max across spreads — the affordable improvement
            # rises with the spread, so a single max would quote the widest
            # book's allowance as if it applied to the narrowest, which is the
            # sampling-rule mistake in memory `budget-spent-in-the-wrong-order`
            # wearing a different hat.
            "max_affordable_improvement_cents": {
                "%gc" % s: (-(a + b * s)) - fee_c for s in spreads}}


def load_rows(path: Path = PREDICTIONS) -> List[dict]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def settled_regime_rows(rows: List[dict]) -> List[dict]:
    """Settled, un-voided, current-regime rows carrying every field the fit needs."""
    out = []
    for r in rows:
        if r.get("voided") or not r.get("settled"):
            continue
        if r.get("selection_rule") != REGIME:
            continue
        if _outcome(r) is None:
            continue
        if not (_is_number(r.get("market_price")) and _is_number(r.get("spread"))):
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------------
# the fit
# --------------------------------------------------------------------------
def _ols(xs: List[float], ys: List[float]) -> Optional[Tuple[float, float, float]]:
    """(intercept, slope, residual_var) for y ~ a + b x, or None if degenerate."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    return a, b, sum(e * e for e in resid) / max(n - 2, 1)


def _cluster_se(xs, ys, a, b, clusters) -> Optional[float]:
    """Cluster-robust SE of the slope, clustering on event_ticker.

    The legs of one event share a single real-world outcome, so treating each
    leg as an independent observation understates the SE — here by ~31%
    (0.219 -> 0.288), which is the difference between an interval that excludes
    the break-even slope and one that contains it. The honest interval is the
    clustered one and it is the one this module reports.
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    by = {}
    for x, y, c in zip(xs, ys, clusters):
        by.setdefault(c, []).append((x, y))
    meat = 0.0
    for _, pts in by.items():
        u = sum((x - mx) * (y - (a + b * x)) for x, y in pts)
        meat += u * u
    return math.sqrt(meat) / sxx


def fit(rows: Optional[List[dict]] = None) -> Optional[dict]:
    """Fit the selection-bias correction. Returns the fit dict, or None.

    Everything is in CENTS, because that is the unit the costs are quoted in
    and mixing the two is how a 1.83c half-spread gets compared against a 0.0183
    edge.
    """
    rows = settled_regime_rows(rows if rows is not None else load_rows())
    if len(rows) < MIN_FIT_ROWS:
        return None

    xs = [r["spread"] * 100.0 for r in rows]
    ys = [(_outcome(r) - r["market_price"]) * 100.0 for r in rows]
    clusters = [r.get("event_ticker") or r.get("ticker") or "?" for r in rows]

    got = _ols(xs, ys)
    if got is None:
        return None
    a, b, s2 = got

    n = len(xs)
    mx = sum(xs) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    se_iid = math.sqrt(s2 / sxx) if sxx > 0 else None
    se_cl = _cluster_se(xs, ys, a, b, clusters)
    se = se_cl if se_cl is not None else se_iid

    return {
        "fitted_at": _now().isoformat(),
        "regime": REGIME,
        "n": n,
        "n_clusters": len(set(clusters)),
        "intercept_cents": a,
        "slope_cents_per_cent_spread": b,
        "se_slope_iid": se_iid,
        "se_slope_clustered": se_cl,
        "slope_ci95": [b - 1.96 * se, b + 1.96 * se] if se else None,
        # Support of the fit. Estimating outside it is extrapolation, and the
        # widest bucket (>=10c, n=68) is exactly where extrapolation would be
        # most tempting and least supported.
        "spread_support_cents": [min(xs), max(xs)],
        # -0.50 is the break-even: half the spread is what crossing costs.
        "beats_crossing_cost_point_estimate": b < -0.50,
        "beats_crossing_cost_at_95": bool(se and (b + 1.96 * se) < -0.50),
    }


# --------------------------------------------------------------------------
# walk-forward validation, embargoed
# --------------------------------------------------------------------------
def validate(rows: Optional[List[dict]] = None, folds: int = 5) -> dict:
    """Out-of-sample Brier of p_hat vs the market's own Brier.

    The market price is the baseline that matters. A correction to a price is
    only worth applying if the corrected number is closer to the truth than the
    number it corrected — anything else is motion. Plain KFold on time-ordered
    rows leaks the future; folds here are forward in time and a training row is
    admitted only if it had SETTLED before the test window opened, so no
    training label can postdate the test row that it helped price.
    """
    rows = settled_regime_rows(rows if rows is not None else load_rows())
    dated = [(_parse_ts(r.get("recorded_at")), _parse_ts(r.get("settled_at")), r)
             for r in rows]
    dated = sorted([t for t in dated if t[0] is not None and t[1] is not None],
                   key=lambda t: t[0])
    if len(dated) < MIN_FIT_ROWS:
        return {"ok": False, "why": f"only {len(dated)} dated rows, need {MIN_FIT_ROWS}"}

    span = (dated[-1][0] - dated[0][0]).days
    if span < MIN_REGIME_SPAN_DAYS:
        # Reported as its own reason, not folded into "not enough folds". The
        # two are different states and demand different actions: too-few-folds
        # is a cutting problem, too-short-a-span is a WAITING problem, and the
        # only honest instruction is a date. Collapsing them is how "blocked on
        # more settlements" got read for months when the real block was that
        # nothing produced an estimate at all.
        need = (dated[0][0] + dt.timedelta(days=MIN_REGIME_SPAN_DAYS)).date()
        return {"ok": False, "reason_code": "REGIME_TOO_YOUNG",
                "why": f"regime spans {span}d of the {MIN_REGIME_SPAN_DAYS}d "
                       f"minimum ({len(dated)} rows); earliest possible "
                       f"verdict {need.isoformat()}",
                "span_days": span, "n_rows": len(dated),
                "earliest_verdict_date": need.isoformat()}

    n = len(dated)
    step = n // (folds + 1)
    if step <= 0:
        return {"ok": False, "why": "too few rows to fold"}

    results = []
    for k in range(1, folds + 1):
        cut = step * k
        window_open = dated[cut][0]           # earliest test row's recorded_at
        # Information availability, not calendar distance: a row may train this
        # fold only if its label was already public when the test window opened.
        train = [r for _, settled, r in dated[:cut] if settled <= window_open]
        test = [r for _, _, r in dated[cut:cut + step]]
        if len(train) < 100 or len(test) < 30:
            continue
        f = fit(train)
        if f is None:
            continue
        b_model = b_market = 0.0
        used = 0
        for r in test:
            y = _outcome(r)
            px = r["market_price"]
            p = _apply(f, r)
            if p is None:
                continue
            b_model += (p - y) ** 2
            b_market += (px - y) ** 2
            used += 1
        if used < 30:
            continue
        results.append({
            "fold": k, "n_train": len(train), "n_test": used,
            "brier_model": b_model / used, "brier_market": b_market / used,
            "improvement": (b_market - b_model) / used,
        })

    if len(results) < MIN_VALIDATION_FOLDS:
        return {"ok": False, "why": f"only {len(results)} usable folds, need "
                                   f"{MIN_VALIDATION_FOLDS}", "folds": results}

    mean_imp = sum(r["improvement"] for r in results) / len(results)
    wins = sum(1 for r in results if r["improvement"] > 0)
    return {
        "ok": True,
        "folds": results,
        "n_folds": len(results),
        "mean_brier_improvement": mean_imp,
        "folds_improved": wins,
        # The gate. A majority of folds must improve AND the mean must be
        # positive. One lucky fold is not a result, and a positive mean carried
        # by a single fold is the shape that produces confident wrong answers.
        "beats_market": bool(mean_imp > 0 and wins > len(results) / 2.0),
    }


# --------------------------------------------------------------------------
# applying the fit
# --------------------------------------------------------------------------
def _apply(f: dict, row: dict) -> Optional[float]:
    """p_hat from a fit dict and a row, or None. No clamping to a default."""
    if not f:
        return None
    px, spread = row.get("market_price"), row.get("spread")
    if not (_is_number(px) and _is_number(spread)):
        return None
    lo, hi = f.get("spread_support_cents") or (None, None)
    s_cents = spread * 100.0
    if not (_is_number(lo) and _is_number(hi)) or not (lo <= s_cents <= hi):
        return None  # extrapolation, not estimation
    bias = (f["intercept_cents"] + f["slope_cents_per_cent_spread"] * s_cents) / 100.0
    p = px + bias
    # A probability outside [0,1] is the model telling us it does not apply
    # here. Clipping would hide that; refusing surfaces it.
    return p if 0.0 <= p <= 1.0 else None


def load_fit() -> Optional[dict]:
    try:
        f = json.loads(FIT_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(f, dict):
        return None
    if f.get("regime") != REGIME:
        return None  # fit describes a population the scanner no longer admits
    ts = _parse_ts(f.get("fitted_at"))
    if ts is None or (_now() - ts).days > FIT_MAX_AGE_DAYS:
        return None
    return f


def estimate(row: dict, f: Optional[dict] = None) -> Optional[float]:
    """THE entry point. p_hat for one scanner row, or None.

    None is a real answer and must be propagated as one. Every caller that
    turns it into a number invents an opinion the firm does not hold.
    """
    f = f if f is not None else load_fit()
    if f is None:
        return None
    if not f.get("validated_beats_market"):
        # Fitted but not proven better than the price it corrects. Refusing is
        # the honest output; a correction that loses to the market is noise
        # wearing a decimal point.
        return None
    return _apply(f, row)


def signal(row: dict, f: Optional[dict] = None) -> Optional[dict]:
    """A tradeable signal for one row, or None if there is nothing to do.

    The rule is a MARGIN, not a level: trade only where the estimated
    mispricing exceeds what it costs to touch the market. The book-wide
    average mispricing (1.60c against a 1.91c cost) is the number that made
    this sector look dead, and it is the wrong number — nobody has to trade
    the average. What decides is whether ANY subset clears its own cost.
    """
    p = estimate(row, f)
    if p is None:
        return None
    px = row.get("market_price")
    rest = round_trip_cost(row, cross=False)
    cross = round_trip_cost(row, cross=True)
    if not (_is_number(px) and _is_number(rest) and _is_number(cross)):
        return None
    edge = p - px                      # negative => YES is dear => buy NO
    net_rest = abs(edge) - rest
    if net_rest <= 0:
        return None
    return {
        "ticker": row.get("ticker"),
        "event_ticker": row.get("event_ticker"),
        "title": row.get("title"),
        "side": "NO" if edge < 0 else "YES",
        # Not decoration. The measured taker table is negative in every bucket
        # on both sides, so a fill obtained by crossing does not earn a smaller
        # edge — it earns a NEGATIVE one. An order that may not be crossed is a
        # different instruction from an order that should not be, and the
        # consumer has to be able to tell. Carried as data, and pinned by
        # `test_signal_never_claims_a_crossable_edge`.
        "execution": "REST_ONLY",
        "market_price": px,
        "p_hat": p,
        "edge_cents": edge * 100.0,
        "cost_cents_rest": rest * 100.0,
        "cost_cents_cross": cross * 100.0,
        "net_cents": net_rest * 100.0,
        "net_cents_if_crossed": (abs(edge) - cross) * 100.0,
        "spread_cents": (row.get("spread") or 0) * 100.0,
        "available_size": row.get("available_size"),
        "days_to_close": row.get("days_to_close"),
        "close_time": row.get("close_time"),
    }


# --------------------------------------------------------------------------
# shadow book — the only thing that can break the deadlock
# --------------------------------------------------------------------------
# `estimate()` refuses until the fit beats the market out-of-sample; validation
# needs settled rows scored by a fit; and if the only writer of those rows were
# `signal()`, nothing would ever be recorded and the refusal would be permanent
# and self-inflicted. That is the same shape as the block this module was built
# to remove: 2,352 rows sat "waiting for more settlements" while the real cause
# was that no producer existed. Building a second one would be unforgivable.
#
# So booking is deliberately gated on the mechanical RULE, not on the fit. The
# rule is fixed in advance, in code, from the mechanism (`SHADOW_MIN_SPREAD_C`
# below) — it cannot drift toward whatever happens to have worked, because a
# threshold that moves with its results is fitting noise with extra steps.
# Validation then gates ARMING, which is a different question and Jack's call.
SHADOW_MIN_SPREAD_C = 5.0

# Why 5c and not 3c or 8c. The previous version of this comment derived 0.547c
# and is WRONG in a way worth keeping visible, because the error is a sign, not
# a slip. It read:
#
#     0.603 - 0.6033*s = s/2   ->   s = 0.603 / 1.1033 = 0.547c
#
# `fit()` regresses `(outcome - market_price)`, which is the BUY side. The arm
# this threshold governs is a resting SELL, which earns the NEGATIVE of that.
# The line above set a buy-side gross equal to a sell-side cost. Carried through
# correctly, `break_even_spread()` gives 10.95c at the mid — twenty times larger,
# outside the observed spread support, and on the OTHER side of the live 5c
# threshold. A number that agrees with the setting it justifies is not evidence
# that the setting is right; this one agreed by cancelling its own sign error.
#
# So the honest statement of where 5c comes from is measurement, not algebra.
# The bucketed means (`round_trip_cost`) put the resting sell positive for the
# first time at 5-10c (+0.21c) and clearly positive at >=10c (+2.80c), and every
# narrower bucket is negative. At 2c the whole harvested quantity is smaller
# than one tick, so a single tick of noise on the fill flips the sign; at 5c a
# tick is a fifth of the edge.
#
# And the derived number is not a competing answer, it is a CONDITIONAL one:
# 10.95c is the break-even IF you must improve to the mid to get filled. At the
# ask it is far lower. The two are not in conflict — the gap between them IS the
# execution question, which is why `execution_curve()` reports every mode and
# `fill_rate()` measures the mode that actually happens. 5c stays fixed while
# that is unsettled: the 5-6.7c band is the pre-registered discriminating
# region, and a threshold that moves with its own results is fitting noise with
# extra steps. Pinned by test; revisit when the shadow book settles enough rows.


def shadow_candidates(rows: Optional[List[dict]] = None,
                      f: Optional[dict] = None) -> List[dict]:
    """Open rows the fixed rule would rest a SELL on. Independent of validation.

    Carries `p_hat_unvalidated` — what the current fit WOULD have said — beside
    the rule's own decision, so that when settlements arrive we can ask the two
    questions separately: did the rule pay, and did the estimator add anything
    to it. Collapsing those into one number would be an aggregate nobody could
    act on.
    """
    rows = rows if rows is not None else load_rows()
    f = f if f is not None else load_fit()
    out = []
    for r in rows:
        if r.get("settled") or r.get("voided"):
            continue
        if r.get("selection_rule") != REGIME:
            continue
        px, spread = r.get("market_price"), r.get("spread")
        if not (_is_number(px) and _is_number(spread)):
            continue
        if spread * 100.0 < SHADOW_MIN_SPREAD_C:
            continue
        cost = round_trip_cost(r, cross=False)
        if cost is None:
            continue
        out.append({
            "ticker": r.get("ticker"),
            "event_ticker": r.get("event_ticker"),
            "title": r.get("title"),
            "rule": f"rest_sell+spread>={SHADOW_MIN_SPREAD_C:g}c",
            "regime": REGIME,
            "side": "NO",              # selling YES == buying NO
            "execution": "REST_ONLY",
            "booked_at": _now().isoformat(),
            "market_price": px,
            "spread_cents": spread * 100.0,
            "cost_cents_rest": cost * 100.0,
            "p_hat_unvalidated": _apply(f, r) if f else None,
            "close_time": r.get("close_time"),
            "days_to_close": r.get("days_to_close"),
            "available_size": r.get("available_size"),
            "outcome": None,
            "realized_net_cents": None,
        })
    return out


def book_shadow(rows: Optional[List[dict]] = None) -> dict:
    """Append today's candidates to the shadow book. Idempotent per ticker+day.

    Idempotent because the VM runs the scanner several times a day and a book
    that double-counts one contract reports a confidence it has not earned —
    the same n-inflation that let 923 rows of one horizon band speak for 929.
    """
    book = load_rows(SHADOW_BOOK)
    seen = {(e.get("ticker"), str(e.get("booked_at"))[:10]) for e in book}
    added = [c for c in shadow_candidates(rows)
             if (c["ticker"], c["booked_at"][:10]) not in seen]
    if added:
        book.extend(added)
        SHADOW_BOOK.write_text(json.dumps(book, indent=2))
    return {"added": len(added), "total": len(book), "path": str(SHADOW_BOOK)}


# --------------------------------------------------------------------------
# the quote track — recording the number the verdict turns on
# --------------------------------------------------------------------------
# Measured 2026-08-28: fill rate could not be reconstructed from anything on
# disk. 4,631 of the 4,646 tickers in `kalshi_predictions.json` appear exactly
# ONCE, and `kalshi_ladders.json` is a single snapshot the scanner overwrites
# on every run. So the one quantity separating "no edge" from "clear edge" had
# never been written down anywhere, by anyone, and no amount of re-analysis of
# the files we already have can produce it. It is not recoverable backwards. It
# can only be recorded forwards, which is what this does.
QUOTE_TRACK = DATA / "kalshi_quote_track.json"
LADDERS = DATA / "kalshi_ladders.json"

# A floor on REPORTING a fill rate, deliberately separate from
# MIN_SHADOW_EVENTS (which gates a P&L verdict) and from kalshi_calibration's
# thresholds (which gate arming). Collapsing them would let a thin fill sample
# satisfy a verdict it cannot support. It is small because the question it
# answers is coarse — "does an offer at the ask ever get lifted, or essentially
# never?" — and that is far cheaper to settle than the sub-cent mean the shadow
# book is chasing.
MIN_FILL_OBS = 50

# A leg needs TWO post-booking snapshots before it can be scored: a fill is an
# event inside an interval, and one observation bounds no interval. Legs with
# fewer are excluded from the denominator rather than counted as unfilled — an
# unmeasured leg is not a failed one, and quietly filing it as one is how a
# fill rate gets talked down to zero by arithmetic nobody looked at.
MIN_QUOTES_PER_LEG = 2


def load_ladder(path: Optional[Path] = None) -> Tuple[dict, List[dict]]:
    """The universe snapshot as `(meta, rungs)`. It is a dict, not a list.

    `load_rows` returns [] for a dict, so calling it here would have produced a
    silent zero-observation quote track that looked like a book nobody quotes.
    The meta half is returned because `scanned_at` lives there and the caller
    needs it — fetching it with a second read is how the two halves drift.
    """
    path = path if path is not None else LADDERS
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, []
    if isinstance(raw, list):
        return {}, [r for r in raw if isinstance(r, dict)]
    if not isinstance(raw, dict):
        return {}, []
    rows = raw.get("rows")
    return raw, [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def record_quotes(book: Optional[List[dict]] = None,
                  ladder: Optional[List[dict]] = None,
                  scanned_at: Optional[str] = None) -> dict:
    """Append one quote observation per still-open shadow leg. Idempotent/minute.

    Stamped with the snapshot's own `scanned_at`, not with now(): the timestamp
    has to describe when the QUOTE was true, or a stale snapshot re-read an hour
    later would enter the track as a fresh observation and manufacture the
    appearance of a book that never moves. Same reason this runs AFTER
    `kalshi_scanner.py` in the cron line rather than beside it.
    """
    book = book if book is not None else load_rows(SHADOW_BOOK)
    open_tickers = {e.get("ticker") for e in book if e.get("outcome") is None}
    meta: dict = {}
    if ladder is None:
        meta, ladder = load_ladder()
    ts = scanned_at or meta.get("scanned_at") or _now().isoformat()
    if not open_tickers:
        return {"observed": 0, "open_legs": 0, "total": len(load_rows(QUOTE_TRACK)),
                "ts": ts, "path": str(QUOTE_TRACK)}
    track = load_rows(QUOTE_TRACK)
    seen = {(o.get("ticker"), str(o.get("ts"))[:16]) for o in track}
    added = 0
    for m in ladder:
        t = m.get("ticker")
        if t not in open_tickers or (t, str(ts)[:16]) in seen:
            continue
        track.append({
            "ticker": t,
            "event_ticker": m.get("event_ticker"),
            "ts": ts,
            "yes_ask": m.get("market_price"),
            "yes_bid": m.get("yes_bid"),
            # Recorded so the "our own order would have added depth" bias in
            # `fill_rate` can be CHECKED against the level we would have joined,
            # instead of asserted to be small.
            "available_size": m.get("available_size"),
            # Cumulative traded volume. Without it a level that CLEARED and a
            # level that was CANCELLED look identical, and only the first one
            # fills anybody.
            "volume": m.get("volume"),
        })
        added += 1
    if added:
        QUOTE_TRACK.write_text(json.dumps(track, indent=2))
    return {"observed": added, "open_legs": len(open_tickers),
            "total": len(track), "ts": ts, "path": str(QUOTE_TRACK)}


def fill_rate(improvement_cents: float = 0.0,
              book: Optional[List[dict]] = None,
              track: Optional[List[dict]] = None) -> dict:
    """Measured frequency that a resting sell at (ask - improvement) was lifted.

    THE OBSERVABLE: within one interval between snapshots, the best ask prints
    strictly ABOVE our price while cumulative volume rises. If the level at P
    is gone then everything resting at or below P was consumed, and an offer of
    ours sitting there was consumed with it; requiring volume to have risen over
    the same interval separates that from a level that merely cancelled, which
    fills nobody.

    Three biases, named rather than estimated away, because they do not point
    the same way and a point estimate would be the over-claim this module exists
    to stop:
      - our own size would ADD depth at P, so the level clears less often than
        it does without us. OVER-states. Small while the order is small against
        the resting size, which is why `available_size` travels with every
        observation — so the condition can be checked, not assumed.
      - partial consumption of the level is invisible at four snapshots a day.
        UNDER-states, and by more the more actively the book trades.
      - a leg with fewer than MIN_QUOTES_PER_LEG post-booking snapshots bounds
        no interval and is excluded from the denominator, not scored zero.

    Refuses below MIN_FILL_OBS. A fill rate of zero because nothing has been
    measured and a fill rate of zero because offers never get lifted are
    opposite findings, and returning 0.0 for both would let a caller multiply
    the edge by it and retire the arm (memory `fallback-that-means-act`).
    """
    book = book if book is not None else load_rows(SHADOW_BOOK)
    track = track if track is not None else load_rows(QUOTE_TRACK)
    by_ticker: Dict[str, List[dict]] = {}
    for o in track:
        if _is_number(o.get("yes_ask")):
            by_ticker.setdefault(o.get("ticker"), []).append(o)
    for obs in by_ticker.values():
        obs.sort(key=lambda o: str(o.get("ts")))

    filled = unfilled = too_few = 0
    for e in book:
        px = e.get("market_price")
        booked = _parse_ts(e.get("booked_at"))
        if not _is_number(px) or booked is None:
            continue
        p_cents = px * 100.0 - float(improvement_cents)
        later = [o for o in by_ticker.get(e.get("ticker"), [])
                 if (_parse_ts(o.get("ts")) or booked) >= booked]
        if len(later) < MIN_QUOTES_PER_LEG:
            too_few += 1
            continue
        hit = False
        for prev, cur in zip(later, later[1:]):
            v, pv = cur.get("volume"), prev.get("volume")
            if not (_is_number(v) and _is_number(pv) and v > pv):
                continue
            if cur["yes_ask"] * 100.0 > p_cents + 1e-9:
                hit = True
                break
        filled += 1 if hit else 0
        unfilled += 0 if hit else 1

    n = filled + unfilled
    out = {"improvement_cents": float(improvement_cents), "n": n,
           "filled": filled, "legs_below_min_quotes": too_few}
    if n < MIN_FILL_OBS:
        out.update({"ok": False, "reason_code": "FILL_UNMEASURED", "rate": None,
                    "why": "%d leg(s) have %d+ post-booking quotes; need %d. "
                           "A fill rate has never been measured on this book."
                           % (n, MIN_QUOTES_PER_LEG, MIN_FILL_OBS)})
        return out
    out.update({"ok": True, "rate": filled / n,
                "bias": "over-states (our own size is not in the book); "
                        "under-states (partial fills invisible at this cadence)"})
    return out


def resolve_shadow(rows: Optional[List[dict]] = None) -> dict:
    """Attach settled outcomes to booked entries and price the realized net.

    The realized number is the RESTING sell: (price - outcome) - spread/2 -
    maker fee. It is an upper bound, because it credits a fill we never had to
    win. Reported as such by `--shadow-report`, never as a P&L.
    """
    book = load_rows(SHADOW_BOOK)
    if not book:
        return {"resolved": 0, "open": 0, "total": 0}
    settled = {}
    for r in (rows if rows is not None else load_rows()):
        if r.get("settled") and not r.get("voided") and _outcome(r) is not None:
            settled[r.get("ticker")] = r
    n = 0
    for e in book:
        if e.get("outcome") is not None:
            continue
        src = settled.get(e.get("ticker"))
        if src is None:
            continue
        y = _outcome(src)
        e["outcome"] = y
        e["settled_at"] = src.get("settled_at")
        e["realized_net_cents"] = ((e["market_price"] - y) * 100.0
                                   - e["cost_cents_rest"])
        n += 1
    if n:
        SHADOW_BOOK.write_text(json.dumps(book, indent=2))
    return {"resolved": n,
            "open": sum(1 for e in book if e.get("outcome") is None),
            "total": len(book)}


def shadow_verdict(book: Optional[List[dict]] = None) -> dict:
    """Mean realized net with a cluster-bootstrap CI, or an explicit refusal.

    Clustered on `event_ticker` because the legs of one event share a single
    real-world outcome; treating them as independent understated the SE by 31%
    on the fit and is the difference between an interval that excludes
    break-even and one that contains it.
    """
    book = book if book is not None else load_rows(SHADOW_BOOK)
    done = [e for e in book if _is_number(e.get("realized_net_cents"))]
    clusters: Dict[str, List[float]] = {}
    for e in done:
        clusters.setdefault(e.get("event_ticker") or e.get("ticker"),
                            []).append(e["realized_net_cents"])
    if len(clusters) < MIN_SHADOW_EVENTS:
        return {"ok": False, "reason_code": "SHADOW_TOO_THIN",
                "why": f"{len(done)} settled shadow leg(s) across "
                       f"{len(clusters)} events; need {MIN_SHADOW_EVENTS} events",
                "n_legs": len(done), "n_events": len(clusters)}
    vals = [v for g in clusters.values() for v in g]
    mean = sum(vals) / len(vals)
    import random
    rnd = random.Random(7)
    groups = list(clusters.values())
    means = []
    for _ in range(SHADOW_BOOTSTRAP):
        s = [v for _ in groups for v in rnd.choice(groups)]
        means.append(sum(s) / len(s))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return {"ok": True, "n_legs": len(done), "n_events": len(clusters),
            "mean_net_cents": round(mean, 4),
            "ci95_cents": [round(lo, 4), round(hi, 4)],
            "p_positive": round(sum(1 for m in means if m > 0) / len(means), 4),
            # PROFITABLE requires the whole interval above zero, not the point
            # estimate. The point estimate has been positive on 68 legs with a
            # CI of [-6.24, +12.07]; acting on that is acting on noise.
            "verdict": ("PROFITABLE" if lo > 0 else
                        "UNPROFITABLE" if hi < 0 else "UNDECIDED")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cmd_fit() -> int:
    f = fit()
    if f is None:
        print("REFUSED — not enough settled current-regime rows to fit.")
        return 1
    v = validate()
    f["validated_at"] = _now().isoformat()
    f["validation"] = {k: v[k] for k in
                       ("ok", "n_folds", "mean_brier_improvement",
                        "folds_improved", "beats_market", "why") if k in v}
    f["validated_beats_market"] = bool(v.get("ok") and v.get("beats_market"))

    FIT_FILE.write_text(json.dumps(f, indent=2))
    ci = f.get("slope_ci95")
    print(f"  n={f['n']} rows / {f['n_clusters']} events")
    print(f"  bias(c) = {f['intercept_cents']:+.3f} "
          f"{f['slope_cents_per_cent_spread']:+.4f} * spread(c)")
    if ci:
        print(f"  slope 95% CI (clustered) [{ci[0]:+.4f}, {ci[1]:+.4f}]  "
              f"break-even = -0.5000")
    print(f"  beats crossing cost: point={f['beats_crossing_cost_point_estimate']} "
          f"at95={f['beats_crossing_cost_at_95']}")
    print(f"  walk-forward beats market: {f['validated_beats_market']} "
          f"({f['validation'].get('why') or ''})")
    print(f"  -> {FIT_FILE}")
    return 0


def _cmd_report() -> int:
    f = load_fit()
    if f is None:
        print("NO USABLE FIT — run --fit. (Missing, stale, or regime changed.)")
        return 1
    if not f.get("validated_beats_market"):
        print("FIT PRESENT BUT NOT SERVING: it did not beat the market "
              "out-of-sample, so estimate() returns None by design.")
        print(f"  validation: {json.dumps(f.get('validation'), indent=2)}")
        return 0
    rows = [r for r in load_rows() if not r.get("settled") and not r.get("voided")]
    sigs = [s for s in (signal(r, f) for r in rows) if s]
    sigs.sort(key=lambda s: -s["net_cents"])
    print(f"  {len(sigs)} signal(s) of {len(rows)} open row(s)")
    for s in sigs[:15]:
        print(f"  [{s['side']:>3}] {str(s['ticker'])[:44]:44s} "
              f"px={s['market_price']*100:5.1f}c p_hat={s['p_hat']*100:5.1f}c "
              f"net={s['net_cents']:+5.2f}c")
    return 0


def _representative_price(book: List[dict]) -> Optional[float]:
    """Median booked price — the price the execution curve is quoted at.

    The maker fee is `rate * p * (1-p)`, so a curve has to name a price to mean
    anything. Taking the median of what the rule ACTUALLY booked, rather than a
    constant like 0.95, keeps the fee in the report tied to the contracts the
    arm trades. Returns None on an empty book instead of a default, because a
    default price here would silently price a curve for a book that does not
    exist.
    """
    px = sorted(e["market_price"] for e in book if _is_number(e.get("market_price")))
    return px[len(px) // 2] if px else None


def _cmd_shadow_report() -> int:
    """Verdict, execution curve and fill rate — printed together, always.

    These three are one answer, not three reports. The verdict is a mean net in
    cents; that mean is computed under ONE assumed execution; and whether that
    execution happens is the fill rate. Printing the verdict alone is how
    `round_trip_cost`'s at-the-mid assumption came to look like a fact — it was
    quoted for months with no fill rate beside it. Any of the three may refuse;
    a refusal prints as a refusal and does not suppress the other two.
    """
    book = load_rows(SHADOW_BOOK)
    out = {"verdict": shadow_verdict(book), "n_booked": len(book)}

    f = load_fit()
    price = _representative_price(book)
    if f is None:
        out["execution_curve"] = {"ok": False, "reason_code": "NO_FIT",
                                  "why": "no usable fit on disk — run --fit"}
    elif price is None:
        out["execution_curve"] = {
            "ok": False, "reason_code": "NO_BOOKED_PRICE",
            "why": "the shadow book has no priced leg, so there is no price to "
                   "quote a fee or a curve at"}
    else:
        out["execution_curve"] = execution_curve(f, price)
        out["execution_curve"]["price_source"] = "median booked leg"

    # Every improvement level the curve prices, so the two tables line up cell
    # for cell. Reporting the fill rate at one level and the curve at four is
    # how a reader ends up multiplying the wrong pair.
    out["fill_rate"] = {"improve_%gc" % d: fill_rate(d) for d in (0.0, 1.0, 2.0)}
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi selection-bias estimator.")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--shadow", action="store_true",
                    help="book today's rule candidates and resolve settled ones")
    ap.add_argument("--shadow-report", action="store_true")
    a = ap.parse_args()
    if a.fit:
        return _cmd_fit()
    if a.validate:
        print(json.dumps(validate(), indent=2))
        return 0
    if a.shadow:
        # ORDER IS LOAD-BEARING. Book first so a leg opened today gets a quote
        # observation on the same pass. Quote BEFORE resolving, so a leg that
        # settles today still contributes its final observation instead of being
        # closed out one snapshot short — `fill_rate` needs two observations to
        # bound an interval, and the cheapest place to lose the second one is
        # here.
        print(json.dumps({"booked": book_shadow(),
                          "quotes": record_quotes(),
                          "resolved": resolve_shadow()}, indent=2))
        return 0
    if a.shadow_report:
        return _cmd_shadow_report()
    return _cmd_report()


if __name__ == "__main__":
    raise SystemExit(main())

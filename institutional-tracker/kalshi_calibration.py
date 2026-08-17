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

# The two-sided rule went live 2026-08-17. Everything before it was selected by
# `tradeable()` and is a different experiment (see `_is_current_regime`).
REGIME_START = "2026-08-17"

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

    Scoped to the CURRENT selection rule, same as `is_premise_alive`. A gate
    that counted the retired `tradeable()` rows toward its sample would clear
    its own size threshold on 216 observations of a rule the scanner no longer
    applies — reaching a verdict sooner by measuring the wrong thing.
    """
    rows = rows if rows is not None else _load_ledger()
    settled = [r for r in rows
               if _is_current_regime(r)
               and r.get("settled") and r.get("outcome") in (0, 1)]

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
    """Was this row selected by `two_sided()`, the rule the sector runs today?

    `yes_bid` is the marker: `two_sided()` refuses any market without a bid, and
    only the post-2026-08-17 ledger writer copies the field through. Absence is
    read as legacy — the conservative direction, since a row wrongly called
    legacy delays a verdict and a row wrongly called current corrupts one.
    """
    return row.get("yes_bid") is not None


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
        why = (f"CANNOT TELL — {len(settled)}/{MIN_SETTLED_FOR_VERDICT} settled "
               f"quotes selected by the CURRENT rule (two-sided book, spread "
               f"<= 10c). The premise is judged on outcomes, and this rule has "
               f"not produced enough yet.")
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
    realised = sum(int(r["outcome"]) for r in settled) / n
    mean_quote = sum(float(r["market_price"]) for r in settled) / n

    z = 1.96
    denom = 1 + z * z / n
    centre = (realised + z * z / (2 * n)) / denom
    half = z * math.sqrt(realised * (1 - realised) / n + z * z / (4 * n * n)) / denom
    ci_low, ci_high = max(0.0, centre - half), min(1.0, centre + half)

    # ---- B. the book -------------------------------------------------------
    # Independent of settlement, and it must be measured on the SAME cohort.
    # Note what this can and cannot say now: every current-regime row carries a
    # bid by construction, so "did anyone quote the other side" is answered YES
    # trivially and is NOT corroboration. What is still informative is how far
    # the bid sits from the ask — a 97c ask against a 90c bid brackets a range
    # the quote does not pin down.
    spreads = [float(r["spread"]) for r in settled if r.get("spread") is not None]
    mean_spread = sum(spreads) / len(spreads) if spreads else None

    detail.update({"mean_quote": round(mean_quote, 4),
                   "realised": round(realised, 4),
                   "ci_low": round(ci_low, 4), "ci_high": round(ci_high, 4),
                   "mean_spread": round(mean_spread, 4) if mean_spread else None})

    if mean_quote > ci_high:
        book = (f" Mean bid-ask spread {mean_spread:.3f} — the book bracketed a "
                f"range the quote did not pin down." if mean_spread else "")
        return DEAD, (
            f"DEAD on n={n} rows selected by the CURRENT two-sided rule — the "
            f"market quoted a mean of {mean_quote:.1%} and settled YES "
            f"{realised:.1%} of the time (95% CI {ci_low:.1%}-{ci_high:.1%}). "
            f"The quote is not above the interval by a little; it is outside it "
            f"entirely, so these numbers are not probabilities and there is "
            f"nothing for an estimator to beat.{book}"), detail

    if mean_quote < ci_low:
        return DEAD, (
            f"DEAD on n={n} — inverted: quoted {mean_quote:.1%}, settled "
            f"{realised:.1%} (95% CI {ci_low:.1%}-{ci_high:.1%}). Systematically "
            f"cheap in this band is still a broken price, not free money; the "
            f"reason a 97c contract settles 100% of the time is that nobody "
            f"will sell it to you."), detail

    return ALIVE, (f"ALIVE on n={n} — quoted {mean_quote:.1%}, settled "
                   f"{realised:.1%} (95% CI {ci_low:.1%}-{ci_high:.1%}); the "
                   f"quote is inside its own interval. This clears the PRIOR "
                   f"question only. is_edge_proven still gates every dollar."), detail


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
    signature = f"{premise}|{'PROVEN' if proven else 'NOT_PROVEN'}"

    try:
        prior_text = VERDICT_STATE.read_text()
        st = json.loads(prior_text)
        prior, last_at = st.get("signature"), st.get("at")
    except (OSError, ValueError, AttributeError):
        prior_text, prior, last_at = None, None, None

    stale = True
    if last_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
            stale = age.days >= VERDICT_HEARTBEAT_DAYS
        except (TypeError, ValueError):
            stale = True

    if not force and signature == prior and not stale:
        return False, f"unchanged ({signature}) and last sent {last_at}"

    try:
        VERDICT_STATE.write_text(json.dumps({"signature": signature, "at": _now()}))
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

    body = (
        f"<p><b>PREMISE: {premise}</b><br>{why_premise}</p>"
        f"<p><b>EDGE: {'PROVEN' if proven else 'NOT PROVEN'}</b><br>{why_edge}</p>"
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

    subject = (f"Kalshi verdict: premise {premise}, edge "
               f"{'PROVEN' if proven else 'not proven'} "
               f"(n={detail.get('settled', 0)} settled)")
    try:
        from email_buffer import buffer_email
        # False means "urgent — I did not buffer it, you send it". This caller
        # has no direct sender, so that return is a MISS, not a success.
        buffered = buffer_email(subject=subject, html_body=body,
                                sender_name="Kalshi Sector",
                                key_metrics={"premise": premise,
                                             "edge_proven": proven,
                                             "settled": detail.get("settled", 0),
                                             "realised": detail.get("realised"),
                                             "mean_quote": detail.get("mean_quote")})
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
        return True, f"sent ({signature})"
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
          f"(two-sided book, {len(cur_rows)} row(s))")
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

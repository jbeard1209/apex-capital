#!/usr/bin/env python3
"""
APEX CAPITAL — COHERENCE LAWS
=============================
A different KIND of detector from the two the firm already has.

CIT reads code. The Red Team argues with conclusions. Both take an input and
critique it, so both are blind to anything that was never IN the input — and
every expensive escape of the last three months has been exactly that:

  catch_up ran main.py 20x/day     every detector looks for ABSENCE. A job that
                                   runs too often never fails.
  deploy.sh excluded gcp_setup/    the audit compares what is IN the deploy set.
                                   An excluded file is not in it.
  the FI pod emitted "BUY"         no `else` branch, so nothing produced a record
                                   for anyone to inspect.
  shorts could not close           the check asked whether sells were ATTEMPTED,
                                   not whether they succeeded.
  the Oracle third host            the host list WAS the doc, and the doc did not
                                   have it.
  Kalshi's 98c band                the code is correct. The DATA is incoherent.

In every one of those the code review passes and the argument sounds fine. You
cannot enumerate failures nobody has thought of. But you CAN enumerate
relationships between numbers that must hold, and a violated relationship names
its own bug without anyone having anticipated it.

THE RULE THAT MAKES THIS WORK
-----------------------------
**Every law compares two INDEPENDENTLY-SOURCED measurements of the same
quantity.** That is the whole trick, and it is why CIT cannot get here from code
and the Red Team cannot get here from argument. A single-source check is useless
against this bug class, because a broken producer's output is perfectly
self-consistent: state.json agreed with itself while it said 0 positions and the
broker held 16. Two sources, and disagreement is the alarm.

If you add a law and cannot name its two sources, it is not a coherence law —
it is an assertion, and it belongs in test_pipeline_smoke.py.

THREE VERDICTS, NOT TWO
-----------------------
HOLDS / VIOLATED / CANNOT_TELL. The third exists because of memory
`verdict-with-no-consequence`: a check that cannot read its inputs must not be
able to report success. CANNOT_TELL is a finding, is printed, names what it
could not read, and counts against the report — it is never folded into HOLDS.

Usage:
    python3 coherence_laws.py              # run every law, print, write report
    python3 coherence_laws.py --report     # same (agent convention)
    python3 coherence_laws.py --quiet      # only print VIOLATED / CANNOT_TELL

Exit code: 0 if every law HOLDS, 1 if any is VIOLATED or CANNOT_TELL (CI-friendly).
"""

import argparse
import ast
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORT = DATA_DIR / "coherence_report.json"

HOLDS = "HOLDS"
VIOLATED = "VIOLATED"
CANNOT_TELL = "CANNOT_TELL"


class LawResult:
    """One law's verdict, plus the two sources it compared.

    `sources` is not decoration. It is the field that makes a stale law
    visible: when a source stops existing, the law starts returning
    CANNOT_TELL naming it, instead of quietly passing on a file nobody writes
    any more (memory `reader-outlived-its-producer`).
    """

    def __init__(self, name: str, verdict: str, detail: str,
                 sources: Tuple[str, str], evidence: Optional[list] = None,
                 n_examined: Optional[int] = None):
        self.name = name
        self.verdict = verdict
        self.detail = detail
        self.sources = sources
        self.evidence = evidence or []
        # HOW MANY THINGS THIS VERDICT IS ABOUT. A pass with a denominator of
        # zero is not a pass — it is a check that found nothing to check and
        # said "clean". That single shape is behind more escapes here than any
        # other: the Kalshi filter rejected 0 of 0 parseable rungs, the host
        # census read an INFRASTRUCTURE.md that wasn't there, coverage looked
        # present on one stale record (memory `presence-is-not-currency`).
        # `internal_audit_agent` reads this field and downgrades any HOLDS that
        # cannot say how much it looked at, which is why HOLDS paths must set
        # it and CANNOT_TELL paths need not.
        #
        # A VIOLATED NEEDS IT FOR THE OPPOSITE REASON, and until 2026-08-24 not
        # one of the ten laws set it there. A missing denominator on a pass is a
        # false all-clear; on a failure it is an untriageable finding. "4 ladders
        # quote an impossible nested pair" is a rounding error at 4/2405 and a
        # broken parser at 4/4, and those want opposite responses — ignore it, or
        # stop trusting the law. escalate_violations() files every violation into
        # a queue that already holds 134 items whose soonest board escalation is
        # overdue; the numerator alone cannot sort them, so the denominator IS
        # the triage. The auditor only ever inspected HOLDS, so the gap was in
        # the one direction the auditor was not looking.
        #
        # It is NOT a rate for every law. `runs_match_schedule` compares two
        # counts (observed vs intended) rather than sampling a population, so its
        # denominator is the heartbeat history it read, not `intended` — writing
        # "7 of 6" to satisfy a checker would be inventing a number, which is the
        # failure this field exists to prevent.
        self.n_examined = n_examined

    def to_dict(self) -> dict:
        return {
            "law": self.name,
            "verdict": self.verdict,
            "detail": self.detail,
            "source_a": self.sources[0],
            "source_b": self.sources[1],
            "n_examined": self.n_examined,
            "evidence": self.evidence[:20],
        }


def sources(a: str, b: str):
    """Declare a law's two independent sources, and refuse the law without them.

    This is a decorator rather than a convention because the two-source rule is
    the only thing separating this module from an assertion file, and a rule
    enforced by convention is a rule that lasts until the first hurried commit.
    A law declaring one source, or the same source twice, raises at IMPORT time —
    the module will not load, which is loud in a way a failing test in a suite
    someone skipped is not.

    It also lets the audit read a law's sources WITHOUT running it. That matters:
    two of these laws ssh to another host and hit the broker, and a smoke test
    that had to execute them to learn their sources would be a network call in
    the commit path.
    """
    if not a or not b:
        raise ValueError(f"a coherence law needs two named sources, got {a!r}/{b!r}")
    if a == b:
        raise ValueError(f"a law's two sources must be independent; both are {a!r}. "
                         f"A single-source check passes on a broken producer.")

    def deco(fn):
        fn.sources = (a, b)
        return fn
    return deco


# =============================================================================
# LAW — nested probabilities are monotone
# =============================================================================

# "Will the Nikkei 225 be at least Y68,450 ..."  -> threshold 68450, DECREASING
# "Will over 2.5 goals be scored?"               -> threshold 2.5,   DECREASING
# "Will the silver close price be above 65.499"  -> threshold 65.499, DECREASING
# "... wins by more than 1.5 goals?"             -> threshold 1.5,   DECREASING
#
# Every one of these says "the quantity lands ABOVE X". Raising X can only make
# that less likely, so within one event the prices must fall as X rises. The
# UNDER/BELOW family is the mirror and must rise. Anything else is not a
# probability.
_ABOVE_RE = re.compile(
    r"\b(?:over|above|at least|more than|greater than|exceed(?:s)?)\b[^\d\-+]{0,24}"
    r"([\d][\d,]*(?:\.\d+)?)",
    re.I,
)
_BELOW_RE = re.compile(
    r"\b(?:under|below|at most|less than|fewer than|no more than)\b[^\d\-+]{0,24}"
    r"([\d][\d,]*(?:\.\d+)?)",
    re.I,
)


def parse_threshold(title: str) -> Optional[Tuple[float, str]]:
    """(threshold, 'above'|'below') for a one-sided threshold market, else None.

    Returns None — never a default direction — for titles this cannot parse.
    A market we cannot place on the number line is one this law must not
    judge, and guessing 'above' would manufacture violations out of
    unparseable text. Same discipline as `kalshi_client.parse_price`.
    """
    if not title:
        return None
    a = _ABOVE_RE.search(title)
    b = _BELOW_RE.search(title)
    if a and b:
        return None            # two-sided / range market: not a nested ladder
    m, direction = (a, "above") if a else (b, "below") if b else (None, None)
    if m is None:
        return None
    try:
        return float(m.group(1).replace(",", "")), direction
    except ValueError:
        return None


# Kalshi's own structured description of a rung. `strike_type` says which side
# of the number the contract pays on and floor/cap carry the number itself.
_STRIKE_ABOVE = {"greater", "greater_or_equal"}
_STRIKE_BELOW = {"less", "less_or_equal"}


def market_threshold(m: dict) -> Optional[Tuple[float, str]]:
    """(threshold, 'above'|'below') from the exchange's own fields, else prose.

    THE STRUCTURED FIELDS COME FIRST, and that ordering is the fix for a real
    miss. Every leg of an MLB total-runs ladder is titled "St. Louis vs
    Cincinnati Total Runs?" — identical across all eleven rungs, with no number
    in it anywhere. The strike lives in `floor_strike` (3.5, 4.5, … 13.5) and in
    `yes_sub_title` ("Over 4.5 runs scored"); `rules_primary` spells it out in
    prose Kalshi writes as "score more 4.5 runs", which the title regex was
    never going to match.

    So `parse_threshold(title)` returned None for all eleven, `_ladders` built
    no ladder, and `drop_incoherent` reported ZERO incoherent candidates. Zero
    dropped read as "everything checked out". It meant "nothing was checkable" —
    the same substitution as an alarm that stays GREEN because it only ever
    asked whether an explanation EXISTS (memory `presence-is-not-currency`).

    Prose stays as the fallback, not because it is better but because it is the
    only thing older ledger rows carry: they were recorded before the scanner
    stored a strike, and dropping them would silently shrink the history the
    law is measured on.
    """
    st = str(m.get("strike_type") or "").strip().lower()
    if st in _STRIKE_ABOVE and m.get("floor_strike") is not None:
        try:
            return float(m["floor_strike"]), "above"
        except (TypeError, ValueError):
            pass
    if st in _STRIKE_BELOW and m.get("cap_strike") is not None:
        try:
            return float(m["cap_strike"]), "below"
        except (TypeError, ValueError):
            pass
    # `between`/`custom` are deliberately not coerced. A range market is not a
    # rung on a ladder, and forcing it onto the number line would manufacture
    # violations out of markets this law has no claim over.
    if st in ("between", "custom", "structured"):
        return None

    for field in ("title", "yes_sub_title"):
        parsed = parse_threshold(str(m.get(field) or ""))
        if parsed is not None:
            return parsed
    return None


def _bid_of(row: dict) -> Optional[float]:
    """The rung's bid, or None. None means "not observed", never zero.

    THIS FUNCTION IS WHY THIS LAW STOPPED SHOUTING. Until 2026-08-17 the law
    compared ask against ask and reported 287 of 1,148 ladders VIOLATED. Four
    of those 287 were real. The other 283 were the bid-ask spread.

    A quote is not a probability, it is a BRACKET around one: the true P lies
    somewhere in [bid, ask]. Comparing two rungs' asks compares the tops of two
    brackets of different widths, and a wide bracket sits above a narrow one
    for reasons that have nothing to do with arithmetic. Monotonicity is
    contradicted only when the brackets cannot be ordered at all — when the
    MOST the wide outcome can be worth is less than the LEAST the narrow one
    is: `ask(wide) < bid(narrow)`. That is also exactly the arbitrage (buy
    wide, sell narrow, collect the difference, and the payoff is never
    negative), which is the proof that no probabilities produce these quotes.

    The 19 "every rung identically priced" ladders were the tell. Six legs all
    asking 0.99 looked like nonsense, and their BIDS were 0.80, 0.65, 0.54,
    0.48, 0.22, 0.18 — flawlessly monotone. The market was coherent. The ask
    was a resting ceiling offer nobody would ever meet, and `kc.two_sided`'s
    0.10 spread cap had already refused every one of them as a candidate. The
    law was blaming the ladder for what the spread gate had caught and named.

    An alarm that is wrong 98.6% of the time is not a strict alarm, it is
    noise, and noise is how a real one gets ignored.
    """
    for key in ("yes_bid", "yes_bid_dollars", "bid"):
        if key not in row:
            continue
        try:
            v = float(row[key])
        except (TypeError, ValueError):
            return None
        # Zero is an empty book, not a bid of nothing — the same reading that
        # opened the Kalshi sector. Parsed here rather than imported from
        # kalshi_client on purpose: a law that borrows the scanner's reading of
        # a number is no longer a second, independent source for it.
        return v if 0.0 < v < 1.0 else None
    return None


def _ladder_fault(rungs: dict, direction: str):
    """(ordered, asks, bids, hit, testable_pairs) for one ladder.

    `hit` is (i, j, credit) for the first pair whose quotes NO probabilities
    produce — `ask(wide) < bid(narrow)` — or None. `testable_pairs` is the
    denominator: zero means the ladder is one-sided and cannot pass or fail.

    THE ONE DEFINITION OF "INCOHERENT" IN THE FIRM. The law and
    `kalshi_scanner.drop_incoherent` both call it, because for a month they
    each carried their own copy of the test and the copies were both wrong in
    the same way — the scanner discarded 439 spread-gated candidates a day
    under `dropped_reason="the quote is not a probability"`, a claim that was
    false for all but two of them. Two consumers of one rule is exactly the
    shape that drifts (memory `wiring_contract_bugs`), so there is one rule.
    """
    ordered = sorted(rungs.items())               # ascending threshold
    if direction == "below":
        ordered = ordered[::-1]                   # index order is always widest-first
    asks = [row.get("market_price") for _, row in ordered]
    bids = [_bid_of(row) for _, row in ordered]
    hit, pairs = None, 0
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if asks[i] is None or bids[j] is None:
                continue
            pairs += 1
            if hit is None and asks[i] < bids[j] - 1e-9:
                hit = (i, j, round(bids[j] - asks[i], 4))
    return ordered, asks, bids, hit, pairs


def arbitraged_events(rows: List[dict]) -> set:
    """Event tickers whose quoted ladder admits no set of probabilities."""
    out = set()
    for (ev, direction), rungs in _ladders(rows).items():
        if _ladder_fault(rungs, direction)[3] is not None:
            out.add(ev)
    return out


def _ladders(rows: List[dict]) -> dict:
    """Group rows into (event_ticker, direction) ladders of >= 2 distinct rungs.

    A ladder is the unit of comparison: the legs of ONE event, which the
    exchange quotes independently but arithmetic binds together.
    """
    grouped = defaultdict(dict)
    for r in rows:
        price = r.get("market_price")
        ev = r.get("event_ticker")
        if price is None or not ev:
            continue
        parsed = market_threshold(r)
        if parsed is None:
            continue
        threshold, direction = parsed
        # Keep one row per rung; duplicate scans of the same rung are not
        # independent observations and must not be compared against each other.
        grouped[(ev, direction)][threshold] = r
    return {k: v for k, v in grouped.items() if len(v) >= 2}


# How stale a scan may be before its rows stop being evidence about now.
# main.py runs 6x/day, so a scan older than this means the scanner stopped and
# the laws below would otherwise keep passing on a frozen snapshot forever —
# the failure in memory `presence-is-not-currency`.
KALSHI_SCAN_MAX_AGE_HOURS = 36


def _own(provenance: str) -> str:
    """Append the sector owner, unless the provenance already named a better one.

    `_kalshi_scan_report` now attributes staleness to a specific host, and
    blindly appending ". Owner: Kalshi sector." after it produced details
    ending "Owner: Kalshi sector cron. Owner: Kalshi sector." — two owners,
    neither actionable. The more specific one wins.
    """
    return provenance if "Owner:" in provenance else f"{provenance}. Owner: Kalshi sector."


def _host_schedules(script: str) -> Optional[bool]:
    """Does THIS host's live crontab run `script`? None if unreadable.

    Same discipline as `_scheduled_pipeline_runs_today`: read the scheduler,
    not a doc, and keep "cannot read" distinct from "reads fine, asks for
    nothing" — they have opposite consequences (memory `fallback-that-means-act`).
    Comment lines are stripped, because the Kalshi block's own header names
    `kalshi_scanner.py` several times in prose.
    """
    import subprocess
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return any(script in line.split("#", 1)[0] for line in out.stdout.splitlines())


def _kalshi_scan_report(filename, key):
    """(rows, provenance) from a Kalshi scanner artifact, or (None, why).

    Shared freshness + shape handling. The age check is not boilerplate: main.py
    scans 6x/day, and without it a law keeps passing forever on a frozen
    snapshot after the scanner stops — an alarm that is GREEN because nothing is
    looking (memory `presence-is-not-currency`).
    """
    path = DATA_DIR / filename
    if not path.exists():
        return None, f"no {filename} — the scanner has not run on this host"
    try:
        report = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        return None, f"{filename} unreadable: {e}"
    if isinstance(report, list):
        report = report[-1] if report else {}
    if not isinstance(report, dict):
        return None, f"{filename} is neither a report nor a list of reports"

    stamped = report.get("scanned_at")
    try:
        t = datetime.fromisoformat(str(stamped).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return None, f"{filename} carries no readable `scanned_at` — its age is unknown"
    if age_h > KALSHI_SCAN_MAX_AGE_HOURS:
        # WHOSE staleness is this? The scanner runs on one host's crontab and
        # there is no state sync between hosts (CLAUDE.md), so on any other
        # host this file is stale BY CONSTRUCTION and always will be. Reporting
        # that as a finding gives the Mac two laws that can never clear, and an
        # alarm permanently in one state carries no information — while the
        # genuine case (the owner's cron died) produces the identical text
        # (memory `both-sides-of-a-comparison`). Defer instead, and name who
        # owns the answer, the way positions_conserve already does.
        runs_here = _host_schedules("kalshi_scanner.py")
        if runs_here is False:
            return None, (f"this host ({_this_host()}) does not schedule "
                          f"kalshi_scanner.py, so its copy of {filename} is stale "
                          f"by construction — there is no state sync between "
                          f"hosts. Owner: the host whose crontab runs the Kalshi "
                          f"scanner")
        owner = ("this host's Kalshi cron" if runs_here
                 else "unknown — `crontab -l` failed here, so the scan is stale "
                      "but nobody can be named for it")
        return None, (f"last scan is {age_h:.0f}h old (limit "
                      f"{KALSHI_SCAN_MAX_AGE_HOURS}h) — these quotes are history, "
                      f"not evidence about the book now. Owner: {owner}")
    return report.get(key) or [], report, age_h


def _kalshi_ladder_rows():
    """(rows, provenance) — every rung of every ladder behind a surfaced candidate.

    **Complete ladders, and that is the entire point.** A ladder's coherence is a
    property of the whole ladder; the near-certainty band is a slice through it,
    and judging the slice is wrong in both directions. Reading only in-band rungs
    the first time this law was repointed produced 26 "leaks" that were not
    leaks: two adjacent rungs both pinned at the 99c ceiling are unremarkable —
    99c is the highest quotable price, so any two near-certain thresholds clip
    there — but they read as "all rungs identically priced", which IS damning
    across a full six-leg ladder. The evidence looked exactly like a finding.

    `kalshi_scanner` publishes the raw rungs; the parse and the verdict are both
    done here, so the two sides of the comparison stay independent.
    """
    got = _kalshi_scan_report("kalshi_ladders.json", "rows")
    if got[0] is None:
        return None, got[1]
    rows, report, age_h = got
    return rows, (f"{len(rows)} rung(s) across {report.get('events', '?')} "
                  f"event(s), published {age_h:.1f}h ago")


def _kalshi_cleared_rows():
    """(rows, provenance) — the quotes the Kalshi scanner PASSED, or (None, why).

    **Not the prediction ledger.** Both Kalshi laws used to read
    `kalshi_predictions.json`, which holds only the rows we chose to record, and
    both therefore reported CANNOT_TELL — "no event carried 2+ parseable rungs" —
    while `kalshi_scan_log.json`, written minutes earlier in the same directory,
    held 1,933 checkable rows of which the scanner itself had just rejected 441
    for violating this exact law.

    Reading the picks makes a law audit the output of its own consumer: it can
    only ever confirm what the filter already removed, which is circular and
    always clean. Reading the CLEARED rows instead makes the comparison real —
    source A is the scanner's verdict (it kept this quote), source B is the law
    re-derived from the raw prices. An inversion among the kept rows is a quote
    that got through the gate, which is a finding. Feeding it the DROPPED rows
    would be the opposite mistake: guaranteed VIOLATED on exactly the rows the
    firm correctly refused, a permanent red light that means nothing.
    """
    got = _kalshi_scan_report("kalshi_scan_log.json", "disagreement")
    if got[0] is None:
        return None, got[1]
    rows, report, age_h = got
    return rows, (f"{len(rows)} row(s) cleared by the scanner "
                  f"{age_h:.1f}h ago; {len(report.get('dropped_incoherent') or [])} "
                  f"were dropped and are deliberately NOT re-judged here")


@sources("kalshi quote, low rung", "kalshi quote, high rung (same event)")
def law_nested_probabilities_are_monotone(rows: Optional[List[dict]] = None) -> LawResult:
    """P(X > 0.5) >= P(X > 1.5) >= ... within one event. Arithmetic, not opinion.

    SOURCE A: the exchange's quote for the low rung.
    SOURCE B: the exchange's quote for the high rung of the SAME event.
    Two independent quotes; nesting binds them. Neither can be checked alone.

    This is the law that caught the Kalshi band. On 2026-08-17 all 22 nested
    goal-line events failed: 11 strictly inverted, 11 with every rung priced
    identically — six legs of one Chilean match all at 0.97, where "over 0.5
    goals" and "over 5.5 goals" cannot both be 97c. The settled ledger agreed
    from the other end: contracts quoted at 0.985 resolved YES 22.6% of the
    time, -0.68 per contract.

    No code review finds this, because the code is right. `tradeable()` sees a
    real ask and a real size and passes it. What is wrong is that a resting
    market-maker offer at the top of the range is not a probability, and the
    only way to know that is to make the numbers argue with each other.
    """
    name = "nested_probabilities_are_monotone"
    src = law_nested_probabilities_are_monotone.sources
    provenance = "rows supplied by caller"
    if rows is None:
        rows, provenance = _kalshi_ladder_rows()
        if rows is None:
            return LawResult(name, CANNOT_TELL,
                             _own(provenance), src)
    if not isinstance(rows, list):
        return LawResult(name, CANNOT_TELL, "ladder rows are not a list", src)

    ladders = _ladders(rows)
    if not ladders:
        return LawResult(name, CANNOT_TELL,
                         f"no event carried 2+ parseable rungs — nothing to "
                         f"compare ({provenance}). Owner: Kalshi sector.", src)

    bad, unprovable, untestable = [], 0, 0
    for (ev, direction), rungs in sorted(ladders.items()):
        ordered, asks, bids, hit, pairs = _ladder_fault(rungs, direction)
        if not pairs:
            untestable += 1
            continue
        if hit is None:
            # The ask ladder may still look inverted. That is not a finding:
            # see `_bid_of`. Counted, never alarmed on.
            if any(a is not None and asks[k + 1] is not None and a < asks[k + 1] - 1e-9
                   for k, a in enumerate(asks[:-1])) or len(set(asks)) == 1:
                unprovable += 1
            continue
        i, j, credit = hit
        bad.append({
            "event_ticker": ev,
            "direction": direction,
            "fault": "provable arbitrage",
            "detail": (f"ask {asks[i]:.2f} on the {ordered[i][0]:g} rung is BELOW "
                       f"bid {bids[j]:.2f} on the {ordered[j][0]:g} rung — buy the "
                       f"wider outcome for less than the narrower one sells for, "
                       f"for a {credit:.2f} credit and a payoff that cannot be negative"),
            "credit": credit,
            "rungs": [[t, row.get("market_price"), _bid_of(row)] for t, row in ordered],
        })

    checked = len(ladders) - untestable
    if not checked:
        return LawResult(name, CANNOT_TELL,
                         f"all {len(ladders)} ladder(s) are one-sided — no rung pair "
                         f"carries both an ask and a bid, so nothing here is "
                         f"falsifiable ({provenance}). Owner: Kalshi sector.", src)
    tail = (f"; {untestable} ladder(s) not testable (one-sided), "
            f"{unprovable} non-monotone on asks alone but spread-explicable")
    if not bad:
        return LawResult(name, HOLDS,
                         f"{checked} testable ladder(s) admit a monotone set of "
                         f"probabilities{tail}", src, n_examined=checked)
    return LawResult(
        name, VIOLATED,
        f"{len(bad)} of {checked} testable ladder(s) quote a nested pair that no "
        f"set of probabilities can produce{tail}",
        src, bad, n_examined=checked,
    )


# =============================================================================
# LAW — quoted prices have two sides
# =============================================================================

@sources("recorded ask (market_price)", "bid on the same contract")
def law_quoted_prices_have_two_sides(rows: Optional[List[dict]] = None) -> LawResult:
    """Every recorded quote had someone on the other side of it.

    SOURCE A: the ask the scanner recorded as `market_price`.
    SOURCE B: the bid on the same contract at the same instant.
    Independent — different participants place them — and a price is only a
    price when both exist. One side alone is an opinion nobody has met.

    This is the law that actually explains the Kalshi band, and it is stricter
    than the monotone-ladder law next to it. Measured 2026-08-17 across 10,000
    open markets: 7 had a real ask at a real size, and **zero** had a bid. The
    band the scanner was mining is not a set of near-certainties, it is the
    asking price of the only participant in an untraded book.

    The ledger's first four days cannot be judged by this law — nothing stored
    the bid — so those rows return CANNOT_TELL rather than passing. A row that
    predates its own audit field is exactly what must not be quietly counted as
    clean (memory `presence-is-not-currency`).
    """
    name = "quoted_prices_have_two_sides"
    src = law_quoted_prices_have_two_sides.sources
    provenance = "rows supplied by caller"
    if rows is None:
        rows, provenance = _kalshi_cleared_rows()
        if rows is None:
            return LawResult(name, CANNOT_TELL,
                             _own(provenance), src)
    if not rows:
        return LawResult(name, CANNOT_TELL,
                         f"the scanner cleared no rows ({provenance}) — nothing "
                         f"to check is not the same as nothing wrong", src)

    with_bid = [r for r in rows if r.get("yes_bid") is not None]
    if not with_bid:
        return LawResult(
            name, CANNOT_TELL,
            f"none of {len(rows)} row(s) carry a `yes_bid` — every one predates "
            f"the field. Their quotes cannot be shown to have had a second side, "
            f"and the settled ones did not behave as if they did (0.97 quoted, "
            f"0.27 realised, n=86). Owner: Kalshi sector.", src)

    one_sided = [r for r in with_bid
                 if r["yes_bid"] <= 0 or r["yes_bid"] >= r.get("market_price", 1.0)]
    if one_sided:
        return LawResult(name, VIOLATED,
                         f"{len(one_sided)} of {len(with_bid)} cleared row(s) carry a "
                         f"bid that is absent or crossed — `two_sided()` passed a "
                         f"quote with no live counterparty ({provenance})", src,
                         [{"ticker": r.get("ticker"), "bid": r.get("yes_bid"),
                           "ask": r.get("market_price")} for r in one_sided[:20]],
                         n_examined=len(with_bid))
    return LawResult(name, HOLDS,
                     f"{len(with_bid)} of {len(rows)} cleared row(s) carry a live "
                     f"two-sided book; {len(rows) - len(with_bid)} predate the "
                     f"field ({provenance})", src,
                     n_examined=len(with_bid))


# =============================================================================
# LAW — a surfaced rung clears its own ladder
# =============================================================================

@sources("the scanner's stored per-candidate coherence verdict",
         "the impossible-ladder set re-derived here from raw rungs")
def law_surfaced_rungs_clear_their_ladder(
        surfaced: Optional[List[dict]] = None,
        impossible: Optional[Iterable[str]] = None) -> LawResult:
    """Nothing the scanner surfaced sits in a ladder the monotone law calls impossible.

    SOURCE A: `coherence`, stamped on each surfaced row by `kalshi_scanner`'s own
    gate at scan time — the second line's decision, in its own words.
    SOURCE B: the impossible-ladder set, re-derived here by
    `law_nested_probabilities_are_monotone` from the raw rungs.

    Two checkers of one invariant, and **nothing was making them agree.** They do
    today, and the agreement is not luck: on 2026-08-24 the ladder law found four
    ladders quoting a provable arbitrage, and the one row the scanner dropped all
    day was a rung of `KXWNBAREB-26AUG24GSMIN` — priced 0.92, 13.2% annualized,
    exactly the kind of row the near-certainty band is built to want. Verified by
    hand: 0 of the 4 impossible ladders had a rung surfaced, and 0 of the 498
    uncheckable rows leaked into the surfaced set.

    A hand-verification that isn't a test decays, and this pair decays in a
    particularly quiet way: weaken the scanner's gate and the ladder law keeps
    reporting VIOLATED into the void — a true alarm, correctly raised, about
    ladders nobody is trading, while rungs of those same ladders sail through the
    gate. Each control stays individually honest and the pair stops meaning
    anything. That is `both-sides-of-a-comparison` with the halves in different
    modules.

    Two faults, and they carry different denominators on purpose:
      1. a surfaced row whose event is in the impossible set — the gate and the
         law disagree, which is the reason this law exists;
      2. a surfaced row whose own stored verdict is not HOLDS — the scan log
         contradicting itself, which needs no second source at all.

    Fault 2 is why a quiet day still means something. On a day the ladder law
    finds nothing impossible, fault 1 has a denominator of zero and proves
    nothing about the gate; fault 2's denominator is every surfaced row, so the
    law is never a pass over an empty set. The detail says how many impossible
    ladders were available to test against, so a HOLDS can be read for how much
    it is worth rather than taken at face value.

    Asymmetry that matters: when source B cannot be built, a PASS is not
    available — half a two-source comparison is not a comparison — but a fault-2
    FINDING still is. A finding from half a comparison is a finding; only the
    all-clear needs both halves.

    The over-refusal direction is deliberately not a fault. The scanner dropping
    a row the ladder law thinks is fine is the gate being stricter than the law,
    and alarming on that would pressure the gate to loosen toward this law —
    the same circularity `_kalshi_cleared_rows` refuses. It is counted and
    reported, never alarmed on.

    WHAT THIS ADDS over `test_the_scanner_and_the_law_share_one_definition_of_
    incoherent`, which already pins that `drop_incoherent` calls `_ladder_fault`
    rather than carrying its own copy of the arithmetic. That test closes the
    rule-drift route, and closing it is what makes the remaining two routes the
    interesting ones:
      - APPLICATION. Sharing the rule does not mean honoring it. The stamp and
        the routing are different steps (`kalshi_scanner` decides on
        `coherence != HOLDS` well after `_ladder_fault` returns), and a row can
        carry a correct VIOLATED and still be routed to `disagreement`. Fault 2
        reads the shipped scan log, so it sees the decision that was actually
        made, not the one the rule implies.
      - CORPUS. The two sides do not see the same rungs. The scanner judges the
        ladder it had in hand mid-scan; this law reads `kalshi_ladders.json`,
        the complete ladder including out-of-band rungs. Identical arithmetic
        over different inputs is exactly how two honest checkers reach opposite
        answers about one event, and fault 1 is the only thing looking at that
        gap.
    A unit test on a two-row fixture cannot reach either. Both need production
    rows and a denominator.
    """
    name = "surfaced_rungs_clear_their_ladder"
    src = law_surfaced_rungs_clear_their_ladder.sources
    provenance = "rows supplied by caller"
    if surfaced is None:
        surfaced, provenance = _kalshi_cleared_rows()
        if surfaced is None:
            return LawResult(name, CANNOT_TELL, _own(provenance), src)
    if not isinstance(surfaced, list):
        return LawResult(name, CANNOT_TELL, "surfaced rows are not a list", src)

    # SOURCE B. Only a VIOLATED ladder law yields an impossible set; any other
    # verdict means the set could not be BUILT, which is not the same as it
    # being empty, and the two must not collapse into each other here.
    ladder_note = "supplied by caller"
    if impossible is None:
        ladder = law_nested_probabilities_are_monotone()
        if ladder.verdict == VIOLATED:
            impossible = {e.get("event_ticker") for e in ladder.evidence
                          if isinstance(e, dict) and e.get("event_ticker")}
            ladder_note = (f"{len(impossible)} impossible ladder(s) to test "
                           f"against")
        elif ladder.verdict == HOLDS:
            impossible = set()
            ladder_note = ("the ladder law found nothing impossible today, so "
                           "the cross-check had a denominator of zero")
        else:
            impossible = None
            ladder_note = (f"the ladder law could not run "
                           f"({ladder.detail[:90]}), so the cross-check did "
                           f"not happen at all")
    impossible = set(impossible) if impossible is not None else None

    leaked, self_contradicting = [], []
    for row in surfaced:
        if not isinstance(row, dict):
            continue
        ev = row.get("event_ticker")
        if impossible and ev in impossible:
            leaked.append({"ticker": row.get("ticker"), "event_ticker": ev,
                           "stored_coherence": row.get("coherence"),
                           "fault": "surfaced from a ladder the monotone law "
                                    "says no probabilities can produce"})
        if row.get("coherence") != HOLDS:
            self_contradicting.append(
                {"ticker": row.get("ticker"), "event_ticker": ev,
                 "stored_coherence": row.get("coherence"),
                 "fault": "surfaced despite the scanner's own verdict"})

    bad = leaked + self_contradicting
    if bad:
        return LawResult(
            name, VIOLATED,
            f"{len(leaked)} surfaced row(s) sit in an impossible ladder and "
            f"{len(self_contradicting)} contradict the scanner's own stored "
            f"verdict, out of {len(surfaced)} surfaced ({provenance}; "
            f"{ladder_note}). Owner: Kalshi sector.",
            src, bad[:20], n_examined=len(surfaced))

    if impossible is None:
        return LawResult(
            name, CANNOT_TELL,
            f"all {len(surfaced)} surfaced row(s) carry their own HOLDS, but "
            f"{ladder_note} — one source agreeing with itself is not two "
            f"sources agreeing ({provenance}). Owner: Kalshi sector.", src)

    return LawResult(
        name, HOLDS,
        f"{len(surfaced)} surfaced row(s) all carry HOLDS and none sits in an "
        f"impossible ladder ({ladder_note}; {provenance})",
        src, n_examined=len(surfaced))


# =============================================================================
# LAW — runs match schedule
# =============================================================================

def _this_host() -> str:
    """This host's persisted identity, never one derived from the hostname.

    `single_writer` is the one place that knows it, and it knows it because it
    is written down rather than inferred — the correction from memory
    `two_brains_one_account`.
    """
    try:
        import single_writer
        return single_writer.this_host() or "unknown"
    except Exception:
        return "unknown"


def _host_for_alarm() -> str:
    """The label an alarm is FILED under. Must be distinct per machine, always.

    `_this_host()` above answers "who am I, or do I not know", and its degraded
    answer is the shared constant "unknown". That is the right answer for a law's
    prose and the wrong one for a key: two hosts that both answer "unknown" file
    their alarms under one identity, and the digest's collapse then deletes one
    machine's alarm as a re-run of the other's — the defect fixed in
    `email_buffer.buffer_email`. So the degraded branch falls back to something
    machine-distinct, deliberately re-accepting the drift the persisted id exists
    to remove. The asymmetry is the whole argument: an unstable label
    OVER-reports (two digest rows for one alarm — visible, annoying, harmless),
    a shared label UNDER-reports (one machine's alarm silently gone). Fail toward
    the visible one.

    This used to be `socket.gethostname().split(".")[0]`, read fresh on every
    send, 1500 lines below the docstring that forbids exactly that. A hostname is
    not an identity, it is a network condition: this laptop has `HostName: not
    set`, so `kern.hostname` is whatever the network last called it and macOS
    rewrites it on every transition — its mDNS collision counter is already at
    173. The live 133-item digest buffer carries ONE machine's coherence alarms
    under TWO interleaved labels: "Mac" (4, Aug 21-23) and "MacBook-Air-173" (39,
    Aug 17-26), neither superseding the other. `order_ledger._host_tag()` fixed
    this for order stamps in July and the fix was never carried here.
    """
    h = _this_host()
    if h and h != "unknown":
        return h
    try:
        import socket
        return "unknown-" + socket.gethostname().split(".")[0]
    except Exception:
        return "unknown-host"


def _designated_pipeline_host() -> Optional[str]:
    """The host that is supposed to be running main.py, or None if unknowable."""
    try:
        import single_writer
        return single_writer.designated_host() or None
    except Exception:
        return None


@sources("pipeline_heartbeat.json starts (observed)", "host schedule (intended)")
def law_runs_match_schedule(now: Optional[datetime] = None) -> LawResult:
    """Pipeline starts observed today == pipeline starts the schedule asks for.

    SOURCE A: `data/pipeline_heartbeat.json` -> `starts`, appended by main.py
              itself at the top of every run. What the pipeline DID.
    SOURCE B: this host's crontab / launchd schedule. What it was ASKED to do.

    The two are written by different things for different reasons and never
    read each other, which is the point.

    This is the law that would have caught catch_up. For six weeks the Mac ran
    main.py 12-22 times a day against a design of 4 — on 2026-08-14, pipeline_1400
    fired at 14:19, 14:44, 15:07, 15:31 and 15:55. Nothing reported it, because
    every other detector in the firm asks "did it run?" and the answer was yes,
    emphatically. Over-running is only visible to something holding a number to
    compare against.

    Tolerance is deliberately one-sided-ish: a shortfall late in the day is
    often just "the day is not over", so it is reported but ranked below an
    overrun, which is never legitimate.
    """
    name = "runs_match_schedule"
    src = law_runs_match_schedule.sources
    now = now or datetime.now()

    # The SCHEDULE is read first, and that ordering is the fix for a false
    # accusation. Read heartbeat-first, this law told every Mac session that
    # `main.py` was writing an obsolete heartbeat format — main.py's producer
    # was correct, and the file was simply the last one written before the Mac
    # stopped running the pipeline at all. A CANNOT_TELL that names the wrong
    # owner is worse than one that names none: it sends the next session to
    # repair working code (memory `verdict-with-no-consequence`).
    intended = _scheduled_pipeline_runs_today(now)
    if intended is None:
        return LawResult(name, CANNOT_TELL,
                         "could not read this host's schedule — `crontab -l` failed. "
                         "Owner: whoever owns this host's scheduler.", src)
    if intended == 0:
        designated = _designated_pipeline_host()
        if designated and _this_host() != designated:
            return LawResult(name, CANNOT_TELL,
                             f"this host ({_this_host()}) schedules no main.py runs — "
                             f"correct since it was narrowed to exit-only. The pipeline "
                             f"cadence belongs to '{designated}' and there is no state "
                             f"sync between hosts. Owner: {designated}.", src)
        return LawResult(name, VIOLATED,
                         f"this host ({_this_host()}) is the designated pipeline writer "
                         f"and its crontab schedules main.py ZERO times. The pipeline is "
                         f"not scheduled anywhere.", src,
                         [{"host": _this_host(), "intended": 0}],
                         n_examined=1)

    hb_path = DATA_DIR / "pipeline_heartbeat.json"
    if not hb_path.exists():
        return LawResult(name, CANNOT_TELL,
                         f"{hb_path} missing — cannot tell how often the pipeline "
                         f"ran. Owner: main.py heartbeat block.", src)
    try:
        hb = json.loads(hb_path.read_text())
    except (ValueError, OSError) as e:
        return LawResult(name, CANNOT_TELL, f"heartbeat unreadable: {e}", src)

    starts = hb.get("starts")
    if not isinstance(starts, list):
        return LawResult(name, CANNOT_TELL,
                         "heartbeat carries no `starts` history — main.py is writing "
                         "the single-timestamp form, which records THAT the pipeline "
                         "ran but not HOW OFTEN. Owner: main.py heartbeat block.", src)

    today = now.date()
    observed = 0
    for s in starts:
        try:
            if datetime.fromisoformat(str(s)[:26]).date() == today:
                observed += 1
        except ValueError:
            continue

    if observed > intended:
        return LawResult(name, VIOLATED,
                         f"main.py started {observed} time(s) today; the schedule asks "
                         f"for {intended}. An overrun is never legitimate — something is "
                         f"launching the pipeline outside the schedule.", src,
                         [{"observed": observed, "intended": intended}],
                         n_examined=len(starts))
    if observed < intended and now.hour >= 23:
        return LawResult(name, VIOLATED,
                         f"main.py started {observed} time(s) today against {intended} "
                         f"scheduled, and the day is over.", src,
                         [{"observed": observed, "intended": intended}],
                         n_examined=len(starts))
    # Denominator is the heartbeat history, not today's count: `observed == 0`
    # is a legitimate result on a quiet host, but a heartbeat that has never
    # recorded a single start means this law is agreeing with a file nobody
    # writes, which is the shape of `reader-outlived-its-producer`.
    return LawResult(name, HOLDS,
                     f"{observed} start(s) today, {intended} scheduled", src,
                     n_examined=len(starts))


def _scheduled_pipeline_runs_today(now: datetime) -> Optional[int]:
    """How many main.py runs this host's own scheduler asks for today.

    Reads the LIVE scheduler, not a doc. A doc is a third party's claim about
    the schedule; `crontab -l` is the schedule (memory `auditor_ground_truth`).
    Returns None when the schedule CANNOT BE READ, and 0 when it reads fine and
    asks for no runs. Those were one value until 2026-08-17 and they mean
    opposite things: the first is a degraded read, the second is a fact about
    this host — the Mac has had no main.py line since it was narrowed to
    exit-only, which is correct and permanent. Collapsing them made the law
    accuse `main.py`'s heartbeat block of writing an old format on a host where
    main.py had simply not run since 2026-08-14. One sentinel standing for two
    states with different consequences is `fallback-that-means-act`.

    0 is only safe because the caller acts on it: on the DESIGNATED host, zero
    scheduled runs is a violation, not an excuse.
    """
    import subprocess
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    weekday = now.weekday() < 5
    count = 0
    saw_any = False
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "main.py" not in line:
            continue
        saw_any = True
        parts = line.split()
        if len(parts) < 5:
            continue
        minute, hour, _dom, _mon, dow = parts[:5]
        if not weekday and dow not in ("*", "0-6", "*/1"):
            continue
        # An hour field of "9,13,17" or "*/4" expands to that many fires.
        count += _expand_field(hour, 24) * _expand_field(minute, 60)
    return count if saw_any else 0


def _expand_field(field: str, span: int) -> int:
    """How many values a single cron field covers. 1 for a literal, N for a list."""
    if field == "*":
        return span
    total = 0
    for part in field.split(","):
        if part.startswith("*/"):
            try:
                total += span // int(part[2:])
            except ValueError:
                total += 1
        elif "-" in part:
            try:
                lo, hi = part.split("-")[:2]
                total += max(int(hi) - int(lo) + 1, 1)
            except ValueError:
                total += 1
        else:
            total += 1
    return total


# =============================================================================
# LAW — the deployed set matches the repo
# =============================================================================

# ssh alias for the host that actually trades. From INFRASTRUCTURE.md row 1.
REMOTE_ALIAS = "apex-capital"
REMOTE_DIR = "~/institutional-tracker"

# Files that live in the repo and are *deliberately* not on the remote, or are
# deliberately per-host. Each needs a reason; an entry without one is how an
# exclusion list becomes the thing it was meant to police.
DEPLOY_EXEMPT = {
    # (none yet — every tracked .py is expected to reach the trading host)
}


@sources("git ls-files + local md5 (repo)", "md5sum + find on the trading host (deployed)")
def law_deployed_set_matches_repo(runner: Optional[Callable] = None,
                                  has_checkout: Optional[bool] = None) -> LawResult:
    """Every tracked .py in the repo is byte-identical on the trading host.

    SOURCE A: `git ls-files '*.py'` + local md5. What the firm WROTE.
    SOURCE B: `md5sum` over the same paths on the VM, plus a `find` of the VM's
              own .py files. What the firm is RUNNING.

    Neither is derived from `deploy.sh`, and that is the entire point. The audit
    that missed this asked "is everything in the deploy set on the VM?" —
    `deploy.sh` excluded `gcp_setup/`, so the VM's own rebuild script was three
    months stale and the fix for it could not reach the machine that needed it
    (memory `fix-that-cannot-reach-production`). An exclusion is invisible to any
    check that enumerates from the include list. This one enumerates from git.

    The `find` half is the other direction: files on the VM that are in no
    commit. That is not hypothetical — 97 of them were found on 2026-07-25,
    including a whole governance subsystem (memory `orphan_code_2026_07`).

    Runs where the git checkout is. On the VM there is no `.git` (deploy.sh
    rsyncs with --exclude=.git/), so this returns CANNOT_TELL naming the Mac
    rather than pretending.

    `has_checkout` overrides that probe and exists for one reason: the probe ran
    BEFORE the injected `runner`, so the four tests that drive both sides through
    a fake runner short-circuited on any host without a repo — they passed on the
    Mac and failed on the VM, which is the two-zone divergence in memory
    `host_divergence_is_a_bug_class` showing up inside the suite meant to catch
    it. A caller supplying both sides is not a host pretending it has a repo.
    Default None keeps production on the real probe.
    """
    name = "deployed_set_matches_repo"
    src = law_deployed_set_matches_repo.sources
    import subprocess
    runner = runner or (lambda cmd, timeout: subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout))

    if has_checkout is None:
        has_checkout = ((BASE_DIR / ".git").exists()
                        or (BASE_DIR.parent / ".git").exists())
    if not has_checkout:
        return LawResult(name, CANNOT_TELL,
                         "no git checkout on this host — the deployed set can only be "
                         "compared where the repo is. Owner: the Mac (dev host).", src)
    try:
        ls = runner(["git", "-C", str(BASE_DIR), "ls-files", "*.py"], 30)
    except Exception as e:
        return LawResult(name, CANNOT_TELL, f"git ls-files failed: {e}", src)
    if getattr(ls, "returncode", 1) != 0:
        return LawResult(name, CANNOT_TELL, "git ls-files failed on this host", src)

    import hashlib
    local = {}
    for rel in ls.stdout.split():
        if rel in DEPLOY_EXEMPT:
            continue
        f = BASE_DIR / rel
        if not f.exists():
            continue
        local[rel] = hashlib.md5(f.read_bytes()).hexdigest()
    if not local:
        return LawResult(name, CANNOT_TELL, "no tracked .py files found — refusing to "
                                            "report a match against an empty set", src)

    # One round trip. `md5sum` for the repo->VM direction, `find` for VM->repo.
    paths = " ".join("'" + p.replace("'", "'\\''") + "'" for p in sorted(local))
    remote_cmd = (
        f"cd {REMOTE_DIR} 2>/dev/null || exit 9; "
        f"md5sum {paths} 2>&1; echo '---FIND---'; "
        # depth 3, not 2: agents/archive/*.py is a real tracked directory and a
        # shallower sweep reports every file in it as absent from the host.
        f"find . -maxdepth 3 -name '*.py' -not -path './.git/*' "
        f"-not -path '*/__pycache__/*' | sed 's|^\\./||'"
    )
    try:
        out = runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                      REMOTE_ALIAS, remote_cmd], 120)
    except Exception as e:
        return LawResult(name, CANNOT_TELL,
                         f"cannot reach {REMOTE_ALIAS} ({type(e).__name__}: {e}) — the "
                         f"deployed set is unknown, not known-good. Owner: Chairman "
                         f"(host reachability).", src)
    if getattr(out, "returncode", 1) == 9:
        return LawResult(name, CANNOT_TELL, f"{REMOTE_DIR} does not exist on "
                                            f"{REMOTE_ALIAS}", src)
    if not out.stdout.strip():
        return LawResult(name, CANNOT_TELL,
                         f"ssh to {REMOTE_ALIAS} returned nothing "
                         f"({(getattr(out, 'stderr', '') or '')[:180]})", src)

    head, _, tail = out.stdout.partition("---FIND---")
    remote_md5, missing = {}, []
    for line in head.splitlines():
        line = line.strip()
        if not line:
            continue
        # GNU md5sum errors read "md5sum: <path>: No such file or directory".
        # `.strip("md5sum ")` would strip a CHARACTER SET, not a prefix, and on
        # the first pass it turned every missing path into the empty string —
        # the count was right and the evidence named nothing.
        if ": No such file" in line or "cannot open" in line:
            parts = line.split(":")
            if len(parts) >= 3:
                missing.append(parts[1].strip())
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 32:
            remote_md5[parts[1].strip()] = parts[0]

    absent = sorted(set(local) - set(remote_md5) | set(missing))
    differs = sorted(p for p, h in local.items()
                     if p in remote_md5 and remote_md5[p] != h)
    vm_only = sorted(set(p.strip() for p in tail.split() if p.strip()) - set(local))

    ev = ([{"absent_on_host": p} for p in absent[:8]]
          + [{"differs": p} for p in differs[:8]]
          + [{"on_host_but_in_no_commit": p} for p in vm_only[:8]])
    if absent or differs or vm_only:
        return LawResult(name, VIOLATED,
                         f"{len(absent)} tracked file(s) absent from {REMOTE_ALIAS}, "
                         f"{len(differs)} differ, {len(vm_only)} file(s) run there that "
                         f"are in no commit. The host is not running the repo.", src, ev,
                         n_examined=len(local))
    return LawResult(name, HOLDS,
                     f"{len(local)} tracked .py identical on {REMOTE_ALIAS}, "
                     f"no uncommitted files there", src, n_examined=len(local))


# The `data/` files the firm deliberately ships. Enumerating these from git —
# the way the .py law does — is wrong here and would be the opposite error:
# most tracked data/*.json is a report that is MEANT to stay per-host, and
# flagging all of it would bury the handful that matter. For code, "tracked"
# means "should be running there". For data, the allowlist IS the decision, so
# that is the honest source. The risk of enumerating from an include list —
# that deleting a line silences the check — is not left hanging: a smoke test
# pins the set as shrink-only, so removing an entry fails the suite rather
# than quietly narrowing this law's denominator.
_ALLOWLIST_RE = r'--include="(data/[^"]+)"'


@sources("deploy.sh data/ allowlist ∩ git ls-files + local md5 (what we chose to ship)",
         "md5sum on the trading host (what it actually has)")
def law_shipped_config_matches_repo(runner: Optional[Callable] = None,
                                    has_checkout: Optional[bool] = None) -> LawResult:
    """Every data/ file the firm chose to deploy is present and identical there.

    The .py law above cannot see this class at all, and the gap was not
    theoretical. `data/signoff_mode.json` — the autonomy gate's SHADOW/ENFORCE
    record — was tracked in git from 2026-07-29 and never named in deploy.sh's
    allowlist, so it had never once reached the VM. Nothing reported it: the .py
    law does not look at data/, and `get_mode()` returns SHADOW for an absent
    file exactly as it does for a recorded one, so both hosts printed the same
    string for entirely different reasons.

    Two ways to fail, and the first needs no network:

    1. AN INERT INCLUDE. `deploy.sh` builds its exclude list from
       `git ls-files --others`, so an UNTRACKED file is held back no matter how
       many --include lines name it. An allowlist entry for an untracked file is
       a line that reads like a decision and ships nothing.
    2. ABSENT OR DRIFTED. The file is allowlisted and tracked and still is not
       byte-identical on the trading host.

    Owner on a non-checkout host is the Mac, same as the .py law: deploy is
    one-way, and the question "what did we choose to ship" can only be asked
    where deploy.sh and the repo are.
    """
    name = "shipped_config_matches_repo"
    src = law_shipped_config_matches_repo.sources
    import re
    import subprocess
    import hashlib
    runner = runner or (lambda cmd, timeout: subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout))

    if has_checkout is None:
        has_checkout = ((BASE_DIR / ".git").exists()
                        or (BASE_DIR.parent / ".git").exists())
    if not has_checkout:
        return LawResult(name, CANNOT_TELL,
                         "no git checkout on this host — what the firm CHOSE to ship "
                         "is only knowable where deploy.sh and the repo are. "
                         "Owner: the Mac (dev host).", src)

    deploy_sh = BASE_DIR / "gcp_setup" / "deploy.sh"
    if not deploy_sh.exists():
        return LawResult(name, CANNOT_TELL,
                         f"{deploy_sh.name} not found — the shipped set is unknown, "
                         f"not known-good. Owner: whoever owns deploy.sh", src)
    allowed = sorted(set(re.findall(_ALLOWLIST_RE, deploy_sh.read_text())))
    # `--include="data/"` is the directory itself, not a file to compare.
    allowed = [p for p in allowed if not p.endswith("/")]
    if not allowed:
        return LawResult(name, CANNOT_TELL,
                         "no data/ allowlist entries parsed out of deploy.sh — "
                         "refusing to report a match against an empty set", src)

    try:
        ls = runner(["git", "-C", str(BASE_DIR), "ls-files"] + allowed, 30)
    except Exception as e:
        return LawResult(name, CANNOT_TELL, f"git ls-files failed: {e}", src)
    if getattr(ls, "returncode", 1) != 0:
        return LawResult(name, CANNOT_TELL, "git ls-files failed on this host", src)
    tracked = set(ls.stdout.split())

    inert = sorted(p for p in allowed if p not in tracked)
    local = {}
    for rel in allowed:
        if rel not in tracked:
            continue
        f = BASE_DIR / rel
        if f.exists():
            local[rel] = hashlib.md5(f.read_bytes()).hexdigest()

    if not local and not inert:
        return LawResult(name, CANNOT_TELL,
                         "every allowlisted data/ file is tracked but missing on "
                         "disk here — nothing to compare", src)

    remote_md5, missing = {}, []
    if local:
        paths = " ".join("'" + p.replace("'", "'\\''") + "'" for p in sorted(local))
        try:
            out = runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                          REMOTE_ALIAS,
                          f"cd {REMOTE_DIR} 2>/dev/null || exit 9; "
                          f"md5sum {paths} 2>&1"], 60)
        except Exception as e:
            return LawResult(name, CANNOT_TELL,
                             f"cannot reach {REMOTE_ALIAS} ({type(e).__name__}: {e}) — "
                             f"the shipped set is unknown, not known-good. "
                             f"Owner: Chairman (host reachability).", src)
        if getattr(out, "returncode", 1) == 9:
            return LawResult(name, CANNOT_TELL,
                             f"{REMOTE_DIR} does not exist on {REMOTE_ALIAS}", src)
        if not (getattr(out, "stdout", "") or "").strip():
            return LawResult(name, CANNOT_TELL,
                             f"ssh to {REMOTE_ALIAS} returned nothing "
                             f"({(getattr(out, 'stderr', '') or '')[:180]})", src)
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if ": No such file" in line or "cannot open" in line:
                parts = line.split(":")
                if len(parts) >= 3:
                    missing.append(parts[1].strip())
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 32:
                remote_md5[parts[1].strip()] = parts[0]

    absent = sorted(set(local) - set(remote_md5) | set(missing))
    differs = sorted(p for p, h in local.items()
                     if p in remote_md5 and remote_md5[p] != h)

    ev = ([{"inert_include": p} for p in inert[:8]]
          + [{"absent_on_host": p} for p in absent[:8]]
          + [{"differs": p} for p in differs[:8]])
    if inert or absent or differs:
        return LawResult(name, VIOLATED,
                         f"{len(inert)} allowlist entr(ies) name an untracked file so "
                         f"the include is inert, {len(absent)} chosen file(s) absent "
                         f"from {REMOTE_ALIAS}, {len(differs)} differ. The host is not "
                         f"running the configuration the firm decided on.", src, ev,
                         n_examined=len(local))
    return LawResult(name, HOLDS,
                     f"{len(local)} allowlisted data/ file(s) tracked and identical "
                     f"on {REMOTE_ALIAS}", src, n_examined=len(local))


# =============================================================================
# LAW — every verb emitted is a verb handled
# =============================================================================

# The keys the consumer actually reads. manager_agent.py:256 is
# `rec.get("action") or rec.get("signal", "BUY_NEW")` — so these two keys, and
# no others. `verdict` is a DIFFERENT vocabulary with a different consumer
# (fi_pipeline filters STRONG_BUY/BUY/WATCH and never hands `verdict` to the
# manager); including it here would manufacture violations that are not real.
_VERB_KEYS = ("action", "signal")


def _emitted_verbs(agent_dir: Optional[Path] = None) -> dict:
    """{verb: {files}} for every action/signal string literal in the agent pool.

    AST, not grep. `manager_agent.py` carries a 20-line comment naming "BUY" and
    every other verb in this vocabulary; a text search would match the
    postmortem and report the bug as its own fix. Comments are never AST nodes.

    Walks the whole value subtree rather than matching a bare Constant, because
    the real emitters are conditional expressions:
        "action": "BUY" if conviction >= 3 else "WATCH"
    Matching only `ast.Constant` finds none of the eight agents that shipped the
    escape.
    """
    agent_dir = agent_dir or (BASE_DIR / "agents")
    import ast as _ast
    out = defaultdict(set)
    for f in sorted(agent_dir.glob("*.py")):
        try:
            tree = _ast.parse(f.read_text())
        except (SyntaxError, OSError):
            continue

        def _collect(node, where):
            for c in _ast.walk(node):
                if isinstance(c, _ast.Constant) and isinstance(c.value, str):
                    v = c.value.strip()
                    if v and v.replace("_", "").isalpha() and v.upper() == v:
                        out[v].add(where)

        for n in _ast.walk(tree):
            if isinstance(n, _ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, _ast.Constant) and k.value in _VERB_KEYS:
                        _collect(v, f.name)
            elif isinstance(n, _ast.Assign):
                for t in n.targets:
                    if isinstance(t, _ast.Name) and t.id in _VERB_KEYS:
                        _collect(n.value, f.name)
            elif (isinstance(n, _ast.AnnAssign) and isinstance(n.target, _ast.Name)
                  and n.target.id in _VERB_KEYS and n.value is not None):
                _collect(n.value, f.name)
    return out


@sources("agents/*.py action|signal literals (producers)",
         "manager_agent KNOWN_ACTIONS + ACTION_ALIASES (consumer)")
def law_emitted_verbs_are_handled(agent_dir: Optional[Path] = None) -> LawResult:
    """Every action verb the agent pool emits is one the manager can name.

    SOURCE A: the producers' own source — string literals reaching `action` /
              `signal` in `agents/*.py`.
    SOURCE B: the consumer's declared vocabulary — `manager_agent.KNOWN_ACTIONS`
              plus `ACTION_ALIASES`.

    Neither reads the other, which is why the gap survived 66 days. Six FI agents
    emitted `"BUY"`; the manager branched on BUY_NEW / ADD / SELL / TRIM / HOLD /
    WATCH with no `else` on either chain. `execute_decision()` fell through and
    returned `{"executed": False, "error": None}` — printed as "✗ Failed: None",
    which reads as a broker having a bad day, six times a day. Meanwhile
    `evaluate_recommendation()` kept its initialiser `decision = "APPROVED"`, so
    ~118 orders per cycle were stamped APPROVED having passed no gate at all
    (memory `unknown-verb-approved-nothing`).

    SCOPE, stated so nobody mistakes this for more than it is: `agents/` only.
    `dynamic_position_sizer.py` emits `action: "SKIP"` into position_sizes.json,
    which is a different vocabulary with a different consumer. Widening this law
    to all of data/ would flag that on day one and every day after, and a law
    that cries wolf daily is a law that gets muted.
    """
    name = "emitted_verbs_are_handled"
    src = law_emitted_verbs_are_handled.sources
    agent_dir = agent_dir or (BASE_DIR / "agents")
    if not agent_dir.is_dir():
        return LawResult(name, CANNOT_TELL, f"{agent_dir} missing — cannot read what "
                                            f"the producers emit", src)
    try:
        sys.path.insert(0, str(BASE_DIR))
        import manager_agent
        handled = set(manager_agent.KNOWN_ACTIONS) | set(manager_agent.ACTION_ALIASES)
    except Exception as e:
        return LawResult(name, CANNOT_TELL,
                         f"cannot read the consumer's vocabulary ({type(e).__name__}: "
                         f"{e}) — an unreadable handler set is not an empty one", src)
    if not handled:
        return LawResult(name, CANNOT_TELL,
                         "manager_agent declares no actions — refusing to compare "
                         "against an empty vocabulary", src)

    emitted = _emitted_verbs(agent_dir)
    if not emitted:
        return LawResult(name, CANNOT_TELL,
                         f"no action/signal literals found in {agent_dir} — either the "
                         f"pool is empty or the scan stopped matching how agents are "
                         f"written. Owner: whoever last changed the agent template.", src)

    unhandled = {v: sorted(f)[:4] for v, f in emitted.items() if v not in handled}
    if unhandled:
        return LawResult(name, VIOLATED,
                         f"{len(unhandled)} verb(s) emitted that the manager cannot "
                         f"name: {', '.join(sorted(unhandled))}. An unrecognised verb "
                         f"is not rejected, it is unheard.", src,
                         [{"verb": v, "emitted_by": f} for v, f in sorted(unhandled.items())],
                         n_examined=len(emitted))
    return LawResult(name, HOLDS,
                     f"{len(emitted)} verb(s) emitted ({', '.join(sorted(emitted))}), "
                     f"all handled", src, n_examined=len(emitted))


# =============================================================================
# LAW — lifecycle statuses are runnable
# =============================================================================

def _subscript_key(node) -> Optional[str]:
    """The literal key of `x[...]`, across the 3.8→3.9 ast.Index change."""
    sl = getattr(node, "slice", None)
    if sl is None:
        return None
    if sl.__class__.__name__ == "Index":            # <=3.8
        sl = getattr(sl, "value", None)
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return sl.value
    return None


def _ladder_assigned_statuses(src: str) -> dict:
    """Every literal status the ladder writes to an agent → the line it does it.

    AST, not grep, and not negotiably so: chief_of_staff_agent.py and
    agent_lifecycle.py now both carry paragraphs of postmortem prose naming
    "noticed" and "pip", so a text search finds this law's own explanation of
    the bug and reports the bug fixed. Only an assignment to `agent["status"]`
    actually moves an agent between stages, so only that is counted.
    """
    out: dict = {}
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Subscript) and _subscript_key(tgt) == "status":
                out.setdefault(node.value.value, node.lineno)
    return out


@sources("statuses the CoS ladder assigns (its own AST)",
         "statuses the firm declares runnable or terminal (agent_lifecycle)")
def law_lifecycle_statuses_are_runnable(ladder_src: Optional[str] = None) -> LawResult:
    """Every status the performance ladder can assign is one an agent survives.

    SOURCE A: what the ladder DOES — `agent["status"] = "..."` in
              chief_of_staff_agent.py, read from its syntax tree.
    SOURCE B: what the firm SAYS those statuses mean — agent_lifecycle's
              RUNNABLE_STATUSES ∪ TERMINAL_STATUSES.

    Neither reads the other. That is the whole point: for months the ladder
    assigned "noticed" and "pip" while research_team.py loaded a hand-written
    ("active", "reassigned"), and both halves were internally consistent. Nothing
    compared them, so a coaching stage was silently a removal from service —
    and since recovery required an approved pick, an agent that could not run
    could not be saved. On 2026-08-28 that took the VM's research pool from 50
    agents to 8 in a single pass, 64% of the roster, in a firm whose Chairman's
    standing instruction is that losing track of an agent is disastrous.

    A status that is neither runnable nor terminal is a third thing nobody
    designed: employed on paper, unable to work, unable to appeal, still
    counting down. This law exists so the NEXT such status is caught on the
    commit that introduces it rather than by reading a log cliff six hours late.
    """
    name = "lifecycle_statuses_are_runnable"
    src_names = law_lifecycle_statuses_are_runnable.sources

    try:
        sys.path.insert(0, str(BASE_DIR))
        import agent_lifecycle
        declared = set(agent_lifecycle.RUNNABLE_STATUSES) | set(agent_lifecycle.TERMINAL_STATUSES)
    except Exception as e:
        return LawResult(name, CANNOT_TELL,
                         f"cannot read the declared status vocabulary "
                         f"({type(e).__name__}: {e}) — an unreadable set is not "
                         f"an empty one", src_names)
    if not declared:
        return LawResult(name, CANNOT_TELL,
                         "agent_lifecycle declares no statuses — refusing to compare "
                         "against an empty vocabulary", src_names)

    if ladder_src is None:
        ladder = BASE_DIR / "chief_of_staff_agent.py"
        if not ladder.exists():
            return LawResult(name, CANNOT_TELL,
                             f"{ladder.name} missing — cannot read what the ladder "
                             f"assigns. Owner: whoever moved the CoS factory.",
                             src_names)
        ladder_src = ladder.read_text()

    try:
        assigned = _ladder_assigned_statuses(ladder_src)
    except SyntaxError as e:
        return LawResult(name, CANNOT_TELL,
                         f"the ladder does not parse ({e}) — cannot tell what it "
                         f"assigns", src_names)
    if not assigned:
        return LawResult(name, CANNOT_TELL,
                         "no `agent[\"status\"] = ...` assignment found — either the "
                         "ladder stopped setting status directly or this scan stopped "
                         "matching how it is written. Owner: whoever last changed "
                         "_update_agent_performance.", src_names)

    orphans = {s: ln for s, ln in assigned.items() if s not in declared}
    if orphans:
        return LawResult(
            name, VIOLATED,
            f"{len(orphans)} of {len(assigned)} status(es) the ladder assigns are "
            f"neither runnable nor terminal: "
            f"{', '.join(f'{s!r} (line {ln})' for s, ln in sorted(orphans.items()))}. "
            f"An agent put there is employed, unable to work, and still counting "
            f"down toward retirement.",
            src_names,
            [{"status": s, "assigned_at_line": ln,
              "runnable": sorted(agent_lifecycle.RUNNABLE_STATUSES)}
             for s, ln in sorted(orphans.items())],
            n_examined=len(assigned))

    return LawResult(name, HOLDS,
                     f"{len(assigned)} status(es) assignable by the ladder "
                     f"({', '.join(sorted(assigned))}), all runnable or terminal",
                     src_names, n_examined=len(assigned))


# =============================================================================
# LAW — positions conserve
# =============================================================================

@sources("Alpaca get_all_positions (broker)", "state.json entry_dates (internal ledger)")
def law_positions_conserve(broker_positions: Optional[dict] = None,
                           state: Optional[dict] = None,
                           host: Optional[str] = None) -> LawResult:
    """What the broker holds == what this host's ledger says it opened.

    SOURCE A: Alpaca `get_all_positions()`. The only medium every host shares,
              and the only one that cannot be wrong about what we own.
    SOURCE B: `data/state.json` -> `entry_dates`, written by this firm when it
              opens a position.

    The escape this is built from is still open as a watch item: state.json
    showed 0 positions while Alpaca held 16. Every downstream rule that reads the
    ledger — stop-loss, holding period, position cap, the pod ledger — was
    reasoning about a portfolio that did not exist, and nothing complained,
    because state.json is perfectly consistent with itself.

    HOST DISCIPLINE: the ledger only means something on the designated writer.
    There is no state sync between hosts (CLAUDE.md), so the Mac's `entry_dates`
    is a month-old fossil and comparing it to the live broker would VIOLATE every
    single day, forever, correctly and uselessly. Off the writer this returns
    CANNOT_TELL and names the host that owns the answer — which is the honest
    verdict, not a downgrade (memory `verdict-with-no-consequence`).
    """
    name = "positions_conserve"
    src = law_positions_conserve.sources
    try:
        import single_writer
        designated = single_writer.designated_host()
        host = host or single_writer.this_host()
    except Exception as e:
        return LawResult(name, CANNOT_TELL, f"cannot resolve host identity: {e}", src)

    if broker_positions is None and host != designated:
        return LawResult(name, CANNOT_TELL,
                         f"this host is '{host}'; the ledger that matters belongs to "
                         f"'{designated}' and there is no state sync between hosts. "
                         f"Owner: {designated}.", src)

    if broker_positions is None:
        broker_positions = single_writer._positions_map()
    if broker_positions is None:
        return LawResult(name, CANNOT_TELL,
                         "broker position lookup failed — an unreadable broker is not a "
                         "flat book (memory `nan-is-not-none`)", src)

    if state is None:
        sp = DATA_DIR / "state.json"
        if not sp.exists():
            return LawResult(name, CANNOT_TELL, f"{sp} missing", src)
        try:
            state = json.loads(sp.read_text())
        except (ValueError, OSError) as e:
            return LawResult(name, CANNOT_TELL, f"state.json unreadable: {e}", src)

    entries = state.get("entry_dates")
    if not isinstance(entries, dict):
        return LawResult(name, CANNOT_TELL,
                         "state.json carries no `entry_dates` map — the ledger has no "
                         "opinion to check. Owner: main.py state writer.", src)

    held = {s.upper() for s, q in broker_positions.items() if q}
    ledger = {s.upper() for s in entries}
    unledgered = sorted(held - ledger)     # broker holds it, the firm forgot
    phantom = sorted(ledger - held)        # the firm thinks it holds it, it does not

    if unledgered or phantom:
        return LawResult(name, VIOLATED,
                         f"{len(unledgered)} position(s) held at the broker with no ledger "
                         f"entry, {len(phantom)} ledger entr(ies) with no position. Every "
                         f"stop-loss and holding-period rule reads the ledger.", src,
                         [{"held_not_ledgered": s} for s in unledgered[:10]]
                         + [{"ledgered_not_held": s} for s in phantom[:10]],
                         # The UNION, not len(held) as the HOLDS path uses. When
                         # the law holds the two sets are equal so either works;
                         # when it fails, a phantom is a symbol the broker does
                         # not hold, and counting it against `held` alone can
                         # report more failures than things examined.
                         n_examined=len(held | ledger))
    return LawResult(name, HOLDS,
                     f"{len(held)} broker position(s), same {len(ledger)} in the ledger",
                     src, n_examined=len(held))


# =============================================================================
# LAW — broker host census
# =============================================================================

def infrastructure_doc() -> Optional[Path]:
    """Where the host inventory actually is on THIS host, or None.

    Two shapes, because the two hosts have two shapes. On the Mac the doc sits
    at the repo root, one level above this package. On the VM there is no repo
    root — deploy.sh scp's the doc into the package directory itself. A resolver
    hard-coded to `BASE_DIR.parent` reported "missing" on the VM while the file
    sat right beside it, which is the same host-shaped assumption that had the
    doc undeployed in the first place.

    Returns None rather than a non-existent Path: the caller must be able to
    tell "no inventory" from "an inventory I could not parse".
    """
    for cand in (BASE_DIR.parent / "INFRASTRUCTURE.md",
                 BASE_DIR / "INFRASTRUCTURE.md"):
        if cand.exists():
            return cand
    return None


# Every host permitted to place an order on the shared account, and why.
# Pinned to INFRASTRUCTURE.md by TestCoherenceLaws — the constant may not drift
# from the registry silently, because a census whose source B is a doc nobody
# updates is exactly how a third host ran for 69 days (memory `oracle_third_host`).
DOCUMENTED_HOST_TAGS = {
    "apexcapitalc": "GCP VM — designated writer (INFRASTRUCTURE.md row 1)",
    "mac":          "this laptop — exit-only since 2026-08-17 (row 2)",
    # Same laptop, before `data/.host_id` existed. `_host_tag()` derived the tag
    # from socket.gethostname() on every call, so one machine answered to two
    # names depending on which network it had joined. Its last order under this
    # tag was 2026-08-07T14:02Z, hours before .host_id was seeded (the file's
    # mtime is 2026-08-07 13:00 local). Listed, not silently forgiven: an
    # undocumented tag on this account is the finding, and the honest way to
    # clear a real one is to write it down.
    "macbookair17": "this laptop pre-.host_id, retired 2026-08-07 (row 2)",
}


@sources("Alpaca order history, host tag off client_order_id",
         "DOCUMENTED_HOST_TAGS / INFRASTRUCTURE.md")
def law_broker_host_census(orders: Optional[list] = None,
                           documented: Optional[dict] = None) -> LawResult:
    """Every host placing orders is a host the firm has written down.

    SOURCE A: the broker's order history, host tag parsed off `client_order_id`.
              Not a host list — a record of who actually traded.
    SOURCE B: DOCUMENTED_HOST_TAGS, the written inventory.

    The Oracle instance ran a full parallel firm for 69 days on this account. It
    did not hide: it placed orders and sent emails. It was invisible because the
    host list WAS the doc, and the doc did not have it — the firm asked its own
    inventory how many hosts it had, and the inventory answered from itself. Only
    the broker can count hosts, because it is the one thing every host must touch
    (memory `two_brains_one_account`).

    UNSTAMPED is a distinct unknown host, never dropped — dropping it is how a
    foreign order becomes invisible to the check written to find it. But it is
    only judged from `order_ledger.STRICT_FROM` onward: before that instant no
    host stamped anything, so counting those orders would brand weeks of the
    firm's own legitimate history as strangers and the law would VIOLATE forever
    on facts nobody can change. The cutover is read from the producer, not
    copied here, so the two cannot drift apart.
    """
    name = "broker_host_census"
    src = law_broker_host_census.sources
    documented = DOCUMENTED_HOST_TAGS if documented is None else documented
    try:
        import single_writer
        from order_ledger import STRICT_FROM
        strict_from = datetime.fromisoformat(STRICT_FROM)
        if orders is None:
            orders = single_writer.fetch_broker_orders(limit=500)
        if orders is None:
            return LawResult(name, CANNOT_TELL,
                             "could not read the broker's order history — the census is "
                             "unknown, not clean", src)
        seen = single_writer.hosts_last_seen(orders)
    except Exception as e:
        return LawResult(name, CANNOT_TELL,
                         f"census unavailable ({type(e).__name__}: {e})", src)

    if not seen:
        return LawResult(name, CANNOT_TELL,
                         "no orders on file — no orders is evidence of nothing, not "
                         "evidence of one host (memory `presence-is-not-currency`)", src)

    if all(ts < strict_from for ts in seen.values()):
        return LawResult(name, CANNOT_TELL,
                         f"every order on file predates stamping ({STRICT_FROM}) — the "
                         f"census cannot be taken from unstamped history", src)

    strangers = sorted(h for h, ts in seen.items()
                       if h not in documented and ts >= strict_from)
    if strangers:
        return LawResult(name, VIOLATED,
                         f"{len(strangers)} host(s) placed orders on this account that "
                         f"are in no registry row: {', '.join(strangers)}. An unlisted "
                         f"writer sizes against the same capital and cannot be seen by "
                         f"anything that reads the doc.", src,
                         [{"host": h, "last_order": seen[h].isoformat()} for h in strangers],
                         n_examined=len(seen))
    return LawResult(name, HOLDS,
                     "; ".join(f"{h} last traded {seen[h].date()}" for h in sorted(seen)),
                     src, n_examined=len(seen))


# =============================================================================
# LAW — every order this host placed went through the choke point
# =============================================================================

@sources("Alpaca order history for orders stamped by THIS host",
         "data/order_ledger.json, written inside the patched submit_order")
def law_broker_orders_are_ledgered(orders: Optional[list] = None,
                                   ledger_ids: Optional[set] = None,
                                   host: Optional[str] = None) -> LawResult:
    """Every order the broker attributes to this host is one the ledger recorded.

    SOURCE A: the broker's order history, filtered to the stamp of THIS host.
              What actually reached Alpaca.
    SOURCE B: `data/order_ledger.json`, appended by `order_ledger.record()`
              from inside the patched `TradingClient.submit_order`.

    WHY THIS EXISTS, and why the gate log could not answer it. `risk_gate.check`
    is called inside that same patched hook, before `original(...)`, and the
    smoke suite pins that by AST. So no order that goes THROUGH the hook can
    skip the gate — the two are the same function call. The question the gate
    log cannot answer is the other one: **did an order reach the broker without
    going through the hook at all?** A raw REST POST, a second SDK client class,
    a subprocess — none of them would appear in the gate log, and their absence
    from it looks exactly like a quiet day. An empty gate log is not evidence.

    This law asks the broker instead. If Alpaca shows an order carrying this
    host's stamp that the ledger never recorded, that order was placed by a path
    that skipped `record()` — and therefore skipped `risk_gate.check`, the
    single-writer guard, and the stamp's own source attribution.

    Measured on the VM 2026-08-20: 57 of 57 post-cutover orders stamped
    `apexcapitalc` are in its ledger. That is what makes the choke point
    complete, and it is a fact the gate log was structurally unable to report.

    SCOPE, deliberately narrow on three axes:
      · STRICT_FROM. Before the stamping cutover nothing was stamped, so
        pre-cutover orders cannot be attributed and are not judged. Read from
        the producer so the two cannot drift.
      · This host only. There is no state sync between hosts (CLAUDE.md), so the
        other host's orders are correctly absent from this ledger. Comparing
        them would VIOLATE every day, forever, and correctly.
      · UNSTAMPED orders are `law_broker_host_census`'s finding, not this one —
        an order with no stamp cannot be attributed to any host, so this law has
        nothing to say about it and says so rather than inventing an owner.
    """
    name = "broker_orders_are_ledgered"
    src = law_broker_orders_are_ledgered.sources
    try:
        import single_writer
        from order_ledger import STRICT_FROM, parse_stamp
        strict_from = datetime.fromisoformat(STRICT_FROM)
        if host is None:
            host = single_writer.this_host()
        if orders is None:
            orders = single_writer.fetch_broker_orders(limit=500)
        if ledger_ids is None:
            ledger_ids = __import__("order_ledger").known_order_ids()
    except Exception as e:
        return LawResult(name, CANNOT_TELL,
                         f"cannot compare ({type(e).__name__}: {e}) — an unreadable "
                         f"broker or ledger is not a clean one", src)

    if orders is None:
        return LawResult(name, CANNOT_TELL,
                         "could not read the broker's order history", src)
    if not host:
        return LawResult(name, CANNOT_TELL,
                         "this host has no stable identity, so no order can be "
                         "attributed to it (see order_ledger._host_tag)", src)

    mine, unattributed, missing = 0, 0, []
    for o in orders:
        get = o.get if isinstance(o, dict) else (lambda k, _o=o: getattr(_o, k, None))
        raw = get("submitted_at") or get("created_at")
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < strict_from:
            continue
        coid = get("client_order_id")
        stamp = parse_stamp(coid)
        if stamp is None:
            unattributed += 1
            continue
        if stamp.get("host") != host:
            continue
        mine += 1
        oid = get("id") or get("order_id")
        if str(oid) not in ledger_ids and str(coid) not in ledger_ids:
            missing.append({"order_id": str(oid), "client_order_id": str(coid),
                            "symbol": str(get("symbol")), "side": str(get("side")),
                            "submitted_at": str(raw)})

    if missing:
        return LawResult(name, VIOLATED,
                         f"{len(missing)} of {mine} order(s) stamped '{host}' are at the "
                         f"broker but were never recorded by order_ledger. An order that "
                         f"skipped record() skipped the risk gate and the single-writer "
                         f"guard in the same breath — there is a path to the broker that "
                         f"does not go through the choke point.", src, missing[:10],
                         n_examined=mine)

    if mine == 0:
        return LawResult(name, CANNOT_TELL,
                         f"no order stamped '{host}' since the cutover ({STRICT_FROM}) — "
                         f"zero orders is not a clean choke point, it is an untested one "
                         f"(memory `presence-is-not-currency`)"
                         + (f"; {unattributed} unstamped order(s) are "
                            f"broker_host_census's finding" if unattributed else ""), src)

    return LawResult(name, HOLDS,
                     f"all {mine} order(s) stamped '{host}' since {strict_from.date()} "
                     f"passed through order_ledger, and therefore through risk_gate.check"
                     + (f"; {unattributed} unstamped (see broker_host_census)"
                        if unattributed else ""),
                     src, n_examined=mine)


# =============================================================================
# LAW — work queues drain
# =============================================================================

# Which data/ files are WORK QUEUES. A queue holds things that must still
# happen; a log holds things that already did. Only the first has a drain, so
# only the first can fail to drain, and `*_log.json` is deliberately absent from
# this list — 12 of the firm's queue-shaped files are append-only records whose
# stock is SUPPOSED to rise forever. Reporting them would be an always-true,
# never-closable finding (memory `an-aggregate-is-not-a-finding`).
#
# The name only decides SCOPE. It never decides the verdict: `rd_pod_pending.json`
# is in scope with 18 rows and passes because its board reads-and-rewrites it, so
# the measured age of its oldest row is zero. The measurement does the work.
_QUEUE_GLOBS = ("*_queue.json", "*_pending.json", "*_requests.json")

# Where a row's arrival time hides. Producers here are not consistent about this
# and never will be; a queue whose rows carry NO recognised timestamp is reported
# as unmeasurable rather than assumed fresh — assuming fresh is how a dead queue
# stays quiet.
_QUEUE_TS_KEYS = ("timestamp", "ts", "queued_at", "created_at", "created",
                  "first_seen", "submitted_at", "date", "when")

# How many complete passes of the firm's ENTIRE schedule a row may survive before
# its presence is evidence that nothing is taking it. Not a staleness threshold
# in days — the day count is derived below from the schedule itself, so retiring
# the last weekly job tightens this automatically instead of leaving a hardcoded
# number pointing at a cadence that no longer exists.
_DRAIN_SLACK_PASSES = 2.0

# cron's `@` shortcuts, in days between consecutive runs — the WORST gap, not the
# average, because that is the number the deadline is priced in (`@monthly` at 31,
# not 30). `@reboot` maps to None on purpose: it is the one shortcut with no
# cadence at all, and it must not be allowed to set this number.
_AT_SHORTCUTS = {
    "@reboot": None, "@hourly": 1.0 / 24, "@daily": 1.0, "@midnight": 1.0,
    "@weekly": 7.0, "@monthly": 31.0, "@yearly": 366.0, "@annually": 366.0,
}


def _slowest_scheduled_gap_days(schedule_text: str) -> Optional[float]:
    """The longest any scheduled job in this firm can go between two runs, in days.

    This is the unit the drain deadline is priced in. The question a drain check
    really asks is not "is this row old?" — old is meaningless without a
    reference — but "has every job in the firm had its turn, and did none of
    them take this row?" One pass of the slowest cadence is exactly that turn.

    Day granularity on purpose. A job firing six times a day and a job firing
    once both come back as 1.0, which understates nothing that matters here
    because we take the MAX across jobs: only the sparse end of the schedule can
    set this number, and at the sparse end the intra-day times are noise.

    `@reboot` is skipped rather than counted as an infinite gap. A boot-triggered
    job has no cadence at all, and letting one set this number would push the
    deadline to infinity and silence the law — the `fallback-that-means-act`
    shape, where the degraded reading is the permissive one.

    The other `@` shortcuts DO have cadences and are priced from `_AT_SHORTCUTS`.
    Skipping them wholesale (as this did when first written) is not neutral: a
    firm whose slowest job is `@weekly` would derive its deadline from the next
    slowest line instead, and 7 real days of tolerance would silently become 1.
    An unrecognised `@token` is skipped, and that direction is deliberate — the
    aggregate here is a MAX, so anything left out only makes the deadline
    TIGHTER, i.e. the law alarms sooner rather than going quiet.
    """
    from calendar import monthrange
    horizon = 28
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    worst = None
    for raw in schedule_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            shortcut = _AT_SHORTCUTS.get(line.split()[0].lower())
            if shortcut:                   # @reboot maps to None: no cadence
                worst = max(worst or 0.0, shortcut)
            continue
        if line.startswith("launchd "):
            # data/mac_launchd_snapshot.txt: "launchd <label> StartInterval=900 x.py"
            m = re.search(r"StartInterval=(\d+)", line)
            if m and int(m.group(1)) > 0:
                worst = max(worst or 0.0, int(m.group(1)) / 86400.0)
            continue
        parts = line.split()
        if len(parts) < 5 or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            continue
        _minute, _hour, dom, mon, dow = parts[:5]
        fires = []
        for d in range(horizon):
            day = base + timedelta(days=d)
            if not _cron_field_matches(mon, day.month, 12):
                continue
            dom_star, dow_star = dom.strip() == "*", dow.strip() == "*"
            dom_hit = _cron_field_matches(dom, day.day, monthrange(day.year, day.month)[1])
            dow_hit = _cron_field_matches(dow, day.weekday() + 1 if day.weekday() < 6 else 0, 7)
            # cron's documented OR rule: with BOTH day fields restricted, either
            # one firing is enough. Treating it as AND would report a job that
            # runs twice a week as one that never runs, and inflate this number.
            if dom_star and dow_star:
                hit = True
            elif dom_star or dow_star:
                hit = dom_hit if dow_star else dow_hit
            else:
                hit = dom_hit or dow_hit
            if hit:
                fires.append(d)
        if len(fires) < 2:
            continue                       # too sparse to measure over 28 days
        gap = max(b - a for a, b in zip(fires, fires[1:]))
        worst = max(worst or 0.0, float(gap))
    return worst


def _cron_field_matches(field: str, value: int, span: int) -> bool:
    """Does one cron field admit `value`? Handles *, a,b, x-y and */n."""
    field = field.strip()
    if field == "*":
        return True
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            step = int(s) if s.isdigit() else 1
        if part in ("*", ""):
            lo, hi = 0, span
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                continue
            lo, hi = int(a), int(b)
        elif part.isdigit():
            lo = hi = int(part)
        else:
            continue
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
        # cron accepts both 0 and 7 for Sunday.
        if span == 7 and value == 0 and lo <= 7 <= hi and (7 - lo) % step == 0:
            return True
    return False


# A row carrying one of these has already been dealt with. It is still in the
# file, but it is no longer work.
#
# Deliberately NOT here: `approved`, `escalated_to_board`, `queued`, `open`,
# `needs_human`, `protected`, `systemic`, `STALE`. Every one of those is a row
# that has moved but not finished, and calling any of them terminal is how a
# real backlog goes silent. `approved` is the sharpest: an approved proposal is
# the one MOST likely to be waiting on someone. The asymmetry is deliberate —
# an unrecognised status counts as outstanding, so a new verb the firm invents
# tomorrow makes this law noisier, never blinder.
_TERMINAL_ROW_STATUSES = frozenset({
    "answered", "archived", "canceled", "cancelled", "closed", "complete",
    "completed", "declined", "done", "dropped", "expired", "failed",
    "implemented", "obsolete", "rejected", "resolved", "skipped",
    "superseded",
})

_ROW_STATUS_KEYS = ("status", "state")


def _row_is_outstanding(row: dict) -> bool:
    """Is this row still work, or was it done in place and left where it lay?

    Measured 2026-08-27: `status` is the ONLY status key in use across all
    13,731 rows of the firm's in-scope queue files, so this reads `status` and
    `state` and does not speculate further. Anything it does not recognise —
    a missing key, a non-string, a verb not in the terminal set — is
    OUTSTANDING. Unknown is not done.
    """
    for key in _ROW_STATUS_KEYS:
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip().lower() in _TERMINAL_ROW_STATUSES:
            return False
    return True


def _oldest_row_age_days(path: Path, now: datetime) -> Optional[float]:
    """Age of the oldest OUTSTANDING row in a queue file, or None if unmeasurable.

    Residence time is what this measures, and residence has two disciplines the
    original version of this function conflated:

      * a POP queue drains by removal — its stock falls when work is consumed,
        so the age of what remains reads residence time directly.
      * a MARK queue drains by flipping a field — `status: "completed"` and the
        row stays put forever. Its stock is monotonically non-decreasing, so
        "age of the oldest row still present" returns the age of the first row
        ever written, which grows without bound.

    THE ESCAPE. On the VM 2026-08-27 this law reported VIOLATED on
    `ml_longterm_experiment_queue.json` and `ml_shortterm_experiment_queue.json`
    — "95.7d, 13.7 complete passes of the firm's schedule, the work in them was
    produced and never done." All 13 and all 6 rows respectively were
    `status: "completed"`. Nineteen finished experiments, reported as nineteen
    stranded ones. Worse than the noise: every mark queue in the firm is
    guaranteed to cross ANY deadline eventually, so the law was on a path to
    permanent, structural VIOLATED — and because `stranded` is sorted by age, a
    genuinely stuck 20-day row would have sorted BELOW a completed row from May
    and been named nowhere in the detail line (memory
    `budget-spent-in-the-wrong-order`).

    Note this was invisible to the poison pill by construction: the pill injects
    `queues={...: 400.0}`, a pre-computed age, so it proves the law's
    CONJUNCTION and never touches the measurement. `TestQueueAgeCountsOnlyWork`
    exercises this function against real file bytes for that reason.

    Returns:
      * None — no rows, or outstanding rows carrying no recognised timestamp.
        Neither is "fresh", and the caller must not read it as a pass.
      * 0.0 — rows exist and every one of them is terminal. Nothing is waiting,
        which is a real measurement of zero, not an absence of one: it counts
        in the denominator and it is a genuine observation that this queue does
        drain.
    """
    try:
        blob = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    rows = blob if isinstance(blob, list) else None
    if rows is None and isinstance(blob, dict):
        rows = next((v for v in blob.values() if isinstance(v, list) and v), None)
    if not rows:
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None
    outstanding = [r for r in rows if _row_is_outstanding(r)]
    if not outstanding:
        return 0.0
    oldest = None
    for row in outstanding:
        for key in _QUEUE_TS_KEYS:
            raw = row.get(key)
            if not isinstance(raw, str) or len(raw) < 10:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                break
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if oldest is None or dt < oldest:
                oldest = dt
            break
    if oldest is None:
        return None
    return (now - oldest).total_seconds() / 86400.0


_READER_GRAPH_CACHE: List = []


def _reader_graph():
    """(sources, STRICTLY scheduler-reachable set, wiring_audit) — built once a run.

    Memoised because `scheduler_reachable` walks every module in the firm and
    costs ~20s, and the drain law asks it once per over-deadline queue. A law
    whose cost scales with the number of findings gets its findings capped by
    whoever notices the pipeline slowing down.

    `strict=True`, and that is the whole reliability of this law's second source.
    The loose walk adds an edge wherever a scheduled module merely NAMES another
    by bare name, and `wiring_audit`'s own docstring says to refuse it "where a
    false pass is the expensive one". Here it is the expensive one and the
    difference is not academic: `ml_data_watchdog.py`, `ml_drift_monitor.py`,
    `ml_research_team_leader.py` and `ml_digest.py` are loose-reachable from all
    66 scheduler roots and strictly reachable from NONE — the ML research chain
    is in no crontab on either host. Under the loose walk this law certified
    `digest_queue.json` as drained by `ml_digest.py`, a module that never runs.
    A false all-clear from the one control built to catch false all-clears is
    worse than no control, so the error is pushed onto the other side: an
    unresolved-but-real invocation edge yields a false VIOLATED, which is noisy,
    visible, and triageable in a way that silence is not.
    """
    if _READER_GRAPH_CACHE:
        return _READER_GRAPH_CACHE[0]
    try:
        import wiring_audit as wa
        srcs = wa._py_sources()
        reachable = wa.scheduler_reachable(srcs, strict=True)
    except Exception:
        return None
    if reachable is None:
        return None
    _READER_GRAPH_CACHE.append((srcs, reachable, wa))
    return _READER_GRAPH_CACHE[0]


def _independent_scheduled_readers(fname: str,
                                   graph: Optional[tuple] = None) -> Optional[List[str]]:
    """Modules that READ `fname` and are not merely writing it back.

    Three filters, each earning its place:

      * a module that only WRITES the file is not a reader. `digest_queue.json`
        has three modules that load it, append, and store it — a writer's own
        load-append-store, which the plain "does the name appear in this file"
        test counts as consumption, and so certifies a queue with no drain at all.
      * a module scheduled NOWHERE is not a drain, however well it reads. That is
        `orphan_agents_blindness` — reachability is not employment — and it is
        why this walks the scheduler rather than the import graph.
      * `test_*` and the audit tools are excluded: this file names queue
        filenames while explaining past bugs, and a tool must not become the
        consumer of the thing it audits by describing it. For the same reason
        the mention test runs against `_code_only(txt)` — the three ML monitors
        each carry a comment saying digest_queue.json is no longer theirs, and
        a raw text search promoted all three back to readers on the strength of
        the sentence explaining that they are not. A postmortem comment is not
        a data dependency.

    Returns None when the reader graph cannot be built (wiring_audit missing, or
    no schedule to walk) — the source is absent, not clean.

    `graph` is a `(sources, reachable, wiring_audit)` triple, for tests only. It
    substitutes the module TEXT, never the three filters above — those still run
    against the real `wiring_audit` helpers, so a test that feeds a synthetic
    writer is exercising the shipped classifier and not a stand-in for it.
    """
    graph = graph or _reader_graph()
    if graph is None:
        return None
    srcs, reachable, wa = graph
    out = []
    for name, txt in srcs.items():
        if name.startswith("test_") or fname not in txt:
            continue
        if name in ("wiring_audit.py", "internal_audit_agent.py", "coherence_laws.py"):
            continue
        if name not in reachable or fname not in wa._code_only(txt):
            continue
        try:
            writes = wa._declared_outputs(txt, wa.DATA_JSON_RE)
        except Exception:
            writes = []
        if fname in writes and not re.search(
                r"\b(?:pop|remove|del\s|clear|drain|dequeue)\b", txt):
            continue                       # writes it, never takes anything out
        out.append(name)
    return sorted(out)


@sources("queue files on disk (how long work has waited)",
         "schedule + reader graph (whether anything can take it)")
def law_work_queues_drain(now: Optional[datetime] = None,
                          queues: Optional[dict] = None,
                          schedule_text: Optional[str] = None) -> LawResult:
    """Rows enter a work queue, so rows must leave it.

    SOURCE A: `data/*_queue.json` etc — the age of the oldest row STILL THERE.
              Written by producers who have no idea whether anyone drains them.
    SOURCE B: the scheduler manifests plus the reader graph — whether any
              scheduled module can take a row out, and how long a full pass of
              the firm's schedule takes.

    The two never read each other, which is the point. A dead queue is perfectly
    self-consistent: three producers append, the file grows, every write
    succeeds, and no error is ever raised. Nothing inside source A can tell you
    the work was never done.

    THE ESCAPE THIS IS BUILT FROM. `data/digest_queue.json` held three ML data-
    quality WARNINGs stamped 2026-06-12 — 73 days undelivered when this was
    written. `ml_data_watchdog.py` sends CRITICAL by email and routes WARNING
    here, and here has no consumer: its only three mentioning modules are its own
    three writers. Two detectors had it in view and both waved it through.
    `check_orphan_queues` skips any file with rows (`non-empty = clearly used`) —
    but rows nobody drains ARE the failure, and emptiness is the one state that
    proves nothing (memory `presence-is-not-currency`). The orphan-agent walk
    counted those three writers as readers of each other's output.

    VIOLATED needs BOTH halves to fail: aged past the deadline AND no independent
    scheduled reader. Age alone is not a defect — `chairman_decision_queue.json`
    has 170 rows and the oldest is months old because it is waiting on a human,
    and the human IS the drain. A law that cannot tell a backlog from a severed
    wire would fire on both and be muted for it within a week.
    """
    name = "work_queues_drain"
    src = law_work_queues_drain.sources
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if schedule_text is None:
        try:
            import wiring_audit as wa
            parts = []
            for reader in (wa._cron_snapshot, wa._launchd_snapshot):
                p = reader()
                if p.exists():
                    parts.append(p.read_text())
            schedule_text = "\n".join(parts)
        except Exception as e:
            return LawResult(name, CANNOT_TELL,
                             f"cannot read the schedule ({type(e).__name__}: {e}) — "
                             f"with no cadence there is no deadline to price a row's "
                             f"age against. Owner: whoever owns the snapshots.", src)
    if not schedule_text.strip():
        return LawResult(name, CANNOT_TELL,
                         "no schedule snapshot on this host — a row's age means "
                         "nothing without a pass of the firm's schedule to measure "
                         "it against. Run scripts/refresh_crontab_snapshot.sh.", src)

    gap = _slowest_scheduled_gap_days(schedule_text)
    if not gap:
        return LawResult(name, CANNOT_TELL,
                         "the schedule parsed to no measurable cadence — cannot "
                         "derive a drain deadline. Owner: coherence_laws "
                         "_slowest_scheduled_gap_days.", src)
    deadline = gap * _DRAIN_SLACK_PASSES

    if queues is None:
        queues = {}
        seen = set()
        for pat in _QUEUE_GLOBS:
            for fp in sorted(DATA_DIR.glob(pat)):
                if fp.name in seen:
                    continue
                seen.add(fp.name)
                queues[fp.name] = _oldest_row_age_days(fp, now)

    measurable = {k: v for k, v in queues.items() if v is not None}
    if not measurable:
        return LawResult(name, CANNOT_TELL,
                         f"{len(queues)} queue file(s) in scope and not one carries a "
                         f"row with a readable timestamp — residence time is "
                         f"unmeasurable, which is not the same as zero. Owner: "
                         f"queue producers.", src)

    stranded = []
    for fname, age in sorted(measurable.items(), key=lambda kv: -kv[1]):
        if age <= deadline:
            continue
        readers = _independent_scheduled_readers(fname)
        if readers is None:
            return LawResult(
                name, CANNOT_TELL,
                f"{fname}'s oldest row is {age:.0f}d old against a {deadline:.0f}d "
                f"deadline, but the reader graph could not be built — cannot tell a "
                f"severed wire from a backlog. Owner: wiring_audit.", src)
        if readers:
            continue                       # a real consumer exists: this is a backlog
        stranded.append({
            "queue": fname, "oldest_row_age_days": round(age, 1),
            "deadline_days": round(deadline, 1),
            "passes_survived": round(age / gap, 1),
            "independent_scheduled_readers": 0,
            "fault": "rows are aging in a queue no scheduled module drains",
        })

    if stranded:
        worst = stranded[0]
        return LawResult(
            name, VIOLATED,
            f"{len(stranded)} of {len(measurable)} measurable work queue(s) hold rows "
            f"that no scheduled module can take out — worst is {worst['queue']} at "
            f"{worst['oldest_row_age_days']}d, {worst['passes_survived']} complete "
            f"passes of the firm's schedule (deadline {deadline:.0f}d = "
            f"{_DRAIN_SLACK_PASSES:g} x the {gap:.0f}d slowest cadence). The work in "
            f"them was produced and never done. Owner: each queue's producer.",
            src, stranded[:20], n_examined=len(measurable))

    return LawResult(
        name, HOLDS,
        f"all {len(measurable)} measurable work queue(s) either drain inside the "
        f"{deadline:.0f}d deadline or have a scheduled consumer holding a backlog "
        f"({len(queues) - len(measurable)} more carry no readable row timestamp)",
        src, n_examined=len(measurable))


# =============================================================================
# LAW — recorded exits are executed
# =============================================================================

# Terminal, and did not fill. Deliberately NOT including `replaced` (the
# replacement can still fill) or `done_for_day` (a day order that may be
# re-sent): a law that escalates must be conservative about what it calls
# permanent, because the cost of a false VIOLATED is that the next real one
# gets ignored.
_EXIT_DEAD_STATES = {"canceled", "cancelled", "expired", "rejected"}

# Longest legitimate queue observed on this account: 65.51h, an after-hours
# decision released after a long weekend (measured 2026-08-27 over 320 orders).
# Past that, "still pending" stops being the broker's release schedule and
# starts being an order nobody is going to fill.
_EXIT_PENDING_GRACE_H = 96.0


@sources("state.json sell_log (this firm's own record of closed trades, and the "
         "pnl_pct that cit_director turns into the firm's win rate)",
         "Alpaca order history (what the broker actually filled)")
def law_recorded_exits_are_executed(sell_log: Optional[list] = None,
                                    orders: Optional[list] = None,
                                    host: Optional[str] = None,
                                    now: Optional[datetime] = None) -> LawResult:
    """Every exit the firm booked a P&L for is an exit the broker actually made.

    SOURCE A: `data/state.json` -> `sell_log`, appended by `main.py` when it
              decides to close a position. Each row carries a `pnl_pct`.
    SOURCE B: the broker's order history — `filled_qty` on the order that was
              supposed to do it.

    WHY THIS IS NOT `positions_conserve` OR `broker_orders_are_ledgered`.
    `positions_conserve` compares the broker to `entry_dates`, and
    `entry_date_ledger.reconcile()` heals `entry_dates` from fill history on the
    next pipeline run — so that law goes quiet again by design, which is correct
    for it and useless here. This law reads `sell_log`, which is append-only and
    never healed: a wrong row stays wrong forever. `broker_orders_are_ledgered`
    runs the other direction (an order at the broker -> did it pass our choke
    point). Neither can see a booked exit that never executed.

    THE ESCAPE. `main.py` writes the sell_log row — with its P&L — on
    `submit_sell` RETURNING A DICT, and `submit_sell` returns one as soon as
    Alpaca accepts the order. `alpaca_trader.py` sets `pnl_pct` from
    `pos["unrealized_pct"]`, the mark at DECISION time. Alpaca queues orders
    placed outside the extended-hours window and releases them at the next open,
    so an exit decided at 20:15 UTC is recorded as a completed trade roughly 17
    hours before it happens — median 17.31h, max 65.51h across the 40 orders
    this account created after the close in 45 days. Measured 2026-08-27: NTNX
    sat in `sell_log` as `take_profit (20.6%)`, `pnl_pct +20.556`, and at the
    broker it was `ACCEPTED, filled 0/90`. It was a winner in the firm's win
    rate and the shares were still in the account.

    THE HONEST SIZE OF IT. On that day 38 of 39 recorded exits had really
    filled, so the live damage was 1.9 points of win rate (25.6% -> 23.7%) and
    neither of `cit_director`'s 40%/70% proposal thresholds moved. This is worth
    a law anyway for two reasons: nothing else in the firm can see the failure
    at all, and the case that IS permanent — an order that goes terminal without
    filling — books a P&L for a trade that never happened and never
    self-corrects.

    WHAT COUNTS AS THE FAILURE, narrowly:
      · terminal and unfilled (canceled/expired/rejected) -> VIOLATED. The exit
        will never happen; the booked P&L is fiction, permanently.
      · pending past 96h -> VIOLATED. Longer than the worst legitimate queue
        ever observed here, so it is stuck, not waiting.
      · pending inside 96h -> reported in the detail, NOT violated. That is the
        broker's release schedule doing its job, and alarming on it nightly
        would be correct and useless.

    THE WINDOW TRAP, which is the inverse of the usual one. The broker fetch is
    `limit=500&direction=desc`, and this account has more orders than that
    across the sell_log's span — so the oldest rows have no order in the fetched
    window. Reading "no matching order" as "never executed" would VIOLATE on
    every old row forever. Rows predating the window are counted as unverifiable
    and excluded from `n_examined`, and the detail says how many, so a shrinking
    verifiable window is visible instead of being reported as a clean pass.

    MATCHING. Rows written from 2026-08-27 carry `order_id` and match exactly.
    Legacy rows have none — main.py had `submit_sell`'s return dict, which
    contains one, and persisted only the ticker — so those fall back to
    (ticker, side=sell, created_at within 4 days). The fallback deliberately
    takes the best-filled candidate, so several rows may map to one order. That
    biases toward HOLDS, which is the right direction for a heuristic feeding an
    escalation queue, and the fallback's share shrinks visibly as new rows
    accumulate.
    """
    name = "recorded_exits_are_executed"
    src = law_recorded_exits_are_executed.sources
    now = now or datetime.now(timezone.utc)

    # Host resolution lives INSIDE the "read it myself" branch on purpose. A
    # caller that supplied both measurements has already answered the question
    # this guard asks, and resolving anyway would make the poison pill depend on
    # single_writer, on broker credentials, and on whichever machine the audit
    # happens to run on.
    if sell_log is None:
        try:
            import single_writer
            designated = single_writer.designated_host()
            host = host or single_writer.this_host()
        except Exception as e:
            return LawResult(name, CANNOT_TELL,
                             f"cannot resolve host identity: {e}", src)

        # sell_log is host-local and there is no state sync (CLAUDE.md). Off the
        # designated writer it is a fossil, and comparing a fossil to the live
        # broker would VIOLATE every day, correctly and uselessly.
        if host != designated:
            return LawResult(name, CANNOT_TELL,
                             f"this host is '{host}'; the sell_log that matters "
                             f"belongs to '{designated}' and there is no state sync "
                             f"between hosts. Owner: {designated}.", src)

        sp = DATA_DIR / "state.json"
        if not sp.exists():
            return LawResult(name, CANNOT_TELL, f"{sp} missing", src)
        try:
            sell_log = json.loads(sp.read_text()).get("sell_log")
        except (ValueError, OSError) as e:
            return LawResult(name, CANNOT_TELL,
                             f"state.json unreadable: {e} — an unreadable ledger is "
                             f"not an empty one", src)
        if not isinstance(sell_log, list):
            return LawResult(name, CANNOT_TELL,
                             "state.json carries no `sell_log` list — the firm has "
                             "recorded no exits to check. Owner: main.py state writer.",
                             src)

    if orders is None:
        try:
            import single_writer
            orders = single_writer.fetch_broker_orders(limit=500)
        except Exception as e:
            return LawResult(name, CANNOT_TELL,
                             f"cannot read the broker's order history "
                             f"({type(e).__name__}: {e}) — refusing to call every "
                             f"recorded exit unexecuted because the broker was "
                             f"unreachable", src)
    if orders is None:
        return LawResult(name, CANNOT_TELL,
                         "could not read the broker's order history", src)
    if not sell_log:
        return LawResult(name, CANNOT_TELL,
                         "sell_log is empty — nothing recorded, so nothing to verify. "
                         "A pass on zero rows is not a pass.", src)

    def _dt(v):
        if not v:
            return None
        s = str(v).replace("Z", "+00:00")
        # NORMALISE THE FRACTIONAL SECONDS TO EXACTLY SIX DIGITS. Python 3.9's
        # `fromisoformat` accepts a fraction of exactly 3 or 6 digits and
        # nothing else, and Alpaca supplies whatever is left after stripping
        # trailing zeros: across 500 live orders the widths were
        # {3: 1, 4: 7, 5: 55, 6: 436, 9: 1}. Sixty-two of them — every 4-, 5-
        # and 9-digit stamp — raised, and the law read that as "no exit order at
        # the broker", reporting AFRM, KEY, SOFI and NTNX as exits that never
        # executed when all four had real orders and only NTNX was genuine. A
        # false VIOLATED assembled entirely out of a parser limit, caught
        # pre-ship 2026-08-27.
        #
        # THIS IS A HOST-DIVERGENCE BUG (memory `host_divergence_is_a_bug_class`).
        # The VM runs 3.11, where `fromisoformat` takes any width, so the law
        # would have been correct on the trading host and blind on this laptop —
        # and the push gate runs the suite twice on the Mac only, so nothing in
        # the commit path compares the two. Pad-and-truncate rather than
        # truncate: truncation alone fixes the one 9-digit stamp and leaves the
        # 62 short ones broken.
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    sells, by_id, unreadable_ts = [], {}, 0
    for o in orders:
        oid = str(o.get("id") or "")
        if oid:
            by_id[oid] = o
        if o.get("created_at") and _dt(o.get("created_at")) is None:
            unreadable_ts += 1
        if str(o.get("side") or "").lower() == "sell":
            sells.append(o)

    # An order whose clock cannot be read is missing from the candidate set by
    # an unknown amount, and the failure is silent and one-directional: it can
    # only ever produce "no order found", never a spurious match. So the whole
    # heuristic path becomes untrustworthy the moment ANY timestamp fails to
    # parse, and the honest verdict is that the comparison could not be made —
    # not a list of exits declared fictional because of a date format.
    if unreadable_ts:
        return LawResult(name, CANNOT_TELL,
                         f"{unreadable_ts} of {len(orders)} broker order(s) carry a "
                         f"created_at this law cannot parse, so the candidate set is "
                         f"incomplete by an unknown amount and every unmatched row "
                         f"would convict on a format change. Owner: whoever last "
                         f"changed the broker timestamp format.", src)

    window_open = min([d for d in (_dt(o.get("created_at")) for o in orders) if d],
                      default=None)

    violations, pending, unverifiable, matched, no_order = [], [], [], 0, 0
    for row in sell_log:
        tkr = row.get("ticker")
        if not tkr or not row.get("date"):
            continue
        try:
            day = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        oid = str(row.get("order_id") or "")
        if oid and oid in by_id:
            order, mode = by_id[oid], "order_id"
        else:
            mode = "heuristic"
            cands = [o for o in sells
                     if o.get("symbol") == tkr
                     and _dt(o.get("created_at")) is not None
                     and abs((_dt(o["created_at"]) - day).days) <= 4]
            if not cands:
                # `.date()`, not the raw datetime: a row is dated midnight, so
                # comparing it against the window's oldest timestamp would call
                # same-day exits unverifiable. Predating the window is "cannot
                # verify", NOT "never executed" — the distinction this law turns
                # on, and the one that would otherwise make it VIOLATE forever
                # on rows nobody can check.
                if window_open is not None and day.date() < window_open.date():
                    unverifiable.append({"ticker": tkr, "date": row["date"],
                                         "why": "predates the fetched order window"})
                else:
                    no_order += 1
                    violations.append({"ticker": tkr, "date": row["date"],
                                       "pnl_pct": row.get("pnl_pct"),
                                       "reason": row.get("reason", ""), "match": mode,
                                       "why": "no exit order at the broker at all"})
                continue
            order = max(cands, key=lambda o: float(o.get("filled_qty") or 0))

        matched += 1
        if float(order.get("filled_qty") or 0) > 0:
            continue

        status = str(order.get("status") or "").lower()
        created = _dt(order.get("created_at"))
        age_h = (now - created).total_seconds() / 3600.0 if created else None
        entry = {"ticker": tkr, "date": row["date"], "pnl_pct": row.get("pnl_pct"),
                 "reason": row.get("reason", ""), "order_id": str(order.get("id") or ""),
                 "status": status, "qty": order.get("qty"), "match": mode,
                 "age_h": round(age_h, 2) if age_h is not None else None}
        if status in _EXIT_DEAD_STATES:
            entry["why"] = (f"order is {status} and never filled — the booked P&L is "
                            f"permanent fiction")
            violations.append(entry)
        elif age_h is not None and age_h > _EXIT_PENDING_GRACE_H:
            entry["why"] = (f"unfilled for {age_h:.0f}h, past the "
                            f"{_EXIT_PENDING_GRACE_H:.0f}h grace — longer than any "
                            f"legitimate queue observed on this account")
            violations.append(entry)
        else:
            pending.append(entry)

    n = matched + no_order
    tail = ""
    if unverifiable:
        tail += (f" {len(unverifiable)} row(s) predate the fetched order window and "
                 f"were not judged.")
    if pending:
        tail += (f" {len(pending)} exit(s) queued but not yet filled "
                 f"({', '.join(p['ticker'] for p in pending[:4])}) — inside the "
                 f"{_EXIT_PENDING_GRACE_H:.0f}h grace, reported not alarmed.")

    if violations:
        booked = sum(1 for v in violations if (v.get("pnl_pct") or 0) > 0)
        return LawResult(name, VIOLATED,
                         f"{len(violations)} of {n} recorded exit(s) never executed "
                         f"({booked} of them booked as WINNERS in the firm's win rate): "
                         f"{', '.join(v['ticker'] for v in violations[:6])}. A P&L the "
                         f"broker never made is still counted by cit_director.{tail}",
                         src, violations, n_examined=n)

    return LawResult(name, HOLDS,
                     f"{n} recorded exit(s) all correspond to a broker fill.{tail}",
                     src, pending, n_examined=n)


# =============================================================================
# registry + runner
# =============================================================================

# Each entry is (law_callable, one-line statement of the conservation).
LAWS: List[Tuple[Callable[[], LawResult], str]] = [
    (law_nested_probabilities_are_monotone,
     "nested outcomes of one event must be priced monotonically"),
    (law_quoted_prices_have_two_sides,
     "a recorded quote must have had a live bid against it"),
    (law_surfaced_rungs_clear_their_ladder,
     "no candidate the scanner surfaced sits in a ladder the monotone law rejects"),
    (law_runs_match_schedule,
     "pipeline starts observed == pipeline starts scheduled"),
    (law_deployed_set_matches_repo,
     "every tracked file in the repo is the file running on the trading host"),
    (law_shipped_config_matches_repo,
     "every data/ file the firm chose to ship is present and identical there"),
    (law_emitted_verbs_are_handled,
     "every verb a producer emits is a verb the consumer can name"),
    (law_lifecycle_statuses_are_runnable,
     "every status the performance ladder assigns is one an agent survives"),
    (law_positions_conserve,
     "what the broker holds == what the ledger says we opened"),
    (law_broker_host_census,
     "every host trading the account is a host the firm has written down"),
    (law_broker_orders_are_ledgered,
     "every order the broker credits to this host went through the choke point"),
    (law_work_queues_drain,
     "work put into a queue must be taken out of it by something that runs"),
    (law_recorded_exits_are_executed,
     "every exit the firm booked a P&L for is one the broker actually made"),
]


def run_all() -> List[LawResult]:
    results = []
    for fn, _statement in LAWS:
        try:
            results.append(fn())
        except Exception as e:                     # a law must never take the run down
            results.append(LawResult(
                getattr(fn, "__name__", "unknown"), CANNOT_TELL,
                f"law raised {type(e).__name__}: {e}",
                ("n/a", "n/a")))
    return results


CHAIRMAN_QUEUE = DATA_DIR / "chairman_action_required.json"


def escalate_violations(results: List[LawResult]) -> int:
    """Put every VIOLATED law in the Chairman's queue — at most one row each.

    Returns the number of rows written or refreshed.

    ONE ROW PER LAW, UPDATED IN PLACE, keyed `coherence-<law>`. That constraint
    is the whole design. This queue already holds 152 items and 127 of them are
    waiting on a decision; a detector that runs six times a day and appends is
    not an alarm, it is a way of making the queue unreadable, and an unreadable
    queue is how a CRITICAL sits for 50 days. Seven laws can therefore add seven
    rows and never an eighth.

    A law that stops violating has its row CLOSED IN PLACE, not deleted. A stale
    finding that outlives its cause is the same false signal as a missing one,
    pointed the other way — but deleting it is the other failure, and this file
    caused it on 2026-08-19. `deployed_set_matches_repo` violated at 02:55, was
    fixed by a deploy, and the row vanished on the next run. problem_lifecycle_
    sentinel cannot see a reason for a disappearance, only the disappearance, so
    a signature that left every queue while its history said OPEN is by
    definition a SILENT_DROP: it filed one against us, plus a `lifecycle-drop-*`
    row to the Chairman, which chairman_decision_sla then puts on a 20-day clock
    to the board. Thirteen of the eighteen SILENT_DROPs on the VM are already
    `[SLA] Board must decide…` rows, two of them about other lifecycle-drop rows.
    Governance auditing governance auditing governance.

    So the row is marked `status: resolved` with the evidence and left where it
    is. The sentinel then observes a closure instead of inferring a drop, and
    chairman_queue_compactor archives it on a later pass once that observation
    is on record. That order — close in place → let the detector see it →
    compact — is the firm's rule for retiring anything, and a producer withdrawing
    its own row is not exempt from it.

    CANNOT_TELL does not escalate here. It is a finding and it is printed and it
    fails the exit code, but it is the ordinary state of a law whose host cannot
    see the source (positions_conserve off the writer, every run). Escalating it
    would put a permanent row in front of the Chairman that no decision can
    clear — and the queue teaches people to ignore exactly those.
    """
    from datetime import datetime as _dt
    try:
        rows = json.loads(CHAIRMAN_QUEUE.read_text()) if CHAIRMAN_QUEUE.exists() else []
        if not isinstance(rows, list):
            return 0
    except (ValueError, OSError):
        return 0

    violated = {f"coherence-{r.name}": r for r in results if r.verdict == VIOLATED}
    mine = {f"coherence-{fn.__name__.replace('law_', '')}" for fn, _ in LAWS}

    kept, written = [], 0
    for row in rows:
        rid = row.get("id") if isinstance(row, dict) else None
        if rid in violated:
            r = violated.pop(rid)
            row["description"] = f"COHERENCE LAW VIOLATED — {r.name}: {r.detail}"
            row["last_seen"] = _dt.now().isoformat()
            row["n_examined"] = r.n_examined
            if row.pop("resolved_at", None):
                # It closed and violated again. Clearing the closure matters:
                # left behind, `status: resolved` would keep an active violation
                # out of the Chairman's inbox and out of the SLA clock.
                row.pop("resolution", None)
                row["status"] = "awaiting_chairman"
                row["reopened_at"] = _dt.now().isoformat()
            kept.append(row)
            written += 1
        elif rid in mine:
            # The law holds again — close the row, do not delete it. See the
            # docstring: a deletion is what the sentinel has to read as a drop.
            #
            # DO NOT "tidy" the description to say the law now holds. It still
            # reads "COHERENCE LAW VIOLATED — ..." on a resolved row, which
            # looks wrong and is not: problem_lifecycle_sentinel.signature()
            # keys on title-or-description, so rewriting it mints a NEW
            # signature and makes the old one vanish — a silent drop, caused by
            # the very code written to stop causing them. The status, the
            # resolved_at stamp and the resolution line carry the closure; the
            # description stays as the identity of what was wrong.
            if not row.get("resolved_at"):
                row["status"] = "resolved"
                row["resolved_at"] = _dt.now().isoformat()
                row["resolution"] = (
                    f"law {rid[len('coherence-'):]} holds again; the row is kept "
                    f"closed so problem_lifecycle_sentinel records a resolution "
                    f"rather than inferring a silent drop. "
                    f"chairman_queue_compactor archives it once it has.")
                written += 1
            kept.append(row)
        else:
            kept.append(row)

    for rid, r in sorted(violated.items()):
        kept.append({
            "id": rid,
            "severity": "HIGH",
            "description": (f"COHERENCE LAW VIOLATED — {r.name}: {r.detail} "
                            f"(A: {r.sources[0]} | B: {r.sources[1]})"),
            "source": "coherence_laws",
            # THE DENOMINATOR TRAVELS WITH THE FINDING. Severity is deliberately
            # NOT computed from it: every coherence violation stays HIGH. A near-
            # total failure rate usually means the law's own parser broke rather
            # than the world (memory `the-instrument-measured-itself`), so
            # auto-promoting it to CRITICAL would route an engineering bug to the
            # Chairman as an emergency; and auto-demoting a small rate would drop
            # findings below the floor that only CRITICAL clears (memory
            # `severity-below-the-escalation-floor`). The scale is reported so a
            # reader can sort; it does not move the row by itself.
            "n_examined": r.n_examined,
            "ts": _dt.now().isoformat(),
            "last_seen": _dt.now().isoformat(),
        })
        written += 1

    try:
        CHAIRMAN_QUEUE.write_text(json.dumps(kept, indent=1))
    except OSError:
        return 0
    return written


NOTIFY_STATE = DATA_DIR / "coherence_notified.json"


def _rollback_notify_state(prior_text: Optional[str]) -> None:
    """Undo the dedupe write when nothing was actually transmitted."""
    try:
        if prior_text is None:
            NOTIFY_STATE.unlink(missing_ok=True)
        else:
            NOTIFY_STATE.write_text(prior_text)
    except OSError:
        pass


def notify_violations(results: List[LawResult]) -> bool:
    """Buffer ONE digest email when the set of violations changes. Returns sent.

    escalate_violations() writes to this host's chairman queue — and there is no
    state sync between hosts, so a violation the VM finds lands in a queue Jack
    never opens. The two laws that can only run on the VM (positions_conserve,
    and the Kalshi ones, which need the live scan) are exactly the ones whose
    findings would die there. The only medium that crosses hosts is email.

    It goes through email_buffer, which is the mail_guard-sanctioned path: the
    message joins the once-a-day digest instead of opening its own smtplib
    connection. That distinction is not cosmetic — the email flood of 2026-07
    was scripts doing precisely that.

    Deduped on the violation SIGNATURE, not the run. A law that has been broken
    since Tuesday is not news on Wednesday; re-mailing it every run would train
    Jack to filter the sender, which is the failure mode this whole layer exists
    to prevent. A new law breaking, or a known one changing its detail, is news.
    """
    violated = sorted((r.name, r.detail) for r in results if r.verdict == VIOLATED)
    signature = json.dumps(violated)

    try:
        prior_text = NOTIFY_STATE.read_text()
        prior = json.loads(prior_text).get("signature")
    except (OSError, ValueError, AttributeError):
        prior_text, prior = None, None

    if signature == prior:
        return False

    # Record BEFORE sending. If the buffer write throws, the alternative is a
    # retry loop that mails on every run — the failure mode is worse than the
    # miss, and the report on disk still carries the violation either way.
    try:
        NOTIFY_STATE.write_text(json.dumps({
            "signature": signature, "at": datetime.now().isoformat()}))
    except OSError:
        pass

    if not violated:
        return False        # everything cleared: the retired queue row says so

    # The persisted identity, not the current network name — see _host_for_alarm.
    # This is both the human-visible label AND the leading component of the
    # dedup_key below, so a label that moves means identical alarms never collapse.
    host = _host_for_alarm()

    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'><b>{r.name}</b><br>"
        f"<span style='color:#666;font-size:12px'>A: {r.sources[0]} &nbsp;|&nbsp; "
        f"B: {r.sources[1]}</span></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{r.detail}</td></tr>"
        for r in results if r.verdict == VIOLATED)

    body = (
        f"<p>{len(violated)} coherence law(s) violated on <b>{host}</b>. Each law "
        f"compares two independently-sourced measurements of the same quantity; a "
        f"violation means the two disagree, so at least one subsystem is wrong "
        f"about the world.</p>"
        f"<table style='border-collapse:collapse;font-family:system-ui,sans-serif'>"
        f"<tr><th align='left' style='padding:6px 10px'>Law</th>"
        f"<th align='left' style='padding:6px 10px'>Disagreement</th></tr>"
        f"{rows}</table>"
        f"<p style='color:#666;font-size:12px'>Full evidence: "
        f"data/coherence_report.json on {host}.</p>")

    try:
        from email_buffer import buffer_email
        # buffer_email returns False for an URGENT subject, meaning "I did not
        # buffer this, the caller must send it directly" — and this caller has
        # no direct sender, by design. Returning True on that would report a
        # delivery that did not happen, which is the failure this whole module
        # was written to catch. No coherence subject matches the urgent list
        # today; if one ever does, this says so instead of lying.
        buffered = buffer_email(
            subject=f"Coherence: {len(violated)} law(s) violated on {host}",
            html_body=body,
            sender_name="Coherence Layer",
            key_metrics={"violated": len(violated), "host": host,
                         "laws": [n for n, _ in violated]},
            # THE SUBJECT ABOVE IS A COUNT, NOT AN IDENTITY. Three unrelated
            # alarms — a stop-loss blind to 13 broker positions, the VM running
            # code that is not the repo's, and 15 Kalshi ladders quoting
            # numbers no probabilities can produce — all render as
            # "Coherence: 1 law(s) violated on <host>". The digest deduped on
            # that string, so on any day two of them fired, one was deleted and
            # the log called it a duplicate. Four were queued for exactly that
            # on the Mac on 2026-08-27, `nested_probabilities_are_monotone`
            # among them.
            #
            # The laws are the identity. Their DETAIL is not: it carries live
            # counts that move every run, which is why the signature check
            # above buffers a fresh mail each time the same law reports a new
            # number. Keying on names collapses those to the newest — the
            # behaviour the digest wanted — while keeping two different laws
            # two different messages.
            dedup_key="coherence:%s:%s" % (host, ",".join(n for n, _ in violated)),
        )
        if buffered is False:
            # Roll the dedupe record back. Recording-before-sending is right for
            # a THROWN write (retrying that mails on every run), but this branch
            # transmitted nothing at all, so there is no flood to protect
            # against — only an alarm that would otherwise be suppressed
            # forever by a state file claiming it was already delivered.
            _rollback_notify_state(prior_text)
            logging.getLogger("coherence_laws").error(
                "coherence email classified URGENT — email_buffer expects the "
                "caller to transmit it, and this caller cannot. NOT SENT.")
            return False
        return True
    except Exception as e:                       # noqa: BLE001 — never break main
        logging.getLogger("coherence_laws").warning(f"notify failed: {e}")
        return False


def _write_report(results: List[LawResult]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "checked_at": datetime.now().isoformat(),
        "laws": len(results),
        "violated": sum(1 for r in results if r.verdict == VIOLATED),
        "cannot_tell": sum(1 for r in results if r.verdict == CANNOT_TELL),
        "results": [r.to_dict() for r in results],
    }, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="Apex Capital coherence laws")
    ap.add_argument("--report", action="store_true", help="agent convention; same as default")
    ap.add_argument("--quiet", action="store_true", help="only print VIOLATED / CANNOT_TELL")
    ap.add_argument("--no-escalate", action="store_true",
                    help="write the report but do not touch the Chairman's queue")
    # --dry-run exists because a TEST is not a run of the firm.
    #
    # test_pipeline_smoke's exit-code test shells out to this file, and a
    # subprocess cannot be sandboxed the way the in-process tests sandbox
    # themselves — it re-resolves DATA_DIR from its own __file__ and writes the
    # real data/. That refreshed `checked_at` in coherence_report.json, which is
    # exactly the timestamp wiring_audit.check_coherence_layer() reads to decide
    # whether this layer is still firing. So running the test suite proved the
    # coherence layer alive. If the scheduled job had died, the suite would have
    # kept its freshness alarm green — the detector would have been defeated by
    # the act of testing it. Same shape as the scheduler that reported healthy
    # while stopped: a liveness signal must be produced only by the live thing.
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate and print; write no report, escalate nothing")
    # The strict exit code (1 on VIOLATED *or* CANNOT_TELL) is right for CI and
    # the pre-push gate: there, "I could not check" must not read as "fine".
    # It is wrong for a scheduler. CANNOT_TELL is the ordinary resting state of
    # several laws — positions_conserve defers off the writer on every single
    # run, by design — so a scheduled job on the strict code would log a failure
    # three times a day forever, and a job that always fails is a job whose
    # output nobody reads. Same trap as a queue with 152 rows.
    ap.add_argument("--exit-on-violation-only", action="store_true",
                    help="exit 1 only on VIOLATED; for schedulers, not for CI")
    args = ap.parse_args()

    results = run_all()
    if not args.dry_run:
        _write_report(results)
    if not (args.no_escalate or args.dry_run):
        escalate_violations(results)
        notify_violations(results)

    bad = [r for r in results if r.verdict != HOLDS]
    if not args.quiet:
        print("=" * 72)
        print("COHERENCE LAWS — two independent sources per law, disagreement is the alarm")
        print("=" * 72)
    for r in results:
        if args.quiet and r.verdict == HOLDS:
            continue
        mark = {HOLDS: "✓", VIOLATED: "✗", CANNOT_TELL: "?"}[r.verdict]
        print(f"  {mark} {r.verdict:<12} {r.name}")
        print(f"      {r.detail}")
        print(f"      A: {r.sources[0]}   B: {r.sources[1]}")
        for e in r.evidence[:5]:
            print(f"        · {json.dumps(e)[:150]}")
    if not args.quiet:
        print("-" * 72)
        print(f"  {len(results)} law(s): "
              f"{sum(1 for r in results if r.verdict == HOLDS)} hold, "
              f"{sum(1 for r in results if r.verdict == VIOLATED)} violated, "
              f"{sum(1 for r in results if r.verdict == CANNOT_TELL)} cannot tell")
        print(f"  report → {REPORT}")
    if args.exit_on_violation_only:
        return 1 if any(r.verdict == VIOLATED for r in results) else 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

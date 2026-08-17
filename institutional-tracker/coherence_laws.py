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
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
                 sources: Tuple[str, str], evidence: Optional[list] = None):
        self.name = name
        self.verdict = verdict
        self.detail = detail
        self.sources = sources
        self.evidence = evidence or []

    def to_dict(self) -> dict:
        return {
            "law": self.name,
            "verdict": self.verdict,
            "detail": self.detail,
            "source_a": self.sources[0],
            "source_b": self.sources[1],
            "evidence": self.evidence[:20],
        }


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
        parsed = parse_threshold(str(r.get("title") or ""))
        if parsed is None:
            continue
        threshold, direction = parsed
        # Keep one row per rung; duplicate scans of the same rung are not
        # independent observations and must not be compared against each other.
        grouped[(ev, direction)][threshold] = r
    return {k: v for k, v in grouped.items() if len(v) >= 2}


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
    src = ("kalshi quote, low rung", "kalshi quote, high rung (same event)")
    if rows is None:
        path = DATA_DIR / "kalshi_predictions.json"
        if not path.exists():
            return LawResult(name, CANNOT_TELL,
                             f"no prediction ledger at {path} — the Kalshi scanner "
                             f"has not recorded a scan on this host. Owner: Kalshi sector.",
                             src)
        try:
            rows = json.loads(path.read_text())
        except (ValueError, OSError) as e:
            return LawResult(name, CANNOT_TELL,
                             f"prediction ledger unreadable: {e}. Owner: Kalshi sector.", src)
    if not isinstance(rows, list):
        return LawResult(name, CANNOT_TELL, "prediction ledger is not a list", src)

    ladders = _ladders(rows)
    if not ladders:
        return LawResult(name, CANNOT_TELL,
                         "no event carried 2+ parseable rungs — nothing to compare. "
                         "Owner: Kalshi sector.", src)

    bad = []
    for (ev, direction), rungs in sorted(ladders.items()):
        ordered = sorted(rungs.items())          # ascending threshold
        prices = [row["market_price"] for _, row in ordered]
        if direction == "below":
            prices = prices[::-1]                # BELOW ladders must RISE
        inverted = any(prices[i] < prices[i + 1] - 1e-9 for i in range(len(prices) - 1))
        flat = len(set(prices)) == 1
        if inverted or flat:
            bad.append({
                "event_ticker": ev,
                "direction": direction,
                "fault": "inverted" if inverted else "all rungs identically priced",
                "rungs": [[t, row["market_price"]] for t, row in ordered],
            })

    if not bad:
        return LawResult(name, HOLDS,
                         f"{len(ladders)} ladder(s) checked, all monotone", src)
    return LawResult(
        name, VIOLATED,
        f"{len(bad)} of {len(ladders)} ladder(s) price nested outcomes "
        f"incoherently — these quotes are not probabilities and must not be "
        f"treated as candidates",
        src, bad,
    )


# =============================================================================
# LAW — quoted prices have two sides
# =============================================================================

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
    src = ("recorded ask (market_price)", "bid on the same contract")
    if rows is None:
        path = DATA_DIR / "kalshi_predictions.json"
        if not path.exists():
            return LawResult(name, CANNOT_TELL,
                             f"no prediction ledger at {path}. Owner: Kalshi sector.", src)
        try:
            rows = json.loads(path.read_text())
        except (ValueError, OSError) as e:
            return LawResult(name, CANNOT_TELL, f"ledger unreadable: {e}", src)
    if not rows:
        return LawResult(name, CANNOT_TELL, "ledger is empty", src)

    with_bid = [r for r in rows if r.get("yes_bid") is not None]
    if not with_bid:
        return LawResult(
            name, CANNOT_TELL,
            f"none of {len(rows)} ledger rows carry a `yes_bid` — every one "
            f"predates the field. Their quotes cannot be shown to have had a "
            f"second side, and the settled ones did not behave as if they did "
            f"(0.97 quoted, 0.27 realised, n=86). Owner: Kalshi sector; "
            f"resolves itself as post-fix scans accumulate.", src)

    one_sided = [r for r in with_bid
                 if r["yes_bid"] <= 0 or r["yes_bid"] >= r.get("market_price", 1.0)]
    if one_sided:
        return LawResult(name, VIOLATED,
                         f"{len(one_sided)} of {len(with_bid)} rows carry a bid that "
                         f"is absent or crossed — the gate let through a quote with "
                         f"no live counterparty", src,
                         [{"ticker": r.get("ticker"), "bid": r.get("yes_bid"),
                           "ask": r.get("market_price")} for r in one_sided])
    return LawResult(name, HOLDS,
                     f"{len(with_bid)} of {len(rows)} row(s) carry a live two-sided "
                     f"book; {len(rows) - len(with_bid)} predate the field", src)


# =============================================================================
# LAW — runs match schedule
# =============================================================================

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
    src = ("pipeline_heartbeat.json starts (observed)", "host schedule (intended)")
    now = now or datetime.now()
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

    intended = _scheduled_pipeline_runs_today(now)
    if intended is None:
        return LawResult(name, CANNOT_TELL,
                         "could not read this host's schedule — no crontab entry for "
                         "main.py and no launchd plist. Owner: whoever owns this host's "
                         "scheduler.", src)

    if observed > intended:
        return LawResult(name, VIOLATED,
                         f"main.py started {observed} time(s) today; the schedule asks "
                         f"for {intended}. An overrun is never legitimate — something is "
                         f"launching the pipeline outside the schedule.", src,
                         [{"observed": observed, "intended": intended}])
    if observed < intended and now.hour >= 23:
        return LawResult(name, VIOLATED,
                         f"main.py started {observed} time(s) today against {intended} "
                         f"scheduled, and the day is over.", src,
                         [{"observed": observed, "intended": intended}])
    return LawResult(name, HOLDS,
                     f"{observed} start(s) today, {intended} scheduled", src)


def _scheduled_pipeline_runs_today(now: datetime) -> Optional[int]:
    """How many main.py runs this host's own scheduler asks for today.

    Reads the LIVE scheduler, not a doc. A doc is a third party's claim about
    the schedule; `crontab -l` is the schedule (memory `auditor_ground_truth`).
    Returns None when it cannot be read — never 0, which would read as "nothing
    is scheduled, so any run is an overrun" and turn a degraded read into a
    false alarm (memory `fallback-that-means-act`, inverted).
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
    if not saw_any:
        return None
    return count


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
# registry + runner
# =============================================================================

# Each entry is (law_callable, one-line statement of the conservation).
LAWS: List[Tuple[Callable[[], LawResult], str]] = [
    (law_nested_probabilities_are_monotone,
     "nested outcomes of one event must be priced monotonically"),
    (law_quoted_prices_have_two_sides,
     "a recorded quote must have had a live bid against it"),
    (law_runs_match_schedule,
     "pipeline starts observed == pipeline starts scheduled"),
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
    args = ap.parse_args()

    results = run_all()
    _write_report(results)

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
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

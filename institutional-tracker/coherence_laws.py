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
import logging
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
def law_deployed_set_matches_repo(runner: Optional[Callable] = None) -> LawResult:
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
    """
    name = "deployed_set_matches_repo"
    src = law_deployed_set_matches_repo.sources
    import subprocess
    runner = runner or (lambda cmd, timeout: subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout))

    if not (BASE_DIR / ".git").exists() and not (BASE_DIR.parent / ".git").exists():
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
                         f"are in no commit. The host is not running the repo.", src, ev)
    return LawResult(name, HOLDS,
                     f"{len(local)} tracked .py identical on {REMOTE_ALIAS}, "
                     f"no uncommitted files there", src)


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
                         [{"verb": v, "emitted_by": f} for v, f in sorted(unhandled.items())])
    return LawResult(name, HOLDS,
                     f"{len(emitted)} verb(s) emitted ({', '.join(sorted(emitted))}), "
                     f"all handled", src)


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
                         + [{"ledgered_not_held": s} for s in phantom[:10]])
    return LawResult(name, HOLDS,
                     f"{len(held)} broker position(s), same {len(ledger)} in the ledger",
                     src)


# =============================================================================
# LAW — broker host census
# =============================================================================

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
                         [{"host": h, "last_order": seen[h].isoformat()} for h in strangers])
    return LawResult(name, HOLDS,
                     "; ".join(f"{h} last traded {seen[h].date()}" for h in sorted(seen)),
                     src)


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
    (law_deployed_set_matches_repo,
     "every tracked file in the repo is the file running on the trading host"),
    (law_emitted_verbs_are_handled,
     "every verb a producer emits is a verb the consumer can name"),
    (law_positions_conserve,
     "what the broker holds == what the ledger says we opened"),
    (law_broker_host_census,
     "every host trading the account is a host the firm has written down"),
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

    A law that stops violating has its row REMOVED. A stale finding that outlives
    its cause is the same false signal as a missing one, pointed the other way.

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
            kept.append(row)
            written += 1
        elif rid in mine:
            continue                     # law now holds: the row is retired
        else:
            kept.append(row)

    for rid, r in sorted(violated.items()):
        kept.append({
            "id": rid,
            "severity": "HIGH",
            "description": (f"COHERENCE LAW VIOLATED — {r.name}: {r.detail} "
                            f"(A: {r.sources[0]} | B: {r.sources[1]})"),
            "source": "coherence_laws",
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
        prior = json.loads(NOTIFY_STATE.read_text()).get("signature")
    except (OSError, ValueError, AttributeError):
        prior = None

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

    try:
        import socket
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "unknown-host"

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
        buffer_email(
            subject=f"Coherence: {len(violated)} law(s) violated on {host}",
            html_body=body,
            sender_name="Coherence Layer",
            key_metrics={"violated": len(violated), "host": host,
                         "laws": [n for n, _ in violated]},
        )
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
    _write_report(results)
    if not args.no_escalate:
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

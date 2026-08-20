# =============================================================================
# APEX CAPITAL — R&D POD · PRICE THEORY AGENT
# =============================================================================
# The R&D pod's economist. It does not pick stocks and it does not find bugs.
# It reads the firm's own artifacts and asks the small set of questions a price
# theorist asks about any decision, then comments where the arithmetic of the
# firm disagrees with the reasoning of the firm.
#
# WHY THIS AGENT EXISTS
#
# On 2026-08-19 the firm's two entry limits — a 20% cash floor and a
# 20-position cap — were both fully utilised, so every new long was blocked,
# including the SH hedge. Both numbers were round, both were inherited as
# defaults, and neither had ever been derived. The quantity they stand in for,
# measured net beta, was 0.64: well inside anything the firm would call
# aggressive. The constraint was binding, the constraint was expensive, and
# nobody had ever asked what it cost. That is not a coding error — no test
# could have failed — and it is not a risk error either, because each limit was
# enforced exactly as written. It is an economics error, and the firm had no
# agent whose job was to notice one.
#
# THE DISCIPLINE (this is the whole design)
#
# An agent that generates economic commentary is the purest possible instance
# of the firm's worst failure mode: fluent, confident, unchecked output that
# nobody can falsify (memory `plausible-is-not-validated`). So this agent does
# not generate prose. Each lens is a deterministic check over real artifacts,
# and it may return exactly one of three things:
#
#   COMMENT      — it fires, and it carries the NUMBERS and the SOURCE FILES
#                  that made it fire. A comment with no numbers is a rejected
#                  comment; `_emit` refuses to build one.
#   SILENT       — it looked, and there was nothing to say. Carries
#                  `n_examined`, because a pass over zero items is not a pass
#                  (same rule the coherence laws are held to).
#   CANNOT_TELL  — the input it needs is missing or unreadable. This is a
#                  verdict, not silence: an artifact that never got written and
#                  an artifact that says "fine" must never look the same.
#
# A run in which EVERY lens returns CANNOT_TELL is itself a finding — the agent
# is blind, and a blind commentator that emits nothing looks exactly like a
# healthy firm. That case escalates to the Chairman rather than passing.
#
# WIRING
#
#   reads   data/risk_limit_derivation.json   (derive_risk_limits.py)
#           data/risk_gate_log.json           (risk_gate.py)
#           data/rd_pod_approved.json         (rd_pod_board.py)
#           data/cit_implemented_proposals.json
#           the live broker snapshot, via risk_gate.snapshot()
#           its own prior comments, for de-duplication
#   writes  data/rd_price_theory_comments.json     (the standing commentary)
#           data/rd_pod_pending.json               (routable comments, as
#                                                   proposals the R&D pod board
#                                                   actually votes on)
#           data/chairman_action_required.json     (only when blind)
#
# Routing to `rd_pod_pending.json` is deliberate: `rd` is one of the two pods
# with a real consumer (`rd_pod_board.py`). Routing to `fi`/`fe`/`etf` would
# have produced comments nobody reads, which is the bug class this repo keeps
# finding (see CLAUDE.md, "producer → queue → consumer contracts").
#
# It changes NOTHING. It has no write path to `config.py` and none to the
# broker; it comments, and the pod board decides. Pinned by an AST test.
#
# Usage:
#   python3 rd_price_theory_agent.py --report     # the scheduled entrypoint
#   python3 rd_price_theory_agent.py --no-route   # look, print, route nothing
#   python3 rd_price_theory_agent.py --json
#
# Guarded by test_pipeline_smoke.py::TestThePriceTheoryAgentCommentsWithNumbers
# =============================================================================

import argparse
import hashlib
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rd_price_theory")

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

COMMENTS = DATA / "rd_price_theory_comments.json"
POD_QUEUE = DATA / "rd_pod_pending.json"
CHAIRMAN = DATA / "chairman_action_required.json"
DERIVATION = DATA / "risk_limit_derivation.json"
GATE_LOG = DATA / "risk_gate_log.json"
POD_APPROVED = DATA / "rd_pod_approved.json"
IMPLEMENTED = DATA / "cit_implemented_proposals.json"

AGENT = "RDPriceTheoryAgent"

# ── named assumptions ────────────────────────────────────────────────────────
# Every one of these is a judgement, so it is written down where it can be
# argued with rather than buried in a comparison. The firm's recurring pattern
# is a constant nobody can defend; an agent about that pattern must not create
# more of them silently.

DERIVATION_STALE_DAYS = 45      # a derivation older than this no longer
                                # describes the book it was computed from
NEAR_BINDING = 0.95             # utilisation at which a limit is "about to
                                # bind" — the cost starts before the block does
MARGIN_UNDERSTATES_RATIO = 5.0  # cumulative:marginal ratio above which quoting
                                # the marginal number misleads the decision
GATE_WINDOW_DAYS = 30           # trailing window for counting blocks
MIN_BLOCKS_TO_COMMENT = 3       # one block is an incident, three is a price
APPROVED_STALE_DAYS = 30        # approved-and-untouched beyond this is a
                                # stated preference with nothing behind it
MIN_APPROVED_UNIMPLEMENTED = 5

COMMENT = "COMMENT"
SILENT = "SILENT"
CANNOT_TELL = "CANNOT_TELL"


# ── plumbing ─────────────────────────────────────────────────────────────────

def _load(path: Path, default=None):
    """Read JSON, or `default`. Never raises.

    Returns `default` for both "absent" and "unreadable" — the callers here all
    treat those the same way, and every one of them turns it into CANNOT_TELL
    rather than into a silent pass.
    """
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception:
        return default


def _age_days(ts) -> Optional[float]:
    """Age of an ISO timestamp in days, or None if it cannot be parsed.

    None, not a large number: an unparseable timestamp that returned 999 would
    read as "very stale" and trigger exactly the alarm a missing field should
    not (memory `fallback-that-means-act`).
    """
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400.0
    except Exception:
        return None


def _signature(lens: str, *parts) -> str:
    """Stable id for a comment, so the same observation on the same facts is
    one comment seen many times rather than a new comment every run.

    Deliberately excludes the measured values: the point of `times_seen` is to
    show that a condition has PERSISTED, and a signature that moved whenever
    net beta moved by a basis point would reset that count daily and make a
    standing problem look new and small forever.
    """
    raw = "|".join([lens] + [str(p) for p in parts])
    return f"{lens}-{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


class Result:
    """One lens's verdict. Status is COMMENT, SILENT, or CANNOT_TELL."""

    def __init__(self, lens: str, status: str, comment: Optional[dict] = None,
                 n_examined: int = 0, reason: str = ""):
        self.lens = lens
        self.status = status
        self.comment = comment
        self.n_examined = n_examined
        self.reason = reason

    def as_dict(self) -> dict:
        return {"lens": self.lens, "status": self.status,
                "n_examined": self.n_examined, "reason": self.reason,
                "comment": self.comment}


def _emit(lens: str, question: str, headline: str, numbers: dict,
          sources: List[str], severity: str, proposal: str,
          signature: str) -> Result:
    """Build a COMMENT, refusing any that carries no numbers or no source.

    This is the guardrail on the agent's own failure mode. A price-theory
    observation with nothing measured behind it is an opinion, and the firm has
    no shortage of those; it has a shortage of numbers attached to them.
    """
    clean = {k: v for k, v in numbers.items() if v is not None}
    if not clean:
        return Result(lens, SILENT, reason="refused: a comment with no numbers")
    if not sources:
        return Result(lens, SILENT, reason="refused: a comment with no source")
    return Result(lens, COMMENT, {
        "lens": lens, "signature": signature, "question": question,
        "headline": headline, "numbers": clean, "sources": sorted(set(sources)),
        "severity": severity, "proposal": proposal,
    })


# ── lens 1: a binding constraint with no price ───────────────────────────────

def lens_binding_constraint_without_a_price() -> Result:
    """Every binding constraint charges the firm something. What?

    A limit that never binds is free and nobody needs to defend it. The moment
    it binds it starts turning away business, and at that point "we have always
    used 20%" stops being an answer. This fires when a limit is at or near its
    bound and no derivation for it exists, or the one on file is stale.
    """
    lens = "a_binding_constraint_with_no_price"
    q = "what does this constraint cost, now that it is actually binding?"
    try:
        import risk_gate
        snap = risk_gate.snapshot()
        lim = risk_gate.limits()
    except Exception as e:
        return Result(lens, CANNOT_TELL, reason=f"cannot read the live limits: {e}")
    if not snap:
        return Result(lens, CANNOT_TELL,
                      reason="the broker snapshot could not be read, so no "
                             "limit's utilisation is knowable")

    pv = float(snap.get("portfolio_value") or 0.0)
    cash = float(snap.get("cash") or 0.0)
    n_pos = len(snap.get("positions") or {})
    if pv <= 0:
        return Result(lens, CANNOT_TELL, reason="portfolio value is not usable")

    cash_pct = cash / pv
    floor = float(lim["cash_floor"])
    cap = int(lim["max_positions"])
    # Utilisation of a floor is inverted: the constraint bites as cash FALLS.
    cash_util = 1.0 if floor <= 0 else min(2.0, (floor - cash_pct) / floor + 1.0)
    slot_util = n_pos / cap if cap else 0.0

    binding = []
    if cash_pct <= floor:
        binding.append("cash floor")
    elif cash_util >= NEAR_BINDING:
        binding.append("cash floor (near)")
    if slot_util >= 1.0:
        binding.append("position cap")
    elif slot_util >= NEAR_BINDING:
        binding.append("position cap (near)")

    if not binding:
        return Result(lens, SILENT, n_examined=2,
                      reason=f"neither limit is binding (cash {cash_pct:.1%} vs "
                             f"floor {floor:.0%}, {n_pos}/{cap} slots)")

    der = _load(DERIVATION)
    age = _age_days((der or {}).get("as_of")) if isinstance(der, dict) else None
    if isinstance(der, dict) and der.get("available") and age is not None \
            and age <= DERIVATION_STALE_DAYS:
        return Result(lens, SILENT, n_examined=2,
                      reason=f"{' and '.join(binding)} binding, but a "
                             f"derivation {age:.0f}d old is on file")

    if der is None:
        why = "no derivation has ever been written"
    elif age is None:
        why = "the derivation on file carries no readable date"
    elif not der.get("available"):
        why = "the derivation on file could not be computed"
    else:
        why = f"the derivation on file is {age:.0f} days old"

    return _emit(
        lens, q,
        f"{' and '.join(binding)} binding at {cash_pct:.1%} cash and "
        f"{n_pos}/{cap} positions — and {why}",
        {"cash_pct": round(cash_pct, 4), "cash_floor_pct": floor,
         "positions": n_pos, "max_positions": cap,
         "derivation_age_days": None if age is None else round(age, 1)},
        ["risk_gate.snapshot", "config.py", DERIVATION.name],
        "HIGH" if not binding[0].endswith("(near)") else "MEDIUM",
        "Run derive_risk_limits.py and put the shadow price of this limit in "
        "front of the Chairman before the next entry is refused. A limit that "
        "turns away trades at a cost nobody has computed is not a risk "
        "decision, it is an unpriced tax.",
        _signature(lens, sorted(binding)))


# ── lens 2: a proxy that disagrees with the quantity it stands for ───────────

def lens_proxy_disagrees_with_its_quantity() -> Result:
    """The rule is written on a proxy. Does the proxy still track the thing?

    Cash percentage and slot count are proxies for risk capacity. A proxy fails
    exactly where it binds, because that is the only place anyone consults it.
    This fires when both proxies read "at capacity" while the quantity they
    stand in for — beta-weighted net exposure — is measurable and modest, and
    the firm has no stated limit on that quantity to compare it against.
    """
    lens = "a_proxy_that_disagrees_with_its_quantity"
    q = "the rule is written on a proxy — does the proxy still track the thing?"
    try:
        import risk_gate
        import portfolio_beta
        snap = risk_gate.snapshot()
        lim = risk_gate.limits()
    except Exception as e:
        return Result(lens, CANNOT_TELL, reason=f"cannot read the live book: {e}")
    if not snap:
        return Result(lens, CANNOT_TELL,
                      reason="the broker snapshot could not be read")

    pv = float(snap.get("portfolio_value") or 0.0)
    cash = float(snap.get("cash") or 0.0)
    positions = snap.get("positions") or {}
    n_pos = len(positions)
    if pv <= 0:
        return Result(lens, CANNOT_TELL, reason="portfolio value is not usable")

    cash_pct = cash / pv
    floor = float(lim["cash_floor"])
    cap = int(lim["max_positions"])
    at_capacity = [n for n, c in (("cash floor", cash_pct <= floor),
                                  ("position cap", n_pos >= cap)) if c]
    if not at_capacity:
        return Result(lens, SILENT, n_examined=2,
                      reason="no proxy is at capacity, so none is being "
                             "consulted at the point where it fails")

    nb = portfolio_beta.net_beta(positions, pv)
    if nb.get("beta") is None:
        return Result(lens, CANNOT_TELL,
                      reason=f"net beta is not measurable: {nb.get('reason')}")

    beta = float(nb["beta"])
    return _emit(
        lens, q,
        f"{' and '.join(at_capacity)} read fully utilised, but the quantity "
        f"they proxy for is a net beta of {beta:.2f} at "
        f"{nb.get('coverage', 0):.0%} coverage — and the firm has no stated "
        f"beta limit for that number to be measured against",
        {"net_beta": round(beta, 4), "coverage": round(nb.get("coverage", 0), 4),
         "cash_pct": round(cash_pct, 4), "cash_floor_pct": floor,
         "positions": n_pos, "max_positions": cap,
         "long_beta_dollars": round(nb.get("long_beta_dollars", 0.0), 2),
         "short_beta_dollars": round(nb.get("short_beta_dollars", 0.0), 2)},
        ["risk_gate.snapshot", "portfolio_beta.net_beta", "config.py"],
        "HIGH",
        "State a beta appetite the firm can be held to, and let the proxies "
        "bind only where they agree with it. Until that number exists, every "
        "block issued at this boundary is a decision about risk taken by a "
        "measure of something else.",
        _signature(lens, sorted(at_capacity)))


# ── lens 3: an average standing in for a margin ──────────────────────────────

def lens_average_standing_in_for_a_margin() -> Result:
    """Decisions are made at the margin. Is the firm quoting one?

    "91% of available diversification is already captured at 20 names" is true
    and is the wrong number, because nobody is choosing between 20 names and
    one. The choice is 20 against a different cap, and that is a cumulative
    quantity. This fires when the two differ enough to change the answer.
    """
    lens = "an_average_standing_in_for_a_margin"
    q = "the decision is made at the margin — is that the number being quoted?"
    der = _load(DERIVATION)
    if not isinstance(der, dict) or not der.get("available"):
        return Result(lens, CANNOT_TELL,
                      reason="no usable derivation on file to read a curve from")
    mp = der.get("marginal_position") or {}
    marginal = mp.get("marginal_bp_at_cap")
    cum40 = mp.get("cumulative_bp_to_40")
    achieved = mp.get("diversification_achieved_pct")
    if marginal is None or cum40 is None:
        return Result(lens, CANNOT_TELL,
                      reason="the derivation carries no marginal/cumulative pair")
    if marginal <= 0:
        return Result(lens, SILENT, n_examined=1,
                      reason="the marginal position buys nothing measurable")

    ratio = cum40 / marginal
    if ratio < MARGIN_UNDERSTATES_RATIO:
        return Result(lens, SILENT, n_examined=1,
                      reason=f"marginal {marginal:.1f}bp and cumulative "
                             f"{cum40:.1f}bp are within {ratio:.1f}x — quoting "
                             f"either leads to the same decision")

    return _emit(
        lens, q,
        f"the marginal name buys {marginal:.1f} bp and that is the number in "
        f"the report, but moving the cap to 40 buys {cum40:.1f} bp — "
        f"{ratio:.0f}x more, and the cap is the thing actually being decided"
        + (f" ({achieved:.0f}% 'already captured' is an average, and averages "
           f"do not decide caps)" if achieved is not None else ""),
        {"marginal_bp_at_cap": marginal, "cumulative_bp_to_40": cum40,
         "ratio": round(ratio, 2), "diversification_achieved_pct": achieved},
        [DERIVATION.name],
        "MEDIUM",
        "Put the cumulative figure next to the marginal one wherever the cap "
        "is argued. The marginal number defends the cap; the cumulative number "
        "is what the Chairman is choosing between. Both are true and only one "
        "is responsive.",
        _signature(lens, "cap_curve"))


# ── lens 4: a block is a price nobody counted ────────────────────────────────

def lens_a_block_is_a_price_nobody_counted() -> Result:
    """A refused trade is not free. It has a price: the foregone alternative.

    The gate log records every block. Nothing reads it on a schedule, which
    means the firm can turn away thirty orders a month and never see a number
    for what that cost. This does not claim the blocks were wrong — it claims
    they are unpriced, which is a different and checkable statement.
    """
    lens = "a_block_is_a_price_nobody_counted"
    q = "the block has a price — the best foregone alternative. What is it?"
    rows = _load(GATE_LOG)
    if rows is None:
        return Result(lens, CANNOT_TELL,
                      reason=f"{GATE_LOG.name} does not exist on this host — "
                             f"the gate has recorded no decision here, which is "
                             f"not the same as having blocked nothing")
    if not isinstance(rows, list):
        return Result(lens, CANNOT_TELL, reason="the gate log is not a list")

    cutoff = datetime.now(timezone.utc) - timedelta(days=GATE_WINDOW_DAYS)
    recent, undated = [], 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        age = _age_days(r.get("ts"))
        if age is None:
            undated += 1
            continue
        if age <= GATE_WINDOW_DAYS:
            recent.append(r)
    blocks = [r for r in recent if not r.get("allowed")]
    if len(blocks) < MIN_BLOCKS_TO_COMMENT:
        return Result(lens, SILENT, n_examined=len(recent),
                      reason=f"{len(blocks)} block(s) in {GATE_WINDOW_DAYS}d, "
                             f"below the {MIN_BLOCKS_TO_COMMENT} it takes to "
                             f"call something a standing price")

    by_verdict: Dict[str, int] = {}
    for b in blocks:
        by_verdict[str(b.get("verdict"))] = by_verdict.get(str(b.get("verdict")), 0) + 1
    symbols = sorted({str(b.get("symbol")) for b in blocks if b.get("symbol")})
    top = max(by_verdict.items(), key=lambda kv: kv[1])

    return _emit(
        lens, q,
        f"{len(blocks)} order(s) refused in {GATE_WINDOW_DAYS} days across "
        f"{len(symbols)} symbol(s), {top[1]} of them by {top[0]} — the firm has "
        f"no figure for what any of them would have earned",
        {"blocks": len(blocks), "window_days": GATE_WINDOW_DAYS,
         "distinct_symbols": len(symbols), "by_verdict": by_verdict,
         "undated_rows": undated or None},
        [GATE_LOG.name],
        "MEDIUM",
        "Record the intended notional and the subsequent forward return of "
        "each refused order. Six weeks of that turns 'the gate is prudent' "
        "into a number, and a limit with a measured cost can be argued about "
        "instead of merely obeyed.",
        _signature(lens, sorted(by_verdict)))


# ── lens 5: a stated priority with nothing revealed behind it ────────────────

def lens_stated_priority_without_revealed_priority() -> Result:
    """What the firm says it values, against where the firm spends.

    An approval is cheap. Implementation is the price, and revealed preference
    is the only preference that moves a portfolio. A long queue of approved-
    and-untouched proposals is not a backlog problem, it is evidence that the
    approvals were never worth their cost — or that the cost was never budgeted.
    """
    lens = "a_stated_priority_with_nothing_revealed_behind_it"
    q = "the firm approved this. Where did the resources actually go?"
    approved = _load(POD_APPROVED)
    done = _load(IMPLEMENTED)
    if not isinstance(approved, list):
        return Result(lens, CANNOT_TELL,
                      reason=f"{POD_APPROVED.name} is missing or not a list")
    if done is None:
        return Result(lens, CANNOT_TELL,
                      reason=f"{IMPLEMENTED.name} is missing — with no record "
                             f"of what was built, 'approved and untouched' "
                             f"cannot be distinguished from 'approved and done'")

    done_ids = {str(d.get("id")) for d in done if isinstance(d, dict)}
    done_titles = {str(d.get("title", "")).strip().lower()
                   for d in done if isinstance(d, dict)}

    stale = []
    for p in approved:
        if not isinstance(p, dict):
            continue
        if str(p.get("id")) in done_ids:
            continue
        if str(p.get("title", "")).strip().lower() in done_titles:
            continue
        age = _age_days(p.get("voted_at") or p.get("routed_at"))
        if age is not None and age >= APPROVED_STALE_DAYS:
            stale.append((age, p))

    if len(stale) < MIN_APPROVED_UNIMPLEMENTED:
        return Result(lens, SILENT, n_examined=len(approved),
                      reason=f"{len(stale)} approved proposal(s) older than "
                             f"{APPROVED_STALE_DAYS}d remain unimplemented, "
                             f"below the {MIN_APPROVED_UNIMPLEMENTED} it takes "
                             f"to call it a pattern")

    stale.sort(key=lambda t: -t[0])
    oldest_age, oldest = stale[0]
    return _emit(
        lens, q,
        f"{len(stale)} of {len(approved)} approved proposals have gone "
        f"unimplemented for over {APPROVED_STALE_DAYS} days (oldest "
        f"{oldest_age:.0f}d: \"{str(oldest.get('title'))[:70]}\"), against "
        f"{len(done_ids)} implemented in total — the firm's stated priorities "
        f"and its revealed ones are not the same list",
        {"approved_total": len(approved), "stale_unimplemented": len(stale),
         "implemented_total": len(done_ids),
         "oldest_age_days": round(oldest_age, 1)},
        [POD_APPROVED.name, IMPLEMENTED.name],
        "MEDIUM",
        "Either budget implementation capacity against the approval rate, or "
        "stop approving at a rate the firm cannot fund. An approval that is "
        "never resourced costs the same as a rejection and teaches the board "
        "that its votes are free.",
        _signature(lens, "approved_unimplemented"))


LENSES: List[Callable[[], Result]] = [
    lens_binding_constraint_without_a_price,
    lens_proxy_disagrees_with_its_quantity,
    lens_average_standing_in_for_a_margin,
    lens_a_block_is_a_price_nobody_counted,
    lens_stated_priority_without_revealed_priority,
]


# ── merge, route, escalate ───────────────────────────────────────────────────

def _merge(new: List[dict], prior: List[dict]) -> List[dict]:
    """Carry `first_seen` and `times_seen` forward by signature.

    A lens that fires every day on an unchanged fact must not read as a fresh
    finding every day; `times_seen` is what distinguishes a standing condition
    from a new one, and a resolved comment is dropped rather than left to
    linger as a fact about a book that no longer exists.
    """
    by_sig = {c.get("signature"): c for c in prior if isinstance(c, dict)}
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for c in new:
        old = by_sig.get(c["signature"])
        c["last_seen"] = now
        if old:
            c["first_seen"] = old.get("first_seen", now)
            c["times_seen"] = int(old.get("times_seen") or 1) + 1
            c["routed"] = bool(old.get("routed"))
        else:
            c["first_seen"] = now
            c["times_seen"] = 1
            c["routed"] = False
        out.append(c)
    return out


def _route(comments: List[dict]) -> int:
    """Put un-routed comments on the R&D pod board's queue. Never raises.

    Only once per signature: the board votes on a proposal, and re-queuing the
    same observation every run would make its own volume look like urgency.
    """
    fresh = [c for c in comments if not c.get("routed")]
    if not fresh:
        return 0
    try:
        pending = _load(POD_QUEUE, [])
        if not isinstance(pending, list):
            pending = []
        have = {str(p.get("id")) for p in pending if isinstance(p, dict)}
        added = 0
        for c in fresh:
            pid = f"PT-{c['signature']}"
            if pid in have:
                c["routed"] = True
                continue
            pending.append({
                "id": pid,
                "agent": AGENT,
                "category": "PROCESS_IMPROVEMENT",
                "severity": c["severity"],
                "confidence": 85,
                "title": f"[price theory] {c['headline'][:110]}",
                "description": (f"{c['question']}\n\n{c['headline']}\n\n"
                                f"Measured: {json.dumps(c['numbers'])}\n"
                                f"Sources: {', '.join(c['sources'])}"),
                "proposed_fix": c["proposal"],
                "evidence": f"lens {c['lens']}; sources {', '.join(c['sources'])}",
                "auto_implementable": False,
                "status": "pending",
                "routed_at": datetime.now(timezone.utc).isoformat(),
            })
            c["routed"] = True
            added += 1
        DATA.mkdir(exist_ok=True)
        POD_QUEUE.write_text(json.dumps(pending, indent=2))
        return added
    except Exception as e:
        logger.warning(f"  [price theory] could not route to the pod board: {e}")
        return 0


def _escalate_blindness(results: List[Result]) -> bool:
    """One Chairman row per day when EVERY lens came back CANNOT_TELL.

    A commentator with no inputs emits nothing, and nothing is what a healthy
    firm also emits. Without this the agent's own failure is indistinguishable
    from its success — the exact shape of every control failure this repo has
    had to find the hard way.
    """
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rid = f"price-theory-blind-{day}"
        rows = _load(CHAIRMAN, [])
        if not isinstance(rows, list):
            return False
        for r in rows:
            if isinstance(r, dict) and r.get("id") == rid:
                return False
        rows.append({
            "id": rid, "severity": "MEDIUM",
            "description": ("The R&D price-theory agent is BLIND: every one of "
                            f"{len(results)} lenses returned CANNOT_TELL, so "
                            "its silence this run says nothing about the firm. "
                            + " ".join(f"[{r.lens}] {r.reason}" for r in results)),
            "source": AGENT, "ts": datetime.now(timezone.utc).isoformat(),
            "status": "awaiting_chairman", "times_seen": 1,
        })
        DATA.mkdir(exist_ok=True)
        CHAIRMAN.write_text(json.dumps(rows, indent=2))
        return True
    except Exception as e:
        logger.warning(f"  [price theory] could not escalate blindness: {e}")
        return False


def run(route: bool = True) -> dict:
    """Run every lens, merge with history, route what is new."""
    results = [lens() for lens in LENSES]
    comments = [r.comment for r in results if r.status == COMMENT and r.comment]
    prior = _load(COMMENTS, {})
    prior_comments = prior.get("comments", []) if isinstance(prior, dict) else []
    merged = _merge(comments, prior_comments)

    blind = bool(results) and all(r.status == CANNOT_TELL for r in results)
    escalated = _escalate_blindness(results) if blind else False
    routed = _route(merged) if route else 0

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "agent": AGENT,
        "lenses_run": len(results),
        "commented": len(comments),
        "silent": sum(1 for r in results if r.status == SILENT),
        "cannot_tell": sum(1 for r in results if r.status == CANNOT_TELL),
        "blind": blind,
        "blindness_escalated": escalated,
        "routed_to_pod_board": routed,
        "results": [r.as_dict() for r in results],
        "comments": merged,
    }
    try:
        DATA.mkdir(exist_ok=True)
        COMMENTS.write_text(json.dumps(out, indent=2))
    except Exception as e:
        logger.warning(f"  [price theory] could not write comments: {e}")
    return out


def _print(out: dict) -> None:
    logger.info("")
    logger.info("R&D POD — PRICE THEORY AGENT")
    logger.info("  %d lens(es): %d commented, %d silent, %d cannot-tell%s",
                out["lenses_run"], out["commented"], out["silent"],
                out["cannot_tell"], "  ** BLIND **" if out["blind"] else "")
    logger.info("")
    for r in out["results"]:
        if r["status"] == COMMENT:
            c = r["comment"]
            logger.info("  [%s] %s", c["severity"], c["lens"])
            logger.info("      Q: %s", c["question"])
            logger.info("      %s", c["headline"])
            logger.info("      numbers: %s", json.dumps(c["numbers"]))
        elif r["status"] == CANNOT_TELL:
            logger.info("  [CANNOT TELL] %s — %s", r["lens"], r["reason"])
        else:
            logger.info("  [silent, n=%d] %s — %s",
                        r["n_examined"], r["lens"], r["reason"])
    if out["routed_to_pod_board"]:
        logger.info("")
        logger.info("  %d comment(s) queued for the R&D pod board.",
                    out["routed_to_pod_board"])
    logger.info("")


if __name__ == "__main__":
    # The CLI has not loaded the env the library callers already hold; without
    # this every --report dies inside snapshot() on a missing ALPACA key and
    # reports it as an unreadable book (memory `fallback-that-means-act`).
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="R&D pod price-theory commentary")
    ap.add_argument("--report", action="store_true", help="human summary")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--no-route", action="store_true",
                    help="look and print, but queue nothing")
    a = ap.parse_args()

    result = run(route=not a.no_route)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    sys.exit(0)

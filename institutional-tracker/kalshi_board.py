#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI BOARD (the sector's governance seat)
==========================================================
Every other wing of this firm has a board that can say no, so this one does
too. Three seats, each owning exactly one way this strategy loses money, each
able to veto alone. The votes are deterministic rules rather than LLM opinions:
the failures they guard against are arithmetic and documentary, and a seat that
can be talked round is not a veto.

  SETTLEMENT COUNSEL   — owns "the contract did not mean what we thought".
      In 2024 Kalshi and Polymarket resolved the SAME government-shutdown
      event in OPPOSITE directions because their rule text differed. A
      structural lock is only riskless *given the rules resolve as read*, so
      somebody has to actually read them. Vetoes on absent or hedged rules.

  LIQUIDITY OFFICER    — owns "the price was not real".
      A quoted price with nothing resting behind it is not a price. Vetoes on
      thin legs and on any lock whose second leg could move while the first
      fills. This seat's brief cited "94.4% of open markets have no book" until
      2026-08-24; that rate belonged to the shard-dominated `/markets` feed, and
      on the genuine population 94.9% DO have an ask. The seat is not thereby
      retired — 53.4% of genuine markets are one-sided, which is the rate that
      decides whether a lock's second leg can actually be filled.

  CALIBRATION OFFICER  — owns "our estimate was wrong".
      Holds the quarantine on DISAGREEMENT candidates until
      kalshi_calibration.is_edge_proven() passes. This seat is expected to vote
      no for months. That is the seat working, not the seat failing.

UNANIMITY FOR STRUCTURAL LOCKS. A structural lock is claimed to be riskless;
if any seat dissents, the claim is false by construction and the trade is not
what it says it is. Majority voting would let a genuine objection be outvoted
on a position whose entire justification is that there is nothing to object to.

Nothing here transmits an order. Approval means "this passed governance and is
eligible", which the Chairman then acts on — or, once armed, a signer does.

Usage:
    python3 kalshi_board.py --report
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
BOARD_LOG = DATA_DIR / "kalshi_board_log.json"
SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"

# A leg thinner than this cannot be filled without moving the price we based
# the lock on. Deliberately stricter than the scanner's own floor: the scanner
# finds candidates, the board decides they are real.
MIN_BOARD_LEG_SIZE = 50

# Resolution text shorter than this is not a rule, it is a label.
MIN_RULES_CHARS = 40

# Words that mean a human will decide. Every one of them is a place where our
# reading and the exchange's can diverge after we are already holding.
AMBIGUITY_MARKERS = (
    "at the discretion", "sole discretion", "may be", "approximately",
    "reasonably", "substantially", "or similar", "as determined by",
    "if necessary", "in the event of a dispute",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _save(p, payload):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


# ------------------------------------------------------------------ seats --

def _settlement_counsel(candidate):
    rules = str(candidate.get("rules_primary") or "")
    if not candidate.get("requires_estimate") and not rules:
        # Structural locks arrive from the market list without rule text
        # attached. Absence of the document is not a clean bill of health.
        return "ABSTAIN", ("no rule text on this candidate — fetch the market "
                           "detail before this seat can vote")
    if len(rules) < MIN_RULES_CHARS:
        return "NO", f"resolution text is {len(rules)} chars — too thin to read"
    low = rules.lower()
    hit = [m for m in AMBIGUITY_MARKERS if m in low]
    if hit:
        return "NO", f"discretionary language in the rules: {hit[:3]}"
    return "YES", "resolution criteria are mechanical as written"


def _liquidity_officer(candidate):
    n = candidate.get("max_contracts") or candidate.get("available_size") or 0
    if n < MIN_BOARD_LEG_SIZE:
        return "NO", (f"thinnest leg holds {int(n)} contracts, below the "
                      f"{MIN_BOARD_LEG_SIZE} needed to fill without moving it")
    if candidate.get("strategy") == "STRUCTURAL_FIELD" \
            and (candidate.get("legs") or 0) > 4:
        return "NO", (f"{candidate['legs']} legs must fill near-simultaneously; "
                      f"published arbitrage windows close in ~2.7s and we poll")
    if candidate.get("requires_estimate") and not candidate.get("volume"):
        return "NO", "zero traded volume — a quote nobody has ever hit"
    return "YES", f"{int(n)} contracts resting on the thinnest leg"


def _calibration_officer(candidate):
    if not candidate.get("requires_estimate"):
        return "YES", "structural lock — arithmetic, no estimate to calibrate"
    try:
        import kalshi_calibration as cal
        ok, why = cal.is_edge_proven()
    except Exception as e:
        return "NO", f"calibration unreadable ({type(e).__name__}) — fail closed"
    if not ok:
        return "NO", f"quarantined: {why}"
    if candidate.get("p_hat") is None:
        return "NO", "no probability estimate attached to this candidate"
    return "YES", "estimator is calibration-proven and an estimate is attached"


SEATS = (("Settlement Counsel", _settlement_counsel),
         ("Liquidity Officer", _liquidity_officer),
         ("Calibration Officer", _calibration_officer))


def vote(candidate):
    """Unanimous YES approves. ABSTAIN blocks too — it is not consent."""
    votes = []
    for name, fn in SEATS:
        try:
            v, why = fn(candidate)
        except Exception as e:
            v, why = "NO", f"seat errored ({type(e).__name__}) — fail closed"
        votes.append({"seat": name, "vote": v, "reason": why})

    approved = all(v["vote"] == "YES" for v in votes)
    return {"approved": approved, "votes": votes,
            "blocked_by": [v["seat"] for v in votes if v["vote"] != "YES"]}


# ----------------------------------------------------------------- session --

def run_session(scan=None):
    scan = scan if scan is not None else _load(SCAN_LOG, {})
    candidates = (list(scan.get("structural_pair", []))
                  + list(scan.get("structural_field", []))
                  + list(scan.get("disagreement", [])))

    decisions = []
    for c in candidates:
        d = vote(c)
        decisions.append({
            "strategy": c.get("strategy"),
            "ticker": c.get("ticker") or c.get("event_ticker"),
            "title": c.get("title"), **d,
        })

    report = {
        "session_at": _now(),
        "scanned_at": scan.get("scanned_at"),
        "considered": len(decisions),
        "approved": sum(1 for d in decisions if d["approved"]),
        "decisions": decisions,
        "note": ("Approval is eligibility, not execution. No code path in this "
                 "sector transmits an order: kalshi_client.place_order raises "
                 "KalshiNotArmed unless the Chairman arms it."),
    }
    _save(BOARD_LOG, report)
    return report


def print_report():
    r = run_session()
    print(f"KALSHI BOARD — {r['session_at'][:19]}Z")
    print(f"  considered {r['considered']}, approved {r['approved']}")
    if r.get("scanned_at"):
        print(f"  on scan from {r['scanned_at'][:19]}Z")

    if not r["decisions"]:
        print("\n  Nothing to vote on. An empty board log here means the "
              "scanner\n  found no candidates, not that the board never ran — "
              "check\n  data/kalshi_scan_log.json for the same timestamp.")
        return 0

    for d in r["decisions"][:15]:
        mark = "APPROVED" if d["approved"] else "BLOCKED"
        print(f"\n  [{mark}] {d['strategy']}  {d['ticker']}")
        if d.get("title"):
            print(f"    {d['title'][:74]}")
        for v in d["votes"]:
            print(f"      {v['vote']:<8}{v['seat']:<22}{v['reason'][:62]}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.parse_args()
    raise SystemExit(print_report())

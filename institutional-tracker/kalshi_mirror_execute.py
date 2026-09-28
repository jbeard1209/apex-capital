#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI MIRROR, ARMED EXECUTION SCAFFOLD
=======================================================
When the mirror strategy passes BOTH gates (`kalshi_mirror.is_mirror_edge_proven`
on the historical backtest AND `kalshi_mirror_paper.is_paper_gate_ready` on the
30-day forward test) AND the Chairman has written `data/.kalshi_mirror_armed`,
this file identifies the day's live tight-tier candidates, sizes them per the
Phase 2 budget, and writes them to a Chairman-facing candidate queue.

WHAT THIS FILE DOES NOT DO — read carefully
--------------------------------------------
This module DOES NOT place orders on Kalshi. It does not import an order
client, does not hold API keys, does not touch the broker. Kalshi contracts
are CFTC-regulated event derivatives, and the standing rule from the
[[kalshi_sector]] postmortem is that Claude never places bets, never
connects to Jack's account, and never handles his API keys. That rule holds
regardless of how many gates are green. What "automatic betting" means in
this codebase is: candidate selection, sizing, and preflight are pre-computed
so the placement path — the one Chairman-authored step — is a click, not a
research task.

The queue this writes (`data/kalshi_mirror_candidate_queue.jsonl`, append-only)
contains one JSON line per candidate:

  {
    "generated_at":      ISO ts,
    "ticker":            "KX...-...-T...",
    "event_ticker":      "KX...-...",
    "title":             "Will X?",
    "yes_price":         0.99,          # Kalshi's YES price at scan time
    "no_price":          0.01,          # 1 - yes_price
    "days_to_close":     3.2,
    "n_contracts":       17,            # sized per 0.10 x Kelly, Phase-2 capped
    "dollars_at_risk":   0.17,          # n_contracts * no_price
    "fee_estimate_c":    1.0,           # kalshi_client.fee_cents
    "phase":             2,             # {2: $500 cap, 3: $5000 cap}
    "gate_snapshot":     {...},         # every gate reading at emit time
    "status":            "awaiting_placement",
  }

Chairman's separate placement path reads this queue, executes each
candidate, and moves it to `data/kalshi_mirror_placed.jsonl` (that writer is
NOT in this file; it is Chairman-only). If the queue file grows without the
placed-file growing, that is its own signal — a candidate line older than
its market's close time is an unfilled candidate.

GATES (all four required, in order)
-----------------------------------
1. `kalshi_mirror.is_mirror_edge_proven()` — the backtest edge is still there
   at today's ledger. If a regime shift kills the historical slice, do not
   emit candidates for it.
2. `kalshi_mirror_paper.is_paper_gate_ready()` — the 30-day forward test
   has cleared its evidential bar (n>=500, EV_net>=+0.5c, CI-low>0, 30d
   elapsed since Chairman started the paper window).
3. `data/.kalshi_mirror_armed` — Chairman's authority-token. Claude sessions
   must never write it. The check reads the payload; a corrupted or absent
   file fails closed.
4. Each candidate row must be TWO-SIDED at scan time: `available_size > 0`
   AND `spread` reported. A NO price of 0 is an empty book, not a free
   contract — [[fallback-that-means-act]].

PHASE BUDGET
------------
The Chairman brief lists $500 (Phase 2) → $5000 (Phase 3). The arm file
carries a `phase` field: `{"armed": true, "env": "paper", "phase": 2, ...}`.
This file uses that number as the total dollar cap across ALL candidates the
day emits; if a day's raw sizing would exceed it, every candidate's
`n_contracts` is pro-rated down. Nothing here decides which phase Kalshi is
in — that decision is Chairman-only and stamped into the arm file.

WHAT IF ANY GATE FAILS
----------------------
The module still runs (this is a daily cron entry point), still writes a
report, but the candidate queue stays untouched. The report file
`data/kalshi_mirror_execute_report.json` says which gate blocked it, so the
absence of new candidate lines has an audit trail.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import kalshi_calibration as kcal
import kalshi_client as kc
import kalshi_mirror as km
import kalshi_mirror_paper as kmp


DATA_DIR = Path(__file__).resolve().parent / "data"
ARM_FILE = km.MIRROR_ARM_FILE                       # data/.kalshi_mirror_armed
CANDIDATE_QUEUE = DATA_DIR / "kalshi_mirror_candidate_queue.jsonl"
EXECUTE_REPORT = DATA_DIR / "kalshi_mirror_execute_report.json"

# Two-sided-market floor. Kalshi's book APIs report available_size in
# contracts on that side. A market that says "available=0" while quoting a
# price is either latency or a fully-drained book — either way, un-fillable.
MIN_AVAILABLE_SIZE = 1

# Kelly fractional. The backtest's Kelly-optimal is dangerous on a strategy
# whose CI-low is barely above zero; 0.10x is the Phase-2 caution multiplier
# in the phase plan.
KELLY_FRACTION = 0.10

# Phase → dollar cap (per DAY across all candidates). Chairman brief:
# Phase 2 = $500 cap, Phase 3 = $5000 cap.
PHASE_BUDGET = {2: 500.0, 3: 5000.0}


def _read_arm() -> dict:
    """Return the arm payload, or {} if not armed. Never writes the file."""
    if not ARM_FILE.exists():
        return {}
    try:
        payload = json.loads(ARM_FILE.read_text())
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("armed") is not True:
        return {}
    return payload


def _today_tight_candidates() -> list:
    """Ledger rows that would be picks today: tight tier, still open,
    two-sided (available_size > 0), not already emitted to the queue.

    "Not already emitted" is a per-day dedup: a ticker already in today's
    entries of the candidate queue is not re-emitted. This lets the cron
    fire more than once a day without inflating exposure.
    """
    already = _todays_emitted_tickers()
    out = []
    for r in kcal._all_rows():
        if r.get("settled"):
            continue
        p = r.get("market_price")
        d = r.get("days_to_close")
        if p is None or d is None:
            # Rows written before the scanner started stamping horizon are
            # not comparable to the tight-tier gate; skip them silently.
            continue
        if not (km.YES_TIER_LO <= float(p) < km.YES_TIER_HI + 0.001):
            continue
        if not (km.HORIZON_LO_DAYS <= float(d) < km.HORIZON_HI_DAYS):
            continue
        if (r.get("available_size") or 0) < MIN_AVAILABLE_SIZE:
            continue
        if r.get("ticker") in already:
            continue
        out.append(r)
    return out


def _todays_emitted_tickers() -> set:
    if not CANDIDATE_QUEUE.exists():
        return set()
    today = datetime.now(timezone.utc).date().isoformat()
    seen = set()
    try:
        for line in CANDIDATE_QUEUE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = str(row.get("generated_at", ""))
            if ts.startswith(today):
                seen.add(row.get("ticker"))
    except Exception:
        pass
    return seen


def _size_candidate(row: dict, phase_budget: float, backtest: dict) -> Optional[int]:
    """Compute contract count for one candidate. Returns 0 if unfillable.

    Kelly on a discrete bet where you win yes_price when NO settles True and
    lose no_price otherwise, with settled probability p_no ≈ 1 - yes_price:
    f* = ((p * b) - q) / b, where b = yes_price/no_price. Cap by
    (a) fractional Kelly, (b) available_size, (c) phase budget slice.
    """
    yes_price = float(row["market_price"])
    no_price = 1.0 - yes_price
    if no_price <= 0.005:
        return 0
    # Historical no_win_rate on the tight tier, from is_mirror_edge_proven().
    p_no = backtest.get("no_win_rate") or (1.0 - yes_price)  # fallback
    b = yes_price / no_price if no_price > 0 else 0.0
    q = 1.0 - p_no
    if b <= 0:
        return 0
    kelly = (p_no * b - q) / b
    if kelly <= 0:
        return 0
    kelly *= KELLY_FRACTION
    # $ at risk per contract = no_price; contracts = dollars / no_price.
    max_dollars = phase_budget * kelly
    n_from_dollars = int(max_dollars / no_price)
    n_from_book = int(row.get("available_size") or 0)
    return max(0, min(n_from_dollars, n_from_book))


def _gates() -> dict:
    """Read all four gates. Returns {ready, blockers, snapshots}."""
    backtest = km.is_mirror_edge_proven()
    paper = kmp.is_paper_gate_ready()
    arm = _read_arm()

    blockers = []
    if not backtest.get("proven"):
        blockers.append(f"backtest not proven: {backtest.get('reasons_against', [])}")
    if not paper.get("ready"):
        blockers.append(f"paper gate not ready: {paper.get('reasons_against', [])}")
    if not arm:
        blockers.append(
            "no armed payload — Chairman has not written "
            "data/.kalshi_mirror_armed (that write is Chairman-only)"
        )
    phase = arm.get("phase") if arm else None
    if arm and phase not in PHASE_BUDGET:
        blockers.append(
            f"arm payload has phase={phase!r}; expected one of "
            f"{sorted(PHASE_BUDGET)} (edit the arm file, not this module)"
        )
    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "backtest_gate": backtest,
        "paper_gate": paper,
        "arm": arm,
        "phase": phase,
    }


def _emit_candidates(gates: dict) -> list:
    """Write today's candidate lines to CANDIDATE_QUEUE. Return the lines."""
    backtest = gates["backtest_gate"]
    phase = gates["phase"]
    phase_budget = PHASE_BUDGET[phase]
    tight = _today_tight_candidates()
    if not tight:
        return []

    # Pre-size, then pro-rate to fit the daily dollar cap.
    sized = []
    for r in tight:
        n = _size_candidate(r, phase_budget, backtest)
        if n <= 0:
            continue
        sized.append((r, n))
    total_dollars = sum((1.0 - r["market_price"]) * n for r, n in sized)
    if total_dollars > phase_budget and total_dollars > 0:
        scale = phase_budget / total_dollars
        sized = [(r, max(1, int(n * scale))) for r, n in sized]

    lines = []
    ts = datetime.now(timezone.utc).isoformat()
    with CANDIDATE_QUEUE.open("a") as f:
        for r, n in sized:
            yes_price = float(r["market_price"])
            no_price = 1.0 - yes_price
            fee_c = kc.fee_cents(yes_price, contracts=n, maker=False)
            entry = {
                "generated_at": ts,
                "ticker": r.get("ticker"),
                "event_ticker": r.get("event_ticker"),
                "title": r.get("title"),
                "yes_price": yes_price,
                "no_price": no_price,
                "days_to_close": r.get("days_to_close"),
                "n_contracts": n,
                "dollars_at_risk": round(no_price * n, 4),
                "fee_estimate_c": fee_c,
                "phase": phase,
                "gate_snapshot": {
                    "backtest_ev_net_c": backtest.get("ev_net_c"),
                    "backtest_ci_low_c": backtest.get("ev_net_ci_low_c"),
                    "paper_ev_net_c": gates["paper_gate"]["snapshot"].get(
                        "ev_net_cents_per_contract"),
                    "paper_ci_low_c": gates["paper_gate"]["snapshot"].get(
                        "ev_net_ci_low_cents"),
                },
                "status": "awaiting_placement",
            }
            f.write(json.dumps(entry) + "\n")
            lines.append(entry)
    return lines


def execute_mirror_trades() -> dict:
    """Daily entry point. NOT an order placer.

    Reads gates; if all clear, appends candidate rows to
    `data/kalshi_mirror_candidate_queue.jsonl` with status
    'awaiting_placement'. Chairman's separate placement path (not in this
    file) reads that queue and does the actual placement. This function
    writes no orders and never signs anything.
    """
    gates = _gates()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready": gates["ready"],
        "blockers": gates["blockers"],
        "backtest_gate": gates["backtest_gate"],
        "paper_gate": {
            "ready": gates["paper_gate"]["ready"],
            "reasons_against": gates["paper_gate"]["reasons_against"],
        },
        "arm_present": bool(gates["arm"]),
        "phase": gates["phase"],
        "candidates_emitted": 0,
        "candidates": [],
    }
    if gates["ready"]:
        emitted = _emit_candidates(gates)
        report["candidates_emitted"] = len(emitted)
        report["candidates"] = emitted
    EXECUTE_REPORT.write_text(json.dumps(report, indent=2, default=str))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute the report but do not write to the candidate queue.")
    ap.add_argument("--report", action="store_true",
                    help="Print the last-run report file.")
    args = ap.parse_args()

    if args.report:
        if not EXECUTE_REPORT.exists():
            print("no execute report yet; run without --report first")
            return 0
        print(EXECUTE_REPORT.read_text())
        return 0

    if args.dry_run:
        gates = _gates()
        print(json.dumps({
            "ready": gates["ready"],
            "blockers": gates["blockers"],
            "phase": gates["phase"],
        }, indent=2))
        return 0

    report = execute_mirror_trades()
    print("=" * 76)
    print("KALSHI MIRROR — armed-execution scaffold")
    print("=" * 76)
    if report["ready"]:
        print(f"Emitted {report['candidates_emitted']} candidate(s) at phase "
              f"{report['phase']} (${PHASE_BUDGET[report['phase']]:,.0f} cap)")
        for c in report["candidates"][:10]:
            print(f"  {c['ticker']:38s}  NO={c['no_price']*100:.2f}c  "
                  f"n={c['n_contracts']:>4d}  ${c['dollars_at_risk']:.2f}")
    else:
        print("Not emitting — blockers:")
        for b in report["blockers"]:
            print(f"  - {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

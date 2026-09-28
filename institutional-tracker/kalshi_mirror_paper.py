#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI MIRROR, PHASE 1 (paper-money prospective test)
====================================================================
The backtest in `kalshi_mirror.py` proves the mirror strategy is edge-positive
on 11,497 rows of ALREADY-SETTLED history. That is necessary but not sufficient:
a strategy can look edge-positive backward and mean-revert to zero forward, and
the fee cliff (98-100c × 1-7d) leaves the strategy exactly one narrow slice —
if that slice was carried by regime-specific noise, we would see it here.

Phase 1 is the 30-day forward test. It does not scan, it does not size, it
does not place orders. It filters the same shared ledger to rows first
observed on or after a Chairman-set START DATE, computes the same tight-tier
aggregates, and asks: does the +1.016c/contract EV_net from the backtest
survive prospectively?

WHY A SEPARATE FILE
-------------------
`kalshi_mirror.py` reads the whole ledger. If I added a `--paper` flag there,
the SAME function would answer two very different Chairman questions (past
vs future) with the same numbers, which is exactly the kind of "one metric,
two meanings" trap that `an-aggregate-is-not-a-finding` calls out. A separate
file with its own report file (`data/kalshi_mirror_paper_report.json`) keeps
the two aggregates side-by-side so a spot-check can compare them directly.

GATE TO PHASE 2 (from KALSHI_MIRROR_PHASE_PLAN_2026-09-24.md § Phase 1)
- n_prospective       >= 500 settled tight-tier rows
- EV_net              >= +0.5c/contract (mean)
- EV_net CI-low       >   0c/contract   (Wilson at 95%)
- days_since_start    >= 30
All four must hold. Then and only then does `is_paper_gate_ready()` return
True, and Phase 2's $500 cap becomes a Chairman-only arming decision — the
gate here is EVIDENCE, not authority. The authority still lives in
`data/.kalshi_mirror_armed`, which Claude sessions must never write.

WHY THE START DATE IS PERSISTED (data/.kalshi_mirror_paper_started.json)
- Sliding-window "n rows in the last 30 days" is subject to look-ahead bias:
  today's rollup would silently drop the earliest observations as they age
  out. A pinned start date fixes the window and makes the 30-day
  measurement reproducible.
- If you re-arm on a new start date (e.g. after a strategy change), the prior
  report file is renamed with the old start date suffixed, so nothing is
  overwritten silently. `--start-paper` writes; nothing else does.

WHAT THIS FILE IS NOT
- Not a trader: no order calls, no client, no key.
- Not a scanner: never talks to Kalshi's API directly; reads only the shared
  ledger the YES-side scanner already writes.
- Not an armer: creating `data/.kalshi_mirror_armed` is Chairman-only. This
  file only reports whether the EVIDENTIAL bar has been cleared.
"""

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import kalshi_calibration as kcal
import kalshi_mirror as km


DATA_DIR = Path(__file__).resolve().parent / "data"
PAPER_START_FILE = DATA_DIR / ".kalshi_mirror_paper_started.json"
PAPER_REPORT_FILE = DATA_DIR / "kalshi_mirror_paper_report.json"

# Phase-2 arming gate (evidence side only; authority still Chairman-only).
MIN_PROSPECTIVE_N = 500
MIN_PROSPECTIVE_EV_NET_C = 0.5   # cents/contract (mean)
MIN_PROSPECTIVE_DAYS = 30


def _load_start() -> dict:
    """Read the pinned paper-start payload, or {} if unstarted."""
    if not PAPER_START_FILE.exists():
        return {}
    try:
        return json.loads(PAPER_START_FILE.read_text())
    except Exception:
        return {}


def start_paper(start_date: str = None) -> dict:
    """Pin the prospective window's start. Writes .kalshi_mirror_paper_started.json.

    Not an arming action — no trading enabled. Only fixes the date from which
    'prospective' is measured, so re-runs produce the same report. Safe to
    call idempotently: if a start is already recorded and the caller passes
    no different date, the existing payload is returned unchanged.
    """
    existing = _load_start()
    if start_date is None:
        start_date = existing.get("start_date") or date.today().isoformat()
    if existing and existing.get("start_date") == start_date:
        return existing
    if existing and existing.get("start_date") != start_date:
        # Preserve prior report so we do not silently rebase.
        prior = existing.get("start_date", "unknown")
        old_report = PAPER_REPORT_FILE.with_name(
            f"kalshi_mirror_paper_report_prior_{prior}.json"
        )
        if PAPER_REPORT_FILE.exists() and not old_report.exists():
            old_report.write_text(PAPER_REPORT_FILE.read_text())
    payload = {
        "start_date": start_date,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "rule": (f"tier {km.YES_TIER_LO:.2f}-{km.YES_TIER_HI:.2f} × horizon "
                 f"{km.HORIZON_LO_DAYS:.0f}-{km.HORIZON_HI_DAYS:.0f}d"),
        "prior_start": existing.get("start_date"),
    }
    PAPER_START_FILE.write_text(json.dumps(payload, indent=2))
    return payload


def _prospective_rows(start_iso: str) -> list:
    """Ledger rows first recorded on or after start_iso, in the tight tier."""
    cutoff = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
    out = []
    for r in kcal._all_rows():
        rec = r.get("recorded_at") or r.get("recorded_date")
        if not rec:
            continue
        try:
            when = datetime.fromisoformat(str(rec).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if when < cutoff:
            continue
        p = r.get("market_price")
        d = r.get("days_to_close")
        if p is None or d is None:
            continue
        if not (km.YES_TIER_LO <= float(p) < km.YES_TIER_HI + 0.001):
            continue
        if not (km.HORIZON_LO_DAYS <= float(d) < km.HORIZON_HI_DAYS):
            continue
        if r.get("selection_rule") is not None and r.get("selection_rule") != kcal.SELECTION_RULE:
            # ignore rows from a different regime label
            continue
        out.append(r)
    return out


def snapshot() -> dict:
    """Compute the current prospective-window snapshot.

    Two halves: `pending` (open/unsettled picks) and `settled` (with outcomes).
    Only `settled` contributes to the EV_net computation and the gate.
    """
    start = _load_start()
    if not start:
        return {
            "started": False,
            "reason": ("paper test not started — run "
                       "`python3 kalshi_mirror_paper.py --start-paper` to pin "
                       "today as the prospective window's start date"),
        }
    start_iso = start["start_date"]
    rows = _prospective_rows(start_iso)
    settled = [r for r in rows if r.get("settled") and r.get("outcome") in (0, 1)]
    pending = [r for r in rows if r not in settled]

    # Same tight-tier summary shape as kalshi_mirror.analyze — but scoped
    # to the prospective window. Reuse the same helpers so any fee/units bug
    # fix in the reference file propagates here.
    n = len(settled)
    if n:
        hits_no = sum(1 for r in settled if int(r["outcome"]) == 0)
        gross_c = sum(km._mirror_pnl_per_contract(r["market_price"], r["outcome"])
                      for r in settled) / n
        mean_fee_c = sum(km._fee_c(r["market_price"]) for r in settled) / n
        net_c = gross_c - mean_fee_c
        mean_no = sum((1.0 - r["market_price"]) for r in settled) / n
        lo, hi = km._wilson_ci(hits_no, n)
        ev_gross_ci_low_c = (lo - mean_no) * 100.0
        ev_net_ci_low_c = ev_gross_ci_low_c - mean_fee_c
        no_win_rate = hits_no / n
    else:
        gross_c = net_c = ev_net_ci_low_c = 0.0
        mean_fee_c = 0.0
        no_win_rate = 0.0
        lo, hi = 0.0, 1.0

    unique_events = len({(r.get("event_ticker") or r.get("ticker", ""))[:20]
                         for r in settled})

    today = datetime.now(timezone.utc).date()
    try:
        start_date = date.fromisoformat(start_iso)
    except ValueError:
        start_date = today
    days_since = (today - start_date).days

    return {
        "started": True,
        "start_date": start_iso,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_since_start": days_since,
        "n_prospective_settled": n,
        "n_prospective_pending": len(pending),
        "unique_events_settled": unique_events,
        "no_win_rate": no_win_rate,
        "no_win_rate_ci": [lo, hi],
        "ev_net_cents_per_contract": net_c,
        "ev_net_ci_low_cents": ev_net_ci_low_c,
        "mean_fee_cents": mean_fee_c,
        "gross_ev_cents_per_contract": gross_c,
        "rule": (f"tier {km.YES_TIER_LO:.2f}-{km.YES_TIER_HI:.2f} × horizon "
                 f"{km.HORIZON_LO_DAYS:.0f}-{km.HORIZON_HI_DAYS:.0f}d"),
    }


def is_paper_gate_ready(snap: dict = None) -> dict:
    """Evidentiary gate to Phase 2. NOT an arming decision.

    Returns {ready, reasons_against}. Chairman still has to sign
    `data/.kalshi_mirror_armed` — that is authority, this is only evidence.
    """
    if snap is None:
        snap = snapshot()
    reasons = []
    if not snap.get("started"):
        reasons.append("paper test has not been started")
        return {"ready": False, "reasons_against": reasons, "snapshot": snap}
    n = snap["n_prospective_settled"]
    ev = snap["ev_net_cents_per_contract"]
    ci = snap["ev_net_ci_low_cents"]
    days = snap["days_since_start"]
    if n < MIN_PROSPECTIVE_N:
        reasons.append(f"n_prospective_settled={n} < {MIN_PROSPECTIVE_N}")
    if ev < MIN_PROSPECTIVE_EV_NET_C:
        reasons.append(f"EV_net {ev:+.3f}c < arming floor {MIN_PROSPECTIVE_EV_NET_C}c")
    if ci <= 0:
        reasons.append(f"EV_net CI-low {ci:+.3f}c is not strictly > 0")
    if days < MIN_PROSPECTIVE_DAYS:
        reasons.append(f"only {days} days elapsed since start (need {MIN_PROSPECTIVE_DAYS})")
    return {
        "ready": len(reasons) == 0,
        "reasons_against": reasons,
        "snapshot": snap,
    }


def _print_report(gate: dict) -> None:
    snap = gate.get("snapshot", {})
    print("=" * 76)
    print("KALSHI MIRROR — PHASE 1 (paper-money prospective test)")
    print("=" * 76)
    if not snap.get("started"):
        print(snap.get("reason", "not started"))
        return
    print(f"Start date:  {snap['start_date']}   days elapsed: {snap['days_since_start']}")
    print(f"Rule:        {snap['rule']}")
    print(f"Settled:     n={snap['n_prospective_settled']}  "
          f"unique_events={snap['unique_events_settled']}  "
          f"pending={snap['n_prospective_pending']}")
    print(f"NO win rate: {snap['no_win_rate']*100:.2f}%  "
          f"CI={snap['no_win_rate_ci'][0]*100:.2f}-"
          f"{snap['no_win_rate_ci'][1]*100:.2f}%")
    print(f"EV_net:      {snap['ev_net_cents_per_contract']:+.3f}c/contract   "
          f"CI-low: {snap['ev_net_ci_low_cents']:+.3f}c/contract")
    print(f"Mean fee:    {snap['mean_fee_cents']:.3f}c/contract")
    print()
    print("PHASE 2 EVIDENTIAL GATE:")
    if gate["ready"]:
        print("  READY — every empirical check passed.")
        print("  Chairman may consider arming `data/.kalshi_mirror_armed`.")
    else:
        print("  NOT READY — the following checks fail:")
        for r in gate["reasons_against"]:
            print(f"    - {r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-paper", action="store_true",
                    help="Pin today (or --date) as the prospective window's start.")
    ap.add_argument("--date", type=str, default=None,
                    help="ISO date to use with --start-paper (defaults to today).")
    ap.add_argument("--report", action="store_true",
                    help="Print the current snapshot and evidential-gate verdict.")
    ap.add_argument("--gate", action="store_true",
                    help="Emit JSON with the evidential-gate verdict.")
    args = ap.parse_args()

    if args.start_paper:
        payload = start_paper(args.date)
        print(json.dumps(payload, indent=2))
        return 0

    gate = is_paper_gate_ready()
    PAPER_REPORT_FILE.write_text(json.dumps(gate, indent=2, default=str))

    if args.gate:
        print(json.dumps({"ready": gate["ready"],
                          "reasons_against": gate["reasons_against"]}, indent=2))
        return 0
    _print_report(gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())

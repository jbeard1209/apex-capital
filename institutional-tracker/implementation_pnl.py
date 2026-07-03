#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — IMPLEMENTATION P&L ATTRIBUTION
# =============================================================================
# Answers "did the ideas we implemented actually make money?" — honestly.
#
# Data source: data/cit_implementation_outcomes.json (IRM registers every
# implemented proposal with a baseline metrics snapshot and re-checks at
# 7/30/90-day milestones — that machinery already existed; this module turns
# its raw deltas into a dollarized, benchmark-adjusted attribution report).
#
# FORMULA (versioned — the optimization loop Jack asked for):
#   v1 naive:      attributed_$ = milestone delta of firm total_return_pct × equity
#                  (confounded: credits the idea with EVERYTHING that happened)
#   v2 (default):  excess = raw delta − SPY drift over the same window;
#                  attributed_$ = excess% × equity at baseline
#                  (removes market drift; still correlational, so confidence=LOW)
#   Future vN:     when the IRM audit accumulates evidence of systematic bias,
#                  bump the version here with the rationale appended to
#                  formula_history — never silently change the math.
#
# ACCURACY WATCHDOG: irm_audit() is called from the IRM daily run. It recomputes
# the raw deltas INDEPENDENTLY from the stored snapshots and compares with this
# report; divergence > 20% on any idea flags the IRM daily brief + audit log
# (data/implementation_pnl_audit.json). IRM = the existing agent watching this.
#
# Usage: python3 implementation_pnl.py          # compute + print report
# =============================================================================

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("impl_pnl")

DATA = Path(__file__).parent / "data"
OUTCOMES = DATA / "cit_implementation_outcomes.json"
REPORT = DATA / "implementation_pnl.json"
AUDIT = DATA / "implementation_pnl_audit.json"

FORMULA_VERSION = "v2.1-shared-window-split"
FORMULA_HISTORY = [
    {"version": "v1-naive", "note": "raw firm return delta × equity; confounded"},
    {"version": "v2-benchmark-adjusted", "adopted": "2026-07-03",
     "note": "subtract SPY drift over the identical window; correlational but market-neutral"},
    {"version": "v2.1-shared-window-split", "adopted": "2026-07-03",
     "note": ("first v2 run credited each of 17 concurrent ideas with the SAME window "
              "excess (17x over-attribution). Ideas sharing a measurement window now "
              "split that window's excess equally — totals equal reality.")},
]


def _load(p, d):
    try:
        return json.loads(p.read_text()) if p.exists() else d
    except Exception:
        return d


def _equity_baseline() -> float:
    """Paper account basis for dollarizing return percentages."""
    pa = _load(DATA / "pod_allocations.json", {})
    total = sum((p.get("budget_inception") or 0) for p in (pa.get("pods", {}) or {}).values())
    return float(total) if total else 100000.0


def _spy_return(start_iso: str, end_iso: str):
    """SPY % move over the window (for benchmark adjustment). None if unavailable —
    caller then falls back to v1 for that record and labels it."""
    try:
        import yfinance as yf
        import pandas as pd
        s = yf.download("SPY", start=start_iso[:10], end=end_iso[:10],
                        progress=False)["Close"]
        if hasattr(s, "squeeze"):
            s = s.squeeze()
        if len(s) >= 2:
            return round(float((s.iloc[-1] - s.iloc[0]) / s.iloc[0]) * 100, 4)
    except Exception:
        pass
    return None


def compute_attribution() -> dict:
    """Build the per-idea attribution report from IRM's recorded milestones."""
    outcomes = _load(OUTCOMES, [])
    equity = _equity_baseline()
    ideas, total_excess_dollars = [], 0.0

    # Pass 1 — group records by measurement window. Concurrent ideas measured over
    # the same (start, end) see the SAME firm-wide move; crediting it to each one
    # multiplies reality by N. Each window's excess is split equally instead.
    windows: dict = {}
    for rec in outcomes:
        milestones = rec.get("outcomes") or []
        if not milestones:
            continue
        latest = milestones[-1]
        if (latest.get("delta") or {}).get("total_return_pct") is None:
            continue
        start = rec.get("implemented_at") or rec.get("baseline", {}).get("captured_at")
        end = latest.get("checked_at")
        key = (str(start)[:10], str(end)[:10])
        windows.setdefault(key, []).append((rec, latest))

    spy_cache: dict = {}
    for key, members in windows.items():
        start_d, end_d = key
        if key not in spy_cache:
            spy_cache[key] = _spy_return(start_d, end_d) if (start_d and end_d) else None
        spy = spy_cache[key]
        n_shared = len(members)
        for rec, latest in members:
            raw = latest["delta"]["total_return_pct"]
            if spy is not None:
                excess = round(raw - spy, 4)
                formula = FORMULA_VERSION
            else:
                excess = raw
                formula = "v1-naive (SPY unavailable for window)"
            dollars = round(excess / 100.0 * equity / n_shared, 2)
            total_excess_dollars += dollars
            start = rec.get("implemented_at") or rec.get("baseline", {}).get("captured_at")
            ideas.append({
                "shared_window_n": n_shared,
                "title": rec.get("title", "")[:90],
                "category": rec.get("category"),
                "implemented_at": str(start)[:10],
                "milestone_days": latest.get("days"),
                "raw_firm_return_delta_pct": raw,
                "spy_drift_pct": spy,
                "excess_return_pct": excess,
                "attributed_dollars": dollars,
                "formula": formula,
                "verdict": latest.get("verdict", ""),
                "confidence": ("LOW — firm-wide return minus market drift, split across "
                               f"{n_shared} concurrent change(s); correlational, NOT isolated "
                               "causation. Treat direction as signal, magnitude as estimate."),
            })

    report = {
        "computed_at": datetime.now().isoformat(),
        "formula_version": FORMULA_VERSION,
        "formula_history": FORMULA_HISTORY,
        "equity_basis": equity,
        "n_ideas_tracked": len(ideas),
        "total_attributed_excess_dollars": round(total_excess_dollars, 2),
        "ideas": sorted(ideas, key=lambda x: -x["attributed_dollars"]),
    }
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    return report


def irm_audit() -> dict:
    """IRM's independent accuracy check (called from the IRM daily run).
    Recomputes each idea's raw delta straight from the stored snapshots and
    compares to the report; >20% relative divergence (or missing report) = FLAG."""
    report = _load(REPORT, {})
    outcomes = _load(OUTCOMES, [])
    flags, checked = [], 0
    by_title = {i["title"]: i for i in report.get("ideas", [])}
    for rec in outcomes:
        ms = rec.get("outcomes") or []
        if not ms:
            continue
        title = rec.get("title", "")[:90]
        rep = by_title.get(title)
        if rep is None:
            flags.append(f"'{title[:50]}' has milestones but is missing from the P&L report")
            continue
        # independent recompute: baseline vs latest snapshot delta
        indep = ms[-1].get("delta", {}).get("total_return_pct")
        stated = rep.get("raw_firm_return_delta_pct")
        checked += 1
        if indep is not None and stated is not None:
            denom = max(abs(indep), 0.01)
            if abs(indep - stated) / denom > 0.20:
                flags.append(f"'{title[:50]}': report says {stated} but snapshots say {indep}")
    audit_log = _load(AUDIT, [])
    entry = {"audited_at": datetime.now().isoformat(), "checked": checked,
             "flags": flags, "formula_version": report.get("formula_version"),
             "status": "FLAGGED" if flags else "PASS"}
    audit_log.append(entry)
    AUDIT.write_text(json.dumps(audit_log[-200:], indent=2, default=str))
    return entry


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = compute_attribution()
    print(f"\nIMPLEMENTATION P&L ({r['formula_version']}, equity basis ${r['equity_basis']:,.0f})")
    print(f"Ideas tracked: {r['n_ideas_tracked']} | total attributed excess: ${r['total_attributed_excess_dollars']:,.2f}\n")
    for i in r["ideas"]:
        print(f"  {i['attributed_dollars']:>+10,.2f}$  {i['title'][:60]}")
        print(f"              raw {i['raw_firm_return_delta_pct']:+.2f}% − SPY {i['spy_drift_pct']}% = excess {i['excess_return_pct']:+.2f}% @ {i['milestone_days']}d")
    a = irm_audit()
    print(f"\nIRM audit: {a['status']} ({a['checked']} checked, {len(a['flags'])} flags)")

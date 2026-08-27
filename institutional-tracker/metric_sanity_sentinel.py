#!/usr/bin/env python3
"""
Metric Sanity Sentinel  (Department: CIT — code/quality oversight)
==================================================================
WHY THIS EXISTS
---------------
On 2026-07-27 we discovered the CRO had blocked ALL buys for 272 days because
the VaR/Herfindahl math weighted SIGNED market_value by the NET sum. On a
market-neutral book that inflated every weight ~87x and produced:
    - 1-day 95% VaR = 44.9% of the portfolio   (real ~0.5%)
    - Herfindahl index = 240.87                 (mathematically IMPOSSIBLE; HHI<=1)
Every existing guard (wiring_audit, smoke tests, canary, CIT) missed it because
they check that PIPES ARE CONNECTED and CODE RUNS — not that the NUMBERS flowing
through are physically possible. A wrong-but-well-formed number is invisible to
them, and this one even disguised itself as a "board must vote" governance item.

This sentinel closes that gap: it asserts that key financial metrics live inside
their physically-possible bounds. An impossible value is a BUG, and it is filed
as one (board proposal) instead of silently steering the firm for months.

WHAT IT DOES
------------
Loads the latest metric artifacts and bounds-checks them. Any violation is:
  1. written to data/metric_sanity_report.json (audit trail, --report reads this)
  2. filed as a BUG_FIX/LOGIC_ERROR proposal in data/cit_pending_proposals.json
     (consumed by deputy_board.py / board_meeting.py)
  3. if it is a *judgement* call for the Chairman (e.g. a risk posture that has
     persisted implausibly long), also handed to the Chairman decision queue via
     data/chairman_action_required.json (managed by chairman_decision_sla.py).

Cron: hourly-ish is plenty. Runs read-only except for the three JSON queues.
"""
from __future__ import annotations
import json, argparse, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
REPORT_PATH   = DATA / "metric_sanity_report.json"
CIT_PENDING   = DATA / "cit_pending_proposals.json"
CHAIR_ACTION  = DATA / "chairman_action_required.json"


def _load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _save(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2))


def _num(x):
    """Coerce to float or return None (never raise)."""
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


# ── Invariants ─────────────────────────────────────────────────────────────
# Each check returns a list of violation dicts. A violation names the metric,
# the offending value, the bound it broke, and how severe/impossible it is.
def check_risk_metrics(violations):
    rpt = _load(DATA / "risk_manager_report.json", {})
    conc = rpt.get("concentration", {}) if isinstance(rpt, dict) else {}
    var  = rpt.get("var_metrics", {}) if isinstance(rpt, dict) else {}

    hhi = _num(conc.get("herfindahl_index"))
    if hhi is not None and (hhi < 0 or hhi > 1.0):
        violations.append({
            "metric": "herfindahl_index", "value": hhi, "bound": "[0, 1]",
            "severity": "HIGH", "impossible": True,
            "note": "Herfindahl index is mathematically bounded in [1/n, 1]. A value "
                    "outside [0,1] means position weights are mis-normalized (likely "
                    "signed market_value / net sum instead of gross/absolute).",
        })

    # VaR as a fraction of portfolio
    pv = _num((rpt.get("account") or {}).get("portfolio_value")) if isinstance(rpt, dict) else None
    v95 = _num(var.get("var_95_1d"))
    if pv and pv > 0 and v95 is not None:
        frac = v95 / pv
        if frac > 1.0:
            violations.append({"metric": "var_95_1d/portfolio", "value": round(frac, 3),
                "bound": "<= 1.0", "severity": "CRITICAL", "impossible": True,
                "note": "1-day VaR exceeds 100% of the portfolio — implausible for a "
                        "long/short equity book; check weight normalization (gross vs net)."})
        elif frac > 0.60:
            violations.append({"metric": "var_95_1d/portfolio", "value": round(frac, 3),
                "bound": "<= 0.60 (plausibility)", "severity": "HIGH", "impossible": False,
                "note": "1-day 95% VaR above 60% of portfolio is extreme for this book; verify."})

    # CVaR must be >= VaR >= 0 (CVaR is the mean of the worst tail)
    cvar = _num(var.get("cvar_95"))
    if v95 is not None and cvar is not None and cvar > 0 and v95 > 0 and cvar < v95:
        violations.append({"metric": "cvar_95 vs var_95_1d", "value": f"cvar={cvar}<var={v95}",
            "bound": "cvar_95 >= var_95_1d", "severity": "MEDIUM", "impossible": True,
            "note": "CVaR (expected shortfall) cannot be smaller than VaR."})
    if cvar == 0 and v95 and v95 > 0:
        violations.append({"metric": "cvar_95", "value": 0.0, "bound": "> 0 when VaR>0",
            "severity": "MEDIUM", "impossible": False,
            "note": "CVaR is zero while VaR is positive — the tail calc likely failed "
                    "(empty return series / bad price download)."})

    # risk_score bounds
    rb = _load(DATA / "risk_bulletin.json", {})
    rs = _num(rb.get("risk_score"))
    if rs is not None and (rs < 0 or rs > 12):
        violations.append({"metric": "risk_score", "value": rs, "bound": "[0, 12]",
            "severity": "MEDIUM", "impossible": True,
            "note": "risk_score outside the additive-flag range — a flag is double-counting."})


def check_model_metrics(violations):
    meta = _load(DATA / "model_meta.json", {})
    for key in ("auc", "wf_cv_mean_auc", "walk_forward_auc_mean", "holdout_auc"):
        v = _num(meta.get(key))
        if v is None:
            continue
        if v < 0 or v > 1:
            violations.append({"metric": f"model.{key}", "value": v, "bound": "[0, 1]",
                "severity": "HIGH", "impossible": True,
                "note": "AUC must be in [0,1]."})
        elif v > 0.90:
            violations.append({"metric": f"model.{key}", "value": v, "bound": "<= 0.90 (leakage tell)",
                "severity": "MEDIUM", "impossible": False,
                "note": "AUC above 0.90 on this data is a leakage red flag (SUSPICIOUS_HIGH)."})


def check_governance_plausibility(decisions):
    """Not a bug — a JUDGEMENT flag. A risk posture that has persisted for an
    implausibly long time should reach the Chairman, not silently continue."""
    rb = _load(DATA / "risk_bulletin.json", {})
    streak = _num(rb.get("consecutive_high_cycles"))
    if streak is not None and streak >= 30:
        decisions.append({
            "id": f"metric-sanity-cro-streak-{int(streak)}",
            "severity": "HIGH",
            "description": (f"CRO risk level has persisted at HIGH for {int(streak)} consecutive "
                            f"cycles. Even if the math is now correct, a sustained buy-block this "
                            f"long is a posture the Chairman/board should consciously own — not a "
                            f"default. Verify the risk numbers are physically sane (see "
                            f"metric_sanity_report.json), then decide: maintain / step down / escalate."),
            "source": "metric_sanity_sentinel",
        })


def _file_bug_proposals(violations):
    if not violations:
        return 0
    pending = _load(CIT_PENDING, [])
    if not isinstance(pending, list):
        pending = []
    existing_ids = {p.get("id") for p in pending if isinstance(p, dict)}
    now = datetime.datetime.utcnow().isoformat()
    added = 0
    for v in violations:
        pid = f"sanity-{v['metric'].replace('/', '_').replace(' ', '_')}"
        if pid in existing_ids:
            continue
        pending.append({
            "id": pid,
            "source": "Metric Sanity Sentinel (CIT)",
            "category": "LOGIC_ERROR" if v.get("impossible") else "BUG_FIX",
            "severity": v["severity"],
            "confidence": 92 if v.get("impossible") else 70,
            "title": f"Physically-impossible metric: {v['metric']} = {v['value']}"
                     if v.get("impossible") else f"Implausible metric: {v['metric']} = {v['value']}",
            "description": v["note"],
            "evidence": f"metric_sanity_sentinel: {v['metric']}={v['value']} violates bound {v['bound']}",
            "proposed_fix": "Trace the metric's computation; verify weights use GROSS/absolute "
                            "exposure and denominators are non-degenerate. Add a bounds assert at "
                            "the source so this can never silently propagate again.",
            "auto_implementable": False,
            "ts": now,
        })
        added += 1
    if added:
        _save(CIT_PENDING, pending)
    return added


def _file_chairman_decisions(decisions):
    if not decisions:
        return 0
    actions = _load(CHAIR_ACTION, [])
    if not isinstance(actions, list):
        actions = []
    existing = {a.get("id") for a in actions if isinstance(a, dict)}
    now = datetime.datetime.utcnow().isoformat()
    added = 0
    for d in decisions:
        if d["id"] in existing:
            continue
        actions.insert(0, {**d, "ts": now, "status": "awaiting_chairman"})
        added += 1
    if added:
        _save(CHAIR_ACTION, actions)
    return added


def run(write: bool = True) -> dict:
    violations: list[dict] = []
    decisions: list[dict] = []
    check_risk_metrics(violations)
    check_model_metrics(violations)
    check_governance_plausibility(decisions)

    report = {
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "violations": violations,
        "decision_flags": decisions,
        "clean": not violations,
    }
    if write:
        # Saved twice on purpose. The first _save is crash insurance for the
        # violations themselves; but it happened BEFORE the two filings, so the
        # receipts — including whether any Chairman decision was filed at all —
        # never reached the artifact a reader actually opens. Same defect as
        # irm_review_agent.run_irm_daily and hr_agent.run_hr_review, found by
        # the same sweep: a dict mutated after the last write meant to record it.
        _save(REPORT_PATH, report)
        report["bugs_filed"] = _file_bug_proposals(violations)
        report["decisions_filed"] = _file_chairman_decisions(decisions)
        _save(REPORT_PATH, report)
    return report


def main():
    ap = argparse.ArgumentParser(description="Bounds-check the firm's financial metrics for physically-impossible values.")
    ap.add_argument("--report", action="store_true", help="print the latest report, do not re-file")
    args = ap.parse_args()

    if args.report:
        rep = _load(REPORT_PATH, None)
        if not rep:
            rep = run(write=False)
    else:
        rep = run(write=True)

    print("=" * 60)
    print("  METRIC SANITY SENTINEL")
    print("=" * 60)
    if rep["clean"]:
        print("  ✅ All checked metrics are within physical bounds.")
    else:
        print(f"  ⚠️  {len(rep['violations'])} violation(s):")
        for v in rep["violations"]:
            tag = "IMPOSSIBLE" if v.get("impossible") else "implausible"
            print(f"     [{v['severity']}/{tag}] {v['metric']} = {v['value']}  (bound {v['bound']})")
    if rep["decision_flags"]:
        print(f"  🧭 {len(rep['decision_flags'])} Chairman decision flag(s) raised.")
    if not args.report:
        print(f"  Filed: {rep.get('bugs_filed',0)} bug proposal(s), "
              f"{rep.get('decisions_filed',0)} decision item(s).")
    print()


if __name__ == "__main__":
    main()

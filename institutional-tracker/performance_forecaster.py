#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — PERFORMANCE FORECASTER  (FP&A / strategy scorecard)
# =============================================================================
# A dedicated forecasting + goal-tracking agent. Mirrors a real firm's FP&A
# function: it FORECASTS data-grounded targets (output + financial) for the next
# week / month / year, the BOARD ratifies them (proposal queue), and each run it
# GRADES the actuals against prior forecasts and keeps a calibration track record.
#
# Honesty discipline (same as the model-AUC work): forecasts are RANGES with
# explicit assumptions and a confidence flag — never a single aspirational number.
# Until the firm has real realized-return history, financial forecasts are flagged
# LOW confidence. The agent grades itself so we learn whether our forecasts are any
# good, not just whether we hit a made-up number.
#
# Files:
#   data/firm_goals.json        — active + historical goals (status: proposed/
#                                 ratified/graded), with target ranges + assumptions
#   data/firm_goals_record.json — calibration track record (forecast vs actual)
# Usage:  python3 performance_forecaster.py [--report]
# Schedule: weekly (set goals + grade matured ones). Exits 0.
# =============================================================================

import argparse, json, logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("forecaster")

BASE = Path(__file__).parent
DATA = BASE / "data"
GOALS = DATA / "firm_goals.json"
RECORD = DATA / "firm_goals_record.json"
CIT_PENDING = DATA / "cit_pending_proposals.json"
FIRM_CHANNEL = DATA / "firm_channel.json"
CHAIRMAN = DATA / "chairman_action_required.json"

HORIZONS = [("1w", 7), ("1m", 30), ("1y", 365)]


def _load(p, d):
    try:
        return json.loads(p.read_text()) if p.exists() else d
    except Exception:
        return d


def _save(p, d):
    p.write_text(json.dumps(d, indent=2, default=str))


def _firm_snapshot() -> dict:
    """Current grounded state: equity, deployment, recent output, model status."""
    alloc = _load(DATA / "pod_allocations.json", {})
    pods = alloc.get("pods", {}) if isinstance(alloc, dict) else {}
    budget, deployed, positions = 0.0, 0.0, 0
    for p in (pods.values() if isinstance(pods, dict) else []):
        b = p.get("budget_inception", 0) or 0
        npos = len(p.get("positions", {}) or {})
        budget += b; positions += npos
        v = p.get("deployed_value")
        if v is None:
            v = p.get("positions_value", 0) or 0
        deployed += float(v or 0)
    # Equity is the BROKER's, not a sum of pod book entries. This used to add
    # each pod's `total_value` (entitlement + market value) plus a reserve with
    # no broker counterpart, and forecast against a firm that was ~$5.7k larger
    # than the one that exists. `firm_value.json` is written by
    # reconciliation_check straight from the account. (2026-08-19)
    _fv = _load(DATA / "firm_value.json", {})
    equity = _fv.get("equity")
    try:
        equity = float(equity)
        if equity != equity or equity <= 0:
            equity = None
    except (TypeError, ValueError):
        equity = None
    # recent output (picks produced per pod, most recent files)
    def _n(f, *keys):
        d = _load(DATA / f, [])
        if isinstance(d, list): return len(d)
        for k in keys:
            if isinstance(d.get(k), list): return len(d[k])
        return 0
    output = {"fi_picks": _n("fi_picks_queue.json", "picks"),
              "fe_picks": _n("fe_picks_queue.json", "picks"),
              "etf_picks": _n("etf_pod_orders.json", "orders")}
    try:
        from model_metrics import get_model_metrics
        m = get_model_metrics()
        model = {"st_auc": m.get("auc"), "lt_auc": m.get("lt_auc"),
                 "st_live": bool(m.get("auc") and m.get("auc") >= 0.57)}
    except Exception:
        model = {}
    return {"equity": round(equity, 2) if equity is not None else None,
            "equity_source": ("data/firm_value.json (broker account)" if equity is not None
                              else "UNAVAILABLE — reconciliation_check has not written it"),
            "budget": round(budget, 2),
            "deployed": round(deployed, 2),
            "deployed_pct": (round(deployed / equity, 3)
                             if equity else None),
            "positions": positions, "output": output, "model": model,
            "return_vs_budget_pct": (round((equity / budget - 1) * 100, 2)
                                     if (equity is not None and budget) else None)}


def _forecast(snap: dict) -> list:
    """Honest, range-based goals per horizon. Assumptions explicit; LOW confidence
    until a realized-return track record exists."""
    eq = snap["equity"]; dep = snap["deployed_pct"]
    if eq is None:
        # No forecast is better than a forecast against a fabricated baseline:
        # every goal here is `baseline_equity * (1 + r)`, so a wrong baseline
        # produces targets that will be graded, recorded and believed. Return
        # nothing and let `run()` say why. (2026-08-19)
        return []
    dep = dep if dep is not None else 0.1
    have_track = len(_load(RECORD, {}).get("graded", [])) >= 3
    conf = "MODERATE" if have_track else "LOW — no realized track record yet; ranges are assumption-based"
    goals = []
    # Conservative per-period return-range assumptions on DEPLOYED capital (NOT a
    # promise). Wide bands; the point is to be graded and recalibrated, not to be right.
    fin = {"1w": (-1.5, 2.0), "1m": (-4.0, 6.0), "1y": (-15.0, 25.0)}
    out_per = {"1w": 1, "1m": 4, "1y": 50}   # net new vetted picks/decisions, firm-wide
    for label, days in HORIZONS:
        lo, hi = fin[label]
        # scale financial range by how much capital is actually deployed
        lo_eq = round(eq * (1 + lo / 100 * max(dep, 0.1)), 0)
        hi_eq = round(eq * (1 + hi / 100 * max(dep, 0.1)), 0)
        goals.append({
            "horizon": label, "set_date": datetime.now().strftime("%Y-%m-%d"),
            "target_date": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"),
            "status": "proposed",
            "financial_target": {"equity_low": lo_eq, "equity_high": hi_eq,
                                  "return_range_pct": [lo, hi]},
            "output_target": {"net_new_elite_picks_min": out_per[label]},
            "baseline_equity": eq, "confidence": conf,
            "assumptions": ("Range = current equity grown by an assumed per-period return band "
                            "on deployed capital. ML pods excluded from financial upside until "
                            "they clear the 0.57 gate (currently below). Grade vs actuals at target_date."),
        })
    return goals


def _grade(snap: dict):
    """Grade any active goal whose target_date has passed: actual equity vs range."""
    goals = _load(GOALS, {"active": [], "history": []})
    rec = _load(RECORD, {"graded": []})
    today = datetime.now().strftime("%Y-%m-%d")
    still = []
    if snap.get("equity") is None:
        # Grading needs an actual. Without one, leave the goals ACTIVE rather
        # than grading them against a guess — a goal graded wrong is worse than
        # a goal graded late, because the grade feeds the calibration record.
        logger.warning("  Not grading: firm equity unavailable this cycle")
        return rec
    for g in goals.get("active", []):
        if g.get("target_date", "9999") > today:
            still.append(g); continue
        ft = g.get("financial_target", {})
        lo, hi = ft.get("equity_low"), ft.get("equity_high")
        actual = snap["equity"]
        hit = (lo is not None and hi is not None and lo <= actual <= hi)
        rec["graded"].append({
            "horizon": g.get("horizon"), "set_date": g.get("set_date"),
            "graded_on": today, "predicted_range": [lo, hi], "actual_equity": actual,
            "within_range": hit,
            "miss_pct": (None if hit or not (lo and hi) else
                         round((actual - (hi if actual > hi else lo)) / ((hi + lo) / 2) * 100, 2)),
        })
        g["status"] = "graded"; goals.setdefault("history", []).append(g)
    goals["active"] = still
    _save(GOALS, goals); _save(RECORD, rec)
    return rec


def _propose_to_board(goals: list):
    if not goals:
        return                       # _forecast refused; nothing to ratify
    pend = _load(CIT_PENDING, [])
    if not isinstance(pend, list): return
    title = f"Ratify firm performance goals ({datetime.now().strftime('%Y-%m-%d')})"
    if title in {p.get("title", "") for p in pend}: return
    summary = "; ".join(f"{g['horizon']}: ${g['financial_target']['equity_low']:,.0f}-"
                        f"${g['financial_target']['equity_high']:,.0f}" for g in goals)
    pend.append({
        "id": f"FCAST-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "source": "Performance Forecaster", "category": "STRATEGY", "severity": "MEDIUM",
        "confidence": 70, "title": title,
        "description": f"Proposed data-grounded firm goals for board ratification. {summary}. "
                       "Ranges are assumption-based (low confidence until a realized track record "
                       "exists) and will be graded vs actuals.",
        "proposed_fix": "Board: ratify, adjust, or reject these targets. Forecaster grades them at horizon.",
        "owner": "Full Board + Chairman", "created_at": datetime.now().isoformat(), "status": "pending",
    })
    _save(CIT_PENDING, pend)


def run(report_only: bool = False) -> dict:
    snap = _firm_snapshot()
    if snap["equity"] is None:
        logger.warning("  Firm equity UNAVAILABLE (%s) — no goals set or graded "
                       "this cycle. Run reconciliation_check first.",
                       snap["equity_source"])
    else:
        logger.info(f"  Firm equity ${snap['equity']:,.0f} "
                    f"({snap['return_vs_budget_pct']}% vs budget) | "
                    f"{snap['positions']} positions | "
                    f"deployed {(snap['deployed_pct'] or 0)*100:.0f}%")
    rec = _grade(snap)
    goals = _load(GOALS, {"active": [], "history": []})
    # set fresh goals only if none active for this week
    if not any(g.get("set_date") == datetime.now().strftime("%Y-%m-%d") for g in goals.get("active", [])):
        new = _forecast(snap)
        goals["active"] = goals.get("active", []) + new
        _save(GOALS, goals)
        if not report_only:
            _propose_to_board(new)
        for g in new:
            ft = g["financial_target"]
            logger.info(f"  GOAL {g['horizon']}: equity ${ft['equity_low']:,.0f}-${ft['equity_high']:,.0f} "
                        f"({ft['return_range_pct'][0]}..{ft['return_range_pct'][1]}%) | conf={g['confidence'][:8]}")
    # calibration so far
    graded = rec.get("graded", [])
    if graded:
        hit = sum(1 for x in graded if x.get("within_range"))
        logger.info(f"  Forecast calibration: {hit}/{len(graded)} within range "
                    f"({100*hit/len(graded):.0f}%)")
    return {"snapshot": snap, "active_goals": goals.get("active", []), "calibration": graded}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    out = run(report_only=args.report)
    print(json.dumps({"equity": out["snapshot"]["equity"],
                      "return_vs_budget_pct": out["snapshot"]["return_vs_budget_pct"],
                      "active_goals": len(out["active_goals"]),
                      "graded": len(out["calibration"])}, indent=2, default=str))

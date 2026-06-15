#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — STRATEGY COUNCIL  (3-perspective FP&A + risk committee)
# =============================================================================
# Models how a large firm forecasts: not one number, but a BULL / BEAR / BASE
# scenario committee that debates, then a chief reconciles a base case the board
# ratifies. Three agents, three perspectives:
#
#   GrowthStrategist (BULL)  — upside case: where can we deploy more, grow output.
#   RiskStrategist   (BEAR)  — downside/stress case; READS IRM's risk view so its
#                              pessimism scales with the firm's actual risk level.
#   ChiefStrategist  (BASE)  — reconciles bull+bear into the official goals and a
#                              "council view" (consensus + dissent) for the board.
#
# WIRING (the part that matters — every output lands in a queue a real consumer
# reads, per the producer->consumer contract discipline in CLAUDE.md):
#   - Official goals + dissent  -> cit_pending_proposals.json, category STRATEGY
#       (consumed by board_meeting.py + deputy_board.py AND read by irm_review_agent)
#   - Risk escalation (bear/IRM HIGH) -> cit_pending, category IMPROVEMENT, owner IRM
#       (deputy-actionable; IRM also reads this file -> two-way contact with IRM)
#   - Pod output-accountability misses -> cit_pending, owner HR (HR reads the board queue)
#   - Severe divergence -> chairman_action_required.json
#   - Always -> firm_channel.json for visibility
# Inputs FROM IRM: data/irm_daily_check.json + data/risk_bulletin.json (risk level).
#
# Guarded by test_pipeline_smoke.TestWiringContracts (categories must be board-recognized).
# Usage: python3 strategy_council.py [--report]   Schedule: weekly (Mon, before forecaster).
# =============================================================================

import argparse, json, logging
from datetime import datetime, timedelta
from pathlib import Path

# Reuse the grounded snapshot + grading engine from the forecaster.
from performance_forecaster import (
    _firm_snapshot, _load, _save, _grade, GOALS, RECORD,
    CIT_PENDING, FIRM_CHANNEL, CHAIRMAN, HORIZONS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("strategy_council")

DATA = Path(__file__).parent / "data"
IRM_CHECK = DATA / "irm_daily_check.json"
RISK_BULLETIN = DATA / "risk_bulletin.json"

# Categories MUST be board-recognized or proposals are silently dropped (the classic
# wiring bug). STRATEGY -> full board; IMPROVEMENT -> deputy. Verified in deputy_board.py.
CAT_GOALS = "STRATEGY"
CAT_ACTION = "IMPROVEMENT"


def _irm_risk_level() -> str:
    """Read the firm's current risk level from IRM / CRO outputs (contact with IRM)."""
    for p, keys in ((RISK_BULLETIN, ("risk_level", "level", "severity", "status")),
                    (IRM_CHECK, ("risk_level", "overall_risk", "level"))):
        d = _load(p, {})
        if isinstance(d, dict):
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v.upper() in ("GREEN", "LOW", "NORMAL", "ELEVATED",
                                                        "MODERATE", "HIGH", "CRITICAL", "RED"):
                    return v.upper()
    return "UNKNOWN"


# Per-period base return bands on deployed capital (assumption-driven; graded later).
_BASE_BANDS = {"1w": (-1.5, 2.0), "1m": (-4.0, 6.0), "1y": (-15.0, 25.0)}
_OUT_TARGET = {"1w": 1, "1m": 4, "1y": 50}


class GrowthStrategist:
    """BULL: tilt the upside higher, push to deploy idle capital."""
    perspective = "BULL"

    def view(self, snap):
        idle = round((1 - snap["deployed_pct"]) * 100)
        bands = {h: (lo, round(hi * 1.4, 1)) for h, (lo, hi) in _BASE_BANDS.items()}
        notes = []
        if snap["deployed_pct"] < 0.85:
            notes.append(f"{idle}% of capital idle — deploy into the elite FE/FI queues to capture upside.")
        if not snap.get("model", {}).get("st_live"):
            notes.append("ML pods are dark; clearing the 0.57 gate would unlock a 4th return engine.")
        return {"perspective": self.perspective, "bands": bands, "stance": "deploy more",
                "notes": notes or ["Capital well-deployed; press the winners."]}


class RiskStrategist:
    """BEAR: downside/stress case, scaled by the firm's actual IRM risk level."""
    perspective = "BEAR"

    def view(self, snap, irm_risk):
        # The worse IRM says risk is, the deeper our downside band.
        mult = {"CRITICAL": 2.2, "RED": 2.2, "HIGH": 1.8, "ELEVATED": 1.4, "MODERATE": 1.4}.get(irm_risk, 1.0)
        bands = {h: (round(lo * mult, 1), hi) for h, (lo, hi) in _BASE_BANDS.items()}
        notes = [f"IRM risk level = {irm_risk}; downside scaled x{mult}."]
        if snap["deployed_pct"] > 0.8:
            notes.append(f"{round(snap['deployed_pct']*100)}% deployed — limited dry powder for a drawdown.")
        if snap.get("return_vs_budget_pct", 0) and snap["return_vs_budget_pct"] > 25:
            notes.append("Gains are largely unrealized/paper — don't extrapolate; protect the high-water mark.")
        escalate = irm_risk in ("HIGH", "CRITICAL", "RED")
        return {"perspective": self.perspective, "bands": bands, "stance": "protect capital",
                "irm_risk": irm_risk, "escalate": escalate, "notes": notes}


class ChiefStrategist:
    """BASE: reconcile bull+bear into official goals + a council view (consensus + dissent)."""
    perspective = "BASE"

    def reconcile(self, snap, bull, bear, irm_risk):
        eq, dep = snap["equity"], max(snap["deployed_pct"], 0.1)
        have_track = len(_load(RECORD, {}).get("graded", [])) >= 3
        conf = "MODERATE" if have_track else "LOW — no realized track record yet; ranges are assumption-based"
        goals = []
        for h, days in HORIZONS:
            bhi = bull["bands"][h][1]            # bull upside
            rlo = bear["bands"][h][0]            # bear downside
            base_lo, base_hi = _BASE_BANDS[h]
            # Base case leans conservative: downside toward the bear, upside a blend
            # of bull and the neutral base (don't fully credit the bull).
            lo = round((rlo + base_lo) / 2, 1)
            hi = round((bhi + base_hi) / 2, 1)
            lo_eq = round(eq * (1 + lo / 100 * dep))
            hi_eq = round(eq * (1 + hi / 100 * dep))
            goals.append({
                "horizon": h, "set_date": datetime.now().strftime("%Y-%m-%d"),
                "target_date": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"),
                "status": "proposed", "baseline_equity": eq, "confidence": conf,
                "financial_target": {"equity_low": lo_eq, "equity_high": hi_eq, "return_range_pct": [lo, hi]},
                "output_target": {"net_new_elite_picks_min": _OUT_TARGET[h]},
                "council": {"bull_band": bull["bands"][h], "bear_band": bear["bands"][h], "irm_risk": irm_risk},
            })
        dissent = abs(bull["bands"]["1y"][1] - bear["bands"]["1y"][0])
        return {"goals": goals, "dissent_spread_pct": round(dissent, 1),
                "consensus": f"BULL says '{bull['stance']}', BEAR says '{bear['stance']}' at IRM risk {irm_risk}."}


def _route_to_board(goals, base, bull, bear):
    pend = _load(CIT_PENDING, [])
    if not isinstance(pend, list):
        return 0
    existing = {p.get("title", "") for p in pend}
    added = 0
    ts = datetime.now()
    g1y = next((g for g in goals if g["horizon"] == "1y"), goals[-1])

    # 1) Official goals -> board (STRATEGY, full-board ratification; IRM also reads this)
    title = f"Ratify Strategy Council goals ({ts.strftime('%Y-%m-%d')})"
    if title not in existing:
        pend.append({
            "id": f"COUNCIL-{ts.strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
            "category": CAT_GOALS, "severity": "MEDIUM", "confidence": 70, "title": title,
            "description": (f"3-perspective forecast. {base['consensus']} Base 1y equity range "
                            f"${g1y['financial_target']['equity_low']:,.0f}-${g1y['financial_target']['equity_high']:,.0f}. "
                            f"Bull/bear dissent spread {base['dissent_spread_pct']}pp. "
                            "Ranges assumption-based (low confidence until track record); graded at horizon."),
            "proposed_fix": "Board: ratify / adjust / reject. " + " | ".join(bull["notes"][:1] + bear["notes"][:1]),
            "owner": "Full Board + Chairman", "created_at": ts.isoformat(), "status": "pending",
        })
        added += 1

    # 2) Risk escalation -> board + IRM (IMPROVEMENT, deputy-actionable; IRM reads this file)
    if bear.get("escalate"):
        rtitle = f"[Risk] Council bear case at IRM {bear['irm_risk']} ({ts.strftime('%Y-%m-%d')})"
        if rtitle not in existing:
            pend.append({
                "id": f"COUNCIL-RISK-{ts.strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
                "category": CAT_ACTION, "severity": "HIGH", "confidence": 75, "title": rtitle,
                "description": "RiskStrategist downside dominates at elevated IRM risk. " + " ".join(bear["notes"]),
                "proposed_fix": "IRM + Board: confirm hedges / trim deployment / hold dry powder.",
                "owner": "IRM Director + Board", "created_at": ts.isoformat(), "status": "pending",
            })
            added += 1

    # 3) Output-accountability -> HR (HR reads the board queue)
    out = _firm_snapshot()["output"]
    if sum(out.values()) == 0:
        htitle = f"[HR] Pods produced zero new picks this period ({ts.strftime('%Y-%m-%d')})"
        if htitle not in existing:
            pend.append({
                "id": f"COUNCIL-HR-{ts.strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
                "category": CAT_ACTION, "severity": "MEDIUM", "confidence": 70, "title": htitle,
                "description": "No new picks across pods this period — output goal at risk.",
                "proposed_fix": "HR: review pod productivity; confirm pipelines ran and standards aren't starving output.",
                "owner": "HR Director", "created_at": ts.isoformat(), "status": "pending",
            })
            added += 1

    if added:
        _save(CIT_PENDING, pend)
    return added


def _post_channel(base, bull, bear):
    fc = _load(FIRM_CHANNEL, [])
    if not isinstance(fc, list):
        return
    fc.append({"ts": datetime.now().isoformat(), "date": datetime.now().strftime("%Y-%m-%d"),
               "agent": "Strategy Council", "category": "STRATEGY", "priority": "NORMAL",
               "content": f"🏛️ Council view — {base['consensus']} Dissent {base['dissent_spread_pct']}pp. "
                          f"Goals filed for board ratification."})
    _save(FIRM_CHANNEL, fc[-500:])


def run(report_only: bool = False) -> dict:
    snap = _firm_snapshot()
    irm_risk = _irm_risk_level()
    logger.info(f"  Equity ${snap['equity']:,.0f} ({snap['return_vs_budget_pct']}%) | deployed "
                f"{snap['deployed_pct']*100:.0f}% | IRM risk={irm_risk}")
    bull = GrowthStrategist().view(snap)
    bear = RiskStrategist().view(snap, irm_risk)
    base = ChiefStrategist().reconcile(snap, bull, bear, irm_risk)
    logger.info(f"  BULL: {bull['stance']} | BEAR: {bear['stance']} (esc={bear['escalate']}) | "
                f"dissent {base['dissent_spread_pct']}pp")

    _grade(snap)  # grade matured goals
    goals = _load(GOALS, {"active": [], "history": []})
    if not any(g.get("set_date") == datetime.now().strftime("%Y-%m-%d") and g.get("source") == "council"
               for g in goals.get("active", [])):
        for g in base["goals"]:
            g["source"] = "council"
        goals["active"] = goals.get("active", []) + base["goals"]
        _save(GOALS, goals)
        if not report_only:
            n = _route_to_board(base["goals"], base, bull, bear)
            _post_channel(base, bull, bear)
            logger.info(f"  Routed {n} item(s) to board/IRM/HR queues + firm channel.")
    return {"snapshot": snap, "irm_risk": irm_risk, "base": base, "bull": bull, "bear": bear}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    out = run(report_only=args.report)
    g1y = out["base"]["goals"][-1]["financial_target"]
    print(json.dumps({"equity": out["snapshot"]["equity"], "irm_risk": out["irm_risk"],
                      "consensus": out["base"]["consensus"], "dissent_pp": out["base"]["dissent_spread_pct"],
                      "base_1y_equity_range": [g1y["equity_low"], g1y["equity_high"]]}, indent=2, default=str))

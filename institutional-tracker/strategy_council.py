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


CANARY_STATE = DATA / "canary_state.json"
APPROVED = DATA / "cit_approved_proposals.json"


def _macro_regime() -> str:
    """Read the market regime from the canary (input for the Macro strategist seat)."""
    d = _load(CANARY_STATE, {})
    if isinstance(d, dict):
        r = d.get("regime")
        if isinstance(r, dict):
            r = r.get("label") or r.get("regime") or r.get("state")
        if isinstance(r, str):
            return r.upper()
        for k in ("last_level", "market_regime"):
            v = d.get(k)
            if isinstance(v, str):
                return v.upper()
    return "UNKNOWN"


def _calibration_widen() -> float:
    """Self-correction: if our graded forecasts hit their range < 50% of the time
    (overconfident), widen future bands. Needs >=4 graded goals to act."""
    graded = _load(RECORD, {}).get("graded", [])
    if len(graded) < 4:
        return 1.0
    hit = sum(1 for g in graded if g.get("within_range"))
    rate = hit / len(graded)
    if rate < 0.5:
        return 1.5          # badly overconfident -> widen 50%
    if rate < 0.7:
        return 1.2
    return 1.0


SEAT_RECORD = DATA / "council_seat_record.json"
DIGEST = DATA / "council_digest.md"
EMAIL_BUFFER = DATA / "email_buffer.json"
_REGIME_TILT = {"BULL": +0.10, "GREEN": +0.05, "NEUTRAL": 0.0, "ORANGE": -0.08,
                "BEAR": -0.15, "RED": -0.20, "YELLOW": -0.05}


def _grade_seats(snap):
    """Track which seat called direction right. For each matured goal, compare the
    realized equity move to each seat's lean (bull=up, bear=down, macro=regime tilt
    sign) and update the hit/total record — so we can trust the better forecaster."""
    rec = _load(SEAT_RECORD, {"bull": {"hit": 0, "n": 0}, "bear": {"hit": 0, "n": 0},
                              "macro": {"hit": 0, "n": 0}})
    goals = _load(GOALS, {"active": [], "history": []})
    today = datetime.now().strftime("%Y-%m-%d")
    for g in goals.get("active", []):
        if g.get("target_date", "9999") > today or g.get("_seats_graded"):
            continue
        ret = snap["equity"] - (g.get("baseline_equity") or snap["equity"])
        up = ret >= 0
        rec["bull"]["n"] += 1; rec["bull"]["hit"] += int(up)
        rec["bear"]["n"] += 1; rec["bear"]["hit"] += int(not up)
        tilt = _REGIME_TILT.get((g.get("council") or {}).get("macro_regime", ""), 0.0)
        rec["macro"]["n"] += 1
        rec["macro"]["hit"] += int((tilt >= 0) == up)   # macro right if its tilt matched direction
        g["_seats_graded"] = True
    _save(GOALS, goals); _save(SEAT_RECORD, rec)
    return rec


def _seat_weights():
    """Hit-rate per seat (default 0.5 until >=3 samples) — used to trust the better
    forecaster more when blending the band."""
    rec = _load(SEAT_RECORD, {})
    w = {}
    for seat in ("bull", "bear", "macro"):
        s = rec.get(seat, {})
        w[seat] = round(s["hit"] / s["n"], 3) if s.get("n", 0) >= 3 else 0.5
    return w


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
# Per-pod output OKRs — a pod is accountable for sustaining a live pick pipeline.
# Keys map to _firm_snapshot()['output']. FE's bar is deliberately low (elite picks
# are rare); ML pods are exempt until they clear the 0.57 gate (cash is valid).
_POD_OUT_MIN = {"fi_picks": 3, "fe_picks": 1, "etf_picks": 1}
_POD_LABEL = {"fi_picks": "Fixed Income", "fe_picks": "First Eagle", "etf_picks": "Macro/ETF"}


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


class MacroStrategist:
    """4th seat (added when the bull/bear axis is too narrow): reads the market regime
    from the canary and tilts the whole band up (bull regime) or down (bear regime)."""
    perspective = "MACRO"

    def view(self, snap, regime):
        tilt = {"BULL": +0.10, "GREEN": +0.05, "NEUTRAL": 0.0, "ORANGE": -0.08,
                "BEAR": -0.15, "RED": -0.20, "YELLOW": -0.05}.get(regime, 0.0)
        return {"perspective": self.perspective, "regime": regime, "tilt": tilt,
                "notes": [f"Market regime = {regime}; tilt bands {tilt:+.0%}."]}


class ChiefStrategist:
    """BASE: reconcile bull+bear+macro into official goals + a council view (consensus + dissent).
    Applies calibration self-correction and bakes in the cash-is-valid guardrail."""
    perspective = "BASE"

    def reconcile(self, snap, bull, bear, macro, irm_risk):
        eq, dep = snap["equity"], max(snap["deployed_pct"], 0.1)
        graded = _load(RECORD, {}).get("graded", [])
        widen = _calibration_widen()
        if len(graded) >= 3:
            conf = "MODERATE" if widen == 1.0 else f"MODERATE (bands widened x{widen} — past forecasts overconfident)"
        else:
            conf = "LOW — no realized track record yet; ranges are assumption-based"
        tilt = macro["tilt"]
        w = _seat_weights()   # trust the seat that's been right more often
        goals = []
        for h, days in HORIZONS:
            bhi = bull["bands"][h][1]
            rlo = bear["bands"][h][0]
            base_lo, base_hi = _BASE_BANDS[h]
            # Lean the upside toward bull / downside toward bear, each scaled by that
            # seat's historical accuracy; macro tilt scaled by macro's accuracy.
            hi = round((base_hi + (bhi - base_hi) * w["bull"]) * (1 + tilt * w["macro"]) * widen, 1)
            lo = round((base_lo + (rlo - base_lo) * w["bear"]) * widen, 1)
            lo_eq = round(eq * (1 + lo / 100 * dep))
            hi_eq = round(eq * (1 + hi / 100 * dep))
            goals.append({
                "horizon": h, "set_date": datetime.now().strftime("%Y-%m-%d"),
                "target_date": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"),
                "status": "proposed", "baseline_equity": eq, "confidence": conf,
                "financial_target": {"equity_low": lo_eq, "equity_high": hi_eq, "return_range_pct": [lo, hi]},
                "output_target": {"net_new_elite_picks_min": _OUT_TARGET[h]},
                "council": {"bull_band": bull["bands"][h], "bear_band": bear["bands"][h],
                            "macro_regime": macro["regime"], "irm_risk": irm_risk, "calibration_widen": widen},
            })
        dissent = abs(bull["bands"]["1y"][1] - bear["bands"]["1y"][0])
        return {"goals": goals, "dissent_spread_pct": round(dissent, 1),
                # GUARDRAIL: a target is never a reason to lower the bar. Cash is a position.
                "guardrail": ("Cash is a valid position. A missed financial goal must NEVER pressure "
                              "deployment into picks below the elite standard (FE 25-50% MoS corroborated; "
                              "ML only above the 0.57 gate). Goals measure discipline, not override it."),
                "consensus": (f"BULL '{bull['stance']}', BEAR '{bear['stance']}', MACRO regime "
                              f"{macro['regime']} ({tilt:+.0%}), IRM risk {irm_risk}.")}


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
            "proposed_fix": ("Board: ratify / adjust / reject. " + " | ".join(bull["notes"][:1] + bear["notes"][:1])
                             + " GUARDRAIL: " + base.get("guardrail", "")),
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

    # 3) Per-pod output OKRs -> HR (HR reads the board queue). Each pod below its
    #    minimum is flagged individually so HR can review THAT pod — not a vague
    #    firm-wide note. ML pods are intentionally exempt (gated, cash is valid).
    out = _firm_snapshot()["output"]
    for key, minimum in _POD_OUT_MIN.items():
        if out.get(key, 0) < minimum:
            pod = _POD_LABEL[key]
            htitle = f"[HR] {pod} pod below output OKR ({out.get(key,0)}/{minimum}) ({ts.strftime('%Y-%m-%d')})"
            if htitle not in existing:
                pend.append({
                    "id": f"COUNCIL-HR-{key}-{ts.strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
                    "category": CAT_ACTION, "severity": "MEDIUM", "confidence": 70, "title": htitle,
                    "description": f"{pod} produced {out.get(key,0)} picks vs OKR minimum {minimum} this period.",
                    "proposed_fix": (f"HR: review {pod} pod productivity — confirm its pipeline ran and that "
                                     "the elite standard isn't starving output (widen the search, don't lower the bar)."),
                    "owner": "HR Director", "created_at": ts.isoformat(), "status": "pending",
                })
                added += 1

    # 4) Extreme bull/bear dissent -> Chairman (the committee can't agree; needs a human call)
    if base.get("dissent_spread_pct", 0) >= 80:
        actions = _load(CHAIRMAN, [])
        if isinstance(actions, list) and not any(
            (a.get("description", "").startswith("[Council] Extreme dissent")) for a in actions
            if a.get("status") not in ("resolved", "dismissed")):
            actions.append({
                "id": f"COUNCIL-DISSENT-{ts.strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
                "severity": "HIGH", "status": "open", "resolution_note": "",
                "description": (f"[Council] Extreme dissent {base['dissent_spread_pct']}pp between bull and bear "
                                f"({base['consensus']}). The committee can't converge — Chairman steers."),
                "ts": ts.isoformat(),
            })
            _save(CHAIRMAN, actions)

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


def _calibration_report(force=False):
    """Quarterly: report how good our forecasts have been (range hit-rate + seat
    accuracy) to the board — not just the silent auto-widen. Fires every ~90 days."""
    rec = _load(RECORD, {"graded": []})
    graded = rec.get("graded", [])
    last = rec.get("last_calibration_report", "")
    due = force or (not last) or (
        (datetime.now() - datetime.fromisoformat(last[:19])).days >= 90 if last else True)
    if not due or len(graded) < 1:
        return None
    hit = sum(1 for g in graded if g.get("within_range"))
    rate = round(hit / len(graded) * 100) if graded else 0
    seats = _load(SEAT_RECORD, {})
    seat_txt = ", ".join(f"{s}: {(v.get('hit',0)/v['n']*100):.0f}% ({v.get('hit',0)}/{v['n']})"
                         for s, v in seats.items() if v.get("n"))
    pend = _load(CIT_PENDING, [])
    title = f"Strategy Council quarterly calibration ({datetime.now().strftime('%Y-%m-%d')})"
    if isinstance(pend, list) and title not in {p.get("title", "") for p in pend}:
        pend.append({
            "id": f"COUNCIL-CAL-{datetime.now().strftime('%Y%m%d%H%M%S')}", "source": "Strategy Council",
            "category": CAT_GOALS, "severity": "LOW", "confidence": 80, "title": title,
            "description": (f"Forecast calibration over {len(graded)} graded goals: {rate}% landed in range. "
                            f"Seat accuracy — {seat_txt or 'insufficient samples'}. "
                            f"{'Bands auto-widened due to overconfidence.' if rate < 50 else 'Calibration acceptable.'}"),
            "proposed_fix": "Board: note calibration; the council auto-widens bands when range hit-rate < ~70%.",
            "owner": "Full Board", "created_at": datetime.now().isoformat(), "status": "pending",
        })
        _save(CIT_PENDING, pend)
    rec["last_calibration_report"] = datetime.now().isoformat()
    _save(RECORD, rec)
    return {"range_hit_pct": rate, "n": len(graded), "seats": seat_txt}


def _write_digest(snap, base, bull, bear, macro, irm_risk):
    """Human-readable council digest (markdown) + buffered to the Chairman email."""
    g = {x["horizon"]: x["financial_target"] for x in base["goals"]}
    seats = _load(SEAT_RECORD, {})
    seat_txt = " | ".join(f"{s} {(v.get('hit',0)/v['n']*100):.0f}%" for s, v in seats.items() if v.get("n")) or "no track record yet"
    md = f"""# Strategy Council Digest — {datetime.now().strftime('%Y-%m-%d')}

**Firm equity:** ${snap['equity']:,.0f} ({snap['return_vs_budget_pct']}% vs budget) | deployed {snap['deployed_pct']*100:.0f}% | {snap['positions']} positions
**IRM risk:** {irm_risk} | **Market regime:** {macro['regime']}

## Committee view
- 🐂 **Bull (Growth):** {bull['stance']} — {bull['notes'][0]}
- 🐻 **Bear (Risk):** {bear['stance']} — {bear['notes'][0]}
- 🌐 **Macro:** regime {macro['regime']}, tilt {macro['tilt']:+.0%}
- 🪑 **Chief (Base):** {base['consensus']} Dissent {base['dissent_spread_pct']}pp.

## Goals (board ratification pending)
| Horizon | Equity range |
|---|---|
| 1 week  | ${g['1w']['equity_low']:,.0f} – ${g['1w']['equity_high']:,.0f} |
| 1 month | ${g['1m']['equity_low']:,.0f} – ${g['1m']['equity_high']:,.0f} |
| 1 year  | ${g['1y']['equity_low']:,.0f} – ${g['1y']['equity_high']:,.0f} |

**Seat accuracy (trust-weighting):** {seat_txt}
**Guardrail:** {base['guardrail']}
"""
    DIGEST.write_text(md)
    # buffer into the Chairman email digest
    eb = _load(EMAIL_BUFFER, [])
    if isinstance(eb, list):
        eb.append({"ts": datetime.now().isoformat(), "agent": "Strategy Council",
                   "subject": f"Apex — Strategy Council Digest {datetime.now().strftime('%b %d')}",
                   "body": md, "priority": "NORMAL"})
        _save(EMAIL_BUFFER, eb[-200:])
    return md


def _sync_ratifications() -> int:
    """Close the loop: when the board APPROVES the council's goal proposal, flip the
    matching active goals from 'proposed' to 'ratified' so they aren't proposed forever."""
    approved = _load(APPROVED, [])
    if not isinstance(approved, list):
        return 0
    ratified_dates = set()
    for p in approved:
        if p.get("source") == "Strategy Council" and "Ratify Strategy Council goals" in (p.get("title") or ""):
            # title carries the set-date: "...goals (YYYY-MM-DD)"
            import re as _re
            m = _re.search(r"\((\d{4}-\d{2}-\d{2})\)", p.get("title", ""))
            if m:
                ratified_dates.add(m.group(1))
    if not ratified_dates:
        return 0
    goals = _load(GOALS, {"active": [], "history": []})
    n = 0
    for g in goals.get("active", []):
        if g.get("status") == "proposed" and g.get("set_date") in ratified_dates:
            g["status"] = "ratified"; n += 1
    if n:
        _save(GOALS, goals)
    return n


def run(report_only: bool = False) -> dict:
    snap = _firm_snapshot()
    irm_risk = _irm_risk_level()
    regime = _macro_regime()
    logger.info(f"  Equity ${snap['equity']:,.0f} ({snap['return_vs_budget_pct']}%) | deployed "
                f"{snap['deployed_pct']*100:.0f}% | IRM risk={irm_risk} | regime={regime}")
    bull = GrowthStrategist().view(snap)
    bear = RiskStrategist().view(snap, irm_risk)
    macro = MacroStrategist().view(snap, regime)
    base = ChiefStrategist().reconcile(snap, bull, bear, macro, irm_risk)
    logger.info(f"  BULL: {bull['stance']} | BEAR: {bear['stance']} (esc={bear['escalate']}) | "
                f"MACRO: {regime} | dissent {base['dissent_spread_pct']}pp")

    rat = _sync_ratifications()
    if rat:
        logger.info(f"  Board ratified {rat} prior goal(s) -> status=ratified.")
    _grade_seats(snap)   # score which seat called direction right (before _grade archives them)
    _grade(snap)         # grade matured goals vs range
    w = _seat_weights()
    logger.info(f"  Seat accuracy weights: bull {w['bull']} | bear {w['bear']} | macro {w['macro']}")
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
    # Digest + quarterly calibration run every invocation (they report the current
    # view, independent of whether fresh goals were set today).
    if not report_only:
        cal = _calibration_report()
        _write_digest(snap, base, bull, bear, macro, irm_risk)
        logger.info("  Council digest written (data/council_digest.md) + buffered to Chairman email"
                    + (f"; quarterly calibration filed ({cal['range_hit_pct']}% in range)" if cal else ""))
    return {"snapshot": snap, "irm_risk": irm_risk, "base": base, "bull": bull, "bear": bear,
            "macro": macro, "seat_weights": w}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    out = run(report_only=args.report)
    g1y = out["base"]["goals"][-1]["financial_target"]
    print(json.dumps({"equity": out["snapshot"]["equity"], "irm_risk": out["irm_risk"],
                      "consensus": out["base"]["consensus"], "dissent_pp": out["base"]["dissent_spread_pct"],
                      "base_1y_equity_range": [g1y["equity_low"], g1y["equity_high"]]}, indent=2, default=str))

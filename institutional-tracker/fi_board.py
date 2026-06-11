#!/usr/bin/env python3
"""
APEX CAPITAL — FIXED INCOME BOARD
====================================
Votes on picks from fi_picks_queue.json.
Three board members vote: Duration Risk Officer, Credit Analyst, Portfolio Manager.
Approved picks are written to data/fi_approved_picks.json.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("fi_board")

DATA_DIR        = Path(__file__).parent / "data"
PICKS_QUEUE     = DATA_DIR / "fi_picks_queue.json"
APPROVED_PICKS  = DATA_DIR / "fi_approved_picks.json"
BOARD_LOG       = DATA_DIR / "fi_board_log.json"

MAX_POSITIONS   = 8    # max concurrent FI positions
MAX_PER_TIER    = 3    # max positions per credit tier (diversification)
MIN_SCORE       = 55   # minimum opportunity score to even be reviewed


def _load(path: Path, default=None):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


_FI_BOARD_SYSTEM = """You are a member of the Apex Capital Fixed Income Board.
The FI pod holds bond ETFs and fixed-income instruments with a $30k budget.
Current spread regimes: TIGHT (<300bps), NORMAL (300-450), WIDE (450-700), DISTRESSED (>700).

Vote on a bond pick: APPROVE | REJECT | WATCH
Reply in exactly this JSON format (no other text):
{"vote": "APPROVE" | "REJECT" | "WATCH", "reason": "one concise sentence"}"""


def _fi_llm_vote(role: str, context: str, pick: dict, regime: str) -> tuple[str, str]:
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("fi_board"): return None, None
        pick_text = (f"Ticker: {pick.get('ticker','?')}\nScore: {pick.get('opportunity_score',0)}\n"
                     f"Credit tier: {pick.get('credit_tier','?')}\nVerdict: {pick.get('verdict','?')}\n"
                     f"Regime: {regime}\nYield curve slope: {pick.get('credit_quality',{}).get('yield_curve_slope','?')}")
        import json as _j
        raw = _r(_FI_BOARD_SYSTEM, f"You are the {role}.\n{context}\n\nPick:\n{pick_text}",
                 agent_name="fi_board", max_tokens=150)
        if not raw: return None, None
        p = _j.loads(raw.strip())
        v, r = p.get("vote","").upper(), p.get("reason","")
        return (v, r) if v in ("APPROVE","REJECT","WATCH") and r else (None, None)
    except Exception: return None, None


def _duration_officer_vote(pick: dict, regime: str) -> tuple[str, str]:
    v, r = _fi_llm_vote("Duration Risk Officer",
        "Focus on yield curve shape and duration risk. Inverted curves hurt long duration. "
        "Reward short-duration picks in uncertain regimes.", pick, regime)
    if v: return v, r
    score = pick.get("opportunity_score", 50)
    slope = pick.get("credit_quality", {}).get("yield_curve_slope", 0) or 0
    if regime in ("WIDE", "DISTRESSED") and score >= 68:
        return "APPROVE", f"Spread regime {regime} supports risk-on; score {score}"
    if slope < -0.3 and "long" in str(pick.get("credit_quality", {}).get("duration", "")):
        return "REJECT", f"Inverted curve (slope={slope:.2f}); long duration risky"
    if score >= 65:
        return "APPROVE", f"Score {score} clears duration threshold"
    return "WATCH", f"Score {score} marginal"


def _credit_analyst_vote(pick: dict, regime: str) -> tuple[str, str]:
    v, r = _fi_llm_vote("Credit Analyst",
        "Focus on credit quality, spread regime, and default risk. "
        "HY in TIGHT spreads is poor risk/reward. EM local adds FX risk in stress.", pick, regime)
    if v: return v, r
    tier  = pick.get("credit_tier", pick.get("credit_quality", {}).get("credit_tier", "IG"))
    score = pick.get("opportunity_score", 50)
    conv  = pick.get("conviction", 3)
    if tier == "HY" and regime == "TIGHT":
        return "REJECT", "HY spreads TIGHT — poor risk/reward"
    if tier == "EM_LOCAL" and regime in ("WIDE", "DISTRESSED"):
        return "WATCH", "EM local adds FX risk in stress"
    if score >= 70 and conv >= 3:
        return "APPROVE", f"Score {score}, conviction {conv}"
    if score >= 58:
        return "APPROVE", f"Acceptable score {score}"
    return "REJECT", f"Score {score} below threshold"


def _portfolio_manager_vote(pick: dict, existing_tiers: dict) -> tuple[str, str]:
    tier  = pick.get("credit_tier", pick.get("credit_quality", {}).get("credit_tier", "IG"))
    score = pick.get("opportunity_score", 50)
    v, r  = _fi_llm_vote("Portfolio Manager",
        "Focus on portfolio construction: tier diversification, concentration limits. "
        f"Already in these tiers: {existing_tiers}. Max {MAX_PER_TIER} per tier.", pick, "")
    if v: return v, r
    if existing_tiers.get(tier, 0) >= MAX_PER_TIER:
        return "REJECT", f"Max {MAX_PER_TIER} positions in {tier} tier reached"
    if score >= 65:
        return "APPROVE", f"Portfolio fit good; diversifies into {tier}"
    return "WATCH", f"Score {score} — watchlist"


def run_fi_board() -> dict:
    queue_data = _load(PICKS_QUEUE, {})
    picks      = queue_data.get("picks", [])
    regime     = queue_data.get("regime", "NORMAL")

    if not picks:
        logger.info("[FIBoard] No picks to review.")
        return {"approved_count": 0, "rejected_count": 0, "approved": []}

    logger.info("[FIBoard] Reviewing %d pick(s) | regime=%s", len(picks), regime)

    approved   = []
    rejected   = []
    tier_count = {}

    for pick in sorted(picks, key=lambda p: p.get("opportunity_score", 0), reverse=True):
        if len(approved) >= MAX_POSITIONS:
            break

        score = pick.get("opportunity_score", 0)
        if score < MIN_SCORE:
            pick["board_decision"] = "REJECTED"
            pick["board_reason"]   = f"Score {score} below minimum {MIN_SCORE}"
            rejected.append(pick)
            continue

        tier = pick.get("credit_tier", pick.get("credit_quality", {}).get("credit_tier", "IG"))

        v1, r1 = _duration_officer_vote(pick, regime)
        v2, r2 = _credit_analyst_vote(pick, regime)
        v3, r3 = _portfolio_manager_vote(pick, tier_count)

        votes      = [v1, v2, v3]
        approves   = votes.count("APPROVE")
        rejects    = votes.count("REJECT")

        if rejects >= 2:
            decision = "REJECTED"
        elif approves >= 2:
            decision = "APPROVED"
        else:
            decision = "WATCH"

        pick["board_votes"]    = [
            {"member": "Duration Officer",  "vote": v1, "reason": r1},
            {"member": "Credit Analyst",    "vote": v2, "reason": r2},
            {"member": "Portfolio Manager", "vote": v3, "reason": r3},
        ]
        pick["board_decision"] = decision
        pick["board_ts"]       = datetime.now().isoformat()

        if decision == "APPROVED":
            tier_count[tier] = tier_count.get(tier, 0) + 1
            approved.append(pick)
            logger.info("[FIBoard] APPROVED: %s (score=%d, tier=%s)", pick.get("ticker"), score, tier)
        elif decision == "REJECTED":
            rejected.append(pick)
        else:
            logger.info("[FIBoard] WATCH: %s (score=%d)", pick.get("ticker"), score)

    # Write approved picks
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(APPROVED_PICKS, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "regime":       regime,
            "approved":     approved,
            "rejected":     rejected,
        }, f, indent=2, default=str)

    # Append to board log
    log = _load(BOARD_LOG, {"sessions": []})
    log["sessions"].append({
        "ts":             datetime.now().isoformat(),
        "regime":         regime,
        "reviewed":       len(picks),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
    })
    log["sessions"] = log["sessions"][-30:]
    with open(BOARD_LOG, "w") as f:
        json.dump(log, f, indent=2)

    logger.info("[FIBoard] Done: %d approved, %d rejected", len(approved), len(rejected))
    return {
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "approved":       approved,
        "regime":         regime,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_fi_board()
    print(f"Approved: {result['approved_count']}")
    for p in result.get("approved", []):
        print(f"  {p.get('ticker')}: score={p.get('opportunity_score')} tier={p.get('credit_tier')}")

#!/usr/bin/env python3
"""APEX CAPITAL — FIRST EAGLE BOARD
Votes on FE picks from fe_picks_queue.json.
Three members: Value Officer, Quality Officer, Risk Officer.
"""
from __future__ import annotations
import json, logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("fe_board")
DATA_DIR = Path(__file__).parent / "data"
PICKS_QUEUE    = DATA_DIR / "fe_picks_queue.json"
APPROVED_PICKS = DATA_DIR / "fe_approved_picks.json"
BOARD_LOG      = DATA_DIR / "fe_board_log.json"
MIN_MOS        = 0.25   # 25% minimum margin of safety
MAX_POSITIONS  = 6


def _load(p, default=None):
    try:
        if p.exists():
            with open(p) as f: return json.load(f)
    except Exception as _e: logger.debug('[FEBoard] load failed: %s', _e); pass
    return default

_FE_SYSTEM = """You are a member of the Apex Capital First Eagle Value Board.
First Eagle strategy: deep value, conviction driven by Margin of Safety (IV vs market price).
Min acceptable MoS: 25%. Max 6 positions. Budget: $5,000 (Lane 4).

Vote on a pick: APPROVE | WATCH | REJECT
Reply in exactly this JSON format (no other text):
{"vote": "APPROVE" | "WATCH" | "REJECT", "reason": "one concise sentence"}"""


def _fe_llm_vote(role: str, context: str, pick: dict) -> tuple[str, str]:
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("fe_board"): return None, None
        mos   = pick.get("margin_of_safety", 0) or 0
        iv    = pick.get("intrinsic_value", pick.get("iv", "?"))
        price = pick.get("current_price", pick.get("price", "?"))
        text  = (f"Ticker: {pick.get('ticker','?')}\nMargin of Safety: {mos*100:.1f}%\n"
                 f"Intrinsic Value: {iv}\nCurrent Price: {price}\n"
                 f"Conviction: {pick.get('conviction','?')}\nVerdict: {pick.get('fe_verdict','?')}")
        import json as _j
        raw = _r(_FE_SYSTEM, f"You are the {role}.\n{context}\n\nPick:\n{text}",
                 agent_name="fe_board", max_tokens=150)
        if not raw: return None, None
        p = _j.loads(raw.strip())
        v, r = p.get("vote","").upper(), p.get("reason","")
        return (v, r) if v in ("APPROVE","WATCH","REJECT") and r else (None, None)
    except Exception: return None, None


def run_board() -> dict:
    queue = _load(PICKS_QUEUE, {})
    if isinstance(queue, list):
        picks = queue
    else:
        picks = queue.get("picks", [])
    if not picks:
        return {"approved_count": 0, "rejected_count": 0, "approved": []}

    approved, rejected = [], []
    for pick in sorted(picks, key=lambda p: p.get("margin_of_safety", 0) or 0, reverse=True):
        mos      = pick.get("margin_of_safety", 0) or 0
        conv     = pick.get("conviction", 0) or 0
        if len(approved) >= MAX_POSITIONS:
            break

        # LLM votes (fall back to rule-based if unavailable)
        vv, rv = _fe_llm_vote("Value Officer",
            "Focus on margin of safety. Require ≥25% MoS to APPROVE. "
            "WATCH if 10-25% MoS. REJECT if below 10%.", pick)
        if not vv:
            vv = "APPROVE" if mos >= MIN_MOS else ("WATCH" if mos >= 0.10 else "REJECT")
            rv = f"MoS={mos*100:.1f}%"

        vq, rq = _fe_llm_vote("Quality Officer",
            "Focus on business quality and conviction. Deep value requires durable earnings. "
            "Require conviction ≥3. Reject lottery tickets and speculative names.", pick)
        if not vq:
            vq = "APPROVE" if conv >= 3 else "REJECT"
            rq = f"conviction={conv}"

        vr, rr = _fe_llm_vote("Risk Officer",
            "Focus on downside protection. Require MoS ≥15% AND conviction ≥2. "
            "Flag leverage, cyclicals, and concentrated bets.", pick)
        if not vr:
            vr = "APPROVE" if mos >= 0.15 and conv >= 2 else "REJECT"
            rr = f"MoS={mos*100:.1f}% conv={conv}"

        approves = [vv, vq, vr].count("APPROVE")
        decision = "APPROVED" if approves >= 2 else ("WATCH" if approves == 1 else "REJECTED")
        pick["board_decision"] = decision
        pick["board_votes"] = [
            {"member": "Value Officer",   "vote": vv, "reason": rv},
            {"member": "Quality Officer", "vote": vq, "reason": rq},
            {"member": "Risk Officer",    "vote": vr, "reason": rr},
        ]
        if decision == "APPROVED":
            approved.append(pick)
            logger.info("[FEBoard] APPROVED: %s MoS=%.1f%% conv=%d", pick.get("ticker"), mos*100, conv)
        else:
            rejected.append(pick)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(APPROVED_PICKS, "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "approved": approved, "rejected": rejected}, f, indent=2, default=str)

    log = _load(BOARD_LOG, {"sessions": []})
    log["sessions"].append({"ts": datetime.now().isoformat(), "approved": len(approved), "rejected": len(rejected)})
    log["sessions"] = log["sessions"][-30:]
    with open(BOARD_LOG, "w") as f:
        json.dump(log, f, indent=2)

    return {"approved_count": len(approved), "rejected_count": len(rejected), "approved": approved}

# Legacy alias
run_fe_board = run_board

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = run_board()
    print(f"Approved: {r['approved_count']}")
    for p in r.get("approved", []):
        print(f"  {p.get('ticker')}: MoS={p.get('margin_of_safety',0)*100:+.1f}%")

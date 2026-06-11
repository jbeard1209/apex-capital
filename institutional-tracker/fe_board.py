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

def run_board() -> dict:
    queue = _load(PICKS_QUEUE, {})
    picks = queue.get("picks", [])
    if not picks:
        return {"approved_count": 0, "rejected_count": 0, "approved": []}

    approved, rejected = [], []
    for pick in sorted(picks, key=lambda p: p.get("margin_of_safety", 0) or 0, reverse=True):
        mos      = pick.get("margin_of_safety", 0) or 0
        conv     = pick.get("conviction", 0) or 0
        verdict  = pick.get("fe_verdict", pick.get("verdict", "HOLD"))
        if len(approved) >= MAX_POSITIONS:
            break
        # All three board members vote
        v_val = "APPROVE" if mos >= MIN_MOS else ("WATCH" if mos >= 0.10 else "REJECT")
        v_qty = "APPROVE" if conv >= 3 else "REJECT"
        v_rsk = "APPROVE" if mos >= 0.15 and conv >= 2 else "REJECT"
        approves = [v_val, v_qty, v_rsk].count("APPROVE")
        decision = "APPROVED" if approves >= 2 else ("WATCH" if approves == 1 else "REJECTED")
        pick["board_decision"] = decision
        pick["board_votes"] = [
            {"member": "Value Officer",   "vote": v_val, "reason": f"MoS={mos*100:.1f}%"},
            {"member": "Quality Officer", "vote": v_qty, "reason": f"conviction={conv}"},
            {"member": "Risk Officer",    "vote": v_rsk, "reason": f"MoS={mos*100:.1f}% conv={conv}"},
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

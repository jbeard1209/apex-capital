#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — ML / QUANTITATIVE POD BOARD (Pod 2)
# =============================================================================
#
# Owns Pod 2 (ML / Quantitative, $30k). Currently passive (CRO=ELEVATED blocking
# deployment) but board governance is in place for when ML model activates.
#
# Board members:
#   - ML Research Director   (model quality, AUC, feature engineering)
#   - Quant Risk Officer     (live pick validation, model drift, overfitting)
#   - Data Quality Officer   (data pipeline health, feature drift, lookahead bias)
#
# Reads from: ml_pod_pending.json
# Writes to:  ml_pod_approved.json, ml_pod_board_log.json
# =============================================================================

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("ml_pod_board")

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
PENDING_PATH  = DATA_DIR / "ml_pod_pending.json"
APPROVED_PATH = DATA_DIR / "ml_pod_approved.json"
BOARD_LOG     = DATA_DIR / "ml_pod_board_log.json"
MAIN_PENDING  = DATA_DIR / "cit_pending_proposals.json"

os.chdir(BASE_DIR)


def _load(path, default=None):
    try:
        if path.exists():
            with open(path) as f: return json.load(f)
    except Exception: pass
    return default if default is not None else {}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, indent=2, default=str)


_ML_BOARD_SYSTEM = """You are a member of the Apex Capital ML / Quantitative Pod Board.
Pod 2 owns a $30k quantitative trading pod using LightGBM/XGBoost models trained on 13F data.
Current model status: training on 1.8M historical holdings; AUC target >0.60.

Vote on CIT proposals: APPROVE | HOLD | ESCALATE
Reply in exactly this JSON format (no other text):
{"vote": "APPROVE" | "HOLD" | "ESCALATE", "reason": "one concise sentence"}"""


def _llm_vote_ml(role: str, context: str, proposal: dict) -> tuple[str, str]:
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("ml_pod_board"): return None, None
        prop_text = (f"Title: {proposal.get('title','')}\nSeverity: {proposal.get('severity','LOW')}\n"
                     f"Description: {proposal.get('description','')[:400]}\n"
                     f"Fix: {proposal.get('proposed_fix','')[:300]}\nConfidence: {proposal.get('confidence',70)}%")
        import json as _j
        raw = _r(_ML_BOARD_SYSTEM, f"You are the {role}.\n{context}\n\nProposal:\n{prop_text}",
                 agent_name="ml_pod_board", max_tokens=200)
        if not raw: return None, None
        p = _j.loads(raw.strip())
        v, r = p.get("vote","").upper(), p.get("reason","")
        return (v, r) if v in ("APPROVE","HOLD","ESCALATE") and r else (None, None)
    except Exception: return None, None


def _board_vote(proposal: dict) -> dict:
    sev   = proposal.get("severity", "LOW")
    title = proposal.get("title", "").lower()
    fix   = proposal.get("proposed_fix", "").lower()
    conf  = proposal.get("confidence", 70)

    # Research Director
    v1, r1 = _llm_vote_ml("ML Research Director",
        "Focus on model quality: AUC, feature engineering, overfitting, data leakage. "
        "Approve anything that improves model accuracy or training pipeline.", proposal)
    if not v1:
        v1 = "APPROVE" if sev in ("CRITICAL","HIGH") or any(x in title for x in ["auc","model","train","feature","drift"]) else "HOLD"
        r1 = f"Model quality concern at {sev}" if v1 == "APPROVE" else "LOW priority for model team"

    # Quant Risk Officer
    v2, r2 = _llm_vote_ml("Quant Risk Officer",
        "Focus on live pick risk, deployment safety, and capital. "
        "Escalate anything touching capital allocation or live positions.", proposal)
    if not v2:
        if any(x in fix+title for x in ["live picks","deploy","capital","budget"]):
            v2, r2 = "ESCALATE", "Capital deployment decisions require main board"
        elif sev in ("CRITICAL","HIGH"):
            v2, r2 = "APPROVE", f"Risk concern at {sev} severity"
        else:
            v2, r2 = "APPROVE", "No portfolio risk"

    # Data Quality Officer
    v3, r3 = _llm_vote_ml("Data Quality Officer",
        "Focus on data pipeline integrity, feature drift, lookahead bias. "
        "Hold proposals with low confidence or vague evidence.", proposal)
    if not v3:
        v3 = "APPROVE" if conf >= 60 or any(x in title for x in ["data","pipeline","validation","picks"]) else "HOLD"
        r3 = f"Confidence {conf}% acceptable" if v3 == "APPROVE" else f"Low confidence {conf}%"

    votes    = [v1, v2, v3]
    decision = "ESCALATE" if "ESCALATE" in votes else ("APPROVED" if votes.count("APPROVE") >= 2 else "DEFERRED")

    return {
        "decision":  decision,
        "voted_at":  datetime.now().isoformat(),
        "votes": [
            {"member": "ML Research Director",  "vote": v1, "reason": r1},
            {"member": "Quant Risk Officer",    "vote": v2, "reason": r2},
            {"member": "Data Quality Officer",  "vote": v3, "reason": r3},
        ],
    }


def run_ops_session() -> dict:
    pending_raw = _load(PENDING_PATH, [])
    if not isinstance(pending_raw, list): pending_raw = []
    pending = [p for p in pending_raw if p.get("status") in ("pending", None, "")]

    if not pending:
        logger.info("[ML Board] No pending proposals.")
        return {"voted": 0, "approved": 0, "deferred": 0, "escalated": 0}

    logger.info("[ML Board] %d proposal(s) to review", len(pending))
    approved, deferred, escalated, results = [], [], [], []

    for prop in pending:
        vote_result = _board_vote(prop)
        decision    = vote_result["decision"]
        prop.update(vote_result)
        prop["status"] = decision.lower()

        if decision == "APPROVED": approved.append(prop)
        elif decision == "ESCALATE": escalated.append(prop)
        else: deferred.append(prop)
        results.append(prop)

    _save(PENDING_PATH, results)

    existing = _load(APPROVED_PATH, [])
    if not isinstance(existing, list): existing = []
    existing.extend(approved)
    _save(APPROVED_PATH, existing[-100:])

    if escalated:
        main = _load(MAIN_PENDING, [])
        if not isinstance(main, list): main = []
        existing_titles = {p.get("title","") for p in main}
        for prop in escalated:
            if prop.get("title","") not in existing_titles:
                prop["escalated_from"] = "ml_pod_board"
                prop["escalated_at"]   = datetime.now().isoformat()
                main.append(prop)
        _save(MAIN_PENDING, main)

    log = _load(BOARD_LOG, {"sessions": []})
    if not isinstance(log, dict): log = {"sessions": []}
    log["sessions"].append({"ts": datetime.now().isoformat(), "type": "ops",
                             "approved": len(approved), "deferred": len(deferred), "escalated": len(escalated)})
    log["sessions"] = log["sessions"][-50:]
    _save(BOARD_LOG, log)

    return {"voted": len(pending), "approved": len(approved), "deferred": len(deferred), "escalated": len(escalated)}


def print_report():
    log      = _load(BOARD_LOG, {})
    pending  = _load(PENDING_PATH, [])
    approved = _load(APPROVED_PATH, [])
    print(f"\n{'='*55}")
    print(f"  ML POD BOARD — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")
    open_p = [p for p in (pending if isinstance(pending,list) else []) if p.get("status") in ("pending","")]
    print(f"  Open proposals: {len(open_p)} | Approved lifetime: {len(approved) if isinstance(approved,list) else 0}")
    sessions = log.get("sessions",[])
    if sessions:
        last = sessions[-1]
        print(f"  Last session: {last.get('ts','?')[:16]} → {last.get('approved',0)}A/{last.get('deferred',0)}D/{last.get('escalated',0)}E")
    print()


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('MLPodBoard'):
        raise SystemExit(0)
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    print_report() if args.report else run_ops_session()

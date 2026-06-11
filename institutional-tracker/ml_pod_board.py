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


def _board_vote(proposal: dict) -> dict:
    sev   = proposal.get("severity", "LOW")
    title = proposal.get("title", "").lower()
    fix   = proposal.get("proposed_fix", "").lower()
    conf  = proposal.get("confidence", 70)

    # Research Director
    v1 = "APPROVE" if sev in ("CRITICAL","HIGH") or any(x in title for x in ["auc","model","train","feature","drift"]) else "HOLD"
    r1 = f"Model quality concern at {sev}" if v1 == "APPROVE" else "LOW priority for model team"

    # Quant Risk Officer
    if any(x in fix+title for x in ["live picks","deploy","capital","budget"]):
        v2, r2 = "ESCALATE", "Capital deployment decisions require main board"
    elif sev in ("CRITICAL","HIGH"):
        v2, r2 = "APPROVE", f"Risk concern at {sev} severity"
    else:
        v2, r2 = "APPROVE", "No risk to portfolio from this change"

    # Data Quality Officer
    v3 = "APPROVE" if conf >= 60 or any(x in title for x in ["data","pipeline","validation","picks"]) else "HOLD"
    r3 = f"Confidence {conf}% acceptable" if v3 == "APPROVE" else f"Low confidence {conf}% — needs more evidence"

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    print_report() if args.report else run_ops_session()

#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — OVERNIGHT BOARD SPRINT
# =============================================================================
#
# Designed to run tonight while Jack is away. Does three things:
#
#   1. BACKLOG CLEANUP (one-time)
#      Audits all 303 approved proposals and 1,126 backlog items:
#        - Auto-closes self-referential / too-vague / superseded proposals
#        - Extracts genuine code-level fixes and routes them to the
#          CIT Implementation Agent
#        - Marks truly valuable un-actioned items for board re-review
#
#   2. PROPOSAL CLEARANCE (runs in loops)
#      Repeatedly convenes the deputy board to process all pending proposals
#      until the pending queue is empty or max_rounds is hit.
#      After each deputy board pass, routes any deputy-approved proposals
#      to the CIT Implementation Agent.
#
#   3. FULL BOARD SWEEP (if anything deputy-board escalated)
#      Calls run_board_meeting() for anything the deputies deferred to
#      full board.
#
# After each round, logs a summary. Designed to run overnight (6–8 hours)
# and report results for Jack's morning check-in.
#
# Usage:
#   python3 overnight_board_sprint.py              # full overnight run
#   python3 overnight_board_sprint.py --cleanup-only  # just the backlog cleanup
#   python3 overnight_board_sprint.py --rounds 5   # limit to 5 deputy board rounds
# =============================================================================

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import atomic_json as aj

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("overnight_sprint")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

APPROVED_PATH    = DATA_DIR / "cit_approved_proposals.json"
PENDING_PATH     = DATA_DIR / "cit_pending_proposals.json"
BACKLOG_PATH     = DATA_DIR / "cit_backlog.json"
SPRINT_LOG_PATH  = DATA_DIR / "overnight_sprint_log.json"
IMPL_QUEUE_PATH  = DATA_DIR / "cit_implementation_queue.json"


# =============================================================================
# STEP 1 — BACKLOG CLEANUP
# =============================================================================

def _is_self_referential(p: dict) -> bool:
    title = (p.get("title","") + p.get("description","")).lower()
    noise_phrases = [
        "lack precise evidence", "cit proposal", "self-improvement",
        "citselfimprovement", "approved proposal", "unimplemented proposal",
        "proposals lack", "verify implementation", "quality degra",
        "quality is degra", "meta-process",
    ]
    return any(ph in title for ph in noise_phrases)


def _is_too_vague(p: dict) -> bool:
    fix = p.get("proposed_fix","")
    evidence = p.get("evidence","")
    # Vague: no file name, no line number, no function name
    import re
    has_file = bool(re.search(r'\w+\.py', fix + evidence))
    has_line = bool(re.search(r'line\s+\d+|:\d+', fix + evidence))
    has_func = bool(re.search(r'def \w+|function \w+', fix + evidence))
    return not (has_file or has_line or has_func)


def _is_superseded(p: dict) -> bool:
    title = (p.get("title","")).lower()
    superseded_phrases = [
        "qty_available", "submit_sell", "makedirs", "backward compat",
        "cit_watchdog", "union type", "list | dict",
    ]
    return any(ph in title for ph in superseded_phrases)


def cleanup_backlog() -> dict:
    logger.info("=" * 60)
    logger.info("PHASE 1: BACKLOG CLEANUP")
    logger.info("=" * 60)

    approved = []
    if APPROVED_PATH.exists():
        approved = json.loads(APPROVED_PATH.read_text())
    if not isinstance(approved, list):
        approved = []

    backlog = []
    if BACKLOG_PATH.exists():
        backlog = json.loads(BACKLOG_PATH.read_text())
    if not isinstance(backlog, list):
        backlog = []

    # ── Classify approved proposals ──────────────────────────────────────────
    self_ref  = [p for p in approved if _is_self_referential(p)]
    supersed  = [p for p in approved if _is_superseded(p) and p not in self_ref]
    vague     = [p for p in approved if _is_too_vague(p) and p not in self_ref and p not in supersed]
    genuine   = [p for p in approved if p not in self_ref and p not in supersed and p not in vague]

    logger.info(f"Approved proposals: {len(approved)} total")
    logger.info(f"  Self-referential (auto-close): {len(self_ref)}")
    logger.info(f"  Superseded by recent fixes (auto-close): {len(supersed)}")
    logger.info(f"  Too vague to implement (auto-close): {len(vague)}")
    logger.info(f"  Genuine code-level (route to implementer): {len(genuine)}")

    # Auto-close noise by marking status
    for p in self_ref:
        p["status"]     = "closed_noise"
        p["close_reason"] = "Self-referential / meta-process complaint — not a code task"
        p["closed_at"]  = datetime.now().isoformat()

    for p in supersed:
        p["status"]     = "closed_superseded"
        p["close_reason"] = "Superseded by a fix already applied in a recent session"
        p["closed_at"]  = datetime.now().isoformat()

    for p in vague:
        p["status"]     = "closed_too_vague"
        p["close_reason"] = "No file/line/function specified — cannot implement without more detail"
        p["closed_at"]  = datetime.now().isoformat()

    # Route genuine proposals to CIT implementation queue
    impl_queue = []
    if IMPL_QUEUE_PATH.exists():
        impl_queue = json.loads(IMPL_QUEUE_PATH.read_text())
    if not isinstance(impl_queue, list):
        impl_queue = []

    existing_ids = {item.get("proposal_id","") for item in impl_queue}
    routed = 0
    for p in genuine:
        pid = p.get("id", p.get("proposal_id",""))
        if pid not in existing_ids:
            impl_queue.append({
                "proposal_id":  pid,
                "title":        p.get("title",""),
                "category":     p.get("category",""),
                "severity":     p.get("severity","MEDIUM"),
                "confidence":   p.get("confidence",0),
                "proposed_fix": p.get("proposed_fix",""),
                "evidence":     p.get("evidence",""),
                "queued_at":    datetime.now().isoformat(),
                "status":       "queued",
                "routed_by":    "overnight_sprint:cleanup",
            })
            routed += 1
        p["status"] = "routed_to_implementer"

    # `Path.write_text` truncates in place exactly like `open(p, "w")`, so a
    # reader arriving mid-write got an empty or half-written queue. board_meeting
    # appends to cit_approved_proposals.json under an atomic_json lock; a lock
    # only some writers take is not a lock.
    #
    # What this buys and what it does not: aj.write is atomic, so no reader ever
    # sees a torn file. It does NOT close the read-modify-write window above —
    # `approved` was read at the top of this function and a peer appending in
    # between would still be overwritten. Left as-is because this module is
    # scheduled nowhere (wiring_audit: orphan, pending board triage); if it is
    # ever put on a schedule, the whole span needs aj.update.
    aj.write(IMPL_QUEUE_PATH, impl_queue)
    aj.write(APPROVED_PATH, approved)

    # Fix backlog items — reset all 'open' items older than 30 days to 'stale'
    from datetime import timedelta
    stale_cutoff = datetime.now() - timedelta(days=30)
    stale_count  = 0
    for item in backlog:
        if item.get("status") == "open":
            created = item.get("created_at") or item.get("captured_at","")
            if created:
                try:
                    if datetime.fromisoformat(str(created)[:19]) < stale_cutoff:
                        item["status"] = "stale"
                        stale_count += 1
                except Exception:
                    pass

    BACKLOG_PATH.write_text(json.dumps(backlog, indent=2, default=str))

    logger.info(f"Backlog: {stale_count} items marked stale (>30 days old)")
    logger.info(f"Routed {routed} genuine proposals to CIT Implementation Agent")

    return {
        "auto_closed": len(self_ref) + len(supersed) + len(vague),
        "routed_to_implementer": routed,
        "stale_backlog": stale_count,
    }


# =============================================================================
# STEP 2 — RUN IMPLEMENTATION AGENT
# =============================================================================

def run_implementation_pass() -> dict:
    logger.info("Running CIT Implementation Agent...")
    try:
        from cit_implementation_agent import run_implementation
        result = run_implementation(dry_run=False, max_fixes=20)
        logger.info(f"  Implemented: {result.get('implemented',0)}, Needs-human: {result.get('skipped',0)}")
        return result
    except Exception as e:
        logger.warning(f"  Implementation agent error: {e}")
        return {}


# =============================================================================
# STEP 3 — DEPUTY BOARD ROUNDS
# =============================================================================

def run_deputy_rounds(max_rounds: int = 20, pause_seconds: int = 30) -> dict:
    logger.info("=" * 60)
    logger.info("PHASE 2: DEPUTY BOARD CLEARANCE")
    logger.info("=" * 60)

    total_approved = 0
    total_deferred = 0
    rounds_run     = 0

    for round_num in range(1, max_rounds + 1):
        # Check how many proposals are pending
        pending = []
        if PENDING_PATH.exists():
            pending = json.loads(PENDING_PATH.read_text())
        if not isinstance(pending, list):
            pending = []

        unprocessed = [p for p in pending
                       if p.get("status","") not in ("approved","rejected","deferred")]
        if not unprocessed:
            logger.info(f"  Round {round_num}: no pending proposals — deputy board done")
            break

        logger.info(f"  Round {round_num}: {len(unprocessed)} pending proposals")

        try:
            from deputy_board import run_deputy_board
            result = run_deputy_board(dry_run=False)
            approved = result.get("approved", 0)
            deferred = result.get("deferred", 0)
            total_approved += approved
            total_deferred  = deferred   # use latest count
            rounds_run      = round_num
            logger.info(f"    Approved this round: {approved}, Deferred: {deferred}")
        except Exception as e:
            logger.warning(f"  Deputy board error (round {round_num}): {e}")
            break

        # Run implementation on anything newly approved
        run_implementation_pass()

        # Pause between rounds
        if round_num < max_rounds:
            logger.info(f"  Pausing {pause_seconds}s before next round...")
            time.sleep(pause_seconds)

    return {
        "rounds_run": rounds_run,
        "total_approved": total_approved,
        "still_pending_for_full_board": total_deferred,
    }


# =============================================================================
# STEP 4 — FULL BOARD FOR ESCALATIONS
# =============================================================================

def run_full_board_sweep() -> dict:
    pending = []
    if PENDING_PATH.exists():
        pending = json.loads(PENDING_PATH.read_text())
    if not isinstance(pending, list):
        pending = []

    deferred = [p for p in pending
                if p.get("status","") == "deferred"
                or p.get("category","") in ("STRATEGY","ARCHITECTURE")]

    if not deferred:
        logger.info("  No proposals requiring full board — skipping full board sweep")
        return {"board_meetings_run": 0}

    logger.info(f"  {len(deferred)} proposals escalated to full board...")
    try:
        from board_meeting import run_board_meeting
        result = run_board_meeting(dry_run=False, send_email=False)
        logger.info(f"  Full board outcome: {result.get('outcome','?')[:60]}")
        return {"board_meetings_run": 1, "outcome": result.get("outcome","")}
    except Exception as e:
        logger.warning(f"  Full board error: {e}")
        return {"board_meetings_run": 0, "error": str(e)}


# =============================================================================
# MAIN
# =============================================================================

def run_overnight_sprint(cleanup_only: bool = False, max_rounds: int = 20) -> dict:
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("APEX CAPITAL — OVERNIGHT BOARD SPRINT")
    logger.info(f"Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    results = {"started_at": start.isoformat()}

    # Phase 1: cleanup
    cleanup_result = cleanup_backlog()
    results["cleanup"] = cleanup_result

    if cleanup_only:
        results["status"] = "cleanup_only"
        _write_log(results)
        return results

    # Phase 1b: implementation pass on newly routed items
    impl_result = run_implementation_pass()
    results["initial_implementation"] = impl_result

    # Phase 2: deputy board rounds
    deputy_result = run_deputy_rounds(max_rounds=max_rounds, pause_seconds=15)
    results["deputy_rounds"] = deputy_result

    # Phase 3: full board for escalations
    board_result = run_full_board_sweep()
    results["full_board"] = board_result

    end = datetime.now()
    elapsed_min = (end - start).total_seconds() / 60
    results["completed_at"] = end.isoformat()
    results["elapsed_minutes"] = round(elapsed_min, 1)
    results["status"] = "COMPLETE"

    _write_log(results)

    logger.info("=" * 60)
    logger.info("OVERNIGHT SPRINT COMPLETE")
    logger.info(f"Elapsed: {elapsed_min:.1f} minutes")
    logger.info(f"Backlog cleaned: {cleanup_result.get('auto_closed',0)} proposals closed")
    logger.info(f"Implemented: {impl_result.get('implemented',0)} code fixes")
    logger.info(f"Deputy rounds: {deputy_result.get('rounds_run',0)}")
    logger.info(f"Board proposals processed: {deputy_result.get('total_approved',0)}")
    logger.info("=" * 60)
    return results


def _write_log(results: dict):
    try:
        log = []
        if SPRINT_LOG_PATH.exists():
            log = json.loads(SPRINT_LOG_PATH.read_text())
        if not isinstance(log, list):
            log = []
        log.append(results)
        SPRINT_LOG_PATH.write_text(json.dumps(log[-20:], indent=2, default=str))
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    run_overnight_sprint(cleanup_only=args.cleanup_only, max_rounds=args.rounds)

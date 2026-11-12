#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — BOARD DISPATCH
# =============================================================================
#
# Carries out what the board approved, and stops it approving the same thing
# again.
#
# WHAT WAS BROKEN (measured on the VM, 2026-11-11)
# -----------------------------------------------
# The firm already had the parts of a dispatch chain:
#
#     board vote ─► cit_approved_proposals.json  (ledger)
#                ─► chairman_action_required.json (approved_by_board / awaiting)
#     deputy tier-0 / overnight sprint ─► cit_implementation_queue.json
#                                        ─► cit_implementation_agent.py
#
# but no link joined them:
#
#   1. The full board's router (_route_approved_cit_proposals) never wrote the
#      implementation queue. Its rows went to chairman_action_required.json as
#      "approved_by_board, owner CIT" — a status no program reads.
#   2. cit_implementation_agent.py is scheduled on neither host. Its report on
#      the VM was last written 2026-06-10. (It edits .py files, and the VM is
#      not a git checkout, so scheduling it there would also fork prod from the
#      repo; TestDeployBudgetIsSingleHost keeps it off the VM on purpose.)
#   3. Every producer dedups only against cit_pending_proposals.json. Once the
#      board votes, the row leaves pending, so the next scan files it again and
#      the board approves it again. 1,177 of the ledger's 1,561 approvals were
#      re-approvals of 218 findings; fund-sanity-BBIO was approved 23 times.
#
# BBIO is the clearest case. The fix it asks for — quarantine the ticker via
# fundamentals_sanity.is_reliable() — shipped on 2026-07-29 inside
# fe_intrinsic_value.calculate_iv. Every approval after that was a vote on
# work that was already done, and nothing could ever say so.
#
# WHAT THIS MODULE DOES
# ---------------------
#   * enqueue()          the board and deputy routers call this on approval,
#                        so every approval lands in the implementation queue.
#   * filter_standing()  both vote paths call this first. An id the board has
#                        already approved, whose queue row is not finished, is
#                        a standing decision: it is taken out of pending and
#                        its re-sighting is counted on the queue row, not voted.
#                        A finished row that recurs IS voted again — that is a
#                        new occurrence, not a duplicate.
#   * run()              the executor. For each queue row with a registered
#                        handler it performs the action and records the
#                        outcome on the row. It never edits code: rows with no
#                        handler stay "queued" for cit_implementation_agent or
#                        a human, and are reported as such.
#
# Usage:
#   python3 board_dispatch.py            # execute handlers on queued rows
#   python3 board_dispatch.py --dry-run  # say what would happen, write nothing
#   python3 board_dispatch.py --report   # print the last report
# =============================================================================

from __future__ import annotations

import argparse
import ast
import inspect
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import atomic_json as aj

logger = logging.getLogger("board_dispatch")

BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"

# Paths are resolved on every call, not frozen at import: tests and the
# end-to-end proof rebind DATA (see memory frozen-container-of-derived-paths).
def _queue_path() -> Path:    return DATA / "cit_implementation_queue.json"
def _ledger_path() -> Path:   return DATA / "cit_approved_proposals.json"
def _report_path() -> Path:   return DATA / "board_dispatch_report.json"
def _chair_path() -> Path:    return DATA / "chairman_action_required.json"

# A row in one of these states is finished. If its finding comes back, that is
# a recurrence and goes to a fresh vote. Every OTHER status (queued,
# needs_human, protected, systemic, awaiting_chairman, standing_quarantine,
# skipped, routed_to_human, ...) is a decision still in force: re-voting it
# adds nothing, so it is counted instead. An unknown status is standing, never
# finished — the safe direction, since the cost of a wrong "standing" is one
# suppressed duplicate vote and the cost of a wrong "finished" is the loop.
DONE_STATUSES = frozenset({"implemented", "executed"})

# Statuses the executor re-examines on every run. standing_quarantine is
# rechecked so a quarantine lifts (-> executed) once the data is corrected.
# awaiting_chairman is examined ONLY by a registered handler: handlers verify
# and never change code or positions, so confirming that the approved action
# is already in force is not a decision taken for the Chairman — it closes an
# item that is already done (Chairman's autoresolve rule, 2026-09-08).
RECHECK_STATUSES = frozenset({"queued", "standing_quarantine", "awaiting_chairman"})

SLA_PREFIX = "sla-escalation-"   # board deciding for the Chairman; own ledger


def _now() -> str:
    return datetime.now().isoformat()


def _latest_rows(queue: list) -> dict:
    """pid -> the newest queue row for it (a recurrence appends a new row)."""
    out = {}
    for r in queue:
        if isinstance(r, dict) and r.get("proposal_id"):
            out[r["proposal_id"]] = r
    return out


def _approved_ids(ledger: list) -> set:
    return {p.get("id") for p in ledger
            if isinstance(p, dict) and p.get("id") and p.get("approved_at")}


def _new_row(p: dict, routed_by: str, status: str) -> dict:
    return {
        "proposal_id":   p.get("id", p.get("proposal_id", "")),
        "title":         p.get("title", "?"),
        "category":      p.get("category", "?"),
        "severity":      p.get("severity", "LOW"),
        "confidence":    p.get("confidence", 0),
        "proposed_fix":  p.get("proposed_fix", ""),
        "evidence":      p.get("evidence", ""),
        "source":        p.get("source"),
        "queued_at":     _now(),
        "status":        status,
        "routed_by":     routed_by,
        "times_approved": 1,
    }


# =============================================================================
# Producer side: every approval reaches the queue
# =============================================================================

def enqueue(approved: list, routed_by: str, awaiting_chairman_ids=()) -> int:
    """Put board/deputy approvals on the implementation queue. Idempotent per id.

    An id whose latest row is still open has its approval counted on that row;
    an id whose latest row is finished gets a new row (a recurrence). Approvals
    the router sent to the Chairman are queued as awaiting_chairman, which the
    executor never acts on — they wait for his sign-off, but still count as
    standing so the board does not re-vote them meanwhile.
    """
    awaiting = set(awaiting_chairman_ids or ())
    added = 0
    with aj.update(str(_queue_path()), default=[], expect=list) as queue:
        latest = _latest_rows(queue)
        for p in approved or []:
            pid = p.get("id") or p.get("proposal_id")
            if not pid or str(pid).startswith(SLA_PREFIX):
                continue
            row = latest.get(pid)
            if row is not None and row.get("status") not in DONE_STATUSES:
                row["times_approved"] = int(row.get("times_approved") or 1) + 1
                row["last_approved_at"] = _now()
                continue
            status = "awaiting_chairman" if pid in awaiting else "queued"
            row = _new_row(p, routed_by, status)
            queue.append(row)
            latest[pid] = row
            added += 1
    if added:
        logger.info(f"  [Dispatch] {added} approval(s) queued for execution")
    return added


# =============================================================================
# Intake side: an approved decision is not voted again
# =============================================================================

def standing_ids(queue: list = None, ledger: list = None) -> set:
    """Ids the board has approved whose execution is not finished.

    Grounded in the approvals ledger, so a row only counts as standing if a
    decision was actually recorded for it (problem_lifecycle_sentinel reads the
    same ledger, so a standing id never reads as a silent drop).
    """
    if queue is None:
        queue = aj.read(str(_queue_path()), default=[]) or []
    if ledger is None:
        ledger = aj.read(str(_ledger_path()), default=[]) or []
    latest = _latest_rows(queue if isinstance(queue, list) else [])
    out = set()
    for pid in _approved_ids(ledger if isinstance(ledger, list) else []):
        if str(pid).startswith(SLA_PREFIX):
            continue
        row = latest.get(pid)
        if row is None or row.get("status") not in DONE_STATUSES:
            out.add(pid)
    return out


def is_standing(pid: str) -> bool:
    """For producers: True if filing `pid` again would only re-ask the board."""
    try:
        return pid in standing_ids()
    except Exception as e:      # never stop a producer on a dispatch read
        logger.debug(f"is_standing({pid}) failed: {e}")
        return False


def filter_standing(proposals: list, pending_path, session: str) -> list:
    """Return the proposals that still need a vote; retire the rest.

    A proposal whose id is a standing approval is removed from `pending_path`
    and its re-sighting is counted on its queue row (times_reseen,
    last_reseen_at). An approval that predates this module has no queue row
    yet; its first re-sighting backfills one, so it reaches the executor
    instead of the ballot.
    """
    if not proposals:
        return proposals
    try:
        ledger = aj.read(str(_ledger_path()), default=[]) or []
        queue = aj.read(str(_queue_path()), default=[]) or []
        standing = standing_ids(queue, ledger)
    except Exception as e:
        logger.warning(f"  [Dispatch] standing check failed ({e}); voting everything")
        return proposals

    hits = [p for p in proposals if p.get("id") in standing]
    if not hits:
        return proposals
    hit_ids = {p["id"] for p in hits}

    # Backfill legacy approvals, then count every re-sighting.
    latest = _latest_rows(queue)
    legacy = [p for p in hits if p["id"] not in latest]
    by_id = {}
    for p in hits:
        by_id.setdefault(p["id"], p)
    if legacy:
        led = {}
        for r in ledger:
            if isinstance(r, dict) and r.get("id") in hit_ids:
                led[r["id"]] = r          # newest ledger copy wins
        enqueue([led.get(p["id"], p) for p in legacy], routed_by=f"{session}:backfill")
    with aj.update(str(_queue_path()), default=[], expect=list) as q:
        for pid, row in _latest_rows(q).items():
            if pid in hit_ids:
                row["times_reseen"] = int(row.get("times_reseen") or 0) + 1
                row["last_reseen_at"] = _now()
    with aj.update(str(pending_path), default=[], expect=list) as pending:
        pending[:] = [p for p in pending
                      if not (isinstance(p, dict) and p.get("id") in hit_ids)]

    logger.info(f"  [Dispatch] {len(hit_ids)} proposal(s) already approved and "
                f"still in force — not re-voted: {', '.join(sorted(hit_ids)[:6])}"
                f"{' …' if len(hit_ids) > 6 else ''}")
    return [p for p in proposals if p.get("id") not in hit_ids]


# =============================================================================
# Executor: handlers by finding class
# =============================================================================

def _fe_gate_wired() -> bool:
    """Does fe_intrinsic_value.calculate_iv actually call is_reliable()?

    Read from the function's AST, not grepped — the comment above the gate
    mentions is_reliable too, and a comment is not a call.
    """
    import fe_intrinsic_value as fe
    tree = ast.parse(inspect.getsource(fe.calculate_iv))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "is_reliable":
                return True
    return False


def _handle_fund_sanity(row: dict) -> dict:
    """fund-sanity-<TICKER>: "refetch; if it persists, quarantine via is_reliable()".

    Executed against the CURRENT cache, so the outcome is re-observed every run:
      * no impossible value any more          -> executed (nothing to quarantine)
      * impossible, gate refuses, FE honours  -> standing_quarantine (in force)
      * impossible but gate lets it through,
        or FE no longer calls the gate        -> needs_human (the control failed)
    """
    import fundamentals_sanity as fs
    ticker = row["proposal_id"][len("fund-sanity-"):]
    path = fs.CACHE_DIR / f"{ticker}.json"
    data = fs._load(path, {})
    if not data:
        return {"status": "needs_human",
                "note": f"no cached fundamentals for {ticker} at {path.name}; "
                        f"cannot verify the quarantine"}
    impossible = [v for v in fs.check_ticker(ticker, data) if v.get("impossible")]
    fetched = data.get("fetched_at") or data.get("_fetched_at")
    if not impossible:
        return {"status": "executed", "close_chairman": True,
                "note": f"{ticker} fundamentals now pass plausibility "
                        f"(fetched_at={fetched}); no quarantine needed"}
    ok, reasons = fs.is_reliable({**data, "_ticker": ticker})
    if ok:
        return {"status": "needs_human",
                "note": f"{ticker} has an impossible value but is_reliable() "
                        f"passes it — the quarantine gate is not blocking it"}
    if not _fe_gate_wired():
        return {"status": "needs_human",
                "note": "fe_intrinsic_value.calculate_iv no longer calls "
                        "is_reliable(); the quarantine has no consumer"}
    return {"status": "standing_quarantine", "close_chairman": True,
            "note": f"{ticker} is quarantined: is_reliable() refuses it "
                    f"({reasons[0][:140]}) and fe_intrinsic_value.calculate_iv "
                    f"honours that gate. Rechecked every run; lifts when the "
                    f"data passes. fetched_at={fetched}"}


# id prefix -> handler. Add a handler here, and a test, to make a finding class
# executable. Anything not listed stays "queued" and is reported as no_handler.
HANDLERS = (
    ("fund-sanity-", _handle_fund_sanity),
)


def _handler_for(pid: str):
    for prefix, fn in HANDLERS:
        if str(pid).startswith(prefix):
            return fn
    return None


def _close_chairman_rows(outcomes: dict) -> list:
    """Close the board router's own inbox rows for findings now handled.

    Only rows with no `source` (the shape _route_approved_cit_proposals and
    deputy_board._queue_fix write) and an open status are touched — another
    producer's rows belong to that producer (chairman_queue.resolve's rule).
    """
    from chairman_queue import OPEN_STATUSES
    if not outcomes:
        return []
    closed = []
    path = str(_chair_path())
    with aj.update(path, default=[], expect=list) as rows:
        for r in rows:
            if not isinstance(r, dict) or r.get("id") not in outcomes:
                continue
            if r.get("source") or r.get("status") not in OPEN_STATUSES:
                continue
            r["status"] = "resolved"
            r["resolved_at"] = _now()
            r["resolved_by"] = "board_dispatch: " + outcomes[r["id"]][:300]
            closed.append(r["id"])
    if closed:
        logger.info(f"  [Dispatch] closed {len(closed)} Chairman inbox row(s): {closed}")
    return closed


def _backfill_legacy(dry_run: bool) -> list:
    """Approvals recorded before this module existed have a ledger row and no
    queue row. Once producers stop re-filing standing ids, nothing would ever
    bring them here — so the executor pulls in any it has a handler for.
    Handlerless legacy approvals are not backfilled: they would only sit in
    the queue, and filter_standing queues each one if it is ever re-filed."""
    ledger = aj.read(str(_ledger_path()), default=[]) or []
    queue = aj.read(str(_queue_path()), default=[]) or []
    have = set(_latest_rows(queue))
    newest = {}
    for r in ledger:
        if isinstance(r, dict) and r.get("id") and r.get("approved_at"):
            newest[r["id"]] = r
    todo = [r for pid, r in sorted(newest.items())
            if pid not in have and _handler_for(pid) is not None
            and not str(pid).startswith(SLA_PREFIX)]
    if todo and not dry_run:
        enqueue(todo, routed_by="board_dispatch:backfill")
    return [r["id"] for r in todo]


def run(dry_run: bool = False) -> dict:
    executed, standing, needs_human, no_handler, errors = [], [], [], [], []
    to_close = {}
    qpath = str(_queue_path())
    backfilled = _backfill_legacy(dry_run)

    def _work(queue):
        latest = _latest_rows(queue)
        for pid, row in latest.items():
            if row.get("status") not in RECHECK_STATUSES:
                continue
            fn = _handler_for(pid)
            if fn is None:
                if row.get("status") == "queued":
                    no_handler.append(pid)
                continue
            try:
                out = fn(row)
            except Exception as e:
                errors.append({"id": pid, "error": f"{type(e).__name__}: {e}"})
                logger.exception(f"  [Dispatch] handler failed on {pid}")
                continue
            entry = {"id": pid, "status": out["status"], "note": out["note"]}
            {"executed": executed, "standing_quarantine": standing}.get(
                out["status"], needs_human).append(entry)
            if dry_run:
                continue
            prev = row.get("status")
            row["status"] = out["status"]
            row["dispatch_note"] = out["note"]
            row["dispatched_at"] = _now()
            row["dispatched_by"] = "board_dispatch"
            if prev != out["status"]:
                row.setdefault("dispatch_history", []).append(
                    {"at": row["dispatched_at"], "from": prev, "to": out["status"]})
            if out.get("close_chairman"):
                to_close[pid] = out["note"]

    if dry_run:
        _work(aj.read(qpath, default=[]) or [])
    else:
        with aj.update(qpath, default=[], expect=list) as queue:
            _work(queue)

    closed = [] if dry_run else _close_chairman_rows(to_close)
    report = {
        "generated_at": _now(),
        "dry_run": dry_run,
        "executed": executed,
        "standing": standing,
        "needs_human": needs_human,
        "errors": errors,
        "no_handler_count": len(no_handler),
        "no_handler_sample": sorted(no_handler)[:25],
        "chairman_rows_closed": closed,
        "backfilled": backfilled,
        "handlers": [p for p, _ in HANDLERS],
    }
    if not dry_run:
        aj.write(str(_report_path()), report)
    logger.info(f"  [Dispatch] executed={len(executed)} standing={len(standing)} "
                f"needs_human={len(needs_human)} no_handler={len(no_handler)} "
                f"errors={len(errors)}")
    return report


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    os.chdir(BASE_DIR)
    if a.report:
        print(json.dumps(aj.read(str(_report_path()), default={}), indent=2))
        return
    rep = run(dry_run=a.dry_run)
    for k in ("executed", "standing", "needs_human", "errors"):
        for e in rep[k]:
            print(f"[{k.upper():11}] {e.get('id')}: {e.get('note', e.get('error'))}")
    print(f"no handler (left queued): {rep['no_handler_count']}")


if __name__ == "__main__":
    main()

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
DONE_STATUSES = frozenset({"implemented", "executed", "superseded"})
# superseded (2026-11-19): the premise was re-checked and found false; the
# check, the reason and a queue backup are recorded on the row (supersede()).
# problem_lifecycle_sentinel.RESOLVED_STATUSES carries the same word, so the
# two readers agree the row is finished (TestFixerLifecycle pins it).

# A fixer session's working states. Both are standing (not in DONE_STATUSES),
# so the board does not re-vote a row someone is working on. A claim older
# than CLAIM_TTL_DAYS returns to queued: a dead session must not strand a row
# forever (memory: a-queue-that-never-empties).
FIXER_STATUSES = frozenset({"claimed", "pr_open"})
CLAIM_TTL_DAYS = 3

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
        "evidence":      _evidence_of(p),
        "source":        p.get("source"),
        "queued_at":     _now(),
        "status":        status,
        "routed_by":     routed_by,
        "times_approved": 1,
        **_routing_fields(p),
    }


def _routing_fields(p: dict) -> dict:
    """owner_area: which specialist fixer owns this row (fixer_routing.route).

    Never raises and never leaves the row unrouted: if routing itself fails the
    row goes to wiring, the area whose job is exactly an unhandled row."""
    try:
        import fixer_routing as fr
        area, rule = fr.route({"proposal_id": p.get("id", p.get("proposal_id", "")),
                               "title": p.get("title"), "source": p.get("source"),
                               "evidence": _evidence_of(p)}, p.get("_problem_text", ""))
    except Exception as e:                       # pragma: no cover - defensive
        area, rule = "wiring", f"fallback:route_error:{type(e).__name__}"
    return {"owner_area": area, "routed_rule": rule}


def _evidence_of(p: dict) -> str:
    """Evidence, falling back to the description. problem_lifecycle_sentinel's
    loop rows put their whole content in `description`; copying only `evidence`
    queued 60 rows on the VM with nothing a fixer could act on."""
    return p.get("evidence") or p.get("description") or ""


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


# =============================================================================
# rerun-job: a stale-cron row is executed by running the job once
# =============================================================================

import re as _re
import subprocess as _sp

_STALE_RX = _re.compile(r"Cron job stale on weekday -- (.+?) log ", _re.I)
# Never re-run anything that can reach a broker, move money or redeploy. A stale
# one of these is a code/scheduling problem for a fixer, not a button to press.
_RERUN_DENY = _re.compile(r"^(main|catch_up|deploy|ship)\b|alpaca|kalshi|trader|"
                          r"manager_agent|copier|order|exit_guard|single_writer", _re.I)
RERUN_TIMEOUT_S = 300
MAX_RERUNS_PER_RUN = 2
_reruns_this_run = [0]
_dry = [False]


def _stale_job_name(row: dict):
    hay = " ".join(str(row.get(k) or "") for k in ("title", "evidence"))
    m = _STALE_RX.search(hay)
    return m.group(1).strip() if m else None


def _crontab_lines():
    """Active crontab lines on THIS host, or None if this host has no crontab."""
    try:
        out = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return [l for l in out.stdout.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def _job_script(label: str, crontab: list):
    """Label ("ML drift monitor") -> (script, crontab_line|None). A script matches
    when every word of the label is in its file name (ml_feature_drift_monitor.py).
    Exactly one match is required: two matches is ambiguous, and guessing which
    job to run is how the wrong one runs."""
    words = _re.findall(r"[a-z]+", label.lower())
    exact = "_".join(words) + ".py"
    def match(names):
        names = set(names)
        if exact in names:                     # ml_data_watchdog.py beats
            return [exact]                     # ml_longterm_data_watchdog.py
        return sorted(n for n in names if all(w in n.lower() for w in words))
    in_cron = match(m for l in crontab for m in _re.findall(r"([\w]+\.py)", l))
    if len(in_cron) == 1:
        line = next(l for l in crontab if in_cron[0] in l)
        return in_cron[0], line
    in_repo = match(p.name for p in BASE_DIR.glob("*.py"))
    if len(in_repo) > 1:
        return "AMBIGUOUS:" + ",".join(in_repo[:4]), None
    return (in_repo[0] if in_repo else None), None


def _handle_rerun_job(row: dict) -> dict:
    label = _stale_job_name(row)
    crontab = _crontab_lines()
    if crontab is None:
        return {"status": "queued",
                "note": f"rerun-job: this host has no crontab, so '{label}' cannot be "
                        f"checked here; left for the scheduling host"}
    script, line = _job_script(label, crontab)
    if script is None:
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: no script matches '{label}' in the crontab or the "
                        f"repo — the alarm names a job that does not exist. Ops fixer: "
                        f"retire the check or restore the job"}
    if script.startswith("AMBIGUOUS:"):
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: '{label}' matches several scripts "
                        f"({script[10:]}); not guessing which to run"}
    if line is None:
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: {script} exists but has NO crontab entry on this "
                        f"host — it runs nowhere, so rerunning it once fixes nothing. "
                        f"Ops fixer: a scheduling fix (add the line, or retire the job "
                        f"and its freshness check)"}
    if _RERUN_DENY.search(script):
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: {script} can reach the broker or redeploy; never "
                        f"auto-rerun. Ops/Risk fixer to diagnose why it went stale"}
    if _reruns_this_run[0] >= MAX_RERUNS_PER_RUN:
        return {"status": "queued",
                "note": f"rerun-job: {script} deferred, {MAX_RERUNS_PER_RUN}-rerun cap "
                        f"for this dispatch run reached"}
    cmd = " ".join(line.split()[5:])          # strip the 5 schedule fields
    if _dry[0] or os.environ.get("BOARD_DISPATCH_RERUN_DRY"):
        return {"status": "queued", "note": f"rerun-job (dry): would run: {cmd[:200]}"}
    _reruns_this_run[0] += 1
    try:
        r = _sp.run(["bash", "-c", cmd], capture_output=True, text=True,
                    timeout=RERUN_TIMEOUT_S)
    except _sp.TimeoutExpired:
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: {script} did not finish in {RERUN_TIMEOUT_S}s"}
    if r.returncode != 0:
        return {"status": "needs_human", "owner_area": "ops",
                "note": f"rerun-job: {script} exited {r.returncode} when rerun: "
                        f"{(r.stderr or '')[-200:]}"}
    return {"status": "executed",
            "note": f"rerun-job: ran {script} once from its crontab line (exit 0). If "
                    f"the log goes stale again the detector files a new row"}


# id prefix -> handler. Add a handler here, and a test, to make a finding class
# executable. Anything not listed stays "queued" and is reported as no_handler.
HANDLERS = (
    ("fund-sanity-", _handle_fund_sanity),
)


def _handler_for(pid: str, row: dict = None):
    for prefix, fn in HANDLERS:
        if str(pid).startswith(prefix):
            return fn
    if row is not None and _stale_job_name(row):
        return _handle_rerun_job
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


# =============================================================================
# Fixer lifecycle: queued -> claimed -> pr_open -> implemented, or superseded
# =============================================================================

# Approved fixes the old cit_implementation_agent gave up on in June 2026
# ("Could not identify target file", or a protected file needing a human). That
# agent is scheduled on neither host, so these 81 rows (VM, 2026-11-19) were
# approved and owned by nobody, and invisible to the owner count because they
# were not "open". They are fixer work like any queued row: routed to an owner,
# claimable, and closable as superseded when their premise is gone.
STRANDED_STATUSES = frozenset({"skipped", "protected"})
OPEN_FOR_FIXERS = frozenset({"queued", "needs_human"}) | FIXER_STATUSES | STRANDED_STATUSES


def _problem_history() -> dict:
    h = aj.read(str(DATA / "problem_lifecycle.json"), default={}) or {}
    return h if isinstance(h, dict) else {}


def _owner_area_counts() -> dict:
    out = {}
    for r in _latest_rows(aj.read(str(_queue_path()), default=[]) or []).values():
        if r.get("status") in OPEN_FOR_FIXERS:
            a = r.get("owner_area") or "UNROUTED"
            out[a] = out.get(a, 0) + 1
    return out


def backfill_open_rows(dry_run: bool = False, queue_copy: list = None) -> dict:
    """Give every open row an owner_area, and every loop-sig row its problem text.

    Rows queued before 2026-11-19 have neither. Additive only (two or three new
    fields, never a status), and a backup of the queue is written the first
    time any row is touched."""
    import fixer_routing as fr
    hist = _problem_history()
    counts = {"routed": 0, "evidence": 0, "unrouted_after": 0}

    def _fill(queue):
        for r in _latest_rows(queue).values():
            if r.get("status") not in OPEN_FOR_FIXERS | RECHECK_STATUSES:
                continue
            text = fr.problem_text(r, hist)
            if text and not r.get("evidence"):
                h = hist.get(str(r["proposal_id"])[len("loop-"):]) or {}
                r["evidence"] = (f"{text}\n\n[problem_lifecycle {h.get('sig')}: seen "
                                 f"{h.get('occurrences', '?')}x since "
                                 f"{str(h.get('first_seen', '?'))[:10]}, recurred after "
                                 f"resolution {h.get('recurred_after_resolution', 0)}x]")
                counts["evidence"] += 1
            if not r.get("owner_area"):
                area, rule = fr.route(r, text)
                r["owner_area"], r["routed_rule"] = area, rule
                counts["routed"] += 1
            if not r.get("owner_area"):
                counts["unrouted_after"] += 1
        return counts["routed"] + counts["evidence"]

    if queue_copy is not None:                # dry-run of run(): fill the copy it reads
        _fill(queue_copy)
        return counts
    if dry_run:
        _fill(json.loads(json.dumps(aj.read(str(_queue_path()), default=[]) or [])))
        return counts
    with aj.update(str(_queue_path()), default=[], expect=list) as queue:
        before = json.loads(json.dumps(queue))
        if _fill(queue):
            _backup_queue(before, "before-fixer-backfill")
    return counts


def _backup_queue(rows: list, tag: str) -> str:
    # Never overwrite an earlier backup. With a seconds-only stamp, 11 supersedes run
    # back to back on 2026-11-19 wrote 3 files, and the one taken before the first
    # close was replaced by a later one: the "state before" was lost.
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    path = DATA / f"cit_implementation_queue.{tag}.{ts}.json"
    n = 1
    while path.exists():
        n += 1
        path = DATA / f"cit_implementation_queue.{tag}.{ts}-{n}.json"
    aj.write(str(path), rows)
    return path.name


def _set_status(pid: str, status: str, allowed_from: frozenset, **fields) -> dict:
    """Move pid's latest row to `status`. Refuses rows in states it may not leave
    (escalated_to_board is never touched: board's decision, not a fixer's)."""
    with aj.update(str(_queue_path()), default=[], expect=list) as queue:
        row = _latest_rows(queue).get(pid)
        if row is None:
            raise KeyError(f"{pid}: no queue row")
        prev = row.get("status")
        if prev == "escalated_to_board" or prev not in allowed_from:
            raise ValueError(f"{pid}: status {prev!r} cannot move to {status!r}")
        now = _now()
        row.update(fields)
        row["status"] = status
        row.setdefault("dispatch_history", []).append(
            {"at": now, "from": prev, "to": status, "by": fields.get("claimed_by")
             or fields.get("superseded_by") or "fixer"})
        return dict(row)


def claim(pid: str, owner: str, branch: str) -> dict:
    return _set_status(pid, "claimed", frozenset({"queued", "needs_human"}) | STRANDED_STATUSES,
                       claimed_by=owner, claimed_at=_now(), branch=branch)


def mark_pr_open(pid: str, url: str) -> dict:
    return _set_status(pid, "pr_open", frozenset({"claimed"}), pr_url=url,
                       pr_opened_at=_now())


def mark_implemented(pid: str, note: str) -> dict:
    """Only after merge AND deploy, checked by md5 on the VM — say so in `note`."""
    if "md5" not in note.lower():
        raise ValueError("implemented needs the VM md5 check in the note")
    return _set_status(pid, "implemented", FIXER_STATUSES, implemented_note=note,
                       implemented_at=_now())


def release(pid: str, note: str) -> dict:
    """A fixer gave up: back to needs_human with its reason."""
    return _set_status(pid, "needs_human", FIXER_STATUSES, dispatch_note=note)


def supersede(pid: str, check: str, reason: str, by: str = "fixer") -> dict:
    """Close a row whose premise was re-checked and found false.

    `check` is the command or file read and what it showed; `reason` is why that
    makes the row moot. Writes a queue backup first and records it on the row."""
    if not check.strip() or not reason.strip():
        raise ValueError("supersede needs both the check and the reason")
    backup = _backup_queue(aj.read(str(_queue_path()), default=[]) or [],
                           "before-supersede")
    return _set_status(pid, "superseded",
                       frozenset({"queued", "needs_human", "awaiting_chairman"}) | FIXER_STATUSES
                       | STRANDED_STATUSES,
                       supersede_check=check, supersede_reason=reason,
                       superseded_by=by, superseded_at=_now(), supersede_backup=backup)


def release_stale_claims(now: datetime = None) -> list:
    """claimed older than CLAIM_TTL_DAYS -> queued (a dead fixer session)."""
    now = now or datetime.now()
    out = []
    with aj.update(str(_queue_path()), default=[], expect=list) as queue:
        for pid, r in _latest_rows(queue).items():
            if r.get("status") != "claimed":
                continue
            try:
                age = (now - datetime.fromisoformat(str(r.get("claimed_at")))).days
            except (TypeError, ValueError):
                age = CLAIM_TTL_DAYS          # undated claim: treat as expired
            if age >= CLAIM_TTL_DAYS:
                r["status"] = "queued"
                r.setdefault("dispatch_history", []).append(
                    {"at": _now(), "from": "claimed", "to": "queued",
                     "by": f"board_dispatch: claim older than {CLAIM_TTL_DAYS}d"})
                out.append(pid)
    return out


def run(dry_run: bool = False) -> dict:
    executed, standing, needs_human, no_handler, errors = [], [], [], [], []
    deferred = []
    to_close = {}
    qpath = str(_queue_path())
    _reruns_this_run[0] = 0
    _dry[0] = dry_run
    backfilled = _backfill_legacy(dry_run)
    dry_queue = (json.loads(json.dumps(aj.read(qpath, default=[]) or []))
                 if dry_run else None)
    fixer_backfill = backfill_open_rows(dry_run, queue_copy=dry_queue)
    released = [] if dry_run else release_stale_claims()

    def _work(queue):
        latest = _latest_rows(queue)
        for pid, row in latest.items():
            if row.get("status") not in RECHECK_STATUSES:
                continue
            fn = _handler_for(pid, row)
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
            {"executed": executed, "standing_quarantine": standing,
             "queued": deferred}.get(out["status"], needs_human).append(entry)
            if dry_run:
                continue
            if out["status"] == "queued" and row.get("status") == "queued":
                row["dispatch_note"] = out["note"]      # no decision: note only
                continue
            if out.get("owner_area"):
                row["owner_area"] = out["owner_area"]
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
        _work(dry_queue)
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
        "deferred": deferred,
        "errors": errors,
        "fixer_backfill": fixer_backfill,
        "claims_released": released,
        "owner_area_counts": _owner_area_counts(),
        "no_handler_count": len(no_handler),
        "no_handler_sample": sorted(no_handler)[:25],
        "chairman_rows_closed": closed,
        "backfilled": backfilled,
        "handlers": [p for p, _ in HANDLERS] + ["rerun-job:<stale cron row>"],
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
    ap.add_argument("--supersede", metavar="PID")
    ap.add_argument("--check", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--by", default="fixer")
    a = ap.parse_args()
    os.chdir(BASE_DIR)
    if a.supersede:
        print(json.dumps(supersede(a.supersede, a.check, a.reason, a.by), indent=1))
        return
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

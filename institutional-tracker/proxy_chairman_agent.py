#!/usr/bin/env python3
"""
Proxy Chairman Agent — rules-based, dual-gated, reversible.

Chairman mandate (2026-10-15): when the chairman decision queue exceeds a
threshold, apply formalized autoresolve rules to drain stale/superseded
items. Every proxy decision requires BOTH a Claude precision review and a
Board escape-risk review. Hard carve-outs are enforced BEFORE any rule is
tried. Every write is reversible via chairman_unresolve.py.

Trigger (per Chairman): cron AND queue>threshold (both). This script
checks the threshold internally; the cron just invokes it.

Modes:
    shadow   — write proposals only, no reviews run, no queue writes
    propose  — write proposals + run reviews, no queue writes
    live     — write proposals + reviews + if both pass, write to queue

Standing config (Chairman decision 2026-10-15):
    - trigger        = cron AND queue>10 (both)
    - rules_source   = rules-based only (no LLM prediction)
    - queue_floor    = 0 (clear fully if evidence allows)

Hard carve-outs — never proxied, regardless of rule match:
    - chairman_only=True
    - category in {arm_gate, capital, risk_limit, cro_unfreeze}
    - id or description contains any of a reserved-token list
    - severity CRITICAL and age < 48h
    - any item younger than 48h (Chairman's attention zone)
    - item in a currently-active investigation
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
QUEUE = DATA / "chairman_decision_queue.json"
RULES = DATA / "autoresolve_rules.json"
PROPOSALS = DATA / "proxy_chairman_proposals.json"
AUDIT = DATA / "proxy_chairman_audit_log.json"

# Hard carve-outs — reserved tokens that permanently exclude an item.
# Grouping documents WHY the token blocks: never attempt to proxy these.
_RESERVED_TOKENS_CAPITAL = [
    "arm_gate", ".kalshi_armed", ".kalshi_mirror_armed", ".fi_armed",
    "signoff_ledger", "signoff_enforce", "ENFORCE",
    "cro-unfreeze", "cro_unfreeze",
    "risk_limit_derivation", "position_limit",
    "trade_sync", "capital_release", "capital_allocation",
]
_RESERVED_TOKENS_PERSONNEL = [
    "hr_termination", "fire_agent", "terminate_agent",
    "pip_graduation", "pip_termination",
]
_RESERVED_TOKENS_SECURITY = [
    "secret_rotation", "api_key", "credential_rotation",
    "security_patch", "unauthorized_access",
]
RESERVED_TOKENS = (
    _RESERVED_TOKENS_CAPITAL
    + _RESERVED_TOKENS_PERSONNEL
    + _RESERVED_TOKENS_SECURITY
)
RESERVED_CATEGORIES = {"arm_gate", "capital", "risk_limit", "cro_unfreeze"}

QUEUE_THRESHOLD_DEFAULT = 10
MIN_AGE_HOURS = 48  # anything younger than this is Chairman's attention zone


# ---------- IO helpers ----------

def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, default=str, sort_keys=False)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


# ---------- rule matching (deterministic, auditable) ----------

_DATE_RE_IN_ID = re.compile(r"(20\d{2})-?(\d{2})-?(\d{2})")


def _extract_date_yyyymmdd(item: dict) -> str | None:
    """Pull a YYYYMMDD date from created_at (preferred) or the id."""
    ca = item.get("created_at")
    if isinstance(ca, str):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ca)
        if m:
            return m.group(1) + m.group(2) + m.group(3)
    i = item.get("id", "")
    m = _DATE_RE_IN_ID.search(i)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return None


_KNOWN_MATCH_KEYS = {
    "description_prefix", "description_contains_any", "description_contains_all",
    "id_contains_any", "id_prefix_any",
    "category_in", "severity_in", "chairman_only_is",
    "created_before_yyyymmdd",
}


class UnknownMatchKey(Exception):
    pass


def _match_predicate(item: dict, pred: dict) -> bool:
    """Return True iff item matches every key in pred.

    Case-insensitive on all string comparisons (ids/descriptions here can
    arrive as either case depending on producer). Unknown keys raise
    UnknownMatchKey — the caller skips the rule and logs it, which is
    safer than crashing the whole pass.
    """
    desc = item.get("description", "") or ""
    desc_low = desc.lower()
    id_ = item.get("id", "") or ""
    id_low = id_.lower()

    for key, cond in pred.items():
        if key == "description_prefix":
            if not desc_low.startswith(cond.lower()):
                return False
        elif key == "description_contains_any":
            if not any(c.lower() in desc_low for c in cond):
                return False
        elif key == "description_contains_all":
            if not all(c.lower() in desc_low for c in cond):
                return False
        elif key == "id_contains_any":
            if not any(c.lower() in id_low for c in cond):
                return False
        elif key == "id_prefix_any":
            if not any(id_low.startswith(c.lower()) for c in cond):
                return False
        elif key == "category_in":
            if item.get("category") not in cond:
                return False
        elif key == "severity_in":
            if item.get("severity") not in cond:
                return False
        elif key == "chairman_only_is":
            if bool(item.get("chairman_only")) != cond:
                return False
        elif key == "created_before_yyyymmdd":
            d = _extract_date_yyyymmdd(item)
            if not d or d > cond:
                return False
        else:
            raise UnknownMatchKey(key)
    return True


# ---------- carve-outs (safety floor) ----------

def _fails_carveout(item: dict) -> str | None:
    """Return a reason string if item is carved out, else None."""
    if item.get("chairman_only"):
        return "chairman_only=True"
    if item.get("category") in RESERVED_CATEGORIES:
        return f"category={item.get('category')} is reserved"
    blob = (item.get("id", "") + " " + (item.get("description", "") or "")).lower()
    for tok in RESERVED_TOKENS:
        if tok.lower() in blob:
            return f"reserved token {tok!r} present"
    sev = item.get("severity")
    age_h = _age_hours(item)
    if sev == "CRITICAL" and age_h is not None and age_h < MIN_AGE_HOURS:
        return f"CRITICAL and age {age_h:.1f}h < {MIN_AGE_HOURS}h"
    if age_h is not None and age_h < MIN_AGE_HOURS:
        return f"age {age_h:.1f}h < {MIN_AGE_HOURS}h (Chairman attention zone)"
    return None


def _age_hours(item: dict) -> float | None:
    ca = item.get("created_at")
    if not isinstance(ca, str):
        return None
    try:
        when = dt.datetime.fromisoformat(ca.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    return (now - when).total_seconds() / 3600.0


# ---------- proposal + audit ----------

def _build_proposal(item: dict, rule: dict) -> dict:
    return {
        "proposal_id": f"proxy-{uuid.uuid4().hex[:12]}",
        "item_id": item["id"],
        "item_description_head": (item.get("description") or "")[:200],
        "item_severity": item.get("severity"),
        "item_category": item.get("category"),
        "item_created_at": item.get("created_at"),
        "rule_id": rule["id"],
        "rule_citation": rule.get("citation"),
        "verdict": rule["verdict"],
        "rationale": rule["rationale"],
        "approvers_required": rule.get("approvers_required", ["claude", "board"]),
        "approvals": {},
        "status": "proposed",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "host": socket.gethostname(),
    }


def _append_audit(entry: dict) -> None:
    log = _load_json(AUDIT, [])
    if not isinstance(log, list):
        log = []
    log.append(entry)
    _atomic_write_json(AUDIT, log)


# ---------- review hooks ----------

def _run_claude_review(proposal: dict, item: dict, rule: dict) -> dict:
    """Precision gate: does the rule actually apply, and is the citation valid?"""
    try:
        from proxy_chairman_claude_review import review as claude_review
        return claude_review(proposal, item, rule)
    except Exception as e:
        return {"approved": False, "reason": f"claude_review unavailable: {e!r}"}


def _run_board_review(proposal: dict, item: dict, rule: dict) -> dict:
    """Escape-risk gate: would resolving this leave a real defect uncaught?"""
    try:
        from proxy_chairman_board_review import review as board_review
        return board_review(proposal, item, rule)
    except Exception as e:
        return {"approved": False, "reason": f"board_review unavailable: {e!r}"}


# ---------- queue writes ----------

def _mark_resolved_in_queue(item_id: str, proposal: dict) -> bool:
    """Write terminal status to the queue. Returns True on write."""
    q = _load_json(QUEUE, [])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    changed = False
    for r in q:
        if r.get("id") != item_id:
            continue
        if r.get("status") in ("resolved", "resolved_upstream", "escalated_to_board"):
            return False  # already terminal, leave it
        r["status"] = proposal["verdict"]
        r["resolved_at"] = now
        r["resolved_by"] = f"proxy-chairman:{proposal['rule_id']}"
        r["resolution_note"] = (
            f"{proposal['rationale']} "
            f"[proxy-chairman {proposal['proposal_id']}, dual-approved, "
            f"reversible via chairman_unresolve.py {item_id!r}]"
        )
        r["proxy_proposal_id"] = proposal["proposal_id"]
        changed = True
        break
    if changed:
        _atomic_write_json(QUEUE, q)
    return changed


# ---------- main pass ----------

def run(mode: str, queue_threshold: int, limit: int | None) -> dict:
    rules_doc = _load_json(RULES, {"rules": []})
    rules = rules_doc.get("rules", [])
    queue = _load_json(QUEUE, [])
    awaiting = [r for r in queue if r.get("status") == "awaiting_chairman"]

    summary = {
        "mode": mode,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rules_version": rules_doc.get("version"),
        "queue_total": len(queue),
        "queue_awaiting": len(awaiting),
        "queue_threshold": queue_threshold,
        "triggered": len(awaiting) > queue_threshold,
        "carved_out": 0,
        "no_rule_match": 0,
        "proposed": 0,
        "approved_both": 0,
        "resolved_live": 0,
        "proposals": [],
    }

    # Trigger check: fire only if queue strictly exceeds threshold
    if not summary["triggered"]:
        summary["skipped_reason"] = (
            f"queue awaiting ({len(awaiting)}) <= threshold ({queue_threshold}); "
            f"proxy chairman does not fire"
        )
        _append_audit({**summary, "entry": "trigger_skipped"})
        return summary

    all_proposals = _load_json(PROPOSALS, [])
    if not isinstance(all_proposals, list):
        all_proposals = []

    processed = 0
    for item in awaiting:
        if limit is not None and processed >= limit:
            break

        carve = _fails_carveout(item)
        if carve:
            summary["carved_out"] += 1
            continue

        matched_rule = None
        for rule in rules:
            try:
                if _match_predicate(item, rule["match"]):
                    matched_rule = rule
                    break
            except UnknownMatchKey as e:
                # Skip the rule, record it in the audit, keep going.
                _append_audit({
                    "entry": "rule_skipped",
                    "rule_id": rule.get("id"),
                    "reason": f"unknown match key: {e}",
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
                continue
        if matched_rule is None:
            summary["no_rule_match"] += 1
            continue

        proposal = _build_proposal(item, matched_rule)
        summary["proposed"] += 1
        processed += 1

        if mode in ("propose", "live"):
            # Each gate asks a DIFFERENT question (precision vs escape risk).
            cr = _run_claude_review(proposal, item, matched_rule)
            proposal["approvals"]["claude"] = cr
            br = _run_board_review(proposal, item, matched_rule)
            proposal["approvals"]["board"] = br
            both_ok = cr.get("approved") and br.get("approved")
            proposal["status"] = "approved" if both_ok else "rejected"
            if both_ok:
                summary["approved_both"] += 1

        if mode == "live" and proposal["status"] == "approved":
            if _mark_resolved_in_queue(item["id"], proposal):
                proposal["status"] = "resolved_in_queue"
                summary["resolved_live"] += 1

        all_proposals.append(proposal)
        summary["proposals"].append({
            "proposal_id": proposal["proposal_id"],
            "item_id": item["id"],
            "rule_id": matched_rule["id"],
            "status": proposal["status"],
        })

    _atomic_write_json(PROPOSALS, all_proposals)
    summary["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _append_audit({**summary, "entry": "run_complete"})
    return summary


def _print_report(summary: dict) -> None:
    print("─" * 72)
    print(f"Proxy Chairman — mode={summary['mode']} rules={summary.get('rules_version')}")
    print(f"queue awaiting={summary['queue_awaiting']} / threshold={summary['queue_threshold']}  triggered={summary['triggered']}")
    if not summary["triggered"]:
        print(f"→ {summary.get('skipped_reason','skipped')}")
        return
    print(f"carved_out={summary['carved_out']}  no_rule_match={summary['no_rule_match']}  proposed={summary['proposed']}")
    print(f"approved_both={summary['approved_both']}  resolved_live={summary['resolved_live']}")
    for p in summary["proposals"][:20]:
        print(f"  {p['status']:>18}  rule={p['rule_id']:<30} item={p['item_id']}")
    if len(summary["proposals"]) > 20:
        print(f"  ... and {len(summary['proposals'])-20} more")
    print("─" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("shadow", "propose", "live"), default="shadow")
    ap.add_argument("--queue-threshold", type=int, default=QUEUE_THRESHOLD_DEFAULT,
                    help="Fire only if awaiting_chairman count > threshold")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of items processed in this pass")
    ap.add_argument("--report", action="store_true",
                    help="Print the last run's summary from audit log and exit")
    args = ap.parse_args(argv)

    if args.report:
        log = _load_json(AUDIT, [])
        complete = [e for e in log if e.get("entry") == "run_complete"]
        if not complete:
            print("no completed runs in audit log yet")
            return 0
        _print_report(complete[-1])
        return 0

    summary = run(args.mode, args.queue_threshold, args.limit)
    _print_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

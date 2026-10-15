#!/usr/bin/env python3
"""
Reverse a proxy-chairman (or any) resolution on the chairman decision queue.

    python3 chairman_unresolve.py <item-id> [--reason "..."]

Any item that was closed by proxy-chairman can be popped back to
awaiting_chairman with one command. The original resolution_note is
preserved in a reopen_history list so the audit trail stays intact.
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
QUEUE = DATA / "chairman_decision_queue.json"
AUDIT = DATA / "proxy_chairman_audit_log.json"


def _atomic_write_json(path: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def unresolve(item_id: str, reason: str) -> bool:
    if not QUEUE.exists():
        print(f"queue not found: {QUEUE}", file=sys.stderr)
        return False
    with open(QUEUE) as f:
        q = json.load(f)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    hit = False
    for r in q:
        if r.get("id") != item_id:
            continue
        prior = {
            "at": now,
            "prior_status": r.get("status"),
            "prior_resolved_at": r.get("resolved_at"),
            "prior_resolved_by": r.get("resolved_by"),
            "prior_note": r.get("resolution_note"),
            "reopen_reason": reason,
        }
        r.setdefault("reopen_history", []).append(prior)
        r["status"] = "awaiting_chairman"
        for k in ("resolved_at", "resolved_by", "resolution_note", "proxy_proposal_id"):
            r.pop(k, None)
        hit = True
        break
    if not hit:
        print(f"item not found: {item_id}", file=sys.stderr)
        return False
    _atomic_write_json(QUEUE, q)
    # Audit entry
    log = []
    if AUDIT.exists():
        try: log = json.load(open(AUDIT))
        except Exception: log = []
    if not isinstance(log, list):
        log = []
    log.append({"entry": "unresolve", "item_id": item_id, "reason": reason, "at": now})
    _atomic_write_json(AUDIT, log)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("item_id")
    ap.add_argument("--reason", default="(none given)")
    args = ap.parse_args(argv)
    ok = unresolve(args.item_id, args.reason)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

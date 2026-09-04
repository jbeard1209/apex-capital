"""
Two-tier log retention. Runs daily on the VM (cron entry added 2026-09-04).

The rule the Chairman set on 2026-09-04: "shorten the time except for logs
considered important." A log is important when it is the paper trail for a
decision or an incident — board minutes, controls audits, coherence checks,
risk governance, HR investigations, chairman/SLA — and we want to be able to
answer "what did we do on that day" months later. Everything else is a run
diagnostic and older copies just consume disk once the day is over.

The classification lives here (never in a wildcard cron) so a new important
log added to the schedule is a code change with a diff, not a silent inclusion
in a routine bucket.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent / "logs"

IMPORTANT_PREFIXES = (
    "board_",
    "cit_",
    "cit_pod_audit_",
    "coherence_",
    "chairman_sla_",
    "council_",
    "hr_",
    "hr_retire_audit_",
    "hr_surveillance_",
    "irm_daily_",
    "lifecycle_",
    "logic_team_",
    "recovery_",
    "risk_governor_",
    "risk_limits_",
    "risk_manager_",
    "redteam_",
    "sentinel_",
    "model_test_",
    "promotion_gate",
    "param_audit_",
    "pipeline_integrity_",
    "data_integrity_",
    "metric_sanity_",
    "kalshi_",
)
IMPORTANT_DAYS = 90
ROUTINE_DAYS = 30

DATED = re.compile(r"_(\d{8})(?:_\d+)?\.log$")


def _is_important(name: str) -> bool:
    return any(name.startswith(p) for p in IMPORTANT_PREFIXES)


def _age_days(path: Path, now_epoch: float) -> float:
    return (now_epoch - path.stat().st_mtime) / 86400.0


def sweep(dry_run: bool = False) -> dict:
    """Delete log files older than their tier. Returns a summary dict."""
    now = time.time()
    kept = 0
    removed_important = 0
    removed_routine = 0
    freed_bytes = 0

    for path in LOGS_DIR.glob("*.log"):
        name = path.name
        # Only touch dated logs — a live handle without a date gets left alone.
        if not DATED.search(name):
            kept += 1
            continue
        age = _age_days(path, now)
        cap = IMPORTANT_DAYS if _is_important(name) else ROUTINE_DAYS
        if age <= cap:
            kept += 1
            continue
        size = path.stat().st_size
        if not dry_run:
            path.unlink()
        freed_bytes += size
        if _is_important(name):
            removed_important += 1
        else:
            removed_routine += 1

    return {
        "kept": kept,
        "removed_important": removed_important,
        "removed_routine": removed_routine,
        "freed_bytes": freed_bytes,
        "important_days": IMPORTANT_DAYS,
        "routine_days": ROUTINE_DAYS,
    }


if __name__ == "__main__":
    import json
    import sys

    dry = "--dry-run" in sys.argv
    result = sweep(dry_run=dry)
    result["ts"] = int(time.time())
    result["dry_run"] = dry
    print(json.dumps(result, indent=2))
